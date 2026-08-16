"""write_receipt.py — MST-0001 at the file layer.

Law: no write without a receipt; no run against an unproven strategy body.

Replaces bare open()/write() calls in agent tools (ScaffoldSignalEngineTool,
any code-editing tool) so that "successfully updated" becomes impossible to
say without {path, bytes_written, sha256} in hand. The runner then refuses
to execute a signal_engine.py whose hash still equals the scaffold's.

Integration points (per your codemap):
  1. autopilot_tool.py :: ScaffoldSignalEngineTool.execute()
       -> use receipted_write() for the scaffold, then record_scaffold_hash()
  2. whatever tool the agent uses to overwrite code/signal_engine.py
       -> use receipted_write(); return the receipt dict as the tool output
          verbatim, so the receipt (not prose) is what enters the transcript
  3. backtest/runner.py, immediately after resolving run_dir, before import
       -> assert_not_scaffold(run_dir)
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time
from pathlib import Path


class WriteNotProvenError(RuntimeError):
    """Raised when a write cannot be verified back from disk."""


class ScaffoldNeverReplacedError(RuntimeError):
    """Raised when a run is attempted against the untouched scaffold body."""


class TrustMarkerWriteRejected(RuntimeError):
    """Raised when a caller tries to write a trust-marker file directly."""


SCAFFOLD_HASH_FILENAME = ".scaffold.sha256"  # lives next to signal_engine.py
STRATEGY_PROVENANCE_FILENAME = ".strategy_provenance.json"  # lives next to signal_engine.py

# Filenames whose contents assert_not_scaffold() trusts to skip refusal or
# drop the 'unverified:' prefix. record_scaffold_hash() and
# mark_deterministic_baseline() are the only functions that may write them —
# never an agent-facing tool. A write_file/edit_file call reaching one of
# these directly would let an agent self-declare its own strategy body
# verified, which is the exact self-attestation the scaffold gate exists to
# prevent. See reject_trust_marker_path().
TRUST_MARKER_FILENAMES = frozenset({SCAFFOLD_HASH_FILENAME, STRATEGY_PROVENANCE_FILENAME})


def reject_trust_marker_path(path: str | os.PathLike) -> None:
    """Raise TrustMarkerWriteRejected if ``path``'s filename is a trust marker.

    Call from every agent-facing write/edit tool before it touches disk.
    Trusted internal code paths (the scaffold generator, the deterministic
    governed endpoint) call record_scaffold_hash() / mark_deterministic_baseline()
    directly and never go through this check.
    """
    name = Path(path).name
    if name in TRUST_MARKER_FILENAMES:
        raise TrustMarkerWriteRejected(
            f"{name} is a trust-marker file; it can only be written by the "
            "scaffold/receipt code paths, not by an agent tool call."
        )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def receipted_write(path: str | os.PathLike, content: str, encoding: str = "utf-8") -> dict:
    """
    Write content to path atomically and prove it landed.

    Returns a receipt: {path, bytes_written, sha256, mtime}.
    Raises WriteNotProvenError if the read-back does not match what was sent.
    Never returns on failure — narration cannot survive this function.
    """
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    data = content.encode(encoding)
    expected_hash = hashlib.sha256(data).hexdigest()

    # Atomic: write to a sibling temp file, fsync, rename over the target.
    tmp = target.with_suffix(target.suffix + ".tmp-receipt")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)

    # Verification is a fresh read from disk — trust the disk, not the buffer.
    actual_hash = _sha256_file(target)
    actual_size = target.stat().st_size

    if actual_hash != expected_hash or actual_size != len(data):
        raise WriteNotProvenError(
            f"Write to {target} not proven: "
            f"expected sha256={expected_hash} size={len(data)}, "
            f"got sha256={actual_hash} size={actual_size}"
        )

    return {
        "path": str(target),
        "bytes_written": actual_size,
        "sha256": actual_hash,
        "mtime": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(target.stat().st_mtime)
        ),
    }


def record_scaffold_hash(run_dir: str | os.PathLike, engine_relpath: str = "code/signal_engine.py") -> str:
    """
    Call immediately after the scaffold is written.
    Freezes the scaffold's identity so the runner can later refuse it.
    """
    run = Path(run_dir).resolve()
    engine = run / engine_relpath
    digest = _sha256_file(engine)
    marker = engine.parent / SCAFFOLD_HASH_FILENAME
    marker.write_text(json.dumps({"scaffold_sha256": digest}) + "\n")
    return digest


def mark_deterministic_baseline(
    run_dir: str | os.PathLike,
    template: str,
    generated_by: str,
    engine_relpath: str = "code/signal_engine.py",
) -> dict:
    """
    Call immediately after a deterministic (non-LLM) route writes a known-good
    strategy body via receipted_write().

    Unlike record_scaffold_hash(), this does NOT freeze the file as "the
    scaffold to be refused" — the body is the final strategy, never a stub
    the model is meant to overwrite. It records *why* the body is trusted
    (generated_by, template) so assert_not_scaffold() can return a verified
    hash instead of the honest-gap 'unverified:' prefix.
    """
    run = Path(run_dir).resolve()
    engine = run / engine_relpath
    digest = _sha256_file(engine)
    provenance = {
        "scaffold_status": "not_scaffold",
        "generated_by": generated_by,
        "template": template,
        "sha256": digest,
    }
    marker = engine.parent / STRATEGY_PROVENANCE_FILENAME
    marker.write_text(json.dumps(provenance) + "\n")
    return provenance


def assert_not_scaffold(run_dir: str | os.PathLike, engine_relpath: str = "code/signal_engine.py") -> str:
    """
    Call in runner.py before importing the strategy module.

    Refuses execution when signal_engine.py is byte-identical to the scaffold.
    Returns the current engine hash on success (stamp it into run_card.json
    as `signal_engine_sha256` — receipts should reach the record).

    A deterministic route (see mark_deterministic_baseline()) can pre-declare
    its strategy body as legitimately non-scaffold; that verified hash is
    returned as-is, unprefixed, as long as the file hasn't changed since.

    If neither marker is present (older run dirs, or ReAct runs that skipped
    record_scaffold_hash), this does NOT block, but returns the hash prefixed
    with 'unverified:' so run cards can show the gap honestly instead of
    hiding it.
    """
    run = Path(run_dir).resolve()
    engine = run / engine_relpath
    if not engine.exists():
        raise ScaffoldNeverReplacedError(f"No strategy body at {engine}")

    current = _sha256_file(engine)

    provenance_marker = engine.parent / STRATEGY_PROVENANCE_FILENAME
    if provenance_marker.exists():
        provenance = json.loads(provenance_marker.read_text())
        if provenance.get("sha256") == current:
            return current

    marker = engine.parent / SCAFFOLD_HASH_FILENAME

    if not marker.exists():
        return f"unverified:{current}"

    scaffold = json.loads(marker.read_text()).get("scaffold_sha256")
    if current == scaffold:
        raise ScaffoldNeverReplacedError(
            f"signal_engine.py in {run} is still the untouched scaffold "
            f"(sha256={current}). A smoke body is not a hypothesis. "
            f"Overwrite the strategy before running."
        )
    return current


_NAN_FALLBACK_TOKENS = ("np.nan", 'float("nan")', "float('nan')", "pd.NA", ".nan", "= nan")


def detect_silencer_patterns(source: str) -> list[str]:
    """Flag exception handlers that convert a loud failure into a quiet nothing.

    A species distinct from the scaffold: real, changed code that survives
    execution and produces artifacts, but whose except clause silently
    degrades a schema mismatch (typically around data_map access) into a
    NaN-filled frame instead of surfacing it. It passes the sha256 gate
    (the file genuinely changed) and passes execution (no exception
    escapes) — the only thing that catches it is reading the body.

    This is a lint, not a gate: it flags, it does not refuse. Callers
    decide whether to surface warnings in a run card, a tool receipt, or
    both.

    Returns:
        Human-readable warning strings, one per offending except handler.
        Empty list when the source has no try/except or nothing suspicious.
    """
    warnings: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return warnings

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        try_segment = ast.get_source_segment(source, node) or ""
        touches_data_map = "data_map" in try_segment

        for handler in node.handlers:
            if handler.type is None:
                warnings.append(
                    f"line {handler.lineno}: bare 'except:' clause"
                    + (" wrapping data_map access" if touches_data_map else "")
                    + " — silent exception handling is narration, just written in Python"
                )
                continue

            handler_segment = ast.get_source_segment(source, handler) or ""
            has_nan_fallback = any(token in handler_segment for token in _NAN_FALLBACK_TOKENS)
            if has_nan_fallback and touches_data_map:
                warnings.append(
                    f"line {handler.lineno}: caught exception falls back to NaN around "
                    "data_map access — a caught error that still produces artifacts is "
                    "a silencer, not a fix"
                )

    return warnings
