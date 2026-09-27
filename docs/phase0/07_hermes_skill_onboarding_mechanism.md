# TASK 0.6 — HERMES SKILL ONBOARDING MECHANISM DISCOVERY

**Scope.** Discover and EVIDENCE Hermes' real supported programmatic skill
onboarding mechanism from the installed runtime/source. NO skill was
installed, selected, or onboarded. No MCP touched; no config edited; no
framework created.

## 1. Inspected installation

| Item | Value |
|---|---|
| Hermes version | `Hermes Agent v0.21.5+2244.g35ad70c (2026.9.24)`, install method `git`, Python 3.14.7 |
| Install dir | `C:\Users\Administrator\AppData\Local\hermes\hermes-agent` |
| HERMES_HOME (profile) | `C:\Users\Administrator\AppData\Local\hermes` |
| Skills CLI | `hermes skills {trust,untrust,browse,search,install,inspect,list,check,update,audit,uninstall,reset,list-modified,diff,opt-out,opt-in,repair-official,publish,snapshot,tap,config}` (exact `--help` output captured) |

## 2. Actual loaded-skill listing (runtime evidence)

`hermes skills list` → **`0 hub-installed, 51 builtin, 0 local — 51 enabled, 0 disabled`**;
all 51 builtin skills enabled across categories (autonomous-ai-agents, creative,
email, media, note-taking, productivity, research, software-development, web).
Columns: Name / Category / Source(`builtin`) / Trust(`builtin`) / Status(`enabled`).

## 3. Where skills live (verified on disk)

| Path | Role | Evidence |
|---|---|---|
| `%LOCALAPPDATA%\hermes\skills\` | THE live skills tree (`SKILLS_DIR`). ALL skills live here, **seeded from bundled** on first run — `tools/skills_tool.py:62`: "all skills live in ~/.hermes/skills/ (seeded from bundled)" | dirs: `apple, autonomous-ai-agents, creative, devops, email, media, note-taking, productivity, research, social-media, software-development, web` |
| `%LOCALAPPDATA%\hermes\skills\.bundled_manifest` | `name:hash` map tracking bundled-seeded skills (enables `list-modified`/`diff`/`reset`/user-modified preservation) | file inspected: `airtable:3b1f…`, `arxiv:b7230…`, … |
| `%LOCALAPPDATA%\hermes\skills\.hub\` | Skills Hub state: `lock.json` (`{"version":1,"installed":{}}`), `taps.json` (`{"taps":[]}`), `audit.log`, `quarantine\`, `index-cache\` | `ensure_hub_dirs()` — `tools/skills_hub.py:406-417`; initialized by Hermes' own `skills list` run during this inspection |
| `hermes-agent\skills\`, `hermes-agent\optional-skills\` | bundled source (repo side) that seeds the profile | dirs present |
| `<project>\.hermes\skills\`, `<project>\.agents\skills\` | PROJECT-LOCAL skills (highest precedence, override same-named profile skills) — load ONLY when the project root is trusted | `agent/skill_utils.py:429-435` `PROJECT_SKILLS_SUBDIRS` |

## 4. How Hermes discovers skills (source-verified)

- Discovery entry: `tools/skills_tool.py::_find_all_skills` (re-exported by
  `hermes_cli/skills_config.py:_list_all_skills`). Per-session scan cache with
  **30 s TTL** (`_SKILLS_CACHE`, `_SKILLS_CACHE_TTL_SECONDS = 30.0`) — new/changed
  skills are re-scanned automatically.
- Skills = a directory holding `SKILL.md` (YAML frontmatter + instructions) +
  optional `references/ templates/ assets/ scripts/` (`skills_tool.py:2-5`).
- Category derives from the path under SKILLS_DIR
  (`~/.hermes/skills/<category>/<name>/SKILL.md`).
- Enable/disable: `config.yaml` `skills.disabled` (+ `skills.platform_disabled`
  per platform) — `hermes_cli/skills_config.py:get_disabled_skills/
  save_disabled_skills`; `ESSENTIAL_SKILLS` can never be disabled.
- External skill dirs: `skills.external_dirs` config key
  (`agent/skill_utils.py:get_external_skills_dirs`).
- Project-local trust: root must be listed in `skills.trusted_project_dirs`
  (set by `hermes skills trust <path>`); untrusted project skills are detected
  and quarantined (scan-time injection defense, `skill_utils.py:533-580`).


## 5. The supported onboarding mechanism (CLI → files → runtime)

**Primary mechanism: `hermes skills install <identifier>`** — exact help:

```text
usage: hermes skills install [-h] [--category CATEGORY] [--name NAME]
                             [--force] [--yes] identifier
identifier: Skill identifier (e.g. openai/skills/skill-creator)
            or a direct HTTP(S) URL to a SKILL.md file
--yes, -y   Skip confirmation prompt (needed in TUI mode)
```

Flow (source-verified):

1. **Resolve** the identifier through source adapters — default registries:
   `official`/`hermes-index`, `skills.sh` (`https://skills.sh`,
   `tools/skills_hub_skillssh.py:24`), `well-known` (ANY site exposing
   `/.well-known/skills/index.json`), `github` (taps), `clawhub`, `lobehub`,
   `browse.sh` (`hermes_cli/subcommands/skills.py:16`,
   `tools/skills_hub_search.py:87`). Custom GitHub sources: `hermes skills tap
   add <repo>` → persisted in `.hub/taps.json`; current state: "No custom taps
   configured. Using default sources only."
