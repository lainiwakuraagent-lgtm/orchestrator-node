# orchestrator-node

An autonomous Claude agent built to absorb Andrii's thinking, clone his prolonged vision,
and eventually orchestrate a team of agents — without being told what to do each time.

Built on the `blank_node` harness by @Lain. Deployed 2026-07-11.

---

## What this is

The orchestrator is not a task executor. It is a **thinking partner** and **vision accumulator**.

Its purpose:
- Absorb Andrii's thoughts, ideas, and long-term vision across hundreds of conversations
- Build an internal model of how Andrii thinks, what he values, where he wants to go
- Develop its own quality standards and identity over time
- Eventually direct @Lain and other agents with enough fidelity to Andrii's intent
  that he doesn't need to re-explain goals constantly

It has no pre-assigned identity. It builds one.

---

## Trigger model

Unlike most node agents, this one runs on **Telegram triggers only** — no nightly timer.

```
Telegram message arrives
  → telegram_poll_trigger.sh (runs every 5 min via systemd)
  → Writes message to state/telegram_incoming.txt
  → Triggers wake.sh (TRIGGER_MODE=manual)
  → Claude session starts, reads message, responds
  → Session ends after processing
```

---

## Directory structure

```
orchestrator-node/
├── scripts/
│   ├── wake.sh                       # Launcher (manual/telegram mode only)
│   ├── telegram-poll.{service,timer} # Telegram polling systemd units
│   └── ...
├── tools/
│   ├── telegram_poll_trigger.sh      # Poll Telegram + trigger session if messages found
│   ├── telegram_send.sh              # Send message (uses identity/agent.env token)
│   ├── check_replies.sh              # Read incoming messages at session start
│   └── ...
├── prompts/
│   ├── wrapper_prompt.md             # Session scaffolding
│   ├── goal.txt                      # Session goal
│   ├── persona.txt                   # Blank identity — builds over time
│   └── initial_briefing.md          # First-session orientation (archived when internalized)
├── identity/
│   ├── agent.env                     # Telegram token + chat ID (gitignored)
│   └── credentials.md               # GitHub PAT, Nexus creds (gitignored)
├── state/                            # Runtime state (mostly gitignored)
├── logs/                             # Session outputs (gitignored)
└── memory/                           # Agent memory — persists across sessions (gitignored)
```

---

## Installing the Telegram polling timer

```bash
# 1. Copy systemd units
cp scripts/telegram-poll.service ~/.config/systemd/user/
cp scripts/telegram-poll.timer ~/.config/systemd/user/

# 2. Enable and start
systemctl --user daemon-reload
systemctl --user enable --now telegram-poll.timer

# 3. Verify
systemctl --user status telegram-poll.timer
```

---

## Origin

Built by @Lain — `lainiwakuraagent-lgtm/lain-node` — as the second agent in an evolving swarm.
Base template: `lainiwakuraagent-lgtm/node`

눈_눈
