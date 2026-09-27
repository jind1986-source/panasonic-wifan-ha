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
from .debounce import CommandDebouncer
from .types import Fan, LightState

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)

# Read back this long after a light command sends. Shared with light.py's
# constant and reason: it lands inside the store's SETTLE (5 s), but it is
# the deliberate, authoritative read the wait promises rather than an
# incidental poll, so it is applied regardless (see _read_back /
# StateStore.read_light_back). Kept as its own constant per module, per the
# repo's convention.
REFRESH_AFTER_COMMAND = 2  # seconds


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
    store = data["store"]
    debounce = data["debounce"]
    states = data.get("states") or {}

    entities = []
    for fan in fans:
        state = states.get(fan.unique_id)
        if state is None or state.light is None:
            continue
        entities.append(PanasonicWiFiSleepSwitch(api, fan, store, debounce))

    _LOGGER.debug("Adding %s sleep mode switch(es)", len(entities))
    async_add_entities(entities)


class PanasonicWiFiSleepSwitch(SwitchEntity):  # type: ignore[misc]
    """The light's sleep mode, as a switch Apple Home can see."""

    _attr_icon = "mdi:weather-night"
    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_name = "Light sleep mode"

    def __init__(self, api, fan: Fan, store, debounce: CommandDebouncer) -> None:
        """Initialize the switch."""
        self._api = api
        self._fan = fan
        self._store = store
        self._debounce = debounce
        # Shared with the light entity: both drive the one light group, so
        # they share the one wait per appliance for it.
        self._key = (fan.unique_id, "light")
        self._attr_unique_id = f"{fan.unique_id}_light_sleep"
        self._attr_is_on = store.light(fan).sleep

        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._fan.unique_id)},
            "name": self._fan.name,
            "manufacturer": "Panasonic",
            "model": self._fan.product_code,
            "serial_number": self._fan.serial_number,
        }

        self._debounce.add_listener(self._key, self._read_back)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Put the light into sleep mode."""
        await self._set_sleep(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Return the light to normal mode."""
        await self._set_sleep(False)

    async def _set_sleep(self, sleep: bool) -> None:
        """Show the mode immediately, and (re)start the shared light wait.

        Leaves every other light setting as it is. The actual send is
        deferred: this shares one wait per appliance with the light entity
        (key ``(fan.unique_id, "light")``), so a change made by either of
        them inside it restarts the wait rather than sending a second
        command. ``_send_command`` builds the packet from the store when the
        wait finally settles, so a brightness change and a sleep toggle
        inside it go out together.
        """
        current = self._store.light(self._fan)
        state = LightState(
            is_on=current.is_on,
            brightness=current.brightness,
            color_temp=current.color_temp,
            sleep=sleep,
            sleep_brightness=current.sleep_brightness,
        )

        _LOGGER.debug("Queuing sleep mode for %s: sleep=%s", self._fan.name, sleep)
        self._store.set_light(self._fan, state)
        self._attr_is_on = sleep
        self.async_write_ha_state()

        self._debounce.trigger(
            self._key, self._send_command, after=REFRESH_AFTER_COMMAND
        )

    async def _send_command(self) -> None:
        """Send the merged light state once the wait has settled.

        Same shape as ``PanasonicWiFiLight._send_command``: whichever
        entity's ``trigger`` call fires last provides the action that
        actually runs, and it reads the merged state from the store rather
        than the value captured when it was queued, so a change made by the
        other entity inside the same wait is included too.

        A failed send is logged and swallowed here, rather than left to
        propagate: the wait helper still runs the read-back after this
        either way, and that is what puts Home Assistant back in step with
        the appliance.
        """
        self._store.forget_light_read(self._fan)
        state = self._store.light(self._fan)
        _LOGGER.debug(
            "Sending sleep mode state for %s: sleep=%s", self._fan.name, state.sleep
        )
        try:
            await self._api.set_light_state(self._fan, state)
        except Exception as err:  # noqa: BLE001 - a failed send must still be read back
            _LOGGER.error("Error sending sleep mode for %s: %s", self._fan.name, err)
            return

        self._store.record_command(self._fan, state)

    async def _read_back(self) -> None:
        """Refresh from the light's read-back, shared with the light entity.

        ``StateStore.read_light_back`` does the actual fetch at most once per
        command (decision 15 — the light entity's own ``_read_back`` calls it
        the same way); this just mirrors whatever it returns onto the switch,
        the same whether this call triggered the fetch or found it already
        answered.
        """

        async def fetch() -> LightState | None:
            state = await self._api.get_state_for_fan(self._fan, max_age=0)
            return state.light

        try:
            state = await self._store.read_light_back(self._fan, fetch)
        except Exception as err:  # noqa: BLE001 - a failed read-back must not raise
            _LOGGER.error(
                "Error reading %s back after a sleep-mode command: %s",
                self._fan.name,
                err,
            )
            return

        if state is None:
            return

        self._attr_is_on = state.sleep
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch the light's mode from the cloud.

        A poll that lands while a change is still waiting to send is ignored
        outright; one that lands within SETTLE of the send is dropped by
        ``record_poll``: either way it may describe the state from before
        the change.
        """
        if self._debounce.is_waiting(self._key):
            _LOGGER.debug(
                "Ignoring a poll for %s while a change is waiting to send",
                self._fan.name,
            )
            return

        try:
            state = await self._api.get_state_for_fan(self._fan)
        except Exception as err:  # noqa: BLE001 - a failed poll must not raise
            _LOGGER.error("Error updating %s sleep mode: %s", self._fan.name, err)
            return

        if state.light is None:
            return

        if not self._store.record_poll(self._fan, state.light):
            _LOGGER.debug(
                "Ignoring a read for %s that may predate the last command",
                self._fan.name,
            )
            return
        self._attr_is_on = state.light.sleep
