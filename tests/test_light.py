"""Tests for the light entity.

Skipped unless Home Assistant is installed, since the entity subclasses it.
"""

import asyncio
import logging

import pytest

from _component import load

pytest.importorskip("homeassistant", reason="Home Assistant is not installed")

light, types_, store_module, debounce_module = load("light", "types", "store", "debounce")
Fan = types_.Fan
LightState = types_.LightState

FAN = Fan(
    appliance_id="abc123",
    com_id="FM14GX",
    hashed_guid="g",
    name="Living Room",
    product_code="F-M14GX",
    serial_number="SN1",
)


@pytest.mark.parametrize("value", range(1, 101))
def test_brightness_survives_a_round_trip(value):
    assert light.to_device_brightness(light.to_ha_brightness(value)) == value


def test_brightness_ends_are_the_ends_of_both_scales():
    assert light.to_ha_brightness(1) == 1
    assert light.to_ha_brightness(100) == 255
    assert light.to_device_brightness(1) == 1
    assert light.to_device_brightness(255) == 100


@pytest.mark.parametrize("value", [-5, 0, 255])
def test_out_of_range_device_brightness_is_clamped(value):
    assert 1 <= light.to_ha_brightness(value) <= 255


def test_entity_reports_brightness_and_colour_temperature_support():
    entity, _, _ = make_entity(
        LightState(is_on=True, brightness=58, color_temp=32)
    )
    assert entity.unique_id == "abc123_light"
    assert entity.color_mode == "color_temp"
    assert entity.supported_color_modes == {"color_temp"}
    assert entity.is_on is True
    assert entity.brightness == light.to_ha_brightness(58)
    assert entity.color_temp_kelvin == light.to_kelvin(32)
    assert entity.min_color_temp_kelvin == 2700
    assert entity.max_color_temp_kelvin == 6500


def test_warm_and_daylight_map_to_the_ends_of_the_kelvin_range():
    assert light.to_kelvin(0) == 2700
    assert light.to_kelvin(100) == 6500
    assert light.to_device_color_temp(2700) == 0
    assert light.to_device_color_temp(6500) == 100


@pytest.mark.parametrize("percent", range(0, 101, 5))
def test_colour_temperature_survives_a_round_trip(percent):
    assert light.to_device_color_temp(light.to_kelvin(percent)) == percent


@pytest.mark.parametrize("kelvin", [1000, 2000, 9000, 20000])
def test_out_of_range_kelvin_is_clamped(kelvin):
    assert 0 <= light.to_device_color_temp(kelvin) <= 100


class Recorder:
    """Stands in for the API, keeping what the entity sent and read back."""

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
        return self.reported


def make_store(light_state):
    return store_module.StateStore(
        {
            FAN.unique_id: types_.DeviceState(
                fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
                light=light_state,
            )
        }
    )


def make_entity(state, store=None, *, monkeypatch=None, delay=0.01, after=0.01, api=None):
    """A light entity wired to a fast debouncer, for tests that don't wait 3/2 s."""
    if monkeypatch is not None:
        monkeypatch.setattr(light, "REFRESH_AFTER_COMMAND", after)
    api = api or Recorder()
    store = store or make_store(state)
    debounce = debounce_module.CommandDebouncer(delay=delay)
    entity = light.PanasonicWiFiLight(api, FAN, store, debounce)
    entity.async_write_ha_state = lambda: None
    return entity, api, debounce


def test_turning_on_with_a_kelvin_value_sends_it(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=58, color_temp=0), monkeypatch=monkeypatch
        )
        await entity.async_turn_on(color_temp_kelvin=6500)
        await debounce.current_task(entity._key)

        assert api.sent[0].color_temp == 100
        assert api.sent[0].is_on is True

    asyncio.run(scenario())


def test_turning_on_without_a_kelvin_value_keeps_the_current_one(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=58, color_temp=32), monkeypatch=monkeypatch
        )
        await entity.async_turn_on()
        await debounce.current_task(entity._key)

        assert api.sent[0].color_temp == 32

    asyncio.run(scenario())


