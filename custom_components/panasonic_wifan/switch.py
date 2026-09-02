"""Switch platform for Panasonic WIFAN integration.

The light's sleep mode is exposed twice: as an effect on the light entity,
which is where it belongs, and as this switch. HomeKit's lightbulb service has
no notion of a light effect, so a bridged light cannot carry sleep mode to
Apple Home at all — a switch is the only shape that crosses that bridge.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ID_LIGHT_POWER
from .types import Fan, LightState

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a sleep mode switch for every fan whose light reports one."""
    if ID_LIGHT_POWER is None:
        return

    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    fans = data["fans"]
    states = data.get("states") or {}

    entities = []
    for fan in fans:
        state = states.get(fan.unique_id)
        if state is None or state.light is None:
            continue
        entities.append(PanasonicWiFiSleepSwitch(api, fan, state.light))

    _LOGGER.debug("Adding %s sleep mode switch(es)", len(entities))
    async_add_entities(entities)


class PanasonicWiFiSleepSwitch(SwitchEntity):  # type: ignore[misc]
    """The light's sleep mode, as a switch Apple Home can see."""

    _attr_icon = "mdi:weather-night"
    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_name = "Light sleep mode"

    def __init__(self, api, fan: Fan, state: LightState) -> None:
        """Initialize the switch."""
        self._api = api
        self._fan = fan
        self._attr_unique_id = f"{fan.unique_id}_light_sleep"
        self._current_state = state
        self._attr_is_on = state.sleep

        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._fan.unique_id)},
            "name": self._fan.name,
            "manufacturer": "Panasonic",
            "model": self._fan.product_code,
            "serial_number": self._fan.serial_number,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Put the light into sleep mode."""
        await self._set_sleep(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Return the light to normal mode."""
        await self._set_sleep(False)

    async def _set_sleep(self, sleep: bool) -> None:
        """Change the mode, leaving every other light setting as it is."""
        current = self._current_state
        state = LightState(
            is_on=current.is_on,
            brightness=current.brightness,
            color_temp=current.color_temp,
            sleep=sleep,
            sleep_brightness=current.sleep_brightness,
        )

        _LOGGER.debug("Setting sleep mode on %s to %s", self._fan.name, sleep)
        await self._api.set_light_state(self._fan, state)

        self._current_state = state
        self._attr_is_on = sleep
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch the light's mode from the cloud."""
        try:
            state = await self._api.get_state_for_fan(self._fan)
        except Exception as err:  # noqa: BLE001 - a failed poll must not raise
            _LOGGER.error("Error updating %s sleep mode: %s", self._fan.name, err)
            return

        if state.light is None:
            return

        self._current_state = state.light
        self._attr_is_on = state.light.sleep
