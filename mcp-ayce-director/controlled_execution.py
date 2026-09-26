"""H4 controlled execution probe — runs EXACTLY ONE real trigger_golden_path
over the REAL MCP stdio transport (no stub, no mock, no LLM).

Prerequisites (checked before use):
- all mcp-ayce-director/test_server.py tests green
- no other director request in flight (single-flight will reject otherwise)

Evidence printed: full tool result + the director ledger record.
The resulting run is verified separately via the EXISTING read-only MCP
server (verify_via_readonly.py) — the director itself performs no
filesystem inspection of the run.

Usage (director venv):
    mcp-ayce-director\\.venv\\Scripts\\python mcp-ayce-director\\controlled_execution.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

REQUEST_ID = (
    "req-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-h4-controlled-01"
)


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {k: v for k, v in os.environ.items()}
    env.pop("AYCE_DIRECTOR_STUB_RUNNER", None)  # REAL execution — no stub
    env.pop("AYCE_DIRECTOR_STUB_RESULT", None)

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("trigger_golden_path", {
                "request_id": REQUEST_ID,
                "command": "trigger_golden_path",
                "script_id": "documentary",
                "reason": "H4 controlled experiment: single real Golden Path "
                          "execution proving request_id -> AYCE run -> "
                          "read-only observation correlation",
            })
            texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
            payload = json.loads("\n".join(texts))

    print(json.dumps(payload, indent=2))
    ledger = HERE / "requests.json"
    if ledger.is_file():
        records = json.loads(ledger.read_text(encoding="utf-8"))["requests"]
        mine = [r for r in records if r.get("request_id") == REQUEST_ID]
        print("LEDGER RECORD:")
        print(json.dumps(mine, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
