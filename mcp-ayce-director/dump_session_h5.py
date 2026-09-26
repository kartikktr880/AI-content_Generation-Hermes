"""H5 evidence extraction — READ-ONLY dump of the Hermes H5 composition
session. Mirrors mcp-ayce-readonly/dump_session.py (H2) but also allows the
director write tool in the AYCE-tool allowlist and can target a specific
session id (default: latest session).

Proves which tools the agent actually used (MCP-only boundary check).

Usage (readonly venv):
    mcp-ayce-readonly\\.venv\\Scripts\\python mcp-ayce-director\\dump_session_h5.py [session_id]
"""
import os
import sqlite3
import sys

db = os.path.join(os.environ["LOCALAPPDATA"], "hermes", "state.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

if len(sys.argv) > 1:
    sid = sys.argv[1]
else:
    sid = con.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0]
print("session_id:", sid)

rows = con.execute(
    "SELECT id, role, tool_name, tool_calls, substr(content,1,700) AS c, timestamp"
    " FROM messages WHERE session_id=? ORDER BY id",
    (sid,),
).fetchall()
print("messages in session:", len(rows))
for r in rows:
    line = f"[{r['id']}] {r['role']} tool={r['tool_name']}"
    if r["tool_calls"]:
        line += " TOOL_CALLS=" + str(r["tool_calls"])[:400]
    print(line)
    content = (r["c"] or "").replace("\n", " ")
    if content:
        print("    content:", content[:400])

# explicit boundary check: any non-AYCE tool calls in this session?
non_ayce = con.execute(
    "SELECT DISTINCT tool_name FROM messages"
    " WHERE session_id=? AND tool_name IS NOT NULL"
    " AND tool_name NOT IN ('list_runs','get_run_state','get_run_artifacts',"
    "'trigger_golden_path')",
    (sid,),
).fetchall()
print("NON-AYCE tools used in this session:", [r[0] for r in non_ayce] or "NONE")
con.close()
