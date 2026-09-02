"""Tests for the light entity.

Skipped unless Home Assistant is installed, since the entity subclasses it.
"""

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
