"""The pluggable dispatch interface.

The listener's own job stops at: poll Telegram, filter to allowed users,
batch a burst of messages from the same chat into one `Turn`, and hand that
turn to whatever `DispatchPlugin` is configured. What happens with a turn -
spawn a `claude` session, call some other API, echo it back - is entirely
the plugin's decision; the listener never imports or special-cases any
particular implementation of it.

A plugin is loaded at startup from the `DISPATCH_PLUGIN` environment
variable, formatted as `module.path:factory`, e.g.
`plugins.claude_agent:build_plugin`. `factory` is called with no arguments
and must return a `DispatchPlugin`.
"""

import importlib
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol


@dataclass
class Turn:
    """One batched unit of conversation handed to a plugin.

    `message_ids` holds every Telegram message_id folded into this turn (a
    burst of messages sent within the same quiet-period window collapses
    into a single turn - see `listener.py`'s batching). `text` is the
    already-combined, already-annotated turn body (sender, provenance,
    attachments, transcripts, etc. folded in as plain text) - a plugin does
    not need to re-derive any of that from raw Telegram fields.
    """

    chat_id: int
    message_ids: list[int]
    text: str
    sender_name: str
    sender_id: Optional[int]


@dataclass
class TurnTextParts:
    """Everything the listener worked out about one message, handed to a
    plugin's optional `build_turn_text` hook so it can assemble the turn
    body itself (from a prompt template of its own, say) instead of taking
    the listener's default layout.

    `message` is the raw Telegram message dict, for anything the listener
    did not think to pull out. `extras` is the already-rendered block of
    addressing signals, attachment paths, provenance, live state and
    out-of-band sends - a plugin that only wants a different wrapper around
    the same information drops this in rather than re-deriving any of it.
    """

    chat_id: int
    message_id: int
    sender_name: str
    sender_id: Optional[int]
    message: dict
    text: str
    extras: str


@dataclass
class ListenerAPI:
    """What a plugin is given to act with - the listener's own send/react/log
    primitives, so a plugin never talks to the Telegram Bot API directly.

    Every send/react primitive is awaitable and must be awaited. They wrap
    blocking HTTP calls, so calling one without awaiting it both loses the
    call and, if it were run inline instead, would stall the shared poll
    loop and every other chat for the duration of the request. `log` is the
    exception: a plain synchronous local file append.
    """

    # Returns the message_id of the last message actually sent, or None if
    # the send failed - a plugin that wants to edit what it just sent (a
    # progress/activity message, say) needs that id back.
    send_message: Callable[..., Awaitable[Optional[int]]]
    send_typing: Callable[[int], Awaitable[None]]
    set_reaction: Callable[[int, int, str], Awaitable[None]]
    # Send a local file to a chat. The listener talks to a self-hosted Bot
    # API server precisely so this isn't capped at the cloud API's 20MB.
    send_document: Callable[..., Awaitable[Any]]
    edit_message: Callable[[int, int, str], Awaitable[bool]]
    delete_message: Callable[[int, int], Awaitable[None]]
    # Synthesize `text` and send it as a Telegram voice message. Returns
    # False when no TTS endpoint is configured or synthesis failed, so the
    # caller can fall back to text rather than go quiet.
    send_voice: Callable[..., Awaitable[bool]]
    # Tell the humans in the chat that something broke, as well as the log.
    # A log file alone is silence.
    report_failure: Callable[..., Awaitable[None]]
    log: Callable[[str], None]
    # Optional event-notification hook (e.g. for a dashboard/activity feed
    # downstream). None unless the process wiring it up actually wants one -
    # the listener core never assumes one exists, and a plugin must treat a
    # missing hook the same as a no-op one.
    on_event: Optional[Callable[[str, dict], None]] = None


class DispatchPlugin(Protocol):
    """The pluggable per-turn dispatch interface.

    `handle_turn` is called once per batched turn. `handle_command` is
    called for any `/command` the listener's own built-in set doesn't
    recognize - a plugin can use this to add its own commands (e.g. a
    Claude-backed plugin's `/compact`, `/clear`, `/model`) without the
    listener needing to know about them. Returning False from
    `handle_command` tells the listener the command was not recognized by
    anyone, and it is handed to `handle_turn` as ordinary text instead.

    Everything below `describe_commands` is optional: the listener probes
    for each with `getattr` and behaves sensibly when a plugin doesn't
    define it. They exist so a plugin can carry behaviour the listener
    cannot have an opinion about, without the listener growing a hard
    dependency on any of it.
    """

    async def handle_turn(self, turn: Turn, api: ListenerAPI) -> None: ...

    async def handle_command(
        self, chat_id: int, message_id: int, command: str, argument: str, api: ListenerAPI
    ) -> bool: ...

    def describe_commands(self) -> list[dict]:
        """Bot commands (Telegram `setMyCommands` shape: {"command", "description"})
        this plugin wants registered/listed, on top of the listener's own built-ins."""
        ...

    # --- optional hooks -------------------------------------------------

    async def on_start(self, api: ListenerAPI) -> None:
        """Called once, after the bot identity is resolved and before the
        poll loop starts. A plugin that needs background work of its own -
        an idle watchdog, a periodic flush - starts it here rather than
        asking the listener to own a loop on its behalf."""
        ...

    async def live_context(self, chat_id: int) -> str:
        """Fresh out-of-band state to fold into the next turn's text for
        this chat (host state, a build log, whatever the deployment cares
        about). The listener decides *when* to ask - at most once per
        LIVE_CONTEXT_REFRESH_INTERVAL_SECONDS per chat - so a plugin never
        has to own that cadence, and the data source stays entirely the
        plugin's business."""
        ...

    async def build_turn_text(self, parts: TurnTextParts) -> str:
        """Assemble the turn body for one message. Without this hook the
        listener uses its own layout (`listener.default_turn_text`), which
        is also the right thing to fall back to inside an implementation
        that only wants to wrap it."""
        ...

    async def interrupt_turn(self, chat_id: int, api: ListenerAPI) -> str:
        """A new batch is waiting on a turn that has already run longer than
        the listener's yield grace period. Abort the in-flight turn if the
        plugin can, and return a note to prepend to the waiting turn's text
        explaining what was interrupted (empty string for no note).

        Without this hook the listener simply waits the running turn out,
        which is correct but can leave a person waiting a long time behind
        a turn their new message may well have made irrelevant.
        """
        ...


def load_plugin() -> DispatchPlugin:
    spec = os.environ.get("DISPATCH_PLUGIN")
    if not spec:
        raise RuntimeError(
            "DISPATCH_PLUGIN is not set - point it at a plugin factory, e.g. "
            "'plugins.claude_agent:build_plugin'"
        )
    module_name, sep, factory_name = spec.partition(":")
    if not sep:
        raise RuntimeError(
            f"DISPATCH_PLUGIN={spec!r} is not in 'module.path:factory' form"
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name)
    return factory()
