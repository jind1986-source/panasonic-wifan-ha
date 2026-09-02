"""Tests for the shared state store."""

import pytest

from _component import load

store_module, types_ = load("store", "types")
StateStore = store_module.StateStore
FanState = types_.FanState
LightState = types_.LightState
DeviceState = types_.DeviceState

FAN = types_.Fan(
    appliance_id="abc123",
    com_id="FM12GC",
    hashed_guid="g",
    name="Master",
    product_code="F-M12GC",
    serial_number="SN1",
)
OTHER = types_.Fan(
    appliance_id="def456",
    com_id="FM12GC",
    hashed_guid="g",
    name="Study",
    product_code="F-M12GC",
    serial_number="SN2",
)

FAN_STATE = FanState(is_on=True, speed=6, reverse=False, yuragi=True)


def make_store():
    return StateStore(
        {
            FAN.unique_id: DeviceState(
                fan=FAN_STATE,
                light=LightState(is_on=False, brightness=80, sleep=False),
            )
        }
    )


def test_a_light_change_is_visible_to_every_reader():
    """The switch's change must be seen by the light entity."""
    store = make_store()
    store.set_light(FAN, LightState(is_on=False, brightness=80, sleep=True))
    assert store.light(FAN).sleep is True


def test_setting_the_light_keeps_the_fan_state():
    store = make_store()
    store.set_light(FAN, LightState(is_on=True, brightness=10, sleep=True))
    assert store.fan_state(FAN) == FAN_STATE


def test_an_unknown_appliance_has_no_state():
    store = make_store()
    assert store.light(OTHER) is None
    assert store.device(OTHER) is None
    assert OTHER not in store


def test_setting_a_light_for_an_unknown_appliance_is_refused():
    store = make_store()
    with pytest.raises(KeyError):
        store.set_light(OTHER, LightState(is_on=True, brightness=10))


def test_a_device_state_can_be_replaced_wholesale():
    store = make_store()
    fresh = DeviceState(
        fan=FanState(is_on=False, speed=1, reverse=True, yuragi=False),
        light=LightState(is_on=True, brightness=5),
    )
    store.set_device(FAN, fresh)
    assert store.device(FAN) is fresh
    assert len(store) == 1


def test_an_empty_store_holds_nothing():
    assert len(StateStore()) == 0
