from pathlib import Path
from dotenv import dotenv_values

p = Path("agent/.env")
try:
    vals = dotenv_values(p)
    print("OK: agent/.env parsed")
    print("Loaded keys:", ", ".join(sorted(k for k in vals if k)))
except Exception as e:
    print("BAD .env:", e)
    print("Show with line numbers:")
    for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
        safe = line
        for word in ["KEY", "TOKEN", "SECRET", "PASSWORD"]:
            if word in safe.upper() and "=" in safe:
                safe = safe.split("=", 1)[0] + "=REDACTED"
        print(f"{i}: {safe}")
