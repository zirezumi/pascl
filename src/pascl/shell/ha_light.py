"""A light the host exposes as an entity, as a device channel for the gamut measurement.

Whatever the transport underneath (Matter, a Hue bridge, ZHA, anything Home Assistant turns
into a ``light`` entity), the host gives the same three things this channel needs: a service
to command a colour with no transition (``light.turn_on``), a service to ask the device for
its current attributes (``homeassistant.update_entity``), and the entity's state changes,
which the link relays on a topic of their own (``HAMqttLink.watch_entity``) with the
attributes, ``xy_color`` among them, in the payload.

What this channel cannot do that the Zigbee2MQTT one can: apply a colour to a fixture that is
off. ``light.turn_on`` turns the light on. So a fixture reached this way is measured lit, in
the forced mode, and is put back to off afterwards when it was off before; the invisible
measurement of a dark fixture needs a transport that carries colour to an off device.

The identity a polygon is bound to is the entity's ``unique_id`` from the host's registry,
which follows the device (a Matter node id, a bridge's light id) and not the entity's name;
the device registry supplies the firmware when the integration reports one.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pascl.core.gamut import XY
from pascl.estimator.gamut import ECHO_TOL
from pascl.shell.gamut_measure import Observation


class EntityLink(Protocol):
    """What the host must offer: the link's entity watch, states, services, registries."""

    def watch_entity(self, entity_id: str) -> str: ...

    def drain(self, seconds: float) -> Any: ...

    def get_state(self, entity_id: str) -> dict[str, Any] | None: ...

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None: ...

    def entity_registry(self, entity_id: str) -> dict[str, Any] | None: ...

    def device_registry(self, device_id: str) -> dict[str, Any] | None: ...


def _xy(attributes: dict[str, Any]) -> XY | None:
    v = attributes.get("xy_color")
    if isinstance(v, list | tuple) and len(v) == 2:
        try:
            return (float(v[0]), float(v[1]))
        except (TypeError, ValueError):
            return None
    return None


def _near(a: XY, b: XY, tol: float = ECHO_TOL) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


class HALightChannel:
    """One host light entity as a :class:`pascl.shell.gamut_measure.DeviceChannel`."""

    def __init__(
        self,
        link: EntityLink,
        entity_id: str,
        identity: str | None = None,
        *,
        occupancy: list[str] | None = None,
    ) -> None:
        self._link = link
        self._entity = entity_id
        self._topic = link.watch_entity(entity_id)
        self._occupancy = frozenset(occupancy or ())
        self._identity = identity
        self._firmware: str | None = None
        self._command: XY | None = None
        self._pre_command: XY | None = None
        self._read_sent = False
        self._last_seen: XY | None = None
        self._before: dict[str, Any] | None = None

    @property
    def identity(self) -> str | None:
        return self._identity

    @property
    def firmware(self) -> str | None:
        return self._firmware

    @property
    def needs_lit(self) -> bool:
        return True  # light.turn_on turns a dark fixture on: forced mode only

    def discover(self) -> dict[str, Any] | None:
        """Bind to the registry's ``unique_id`` (unless an identity was given) and take the
        firmware from the device registry when it has one."""
        entry = self._link.entity_registry(self._entity)
        if entry is None:
            return None
        if self._identity is None and entry.get("unique_id"):
            self._identity = str(entry["unique_id"])
        device_id = entry.get("device_id")
        if isinstance(device_id, str):
            dev = self._link.device_registry(device_id)
            if dev is not None and dev.get("sw_version"):
                self._firmware = str(dev["sw_version"])
        return entry

    # -- snapshot and restore --------------------------------------------------------------

    def snapshot(self) -> dict[str, Any] | None:
        st = self._link.get_state(self._entity)
        if st is None:
            return None
        attributes = st.get("attributes") or {}
        self._before = {"state": st.get("state"), **attributes}
        self._last_seen = _xy(attributes)
        return self._before

    def is_lit(self) -> bool | None:
        before = self._before if self._before is not None else self.snapshot()
        if before is None:
            return None
        return before.get("state") == "on"

    def restore(self, transition: float = 0.2) -> None:
        before = self._before
        if before is None:
            return
        if before.get("state") != "on":
            self._link.call_service("light", "turn_off", {"entity_id": self._entity})
            return
        data: dict[str, Any] = {"entity_id": self._entity, "transition": transition}
        if before.get("color_mode") == "color_temp" and before.get("color_temp_kelvin"):
            data["color_temp_kelvin"] = before["color_temp_kelvin"]
        else:
            xy = _xy(before)
            if xy is None:
                return
            data["xy_color"] = [xy[0], xy[1]]
        self._link.call_service("light", "turn_on", data)

    # -- the sample --------------------------------------------------------------------------

    def command(self, xy: XY) -> None:
        self._command = xy
        self._pre_command = self._last_seen
        self._read_sent = False
        self._link.call_service(
            "light",
            "turn_on",
            {"entity_id": self._entity, "xy_color": [xy[0], xy[1]], "transition": 0},
        )

    def read(self) -> None:
        self._read_sent = True
        self._link.call_service("homeassistant", "update_entity", {"entity_id": self._entity})

    def observe(self, seconds: float) -> list[Observation]:
        out: list[Observation] = []
        for topic, raw in self._link.drain(seconds):
            if topic in self._occupancy:
                if _relayed_state(raw) == "on":
                    out.append(Observation("occupied"))
                continue
            if topic != self._topic:
                continue
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(body, dict):
                continue
            ob = self._classify(_xy(body.get("attributes") or {}))
            if ob is not None:
                out.append(ob)
        return out

    def _classify(self, value: XY | None) -> Observation | None:
        cmd = self._command
        if cmd is None or value is None:
            return None
        if not self._read_sent:
            if _near(value, cmd):
                return Observation("echo")
            if self._last_seen is not None and _near(value, self._last_seen):
                return None
        elif (
            self._pre_command is not None
            and _near(value, self._pre_command)
            and not _near(value, cmd)
        ):
            return None  # not applied yet; the driver reads again
        self._last_seen = value
        return Observation("device", value, trusted=self._read_sent)


def _relayed_state(raw: str) -> str:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip().lower()
    if isinstance(body, dict):
        return str(body.get("state", "")).lower()
    return ""
