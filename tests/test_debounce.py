"""Tests for the shared command-debounce helper.

Free of Home Assistant, like the module it tests, so it runs in the fast
lane alongside the other protocol-module tests.
"""

import asyncio

from _component import load

debounce_module = load("debounce")
CommandDebouncer = debounce_module.CommandDebouncer

KEY = ("abc123", "fan")


def test_the_action_runs_once_the_wait_elapses():
    async def scenario():
        sent = []
        debouncer = CommandDebouncer(delay=0.01)

        async def action():
            sent.append("sent")

        task = debouncer.trigger(KEY, action, after=0.01)
        await task
        assert sent == ["sent"]

    asyncio.run(scenario())


def test_a_burst_of_triggers_collapses_into_one_run_of_the_last_action():
    async def scenario():
        sent = []
        debouncer = CommandDebouncer(delay=0.02)

        async def first():
            sent.append("first")

        async def second():
            sent.append("second")

        debouncer.trigger(KEY, first, after=0.01)
        await asyncio.sleep(0.005)
        task = debouncer.trigger(KEY, second, after=0.01)
        await task

        assert sent == ["second"]

    asyncio.run(scenario())


def test_a_trigger_after_the_wait_elapses_gets_its_own_run():
    async def scenario():
        sent = []
        debouncer = CommandDebouncer(delay=0.01)

        async def action():
            sent.append("sent")

        await debouncer.trigger(KEY, action, after=0.001)
        await debouncer.trigger(KEY, action, after=0.001)

        assert sent == ["sent", "sent"]

    asyncio.run(scenario())


def test_every_listener_runs_after_the_action_settles():
    async def scenario():
        order = []
        debouncer = CommandDebouncer(delay=0.01)

        async def action():
            order.append("action")

        async def listener_one():
            order.append("one")

        async def listener_two():
            order.append("two")

        debouncer.add_listener(KEY, listener_one)
        debouncer.add_listener(KEY, listener_two)

        task = debouncer.trigger(KEY, action, after=0.01)
        await task

        assert order == ["action", "one", "two"]

    asyncio.run(scenario())


def test_cancel_drops_a_pending_wait_without_running_the_action():
    async def scenario():
        sent = []
        debouncer = CommandDebouncer(delay=0.02)

        async def action():
            sent.append("sent")

        debouncer.trigger(KEY, action, after=0.01)
        debouncer.cancel(KEY)

        await asyncio.sleep(0.04)
        assert sent == []

    asyncio.run(scenario())


def test_cancel_all_drops_every_pending_wait():
    async def scenario():
        sent = []
        debouncer = CommandDebouncer(delay=0.02)

        async def action():
            sent.append("sent")

        debouncer.trigger(("a", "fan"), action, after=0.01)
        debouncer.trigger(("b", "light"), action, after=0.01)
        debouncer.cancel_all()

        await asyncio.sleep(0.04)
        assert sent == []

    asyncio.run(scenario())


def test_is_waiting_is_true_until_the_action_starts():
    async def scenario():
        debouncer = CommandDebouncer(delay=0.02)

        async def action():
            pass

        task = debouncer.trigger(KEY, action, after=0.001)
        assert debouncer.is_waiting(KEY) is True

        await task
        assert debouncer.is_waiting(KEY) is False

    asyncio.run(scenario())


def test_is_waiting_is_false_once_cancelled():
    async def scenario():
        debouncer = CommandDebouncer(delay=0.02)

        async def action():
            pass

        debouncer.trigger(KEY, action, after=0.01)
        debouncer.cancel(KEY)
        assert debouncer.is_waiting(KEY) is False

        # Let the loop deliver the cancellation before it closes.
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_a_raising_action_stops_the_listeners_from_running():
    """The caller is expected to catch its own errors; see fan.py's pattern."""

    async def scenario():
        ran = []
        debouncer = CommandDebouncer(delay=0.01)

        async def action():
            raise RuntimeError("boom")

        async def listener():
            ran.append("listener")

        debouncer.add_listener(KEY, listener)
        task = debouncer.trigger(KEY, action, after=0.01)

        try:
            await task
        except RuntimeError:
            pass

        assert ran == []

    asyncio.run(scenario())
