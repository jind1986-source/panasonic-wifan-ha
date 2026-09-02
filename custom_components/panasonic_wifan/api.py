"""
API Client for interacting with the Panasonic WiFan cloud service.
"""

import aiohttp
import asyncio
from datetime import datetime as dt, timezone
import logging
from typing import Literal, Sequence

from . import packet
from .auth import PanasonicGLBAuthClient
from .const import (
    CMD_HEADER,
    DIGIT_HIGH_NIBBLE,
    DIRECTION_FORWARD_LOW,
    DIRECTION_HIGH_NIBBLE,
    DIRECTION_REVERSE_LOW,
    ID_LIGHT_BRIGHTNESS,
    ID_LIGHT_POWER,
    ID_OFF_TIMER,
    ID_POWER,
    ID_SPEED,
    ID_TIMER,
    ID_DIRECTION,
    ID_YURAGI,
    MAX_BRIGHTNESS,
    MAX_SPEED,
    MIN_BRIGHTNESS,
    MIN_SPEED,
    POWER_OFF,
    POWER_ON,
    QUERY_IDS,
    YURAGI_OFF,
    YURAGI_ON,
)
from .packet import Field, PacketError
from .types import DeviceState, Fan, FanState, LightState

BASE_URL = "https://prod.mycfan.pgtls.net/v1/mycfan/user"
DEVICE_CONTROLS_URL = "https://prod.mycfan.pgtls.net/v1/mycfan/deviceControls"
API_KEY = "rZLwuRtU0nFb20Mh6LShL6uY3fZ5tBlarz4ONmdl"
OAUTH_CLIENT_ID = "8k1QeEXDxt3qGgYOvDY7NmZLfl60YfNi"

# Trailing fields the app sends with every command. They look like timer
# slots; 0xFF appears to mean "leave alone".
ON_TRAILER = Field(id=ID_TIMER, value=bytes.fromhex("FF31FFFF"))
OFF_TRAILER = Field(id=ID_OFF_TIMER, value=bytes.fromhex("3140FFFF"))

SLEEP_AFTER_QUERY = 2  # seconds
GET = "GET"
SET = "SET"

_LOGGER = logging.getLogger(__name__)


def _query_ids() -> tuple[int, ...]:
    """Field ids to poll, including the light's if they are known."""
    ids = list(QUERY_IDS)
    ids.extend(i for i in (ID_LIGHT_POWER, ID_LIGHT_BRIGHTNESS) if i is not None)
    return tuple(ids)


QUERY_PACKET = packet.query(_query_ids())


