# H2.5 ground-truth capture: call the three AYCE read-only MCP tools against
# the REAL data/runs root via the real MCP stdio client, and print a compact
# summary that becomes the answer key for the credential-gated agent run.
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = HERE / "server.py"

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    env = {k: v for k, v in os.environ.items()}
    env["AYCE_RUNS_ROOT"] = str(REPO / "data" / "runs")
    params = StdioServerParameters(
        command=str(HERE / ".venv" / "Scripts" / "python.exe"),
        args=[str(SERVER)],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", sorted(t.name for t in tools.tools))

            listing = await session.call_tool("list_runs", {})
            runs = json.loads(listing.content[0].text)
            print("LIST_RUNS ok:", runs["ok"], "count:", len(runs["runs"]))
            for r in runs["runs"]:
                print("  ", r.get("run_id"), "stages:", r.get("stage_count"),
                      "ok:", r.get("stages_succeeded"), "failed:", r.get("stages_failed"))

            if target:
                state = await session.call_tool("get_run_state", {"run_id": target})
                sdata = json.loads(state.content[0].text)
                print("GET_RUN_STATE ok:", sdata["ok"], "run_id:", sdata.get("run_id"))
                print("  job_id:", sdata["data"]["job_id"])
                for name, rec in sdata["data"]["stages"].items():
                    print("   stage:", name, "->", rec["status"])
                arts = await session.call_tool("get_run_artifacts", {"run_id": target})
                adata = json.loads(arts.content[0].text)
                print("GET_RUN_ARTIFACTS ok:", adata["ok"], "count:", adata["artifact_count"])
                for a in adata["artifacts"]:
                    print("   artifact:", a["kind"], "stage:", a["stage"], "path:", a["path"])


asyncio.run(main())
