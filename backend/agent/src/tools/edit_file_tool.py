"""Edit file tool: find-and-replace in workspace files."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.tools.path_utils import allowed_write_roots
from src.tools.path_utils import resolve_safe_path
from src.tools.redaction import redact_internal_paths
from src.tools.signal_engine_contract import (
    SignalEngineContractError,
    validate_signal_engine_contract,
)
from write_receipt import (
    detect_silencer_patterns,
    receipted_write,
    reject_trust_marker_path,
    TrustMarkerWriteRejected,
    WriteNotProvenError,
)


class EditFileTool(BaseTool):
    """Find and replace the first occurrence of a string in a workspace file."""

    name = "edit_file"
    description = "Find and replace the first occurrence of old_text with new_text in a file."
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to run_dir"},
            "old_text": {"type": "string", "description": "Text to find"},
            "new_text": {"type": "string", "description": "Text to replace with"},
        },
        "required": ["path", "old_text", "new_text"],
    }
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        """Perform find-and-replace.

        Args:
            **kwargs: Must include path, old_text, new_text. Optional run_dir.

        Returns:
            JSON string with the operation result or an error.
        """
        file_path = kwargs["path"]
        old_text = kwargs["old_text"]
        new_text = kwargs["new_text"]
        run_dir = kwargs.get("run_dir")

        try:
            resolved = resolve_safe_path(
                file_path=file_path,
                run_dir=run_dir,
                allowed_roots=allowed_write_roots(),
                purpose="write",
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
            )

        try:
            reject_trust_marker_path(resolved)
        except TrustMarkerWriteRejected as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                },
                ensure_ascii=False,
            )

        if not resolved.exists():
            return json.dumps(
                {
                    "status": "error",
                    "error": f"File not found: {file_path}",
                },
                ensure_ascii=False,
            )

        try:
            content = resolved.read_text(encoding="utf-8")
            if old_text not in content:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"old_text not found in {file_path}",
                    },
                    ensure_ascii=False,
                )
            new_content = content.replace(old_text, new_text, 1)
            contract_receipt = None
            candidate_bytes = new_content.encode("utf-8")
            if resolved.name == "signal_engine.py":
                contract_receipt = validate_signal_engine_contract(
                    new_content,
                    signal_path=resolved,
                    run_dir=resolved.parent.parent if resolved.parent.name == "code" else None,
                    candidate_bytes=candidate_bytes,
                )
            receipt = receipted_write(resolved, new_content)
            if contract_receipt is not None:
                contract_hash = contract_receipt.get("signal_engine_sha256")
                if receipt["sha256"] != contract_hash:
                    raise WriteNotProvenError(
                        "write_receipt.sha256 != contract_receipt.signal_engine_sha256"
                    )
            payload = {"status": "ok", **receipt}
            if contract_receipt is not None:
                payload["contract_receipt"] = contract_receipt
            if resolved.name == "signal_engine.py":
                lint_warnings = detect_silencer_patterns(new_content)
                if lint_warnings:
                    payload["lint_warnings"] = lint_warnings
            return json.dumps(
                payload,
                ensure_ascii=False,
            )
        except SignalEngineContractError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                    "contract_receipt": {"status": "error"},
                },
                ensure_ascii=False,
            )
        except WriteNotProvenError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_internal_paths(str(exc)),
                },
                ensure_ascii=False,
            )
