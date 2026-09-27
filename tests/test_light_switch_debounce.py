"""Tests for the light entity and the sleep switch sharing one light wait.

T1 built the shared restartable-wait helper for the fan; this pins the part
that is specific to the light group — one wait per appliance shared by the
light entity and the sleep switch, a command built from the store at send
time so a change from either entity folds into it, and a single fresh
read-back applied to both (decision 15).

Skipped unless Home Assistant is installed, since the entities subclass it.
"""

import asyncio
import logging

import pytest

from _component import load

pytest.importorskip("homeassistant", reason="Home Assistant is not installed")

light, switch, types_, store_module, debounce_module, api_module = load(
    "light", "switch", "types", "store", "debounce", "api"
)
LightState = types_.LightState
DeviceState = types_.DeviceState
FanState = types_.FanState

FAN = types_.Fan(
    appliance_id="abc123",
    com_id="FM14GX",
    hashed_guid="g",
    name="Living Room",
    product_code="F-M14GX",
    serial_number="SN1",
)


class Recorder:
    """Stands in for the shared ApiClient used by both entities."""

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


def make_pair(light_state, *, monkeypatch, delay=0.02, after=0.01, api=None):
    """A light entity and a sleep switch for the same fan, one store, one
    debouncer, and — crucially — the same shared key between them."""
    monkeypatch.setattr(light, "REFRESH_AFTER_COMMAND", after)
    monkeypatch.setattr(switch, "REFRESH_AFTER_COMMAND", after)

    api = api or Recorder()
    store = store_module.StateStore(
        {
            FAN.unique_id: DeviceState(
                fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
                light=light_state,
            )
        }
    )
    debounce = debounce_module.CommandDebouncer(delay=delay)

    the_light = light.PanasonicWiFiLight(api, FAN, store, debounce)
    the_switch = switch.PanasonicWiFiSleepSwitch(api, FAN, store, debounce)
    the_light.async_write_ha_state = lambda: None
    the_switch.async_write_ha_state = lambda: None

    assert the_light._key == the_switch._key  # one wait per appliance, shared

    return the_light, the_switch, api, store, debounce


def test_a_brightness_change_and_a_sleep_toggle_merge_into_one_command(monkeypatch):
    async def scenario():
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False), monkeypatch=monkeypatch
        )

        await the_light.async_turn_on(brightness=light.to_ha_brightness(90))
        await asyncio.sleep(0.005)
        await the_switch.async_turn_on()  # sleep mode, inside the same wait

        await debounce.current_task(the_light._key)

        assert len(api.sent) == 1
        sent = api.sent[0]
        assert sent.sleep is True
        assert sent.brightness == light.to_device_brightness(
            light.to_ha_brightness(90)
        )

    asyncio.run(scenario())


def test_a_sleep_toggle_and_a_brightness_change_merge_the_other_way(monkeypatch):
    """Whichever entity changes last still carries the whole group.

    The switch's toggle lands first, so by the time the brightness change
    comes in, sleep mode is already the active one — the brightness belongs
    to ``sleep_brightness`` (snapped to a step), leaving the normal
    ``brightness`` field untouched, same as ``test_light.py``'s own pinning
    of that rule.
    """

    async def scenario():
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False), monkeypatch=monkeypatch
        )

        await the_switch.async_turn_on()
        await asyncio.sleep(0.005)
        await the_light.async_turn_on(brightness=light.to_ha_brightness(15))

        await debounce.current_task(the_light._key)

        assert len(api.sent) == 1
        sent = api.sent[0]
        assert sent.sleep is True
        assert sent.brightness == 40  # the normal setting is untouched
        assert sent.sleep_brightness == api_module.nearest_sleep_step(
            light.to_device_brightness(light.to_ha_brightness(15))
        )

    asyncio.run(scenario())


def test_a_change_after_the_wait_settles_gets_its_own_command(monkeypatch):
    async def scenario():
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            delay=0.01,
            after=0.001,
        )

        await the_light.async_turn_on(brightness=light.to_ha_brightness(20))
        await debounce.current_task(the_light._key)

        await the_switch.async_turn_on()
        await debounce.current_task(the_switch._key)

        assert len(api.sent) == 2
        assert api.sent[0].sleep is False
        assert api.sent[1].sleep is True

    asyncio.run(scenario())


