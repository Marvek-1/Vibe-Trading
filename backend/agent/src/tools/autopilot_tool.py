"""Research Autopilot: goal-hypothesis bridge + backtest config generation.

Phase 1: Connects the Hypothesis Registry to the Research Goal runtime.
Phase 2: Auto-generates backtest config.json from hypothesis metadata.
Phase 3: Scaffolds a contract-correct signal_engine.py stub and links
    backtest run-card metrics back to the hypothesis, closing the
    hypothesis -> backtest -> evidence loop.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool
from src.hypotheses import HypothesisRegistry


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"status": "ok", **payload}, ensure_ascii=False)


def _error(exc: Exception) -> str:
    return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)


def _get_hypothesis(hypothesis_id: str):
    """Return a hypothesis by id, or None when absent."""
    for hypothesis in HypothesisRegistry().list():
        if hypothesis.hypothesis_id == hypothesis_id:
            return hypothesis
    return None


_AUTOPILOT_OBJECTIVE_TEMPLATE = """<hypothesis-id>{hypothesis_id}</hypothesis-id>
<hypothesis-title>{title}</hypothesis-title>

{thesis}

---
**Autopilot**: This goal was auto-scaffolded from a research hypothesis.
Continue through the workflow: generate backtest code → execute → evaluate → record evidence."""


class RunResearchAutopilotTool(BaseTool):
    """Start a research workflow from a durable hypothesis.

    Reads a hypothesis from the local registry, creates a research goal
    with the hypothesis thesis as its objective, and returns the goal
    snapshot so the agent can continue the backtest → evidence pipeline.
    """

    name = "run_research_autopilot"
    description = (
        "Start a research goal from a saved hypothesis. "
        "Reads the hypothesis, creates a goal with the thesis as objective "
        "and backtest-relevant criteria. NOTE: this replaces the session's "
        "current research goal. Returns a goal snapshot you can continue "
        "from with backtest/evidence tools."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "hypothesis_id": {
                "type": "string",
                "description": "ID of a previously created research hypothesis",
            },
            "session_id": {
                "type": "string",
                "description": "Current session id (host-injected)",
            },
        },
        "required": ["hypothesis_id"],
    }

    def __init__(
        self,
        *,
        default_session_id: str | None = None,
        event_callback: Any = None,
    ) -> None:
        """Initialize the autopilot tool.

        Args:
            default_session_id: Session id injected by the host runtime, so the
                tool can create a goal without the LLM ever knowing the id.
            event_callback: Optional host callback, accepted for registry
                construction parity with the goal tools (currently unused).
        """
        self._default_session_id = default_session_id
        self._event_callback = event_callback

    def execute(self, **kwargs: Any) -> str:
        try:
            hypothesis_id = str(kwargs.get("hypothesis_id", "")).strip()
            if not hypothesis_id:
                return json.dumps(
                    {"status": "error", "error": "hypothesis_id is required"},
                    ensure_ascii=False,
                )

            hypothesis = _get_hypothesis(hypothesis_id)
            if hypothesis is None:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Hypothesis not found: {hypothesis_id}",
                        "hint": "Use search_hypotheses to list available hypotheses.",
                    },
                    ensure_ascii=False,
                )

            session_id = str(
                kwargs.get("session_id") or self._default_session_id or ""
            ).strip()
            if not session_id:
                return json.dumps(
                    {
                        "status": "error",
                        "error": "session_id is required",
                        "hint": "Ask the host runtime for the current session id.",
                    },
                    ensure_ascii=False,
                )

            objective = _AUTOPILOT_OBJECTIVE_TEMPLATE.format(
                hypothesis_id=hypothesis.hypothesis_id,
                title=hypothesis.title,
                thesis=hypothesis.thesis,
            )

            criteria = [
                "Generate backtest code (signal_engine.py + config.json) from the signal definition",
                "Execute a deterministic backtest with the configured data sources",
                "Evaluate backtest metrics against the hypothesis thesis",
                "Record evidence: link_backtest to hypothesis and add_goal_evidence",
            ]

            from src.goal import GoalStore

            store = GoalStore()

            goal = store.replace_goal(
                session_id=session_id,
                objective=objective,
                criteria=criteria,
                ui_summary=f"Research Autopilot: {hypothesis.title}",
                source="autopilot",
                protocol="thesis_review",
            )

            snapshot = store.get_goal_snapshot(goal.goal_id)

            hypothesis_summary = {
                "hypothesis_id": hypothesis.hypothesis_id,
                "title": hypothesis.title,
                "thesis": hypothesis.thesis[:300],
                "status": hypothesis.status,
                "universe": hypothesis.universe,
                "signal_definition": hypothesis.signal_definition[:300],
                "data_sources": hypothesis.data_sources,
                "skills": hypothesis.skills,
                "run_cards_count": len(hypothesis.run_cards),
            }

            return _ok(
                {
                    "goal": snapshot,
                    "hypothesis": hypothesis_summary,
                    "next_step": "Continue the research workflow. Generate backtest code → execute → add_goal_evidence.",
                }
            )

        except Exception as exc:
            return _error(exc)


_UNIVERSE_CODES: dict[str, list[str]] = {
    "crypto majors": ["BTC-USDT", "ETH-USDT"],
    "crypto": ["BTC-USDT", "ETH-USDT"],
    "majors": ["BTC-USDT", "ETH-USDT"],
    "csi 300": ["000300.SH"],
    "csi300": ["000300.SH"],
    "csi 500": ["000905.SH"],
    "csi500": ["000905.SH"],
    "sse 50": ["000016.SH"],
    "sse50": ["000016.SH"],
    "szse comp": ["399001.SZ"],
    "sse comp": ["000001.SH"],
    "chinext": ["399006.SZ"],
    "chi next": ["399006.SZ"],
    "s&p 500": ["SPY.US"],
    "sp500": ["SPY.US"],
    "nasdaq": ["QQQ.US"],
    "dow jones": ["DIA.US"],
    "hang seng": ["^HSI.HK"],
    "nikkei": ["^N225.HK"],
}


def _lookup_codes(universe: str) -> list[str]:
    key = universe.strip().lower().replace("-", " ").replace("_", " ")
    return _UNIVERSE_CODES.get(key, [universe])


def _resolve_source(data_sources: list[str] | None) -> tuple[str, str | None]:
    """Pick a valid loader source from the hypothesis, else fall back to ``auto``.

    A hypothesis ``data_sources`` entry is free text, so an unrecognised value
    would otherwise only fail deep inside the backtest runner with a confusing
    message. Validate it up front and degrade to ``auto`` with a warning the
    agent can surface.

    Args:
        data_sources: The hypothesis ``data_sources`` list (may be empty/None).

    Returns:
        A ``(source, warning)`` tuple; ``warning`` is ``None`` when the source
        is valid or the source whitelist cannot be imported.
    """
    candidate = (data_sources or ["auto"])[0]
    try:
        from backtest.loaders.registry import VALID_SOURCES
    except Exception:  # pragma: no cover - registry import is environment-stable
        return candidate, None
    if candidate in VALID_SOURCES:
        return candidate, None
    return "auto", (
        f"hypothesis data_source {candidate!r} is not a known loader source; "
        "fell back to 'auto'"
    )


def _validate_backtest_dates(start_date: str, end_date: str) -> None:
    """Validate backtest dates before writing any run artifacts."""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("start_date must be YYYY-MM-DD") from exc
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("end_date must be YYYY-MM-DD") from exc
    if start > end:
        raise ValueError("start_date must be on or before end_date")


def _run_dir_for_hypothesis(hypothesis_id: str) -> Path:
    """Return a path-contained run directory for any persisted hypothesis id."""
    suffix = hashlib.sha256(hypothesis_id.encode("utf-8")).hexdigest()[:12]
    return Path.home() / ".vibe-trading" / "runs" / f"autopilot_{suffix}"


class GenerateBacktestConfigTool(BaseTool):
    """Generate backtest config.json from a research hypothesis.

    Reads a hypothesis, derives config fields from its universe and
    data_sources, and writes a ready-to-run config.json to a run directory.
    The agent should then create signal_engine.py from the signal_definition
    and call the backtest tool.
    """

    name = "generate_backtest_config"
    description = (
        "Generate a backtest config.json from a saved hypothesis. "
        "Auto-populates codes from the hypothesis universe and source from "
        "data_sources. Writes config.json to a run directory. You must still "
        "create code/signal_engine.py from the signal_definition before calling "
        "the backtest tool."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "hypothesis_id": {
                "type": "string",
                "description": "ID of a previously created research hypothesis",
            },
            "start_date": {
                "type": "string",
                "description": "Backtest start date (YYYY-MM-DD)",
            },
            "end_date": {
                "type": "string",
                "description": "Backtest end date (YYYY-MM-DD)",
            },
            "session_id": {
                "type": "string",
                "description": "Current session id (host-injected)",
            },
        },
        "required": ["hypothesis_id", "start_date", "end_date"],
    }

    def execute(self, **kwargs: Any) -> str:
        try:
            hypothesis_id = str(kwargs.get("hypothesis_id", "")).strip()
            if not hypothesis_id:
                return json.dumps(
                    {"status": "error", "error": "hypothesis_id is required"},
                    ensure_ascii=False,
                )

            hypothesis = _get_hypothesis(hypothesis_id)
            if hypothesis is None:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Hypothesis not found: {hypothesis_id}",
                        "hint": "Use search_hypotheses to list available hypotheses.",
                    },
                    ensure_ascii=False,
                )

            if not hypothesis.universe.strip():
                return json.dumps(
                    {
                        "status": "error",
                        "error": "Hypothesis has no universe set",
                        "hint": "Use update_hypothesis to set a universe (e.g. 'CSI 300').",
                    },
                    ensure_ascii=False,
                )

            start_date = str(kwargs.get("start_date", "")).strip()
            end_date = str(kwargs.get("end_date", "")).strip()
            _validate_backtest_dates(start_date, end_date)

            from universe_resolution import resolve_universe, UniverseUnresolvableError
            from backtest.loaders.registry import VALID_SOURCES

            try:
                resolve_universe(hypothesis.universe, list(VALID_SOURCES))
            except UniverseUnresolvableError as exc:
                return json.dumps(
                    {"status": "error", "error": str(exc)}, ensure_ascii=False
                )

            codes = _lookup_codes(hypothesis.universe)
            source, source_warning = _resolve_source(hypothesis.data_sources)

            config = {
                "codes": codes,
                "start_date": start_date,
                "end_date": end_date,
                "source": source,
                "interval": "1D",
            }

            run_dir = _run_dir_for_hypothesis(hypothesis_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "code").mkdir(parents=True, exist_ok=True)

            config_path = run_dir / "config.json"
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            payload: dict[str, Any] = {
                "run_dir": str(run_dir),
                "config": config,
                "config_path": str(config_path),
                "hypothesis": {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "title": hypothesis.title,
                    "signal_definition": hypothesis.signal_definition,
                    "universe": hypothesis.universe,
                    "data_sources": hypothesis.data_sources,
                },
                "next_step": (
                    "Config written. Next: use write_file to create "
                    "code/signal_engine.py from the signal_definition above, "
                    "then call backtest(run_dir=...)."
                ),
            }
            if source_warning:
                payload["warning"] = source_warning
            return _ok(payload)

        except Exception as exc:
            return _error(exc)


_SIGNAL_ENGINE_TEMPLATE = '''"""Auto-scaffolded signal engine for hypothesis {hypothesis_id}.