class ApiClient:
    def __init__(self, username: str, password: str):
        self.auth = PanasonicGLBAuthClient(username, password)
        self.session = aiohttp.ClientSession()

    async def _request(self, method: str, url: str, **kwargs):
        """Helper method to make API requests with common headers and error handling."""
        headers = {
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "authorization": await self.auth.get_access_token(),
            "x-timestamp": get_timestamp(),
        }

        resp = await self.session.request(method, url, headers=headers, **kwargs)
        resp.raise_for_status()
        return await resp.json()

    async def get_devices(self) -> list[Fan]:
        data = await self._request("GET", f"{BASE_URL}/devices")
        return [Fan.from_api(item) for item in data.get("devices", [])]

    async def get_state_for_fans(self, fans: list[Fan]) -> dict[str, DeviceState]:
        fans_by_id = {fan.unique_id: fan for fan in fans}

        for fan in fans:
            await self._request(
                "POST",
                DEVICE_CONTROLS_URL,
                json={
                    "appliance_id": fan.unique_id,
                    "method": GET,
                    "packet": QUERY_PACKET,
                },
            )

        # Wait a bit to allow cloud to process the responses
        await asyncio.sleep(SLEEP_AFTER_QUERY)

        data = await self._request("GET", DEVICE_CONTROLS_URL)

        """
        Example response data:
        {   
            "controls": [
                {
                    "accepted_id": "xxx",
                    "accepted_at": "20251117054743+0000",
                    "appliance_id": "xxx",
                    "method": "GET",
                    "status": "complete",
                    "completed_at": "20251117054744+0000",
                    "result": "success_response",
                    "reason": "200",
                    "packet": "0A0080013000F0013200F1014100F20...",
                },
            ]
        }
        """

        _LOGGER.debug("Fetched deviceControls: %s", data)

        device_states: dict[str, DeviceState] = {}
        controls = sorted(
            _controls(data), key=lambda x: x.get("completed_at", ""), reverse=True
        )
        if controls:
            for control in controls:
                if control["method"] != GET:
                    continue
                if control.get("status") != "complete":
                    continue
                if control.get("result") != "success_response":
                    continue
                if (fan := fans_by_id.get(control["appliance_id"])) is None:
                    continue
                if fan.unique_id in device_states:
                    continue

                state = decode_get_state_packet(control["packet"])
                device_states[fan.unique_id] = state
                _LOGGER.debug(
                    "Fetched state for %s: is_on=%s, speed=%s, reverse=%s, "
                    "yuragi=%s, light=%s",
                    fan.name,
                    state.fan.is_on,
                    state.fan.speed,
                    state.fan.reverse,
                    state.fan.yuragi,
                    state.light,
                )

        return device_states

    async def get_state_for_fan(self, fan: Fan) -> DeviceState:
        states = await self.get_state_for_fans([fan])
        return states[fan.unique_id]

    async def _latest_control(self, fan: Fan) -> dict | None:
        """The most recently completed GET response for one appliance."""
        data = await self._request("GET", DEVICE_CONTROLS_URL)
        controls = [
            control
            for control in _controls(data)
            if control.get("method") == GET
            and control.get("appliance_id") == fan.unique_id
            and control.get("status") == "complete"
        ]
        if not controls:
            return None
        return max(controls, key=lambda control: control.get("completed_at", ""))

    async def controls(self) -> list[dict]:
        """Every control record the cloud reports for this account.

        Includes commands issued by other clients, Panasonic's own app among
        them, which is how the app's packets can be observed rather than
        guessed at.
        """
        data = await self._request("GET", DEVICE_CONTROLS_URL)
        return _controls(data)

    async def newest_completed_at(self, fan: Fan) -> str:
        """Timestamp of the most recent completed response, or an empty string."""
        control = await self._latest_control(fan)
        return control.get("completed_at", "") if control else ""

    async def request_fields(self, fan: Fan, ids: Sequence[int]) -> None:
        """Ask a device for fields without waiting for its reply.

        The cloud queues each request separately, so several can be in flight at
        once and collected together with recent_controls().
        """
        await self._post_device_controls(fan, GET, packet.query(ids))

    async def recent_controls(self, fan: Fan, since: str = "") -> list[dict]:
        """Completed responses for one appliance newer than the given timestamp."""
        data = await self._request("GET", DEVICE_CONTROLS_URL)
        controls = [
            control
            for control in _controls(data)
            if control.get("method") == GET
            and control.get("appliance_id") == fan.unique_id
            and control.get("status") == "complete"
            and control.get("completed_at", "") > since
        ]
        return sorted(controls, key=lambda control: control.get("completed_at", ""))

    async def query_raw(
        self,
        fan: Fan,
        ids: Sequence[int],
        *,
        attempts: int = 5,
        delay: float = SLEEP_AFTER_QUERY,
    ) -> dict | None:
        """Ask a device for arbitrary fields and return the raw control record.

        Used by the field discovery scripts. Returns None if no fresh response
        arrives, and the record itself — including a failed ``result`` — when
        the device rejects the query.
        """
        previous = await self._latest_control(fan)
        since = previous.get("completed_at", "") if previous else ""

        await self._post_device_controls(fan, GET, packet.query(ids))

        for _ in range(attempts):
            await asyncio.sleep(delay)
            control = await self._latest_control(fan)
            if control and control.get("completed_at", "") > since:
                return control

        return None

    async def set_state(self, fan: Fan, state: FanState):
        await self._post_device_controls(fan, SET, make_command_packet(state))

    async def set_light_state(self, fan: Fan, state: LightState):
        await self._post_device_controls(fan, SET, make_light_command_packet(state))

    async def _post_device_controls(
        self, fan: Fan, method: Literal["GET", "SET"], payload: str
    ):
        data = await self._request(
            "POST",
            DEVICE_CONTROLS_URL,
            json={
                "appliance_id": fan.unique_id,
                "method": method,
                "packet": payload,
            },
        )
        _LOGGER.debug("deviceControls response: %s", data)


def _controls(data) -> list[dict]:
    """The control records in a deviceControls response.

    The endpoint answers with JSON null when nothing is queued, and omits the
    key in some replies, so neither can be assumed present.
    """
    if not isinstance(data, dict):
        return []
    return data.get("controls") or []


def get_timestamp():
    now = dt.now(timezone.utc)
    return now.strftime("%Y%m%d%H%M%S+0000")


def _header_fields() -> list[Field]:
    return [
        Field(id=field_id, value=bytes([value])) for field_id, value in CMD_HEADER
    ]


def _digit_field(field_id: int, value: int) -> Field:
    """A numeric setting, stored as one byte with 0x3 in the high nibble."""
    return Field(id=field_id, value=packet.nibble_byte(DIGIT_HIGH_NIBBLE, value))


