# telegram-listener

A standalone Telegram long-polling listener with a pluggable per-turn
dispatch interface.

Extracted from [painapple](https://github.com/painapple-org/painapple)/
spoor's own `telegram/listener.py`, which hard-codes dispatch to a spawned
`claude` session. Here that's split along its real seam: this repo's own
`telegram_listener/` package is protocol-level only (long-polling,
per-chat message batching, attachment download and transcription, full
message-content description, inbound reactions, callback-query
confirmations) and never imports anything Claude-specific.
What happens with a batched turn is entirely up to a `DispatchPlugin`
(`telegram_listener/plugin.py`), loaded at startup from the
`DISPATCH_PLUGIN` environment variable.

## Layout

- `telegram_listener/` - the core listener. `listener.py` is the
  long-polling loop and batching; `plugin.py` defines the `DispatchPlugin`
  interface plugins implement; `common.py` is the minimal shared helper
  code (bot-API base resolution, transcript logging, pending-confirmation
  records for callback queries).
- `plugins/claude_agent.py` - an example plugin: one resumable
  `claude-agent-sdk` session per chat, with streamed replies, an in-place
  activity message while tools run, context compaction, daily session
  rotation and the CLI's own meta-commands passed through. Wired up via
  `DISPATCH_PLUGIN=plugins.claude_agent:build_plugin`. This is the only
  place `claude-agent-sdk` is imported anywhere in the repo - install it
  via the `claude-agent-plugin` extra (`uv sync --extra claude-agent-plugin`),
  not as a core dependency.
- `installer/` - `docker-compose.yml` for the self-hosted local
  `telegram-bot-api` server the listener talks to, and `install.sh` for a
  systemd `--user` unit that runs the listener itself as a real host
  process (see the comments in each for why).
- `tests/` - a fake-HTTP-client based test suite; no live network, no live
  Claude calls.

## Access control

The whole model, and it is worth understanding before pointing a
privileged plugin at it:

- `TELEGRAM_ALLOWED_USER_IDS` (required) — every message and every
  inline-keyboard callback is gated on the numeric sender ID. The listener
  refuses to start on an empty allowlist rather than run allowlisting
  nobody. Drops are logged with their reason.
- `TELEGRAM_ALLOWED_CHAT_IDS` (optional) — additionally gate on *which
  chat* the message came from. Empty means an allowlisted person can drive
  the bot from any chat, including a group a stranger added it to. See
  `SETUP.md` §3b for why that matters and when to set it.
- Nothing from a non-allowlisted sender is written to disk or allocated
  per-chat state; the gate runs before either.
- The bot token is never written to a log file — `requests` puts it in
  every URL it quotes, so error paths report endpoint names only and all
  log output passes through `redact` in `telegram_listener/common.py`.

One thing this cannot gate: text a non-allowlisted person wrote can still
reach a plugin *indirectly*, as quoted or forwarded content inside an
allowlisted person's message. The turn text labels it as such (see
`describe_message_provenance`), but a plugin that acts on instructions
found in a turn body is trusting that labelling — treat forwarded and
quoted content as untrusted input.

## What the listener does with a message before a plugin sees it

All of this is the listener's own work, and a plugin gets the result as
plain text in `Turn.text` rather than having to re-derive it:

- **Batching.** A burst of messages from one chat inside the quiet period
  arrives as a single turn, with a preamble saying so.
- **Content description.** Replies, quotes, forwards (including who they
  really came from), edits, inline-bot compositions, polls, locations,
  venues, contact cards, checklists, dice, stories, games, stickers, album
  membership and chat events (pins, joins, renames) are each described in
  words. A message carrying a Bot API field this listener has no handling
  for says so out loud, rather than quietly arriving as less than it was.
- **Attachments.** Downloaded to a durable local path before the turn is
  dispatched, and handed over as that path.
- **Transcription.** With `TELEGRAM_STT_ENDPOINT` set, a voice message,
  audio file, video or video note arrives as what was actually said in it,
  and the transcript is sent back into the chat as a file.
- **Built-in commands.** `/help`, `/sleep` (stay out of the conversation
  for N seconds, keep reading, answer once when the window ends) and —
  with `TELEGRAM_TTS_ENDPOINT` set — `/voice <text>`, which speaks that
  text back directly with no plugin turn at all.
- **Reactions.** An incoming message is acknowledged the moment it is
  queued, and marked done or failed when its turn ends. Inbound reaction
  updates from other people are recorded into the transcript.
