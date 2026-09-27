"""Tests for the sleep mode switch.

It exists because HomeKit's lightbulb service has no light effects, so sleep
mode cannot reach Apple Home through the light entity.
"""

import asyncio
import logging

import pytest

from _component import load

pytest.importorskip("homeassistant", reason="Home Assistant is not installed")

switch, types_, store_module, debounce_module = load(
    "switch", "types", "store", "debounce"
)
LightState = types_.LightState

FAN = types_.Fan(
    appliance_id="abc123",
    com_id="FM12GC",
    hashed_guid="g",
    name="Master",
    product_code="F-M12GC",
    serial_number="SN1",
)


class Recorder:
    """Stands in for the API, keeping what the entity sent and read back.

    ``reported`` left as ``None`` (the default) means the read-back has
    nothing configured to answer with, so its fetch fails and is caught and
    logged like a real cloud hiccup — harmless for tests that only care
    about what was sent.
    """

    def __init__(self, *, reported=None, fail_send=False):
        self.sent = []
        self.get_state_calls = []
        self.fail_send = fail_send
        self.reported = reported

    async def set_light_state(self, fan, state):
        if self.fail_send:
            raise RuntimeError("boom")
        self.sent.append(state)

    async def get_state_for_fan(self, fan, *, max_age=None):
        self.get_state_calls.append(max_age)
        if self.reported is None:
            raise RuntimeError("no reported state configured")
        return self.reported


def make_store(light_state):
    return store_module.StateStore(
        {
            FAN.unique_id: types_.DeviceState(
                fan=types_.FanState(is_on=True, speed=6, reverse=False, yuragi=True),
                light=light_state,
            )
        }
    )


def make_switch(state, store=None, *, monkeypatch=None, delay=0.01, after=0.01, api=None):
    """A switch wired to a fast debouncer, for tests that don't wait 3/2 s."""
    if monkeypatch is not None:
        monkeypatch.setattr(switch, "REFRESH_AFTER_COMMAND", after)
    api = api or Recorder()
    store = store or make_store(state)
    debounce = debounce_module.CommandDebouncer(delay=delay)
    entity = switch.PanasonicWiFiSleepSwitch(api, FAN, store, debounce)
    entity.async_write_ha_state = lambda: None
    return entity, api, debounce


def test_the_switch_reflects_the_light_mode():
    entity, _, _ = make_switch(LightState(is_on=True, brightness=50, sleep=True))
    assert entity.is_on is True
    assert entity.unique_id == "abc123_light_sleep"


def test_a_change_shows_in_home_assistant_before_it_is_sent(monkeypatch):
    async def scenario():
        entity, api, _ = make_switch(
            LightState(is_on=True, brightness=50, sleep=False),
            monkeypatch=monkeypatch,
            delay=1,
            after=1,
        )
        await entity.async_turn_on()

        assert entity.is_on is True
        assert api.sent == []

    asyncio.run(scenario())


