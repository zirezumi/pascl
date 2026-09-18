"""The host-entity channel against a fake host: commands as service calls, the relayed state
as the answer, the colour from before the command as ``unapplied``, and the snapshot's state
put back afterwards."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from pascl.shell.ha_light import HALightChannel


class FakeHost:
    def __init__(self, state: str, attributes: dict[str, Any]) -> None:
        self.state = {"state": state, "attributes": attributes}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inbox: list[tuple[str, str]] = []
        self.watched: list[str] = []

    def watch_entity(self, entity_id: str) -> str:
        self.watched.append(entity_id)
        return f"ha/state/{entity_id}"

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        out, self.inbox = self.inbox, []
        yield from out

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        return self.state

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.calls.append((domain, service, data))

    def entity_registry(self, entity_id: str) -> dict[str, Any] | None:
        return {"unique_id": "matter-node-7-light", "device_id": "dev7"}

    def device_registry(self, device_id: str) -> dict[str, Any] | None:
        return {"sw_version": "2.4.1"}

    def relay(self, state: str, xy: tuple[float, float] | None, kelvin: int | None = None) -> None:
        attrs: dict[str, Any] = {"xy_color": list(xy)} if xy is not None else {}
        if kelvin is not None:
            attrs["color_temp_kelvin"] = kelvin
        self.inbox.append(
            ("ha/state/light.den_lamp", json.dumps({"state": state, "attributes": attrs}))
        )


def test_entity_channel_identity_command_read_and_unapplied() -> None:
    host = FakeHost("off", {})
    ch = HALightChannel(host, "light.den_lamp")
    assert ch.needs_lit and host.watched == ["light.den_lamp"]
    ch.discover()
    assert ch.identity == "matter-node-7-light" and ch.firmware == "2.4.1"
    assert ch.is_lit() is False
    # a lamp resting on a warm white, then commanded
    host.state = {"state": "on", "attributes": {"xy_color": [0.4726, 0.413]}}
    ch.snapshot()
    ch.command((0.95, 0.04))
    assert host.calls[-1] == (
        "light",
        "turn_on",
        {"entity_id": "light.den_lamp", "xy_color": [0.95, 0.04], "transition": 0},
    )
    host.relay("on", (0.95, 0.04))  # the optimistic state: the echo
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    assert host.calls[-1] == ("homeassistant", "update_entity", {"entity_id": "light.den_lamp"})
    host.relay("on", (0.4726, 0.413))  # still the colour from before: not applied (yet)
    obs = ch.observe(0.1)
    assert [(o.kind, o.trusted) for o in obs] == [("unapplied", False)]
    host.relay("on", (0.6915, 0.3083))
    obs = ch.observe(0.1)
    assert [(o.kind, o.xy, o.trusted) for o in obs] == [("device", (0.6915, 0.3083), True)]


def test_entity_channel_light_up_and_restore() -> None:
    host = FakeHost("off", {})
    ch = HALightChannel(host, "light.den_lamp")
    assert ch.is_lit() is False
    assert ch.light_up() is True
    assert host.calls[-1] == ("light", "turn_on", {"entity_id": "light.den_lamp"})
    ch.restore()  # it was off: off again
    assert host.calls[-1] == ("light", "turn_off", {"entity_id": "light.den_lamp"})
    host2 = FakeHost("on", {"color_mode": "color_temp", "color_temp_kelvin": 2700})
    ch2 = HALightChannel(host2, "light.den_lamp")
    assert ch2.is_lit() is True
    ch2.restore(0.0)
    assert host2.calls[-1] == (
        "light",
        "turn_on",
        {"entity_id": "light.den_lamp", "transition": 0.0, "color_temp_kelvin": 2700},
    )


def test_level_reads_back_in_device_units() -> None:
    from pascl.shell.ha_light import level_of

    host = FakeHost("on", {"brightness": 131, "xy_color": [0.4, 0.4]})
    ch = HALightChannel(host, "light.den_lamp")
    assert ch.level() == 130  # the host shows 131 for a Zigbee level of 130
    assert ch.level(scale=255) == 131  # a light the host drives in its own units
    assert level_of({"state": "on", "attributes": {}}) is None
    assert level_of({"state": "on", "attributes": {"brightness": "x"}}) is None
    assert level_of(None) is None


def test_entity_channel_commands_a_colour_temperature_in_kelvin_and_reads_it_back() -> None:
    """The host takes kelvin and reports kelvin; the channel speaks mireds to the driver,
    so 50 mired goes out as 20000 K and the bulb's 6535 K answer comes back as 153."""
    host = FakeHost("on", {"color_mode": "color_temp", "color_temp_kelvin": 2702})
    ch = HALightChannel(host, "light.den_lamp")
    ch.snapshot()
    ch.command_ct(50)
    assert host.calls[-1] == (
        "light",
        "turn_on",
        {"entity_id": "light.den_lamp", "color_temp_kelvin": 20000, "transition": 0},
    )
    host.relay("on", None, 20000)  # the optimistic state: the echo
    host.relay("on", None, 2702)  # the resting value again: not new
    assert [o.kind for o in ch.observe(0.1)] == ["echo"]
    ch.read()
    host.relay("on", None, 2702)  # the value from before the command: not applied yet
    assert [(o.kind, o.mired) for o in ch.observe(0.1)] == [("unapplied", 370)]
    host.relay("on", (0.3127, 0.329), 6535)
    obs = ch.observe(0.1)
    assert [(o.kind, o.mired, o.xy, o.trusted) for o in obs] == [("device", 153, None, True)]
    # back to colour: the colour temperature in the same message is ignored
    ch.command((0.95, 0.04))
    ch.read()
    host.relay("on", (0.6915, 0.3083), 6535)
    obs = ch.observe(0.1)
    assert [(o.kind, o.xy, o.mired) for o in obs] == [("device", (0.6915, 0.3083), None)]
