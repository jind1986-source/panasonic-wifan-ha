"""Tests for the sleep mode switch.

It exists because HomeKit's lightbulb service has no light effects, so sleep
mode cannot reach Apple Home through the light entity.
"""

import asyncio

import pytest

from _component import load

pytest.importorskip("homeassistant", reason="Home Assistant is not installed")

switch, types_ = load("switch", "types")
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
    def __init__(self):
        self.sent = []

    async def set_light_state(self, fan, state):
        self.sent.append(state)


def make_switch(state):
    api = Recorder()
    entity = switch.PanasonicWiFiSleepSwitch(api, FAN, state)
    entity.async_write_ha_state = lambda: None
    return entity, api


def test_the_switch_reflects_the_light_mode():
    entity, _ = make_switch(LightState(is_on=True, brightness=50, sleep=True))
    assert entity.is_on is True
    assert entity.unique_id == "abc123_light_sleep"


def test_turning_the_switch_on_selects_sleep_mode():
    entity, api = make_switch(LightState(is_on=True, brightness=50, sleep=False))
    asyncio.run(entity.async_turn_on())
    assert api.sent[0].sleep is True


def test_turning_the_switch_off_returns_to_normal():
    entity, api = make_switch(
        LightState(is_on=True, brightness=50, sleep=True, sleep_brightness=50)
    )
    asyncio.run(entity.async_turn_off())
    assert api.sent[0].sleep is False


def test_the_switch_leaves_every_other_setting_alone():
    entity, api = make_switch(
        LightState(
            is_on=True, brightness=80, color_temp=32, sleep=False, sleep_brightness=50
        )
    )
    asyncio.run(entity.async_turn_on())
    sent = api.sent[0]
    assert (sent.is_on, sent.brightness, sent.color_temp, sent.sleep_brightness) == (
        True, 80, 32, 50
    )


def test_the_switch_shares_a_device_with_the_fan():
    entity, _ = make_switch(LightState(is_on=True, brightness=50))
    assert entity.device_info["identifiers"] == {("panasonic_wifan", "abc123")}
