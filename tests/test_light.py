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

SLEEP_STEPS = (1, 50, 100)


class Recorder:
    """Stands in for the API, keeping what the entity sent."""

    def __init__(self):
        self.sent = []

    async def set_light_state(self, fan, state):
        self.sent.append(state)


def make_entity(state):
    api = Recorder()
    entity = light.PanasonicWiFiLight(api, FAN, state)
    entity.async_write_ha_state = lambda: None
    return entity, api


def at_percent(percent: int) -> int:
    """Home Assistant brightness for a percentage, as a UI would send it."""
    return round(percent / 100 * 255)


# --- the combined brightness scale ------------------------------------------


def test_sleep_levels_sit_below_the_normal_range():
    """Sleep mode dims further than normal mode allows, so it goes underneath."""
    for index, step in enumerate(SLEEP_STEPS, start=1):
        state = LightState(
            is_on=True, brightness=50, sleep=True, sleep_brightness=step
        )
        assert light.state_to_level(state) == index

    assert light.state_to_level(LightState(is_on=True, brightness=1)) == 4
    assert light.state_to_level(LightState(is_on=True, brightness=100)) == 103


@pytest.mark.parametrize("level", range(1, 104))
def test_every_level_survives_a_round_trip(level):
    assert light.ha_brightness_to_level(light.level_to_ha_brightness(level)) == level


def test_the_scale_spans_the_whole_slider():
    assert light.level_to_ha_brightness(1) == 1
    assert light.level_to_ha_brightness(light.TOTAL_LEVELS) == 255


@pytest.mark.parametrize("brightness", [-50, 0, 300, 1000])
def test_a_brightness_outside_the_slider_is_clamped(brightness):
    assert 1 <= light.ha_brightness_to_level(brightness) <= light.TOTAL_LEVELS


# --- brightness driving the mode --------------------------------------------


def test_the_bottom_of_the_slider_selects_sleep_mode():
    entity, api = make_entity(LightState(is_on=True, brightness=80, sleep=False))
    asyncio.run(entity.async_turn_on(brightness=1))
    assert api.sent[0].sleep is True
    assert api.sent[0].sleep_brightness == 1


@pytest.mark.parametrize("level,step", [(1, 1), (2, 50), (3, 100)])
def test_each_sleep_step_is_reachable(level, step):
    entity, api = make_entity(LightState(is_on=True, brightness=80, sleep=False))
    asyncio.run(
        entity.async_turn_on(brightness=light.level_to_ha_brightness(level))
    )
    assert api.sent[0].sleep is True
    assert api.sent[0].sleep_brightness == step


def test_dragging_above_the_sleep_band_returns_to_normal_mode():
    entity, api = make_entity(
        LightState(is_on=True, brightness=80, sleep=True, sleep_brightness=50)
    )
    asyncio.run(entity.async_turn_on(brightness=at_percent(50)))
    assert api.sent[0].sleep is False
    assert 40 <= api.sent[0].brightness <= 60


def test_the_top_of_the_slider_is_full_normal_brightness():
    entity, api = make_entity(LightState(is_on=True, brightness=10, sleep=False))
    asyncio.run(entity.async_turn_on(brightness=255))
    assert api.sent[0].sleep is False
    assert api.sent[0].brightness == 100


def test_entering_sleep_mode_keeps_the_normal_brightness():
    entity, api = make_entity(LightState(is_on=True, brightness=80, sleep=False))
    asyncio.run(entity.async_turn_on(brightness=1))
    assert api.sent[0].brightness == 80


def test_leaving_sleep_mode_keeps_the_sleep_brightness():
    entity, api = make_entity(
        LightState(is_on=True, brightness=80, sleep=True, sleep_brightness=100)
    )
    asyncio.run(entity.async_turn_on(brightness=at_percent(50)))
    assert api.sent[0].sleep_brightness == 100


def test_a_sleeping_light_reports_a_brightness_in_the_sleep_band():
    entity, _ = make_entity(
        LightState(is_on=True, brightness=100, sleep=True, sleep_brightness=50)
    )
    assert entity.brightness == light.level_to_ha_brightness(2)


# --- effects ----------------------------------------------------------------


def test_the_light_offers_normal_and_sleep_as_effects():
    entity, _ = make_entity(LightState(is_on=True, brightness=58))
    assert entity.effect_list == ["Normal", "Sleep"]
    assert entity.effect == "Normal"


def test_selecting_sleep_as_an_effect_switches_the_mode():
    entity, api = make_entity(
        LightState(is_on=True, brightness=58, sleep=False, sleep_brightness=50)
    )
    asyncio.run(entity.async_turn_on(effect="Sleep"))
    assert api.sent[0].sleep is True
    assert entity.effect == "Sleep"


def test_selecting_an_effect_keeps_each_mode_brightness():
    entity, api = make_entity(
        LightState(is_on=True, brightness=80, sleep=False, sleep_brightness=50)
    )
    asyncio.run(entity.async_turn_on(effect="Sleep"))
    assert api.sent[0].brightness == 80
    assert api.sent[0].sleep_brightness == 50


# --- colour temperature -----------------------------------------------------


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
    entity, api = make_entity(LightState(is_on=False, brightness=58, color_temp=0))
    asyncio.run(entity.async_turn_on(color_temp_kelvin=6500))
    assert api.sent[0].color_temp == 100
    assert api.sent[0].is_on is True


def test_turning_on_without_a_kelvin_value_keeps_the_current_one():
    entity, api = make_entity(LightState(is_on=False, brightness=58, color_temp=32))
    asyncio.run(entity.async_turn_on())
    assert api.sent[0].color_temp == 32


def test_the_light_offers_no_colour_picker():
    """The fitting has white balance only, so no RGB mode is declared."""
    entity, _ = make_entity(LightState(is_on=True, brightness=58))
    assert entity.supported_color_modes == {"color_temp"}
    assert "hs" not in entity.supported_color_modes
    assert "rgb" not in entity.supported_color_modes


def test_entity_reports_its_identity_and_ranges():
    entity, _ = make_entity(
        LightState(is_on=True, brightness=58, color_temp=32)
    )
    assert entity.unique_id == "abc123_light"
    assert entity.color_mode == "color_temp"
    assert entity.is_on is True
    assert entity.color_temp_kelvin == light.to_kelvin(32)
    assert entity.min_color_temp_kelvin == 2700
    assert entity.max_color_temp_kelvin == 6500


# --- turning off ------------------------------------------------------------


def test_turning_off_keeps_every_setting():
    entity, api = make_entity(
        LightState(
            is_on=True, brightness=80, color_temp=32, sleep=True, sleep_brightness=50
        )
    )
    asyncio.run(entity.async_turn_off())
    sent = api.sent[0]
    assert sent.is_on is False
    assert (sent.brightness, sent.color_temp, sent.sleep, sent.sleep_brightness) == (
        80, 32, True, 50
    )


# --- platform setup ---------------------------------------------------------


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
    assert [e.unique_id for e in setup_light(states)] == ["abc123_light"]


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


def test_entity_shares_a_device_with_the_fan():
    entity, _ = make_entity(LightState(is_on=False, brightness=1))
    assert entity.device_info["identifiers"] == {("panasonic_wifan", "abc123")}
    assert entity.is_on is False
