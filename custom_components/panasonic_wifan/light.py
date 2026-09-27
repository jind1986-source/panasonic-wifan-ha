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
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import nearest_sleep_step
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
from .debounce import CommandDebouncer
from .types import Fan, LightState

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)

# Read back this long after a light command sends, once the wait's merged
# state has had a moment to reach the device: entering sleep mode lights the
# fitting whether or not the command said so, so optimistic state is checked
# against the device rather than trusted outright. It lands inside the
# store's SETTLE (5 s), but it is the deliberate, authoritative read the wait
# promises rather than an incidental poll, so it is applied regardless (see
# _read_back / StateStore.read_light_back). switch.py shares the same value
# and reason; kept as its own constant per module, per the repo's convention.
REFRESH_AFTER_COMMAND = 2  # seconds

# The light's two modes, exposed as effects. Sleep mode dims further than the
# normal range allows and keeps its own brightness, so it reads as a mode of
# the one light rather than as a separate entity.
EFFECT_NORMAL = "Normal"
EFFECT_SLEEP = "Sleep"


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
    store = data["store"]
    debounce = data["debounce"]
    states = data.get("states") or {}

    # Any device whose state did not arrive during setup is asked again here
    # rather than silently going without a light entity.
    unknown = [fan for fan in fans if fan.unique_id not in states]
    if unknown:
        _LOGGER.debug(
            "Re-reading state for %s", ", ".join(fan.name for fan in unknown)
        )
        try:
            fetched = await api.get_state_for_fans(unknown)
        except Exception as err:  # noqa: BLE001 - setup must not abort on one device
            _LOGGER.error("Could not read state while looking for lights: %s", err)
        else:
            states = {**states, **fetched}
            for fan in unknown:
                if (state := fetched.get(fan.unique_id)) is not None:
                    store.set_device(fan, state)

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
        entities.append(PanasonicWiFiLight(api, fan, store, debounce))

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
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [EFFECT_NORMAL, EFFECT_SLEEP]
    _attr_has_entity_name = True
    _attr_name = "Light"

    def __init__(self, api, fan: Fan, store, debounce: CommandDebouncer) -> None:
        """Initialize the light entity."""
        self._api = api
        self._fan = fan
        self._store = store
        self._debounce = debounce
        # Shared with the sleep switch: both entities drive the one light
        # group, so they share the one wait per appliance for it.
        self._key = (fan.unique_id, "light")
        self._attr_unique_id = f"{fan.unique_id}_light"

        self._apply(store.light(fan))

        # Same identifiers as the fan, so both entities sit on one device.
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._fan.unique_id)},
            "name": self._fan.name,
            "manufacturer": "Panasonic",
            "model": self._fan.product_code,
            "serial_number": self._fan.serial_number,
        }

        self._debounce.add_listener(self._key, self._read_back)

    @property
    def _current_state(self) -> LightState:
        """The light's settings, as last seen by any entity of this device.

        Read from the shared store rather than a copy of its own: the sleep
        switch changes the same light, and a command built from a stale copy
        would send the mode back.
        """
        return self._store.light(self._fan)

    def _apply(self, state: LightState, *, commanded: bool = False) -> None:
        """Mirror a light state onto the entity's attributes, via the store.

        Used for the initial state and for a manual poll (``async_update``);
        the store decides whether a poll is worth taking (``record_poll``'s
        SETTLE guard). The optimistic push and the read-back go straight to
        ``_mirror`` instead, since they update the store themselves.
        """
        if commanded:
            self._store.record_command(self._fan, state)
        elif not self._store.record_poll(self._fan, state):
            _LOGGER.debug(
                "Ignoring a read for %s that may predate the last command",
                self._fan.name,
            )
            return
        self._mirror(state)

    def _mirror(self, state: LightState) -> None:
        """Set the entity's attributes to match a light state."""
        self._attr_is_on = state.is_on
        self._attr_brightness = to_ha_brightness(state.active_brightness)
        self._attr_color_temp_kelvin = to_kelvin(state.color_temp)
        self._attr_effect = EFFECT_SLEEP if state.sleep else EFFECT_NORMAL

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, with brightness, colour temperature or mode."""
        current = self._current_state

        sleep = current.sleep
        if (effect := kwargs.get(ATTR_EFFECT)) is not None:
            sleep = effect == EFFECT_SLEEP

        color_temp = current.color_temp
        if (kelvin := kwargs.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
            color_temp = to_device_color_temp(kelvin)

        # Brightness belongs to whichever mode is active; the other mode keeps
        # the value the device is holding for it.
        brightness = current.brightness
        sleep_brightness = current.sleep_brightness
        if (requested := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            if sleep:
                # Sleep mode has three fixed steps, so a slider value has to
                # be snapped to the nearest; the device ignores anything else.
                sleep_brightness = nearest_sleep_step(
                    to_device_brightness(requested)
                )
            else:
                brightness = to_device_brightness(requested)

        await self._queue(
            LightState(
                is_on=True,
                brightness=brightness,
                color_temp=color_temp,
                sleep=sleep,
                sleep_brightness=sleep_brightness,
            )
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off, leaving its settings as they are."""
        current = self._current_state
        await self._queue(
            LightState(
                is_on=False,
                brightness=current.brightness,
                color_temp=current.color_temp,
                sleep=current.sleep,
                sleep_brightness=current.sleep_brightness,
            )
        )

    async def _queue(self, state: LightState) -> None:
        """Show a light state immediately, and (re)start its shared wait.

        The actual send is deferred: this shares one wait per appliance with
        the sleep switch (key ``(fan.unique_id, "light")``), so a change made
        by either of them inside it restarts the wait rather than sending a
        second command. ``_send_command`` builds the packet from the store
        when the wait finally settles, so it always carries the last of a
        burst from either entity.
        """
        _LOGGER.debug(
            "Queuing light state for %s: is_on=%s, brightness=%s, "
            "color_temp=%s, sleep=%s, sleep_brightness=%s",
            self._fan.name,
            state.is_on,
            state.brightness,
            state.color_temp,
            state.sleep,
            state.sleep_brightness,
        )
        self._store.set_light(self._fan, state)
        self._mirror(state)
        self.async_write_ha_state()

        self._debounce.trigger(
            self._key, self._send_command, after=REFRESH_AFTER_COMMAND
        )

    async def _send_command(self) -> None:
        """Send the merged light state once the wait has settled.

        Built from the store rather than the state captured when this call
        was queued, so a change made by the sleep switch — or another light
        change — inside the same wait is folded into the one packet that
        goes out.

        A failed send is logged and swallowed here, rather than left to
        propagate: the wait helper still runs the read-back after this
        either way (see ``debounce.CommandDebouncer``), and that is what
        puts Home Assistant back in step with the appliance.
        """
        self._store.forget_light_read(self._fan)
        state = self._store.light(self._fan)
        _LOGGER.debug(
            "Sending light state for %s: is_on=%s, brightness=%s, "
            "color_temp=%s, sleep=%s, sleep_brightness=%s",
            self._fan.name,
            state.is_on,
            state.brightness,
            state.color_temp,
            state.sleep,
            state.sleep_brightness,
        )
        try:
            await self._api.set_light_state(self._fan, state)
        except Exception as err:  # noqa: BLE001 - a failed send must still be read back
            _LOGGER.error("Error sending light state for %s: %s", self._fan.name, err)
            return

        self._store.record_command(self._fan, state)

    async def _read_back(self) -> None:
        """Refresh from the light's read-back, shared with the sleep switch.

        ``StateStore.read_light_back`` does the actual fetch at most once per
        command (decision 15 — the switch's own ``_read_back`` calls it the
        same way); this just mirrors whatever it returns onto the entity,
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
                "Error reading %s back after a light command: %s",
                self._fan.name,
                err,
            )
            return

        if state is None:
            _LOGGER.debug("%s stopped reporting light state", self._fan.name)
            return

        self._mirror(state)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Fetch the light's state from the cloud.

        This is called by Home Assistant when polling is enabled or when a
        manual refresh is requested. A poll that lands while a change is
        still waiting to send is ignored outright; one that lands within
        SETTLE of the send is dropped by ``record_poll`` inside ``_apply``:
        either way it may describe the state from before the change.
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
            _LOGGER.error("Error updating %s light: %s", self._fan.name, err)
            return

        if state.light is None:
            _LOGGER.debug("%s stopped reporting light state", self._fan.name)
            return

        self._apply(state.light)

        _LOGGER.debug(
            "Updated %s light: is_on=%s, brightness=%s, color_temp=%sK, mode=%s",
            self._fan.name,
            state.light.is_on,
            self._attr_brightness,
            self._attr_color_temp_kelvin,
            self._attr_effect,
        )
