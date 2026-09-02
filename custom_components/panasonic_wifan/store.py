"""The last known state of each appliance, shared across the platforms.

A light command carries the whole light group, so every command has to be built
from the light's current settings. Each entity keeping its own copy means one
entity's change is invisible to the others until the next poll: switching sleep
mode on and then turning the light on would send the mode back to normal,
because the light entity never saw the switch's change.

They therefore read and write one store per config entry instead.
"""

from __future__ import annotations

from dataclasses import replace

from .types import DeviceState, Fan, FanState, LightState


class StateStore:
    """Shared, in-memory state for the appliances of one config entry."""

    def __init__(self, states: dict[str, DeviceState] | None = None) -> None:
        self._states: dict[str, DeviceState] = dict(states or {})

    def device(self, fan: Fan) -> DeviceState | None:
        return self._states.get(fan.unique_id)

    def light(self, fan: Fan) -> LightState | None:
        device = self.device(fan)
        return device.light if device else None

    def fan_state(self, fan: Fan) -> FanState | None:
        device = self.device(fan)
        return device.fan if device else None

    def set_device(self, fan: Fan, state: DeviceState) -> None:
        self._states[fan.unique_id] = state

    def set_light(self, fan: Fan, light: LightState) -> None:
        """Record a new light state, keeping the fan's."""
        if (device := self.device(fan)) is None:
            raise KeyError(f"No state stored for {fan.name}")
        self._states[fan.unique_id] = replace(device, light=light)

    def __contains__(self, fan: Fan) -> bool:
        return fan.unique_id in self._states

    def __len__(self) -> int:
        return len(self._states)