def make_command_packet(state: FanState) -> str:
    if state.speed < MIN_SPEED or state.speed > MAX_SPEED:
        raise ValueError(f"Speed must be between {MIN_SPEED} and {MAX_SPEED}")

    fields = _header_fields()

    if not state.is_on:
        fields.append(_digit_field(ID_POWER, POWER_OFF))
        fields.append(OFF_TRAILER)
        return packet.encode(fields)

    fields.append(_digit_field(ID_POWER, POWER_ON))
    fields.append(_digit_field(ID_SPEED, state.speed))
    fields.append(
        Field(
            id=ID_DIRECTION,
            value=packet.nibble_byte(
                DIRECTION_HIGH_NIBBLE,
                DIRECTION_REVERSE_LOW if state.reverse else DIRECTION_FORWARD_LOW,
            ),
        )
    )
    fields.append(
        _digit_field(ID_YURAGI, YURAGI_ON if state.yuragi else YURAGI_OFF)
    )
    fields.append(ON_TRAILER)

    return packet.encode(fields)


def make_light_command_packet(state: LightState) -> str:
    """Build a SET packet for the light.

    Raises RuntimeError until the light's field ids are known; see
    ID_LIGHT_POWER in const.py.
    """
    if ID_LIGHT_POWER is None or ID_LIGHT_BRIGHTNESS is None:
        raise RuntimeError(
            "Light field ids are unknown for this integration; "
            "run scripts/sweep_ids.py to discover them"
        )
    if state.brightness < MIN_BRIGHTNESS or state.brightness > MAX_BRIGHTNESS:
        raise ValueError(
            f"Brightness must be between {MIN_BRIGHTNESS} and {MAX_BRIGHTNESS}"
        )

    fields = _header_fields()
    fields.append(
        _digit_field(ID_LIGHT_POWER, POWER_ON if state.is_on else POWER_OFF)
    )
    if state.is_on:
        # Brightness is a percentage byte, so it is written whole rather than
        # through the nibble encoding the fan settings use.
        fields.append(
            Field(id=ID_LIGHT_BRIGHTNESS, value=bytes([state.brightness]))
        )

    return packet.encode(fields)


def decode_get_state_packet(response: str) -> DeviceState:
    """
    Example packet value:
    0A0080013000F0013100F1014100F2013100F8043131000000F902000000FA04314
    0000000FB02000000862E2A0000FE01000000000000000000000000000000000000
    000000000000000000000000000000000000000000000000880142
    """

    fields = packet.as_dict(packet.decode(response))

    def require(field_id: int, name: str) -> Field:
        if (field := fields.get(field_id)) is None:
            raise PacketError(f"Packet has no {name} field ({field_id:#06x})")
        return field

    power = require(ID_POWER, "power")
    if power.high_nibble != DIGIT_HIGH_NIBBLE:
        raise PacketError(f"Unknown power value {power.byte:#04x}")
    if power.low_nibble == POWER_ON:
        is_on = True
    elif power.low_nibble == POWER_OFF:
        is_on = False
    else:
        raise PacketError(f"Unknown ON/OFF nibble {power.low_nibble:#x}")

    speed_field = require(ID_SPEED, "speed")
    if speed_field.high_nibble != DIGIT_HIGH_NIBBLE:
        raise PacketError(f"Unknown speed value {speed_field.byte:#04x}")
    speed = speed_field.low_nibble

    direction = require(ID_DIRECTION, "direction")
    if direction.high_nibble != DIRECTION_HIGH_NIBBLE:
        raise PacketError(f"Unknown direction value {direction.byte:#04x}")
    reverse = direction.low_nibble == DIRECTION_REVERSE_LOW

    yuragi_field = require(ID_YURAGI, "yuragi")
    if yuragi_field.high_nibble != DIGIT_HIGH_NIBBLE:
        raise PacketError(f"Unknown yuragi value {yuragi_field.byte:#04x}")
    yuragi = yuragi_field.low_nibble == YURAGI_ON

    return DeviceState(
        fan=FanState(
            is_on=is_on,
            speed=speed,
            reverse=reverse,
            yuragi=yuragi,
        ),
        light=_decode_light(fields),
    )


def _decode_light(fields: dict[int, Field]) -> LightState | None:
    """Read light state, if this device reports any."""
    if ID_LIGHT_POWER is None:
        return None
    if (power := fields.get(ID_LIGHT_POWER)) is None:
        return None

    brightness = MAX_BRIGHTNESS
    if ID_LIGHT_BRIGHTNESS is not None and (
        field := fields.get(ID_LIGHT_BRIGHTNESS)
    ):
        brightness = min(MAX_BRIGHTNESS, max(MIN_BRIGHTNESS, field.byte))

    return LightState(is_on=power.low_nibble == POWER_ON, brightness=brightness)
