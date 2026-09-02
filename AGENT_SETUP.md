# Agent setup guide

This doc is addressed to an AI agent (e.g. Claude) operating this package on
a human's behalf — installing it, gathering credentials from that human, and
wiring it into an agent framework. It is not a human-facing README; if a
human is reading this directly, `README.md` and `SETUP.md` are the docs
meant for them.

Nothing below assumes the human has prior Telegram or agent-framework
knowledge. Ask, don't assume — every credential-gathering step below is
written as one question at a time, waiting for their answer, not a wall of
text to fill in.

## 1. Gather credentials, one at a time

`SETUP.md` in this repo is the single source of truth for *how* to obtain
each of these values (the exact BotFather flow, the my.telegram.org
"application" registration, how to find a numeric Telegram user ID). Read
it yourself first, then walk the human through it conversationally: ask for
one value, wait for their answer, confirm it looks right (e.g. a bot token
is `<digits>:<35 characters>`, a user ID is a bare integer), then move to
the next. Don't paste `SETUP.md`'s full text at them unprompted — quote the
specific step you're currently on, in your own words.

The concrete list of values you need — this is `.env.example` in this
repo's root, read it there for the current, authoritative set rather than
trusting the list below to have stayed in sync — is, as of this doc:

1. **`TELEGRAM_BOT_TOKEN`** — from @BotFather's `/newbot` flow. Ask the
   human to message @BotFather on Telegram, run `/newbot`, follow its
   prompts (bot display name, then a unique `_bot`-suffixed username), and
   paste back the token it returns.
