"""H2.5 evidence extraction — READ-ONLY dump of the latest Hermes session.

Reads %LOCALAPPDATA%/hermes/state.db in SQLite read-only mode and prints
the latest session's messages: roles, tool names, tool-call arguments,
and truncated content — proving which tools the agent actually used.
"""
import os
import sqlite3

db = os.path.join(os.environ["LOCALAPPDATA"], "hermes", "state.db")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

sid = con.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()[0]
print("latest session_id:", sid)

rows = con.execute(
    "SELECT id, role, tool_name, tool_calls, substr(content,1,700) AS c, timestamp"
    " FROM messages WHERE session_id=? ORDER BY id",
    (sid,),
).fetchall()
print("messages in session:", len(rows))
for r in rows:
    line = f"[{r['id']}] {r['role']} tool={r['tool_name']}"
    if r["tool_calls"]:
        line += " TOOL_CALLS=" + str(r["tool_calls"])[:300]
    print(line)
    content = (r["c"] or "").replace("\n", " ")
    if content:
        print("    content:", content[:400])

# explicit boundary check: any non-ayce tool calls in this session?
non_ayce = con.execute(
    "SELECT DISTINCT tool_name FROM messages"
    " WHERE session_id=? AND tool_name IS NOT NULL"
    " AND tool_name NOT IN ('list_runs','get_run_state','get_run_artifacts')",
    (sid,),
).fetchall()
print("NON-AYCE tools used in this session:", [r[0] for r in non_ayce] or "NONE")
con.close()
