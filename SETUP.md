# Setup: obtaining every credential this listener needs

This walks through getting every value `.env.example` asks for, starting from
nothing — no bot, no `my.telegram.org` account, no chat set up yet. By the
end you'll have a filled-in `.env` and can follow the README's "Running"
section to actually start the listener.

## 1. `TELEGRAM_BOT_TOKEN` — create a bot via BotFather

1. In Telegram, open a chat with [@BotFather](https://t.me/BotFather) (the
   official bot-management bot — verified account, blue checkmark).
2. Send `/newbot`.
3. Follow the prompts: a display name (shown to users, can be anything), then
   a username (must be unique and end in `bot`, e.g. `my_listener_bot`).
4. BotFather replies with a token that looks like
   `123456789:AAExampleTokenNotReal-abcdefghijk`. That whole string is
   `TELEGRAM_BOT_TOKEN`.
5. Treat this token like a password — anyone who has it can send/receive
   messages as your bot. Don't commit it; `.env` is gitignored for this
   reason.

Optional but useful while you're in BotFather: `/setcommands` lets you
register a command menu (this repo already registers a `/help` command at
runtime; BotFather's `/setcommands` is only about the client-side "/" menu
UI, not required for the listener to work).

## 2. `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — register an application at my.telegram.org

These belong to the self-hosted `telegram-bot-api` server this listener
talks to (see `installer/docker-compose.yml`), not to your bot itself —
`telegram-bot-api` needs its own Telegram "application" credentials to run
as a local Bot API server instead of relying on the public
`api.telegram.org`.

1. Go to <https://my.telegram.org> and log in with a real Telegram *account*
   phone number (not the bot — your own personal account). You'll receive a
   login code in Telegram to confirm.
2. Click "API development tools".
3. Fill in the "Create new application" form. Only "App title" and "Short
   name" are required; the rest (URL, platform, description) can be left
   blank or filled in loosely — none of it is validated against how you
   actually use the credentials.
4. Submit. The resulting page shows `App api_id` and `App api_hash` — these
   are `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` respectively.
5. These are also sensitive (they authenticate as your personal Telegram
   account's application registration) — keep them out of git the same way
   as the bot token.

## 3. `TELEGRAM_ALLOWED_USER_IDS` — find your numeric Telegram user ID

The listener checks the numeric sender ID of every incoming message against
this allowlist and silently drops anyone not on it — including in a group
chat, since the check is per-sender, not per-chat (there's no separate
group/chat-ID allowlist to configure).

To find your own numeric user ID:

1. Open a chat with [@userinfobot](https://t.me/userinfobot) (or any similar
   "get my Telegram ID" bot) and send it any message.
2. It replies with your numeric `Id` — that's the value to use.

`TELEGRAM_ALLOWED_USER_IDS` takes a comma-separated list, so multiple people
can be allowlisted: `TELEGRAM_ALLOWED_USER_IDS=111111111,222222222`.

To allow a bot to respond inside a group chat: add your bot to the group as
a member, then message the group — the same per-sender allowlist check
applies there too, so anyone in the group whose user ID is on the list can
trigger it, but no group/chat ID needs to be configured. If you do want to
know a specific chat's numeric ID (e.g. for your own tooling or a custom
plugin that needs to proactively message a chat rather than only reply to
one), forward any message from that chat to [@userinfobot](https://t.me/userinfobot)
and it will report the originating chat's ID alongside the sender's.

## 4. Everything else in `.env.example`

* `DISPATCH_PLUGIN` — which plugin handles a batched turn. Defaults to the
  example shipped in this repo (`plugins.claude_agent:build_plugin`); leave
  it as-is unless you've written your own (`telegram_listener/plugin.py`
  documents the `DispatchPlugin` interface, and the README's "Writing your
  own plugin" section walks through it).
* `CLAUDE_AGENT_MODEL` — optional, only read by the shipped
  `claude_agent` example plugin. Leave unset to use that plugin's own
  default.
* `TELEGRAM_LISTENER_DATA_DIR` — optional, where the listener stores its
  offset/transcripts/sessions/downloaded files. Leave unset to use `/data`
  if present, else `./data` (see `telegram_listener/common.py`).

## 5. Put it together

```
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_API_ID`,
and `TELEGRAM_API_HASH` from steps 1–3 above. Then follow the README's
"Running" section to bring up `telegram-bot-api` and start the listener (or
`installer/install.sh` for a persistent host install).
