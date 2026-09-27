"""Coalesces rapid changes to one appliance into a single delayed command.

Every setting change on the fan or the light group starts, or restarts, a
short wait for that appliance's command; only the last change before the
wait goes quiet is the one that actually gets sent. Some time after the
send, the appliance is read back so the entities involved can refresh from
what it actually reports.

Kept free of Home Assistant so the whole mechanism can be driven with plain
asyncio in tests: nothing here reaches into ``hass``, and both ``trigger``
and ``add_listener`` take ordinary async callables.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Hashable

_LOGGER = logging.getLogger(__name__)

# How long a burst of changes is given to settle before the merged command
# goes out. Restarted by every change; fixed rather than configurable (see
# docs/releases/debounced-commands.md).
DEBOUNCE = 3  # seconds

Action = Callable[[], Awaitable[None]]


class CommandDebouncer:
    """One restartable wait per key (an appliance plus a command kind).

    ``trigger`` cancels whatever wait is already running for ``key`` and
    starts a new one, so a burst of calls collapses into a single fire: only
    the ``action`` passed to the last call before the wait elapses ever runs.
    Once it does, ``after`` seconds later every listener registered for
    ``key`` via ``add_listener`` runs, so an entity can read the appliance
    back once whatever was sent has had a chance to take effect.

    ``action`` is expected to handle its own errors: a listener only runs
    after it if it does not raise (see ``fan.PanasonicWiFiFan._send_command``
    for the pattern of logging a failed send and returning normally).
    """

    def __init__(self, delay: float = DEBOUNCE) -> None:
        self._delay = delay
        self._tasks: dict[Hashable, asyncio.Task] = {}
        self._waiting: set[Hashable] = set()
        self._listeners: dict[Hashable, list[Action]] = {}

    def add_listener(self, key: Hashable, callback: Action) -> None:
        """Register something to run after the read-back for ``key``."""
        self._listeners.setdefault(key, []).append(callback)

    def trigger(self, key: Hashable, action: Action, *, after: float) -> asyncio.Task:
        """(Re)start the wait for ``key``; ``action`` fires once it is quiet."""
        self.cancel(key)
        self._waiting.add(key)
        task = asyncio.ensure_future(self._run(key, action, after))
        self._tasks[key] = task
        return task

    async def _run(self, key: Hashable, action: Action, after: float) -> None:
        try:
            await asyncio.sleep(self._delay)
        finally:
            # The wait is over the moment the send begins, whether or not it
            # goes on to succeed.
            self._waiting.discard(key)

        await action()

        await asyncio.sleep(after)
        for listener in list(self._listeners.get(key, [])):
            await listener()

    def is_waiting(self, key: Hashable) -> bool:
        """Whether a change for ``key`` is still waiting to be sent."""
        return key in self._waiting

    def current_task(self, key: Hashable) -> asyncio.Task | None:
        """The task behind the current wait for ``key``, if any. For tests."""
        return self._tasks.get(key)

    def cancel(self, key: Hashable) -> None:
        """Cancel a pending wait for ``key``, if there is one."""
        self._waiting.discard(key)
        if (task := self._tasks.pop(key, None)) is not None:
            task.cancel()

    def cancel_all(self) -> None:
        """Cancel every pending wait, e.g. when the config entry unloads."""
        for key in list(self._tasks):
            self.cancel(key)
