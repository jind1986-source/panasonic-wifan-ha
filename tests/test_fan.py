"""Tests for the fan entity's debounced commands and read-back.

Skipped unless Home Assistant is installed, since the entity subclasses it.
"""

import asyncio
import logging

import pytest

from _component import load

pytest.importorskip("homeassistant", reason="Home Assistant is not installed")

fan, types_, debounce_module = load("fan", "types", "debounce")
Fan = types_.Fan
FanState = types_.FanState
DeviceState = types_.DeviceState

FAN = Fan(
    appliance_id="abc123",
    com_id="FM12EC",
    hashed_guid="g",
    name="Study",
    product_code="F-M12EC",
    serial_number="SN1",
)


class Recorder:
    """A fake ApiClient recording sends and answering get_state_for_fan."""

    def __init__(self, *, reported=None, fail_send=False):
        self.sent = []
        self.get_state_calls = []
        self.fail_send = fail_send
        self.reported = reported or DeviceState(
            fan=FanState(is_on=True, speed=5, reverse=False, yuragi=False)
        )

    async def set_state(self, fan, state):
        if self.fail_send:
            raise RuntimeError("boom")
        self.sent.append(state)

    async def get_state_for_fan(self, fan, *, max_age=None):
        self.get_state_calls.append(max_age)
        return self.reported


def make_fan(monkeypatch, *, delay=0.01, after=0.01, api=None):
    """A fan entity wired to a fast debouncer, for tests that don't wait 3/2 s."""
    monkeypatch.setattr(fan, "REFRESH_AFTER_COMMAND", after)
    api = api or Recorder()
    debounce = debounce_module.CommandDebouncer(delay=delay)
    entity = fan.PanasonicWiFiFan(api, FAN, debounce)
    entity.async_write_ha_state = lambda: None
    return entity, api, debounce


def test_a_change_shows_in_home_assistant_before_it_is_sent(monkeypatch):
    async def scenario():
        entity, api, _ = make_fan(monkeypatch, delay=1, after=1)
        await entity.async_set_percentage(60)

        assert entity.percentage == 60
        assert entity.is_on is True
        assert api.sent == []

    asyncio.run(scenario())


def test_a_burst_of_changes_produces_one_merged_fan_command(monkeypatch):
    async def scenario():
        entity, api, debounce = make_fan(monkeypatch, delay=0.02, after=0.01)

        await entity.async_set_percentage(40)
        await asyncio.sleep(0.005)
        await entity.async_set_percentage(70)
        await asyncio.sleep(0.005)
        await entity.async_set_direction("reverse")

        await debounce.current_task(entity._key)

        assert len(api.sent) == 1
        sent = api.sent[0]
        assert sent.speed == entity._percentage_to_speed(70)
        assert sent.reverse is True
        assert sent.is_on is True

    asyncio.run(scenario())


def test_a_change_after_the_wait_settles_gets_its_own_command(monkeypatch):
    async def scenario():
        entity, api, debounce = make_fan(monkeypatch, delay=0.01, after=0.001)

        await entity.async_set_percentage(40)
        await debounce.current_task(entity._key)

        await entity.async_set_percentage(80)
        await debounce.current_task(entity._key)

        assert len(api.sent) == 2
        assert api.sent[0].speed == entity._percentage_to_speed(40)
        assert api.sent[1].speed == entity._percentage_to_speed(80)

    asyncio.run(scenario())


def test_a_poll_during_the_wait_does_not_revert_the_state(monkeypatch):
    async def scenario():
        stale = DeviceState(
            fan=FanState(is_on=False, speed=1, reverse=False, yuragi=False)
        )
        entity, api, debounce = make_fan(
            monkeypatch, delay=0.05, after=0.01, api=Recorder(reported=stale)
        )

        await entity.async_set_percentage(90)
        # Still inside the 3 s (here 0.05 s) wait.
        await entity.async_update()

        assert entity.percentage == 90
        assert entity.is_on is True
        assert api.get_state_calls == []

        await debounce.current_task(entity._key)

    asyncio.run(scenario())


def test_a_poll_within_settle_of_the_send_is_ignored(monkeypatch):
    async def scenario():
        stale = DeviceState(
            fan=FanState(is_on=False, speed=1, reverse=False, yuragi=False)
        )
        entity, api, debounce = make_fan(
            monkeypatch, delay=0.01, after=0.001, api=Recorder(reported=stale)
        )

        await entity.async_set_percentage(90)
        await debounce.current_task(entity._key)

        # The send has just happened; real time elapsed is far under SETTLE
        # (5 s), and the entity should still be showing what it sent, not
        # the read-back's own value (which _read_back applies deliberately)
        # nor whatever a fresh incidental poll would fetch.
        before_poll = entity.percentage
        calls_before = len(api.get_state_calls)

        await entity.async_update()

        assert len(api.get_state_calls) == calls_before
        assert entity.percentage == before_poll

    asyncio.run(scenario())


def test_a_poll_is_accepted_once_settle_has_passed(monkeypatch):
    async def scenario():
        fresh = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=True, yuragi=True)
        )
        entity, api, debounce = make_fan(
            monkeypatch, delay=0.01, after=0.001, api=Recorder(reported=fresh)
        )

        await entity.async_set_percentage(90)
        await debounce.current_task(entity._key)

        # Move the clock forward past SETTLE without a real 5 s sleep.
        later = fan.monotonic() + fan.SETTLE + 1
        monkeypatch.setattr(fan, "monotonic", lambda: later)

        await entity.async_update()

        assert entity.current_direction == "reverse"
        assert entity.oscillating is True

    asyncio.run(scenario())


def test_the_read_back_forces_a_fresh_read_and_updates_the_fan(monkeypatch):
    async def scenario():
        reported = DeviceState(
            fan=FanState(is_on=True, speed=7, reverse=True, yuragi=True)
        )
        entity, api, debounce = make_fan(
            monkeypatch, delay=0.01, after=0.01, api=Recorder(reported=reported)
        )

        await entity.async_set_percentage(20)
        await debounce.current_task(entity._key)

        assert api.get_state_calls == [0]
        assert entity.current_direction == "reverse"
        assert entity.oscillating is True

    asyncio.run(scenario())


def test_a_failed_send_is_logged_and_still_followed_by_a_read_back(
    monkeypatch, caplog
):
    async def scenario():
        api = Recorder(fail_send=True)
        entity, api, debounce = make_fan(monkeypatch, delay=0.01, after=0.01, api=api)

        with caplog.at_level(logging.ERROR):
            await entity.async_set_percentage(55)
            await debounce.current_task(entity._key)

        assert api.sent == []
        assert api.get_state_calls == [0]
        assert "Error sending state" in caplog.text
        # A failed send never sets _commanded_at, so a poll right after is
        # not held back by the settle guard.
        assert entity._commanded_at is None

    asyncio.run(scenario())


def test_unloading_cancels_a_pending_send(monkeypatch):
    async def scenario():
        entity, api, debounce = make_fan(monkeypatch, delay=0.02, after=0.01)

        await entity.async_set_percentage(33)
        # What the config entry's unload does.
        debounce.cancel_all()

        await asyncio.sleep(0.06)
        assert api.sent == []
        assert api.get_state_calls == []

    asyncio.run(scenario())