def test_a_change_shows_in_home_assistant_before_it_is_sent(monkeypatch):
    async def scenario():
        entity, api, _ = make_entity(
            LightState(is_on=False, brightness=58, color_temp=0),
            monkeypatch=monkeypatch,
            delay=1,
            after=1,
        )
        await entity.async_turn_on(color_temp_kelvin=6500)

        assert entity.is_on is True
        assert entity.color_temp_kelvin == 6500
        assert api.sent == []

    asyncio.run(scenario())


def test_the_light_offers_normal_and_sleep_as_effects():
    entity, _, _ = make_entity(LightState(is_on=True, brightness=58))
    assert entity.effect_list == ["Normal", "Sleep"]
    assert entity.effect == "Normal"


def test_selecting_sleep_switches_the_mode(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=True, brightness=58, sleep=False, sleep_brightness=50),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_on(effect="Sleep")
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep is True
        assert entity.effect == "Sleep"

    asyncio.run(scenario())


def test_a_sleeping_light_reports_its_sleep_brightness():
    entity, _, _ = make_entity(
        LightState(is_on=True, brightness=100, sleep=True, sleep_brightness=50)
    )
    assert entity.brightness == light.to_ha_brightness(50)


def test_brightness_in_sleep_mode_snaps_to_a_step(monkeypatch):
    """Sleep mode takes three fixed steps; anything else the device ignores."""

    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=True, brightness=80, sleep=True, sleep_brightness=100),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_on(brightness=light.to_ha_brightness(10))
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep_brightness == 1
        assert api.sent[0].brightness == 80  # the normal setting is untouched

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "percent,expected", [(1, 1), (20, 1), (40, 50), (60, 50), (90, 100), (100, 100)]
)
def test_every_slider_position_lands_on_a_step(percent, expected, monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=True, brightness=80, sleep=True, sleep_brightness=1),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_on(brightness=light.to_ha_brightness(percent))
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep_brightness == expected

    asyncio.run(scenario())


def test_brightness_in_normal_mode_leaves_the_sleep_brightness_alone(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=True, brightness=80, sleep=False, sleep_brightness=50),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_on(brightness=light.to_ha_brightness(30))
        await debounce.current_task(entity._key)

        assert api.sent[0].brightness == 30
        assert api.sent[0].sleep_brightness == 50

    asyncio.run(scenario())


def test_turning_off_keeps_every_setting(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(
                is_on=True, brightness=80, color_temp=32, sleep=True,
                sleep_brightness=50,
            ),
            monkeypatch=monkeypatch,
        )
        await entity.async_turn_off()
        await debounce.current_task(entity._key)

        sent = api.sent[0]
        assert sent.is_on is False
        assert (
            sent.brightness, sent.color_temp, sent.sleep, sent.sleep_brightness
        ) == (80, 32, True, 50)

    asyncio.run(scenario())


def test_the_light_offers_no_colour_picker():
    """The fitting has white balance only, so no RGB mode is declared."""
    entity, _, _ = make_entity(LightState(is_on=True, brightness=58))
    assert entity.supported_color_modes == {"color_temp"}
    assert "hs" not in entity.supported_color_modes
    assert "rgb" not in entity.supported_color_modes


def test_entity_shares_a_device_with_the_fan():
    entity, _, _ = make_entity(LightState(is_on=False, brightness=1))
    assert entity.device_info["identifiers"] == {("panasonic_wifan", "abc123")}
    assert entity.is_on is False


def test_a_burst_of_changes_produces_one_merged_light_command(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=50, color_temp=10, sleep=False),
            monkeypatch=monkeypatch,
            delay=0.02,
            after=0.01,
        )

        await entity.async_turn_on(brightness=light.to_ha_brightness(40))
        await asyncio.sleep(0.005)
        await entity.async_turn_on(color_temp_kelvin=6500)
        await asyncio.sleep(0.005)
        await entity.async_turn_on(effect="Sleep")

        await debounce.current_task(entity._key)

        assert len(api.sent) == 1
        sent = api.sent[0]
        assert sent.is_on is True
        assert sent.brightness == light.to_device_brightness(
            light.to_ha_brightness(40)
        )
        assert sent.color_temp == 100
        assert sent.sleep is True

    asyncio.run(scenario())


