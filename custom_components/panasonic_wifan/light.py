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

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    ID_LIGHT_POWER,
    MAX_BRIGHTNESS,
    MIN_BRIGHTNESS,
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

    try:
        states = await api.get_state_for_fans(fans)
    except Exception as err:  # noqa: BLE001 - setup must not abort on one device
        _LOGGER.error("Could not read state while looking for lights: %s", err)
        return

    entities = []
    for fan in fans:
        state = states.get(fan.unique_id)
        if state is None or state.light is None:
            _LOGGER.debug("%s reports no light", fan.name)
            continue
        entities.append(PanasonicWiFiLight(api, fan, state.light))

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


class PanasonicWiFiLight(LightEntity):  # type: ignore[misc]
    """The light built into a Panasonic WIFAN."""

    _attr_icon = "mdi:ceiling-fan-light"
    _attr_should_poll = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
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

        # Same identifiers as the fan, so both entities sit on one device.
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._fan.unique_id)},
            "name": self._fan.name,
            "manufacturer": "Panasonic",
            "model": self._fan.product_code,
            "serial_number": self._fan.serial_number,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally at a given brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255)
        await self._push_state(True, to_device_brightness(brightness))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self._push_state(False, self._current_state.brightness)

    async def _push_state(self, is_on: bool, brightness: int) -> None:
        """Send a light command and update the optimistic state."""
        state = LightState(is_on=is_on, brightness=brightness)

        _LOGGER.debug(
            "Pushing light state for %s: is_on=%s, brightness=%s",
            self._fan.name,
            state.is_on,
            state.brightness,
        )
        await self._api.set_light_state(self._fan, state)

        self._current_state = state
        self._attr_is_on = state.is_on
        self._attr_brightness = to_ha_brightness(state.brightness)
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

        _LOGGER.debug(
            "Updated %s light: is_on=%s, brightness=%s",
            self._fan.name,
            state.light.is_on,
            self._attr_brightness,
        )
