"""Swarm Worker: standalone worker execution engine with a lightweight ReAct loop.

Uses ChatLLM.chat + manual for-loop directly (without instantiating AgentLoop),
keeping the worker self-contained and the agent core unchanged.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.agent.context import ContextBuilder
from src.agent.progress import HeartbeatTimer
from src.agent.skills import SkillsLoader
from src.agent.tools import ToolRegistry
from src.config.schema import AgentConfig
from src.providers.chat import ChatLLM, LLMResponse, ProviderStreamError
from src.providers.content_filter import (
    CONTENT_FILTER_SKIP_MESSAGE,
    MAX_CONSECUTIVE_CONTENT_FILTER_SKIPS,
    compute_content_filter_warnings,
)
from src.swarm.models import (
    SwarmAgentSpec,
    SwarmEvent,
    SwarmTask,
    WorkerResult,
)
from src.tools import build_swarm_registry
from src.tools.mcp import MCPRemoteTool
from src.tools.redaction import is_sensitive_arg, redact_payload

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ITERATIONS = int(os.getenv("SWARM_WORKER_MAX_ITER", "50"))
_DEFAULT_TIMEOUT_SECONDS = int(os.getenv("SWARM_WORKER_TIMEOUT", "300"))


def _heartbeat_interval_s() -> float:
    """Resolve the heartbeat tick interval from env, robust to garbage values.

    Matches the parsing discipline in :func:`SwarmStore.compute_stale_threshold`
    — both sides use the same env var, so they must fail the same way. A bad
    value (``"abc"``, empty) falls back to 3.0s instead of crashing import.
    """
    try:
        return float(os.getenv("SWARM_HEARTBEAT_INTERVAL_S", "3.0"))
    except ValueError:
        return 3.0


def _stream_retry_delay_s() -> float:
    """Resolve the base delay before a stream retry, robust to garbage.

    Returns:
        Seconds to sleep before the first ``stream_chat`` retry (subsequent
        retries back off from this base). Configurable via
        ``SWARM_STREAM_RETRY_DELAY_S``; a bad value falls back to 1.0s
        instead of crashing import.
    """
    try:
        return float(os.getenv("SWARM_STREAM_RETRY_DELAY_S", "1.0"))
    except ValueError:
        return 1.0


def _stream_max_retries() -> int:
    """Resolve the max number of stream retries, robust to garbage."""
    try:
        return int(os.getenv("SWARM_STREAM_MAX_RETRIES", "3"))
    except ValueError:
        return 3


def _stream_retry_backoff() -> float:
    """Resolve the exponential backoff multiplier, robust to garbage."""
    try:
        return float(os.getenv("SWARM_STREAM_RETRY_BACKOFF", "2.0"))
    except ValueError:
        return 2.0


_HEARTBEAT_INTERVAL_S = _heartbeat_interval_s()
_STREAM_RETRY_DELAY_S = _stream_retry_delay_s()
_STREAM_MAX_RETRIES = _stream_max_retries()
_STREAM_RETRY_BACKOFF = _stream_retry_backoff()
_MAX_TOKEN_ESTIMATE = 60_000
# Tool-result content is bounded at first insertion (not just by the later
# microcompaction pass) so a single load_skill/get_market_data payload can't
# by itself dominate the transcript before the 3-most-recent window clears it.
_MAX_TOOL_RESULT_CHARS = 4_000
# Assistant tool-call argument values (write_file/edit_file bodies, generated
# scripts, large JSON payloads) are bounded the same way when the assistant
# message is stored for history — the full, untruncated arguments are still
# used for actual tool execution below.
_MAX_TOOL_ARG_CHARS = 1_000

class _FairLock:
    """A mutex that grants waiters strict FIFO ownership.

    ``threading.Lock`` gives no ordering guarantee among waiters: under fast,
    uneven contention (one agent's iterations consistently quicker than
    another's), the same thread can keep winning re-acquisition races
    indefinitely, starving the other — observed directly in swarm runs where
    one agent cycled through several iterations while its sibling sat idle
    waiting for the lock. This ticket-queue lock instead hands ownership
    directly from the releaser to the longest-waiting thread, so no waiter
    can be skipped regardless of how fast others cycle.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owned = False
        self._waiters: collections.deque[threading.Event] = collections.deque()

    def acquire(self, timeout: float) -> bool:
        """Acquire the lock, waiting in FIFO order up to ``timeout`` seconds."""
        event = threading.Event()
        with self._guard:
            if not self._owned and not self._waiters:
                self._owned = True
                return True
            self._waiters.append(event)
        granted = event.wait(timeout)
        with self._guard:
            if granted:
                return True
            try:
                self._waiters.remove(event)
            except ValueError:
                # release() already popped us and set the event concurrently
                # with our timeout firing — we did get ownership.
                return True
            return False

    def release(self) -> None:
        """Release the lock, handing off directly to the next FIFO waiter."""
        with self._guard:
            if self._waiters:
                # Ownership transfers straight to the next waiter; `_owned`
                # stays True so no fresh acquire() can jump the queue.
                self._waiters.popleft().set()
            else:
                self._owned = False


# Two swarm workers calling a single local Ollama instance concurrently were
# observed thrashing: a partially CPU-offloaded model with a large per-slot
# KV cache (num_ctx) can't serve two concurrent conversations without one
# starving the other — single calls stalled 500s+ even with bounded prompt
# size. Hosted providers have no such local resource contention, so only
# ollama calls are serialized through this process-wide lock.
_OLLAMA_CALL_LOCK = _FairLock()


def _is_ollama_provider() -> bool:
    """Return True when the active LLM provider is local Ollama."""
    return os.getenv("LANGCHAIN_PROVIDER", "").strip().lower() == "ollama"


def _truncate_for_history(text: str, limit: int) -> str:
    """Truncate a string for transcript storage, marking what was cut.

    Args:
        text: Original string.
        limit: Maximum length to keep.

    Returns:
        ``text`` unchanged if within ``limit``, else a truncated prefix with
        a ``[...N chars truncated]`` marker appended.
    """
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return f"{text[:limit]}...[{cut} chars truncated]"


def _bounded_tool_calls_for_history(tool_calls: list) -> list:
    """Return a copy of ``tool_calls`` with large argument values truncated.

    Only string-valued arguments are truncated (e.g. ``write_file``'s
    ``content``); other argument types pass through unchanged. Truncation
    happens once, at the point the assistant message is first stored, so the
    transcript prefix stays stable across later iterations instead of being
    edited retroactively.

    Args:
        tool_calls: Tool calls from the LLM response.

    Returns:
        New list of shallow-copied tool call objects safe to hand to
        ``ContextBuilder.format_assistant_tool_calls`` for storage. The
        original objects (used for actual tool execution) are untouched.
    """
    import copy

    bounded = []
    for tc in tool_calls:
        tc_copy = copy.copy(tc)
        tc_copy.arguments = {
            key: _truncate_for_history(value, _MAX_TOOL_ARG_CHARS)
            if isinstance(value, str)
            else value
            for key, value in tc.arguments.items()
        }
        bounded.append(tc_copy)
    return bounded


def _emit(
    callback: Callable[[SwarmEvent], None] | None,
    event_type: str,
    agent_id: str,
    task_id: str,
    data: dict | None = None,
) -> None:
    """Emit a swarm event via callback if provided.

    Args:
        callback: Optional event callback function.
        event_type: Event type string.
        agent_id: Agent identifier.
        task_id: Task identifier.
        data: Additional event data.
    """
    if callback is None:
        return
    event = SwarmEvent(
        type=event_type,
        agent_id=agent_id,
        task_id=task_id,
        data=data or {},
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    try:
        callback(event)
    except Exception:
        logger.warning("Event callback failed for %s", event_type, exc_info=True)


def _filter_skill_descriptions(loader: SkillsLoader, skill_names: list[str]) -> str:
    """Return skill descriptions filtered to the given whitelist.

    Args:
        loader: SkillsLoader instance with all skills loaded.
        skill_names: Skill names to include. Empty list means include all.

    Returns:
        Formatted skill descriptions string.
    """
    if not skill_names:
        return loader.get_descriptions()
    lines: list[str] = []
    for skill in loader.skills:
        if skill.name in skill_names:
            lines.append(f"  - {skill.name}: {skill.description}")
    return "\n".join(lines) if lines else "(no matching skills)"


def _estimate_tokens(
    messages: list[dict],
    response: object,
) -> tuple[int, int]:
    """Return token usage for a single LLM call, real if available.

    Prefers the provider-reported counts attached to the response by
    :func:`ChatLLM._parse_response` (``usage_metadata``). Falls back to a
    character-length heuristic (``len // 4``) only when the provider
    didn't return usage data — keeps the behaviour contract for legacy
    or partial responses while making per-run totals (which feed
    ``SwarmRun.total_input_tokens`` / ``total_output_tokens``) reflect
    real billing instead of a CJK-hostile char-count guess.

    Args:
        messages: Messages sent to the LLM for this call. Used only for
            the fallback estimate when ``response.usage_metadata`` is
            missing.
        response: ``LLMResponse`` from ``ChatLLM.chat`` /
            ``ChatLLM.stream_chat``.

    Returns:
        Tuple of (input_tokens, output_tokens). Either component may be
        zero — that simply means the provider didn't report it and the
        fallback couldn't compute it either (e.g. binary content).
    """
    from src.providers.chat import LLMResponse

    if isinstance(response, LLMResponse) and response.usage_metadata:
        usage = response.usage_metadata
        real_input = int(usage.get("input_tokens") or 0)
        real_output = int(usage.get("output_tokens") or 0)
        if real_input or real_output:
            return real_input, real_output

    # Fallback: provider didn't return usage_metadata. Estimate from
    # serialized message length and response content length. ~4 chars per
    # English token; under-counts for CJK / Thai / emoji-heavy prompts but
    # at least preserves the prior behaviour.
    try:
        input_tokens = len(json.dumps(messages, ensure_ascii=False)) // 4
    except Exception:
        input_tokens = 0

    if isinstance(response, LLMResponse):
        output_tokens = len(response.content or "") // 4
    else:
        output_tokens = 0

    return input_tokens, output_tokens


def build_worker_prompt(
    agent_spec: SwarmAgentSpec,
    upstream_summaries: dict[str, str],
    skill_descriptions: str,
    grounding_block: str = "",
) -> str:
    """Build the worker's system prompt with role, upstream context, and skills.

    Args:
        agent_spec: The agent's role specification.
        upstream_summaries: Mapping of context_key -> upstream task summary.
        skill_descriptions: Pre-filtered skill description text.
        grounding_block: Optional "Ground Truth" markdown produced by
            :func:`src.swarm.grounding.format_grounding_block`. Spliced in
            ahead of the Execution Rules section so the worker sees real
            recent prices before any tool decision. Empty string skips the
            section entirely.

    Returns:
        Complete system prompt string for the worker LLM.
    """
    upstream_block = ""
    if upstream_summaries:
        sections = []
        for key, summary in upstream_summaries.items():
            sections.append(f"### {key}\n{summary}")
        upstream_block = (
            "## Upstream Context (from previous agents)\n\n"
            + "\n\n".join(sections)
        )

    prompt_parts = [
        f"## Role\n\n{agent_spec.role}",
        agent_spec.system_prompt.replace("{upstream_context}", upstream_block),
    ]

    if skill_descriptions and skill_descriptions != "(no matching skills)":
        prompt_parts.append(
            f"## Available Skills (use load_skill to access full documentation)\n\n{skill_descriptions}"
        )

    if grounding_block:
        # Placed before Execution Rules so it's in scope when the worker
        # plans its first tool call. The block already contains an explicit
        # instruction to prefer these prices over training data.
        prompt_parts.append(grounding_block)

    if "get_market_data" in (agent_spec.tools or []):
        prompt_parts.append(
            "## Market Data Tool Policy\n\n"
            "For OHLCV price bars, recent closes, volume, technical indicators, "
            "or return calculations, call `get_market_data` before writing raw "
            "provider scripts. It uses the repository loader layer, normalizes "
            "symbols, drops malformed OHLC rows, and returns strict JSON. Use "
            "raw yfinance scripts only for fields outside OHLCV coverage, such "
            "as fundamentals, holders, options, or corporate metadata."
        )

    # Universal anti-fabrication rule. The grounding_block carries a similar
    # instruction but only renders when user_vars supplies explicit symbols.
    # Free-form prompts ("look at A-share short-term sentiment") otherwise
    # leave the worker with no guardrail and it cheerfully cites training-data
    # prices and sector weights. This block applies the rule unconditionally
    # — including to aggregator / synthesis agents that have no data tools
    # and previously had no instruction against inventing numbers.
    prompt_parts.append(
        "## Data Citation Discipline (HARD RULE)\n\n"
        "Every specific number you cite in your output — prices, percentages, "
        "volumes, fund flows, market-cap rankings, sector weights, ETF codes, "
        "ticker recommendations — MUST be traceable to one of:\n"
        "  (a) a tool call result obtained in THIS run,\n"
        "  (b) the Ground Truth block above (if present),\n"
        "  (c) the Upstream Context above (if present and the upstream agent "
        "itself sourced it from (a) or (b)).\n\n"
        "You may NOT cite numbers from memory or training data. Markets have "
        "moved since your cutoff; any specific price/percentage you recall is "
        "wrong by default.\n\n"
        "If you cannot back a number with (a), (b), or (c), you have two "
        "choices:\n"
        "  - call a data tool to fetch it (preferred), or\n"
        "  - omit the number and qualify the statement (e.g. \"directional "
        "only — not verified against live data\").\n\n"
        "This rule applies equally to synthesis / aggregator / editor roles "
        "that lack data tools. If upstream did not provide a specific number, "
        "do NOT introduce one from training data — say the upstream omitted "
        "it and proceed without."
    )

    prompt_parts.append(
        "## Execution Rules\n\n"
        "You have a HARD LIMIT of 20 tool calls. After that you will be cut off. Work efficiently.\n\n"
        "**Phase 1 — Plan (0 tool calls):** Before calling any tool, state your plan in 3-5 bullet points.\n\n"
        "**Phase 2 — Execute (≤15 tool calls):**\n"
        "- `load_skill` first to get data access methods and analysis patterns.\n"
        "- Write ONE focused Python script via `write_file`, then run it with `bash python script.py`.\n"
        "- Do NOT write long Python code inside bash. Use write_file + bash.\n"
        "- Do NOT fetch data with curl/requests. Use the patterns from load_skill (yfinance, OKX API via Python).\n"
        "- If a script fails, read the error, fix with `edit_file`, re-run. Max 2 retries per script.\n\n"
        "**Phase 3 — Summarize (MUST use write_file):**\n"
        "- You MUST call `write_file` with path `report.md` to save your final report as a markdown file.\n"
        "- This is REQUIRED, not optional. Your final response MUST include a write_file call for report.md.\n"
        "- The report must include specific numbers, dates, and actionable conclusions.\n"
        "- After writing report.md, output a brief 2-3 sentence summary in your text response.\n"
        "- Respond in the same language as the task prompt."
    )

    now = datetime.now()
    prompt_parts.append(
        f"## Current Date & Time\n\n"
        f"Today is {now.strftime('%A, %B %d, %Y %H:%M (local)')}."
    )

    return "\n\n".join(prompt_parts)


def run_worker(
    agent_spec: SwarmAgentSpec,
    task: SwarmTask,
    upstream_summaries: dict[str, str],
    user_vars: dict[str, str],
    run_dir: Path,
    event_callback: Callable[[SwarmEvent], None] | None = None,
    include_shell_tools: bool = False,
    grounding_block: str = "",
    agent_config: AgentConfig | None = None,
) -> WorkerResult:
    """Execute a single worker task using a lightweight ReAct loop.

    Steps:
      1. Build filtered ToolRegistry from agent_spec.tools
      2. Create ChatLLM with agent_spec.model_name
      3. Build system prompt with role + upstream summaries + filtered skills
      4. Resolve task.prompt_template with user_vars
      5. Run ReAct loop (for iteration in range(max_iterations))
      6. Write summary to artifacts/{agent_id}/summary.md
      7. Return WorkerResult

    Args:
        agent_spec: Agent role specification with tools/skills/model config.
        task: The task to execute, including prompt template.
        upstream_summaries: Summaries from upstream tasks keyed by input_from keys.
        user_vars: User-provided variables for template rendering.
        run_dir: Path to .swarm/runs/{run_id}/ directory.
        event_callback: Optional callback for swarm events.
        include_shell_tools: Whether this worker may register shell tools.
        grounding_block: Optional pre-rendered "Ground Truth" markdown that
            anchors the worker on real recent prices for symbols mentioned in
            ``user_vars``. Forwarded verbatim to :func:`build_worker_prompt`.
        agent_config: Optional resolved agent config carrying remote MCP
            server definitions. Threaded from :class:`SwarmRuntime` and
            consumed by :func:`build_swarm_registry` to merge remote MCP
            tools with the local-tool pool before applying the agent's
            whitelist. ``None`` preserves the prior local-only behavior.

    Returns:
        WorkerResult with status, summary, artifacts, and iteration count.
    """
    agent_id = agent_spec.id
    task_id = task.id
    max_iterations = agent_spec.max_iterations or _DEFAULT_MAX_ITERATIONS
    timeout = agent_spec.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS

    _emit(event_callback, "worker_started", agent_id, task_id)

    # 1. Build per-worker tool registry — local pool plus any operator-
    #    surfaced MCP tools, projected onto the agent's whitelist.
    registry = build_swarm_registry(
        agent_spec.tools,
        agent_config=agent_config,
        include_shell_tools=include_shell_tools,
    )

    # 2. Create LLM
    llm = ChatLLM(model_name=agent_spec.model_name)

    # 3. Build system prompt with filtered skills
    skills_loader = SkillsLoader()
    skill_desc = _filter_skill_descriptions(skills_loader, agent_spec.skills)
    system_prompt = build_worker_prompt(
        agent_spec, upstream_summaries, skill_desc, grounding_block=grounding_block,
    )

    # 4. Resolve prompt template with user vars (missing vars → LLM infers)
    class _FallbackDict(dict):
        """Dict that hints LLM to infer missing template variables."""
        def __missing__(self, key: str) -> str:
            return f"(determine the appropriate {key} based on the objective)"

    template_vars = _FallbackDict(user_vars)

    try:
        user_prompt = task.prompt_template.format_map(_FallbackDict(template_vars))
    except (KeyError, ValueError) as exc:
        error_msg = f"Failed to render prompt template: {exc}"
        _emit(event_callback, "worker_failed", agent_id, task_id, {"error": error_msg})
        return WorkerResult(
            status="failed", summary="", iterations=0, error=error_msg,
            input_tokens=0, output_tokens=0,
        )

    # 5. Build initial messages
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # 6. ReAct loop
    artifact_dir = run_dir / "artifacts" / agent_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    iteration = 0
    summary = ""
    total_input_tokens = 0
    total_output_tokens = 0

    # Threshold for injecting a "wrap up" nudge (80% of budget)
    wrap_up_at = max(1, int(max_iterations * 0.8))
    last_assistant_content = ""

    _KEEP_RECENT_TOOLS = 3
    data_tool_calls = 0
    content_filter_count = 0
    consecutive_content_filter_count = 0

    for iteration in range(max_iterations):
        # Microcompact: clear old tool results to prevent token bloat
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if len(tool_msgs) > _KEEP_RECENT_TOOLS:
            for msg in tool_msgs[:-_KEEP_RECENT_TOOLS]:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 100:
                    msg["content"] = "[cleared]"

        # Check timeout
        elapsed = time.monotonic() - t0
        if elapsed > timeout:
            summary = _best_summary(messages, last_assistant_content) or f"Worker timed out after {elapsed:.0f}s ({iteration} iterations)"
            summary = _resolve_summary(artifact_dir, summary)
            _emit(event_callback, "worker_timeout", agent_id, task_id, {"elapsed": elapsed})
            _write_summary(artifact_dir, summary)
            _persist_messages(artifact_dir, messages)
            return WorkerResult(
                status="timeout",
                summary=summary,
                artifact_paths=_collect_artifacts(artifact_dir),
                iterations=iteration,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                content_filter_warnings=compute_content_filter_warnings(
                    content_filter_count, iteration + 1,
                ),
            )

        # Check token estimate
        token_estimate = len(json.dumps(messages, ensure_ascii=False)) // 4
        if token_estimate > _MAX_TOKEN_ESTIMATE:
            summary = last_assistant_content or f"Worker context too large (~{token_estimate} tokens, {iteration} iterations)"
            summary = _resolve_summary(artifact_dir, summary)
            _emit(event_callback, "worker_token_limit", agent_id, task_id, {"tokens": token_estimate})
            _write_summary(artifact_dir, summary)
            return WorkerResult(
                status="token_limit",
                summary=summary,
                artifact_paths=_collect_artifacts(artifact_dir),
                iterations=iteration,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                content_filter_warnings=compute_content_filter_warnings(
                    content_filter_count, iteration + 1,
                ),
            )

        # Inject wrap-up nudge when approaching iteration limit
        if iteration == wrap_up_at:
            remaining = max_iterations - iteration
            messages.append({
                "role": "user",
                "content": (
                    f"[SYSTEM] You have {remaining} iterations remaining. "
                    "If report.md is not written yet, make one final write_file call for report.md. "
                    "Otherwise stop calling tools and output your final analysis summary as plain text."
                ),
            })

        # On last iteration, call LLM without tool definitions to force text output
        is_last_iteration = iteration == max_iterations - 1
        tool_defs = None if is_last_iteration else registry.get_definitions()

        # Stream the LLM — moonshot/kimi non-streaming invoke is unreliable
        # (issue #42), and streaming also feeds dashboard live progress.
        llm_call_start = time.monotonic()
        try:
            def _on_text_chunk(delta: str) -> None:
                _emit(event_callback, "worker_text", agent_id, task_id,
                      {"content": delta, "iteration": iteration})

            # LLM streaming can stall for 30s+ between request start and the
            # first text chunk (slow first-token providers, reasoning models'
            # think phase, pure-tool-call responses with no text). Without a
            # ticker, the stale-run reaper would mark a healthy run failed
            # the moment its silence exceeds the heartbeat-based threshold.
            # Wrap the call in the same HeartbeatTimer used for tool execution
            # so events.jsonl gets a fresh entry every few seconds no matter
            # what the provider is doing.
            def _on_llm_heartbeat(payload: dict) -> None:
                _emit(
                    event_callback,
                    "task_heartbeat",
                    agent_id,
                    task_id,
                    {**payload, "iteration": iteration, "phase": "llm"},
                )

            def _stream_once() -> LLMResponse:
                """Run one heartbeat-wrapped streaming LLM call.

                Recomputes the remaining time budget at call time so the
                single retry after a stream failure never reuses a stale
                timeout.

                Returns:
                    Parsed ``LLMResponse`` from ``ChatLLM.stream_chat``.

                Raises:
                    ProviderStreamError: When provider streaming fails.
                """
                remaining_timeout = max(10, int(timeout - (time.monotonic() - t0)))
                with HeartbeatTimer(
                    tool_name=f"llm:{agent_spec.model_name or 'default'}",
                    interval=_HEARTBEAT_INTERVAL_S,
                    emit=_on_llm_heartbeat,
                ):
                    return llm.stream_chat(
                        messages,
                        tools=tool_defs,
                        timeout=remaining_timeout,
                        on_text_chunk=_on_text_chunk,
                    )

            # A transient mid-stream hiccup (connection reset) used to be
            # absorbed by ChatLLM's silent non-streaming fallback; it now
            # surfaces as ProviderStreamError, so retry the stream exactly
            # once before taking the existing failure path. Deterministic
            # 4xx errors skip the retry and fail immediately.
            #
            # For local Ollama, acquire the process-wide call lock first so
            # concurrent swarm agents don't thrash the same partially
            # CPU-offloaded model instance. The acquire itself is bounded by
            # this worker's own remaining timeout budget — a stuck peer
            # holding the lock can't defeat this worker's timeout, it just
            # makes this call fail through the normal failure path below.
            is_ollama = _is_ollama_provider()
            lock_acquired = True
            if is_ollama:
                lock_wait_budget = max(10, int(timeout - (time.monotonic() - t0)))
                lock_acquired = _OLLAMA_CALL_LOCK.acquire(timeout=lock_wait_budget)
                if not lock_acquired:
                    raise RuntimeError(
                        f"Timed out waiting {lock_wait_budget}s for the local "
                        "Ollama call lock (another swarm agent is still using it)"
                    )
            try:
                stream_attempt = 0
                while True:
                    try:
                        response = _stream_once()
                        break
                    except ProviderStreamError as stream_exc:
                        if not stream_exc.retryable or stream_attempt >= _STREAM_MAX_RETRIES:
                            raise
                        delay = _STREAM_RETRY_DELAY_S * (_STREAM_RETRY_BACKOFF ** stream_attempt)
                        stream_attempt += 1
                        logger.warning(
                            "Provider stream failed for agent=%s task=%s iteration=%d "
                            "(provider=%s model=%s); retrying %d/%d in %.1fs: %s",
                            agent_id,
                            task_id,
                            iteration,
                            stream_exc.provider,
                            stream_exc.model,
                            stream_attempt,
                            _STREAM_MAX_RETRIES,
                            delay,
                            stream_exc,
                        )
                        time.sleep(delay)
            finally:
                if is_ollama and lock_acquired:
                    _OLLAMA_CALL_LOCK.release()
        except Exception as exc:
            error_msg = f"LLM call failed at iteration {iteration}: {exc}"
            logger.warning(error_msg)
            _emit(event_callback, "worker_failed", agent_id, task_id, {"error": error_msg})
            return WorkerResult(
                status="failed",
                summary=_resolve_summary(artifact_dir, last_assistant_content or ""),
                artifact_paths=_collect_artifacts(artifact_dir),
                iterations=iteration,
                error=error_msg,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                content_filter_warnings=compute_content_filter_warnings(
                    content_filter_count, iteration + 1,
                ),
            )

        # Accumulate token counts
        iter_in, iter_out = _estimate_tokens(messages, response)
        total_input_tokens += iter_in
        total_output_tokens += iter_out

        # Witness metrics: wall-clock + token throughput for this LLM call,
        # using fields actually available through the OpenAI-compatible
        # provider transport (usage_metadata token counts + measured wall
        # time) rather than Ollama-native timing fields the current
        # transport never receives.
        _emit(
            event_callback,
            "llm_call_stats",
            agent_id,
            task_id,
            {
                "iteration": iteration,
                "wall_clock_seconds": round(time.monotonic() - llm_call_start, 2),
                "prompt_tokens": iter_in,
                "completion_tokens": iter_out,
                "model": agent_spec.model_name or os.getenv("LANGCHAIN_MODEL_NAME", ""),
                "provider": os.getenv("LANGCHAIN_PROVIDER", ""),
                "messages_count": len(messages),
            },
        )

        # Track last meaningful assistant content
        if response.content and len(response.content.strip()) > 20:
            last_assistant_content = response.content

        # Content-filter skip: provider blocked the response — continue to
        # the next iteration instead of finalising on empty/garbage content.
        if response.content_filter_triggered:
            content_filter_count += 1
            consecutive_content_filter_count += 1
            if consecutive_content_filter_count >= MAX_CONSECUTIVE_CONTENT_FILTER_SKIPS:
                _emit(
                    event_callback,
                    "content_filter_circuit_breaker",
                    agent_id,
                    task_id,
                    {"count": content_filter_count},
                )
                summary = _resolve_summary(artifact_dir, last_assistant_content or "")
                _write_summary(artifact_dir, summary)
                return WorkerResult(
                    status="failed",
                    summary=summary,
                    artifact_paths=_collect_artifacts(artifact_dir),
                    iterations=iteration + 1,
                    error=(
                        f"content_filter_circuit_breaker: "
                        f"{MAX_CONSECUTIVE_CONTENT_FILTER_SKIPS} consecutive "
                        "LLM responses were blocked by content moderation"
                    ),
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    content_filter_warnings=compute_content_filter_warnings(
                        content_filter_count, iteration + 1,
                    ),
                )
            _emit(
                event_callback,
                "content_filter_skipped",
                agent_id,
                task_id,
                {"iteration": iteration, "content_filter_count": content_filter_count},
            )
            messages.append({
                "role": "system",
                "content": CONTENT_FILTER_SKIP_MESSAGE,
            })
            continue

        consecutive_content_filter_count = 0

        # If no tool calls, this is the final response
        if not response.has_tool_calls:
            summary = response.content or last_assistant_content or "(no summary)"
            summary = _resolve_summary(artifact_dir, summary)
            _write_summary(artifact_dir, summary)
            reason = _classify_deliverable(
                summary,
                is_data_agent=_is_data_agent(agent_spec),
                report_written=_report_written(artifact_dir),
                data_tool_calls=data_tool_calls,
            )
            if reason:
                _emit(event_callback, "worker_incomplete", agent_id, task_id,
                      {"iterations": iteration + 1, "reason": reason})
                return WorkerResult(
                    status="incomplete",
                    summary=summary,
                    artifact_paths=_collect_artifacts(artifact_dir),
                    iterations=iteration + 1,
                    error=f"output contract not met: {reason}",
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    content_filter_warnings=compute_content_filter_warnings(
                        content_filter_count, iteration + 1,
                    ),
                )
            _emit(event_callback, "worker_completed", agent_id, task_id, {"iterations": iteration + 1})
            return WorkerResult(
                status="completed",
                summary=summary,
                artifact_paths=_collect_artifacts(artifact_dir),
                iterations=iteration + 1,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                content_filter_warnings=compute_content_filter_warnings(
                    content_filter_count, iteration + 1,
                ),
            )

        # Append assistant message with tool calls. Arguments are bounded for
        # history storage only — response.tool_calls (used below for actual
        # tool execution) keeps its full, untruncated content.
        messages.append(
            ContextBuilder.format_assistant_tool_calls(
                _bounded_tool_calls_for_history(response.tool_calls),
                content=response.content,
                reasoning_content=response.reasoning_content,
            )
        )

        # Execute each tool call — inject run_dir so tools write inside artifact_dir
        for tc in response.tool_calls:
            mcp_meta = _remote_tool_metadata(registry, tc.name)
            _emit(
                event_callback, "tool_call", agent_id, task_id,
                {"tool": tc.name, "iteration": iteration,
                 "arguments": _preview_tool_arguments(tc.arguments),
                 **mcp_meta},
            )
            tc_start = time.monotonic()
            args = {**tc.arguments, "run_dir": str(artifact_dir)}

            # Wrap tool execution in a heartbeat so the events.jsonl tail has a
            # fresh timestamp every few seconds. The stale-run reaper relies on
            # this signal to tell a hung tool call apart from a dead host; the
            # CLI dashboard / SSE clients also get live "still working" ticks.
            def _on_heartbeat(payload: dict) -> None:
                _emit(
                    event_callback,
                    "task_heartbeat",
                    agent_id,
                    task_id,
                    {**payload, "iteration": iteration, "phase": "tool"},
                )

            with HeartbeatTimer(
                tool_name=tc.name,
                interval=_HEARTBEAT_INTERVAL_S,
                emit=_on_heartbeat,
            ):
                result = registry.execute(tc.name, args)
            if tc.name != "load_skill" and not _is_error_result(result):
                data_tool_calls += 1
            tc_elapsed = time.monotonic() - tc_start
            _emit(
                event_callback, "tool_result", agent_id, task_id,
                {"tool": tc.name, "elapsed_ms": int(tc_elapsed * 1000),
                 "status": "ok", "iteration": iteration,
                  "result_preview": _preview_tool_result(result),
                 **mcp_meta},
            )
            messages.append(
                ContextBuilder.format_tool_result(
                    tc.id, tc.name, _truncate_for_history(result, _MAX_TOOL_RESULT_CHARS)
                )
            )

    # Content filter ratio tracking
    content_filter_warnings = compute_content_filter_warnings(
        content_filter_count, iteration + 1,
    )

    # Hit iteration limit — use last meaningful content as summary
    summary = _best_summary(messages, last_assistant_content) or f"Worker hit iteration limit ({max_iterations} iterations)"
    summary = _resolve_summary(artifact_dir, summary)
    _write_summary(artifact_dir, summary)
    _persist_messages(artifact_dir, messages)
    reason = _classify_deliverable(
        summary,
        is_data_agent=_is_data_agent(agent_spec),
        report_written=_report_written(artifact_dir),
        data_tool_calls=data_tool_calls,
    )
    if reason:
        _emit(event_callback, "worker_incomplete", agent_id, task_id,
              {"iterations": max_iterations, "reason": f"iteration limit; {reason}"})
        return WorkerResult(
            status="incomplete",
            summary=summary,
            artifact_paths=_collect_artifacts(artifact_dir),
            iterations=max_iterations,
            error=f"hit iteration limit without a valid deliverable: {reason}",
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            content_filter_warnings=content_filter_warnings,
        )
    _emit(event_callback, "worker_iteration_limit", agent_id, task_id)
    return WorkerResult(
        status="completed",
        summary=summary,
        artifact_paths=_collect_artifacts(artifact_dir),
        iterations=max_iterations,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        content_filter_warnings=content_filter_warnings,
    )


def _best_summary(messages: list[dict], fallback: str) -> str:
    """Extract the best summary from all assistant messages."""
    texts = [
        m["content"] for m in messages
        if m.get("role") == "assistant" and m.get("content")
        and len(m["content"].strip()) > 100
    ]
    if texts:
        return max(texts, key=len)
    return fallback


def _remote_tool_metadata(registry: ToolRegistry, tool_name: str) -> dict[str, str]:
    """Return MCP routing metadata for ``tool_name`` if it's a remote MCP tool.

    Auditors of ``events.jsonl`` need to tell at a glance which remote MCP
    server a swarm worker reached and what the *original* (non-prefixed)
    tool name was. The local-side name is already in ``data["tool"]``; this
    helper supplies the missing ``server`` + ``remote_tool`` pair when the
    registered tool is an :class:`MCPRemoteTool`. Local-only tools yield
    an empty dict so the event payload is unchanged for them.
    """
    tool = registry.get(tool_name)
    if not isinstance(tool, MCPRemoteTool):
        return {}
    spec = getattr(tool, "_spec", None)
    if spec is None:
        return {}
    return {"server": spec.server_name, "remote_tool": spec.remote_name}


def _preview_tool_arguments(arguments: dict) -> dict[str, str]:
    """Return a short, redacted argument preview for streamed events."""
    preview: dict[str, str] = {}
    for key, value in arguments.items():
        if key == "run_dir":
            continue
        if is_sensitive_arg(key):
            preview[key] = "[redacted]"
            continue
        preview[key] = _truncate_preview(redact_payload(value))
    return preview


def _preview_tool_result(result: str) -> str:
    """Return a short, redacted result preview for streamed events."""
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return _truncate_preview(result)
    return _truncate_preview(redact_payload(parsed))


def _truncate_preview(value: Any, *, limit: int = 200) -> str:
    """Stringify and truncate an event preview payload."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


# Tools that do not themselves fetch/compute market data. An agent whose
# entire toolset is a subset of these is a synthesis/editor role (e.g. the
# research editor in equity_research_team) and may legitimately produce a
# text deliverable with no tool calls — it must NOT be failed for "no tool
# evidence" (that would regress correct runs; see #115 framing).
_GENERIC_TOOLS = {"bash", "read_file", "write_file", "load_skill", "edit_file"}

_UNPARSED_TOOL_MARKERS = (
    "<\uff5ctool\u2581calls\u2581begin\uff5c>",
    "<tool_calls_begin>",
    "<tool_call_begin>",
    "<tool_sep>",
    "tool\u2581sep",
)
_FABRICATION_MARKERS = ("mock data", "without actual data", "fabricated data", "placeholder data")
_PLAN_PREFIXES = (
    "# phase 1", "## phase 1", "### phase 1",
    "phase 1 \u2014 plan", "phase 1 - plan", "phase 1: plan",
    "# plan", "## plan", "### plan", "**plan**",
)
_HANDOFF_TAILS = (
    "execute", "execute.", "execute:", "skills.", "skills", "proceed?",
    "proceed.", "without writing files.", "let me adjust the approach",
    "let me adjust the approach.", "stand by for final synthesis.",
)


def _report_written(artifact_dir: Path) -> bool:
    """True iff a non-empty report.md was actually produced by the worker."""
    try:
        p = artifact_dir / "report.md"
        return p.is_file() and bool(p.read_text(encoding="utf-8").strip())
    except Exception:
        return False


def _is_data_agent(agent_spec: SwarmAgentSpec) -> bool:
    """An agent with at least one data/analysis tool beyond the generic kit."""
    return bool(set(agent_spec.tools or []) - _GENERIC_TOOLS)


def _is_error_result(result: str) -> bool:
    """Did a tool call return a top-level error envelope?

    Parses the result as JSON and checks for a top-level ``status == "error"``.
    A nested ``status`` (e.g. inside ``data``) is intentionally ignored — only
    the envelope matters for the deliverable contract.

    Falls back to a fast substring check on the head for truncated or
    non-JSON payloads, so the function is robust to streaming / partial
    output without ever raising.
    """
    text = (result or "").strip()
    if not text or not text.startswith("{"):
        return False
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        # Truncated / non-JSON payload — keep the original heuristic so we
        # never raise from a classifier on the worker hot path.
        head = text[:160].lower()
        return '"status": "error"' in head or '"status":"error"' in head
    return isinstance(parsed, dict) and parsed.get("status") == "error"


def _classify_deliverable(
    summary: str,
    *,
    is_data_agent: bool,
    report_written: bool,
    data_tool_calls: int,
) -> str | None:
    """Hybrid output contract. Return a short reason string when the worker
    did NOT produce a substantive deliverable, else ``None``.

    Content-sanity applies to every agent; the tool-evidence requirement
    applies ONLY to data agents so tool-less synthesis/editor roles are
    not false-rejected.
    """
    text = (summary or "").strip()
    if not text:
        return "empty deliverable"
    low = text.lower()
    if any(m in low for m in _UNPARSED_TOOL_MARKERS):
        return "unparsed tool-call markup (provider did not parse tool calls)"
    if any(m in low for m in _FABRICATION_MARKERS):
        return "explicitly fabricated / mock data"
    if text.startswith("{") and '"status"' in text[:40] and (
        '"content"' in text[:300] or '"ok"' in text[:40]
    ):
        return "raw tool-result envelope, not analysis"
    if low.startswith(_PLAN_PREFIXES):
        tail = low.rsplit("phase 2", 1)[-1].strip() if "phase 2" in low else ""
        if len(text) < 600 or low.rstrip().endswith(_HANDOFF_TAILS) or (
            "phase 2" in low and len(tail) < 80
        ):
            return "plan-only stub (no executed analysis / conclusion)"
    if is_data_agent and not report_written and data_tool_calls == 0:
        return "data agent produced no tool calls and no report.md"
    return None


def _resolve_summary(artifact_dir: Path, fallback: str) -> str:
    """Return report.md content if it exists, otherwise fall back to text."""
    report_path = artifact_dir / "report.md"
    try:
        if report_path.is_file():
            content = report_path.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception:
        logger.warning("Failed to read report.md from %s", artifact_dir, exc_info=True)
    return fallback


def _persist_messages(artifact_dir: Path, messages: list[dict]) -> None:
    """Persist messages to disk for post-mortem analysis."""
    try:
        path = artifact_dir / "messages.json"
        path.write_text(
            json.dumps(messages, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Failed to persist messages to %s", artifact_dir, exc_info=True)


def _write_summary(artifact_dir: Path, summary: str) -> None:
    """Write worker summary to artifacts directory.

    Args:
        artifact_dir: Path to artifacts/{agent_id}/ directory.
        summary: Summary text to write.
    """
    try:
        summary_path = artifact_dir / "summary.md"
        summary_path.write_text(summary, encoding="utf-8")
    except Exception:
        logger.warning("Failed to write summary to %s", artifact_dir, exc_info=True)


def _collect_artifacts(artifact_dir: Path) -> list[str]:
    """Collect all artifact file paths from agent's artifact directory.

    Args:
        artifact_dir: Path to artifacts/{agent_id}/ directory.

    Returns:
        List of artifact file path strings.
    """
    if not artifact_dir.exists():
        return []
    return [str(p) for p in artifact_dir.iterdir() if p.is_file()]
