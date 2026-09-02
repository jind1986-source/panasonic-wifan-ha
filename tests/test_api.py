"""Tests for packet building and parsing.

The expected command strings below are the exact bytes the integration sent
before the codec refactor, so a change in output shows up as a failure here.
"""

import pytest

from _component import load

api, types_, packet = load("api", "types", "packet")
FanState = types_.FanState
LightState = types_.LightState

OFF_COMMAND = "060093014200FD010400FC013000FE01400080013100FA043140FFFF"
ON_COMMAND_SPEED_2 = (
    "090093014200FD010400FC013000FE014000800130"
    "00F0013200F1014100F2013100F804FF31FFFF"
)
ON_COMMAND_SPEED_10_REVERSE_YURAGI = (
    "090093014200FD010400FC013000FE014000800130"
    "00F0013A00F1014200F2013000F804FF31FFFF"
)
REAL_RESPONSE = (
    "0A0080013000F0013100F1014100F2013100F8043131000000F902000000FA0431"
    "40000000FB02000000862E2A0000FE010000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000880142"
)


def test_off_command_matches_the_original_bytes():
    state = FanState(is_on=False, speed=1, reverse=False, yuragi=False)
    assert api.make_command_packet(state) == OFF_COMMAND


def test_on_command_matches_the_original_bytes():
    state = FanState(is_on=True, speed=2, reverse=False, yuragi=False)
    assert api.make_command_packet(state) == ON_COMMAND_SPEED_2


def test_top_speed_reverse_and_yuragi_match_the_original_bytes():
    state = FanState(is_on=True, speed=10, reverse=True, yuragi=True)
    assert api.make_command_packet(state) == ON_COMMAND_SPEED_10_REVERSE_YURAGI


@pytest.mark.parametrize("speed", [0, 11, -1])
def test_out_of_range_speed_is_rejected(speed):
    state = FanState(is_on=True, speed=speed, reverse=False, yuragi=False)
    with pytest.raises(ValueError, match="Speed must be"):
        api.make_command_packet(state)


def test_query_packet_asks_for_the_fan_and_light_fields():
    assert api.QUERY_PACKET == (
        "0C00800000F00000F10000F20000F80000F90000FA0000FB00008600008800"
        "00F30000F500"
    )


def test_query_packet_keeps_the_original_fan_fields_first():
    original = "0A00800000F00000F10000F20000F80000F90000FA0000FB00008600008800"
    assert api.QUERY_PACKET[2:].startswith(original[2:])


def test_decode_real_response():
    state = api.decode_get_state_packet(REAL_RESPONSE)
    assert state.fan == FanState(is_on=True, speed=1, reverse=False, yuragi=False)
    assert state.light is None


def test_decode_reads_off_reverse_and_yuragi():
    response = packet.encode(
        [
            packet.Field(id=0x0080, value=bytes([0x31])),
            packet.Field(id=0x00F0, value=bytes([0x3A])),
            packet.Field(id=0x00F1, value=bytes([0x42])),
            packet.Field(id=0x00F2, value=bytes([0x30])),
        ]
    )
    state = api.decode_get_state_packet(response)
    assert state.fan == FanState(is_on=False, speed=10, reverse=True, yuragi=True)


def test_decode_rejects_a_response_missing_the_speed_field():
    response = packet.encode([packet.Field(id=0x0080, value=bytes([0x30]))])
    with pytest.raises(packet.PacketError, match="no speed field"):
        api.decode_get_state_packet(response)


def test_decode_rejects_an_unknown_power_value():
    response = packet.encode([packet.Field(id=0x0080, value=bytes([0x41]))])
    with pytest.raises(packet.PacketError, match="Unknown power value"):
        api.decode_get_state_packet(response)


def test_light_command_turns_the_light_on_at_a_brightness():
    command = api.make_light_command_packet(LightState(is_on=True, brightness=58))
    fields = packet.as_dict(packet.decode(command))
    assert fields[0x00F3].value == bytes([0x30])
    assert fields[0x00F5].value == bytes([0x3A])


def test_light_off_command_omits_brightness():
    command = api.make_light_command_packet(LightState(is_on=False, brightness=58))
    fields = packet.as_dict(packet.decode(command))
    assert fields[0x00F3].value == bytes([0x31])
    assert 0x00F5 not in fields


def test_light_command_carries_the_same_header_as_a_fan_command():
    command = api.make_light_command_packet(LightState(is_on=True, brightness=100))
    fields = packet.as_dict(packet.decode(command))
    assert fields[0x0093].value == bytes([0x42])
    assert fields[0x00FD].value == bytes([0x04])
    assert fields[0x00FC].value == bytes([0x30])
    assert fields[0x00FE].value == bytes([0x40])


def test_light_command_does_not_touch_the_fan_fields():
    command = api.make_light_command_packet(LightState(is_on=True, brightness=100))
    fields = packet.as_dict(packet.decode(command))
    for fan_field in (0x0080, 0x00F0, 0x00F1, 0x00F2):
        assert fan_field not in fields


@pytest.mark.parametrize("brightness", [0, -1, 101, 255])
def test_light_command_rejects_out_of_range_brightness(brightness):
    with pytest.raises(ValueError, match="Brightness must be"):
        api.make_light_command_packet(LightState(is_on=True, brightness=brightness))


@pytest.mark.parametrize(
    "byte,expected",
    [(0x64, 100), (0x3A, 58), (0x2F, 47), (0x01, 1)],
)
def test_brightness_is_read_as_a_percentage(byte, expected):
    """0x64/0x3A were observed on a real F-M12GC at 100% and 58%."""
    response = packet.encode(
        [
            packet.Field(id=0x0080, value=bytes([0x30])),
            packet.Field(id=0x00F0, value=bytes([0x33])),
            packet.Field(id=0x00F1, value=bytes([0x41])),
            packet.Field(id=0x00F2, value=bytes([0x31])),
            packet.Field(id=0x00F3, value=bytes([0x30])),
            packet.Field(id=0x00F5, value=bytes([byte])),
        ]
    )
    assert api.decode_get_state_packet(response).light == LightState(
        is_on=True, brightness=expected
    )


def test_light_off_is_read_from_the_power_field():
    response = packet.encode(
        [
            packet.Field(id=0x0080, value=bytes([0x30])),
            packet.Field(id=0x00F0, value=bytes([0x33])),
            packet.Field(id=0x00F1, value=bytes([0x41])),
            packet.Field(id=0x00F2, value=bytes([0x31])),
            packet.Field(id=0x00F3, value=bytes([0x31])),
            packet.Field(id=0x00F5, value=bytes([0x64])),
        ]
    )
    assert api.decode_get_state_packet(response).light == LightState(
        is_on=False, brightness=100
    )


def test_a_device_reporting_no_light_field_has_no_light():
    response = packet.encode(
        [
            packet.Field(id=0x0080, value=bytes([0x30])),
            packet.Field(id=0x00F0, value=bytes([0x33])),
            packet.Field(id=0x00F1, value=bytes([0x41])),
            packet.Field(id=0x00F2, value=bytes([0x31])),
        ]
    )
    assert api.decode_get_state_packet(response).light is None


def test_light_ids_are_included_in_the_query():
    assert api._query_ids()[-2:] == (0x00F3, 0x00F5)
