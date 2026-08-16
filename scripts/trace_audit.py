import json
from pathlib import Path

trace_path = Path("/home/idona/MoStar/_apps/financial/Vibe-Trading-main/Vibe-Trading-main/agent/sessions/991942fcaee0/trace.jsonl")

print("=== TOOL CALLS IN TRACE (session 991942fcaee0) ===")
with open(trace_path, encoding="utf-8", errors="replace") as f:
    for i, line in enumerate(f):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = e.get("type", e.get("event", ""))
        tool = e.get("tool_name") or e.get("tool") or e.get("name") or ""
        if "tool" in str(etype).lower() or tool:
            args = json.dumps(e.get("args") or e.get("arguments") or e.get("input") or "")[:150]
            result = str(e.get("result") or e.get("output") or "")[:150]
            ts = e.get("timestamp", e.get("ts", ""))
            print(f"[{i}] {ts} | type={etype} | tool={tool}")
            if args and args != '""':
                print(f"     args: {args}")
            if result:
                print(f"     result: {result}")
