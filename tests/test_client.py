"""Tests for ApiClient's handling of deviceControls responses.

The endpoint answers with JSON null when no control is queued, which is what a
freshly polled account returns.
"""

import asyncio

import pytest

from _component import load

api, types_ = load("api", "types")

FAN = types_.Fan(
    appliance_id="abc123",
    com_id="FM12GC",
    hashed_guid="g",
    name="Master",
    product_code="F-M12GC",
    serial_number="SN1",
)

STATE_PACKET = "040080013000F0013300F1014100F20131"


def make_client(responses):
    """An ApiClient whose HTTP layer replays the given responses in order."""
    client = api.ApiClient.__new__(api.ApiClient)
    remaining = list(responses)

    async def fake_request(method, url, **kwargs):
        return remaining.pop(0) if remaining else None

    client._request = fake_request
    return client


def control(completed_at, packet=STATE_PACKET, result="success_response"):
    return {
        "method": "GET",
        "appliance_id": FAN.unique_id,
        "status": "complete",
        "result": result,
        "completed_at": completed_at,
        "packet": packet,
    }


@pytest.mark.parametrize("response", [None, {}, {"controls": None}, {"controls": []}])
def test_latest_control_survives_an_empty_response(response):
    client = make_client([response])
    assert asyncio.run(client._latest_control(FAN)) is None


def test_latest_control_picks_the_newest_record():
    client = make_client(
        [{"controls": [control("20260101000000+0000"), control("20260202000000+0000")]}]
    )
    found = asyncio.run(client._latest_control(FAN))
    assert found["completed_at"] == "20260202000000+0000"


def test_latest_control_ignores_other_appliances():
    other = control("20260202000000+0000") | {"appliance_id": "someone-else"}
    client = make_client([{"controls": [other]}])
    assert asyncio.run(client._latest_control(FAN)) is None


def test_query_raw_returns_a_fresh_control(monkeypatch):
    monkeypatch.setattr(api, "SLEEP_AFTER_QUERY", 0)
    client = make_client(
        [
            None,  # nothing queued yet
            {"accepted": True},  # the POST
            {"controls": [control("20260202000000+0000")]},
        ]
    )
    found = asyncio.run(client.query_raw(FAN, [0x0080], attempts=1, delay=0))
    assert found["packet"] == STATE_PACKET


def test_query_raw_gives_up_when_no_newer_control_arrives():
    client = make_client(
        [
            {"controls": [control("20260202000000+0000")]},
            {"accepted": True},
            {"controls": [control("20260202000000+0000")]},
        ]
    )
    assert asyncio.run(client.query_raw(FAN, [0x0080], attempts=1, delay=0)) is None


def test_get_state_for_fans_survives_an_empty_response(monkeypatch):
    monkeypatch.setattr(api, "SLEEP_AFTER_QUERY", 0)
    client = make_client([{"accepted": True}, None])
    assert asyncio.run(client.get_state_for_fans([FAN])) == {}


def test_get_state_for_fans_decodes_a_control(monkeypatch):
    monkeypatch.setattr(api, "SLEEP_AFTER_QUERY", 0)
    client = make_client(
        [{"accepted": True}, {"controls": [control("20260202000000+0000")]}]
    )
    states = asyncio.run(client.get_state_for_fans([FAN]))
    assert states[FAN.unique_id].fan.speed == 3


def test_state_arriving_on_a_later_attempt_is_still_returned():
    """The bug that left lights missing: one read was not enough."""
    client = make_client(
        [
            {"accepted": True},  # the POST
            None,  # nothing yet
            {"controls": []},  # still nothing
            {"controls": [control("20260202000000+0000")]},
        ]
    )
    states = asyncio.run(
        client.get_state_for_fans([FAN], attempts=4, delay=0)
    )
    assert states[FAN.unique_id].fan.speed == 3


def test_state_polling_gives_up_after_the_last_attempt():
    client = make_client([{"accepted": True}, None, None])
    assert asyncio.run(client.get_state_for_fans([FAN], attempts=2, delay=0)) == {}


def test_state_polling_stops_early_once_every_fan_has_answered():
    client = make_client(
        [{"accepted": True}, {"controls": [control("20260202000000+0000")]}]
    )
    states = asyncio.run(client.get_state_for_fans([FAN], attempts=5, delay=0))
    assert len(states) == 1


def test_state_polling_ignores_a_failed_control():
    failed = control("20260202000000+0000", result="error_response")
    client = make_client([{"accepted": True}, {"controls": [failed]}])
    assert asyncio.run(client.get_state_for_fans([FAN], attempts=1, delay=0)) == {}