2. **`TELEGRAM_API_ID`** and **`TELEGRAM_API_HASH`** — from
   [my.telegram.org](https://my.telegram.org), registering an
   "application" (this is unrelated to the bot token above; it's what the
   self-hosted `telegram-bot-api` server in this repo's `installer/`
   authenticates itself to Telegram's own servers with). Ask for both
   values together once they've registered the application, since they're
   generated on the same page.
3. **`TELEGRAM_ALLOWED_USER_IDS`** — the numeric Telegram user ID(s)
   allowed to talk to this bot, comma-separated. If the human doesn't
   already know their own numeric ID, the simplest path is telling them to
   message @userinfobot on Telegram and read back the number it replies
   with.
4. **`DISPATCH_PLUGIN`** — not a credential, but still a required `.env`
   value: which dispatch plugin the listener loads at startup, in
   `module.path:factory` form. Don't ask the human to guess this — see
   section 3 below for what it actually controls and what value to set
   once you know which framework you're wiring this into. The shipped
   example is `plugins.claude_agent:build_plugin`.

Once you have all of them, write them into a `.env` file at this repo's
root (`cp .env.example .env` first, then fill it in) rather than asking the
human to edit the file themselves.

## 2. Install and start the listener

This repo ships its own installer under `installer/` — use it directly,
don't re-derive install steps from spoor's own `install.sh` (that one
installs a different, spoor-specific deployment).

```
cp .env.example .env          # if not already done in step 1
# ... fill in .env as gathered above ...
sudo ./installer/install.sh <run-as-user> <path-to-this-repo>
```

Read `installer/install.sh` before running it — it documents what each step
does inline. In order, it: brings up the `telegram-bot-api` Compose service
(`installer/docker-compose.yml`), runs `uv sync --extra
claude-agent-plugin` for `<run-as-user>` (drop the `--extra` if the plugin
you're wiring up in section 3 doesn't need `claude-agent-sdk`), enables
`loginctl linger` for that user, and writes + enables (but does not yet
start) a `telegram-listener.service` systemd `--user` unit for
`<run-as-user>`.

It requires `<run-as-user>` to already exist on the host and have `uv` on
its `PATH`. Create the user first if it doesn't exist yet
(`useradd -m <run-as-user>`, then make sure `uv` is installed for that
user) — the installer deliberately refuses to do this for you, since
creating a system user is exactly the kind of action that should be a
visible, separate step.

Start it once the installer finishes:

```
sudo -u <run-as-user> env XDG_RUNTIME_DIR=/run/user/$(id -u <run-as-user>) \
  systemctl --user start telegram-listener.service
```

### Verify it's actually receiving messages

Don't declare this done on the service reporting `active`. Confirm the
listener is really talking to Telegram:

```
sudo -u <run-as-user> env XDG_RUNTIME_DIR=/run/user/$(id -u <run-as-user>) \
  systemctl --user status telegram-listener.service
```

Then have the human send the bot a message from an allowed account (one of
the IDs in `TELEGRAM_ALLOWED_USER_IDS`), and check it landed:

```
tail -n 5 <path-to-this-repo>/data/telegram-listener/transcript.jsonl
```

A new line with `"direction": "incoming"` matching what they sent means the
listener received and logged it (see `telegram_listener/common.py`'s
`append_transcript` — this is the only direction it ever writes; outgoing
replies go straight to Telegram via `ListenerAPI.send_message` and are
never appended to this file). If the plugin you wired up in section 3 also
replies, confirm that by watching for the reply to actually arrive in the
chat, not by looking for a further transcript line.

### Troubleshooting

- **Service won't start, or dies immediately.** Check
  `journalctl --user -u telegram-listener.service` (same
  `sudo -u <run-as-user> env XDG_RUNTIME_DIR=...` prefix as above). Most
  often this is a missing/malformed `.env` value — the unit's
  `EnvironmentFile=` points straight at this repo's `.env`, so a typo'd key
  name there is silent until the process actually reads it.
- **Listener runs but no messages ever arrive.** Check
  `docker compose -f installer/docker-compose.yml ps` — the
  `telegram-bot-api` container has to be up first, since the listener talks
  to it (not directly to `api.telegram.org`) for `getUpdates`. If it's not
  up, check `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are actually set in
  `.env` before it was brought up (a compose service reads `.env` at
  `up` time, not continuously — `docker compose up -d telegram-bot-api`
  again after fixing `.env`).
- **Considering running the listener itself in a container instead of via
  the systemd `--user` unit the installer sets up — don't.** This repo's
  installer deliberately runs the listener as a real host process, not a
  container, and that's not an arbitrary choice: `telegram-bot-api` (the
  proxy in front of `api.telegram.org`) is stateless and fine as a
  container, but the listener process itself, and especially whatever
  dispatch plugin it's driving, tends to need genuine host identity — a
  writable `uv`/`pip` cache, full filesystem visibility, real git/SSH
  credentials for a plugin that does agent work. A prior containerized
  version of exactly this listener (in `painapple`/spoor's own deployment,
  documented in `/home/spoor/spoor/telegram/README.md`'s "Why this moved
  off Docker" section) hit three separate, structural problems from
  running that way: SSH git pushes failed with
  `ssh: No user exists for uid 1001` (a container UID with no matching
  `passwd` entry breaks OpenSSH's own `getpwuid()` lookup), `uv`'s cache
  directory wasn't writable without overriding `UV_CACHE_DIR` on every
  invocation, and a freshly created sibling directory on the host was
  invisible inside the container because only specific subdirectories, not
  the whole home directory, were ever bind-mounted in. All three are
  structural to containerizing this specific process, not one-off
  misconfigurations — if you're tempted to "just" run the listener in
  Docker too for consistency with `telegram-bot-api`, don't; explain why
  to whoever's asking instead.

## 3. Wire the dispatch plugin up to an agent framework

The listener's own job stops at polling Telegram, filtering to allowed
users, batching a burst of messages from the same chat into one `Turn`, and
handing that `Turn` to whatever plugin `DISPATCH_PLUGIN` names. It never
imports or special-cases any particular framework — read
`telegram_listener/plugin.py` yourself for the authoritative interface
before wiring anything up; what follows is a guide to reading it, not a
substitute for it.

The interface, in short:

- **`Turn`** (`telegram_listener/plugin.py`) — one batched unit of
  conversation: `chat_id`, `message_ids` (every Telegram message folded
  into this turn), `text` (already combined and annotated — sender,
  provenance, attachments folded in as plain text), `sender_name`,
  `sender_id`. A plugin does not need to re-derive any of this from raw
  Telegram fields.
- **`ListenerAPI`** — what a plugin acts back through:
  `send_message(chat_id, text)`, `send_typing(chat_id)`,
  `set_reaction(chat_id, message_id, emoji)`, `log(text)`, and an optional
  `on_event(name, payload)` hook for a downstream dashboard/activity feed
  (`None` unless the process wiring the plugin up actually wants one — a
  plugin must treat a missing hook as a no-op, never assume it exists).
- **`DispatchPlugin`** protocol — a plugin implements `async
  handle_turn(turn, api)` (called once per batched turn),
  `async handle_command(chat_id, message_id, command, argument, api) ->
  bool` (called for any `/command` the listener's own small built-in set
  doesn't recognize; return `False` if your plugin doesn't recognize it
  either), and `describe_commands()` (the plugin's own commands, in
  Telegram `setMyCommands` shape, registered on top of the listener's
  built-ins).
- **`DISPATCH_PLUGIN`** env var — `module.path:factory`, e.g.
  `plugins.claude_agent:build_plugin`. `factory` is called with no
  arguments at listener startup and must return a `DispatchPlugin`
  instance.

### Worked example: `plugins/claude_agent.py`

This repo ships one concrete plugin, `ClaudeAgentPlugin`
(`plugins/claude_agent.py`), wired up via
`DISPATCH_PLUGIN=plugins.claude_agent:build_plugin` in `.env.example`. It
reimplements spoor's own current Telegram wiring — one resumable
`claude-agent-sdk` session per chat, replies streamed back as they
arrive — purely as a `DispatchPlugin`, matching how
`/home/spoor/spoor`'s `prompts/respond_to_telegram.md` drives spoor's own
listener: a fresh or resumed agent session handles a whole batched turn,
not one call per raw Telegram message.

Concretely, it:

- keeps one `ClaudeSDKClient` per `chat_id` (`self._clients`), persisting
  each chat's session ID to disk (`_sessions_file()`) so a process restart
  resumes the same conversation instead of starting over;
- on `handle_turn`, sends a typing indicator, gets or creates that chat's
  client, `query()`s it with `turn.text`, and streams every `AssistantMessage`
  text block back via `api.send_message` as it arrives — this is the same
  "the chat is a window onto the session" behavior
  `respond_to_telegram.md` describes for spoor's own deployment;
  - registers two of its own commands (`describe_commands()`): `/clear`
  (disconnect and drop the persisted session, so the next message starts
  fresh) and `/compact` (send `/compact` to the underlying session).

If you're wiring this repo into a spoor-style framework, this plugin is
usually the right starting point to copy and adapt rather than writing a
plugin from scratch — change `DEFAULT_SYSTEM_PROMPT`, the model
(`CLAUDE_AGENT_MODEL` in `.env`), and `COMMANDS`/`handle_command` to match
the target framework's own conventions.

It deliberately does **not** reproduce every feature of spoor's own
`ChatSessionManager` — activity-bubble editing (collapsing tool calls into
one live-updating "bezig" bubble), daily session rotation, context-size-
triggered auto-compaction, `/sleep`/`/cancel` turn interruption, or voice
replies. Those are real spoor product behaviors, not requirements of the
plugin interface itself. Treat them as a checklist of extension points a
production deployment likely wants, each implementable as an addition to a
plugin's own `handle_turn`/`handle_command`/`describe_commands`, not as
something missing from the interface that needs the listener core changed.

### If you're writing a new plugin instead

Implement `telegram_listener.plugin.DispatchPlugin`, point
`DISPATCH_PLUGIN` at it, and install whatever dependencies it needs as your
own package's dependencies (not this repo's — see `pyproject.toml`'s
`claude-agent-plugin` extra for the pattern: the core listener has no
`claude-agent-sdk` dependency at all, only the example plugin does). Run
`uv run pytest` after wiring a new plugin in — `tests/fakes.py` gives you a
fake HTTP client to write a test against without hitting a live Telegram
API, the same way `tests/test_claude_agent_plugin.py` tests the shipped
example.
