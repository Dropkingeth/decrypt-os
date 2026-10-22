# ICT assistant (workspace 4)

A packaged OpenClaw agent that acts as a personal ICT (Inner Circle Trader) mentor. It runs on the customer Claude runtime (`app/harness`) as agent `ict` and shares the user's Claude sign-in with the product assistant.

| Path | What it is |
| --- | --- |
| `openclaw-agent.json` | OpenClaw config fragment for agent `ict` (bootstrap limits, tools). `harness/bootstrap.py` merges it and sets the workspace. |
| `workspace/SOUL.md` | Persona and boundaries. |
| `workspace/AGENTS.md` | Operating rules: tools, answer method, chart-review checklist. |
| `workspace/IDENTITY.md` | Name and time zone. |
| `knowledge/*.md` | Own-words ICT knowledge pack. Appended to AGENTS.md at runtime start, so the agent needs no file tools. |
| `ict_clock.py` | New York session/killzone/macro clock. `--json` gives Waybar output. |
| `launcher.py` | Workspace 4 launcher: shows the clock, then starts/unlocks/connects as needed and opens the ICT terminal. |
| `desktop/decrypt-ict-assistant.desktop` | Desktop entry for the launcher. |

Tools: `session_status` (exact time) and `web_fetch` (public pages). No shell, files, browser, broker or trading tools.

Content rules: summaries are in our own words. ICT's videos and transcripts are his copyrighted work, so the pack links to his official playlists instead of copying them. Nothing in this folder may contain account data, P&L, owner notes or private operations.

Open it: Connections > Open ICT assistant, or run `/usr/local/bin/decrypt-ict-assistant` (installed) / `python3 app/ict-assistant/launcher.py` (source tree).