- **Edits.** An edit produces a turn, marked as an edit. An edit is
  usually a correction, and ignoring one means answering text the person
  has already replaced. An edit of a message still waiting out the quiet
  period replaces it in the batch rather than being delivered alongside
  it.
- **Out-of-band sends.** See below.

## Messages your plugin didn't send

Anything else on the host can send into the same chat — a cron alert, a
deploy notification, another session. The person sees one conversation;
without help the plugin sees only its own half of it, and answers "what
did you just say" with the last thing *it* said rather than the message
they are looking at.

So: messages sent into a chat since its last turn that the listener did
not send itself are included in the next turn as their own labelled
block, with whatever each sender called itself. They never trigger a turn
of their own — an alert stays silent until the person next says
something, and is then simply part of that turn's context.

"Not mine" is answered structurally rather than guessed at. The
listener's own send primitives mark their transcript entries
`from_listener`; everything else in the transcript is out-of-band by
construction. A sender that forgets to identify itself is still reported,
just unattributed — the failure direction that matters is a message the
plugin never hears about at all.

To send into a chat from outside the listener and have it show up this
way, record it:

```python
from telegram_listener import common

common.append_transcript("outgoing", chat_id, text=body, sent_by="deploy-notify")
```

`sent_by` is what the plugin will see, so name the thing a person would
recognize. Omitting the call entirely means the message reaches the chat
and the plugin never learns it happened.

To mirror the conversation somewhere else as well — an activity feed, a
dashboard, an audit index — register an observer instead of wrapping the
writers, which would miss everything the listener itself records:

```python
common.set_transcript_observer(lambda direction, entry, byte_offset: ...)
```

It is called after each entry is written, with the byte offset the entry
landed at in `common.TRANSCRIPT_FILE`, so the observer can link straight
at it. An observer that raises is reported and does not take the chat
down with it.

## Writing your own plugin

Implement `telegram_listener.plugin.DispatchPlugin` — read that module for
the full interface, including the optional hooks and what the listener
does when a plugin doesn't define them. The three required members:

```python
class DispatchPlugin(Protocol):
    async def handle_turn(self, turn: Turn, api: ListenerAPI) -> None: ...
    async def handle_command(self, chat_id, message_id, command, argument, api: ListenerAPI) -> bool: ...
    def describe_commands(self) -> list[dict]: ...
```

and the optional ones — `on_start` (background work of the plugin's own,
e.g. a watchdog), `live_context` (fresh host state the listener folds
into the next turn, on its own refresh interval), `build_turn_text` (own
the turn body's layout, e.g. from a prompt template of your own, with the
listener's already-rendered `extras` handed to you so nothing has to be
re-derived), and `interrupt_turn` (abort a turn that has overrun while a
new message waits).

Point `DISPATCH_PLUGIN` at `module.path:factory_function`, where
`factory_function()` returns an instance of your plugin. See
`plugins/claude_agent.py` for a complete example.

Every `ListenerAPI` send primitive is awaitable — they wrap blocking HTTP
calls, and running one inline would stall the shared poll loop and every
other chat for the length of the request. `log` and `drop_queued` (throw
away this chat's batched-but-undelivered messages — what a plugin's own
cancel command needs, since those messages are part of what is being
cancelled) are the two synchronous members. The listener guarantees one `handle_turn` at a time per chat, so a
plugin's own per-chat state needs no locking of its own, and it holds that
turn open until `handle_turn` returns — so a plugin that dispatches work
asynchronously must await it rather than returning early.

## Running

Starting from zero (no bot, no `my.telegram.org` app, no allowlisted user
ID yet)? See [`SETUP.md`](SETUP.md) for how to obtain every value below.

```
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS, etc.
uv sync --extra claude-agent-plugin   # or omit the extra if your own plugin doesn't need it
docker compose -f installer/docker-compose.yml --env-file .env up -d telegram-bot-api
uv run python -m telegram_listener.listener
```

`--env-file .env` is not optional: the compose file lives in `installer/`
but `.env` is at the repo root, so without it compose looks for an
`installer/.env` that isn't there and the container starts with no
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH`.

For a persistent host install (systemd `--user` unit + linger), see
`installer/install.sh`.

For an AI agent installing this package and wiring it into an agent
framework on a human's behalf, see `AGENT_SETUP.md` instead of this
section — it covers the same ground plus credential gathering and plugin
wiring, written for an agent to read and act on rather than for a human to
follow by hand.

## Testing

```
uv sync
uv run pytest
```

The core listener's tests run on core dependencies alone. The example
plugin's own tests need the optional extra (`uv sync --extra
claude-agent-plugin`) and skip without it.