def test_turning_the_switch_on_selects_sleep_mode(monkeypatch):
    async def scenario():
        entity, api, debounce = make_switch(
            LightState(is_on=True, brightness=50, sleep=False),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_on()
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep is True

    asyncio.run(scenario())


def test_turning_the_switch_off_returns_to_normal(monkeypatch):
    async def scenario():
        entity, api, debounce = make_switch(
            LightState(is_on=True, brightness=50, sleep=True, sleep_brightness=50),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_off()
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep is False

    asyncio.run(scenario())


def test_the_switch_leaves_every_other_setting_alone(monkeypatch):
    async def scenario():
        entity, api, debounce = make_switch(
            LightState(
                is_on=True, brightness=80, color_temp=32, sleep=False,
                sleep_brightness=50,
            ),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_on()
        await debounce.current_task(entity._key)

        sent = api.sent[0]
        assert (
            sent.is_on, sent.brightness, sent.color_temp, sent.sleep_brightness
        ) == (True, 80, 32, 50)

    asyncio.run(scenario())


def test_the_switch_shares_a_device_with_the_fan():
    entity, _, _ = make_switch(LightState(is_on=True, brightness=50))
    assert entity.device_info["identifiers"] == {("panasonic_wifan", "abc123")}


def test_the_switch_sees_a_change_made_by_the_light(monkeypatch):
    """The light and the switch drive the same fitting, so they share state."""

    async def scenario():
        store = make_store(LightState(is_on=False, brightness=10, sleep=False))

        # The light entity turns itself on and brightens.
        store.set_light(FAN, LightState(is_on=True, brightness=90, sleep=False))

        entity, api, debounce = make_switch(
            None, store=store, monkeypatch=monkeypatch
        )
        await entity.async_turn_on()
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep is True
        assert api.sent[0].is_on is True
        assert api.sent[0].brightness == 90

    asyncio.run(scenario())


def test_a_burst_including_a_sleep_toggle_produces_one_merged_command(monkeypatch):
    async def scenario():
        store = make_store(LightState(is_on=True, brightness=40, sleep=False))
        entity, api, debounce = make_switch(
            None, store=store, monkeypatch=monkeypatch, delay=0.02, after=0.01
        )

        await entity.async_turn_on()
        await asyncio.sleep(0.005)
        await entity.async_turn_off()

        await debounce.current_task(entity._key)

        assert len(api.sent) == 1
        assert api.sent[0].sleep is False

    asyncio.run(scenario())


def test_a_change_after_the_wait_settles_gets_its_own_command(monkeypatch):
    async def scenario():
        store = make_store(LightState(is_on=True, brightness=40, sleep=False))
        entity, api, debounce = make_switch(
            None, store=store, monkeypatch=monkeypatch, delay=0.01, after=0.001
        )

        await entity.async_turn_on()
        await debounce.current_task(entity._key)

        await entity.async_turn_off()
        await debounce.current_task(entity._key)

        assert len(api.sent) == 2
        assert api.sent[0].sleep is True
        assert api.sent[1].sleep is False

    asyncio.run(scenario())


def test_a_poll_during_the_wait_does_not_revert_the_state(monkeypatch):
    async def scenario():
        stale = types_.DeviceState(
            fan=types_.FanState(is_on=True, speed=6, reverse=False, yuragi=True),
            light=LightState(is_on=True, brightness=50, sleep=False),
        )
        entity, api, debounce = make_switch(
            LightState(is_on=True, brightness=50, sleep=False),
            monkeypatch=monkeypatch,
            delay=0.05,
            after=0.01,
            api=Recorder(reported=stale),
        )

        await entity.async_turn_on()
        # Still inside the 3 s (here 0.05 s) wait.
        await entity.async_update()

        assert entity.is_on is True
        assert api.get_state_calls == []

        await debounce.current_task(entity._key)

    asyncio.run(scenario())


def test_a_failed_send_is_logged_and_still_followed_by_a_read_back(
    monkeypatch, caplog
):
    async def scenario():
        # The device still reports sleep mode on: the read-back is what the
        # entity ends up showing, not the (unsent) normal-mode optimism.
        fresh = types_.DeviceState(
            fan=types_.FanState(is_on=True, speed=6, reverse=False, yuragi=True),
            light=LightState(is_on=True, brightness=50, sleep=True),
        )
        entity, api, debounce = make_switch(
            LightState(is_on=True, brightness=50, sleep=True),
            monkeypatch=monkeypatch,
            api=Recorder(reported=fresh, fail_send=True),
        )

        with caplog.at_level(logging.ERROR):
            await entity.async_turn_off()
            await debounce.current_task(entity._key)

        assert api.sent == []
        assert api.get_state_calls == [0]
        assert "Error sending sleep mode" in caplog.text
        assert entity.is_on is True  # the read-back's own answer, not False

    asyncio.run(scenario())


def test_the_switch_ignores_a_read_that_may_predate_its_command(monkeypatch):
    """A read arriving straight after a command can describe the old state."""

    async def scenario():
        store = make_store(LightState(is_on=False, brightness=80, sleep=False))
        entity, api, debounce = make_switch(
            None, store=store, monkeypatch=monkeypatch
        )

        await entity.async_turn_on()
        await debounce.current_task(entity._key)
        assert entity.is_on is True

        class Api:
            async def get_state_for_fan(self, fan):
                return types_.DeviceState(
                    fan=types_.FanState(
                        is_on=True, speed=6, reverse=False, yuragi=True
                    ),
                    light=LightState(is_on=False, brightness=80, sleep=False),
                )

        entity._api = Api()
        await entity.async_update()

        assert entity.is_on is True
        assert store.light(FAN).sleep is True

    asyncio.run(scenario())
