"""Light platform for Panasonic WIFAN integration.

Fans that carry a light expose it as a separate entity on the same device. The
platform adds nothing when the light's packet field ids are still unknown (see
ID_LIGHT_POWER in const.py) or when a device does not report them, so fan-only
models are unaffected.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DAYLIGHT_KELVIN,
    DOMAIN,
    ID_LIGHT_POWER,
    MAX_BRIGHTNESS,
    MAX_COLOR_TEMP,
    MIN_BRIGHTNESS,
    MIN_COLOR_TEMP,
    WARM_KELVIN,
)
from .types import Fan, LightState

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light of every fan that reports one."""
    if ID_LIGHT_POWER is None:
        _LOGGER.debug(
            "Light field ids are unknown for this integration, skipping lights"
        )
        return

    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    fans = data["fans"]
    states = data.get("states") or {}

    # Any device whose state did not arrive during setup is asked again here
    # rather than silently going without a light entity.
    unknown = [fan for fan in fans if fan.unique_id not in states]
    if unknown:
        _LOGGER.debug(
            "Re-reading state for %s", ", ".join(fan.name for fan in unknown)
        )
        try:
            states = {**states, **await api.get_state_for_fans(unknown)}
        except Exception as err:  # noqa: BLE001 - setup must not abort on one device
            _LOGGER.error("Could not read state while looking for lights: %s", err)

    entities = []
    for fan in fans:
        state = states.get(fan.unique_id)
        if state is None:
            _LOGGER.warning(
                "%s did not report its state, so it gets no light entity",
                fan.name,
            )
            continue
        if state.light is None:
            _LOGGER.debug("%s reports no light", fan.name)
            continue
        entities.append(PanasonicWiFiLight(api, fan, state.light))

    _LOGGER.debug("Adding %s light entity(ies)", len(entities))
    async_add_entities(entities)


def to_ha_brightness(value: int) -> int:
    """Convert a device brightness step to Home Assistant's 1-255 scale."""
    span = MAX_BRIGHTNESS - MIN_BRIGHTNESS
    if span <= 0:
        return 255
    clamped = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, value))
    return round((clamped - MIN_BRIGHTNESS) / span * 254) + 1


def to_device_brightness(brightness: int) -> int:
    """Convert Home Assistant's 1-255 brightness to a device step."""
    span = MAX_BRIGHTNESS - MIN_BRIGHTNESS
    if span <= 0:
        return MAX_BRIGHTNESS
    value = round((brightness - 1) / 254 * span) + MIN_BRIGHTNESS
    return max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, value))


def to_kelvin(color_temp: int) -> int:
    """Convert the device's warm-to-daylight percentage to Kelvin."""
    span = MAX_COLOR_TEMP - MIN_COLOR_TEMP
    clamped = max(MIN_COLOR_TEMP, min(MAX_COLOR_TEMP, color_temp))
    fraction = (clamped - MIN_COLOR_TEMP) / span if span else 0
    return round(WARM_KELVIN + fraction * (DAYLIGHT_KELVIN - WARM_KELVIN))


def to_device_color_temp(kelvin: int) -> int:
    """Convert Kelvin to the device's warm-to-daylight percentage."""
    span = DAYLIGHT_KELVIN - WARM_KELVIN
    fraction = (kelvin - WARM_KELVIN) / span if span else 0
    value = round(MIN_COLOR_TEMP + fraction * (MAX_COLOR_TEMP - MIN_COLOR_TEMP))
    return max(MIN_COLOR_TEMP, min(MAX_COLOR_TEMP, value))


class PanasonicWiFiLight(LightEntity):  # type: ignore[misc]
    """The light built into a Panasonic WIFAN."""

    _attr_icon = "mdi:ceiling-fan-light"
    _attr_should_poll = True
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = WARM_KELVIN
    _attr_max_color_temp_kelvin = DAYLIGHT_KELVIN
    _attr_has_entity_name = True
    _attr_name = "Light"

    def __init__(self, api, fan: Fan, state: LightState) -> None:
        """Initialize the light entity."""
        self._api = api
        self._fan = fan
        self._attr_unique_id = f"{fan.unique_id}_light"

        self._current_state = state
        self._attr_is_on = state.is_on
        self._attr_brightness = to_ha_brightness(state.brightness)
        self._attr_color_temp_kelvin = to_kelvin(state.color_temp)

        # Same identifiers as the fan, so both entities sit on one device.
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._fan.unique_id)},
            "name": self._fan.name,
            "manufacturer": "Panasonic",
            "model": self._fan.product_code,
            "serial_number": self._fan.serial_number,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally at a brightness or colour temperature."""
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255)
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        color_temp = (
            to_device_color_temp(kelvin)
            if kelvin is not None
            else self._current_state.color_temp
        )
        await self._push_state(True, to_device_brightness(brightness), color_temp)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self._push_state(
            False, self._current_state.brightness, self._current_state.color_temp
        )

    async def _push_state(
        self, is_on: bool, brightness: int, color_temp: int
    ) -> None:
        """Send a light command and update the optimistic state.

        The companion fields come from the last state read: the device ignores
        a light command that does not carry them.
        """
        state = LightState(
            is_on=is_on,
            brightness=brightness,
            color_temp=color_temp,
            companions=self._current_state.companions,
        )

        _LOGGER.debug(
            "Pushing light state for %s: is_on=%s, brightness=%s, color_temp=%s",
            self._fan.name,
            state.is_on,
            state.brightness,
            state.color_temp,
        )
        await self._api.set_light_state(self._fan, state)

        self._current_state = state
        self._attr_is_on = state.is_on
        self._attr_brightness = to_ha_brightness(state.brightness)
        self._attr_color_temp_kelvin = to_kelvin(state.color_temp)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch the light's state from the cloud."""
        try:
            state = await self._api.get_state_for_fan(self._fan)
        except Exception as err:  # noqa: BLE001 - a failed poll must not raise
            _LOGGER.error("Error updating %s light: %s", self._fan.name, err)
            return

        if state.light is None:
            _LOGGER.debug("%s stopped reporting light state", self._fan.name)
            return

        self._current_state = state.light
        self._attr_is_on = state.light.is_on
        self._attr_brightness = to_ha_brightness(state.light.brightness)
        self._attr_color_temp_kelvin = to_kelvin(state.light.color_temp)

        _LOGGER.debug(
            "Updated %s light: is_on=%s, brightness=%s, color_temp=%sK",
            self._fan.name,
            state.light.is_on,
            self._attr_brightness,
            self._attr_color_temp_kelvin,
        )