def test_a_change_after_the_wait_settles_gets_its_own_command(monkeypatch):
    async def scenario():
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=50),
            monkeypatch=monkeypatch,
            delay=0.01,
            after=0.001,
        )

        await entity.async_turn_on(brightness=light.to_ha_brightness(20))
        await debounce.current_task(entity._key)

        await entity.async_turn_on(brightness=light.to_ha_brightness(80))
        await debounce.current_task(entity._key)

        assert len(api.sent) == 2
        assert api.sent[0].brightness == light.to_device_brightness(
            light.to_ha_brightness(20)
        )
        assert api.sent[1].brightness == light.to_device_brightness(
            light.to_ha_brightness(80)
        )

    asyncio.run(scenario())


def test_a_change_while_the_send_is_in_flight_does_not_abort_it(monkeypatch):
    """decision 19, at the entity level.

    A brightness change made while the previous one is still being sent must
    not abort that write, and must not have its own read-back clobbered by
    the superseded cycle's — the store still holds what this fresh change
    set, not the stale value the first send captured before it went out
    (see PanasonicWiFiLight._send_command's record_command guard).
    """

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowFirstSend:
            def __init__(self):
                self.sent = []
                self.get_state_calls = []
                self._first = True
                self.reported = types_.DeviceState(
                    fan=types_.FanState(
                        is_on=True, speed=3, reverse=False, yuragi=False
                    ),
                    light=LightState(is_on=True, brightness=70, color_temp=10),
                )

            async def set_light_state(self, fan, state):
                if self._first:
                    self._first = False
                    started.set()
                    await release.wait()  # genuinely in flight
                self.sent.append(state)

            async def get_state_for_fan(self, fan, *, max_age=None):
                self.get_state_calls.append(max_age)
                return self.reported

        api = SlowFirstSend()
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=50, color_temp=10),
            monkeypatch=monkeypatch,
            delay=0.01,
            after=0.001,
            api=api,
        )

        await entity.async_turn_on(brightness=light.to_ha_brightness(40))
        first_task = debounce.current_task(entity._key)

        await started.wait()  # the first send is under way

        await entity.async_turn_on(brightness=light.to_ha_brightness(90))
        second_task = debounce.current_task(entity._key)

        release.set()  # let the first send run to completion
        await first_task
        await second_task

        assert len(api.sent) == 2
        assert api.sent[0].brightness == light.to_device_brightness(
            light.to_ha_brightness(40)
        )
        assert api.sent[1].brightness == light.to_device_brightness(
            light.to_ha_brightness(90)
        )
        # Only the second (current) cycle read back — the first cycle's
        # read-back was skipped once it was superseded.
        assert api.get_state_calls == [0]
        assert entity.brightness == light.to_ha_brightness(70)

    asyncio.run(scenario())


def test_a_poll_during_the_wait_does_not_revert_the_state(monkeypatch):
    async def scenario():
        stale = types_.DeviceState(
            fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(is_on=False, brightness=1),
        )
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=50),
            monkeypatch=monkeypatch,
            delay=0.05,
            after=0.01,
            api=Recorder(reported=stale),
        )

        await entity.async_turn_on(brightness=light.to_ha_brightness(90))
        # Still inside the 3 s (here 0.05 s) wait.
        await entity.async_update()

        assert entity.is_on is True
        assert entity.brightness == light.to_ha_brightness(90)
        assert api.get_state_calls == []

        await debounce.current_task(entity._key)

    asyncio.run(scenario())