2. **Safety scan** — hub skills are scanned; a `dangerous` verdict blocks
   install for community/trusted sources unless `--force`
   (`hermes_cli/skills_hub.py:495-513`); suspect bundles go to `.hub/quarantine\`
   (`install_from_quarantine` path exists for reviewed content).
3. **Install** into `SKILLS_DIR/<category>/<name>` and record the install in
   `.hub/lock.json` (`HubLockFile`, `install_path`) + `.hub/audit.log`
   (`hermes_cli/skills_hub.py:749-750`: "Installed: <relative path>").
4. **Preview without installing:** `hermes skills inspect <identifier>`.
5. **Enable/disable after install:** `hermes skills config` (interactive
   checklist, global or per platform) or config.yaml `skills.disabled`.
6. **Updates/removal:** `hermes skills check|update|audit|uninstall` (hub
   skills); bundled skills: `opt-out/opt-in`, `list-modified`, `diff`, `reset`,
   `repair-official`. Reproducible export/import: `hermes skills snapshot`.

**Project-local alternative (no registry, fully local):** place a skill
directory under `<repo>\.hermes\skills\` (or `.agents\skills\`) and run
`hermes skills trust <repo-root>` → adds the root to config.yaml
`skills.trusted_project_dirs`; project skills then load and OVERRIDE
same-named profile/bundled skills (with quarantine scan on trust).

## 6. Reload / restart behavior (dynamic, NOT startup-only)

- `agent/prompt_builder.py:1115-1182`: skills enter the system prompt via a
  **two-layer cache** (in-process LRU per profile×platform, then a disk
  snapshot `%LOCALAPPDATA%\hermes\.skills_prompt_snapshot.json`, version 3)
  that is **validated against a file-signature manifest of every SKILL.md /
  DESCRIPTION.md** — any added/removed/changed skill invalidates the snapshot
  and it is **rebuilt automatically on the next prompt build**. No Hermes
  restart is required for skill-file changes.
- The in-agent `skills_list`/`skill_view` tool re-scans with the 30 s TTL
  cache (staleness bound for in-place edits).
- Hub metadata (lock/audit) is updated transactionally at install time; the
  skills *prompt surface* picks changes up via manifest invalidation.

## 7. Layer separation (dashboard vs config vs runtime)

| Layer | Mechanism |
|---|---|
| Dashboard/UI (`hermes_cli/web_routers/skills.py`) | presentation over the same state; same source ids ("skills-sh": "skills.sh") |
| Persisted configuration | `%LOCALAPPDATA%\hermes\skills\` tree + `.hub\{lock.json,taps.json,audit.log}` + `config.yaml` `skills.{disabled,platform_disabled,trusted_project_dirs,external_dirs}` + `.bundled_manifest` |
| Runtime loading | `tools/skills_tool._find_all_skills` (30 s TTL scan) → `agent/skill_utils` filters (disabled/platform/environment/apps) → `agent/prompt_builder` manifest-validated snapshot |

## 8. Evidence captured (all from the installed runtime)

- `hermes skills --help` and subcommand helps (`install/trust/list/config/tap/inspect`) — exact outputs recorded above.
- `hermes skills list` — 51 builtin enabled / 0 hub / 0 local.
- `hermes skills tap list` — "No custom taps configured. Using default sources only."
- Disk: `skills\` category tree, `.bundled_manifest` (name:hash entries), `.hub\` with empty `lock.json`/`taps.json` + `audit.log` + `quarantine\` + `index-cache\`.
- Source files (read, not modified): `tools/skills_hub.py` (dynamic path constants, lock/scan/`ensure_hub_dirs`), `tools/skills_hub_install.py`, `tools/skills_hub_search.py`, `tools/skills_hub_skillssh.py`, `tools/skills_hub_sources.py`, `hermes_cli/skills_config.py`, `hermes_cli/subcommands/skills.py`, `tools/skills_tool.py`, `agent/skill_utils.py`, `agent/prompt_builder.py:1115-1182`.

## 9. Limitations / still unknown

- Registry sources are NETWORK-dependent (skills.sh / GitHub / ClawHub / …);
  install-time reachability and content quality were NOT tested in this task
  (no install permitted).
- The exact interactive `install` confirmation text in TUI mode and the
  dashboard skill UX were not exercised (dashboard explicitly out of scope).
- Whether a specific future research-selected skill installs via identifier
  vs URL depends on that source; proven per-skill at onboarding time.
- `hermes update` reports 930 commits behind upstream — the mechanism may
  evolve; re-verify after any Hermes update.

## 10. Recommended next onboarding action

For a research-selected skill: prefer (a) `hermes skills install <identifier>
--yes` from an official/trusted source, then `hermes skills list` +
`hermes skills config` to enable; or (b) for a project-specific skill we
author ourselves, place it under `AI-Content-Generation\.hermes\skills\<name>\
SKILL.md` and run `hermes skills trust
C:\Users\Administrator\AI-Content-Generation` (no network, full control,
native precedence + override). Verify with `hermes skills list --source local`
and a new Hermes session.

## 11. Scope discipline honored

No skill installed. No config.yaml change (no `skills:` block exists —
verified). `ayce-readonly` / `ayce-director` untouched. Only Hermes-native
side effect: `hermes skills list` initialized the empty `.hub\` skeleton
(Hermes' own behavior; empty lock/taps, no skills).

## SKILL_ONBOARDING_MECHANISM = VERIFIED