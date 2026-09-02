# telegram-listener

A standalone Telegram long-polling listener with a pluggable per-turn
dispatch interface.

Extracted from [painapple](https://github.com/painapple-org/painapple)/
spoor's own `telegram/listener.py`, which hard-codes dispatch to a spawned
`claude` session. Here that's split along its real seam: this repo's own
`telegram_listener/` package is protocol-level only (long-polling,
per-chat message batching, attachment download, message provenance,
callback-query confirmations) and never imports anything Claude-specific.
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
  `claude-agent-sdk` session per chat, wired up via
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

## Writing your own plugin

Implement `telegram_listener.plugin.DispatchPlugin`:

```python
class DispatchPlugin(Protocol):
    async def handle_turn(self, turn: Turn, api: ListenerAPI) -> None: ...
    async def handle_command(self, chat_id, message_id, command, argument, api: ListenerAPI) -> bool: ...
    def describe_commands(self) -> list[dict]: ...
```

Point `DISPATCH_PLUGIN` at `module.path:factory_function`, where
`factory_function()` returns an instance of your plugin. See
`plugins/claude_agent.py` for a complete example.

## Running

Starting from zero (no bot, no `my.telegram.org` app, no allowlisted user
ID yet)? See [`SETUP.md`](SETUP.md) for how to obtain every value below.

```
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS, etc.
uv sync --extra claude-agent-plugin   # or omit the extra if your own plugin doesn't need it
cd installer && docker compose up -d telegram-bot-api
uv run python -m telegram_listener.listener
```

For a persistent host install (systemd `--user` unit + linger), see
`installer/install.sh`.

For an AI agent installing this package and wiring it into an agent
framework on a human's behalf, see `AGENT_SETUP.md` instead of this
section — it covers the same ground plus credential gathering and plugin
wiring, written for an agent to read and act on rather than for a human to
follow by hand.

## Testing

```
uv run pytest
```
