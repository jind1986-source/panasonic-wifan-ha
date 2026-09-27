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

    ``trigger`` restarts the wait running for ``key``, so a burst of calls
    collapses into a single fire: only the ``action`` passed to the last call
    before the wait elapses ever runs. Once it does, ``after`` seconds later
    every listener registered for ``key`` via ``add_listener`` runs, so an
    entity can read the appliance back once whatever was sent has had a
    chance to take effect.

    ``action`` is expected to handle its own errors: a listener only runs
    after it if it does not raise (see ``fan.PanasonicWiFiFan._send_command``
    for the pattern of logging a failed send and returning normally).

    A same-key ``trigger`` that arrives once the wait is already over no
    longer aborts the send it caught in progress (or already sent): cancelling
    mid-write would leave the device with a half-applied command, and letting
    the old read-back that follows still run would overwrite the newer,
    still-pending change this new ``trigger`` is about to show in Home
    Assistant. Instead the old cycle is left to run to completion, but its
    read-back is skipped; the new cycle gets its own wait, send and read-back,
    same as if nothing had been pending. Only a countdown that has not yet
    reached its send (``is_waiting`` is still true) is cancelled outright,
    same as before ``trigger`` drew this distinction.

    ``cancel`` and ``cancel_all`` are unaffected by any of this: both always
    stop a key completely, in-flight send included. That is the right call
    for ``cancel_all``'s one caller, which closes the aiohttp session right
    after (see ``__init__.async_unload_entry``) — a write left running past
    that would just fail against a closed session instead of completing —
    and it keeps ``cancel`` one thing for every caller rather than a special
    case carved out for ``trigger``.
    """

    def __init__(self, delay: float = DEBOUNCE) -> None:
        self._delay = delay
        self._tasks: dict[Hashable, asyncio.Task] = {}
        self._waiting: set[Hashable] = set()
        self._listeners: dict[Hashable, list[Action]] = {}
        # Bumped on every trigger() for a key. A _run reads its own value at
        # the start and compares it back at each point that matters: if the
        # key has moved on to a later generation by then, a later trigger
        # has superseded it. This is what tells a cycle it was superseded
        # without a shared, key-only flag that a stale, already-cancelled
        # cycle's own cleanup could otherwise clobber out of turn.
        self._generation: dict[Hashable, int] = {}
        # Strong references to sends left running after being superseded by
        # a later trigger. asyncio does not itself keep a pending task alive
        # once nothing else references it, so these are kept until each one
        # finishes (its done callback drops it) or cancel/cancel_all stops it.
        self._background: dict[Hashable, set[asyncio.Task]] = {}

    def add_listener(self, key: Hashable, callback: Action) -> None:
        """Register something to run after the read-back for ``key``."""
        self._listeners.setdefault(key, []).append(callback)

    def trigger(self, key: Hashable, action: Action, *, after: float) -> asyncio.Task:
        """(Re)start the wait for ``key``; ``action`` fires once it is quiet.

        A wait still counting down is dropped and replaced, as always. One
        that already sent, or is sending, is left alone instead — only its
        read-back is skipped — so the device write it is making is never
        aborted part-way through.
        """
        gen = self._generation.get(key, 0) + 1
        self._generation[key] = gen

        if key in self._waiting:
            # Nothing has gone out yet: the whole cycle can simply be
            # dropped and replaced. Only the current cycle — never a send
            # an *earlier* trigger already left running in the background
            # (see the elif below), which this trigger has no part in.
            self._cancel_current(key)
        elif (previous := self._tasks.pop(key, None)) is not None:
            # A previous cycle already started sending (or has finished and
            # is only waiting to read back). Leave it running — its
            # generation no longer matches, so _run will skip its read-back
            # once it gets there — but keep a reference so it is not
            # dropped mid-flight (see _background above).
            self._background.setdefault(key, set()).add(previous)
            previous.add_done_callback(
                lambda t, key=key: self._background.get(key, set()).discard(t)
            )

        self._waiting.add(key)
        task = asyncio.ensure_future(self._run(key, action, after, gen))
        self._tasks[key] = task
        return task

    async def _run(
        self, key: Hashable, action: Action, after: float, gen: int
    ) -> None:
        try:
            await asyncio.sleep(self._delay)
        finally:
            # The wait is over the moment the send begins, whether or not it
            # goes on to succeed. Guarded by generation: a cancelled cycle's
            # cleanup can run late, after a newer one has already restarted
            # the wait for the same key, and must not clear its flag.
            if self._generation.get(key) == gen:
                self._waiting.discard(key)

        await action()

        await asyncio.sleep(after)

        # Checked here, right before the read-back, rather than earlier: a
        # same-key trigger arriving anywhere from the send's start up to this
        # point must still cancel this cycle's read-back. A mismatch means
        # exactly that happened.
        if self._generation.get(key) != gen:
            return

        for listener in list(self._listeners.get(key, [])):
            await listener()

    def is_waiting(self, key: Hashable) -> bool:
        """Whether a change for ``key`` is still waiting to be sent."""
        return key in self._waiting

    def current_task(self, key: Hashable) -> asyncio.Task | None:
        """The task behind the current wait for ``key``, if any. For tests."""
        return self._tasks.get(key)

    def _cancel_current(self, key: Hashable) -> None:
        """Cancel just the task presently recorded for ``key``.

        Leaves any earlier send a previous supersede already left running in
        the background untouched — a same-key ``trigger`` only ever means to
        replace the *current* cycle, never to reach back into one it has
        nothing to do with.
        """
        self._waiting.discard(key)
        if (task := self._tasks.pop(key, None)) is not None:
            task.cancel()

    def cancel(self, key: Hashable) -> None:
        """Cancel ``key`` outright: any pending wait and any send under way.

        Unlike a same-key ``trigger``, this always stops every send for
        ``key`` too, superseded ones left running in the background
        included — see the class docstring for why that is the right call
        for both of its callers, ``cancel_all`` and a direct call.
        """
        self._cancel_current(key)
        for task in self._background.pop(key, set()):
            task.cancel()

    def cancel_all(self) -> None:
        """Cancel every pending wait, e.g. when the config entry unloads."""
        for key in set(self._tasks) | set(self._background):
            self.cancel(key)
