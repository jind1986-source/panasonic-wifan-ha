"""Tests for the shared command-debounce helper.

Free of Home Assistant, like the module it tests, so it runs in the fast
lane alongside the other protocol-module tests.
"""

import asyncio

import pytest

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


def test_cancel_all_also_stops_a_send_already_under_way():
    """Unlike a same-key trigger, cancel_all (unload) aborts an in-flight
    write too — the aiohttp session closes right behind it either way, so
    letting the write run on into a closing session serves nothing."""

    async def scenario():
        sent = []
        started = asyncio.Event()
        debouncer = CommandDebouncer(delay=0.01)

        async def action():
            started.set()
            await asyncio.sleep(10)  # would hang the test if not cancelled
            sent.append("sent")  # pragma: no cover - never reached

        task = debouncer.trigger(KEY, action, after=0.01)
        await started.wait()  # the send is genuinely under way

        debouncer.cancel_all()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert sent == []

    asyncio.run(scenario())


def test_cancel_all_also_stops_a_send_superseded_earlier():
    """A send left running in the background after being superseded is
    still one cancel_all should reach, same reasoning as above."""

    async def scenario():
        sent = []
        started = asyncio.Event()
        debouncer = CommandDebouncer(delay=0.01)

        async def first_send():
            started.set()
            await asyncio.sleep(10)  # would hang the test if not cancelled
            sent.append("first")  # pragma: no cover - never reached

        async def second_send():
            sent.append("second")

        first_task = debouncer.trigger(KEY, first_send, after=0.01)
        await started.wait()

        debouncer.trigger(KEY, second_send, after=0.01)  # supersedes first_send
        debouncer.cancel_all()

        with pytest.raises(asyncio.CancelledError):
            await first_task

        await asyncio.sleep(0.03)
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


def test_a_trigger_while_the_send_is_in_flight_lets_it_finish_but_skips_its_read_back():
    """decision 19: a same-key trigger must not abort a write under way.

    Cancelling during the countdown still drops the action outright (see
    test_cancel_drops_a_pending_wait_without_running_the_action); once the
    action has started, a same-key trigger instead lets it run to
    completion and only cancels its read-back, because the read-back would
    otherwise overwrite the newer, still-pending change with a stale answer.
    """

    async def scenario():
        sent = []
        read_backs = []
        started = asyncio.Event()
        release = asyncio.Event()
        debouncer = CommandDebouncer(delay=0.01)

        async def first_send():
            started.set()
            await release.wait()  # genuinely in flight, not just about to start
            sent.append("first")

        async def second_send():
            sent.append("second")

        async def listener():
            read_backs.append("read-back")

        debouncer.add_listener(KEY, listener)

        first_task = debouncer.trigger(KEY, first_send, after=0.001)
        await started.wait()

        # A same-key change arrives while the first send is still awaiting.
        second_task = debouncer.trigger(KEY, second_send, after=0.001)

        release.set()  # let the in-flight send finish
        await first_task
        await second_task

        assert sent == ["first", "second"]  # the in-flight write ran to completion
        assert read_backs == ["read-back"]  # only the second cycle's read-back ran

    asyncio.run(scenario())


def test_cancelling_a_later_countdown_leaves_an_earlier_superseded_send_alone():
    """A trigger cancelling a still-counting-down cycle must reach only that
    cycle — never a send an *earlier* supersede already left running in the
    background, which it has nothing to do with."""

    async def scenario():
        sent = []
        started = asyncio.Event()
        release = asyncio.Event()
        debouncer = CommandDebouncer(delay=0.02)

        async def first_send():
            started.set()
            await release.wait()  # genuinely in flight
            sent.append("first")

        async def second_send():
            sent.append("second")  # pragma: no cover - cancelled before it runs

        async def third_send():
            sent.append("third")

        first_task = debouncer.trigger(KEY, first_send, after=0.001)
        await started.wait()

        # Supersedes first_send, which keeps running in the background.
        debouncer.trigger(KEY, second_send, after=0.001)
        # Still inside second_send's own countdown: cancelled outright,
        # exactly as an ordinary burst would be.
        task = debouncer.trigger(KEY, third_send, after=0.001)

        release.set()  # let the superseded first send run to completion
        await first_task
        await task

        assert set(sent) == {"first", "third"}  # second_send never ran

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