def test_a_poll_within_settle_of_the_send_is_ignored(monkeypatch):
    async def scenario():
        stale = types_.DeviceState(
            fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(is_on=False, brightness=1),
        )
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=50),
            monkeypatch=monkeypatch,
            delay=0.01,
            after=0.001,
            api=Recorder(reported=stale),
        )

        await entity.async_turn_on(brightness=light.to_ha_brightness(90))
        await debounce.current_task(entity._key)

        # The send has just happened; real time elapsed is far under SETTLE
        # (5 s). The manual poll still reaches the cloud (record_poll's
        # SETTLE guard runs on the answer, like the fan's own commanded_at
        # guard runs before it), but its stale answer must not be shown.
        before_poll = entity.brightness

        await entity.async_update()

        assert entity.brightness == before_poll

    asyncio.run(scenario())


def test_a_failed_send_is_logged_and_still_followed_by_a_read_back(
    monkeypatch, caplog
):
    async def scenario():
        fresh = types_.DeviceState(
            fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
            light=LightState(is_on=True, brightness=77),
        )
        entity, api, debounce = make_entity(
            LightState(is_on=False, brightness=50),
            monkeypatch=monkeypatch,
            delay=0.01,
            after=0.01,
            api=Recorder(reported=fresh, fail_send=True),
        )

        with caplog.at_level(logging.ERROR):
            await entity.async_turn_on(brightness=light.to_ha_brightness(60))
            await debounce.current_task(entity._key)

        assert api.sent == []
        assert api.get_state_calls == [0]
        assert "Error sending light state" in caplog.text
        assert entity.brightness == light.to_ha_brightness(77)

    asyncio.run(scenario())


class Entry:
    entry_id = "entry-1"


def setup_light(states, api=None):
    """Run the light platform's setup with a stand-in hass and collect entities."""
    hass = type("Hass", (), {})()
    hass.data = {
        "panasonic_wifan": {
            Entry.entry_id: {
                "api": api,
                "fans": [FAN],
                "states": states,
                "store": store_module.StateStore(states),
                "debounce": debounce_module.CommandDebouncer(),
            }
        }
    }
    added = []
    asyncio.run(light.async_setup_entry(hass, Entry(), added.extend))
    return added


def test_setup_adds_a_light_for_a_device_that_reports_one():
    states = {FAN.unique_id: types_.DeviceState(
        fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
        light=LightState(is_on=True, brightness=58),
    )}
    entities = setup_light(states)
    assert [e.unique_id for e in entities] == ["abc123_light"]


def test_setup_adds_nothing_for_a_device_with_no_light():
    states = {FAN.unique_id: types_.DeviceState(
        fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
        light=None,
    )}
    assert setup_light(states) == []


def test_setup_re_reads_state_that_setup_did_not_capture():
    """A slow first poll must not cost the device its light entity."""
    state = types_.DeviceState(
        fan=types_.FanState(is_on=True, speed=3, reverse=False, yuragi=False),
        light=LightState(is_on=False, brightness=100),
    )

    class Api:
        def __init__(self):
            self.asked_for = None

        async def get_state_for_fans(self, fans):
            self.asked_for = [f.name for f in fans]
            return {FAN.unique_id: state}

    api = Api()
    entities = setup_light({}, api=api)
    assert api.asked_for == ["Living Room"]
    assert [e.unique_id for e in entities] == ["abc123_light"]


def test_setup_survives_a_re_read_that_fails():
    class Api:
        async def get_state_for_fans(self, fans):
            raise RuntimeError("cloud unreachable")

    assert setup_light({}, api=Api()) == []


def test_the_light_sees_a_mode_change_made_by_the_switch(monkeypatch):
    """Turning the light on after the sleep switch must not reset the mode."""

    async def scenario():
        store = make_store(LightState(is_on=False, brightness=80, sleep=False))

        # The switch puts the light into sleep mode.
        store.set_light(FAN, LightState(is_on=False, brightness=80, sleep=True))

        entity, api, debounce = make_entity(
            None, store=store, monkeypatch=monkeypatch
        )
        await entity.async_turn_on()
        await debounce.current_task(entity._key)

        assert api.sent[0].sleep is True
        assert api.sent[0].is_on is True

    asyncio.run(scenario())