def test_both_entities_update_from_the_2s_read_back_inside_settle(monkeypatch):
    """The read-back is applied to both, although it lands inside SETTLE."""

    async def scenario():
        fresh = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            # The device answers with something other than what was sent —
            # the case this exercises is whether both entities pick it up.
            # sleep=True, so the brightness shown comes from sleep_brightness.
            light=LightState(
                is_on=True, brightness=1, sleep=True, sleep_brightness=77
            ),
        )
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            api=Recorder(reported=fresh),
        )

        await the_light.async_turn_on(brightness=light.to_ha_brightness(10))
        await debounce.current_task(the_light._key)

        assert the_light.brightness == light.to_ha_brightness(77)
        assert the_light.effect == "Sleep"
        assert the_switch.is_on is True
        assert store.light(FAN).sleep_brightness == 77

    asyncio.run(scenario())


def test_only_one_fresh_read_answers_both_entities(monkeypatch):
    """Decision 15: one max_age=0 read shared by the light and the switch."""

    async def scenario():
        fresh = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(is_on=True, brightness=63, sleep=False),
        )
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            api=Recorder(reported=fresh),
        )

        await the_light.async_turn_on(brightness=light.to_ha_brightness(10))
        await debounce.current_task(the_light._key)

        assert api.get_state_calls == [0]  # exactly one fresh cloud read
        assert the_light.brightness == light.to_ha_brightness(63)
        assert the_switch.is_on is False

    asyncio.run(scenario())


def test_the_sleep_mode_case_from_83aede5(monkeypatch):
    """Turning sleep on while the light is off: after the read-back the light
    shows on in sleep mode, and the next light change still carries it."""

    async def scenario():
        fresh = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            # Entering sleep mode lights the fitting on its own.
            light=LightState(is_on=True, brightness=1, sleep=True, sleep_brightness=1),
        )
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=False, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            api=Recorder(reported=fresh),
        )

        await the_switch.async_turn_on()  # sleep on, light was off
        await debounce.current_task(the_switch._key)

        assert the_light.is_on is True
        assert the_light.effect == "Sleep"
        assert the_switch.is_on is True

        # The next light change still carries sleep mode.
        api.sent.clear()
        await the_light.async_turn_on(brightness=light.to_ha_brightness(50))
        await debounce.current_task(the_light._key)

        assert api.sent[0].sleep is True

    asyncio.run(scenario())


def test_an_incidental_poll_during_the_wait_is_dropped_for_both(monkeypatch):
    async def scenario():
        stale = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(is_on=False, brightness=1, sleep=False),
        )
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            delay=0.05,
            after=0.01,
            api=Recorder(reported=stale),
        )

        await the_light.async_turn_on(brightness=light.to_ha_brightness(90))
        # Still inside the wait: a 5-min scan or a manual refresh on either
        # entity must not revert the optimistic state.
        await the_light.async_update()
        await the_switch.async_update()

        assert the_light.brightness == light.to_ha_brightness(90)
        assert the_switch.is_on is False
        assert api.get_state_calls == []

        await debounce.current_task(the_light._key)

    asyncio.run(scenario())


def test_an_incidental_poll_inside_settle_of_the_send_is_dropped(monkeypatch):
    """A poll within SETTLE of the send is dropped even on the other entity."""

    async def scenario():
        fresh = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(is_on=True, brightness=77, sleep=False),
        )
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            delay=0.01,
            after=0.001,
            api=Recorder(reported=fresh),
        )

        await the_light.async_turn_on(brightness=light.to_ha_brightness(90))
        await debounce.current_task(the_light._key)

        # The deliberate read-back has just applied `fresh`. A further
        # incidental poll on the switch, right after, must not show a
        # different (stale) answer, even though it still reaches the cloud.
        before = the_light.brightness

        class StalePoll:
            async def get_state_for_fan(self, fan):
                return DeviceState(
                    fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
                    light=LightState(is_on=False, brightness=1, sleep=True),
                )

        the_switch._api = StalePoll()
        await the_switch.async_update()

        assert the_light.brightness == before
        assert store.light(FAN).sleep is False  # untouched by the stale poll

    asyncio.run(scenario())


def test_a_failed_send_is_logged_and_still_read_back_for_both(monkeypatch, caplog):
    async def scenario():
        fresh = DeviceState(
            fan=FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(
                is_on=True, brightness=1, sleep=True, sleep_brightness=88
            ),
        )
        the_light, the_switch, api, store, debounce = make_pair(
            LightState(is_on=True, brightness=40, sleep=False),
            monkeypatch=monkeypatch,
            api=Recorder(reported=fresh, fail_send=True),
        )

        with caplog.at_level(logging.ERROR):
            await the_light.async_turn_on(brightness=light.to_ha_brightness(10))
            await debounce.current_task(the_light._key)

        assert api.sent == []
        assert "Error sending light state" in caplog.text
        assert the_light.brightness == light.to_ha_brightness(88)
        assert the_switch.is_on is True

    asyncio.run(scenario())
