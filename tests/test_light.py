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


def test_entity_reports_brightness_support():
    entity = light.PanasonicWiFiLight(None, FAN, LightState(is_on=True, brightness=58))
    assert entity.unique_id == "abc123_light"
    assert entity.color_mode == "brightness"
    assert entity.supported_color_modes == {"brightness"}
    assert entity.is_on is True
    assert entity.brightness == light.to_ha_brightness(58)


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
