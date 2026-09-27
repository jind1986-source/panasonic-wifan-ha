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
from time import monotonic

from .types import DeviceState, Fan, FanState, LightState

# An appliance takes a moment to report a change back through the cloud, so a
# read that arrives immediately after a command can still describe the state
# before it. Within this window a command is trusted over a read.
SETTLE = 5  # seconds


class StateStore:
    """Shared, in-memory state for the appliances of one config entry."""

    def __init__(self, states: dict[str, DeviceState] | None = None) -> None:
        self._states: dict[str, DeviceState] = dict(states or {})
        self._commanded_at: dict[str, float] = {}
        # The light read-back already fetched for a fan, if any. The light
        # entity and the sleep switch both want it applied; caching it here
        # means only the first of them pays for the fresh cloud read, and the
        # second gets the same answer instead of a second one (decision 15).
        # forget_light_read drops it again once the next command is about to
        # send, so the following read-back fetches afresh.
        self._light_read: dict[str, LightState] = {}

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

    def record_command(self, fan: Fan, light: LightState) -> None:
        """Record a light state that was just commanded."""
        self.set_light(fan, light)
        self._commanded_at[fan.unique_id] = monotonic()

    def record_poll(self, fan: Fan, light: LightState) -> bool:
        """Record a light state that was read back, unless it may be stale.

        Returns whether it was recorded. A read arriving within SETTLE of a
        command is dropped: the appliance may not have reported the change yet,
        and taking it would undo what was just asked for.
        """
        commanded_at = self._commanded_at.get(fan.unique_id)
        if commanded_at is not None and monotonic() - commanded_at < SETTLE:
            return False

        self.set_light(fan, light)
        return True

    async def read_light_back(self, fan: Fan, read) -> LightState | None:
        """Apply the light's read-back, fetching it at most once.

        ``read`` is the caller's own fresh fetch (an async callable taking no
        arguments, e.g. ``lambda: api.get_state_for_fan(fan, max_age=0)``
        reduced to its light). The first call for a fan runs it and records
        the result directly, bypassing SETTLE — a deliberate read-back is
        exempt from it, unlike an incidental poll. A second call for the same
        fan, before ``forget_light_read`` clears it, gets that same result
        instead of triggering its own fetch.
        """
        if fan.unique_id not in self._light_read:
            state = await read()
            if state is None:
                return None
            self._light_read[fan.unique_id] = state
            self.set_light(fan, state)

        return self._light_read[fan.unique_id]

    def forget_light_read(self, fan: Fan) -> None:
        """Drop a cached read-back result so the next one is fetched fresh.

        Called right before a light command sends, so the read-back that
        follows always answers for the command that just went out rather
        than reusing a result cached for a previous one.
        """
        self._light_read.pop(fan.unique_id, None)

    def __contains__(self, fan: Fan) -> bool:
        return fan.unique_id in self._states

    def __len__(self) -> int:
        return len(self._states)