Title: {title}

Implement your signal in ``SignalEngine.generate``. The default below holds
no position (a flat 0.0 signal) so the backtest runner contract is satisfied
and you can run a smoke backtest immediately, then replace the body with real
logic derived from the signal definition.
"""

from __future__ import annotations

import pandas as pd


class SignalEngine:
    """Signal engine consumed by the backtest runner.

    The runner instantiates this class with no arguments and calls
    ``generate(data_map)`` once per backtest.
    """

    def generate(self, data_map: dict[str, "pd.DataFrame"]) -> dict[str, "pd.Series"]:
        """
        CONTRACT -- read before overwriting. This is the only shape the engine accepts.

        INPUT
          data_map: {{symbol_code: OHLCV DataFrame}}, one entry per symbol.
            Each frame: DatetimeIndex ascending; columns include at least
            'open','high','low','close','volume'. There is NO key named 'data'.

        OUTPUT
          {{symbol_code: pd.Series}} -- same keys as data_map, each Series aligned
          to that frame's index. Values in [0.0, 1.0] = target position weight.

        CROSS-SECTIONAL STRATEGIES (ranking, deciles, relative momentum):
          Do NOT compute per-symbol in isolation. Build the panel first:

            closes  = pd.DataFrame({{c: f['close'] for c, f in data_map.items()}})
            returns = closes.pct_change(LOOKBACK)          # dates x symbols
            ranks   = returns.rank(axis=1, pct=True)        # per-date ranking
            longs   = (ranks >= 0.9).astype(float)          # top decile
            return {{c: longs[c].reindex(data_map[c].index).fillna(0.0)
                    for c in data_map}}

        WARMUP: gate all signals before TEST_START to 0.0 inside this function.

        HYPOTHESIS SIGNAL TO IMPLEMENT:
            {signal_definition}
        """
        signals: dict[str, "pd.Series"] = {{}}
        for code, frame in data_map.items():
            signals[code] = pd.Series(0.0, index=frame.index)
        return signals
'''


def _is_unimplemented_scaffold_signal_engine(signal_path: Path) -> bool:
    """Return True when ``signal_engine.py`` is still the flat scaffold stub."""
    try:
        source = signal_path.read_text(encoding="utf-8")
    except OSError:
        return False

    if (
        "Auto-scaffolded signal engine" not in source
        or "flat 0.0" not in source
        or "Implement your signal" not in source
    ):
        return False

    try:
        tree = ast.parse(source, filename=str(signal_path))
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "generate":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign) or len(child.targets) != 1:
                continue
            target = child.targets[0]
            if not (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "signals"
                and isinstance(target.slice, ast.Name)
                and target.slice.id == "code"
            ):
                continue
            call = child.value
            if not isinstance(call, ast.Call):
                continue
            if not (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "Series"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "pd"
            ):
                continue
            first_arg = call.args[0] if call.args else None
            if not (
                isinstance(first_arg, ast.Constant)
                and isinstance(first_arg.value, (int, float))
                and float(first_arg.value) == 0.0
            ):
                continue
            if any(
                kw.arg == "index"
                and isinstance(kw.value, ast.Attribute)
                and kw.value.attr == "index"
                and isinstance(kw.value.value, ast.Name)
                and kw.value.value.id == "frame"
                for kw in call.keywords
            ):
                return True
    return False


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_data_row_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
    except OSError:
        return 0
    return max(0, len(lines) - 1)


def _missing_hypothesis_evidence_artifacts(run_path: Path) -> list[str]:
    required = (
        Path("config.json"),
        Path("code/signal_engine.py"),
        Path("artifacts/metrics.csv"),
        Path("artifacts/equity.csv"),
    )
    return [
        relative.as_posix()
        for relative in required
        if not (run_path / relative).exists() or not (run_path / relative).is_file()
    ]


def _strategy_hash_matches_card(card: dict[str, Any], signal_path: Path) -> bool:
    expected = card.get("reproducibility", {}).get("strategy_hash")
    if not isinstance(expected, str) or not expected.strip():
        return False
    try:
        return expected == _file_hash(signal_path)
    except OSError:
        return False


def _invalid_hypothesis_evidence_metadata(card: dict[str, Any]) -> dict[str, Any] | None:
    status = card.get("strategy_implementation_status")
    provenance_valid = card.get("provenance_valid")
    run_purpose = card.get("run_purpose")
    if (
        status == "implemented"
        and provenance_valid is True
        and run_purpose == "hypothesis_test"
    ):
        return None
    return {
        "strategy_implementation_status": status or "missing",
        "provenance_valid": provenance_valid if isinstance(provenance_valid, bool) else False,
        "run_purpose": run_purpose or "missing",
    }


class ScaffoldSignalEngineTool(BaseTool):
    """Write a contract-correct ``signal_engine.py`` stub for a hypothesis.

    The backtest runner requires a ``SignalEngine`` class that is
    constructible with no arguments and exposes ``generate(self, data_map)``.
    This tool emits exactly that, with a runnable flat-signal default and the
    hypothesis ``signal_definition`` embedded as a docstring, so the agent can
    fill in real logic instead of re-deriving the boilerplate.
    """

    name = "scaffold_signal_engine"
    description = (
        "Write a contract-correct code/signal_engine.py stub into a backtest "
        "run directory for a saved hypothesis. The stub satisfies the backtest "
        "runner contract (no-arg SignalEngine, generate(data_map) -> dict of "
        "pd.Series) with a flat no-position default and the signal_definition "
        "embedded as a docstring. Replace the generate body with real logic, "
        "then call backtest(run_dir=...)."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "hypothesis_id": {
                "type": "string",
                "description": "ID of a previously created research hypothesis",
            },
            "run_dir": {
                "type": "string",
                "description": "Backtest run directory (from generate_backtest_config)",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Overwrite an existing signal_engine.py (default false)",
            },
        },
        "required": ["hypothesis_id", "run_dir"],
    }

    def execute(self, **kwargs: Any) -> str:
        try:
            hypothesis_id = str(kwargs.get("hypothesis_id", "")).strip()
            if not hypothesis_id:
                return json.dumps(
                    {"status": "error", "error": "hypothesis_id is required"},
                    ensure_ascii=False,
                )

            run_dir_raw = str(kwargs.get("run_dir", "")).strip()
            if not run_dir_raw:
                return json.dumps(
                    {"status": "error", "error": "run_dir is required"},
                    ensure_ascii=False,
                )

            from src.tools.path_utils import safe_run_dir

            try:
                run_path = safe_run_dir(run_dir_raw)
            except ValueError as exc:
                return json.dumps(
                    {"status": "error", "error": str(exc)}, ensure_ascii=False
                )

            hypothesis = _get_hypothesis(hypothesis_id)
            if hypothesis is None:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Hypothesis not found: {hypothesis_id}",
                        "hint": "Use search_hypotheses to list available hypotheses.",
                    },
                    ensure_ascii=False,
                )

            overwrite = bool(kwargs.get("overwrite", False))
            code_dir = run_path / "code"
            code_dir.mkdir(parents=True, exist_ok=True)
            signal_path = code_dir / "signal_engine.py"
            if signal_path.exists() and not overwrite:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"signal_engine.py already exists: {signal_path}",
                        "hint": "Pass overwrite=true to replace it.",
                    },
                    ensure_ascii=False,
                )

            signal_definition = (
                hypothesis.signal_definition.strip()
                or "(no signal_definition set on the hypothesis)"
            )
            source = _SIGNAL_ENGINE_TEMPLATE.format(
                hypothesis_id=hypothesis.hypothesis_id,
                title=hypothesis.title,
                signal_definition=signal_definition,
            )

            from write_receipt import receipted_write, record_scaffold_hash

            receipt = receipted_write(signal_path, source)
            record_scaffold_hash(run_path)

            return _ok(
                {
                    "receipt": receipt,
                    "signal_engine_path": str(signal_path),
                    "run_dir": str(run_path),
                    "hypothesis": {
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "title": hypothesis.title,
                        "signal_definition": hypothesis.signal_definition,
                    },
                    "next_step": (
                        "Stub written with a flat no-position default. Edit the "
                        "generate() body to implement the signal_definition, then "
                        "call backtest(run_dir=...)."
                    ),
                }
            )

        except Exception as exc:
            return _error(exc)


class LinkAutopilotBacktestTool(BaseTool):
    """Read run_card.json metrics and link the run to a hypothesis.

    After a backtest completes, its metrics live in ``run_card.json``. The
    existing ``link_backtest`` tool requires the agent to hand-extract that
    metrics dict. This tool reads the run card, extracts the scalar metrics,
    and links the run in one step, returning the metrics for thesis evaluation.
    """

    name = "link_autopilot_backtest"
    description = (
        "Read run_card.json from a completed backtest run directory, extract "
        "its metrics, and link the run to a research hypothesis. Returns the "
        "metrics so you can evaluate them against the thesis. Use this after "
        "the backtest tool succeeds."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "hypothesis_id": {
                "type": "string",
                "description": "ID of the hypothesis this backtest tests",
            },
            "run_dir": {
                "type": "string",
                "description": "Backtest run directory containing run_card.json",
            },
            "notes": {
                "type": "string",
                "description": "Optional note about this backtest link",
            },
        },
        "required": ["hypothesis_id", "run_dir"],
    }

    def execute(self, **kwargs: Any) -> str:
        try:
            hypothesis_id = str(kwargs.get("hypothesis_id", "")).strip()
            if not hypothesis_id:
                return json.dumps(
                    {"status": "error", "error": "hypothesis_id is required"},
                    ensure_ascii=False,
                )

            run_dir_raw = str(kwargs.get("run_dir", "")).strip()
            if not run_dir_raw:
                return json.dumps(
                    {"status": "error", "error": "run_dir is required"},
                    ensure_ascii=False,
                )

            from src.tools.path_utils import safe_run_dir

            try:
                run_path = safe_run_dir(run_dir_raw)
            except ValueError as exc:
                return json.dumps(
                    {"status": "error", "error": str(exc)}, ensure_ascii=False
                )

            card_path = run_path / "run_card.json"
            if not card_path.exists():
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"run_card.json not found in {run_path}",
                        "hint": "Run the backtest tool first; it writes run_card.json.",
                    },
                    ensure_ascii=False,
                )

            try:
                card = json.loads(card_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"run_card.json parse error: {exc}",
                    },
                    ensure_ascii=False,
                )
            if not isinstance(card, dict):
                return json.dumps(
                    {
                        "status": "error",
                        "error": "run_card.json must contain a JSON object",
                    },
                    ensure_ascii=False,
                )

            missing = _missing_hypothesis_evidence_artifacts(run_path)
            if missing:
                return json.dumps(
                    {
                        "status": "error",
                        "error": (
                            "Run is missing required hypothesis evidence "
                            f"artifacts: {', '.join(missing)}"
                        ),
                        "hint": (
                            "Run the backtest to produce config, strategy, "
                            "metrics.csv, equity.csv, and run_card.json before linking."
                        ),
                    },
                    ensure_ascii=False,
                )

            signal_path = run_path / "code" / "signal_engine.py"
            if _is_unimplemented_scaffold_signal_engine(signal_path):
                trades_path = run_path / "artifacts" / "trades.csv"
                smoke_only = (
                    _csv_data_row_count(trades_path) == 0
                    and _strategy_hash_matches_card(card, signal_path)
                )
                return json.dumps(
                    {
                        "status": "error",
                        "error": (
                            "code/signal_engine.py is still the auto-scaffolded "
                            "flat 0.0 placeholder"
                        ),
                        "strategy_implementation_status": "scaffold",
                        "provenance_valid": False,
                        "run_purpose": "smoke_only" if smoke_only else "not_hypothesis_valid",
                        "hint": (
                            "Overwrite SignalEngine.generate() with hypothesis "
                            "logic before linking this run as evidence."
                        ),
                    },
                    ensure_ascii=False,
                )

            invalid_metadata = _invalid_hypothesis_evidence_metadata(card)
            if invalid_metadata is not None:
                return json.dumps(
                    {
                        "status": "error",
                        "error": (
                            "run_card.json is not marked as hypothesis-valid "
                            "implemented evidence"
                        ),
                        **invalid_metadata,
                    },
                    ensure_ascii=False,
                )

            warning: str | None = None
            metrics = card.get("metrics") if isinstance(card, dict) else None
            if not isinstance(metrics, dict):
                metrics = {}
                warning = "run_card.json had no 'metrics' object; linked with empty metrics"

            try:
                hypothesis = HypothesisRegistry().link_backtest(
                    hypothesis_id,
                    backtest_run_dir=str(run_path),
                    metrics=metrics,
                    notes=str(kwargs.get("notes", "")),
                )
            except KeyError:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Hypothesis not found: {hypothesis_id}",
                        "hint": "Use search_hypotheses to list available hypotheses.",
                    },
                    ensure_ascii=False,
                )

            payload: dict[str, Any] = {
                "metrics": metrics,
                "run_dir": str(run_path),
                "hypothesis": {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "title": hypothesis.title,
                    "status": hypothesis.status,
                    "run_cards_count": len(hypothesis.run_cards),
                },
                "next_step": (
                    "Backtest linked. Evaluate the metrics against the thesis, "
                    "then record_evidence / add_goal_evidence to close the loop."
                ),
            }
            if warning:
                payload["warning"] = warning
            return _ok(payload)

        except Exception as exc:
            return _error(exc)
