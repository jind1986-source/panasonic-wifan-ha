#!/usr/bin/env python3
"""Send one light command to a fan and report what changed.

Unlike the other scripts this one WRITES to an appliance: it is the check that
a light command built from the discovered field ids is actually accepted.

    python3 scripts/try_light.py --device 1 --on --brightness 40
    python3 scripts/try_light.py --device 1 --off

It prints the packet before sending, reads the light's state before and after,
and says whether the device reported the change.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _component import load, use_threaded_dns  # noqa: E402

api, const, types_ = load("api", "const", "types")


def pick(fans, wanted):
    if wanted is None:
        return fans[0]
    if wanted.isdigit() and int(wanted) < len(fans):
        return fans[int(wanted)]
    matches = [f for f in fans if f.name.lower() == wanted.lower()]
    return matches[0] if matches else None


async def run(args: argparse.Namespace) -> int:
    username = args.username or os.environ.get("PANASONIC_USERNAME")
    password = args.password or os.environ.get("PANASONIC_PASSWORD")
    if not username:
        username = input("Panasonic account email: ").strip()
    if not password:
        password = getpass.getpass("Panasonic account password: ")

    use_threaded_dns()
    client = api.ApiClient(username, password)
    try:
        fans = await client.get_devices()
        if not fans:
            print("No devices on this account.")
            return 1

        fan = pick(fans, args.device)
        if fan is None:
            print(f"No device called {args.device!r}")
            return 1

        before = await client.get_state_for_fan(fan)
        print(f"{fan.name}")
        print(f"  light before : {before.light}")
        print(f"  fan before   : {before.fan}")

        brightness = args.brightness
        if brightness is None:
            brightness = (
                before.light.brightness if before.light else const.MAX_BRIGHTNESS
            )

        companions = before.light.companions if before.light else ()
        state = types_.LightState(
            is_on=args.on, brightness=brightness, companions=companions
        )
        command = api.make_light_command_packet(state)
        print(f"\n  sending      : {state}")
        print(f"  packet       : {command}")

        await client.set_light_state(fan, state)

        after = await client.get_state_for_fan(fan)
        print(f"\n  light after  : {after.light}")
        print(f"  fan after    : {after.fan}")

        if after.light is None:
            print("\n  The device stopped reporting light state.")
            return 1
        if after.light.is_on != state.is_on:
            print("\n  The light did not switch. The command was not accepted.")
            return 1
        if state.is_on and after.light.brightness != state.brightness:
            print(
                f"\n  Switched, but brightness came back as "
                f"{after.light.brightness} rather than {state.brightness}."
            )
            return 1

        print("\n  Accepted: the device reports the state that was asked for.")
        if before.fan != after.fan:
            print("  Note: the fan state also changed, which it should not have.")
            return 1
        return 0
    finally:
        await client.session.close()
        await client.auth.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--device", help="device index or name (default: the first)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", action="store_true", help="switch the light on")
    group.add_argument("--off", action="store_true", help="switch the light off")
    parser.add_argument(
        "--brightness",
        type=int,
        help=f"{const.MIN_BRIGHTNESS}-{const.MAX_BRIGHTNESS} percent "
        "(default: leave as it is)",
    )
    args = parser.parse_args()
    args.on = not args.off
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
