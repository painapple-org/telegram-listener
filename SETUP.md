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
this allowlist and drops anyone not on it, logging the drop. This is
required — the listener refuses to start on an empty allowlist rather than
run allowlisting nobody.

To find your own numeric user ID:

1. Open a chat with [@userinfobot](https://t.me/userinfobot) (or any similar
   "get my Telegram ID" bot) and send it any message.
2. It replies with your numeric `Id` — that's the value to use.

`TELEGRAM_ALLOWED_USER_IDS` takes a comma-separated list, so multiple people
can be allowlisted: `TELEGRAM_ALLOWED_USER_IDS=111111111,222222222`.

To allow a bot to respond inside a group chat: add your bot to the group as
a member, then message the group — the same per-sender check applies there,
so anyone in the group whose user ID is on the list can trigger it.

## 3b. `TELEGRAM_ALLOWED_CHAT_IDS` — optional, but read this before skipping it

The sender allowlist alone does not restrict *where* an allowlisted person
may drive the bot from. Anyone can add your bot to any group; if one
allowlisted person is also in that group, their messages there reach the
plugin like any other. Whatever your plugin does — for an agent plugin,
that is real tool access — is then being driven from a chat you never chose,
in front of people you did not pick, and non-allowlisted members' text can
still reach the plugin indirectly as quoted or forwarded content inside an
allowlisted person's own message.

Setting `TELEGRAM_ALLOWED_CHAT_IDS` closes that: a message is only
dispatched if the sender **and** the chat are both on their allowlist.
Leaving it empty keeps the sender-only behaviour. For a deployment whose
plugin has real privileges, set it.

To find a chat's numeric ID (a group's is negative, a DM's equals your own
user ID), forward any message from that chat to
[@userinfobot](https://t.me/userinfobot) — it reports the originating
chat's ID alongside the sender's. Comma-separate multiple:
`TELEGRAM_ALLOWED_CHAT_IDS=-1001234567890,111111111`.

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
  offset/transcripts/sessions/downloaded files. When unset, `/data` if it
  exists, else `./data` (see `telegram_listener/common.py`).
* `TELEGRAM_BOT_API_HOST` / `TELEGRAM_BOT_API_PORT`, `RAW_UPDATES_MAX_BYTES`,
  and the `MESSAGE_BATCH_*` tunables — all optional, all documented in
  `.env.example` with their defaults owned by
  `telegram_listener/listener.py` and `telegram_listener/common.py`. Don't
  set any of them to get a first deployment running.

## 5. Put it together

```
cp .env.example .env
```

Fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_API_ID`,
and `TELEGRAM_API_HASH` from steps 1–3 above. Then follow the README's
"Running" section to bring up `telegram-bot-api` and start the listener (or
`installer/install.sh` for a persistent host install, which creates and
locks down `.env` for you).

`.env` holds two passwords (the bot token and the API hash). Keep it mode
`600`; `install.sh` sets that, and `.gitignore` already excludes it. The
listener never writes the token to its own log — see `redact` in
`telegram_listener/common.py`.
