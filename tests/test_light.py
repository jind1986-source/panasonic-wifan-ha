"""Tests for the light entity.

Skipped unless Home Assistant is installed, since the entity subclasses it.
"""

import asyncio

import pytest

from _component import load

pytest.importorskip("homeassistant", reason="Home Assistant is not installed")

light, types_ = load("light", "types")
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
    entity = light.PanasonicWiFiLight(
        None, FAN, LightState(is_on=True, brightness=58, color_temp=32)
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


def test_turning_on_with_a_kelvin_value_sends_it():
    sent = []

    class Api:
        async def set_light_state(self, fan, state):
            sent.append(state)

    entity = light.PanasonicWiFiLight(
        Api(),
        FAN,
        LightState(
            is_on=False,
            brightness=58,
            color_temp=0,
            companions=((0x00F4, b"\x42"), (0x00F7, b"\x01")),
        ),
    )
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_turn_on(color_temp_kelvin=6500))
    assert sent[0].color_temp == 100
    assert sent[0].is_on is True


def test_turning_on_without_a_kelvin_value_keeps_the_current_one():
    sent = []

    class Api:
        async def set_light_state(self, fan, state):
            sent.append(state)

    entity = light.PanasonicWiFiLight(
        Api(),
        FAN,
        LightState(
            is_on=False,
            brightness=58,
            color_temp=32,
            companions=((0x00F4, b"\x42"), (0x00F7, b"\x01")),
        ),
    )
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_turn_on())
    assert sent[0].color_temp == 32


def test_entity_shares_a_device_with_the_fan():
    entity = light.PanasonicWiFiLight(None, FAN, LightState(is_on=False, brightness=1))
    assert entity.device_info["identifiers"] == {("panasonic_wifan", "abc123")}
    assert entity.is_on is False


class Entry:
    entry_id = "entry-1"


def setup_light(states, api=None):
    """Run the light platform's setup with a stand-in hass and collect entities."""
    hass = type("Hass", (), {})()
    hass.data = {
        "panasonic_wifan": {
            Entry.entry_id: {"api": api, "fans": [FAN], "states": states}
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


def test_entity_sends_the_companions_it_last_read():
    """Without them the device beeps and ignores the command."""
    companions = ((0x00F4, b"\x42"), (0x00F6, b"\x20"), (0x00F7, b"\x01"))
    sent = []

    class Api:
        async def set_light_state(self, fan, state):
            sent.append(state)

    entity = light.PanasonicWiFiLight(
        Api(), FAN, LightState(is_on=False, brightness=58, companions=companions)
    )
    entity.hass = None
    entity.async_write_ha_state = lambda: None

    asyncio.run(entity.async_turn_on())
    assert sent[0].companions == companions
    assert sent[0].is_on is True
