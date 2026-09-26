"""Stage 2 — ONE controlled research execution through the REAL Director
MCP stdio transport (execute_research_slice -> real subprocess worker ->
real yt-dlp ingestion). Mirrors the H4 controlled-execution convention.

Run with the director's isolated venv:
    set PYTHONPATH=<repo>\\src
    mcp-ayce-director\\.venv\\Scripts\\python mcp-ayce-director\\research_roundtrip.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

SERVER = HERE / "server.py"

REQUEST = {
    "objective": "Identify outlier opening-hook formats in the AI productivity tools niche",
    "niche": "AI productivity tools",
    "query": "AI productivity tools",
    "max_outliers": 2,
    "max_videos": 3,
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(REPO / "src")
    # bounded, single controlled run against the REAL ledger
    env.setdefault("AYCE_RESEARCH_TIMEOUT_S", "300")

    async def _run():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                print("TOOLS:", names)
                result = await session.call_tool(
                    "execute_research_slice", REQUEST)
                texts = [b.text for b in result.content
                         if getattr(b, "type", "") == "text"]
                return names, json.loads("\n".join(texts))

    names, envelope = asyncio.run(_run())
    out_path = HERE / "research_roundtrip_output.json"
    out_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print("WROTE:", out_path)
    print("ok:", envelope.get("ok"))
    print("research_id:", envelope.get("research_id"))
    print("status:", envelope.get("status"))
    print("candidate_count:", envelope.get("candidate_count"))
    artifact = envelope.get("artifact") or {}
    print("tiers:", artifact.get("tiers"))
    print("clustering:", (artifact.get("provenance") or {}).get("clustering"))
    for outlier in (artifact.get("outliers") or [])[:2]:
        print("-", outlier.get("title"), "|", outlier.get("channel"),
              "| views:", outlier.get("view_count"),
              "| hook:", (outlier.get("hook_summary") or "")[:80])
    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())