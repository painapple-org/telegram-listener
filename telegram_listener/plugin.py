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
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol


@dataclass
class Turn:
    """One batched unit of conversation handed to a plugin.

    `message_ids` holds every Telegram message_id folded into this turn (a
    burst of messages sent within the same quiet-period window collapses
    into a single turn - see `listener.py`'s batching). `text` is the
    already-combined, already-annotated turn body (sender, provenance,
    attachments, etc. folded in as plain text) - a plugin does not need to
    re-derive any of that from raw Telegram fields.
    """

    chat_id: int
    message_ids: list[int]
    text: str
    sender_name: str
    sender_id: Optional[int]


@dataclass
class ListenerAPI:
    """What a plugin is given to act with - the listener's own send/react/log
    primitives, so a plugin never talks to the Telegram Bot API directly."""

    send_message: Callable[[int, str], None]
    send_typing: Callable[[int], None]
    set_reaction: Callable[[int, int, str], None]
    log: Callable[[str], None]
    # Optional event-notification hook (e.g. for a dashboard/activity feed
    # downstream). None unless the process wiring it up actually wants one -
    # the listener core never assumes one exists, and a plugin must treat a
    # missing hook the same as a no-op one.
    on_event: Optional[Callable[[str, dict], None]] = None


class DispatchPlugin(Protocol):
    """The pluggable per-turn dispatch interface.

    `handle_turn` is called once per batched turn. `handle_command` is
    called for any `/command` the listener's own small built-in set
    (currently just `/help`) doesn't recognize - a plugin can use this to
    add its own commands (e.g. a Claude-backed plugin's `/compact`,
    `/clear`, `/model`) without the listener needing to know about them.
    Returning False from `handle_command` tells the listener the command
    was not recognized by anyone.
    """

    async def handle_turn(self, turn: Turn, api: ListenerAPI) -> None: ...

    async def handle_command(
        self, chat_id: int, message_id: int, command: str, argument: str, api: ListenerAPI
    ) -> bool: ...

    def describe_commands(self) -> list[dict]:
        """Bot commands (Telegram `setMyCommands` shape: {"command", "description"})
        this plugin wants registered/listed, on top of the listener's own built-ins."""
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
