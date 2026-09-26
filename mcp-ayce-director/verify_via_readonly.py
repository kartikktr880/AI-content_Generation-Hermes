"""H4 independent verification probe — observes the controlled run through
the EXISTING read-only MCP server (mcp-ayce-readonly/server.py).

This script NEVER reads the run directory directly: it uses the real MCP
stdio transport against the real read-only server, exactly like Hermes.

Run with the READ-ONLY server's venv (it has the mcp SDK):
    mcp-ayce-readonly\\.venv\\Scripts\\python mcp-ayce-director\\verify_via_readonly.py <run_id>
"""

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
READONLY_SERVER = REPO / "mcp-ayce-readonly" / "server.py"


async def main() -> int:
    run_id = sys.argv[1]
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {k: v for k, v in os.environ.items()}
    env["AYCE_RUNS_ROOT"] = str(REPO / "data" / "runs")

    params = StdioServerParameters(command=sys.executable, args=[str(READONLY_SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            state = await session.call_tool("get_run_state", {"run_id": run_id})
            artifacts = await session.call_tool("get_run_artifacts", {"run_id": run_id})
            for label, call in (("GET_RUN_STATE", state), ("GET_RUN_ARTIFACTS", artifacts)):
                texts = [b.text for b in call.content if getattr(b, "type", "") == "text"]
                print(f"{label}:")
                print(json.dumps(json.loads("\n".join(texts)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
