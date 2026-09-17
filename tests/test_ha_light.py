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

    def relay(self, state: str, xy: tuple[float, float] | None) -> None:
        attrs = {"xy_color": list(xy)} if xy is not None else {}
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
