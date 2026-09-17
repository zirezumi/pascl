"""Zigbee2MQTT as a device channel for the gamut measurement.

Everything Zigbee2MQTT-specific about talking to one light lives here: the ``/set`` and ``/get``
topics, the optimistic echo it publishes before the device answers, the endpoint-suffixed keys
of a multi-endpoint device, and the state message that carries the answer to a read. The MQTT
link itself (publish, subscribe, drain) is injected, so the same channel runs over a broker
client or over Home Assistant's MQTT integration.

Rules the reference installation's fleet taught, each one a measurement that went wrong first:

* The echo is recognised by value (within 5e-4 of the command), never trusted as the device.
* A colour ``/get`` is answered in a state message on the base topic. On a multi-endpoint
  device that answer lands under the UNSUFFIXED ``color`` key while ``color_<endpoint>`` keeps
  the echo, and the unsuffixed value is then republished, stale, with every later message. It
  is taken only after this sample's read went out and only if it differs from every unsuffixed
  value seen earlier in the sample.
* A ``/set`` on the device's own topic that this channel did not send is a foreign command
  (a render, a repaint); the sample is reported as such and the driver takes it again.
* Every state message carries the endpoint's ``state``; ``ON`` during a measurement of a
  fixture that was off is reported as ``lit`` and the driver aborts, since the probe colours
  are on the wall from then on. A group command lights a device without any message on its
  own ``/set`` topic, so the state report is the only signal that catches every case.
* The transport's identity for a device is its IEEE address, read from the retained
  ``bridge/devices`` list (``discover``), suffixed with the endpoint for a multi-endpoint
  device. A topic is a name the user can change; the address survives a rename and changes
  with the hardware, which is exactly what a bound polygon needs. The same list carries the
  firmware build, recorded with the measurement as information.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from pascl.core.gamut import XY
from pascl.estimator.gamut import ECHO_TOL
from pascl.shell.gamut_measure import Observation


class MqttLink(Protocol):
    """The few MQTT operations the channel needs."""

    def publish(self, topic: str, payload: str) -> None: ...

    def subscribe(self, topic: str) -> None: ...

    def drain(self, seconds: float) -> Iterable[tuple[str, str]]:
        """Block up to ``seconds``; yield (topic, payload) for everything that arrived."""
        ...


def split_set_topic(set_topic: str) -> tuple[str, str | None]:
    """'z2m-1/dev/ep/set' -> ('z2m-1/dev', 'ep'); 'z2m-3/dev/set' -> ('z2m-3/dev', None)."""
    parts = set_topic.split("/")
    if parts[-1] != "set":
        raise ValueError(f"not a /set topic: {set_topic}")
    if len(parts) == 4:
        return "/".join(parts[:2]), parts[2]
    if len(parts) == 3:
        return "/".join(parts[:2]), None
    raise ValueError(f"unexpected topic shape: {set_topic}")


@dataclass(frozen=True)
class DeviceInfo:
    """What Zigbee2MQTT knows about one device, from ``bridge/devices``."""

    friendly_name: str
    ieee_address: str
    model_id: str | None = None
    manufacturer: str | None = None
    firmware: str | None = None
    description: str | None = None


def parse_bridge_devices(payload: str) -> dict[str, DeviceInfo]:
    """friendly_name -> DeviceInfo from a ``bridge/devices`` message; malformed entries are
    skipped rather than fatal (the coordinator's own entry has no model, for one)."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    out: dict[str, DeviceInfo] = {}
    if not isinstance(raw, list):
        return out
    for d in raw:
        if not isinstance(d, dict):
            continue
        name, ieee = d.get("friendly_name"), d.get("ieee_address")
        if not isinstance(name, str) or not isinstance(ieee, str):
            continue
        definition = d.get("definition")
        desc = definition.get("description") if isinstance(definition, dict) else None
        out[name] = DeviceInfo(
            friendly_name=name,
            ieee_address=ieee,
            model_id=_opt_str(d.get("model_id")),
            manufacturer=_opt_str(d.get("manufacturer")),
            firmware=_opt_str(d.get("software_build_id")),
            description=_opt_str(desc),
        )
    return out


def _opt_str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def bridge_devices(link: MqttLink, base_topic: str, seconds: float = 3.0) -> dict[str, DeviceInfo]:
    """Read the retained device list of the coordinator at ``base_topic``."""
    topic = f"{base_topic}/bridge/devices"
    link.subscribe(topic)
    found: dict[str, DeviceInfo] = {}
    for t, raw in link.drain(seconds):
        if t == topic:
            found = parse_bridge_devices(raw)
            break
    return found


def parse_bridge_groups(payload: str) -> dict[str, list[tuple[str, int | None]]]:
    """group friendly_name -> its members as (ieee_address, endpoint) from ``bridge/groups``."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    out: dict[str, list[tuple[str, int | None]]] = {}
    if not isinstance(raw, list):
        return out
    for g in raw:
        if not isinstance(g, dict) or not isinstance(g.get("friendly_name"), str):
            continue
        members: list[tuple[str, int | None]] = []
        for m in g.get("members") or []:
            if isinstance(m, dict) and isinstance(m.get("ieee_address"), str):
                ep = m.get("endpoint")
                members.append((m["ieee_address"], ep if isinstance(ep, int) else None))
        out[g["friendly_name"]] = members
    return out


def group_set_topics(
    link: MqttLink, base_topic: str, ieee_address: str, seconds: float = 3.0
) -> list[str]:
    """The ``/set`` topics of every group on the coordinator at ``base_topic`` that the device
    belongs to (any endpoint), from the retained ``bridge/groups``: a measurement watches them
    as foreign, since a command there reaches the device without touching its own topic."""
    topic = f"{base_topic}/bridge/groups"
    link.subscribe(topic)
    groups: dict[str, list[tuple[str, int | None]]] = {}
    for t, raw in link.drain(seconds):
        if t == topic:
            groups = parse_bridge_groups(raw)
            break
    return sorted(
        f"{base_topic}/{name}/set"
        for name, members in groups.items()
        if any(ieee == ieee_address for ieee, _ep in members)
    )


def _xy(payload: dict[str, object], key: str) -> XY | None:
    c = payload.get(key)
    if isinstance(c, dict) and "x" in c and "y" in c:
        try:
            return (float(c["x"]), float(c["y"]))
        except (TypeError, ValueError):
            return None
    return None


def _near(a: XY, b: XY, tol: float = ECHO_TOL) -> bool:
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _commands_on(raw: str) -> bool:
    """A ``/set`` payload that turns the light on (``state: ON``, any endpoint)."""
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip().upper() == "ON"
    if not isinstance(body, dict):
        return False
    return any(k == "state" or k.startswith("state_") for k, v in body.items() if v == "ON")


def _means_occupied(raw: str) -> bool:
    """An occupancy message: a relayed entity state ``on`` (bare, or JSON with ``state``), or
    a sensor's own ``occupancy``/``presence`` true."""
    text = raw.strip()
    if text.lower() == "on":
        return True
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(body, dict):
        return False
    if str(body.get("state", "")).lower() == "on":
        return True
    return any(body.get(k) is True for k in ("occupancy", "presence"))


class Z2MDeviceChannel:
    """One Zigbee2MQTT light (or one endpoint of one) as a :class:`DeviceChannel`."""

    def __init__(
        self,
        link: MqttLink,
        set_topic: str,
        identity: str | None = None,
        *,
        watch: Iterable[str] = (),
        occupancy: Iterable[str] = (),
    ) -> None:
        """``watch`` names further ``/set`` topics whose commands reach this device without
        touching its own topic: the groups it belongs to. A command on any of them during a
        sample is foreign, exactly like one on the device's own topic, and one that carries
        ``state: ON`` is the fixture being turned on. ``occupancy`` names topics whose message
        means the room this fixture lights has become occupied (a presence entity's state
        relayed by the link as ``on``, or a sensor's own ``occupancy: true``): the earliest
        warning of the render that will turn the fixture on."""
        self._link = link
        self._set_topic = set_topic
        self._base, self._ep = split_set_topic(set_topic)
        self._get_topic = set_topic[: -len("/set")] + "/get"
        self._identity = identity
        self._firmware: str | None = None
        self._info: DeviceInfo | None = None
        self._sent: list[str] = []
        self._command: XY | None = None
        self._pre_command: XY | None = None
        self._read_sent = False
        self._seen_unsuffixed: list[XY] = []
        self._before: dict[str, object] | None = None
        self._last_seen: XY | None = None
        self._watch = frozenset(watch) - {set_topic}
        self._occupancy = frozenset(occupancy)
        link.subscribe(self._base)
        link.subscribe(self._set_topic)
        for topic in sorted(self._watch | self._occupancy):
            link.subscribe(topic)

    @property
    def identity(self) -> str | None:
        return self._identity

    @property
    def firmware(self) -> str | None:
        return self._firmware

    @property
    def needs_lit(self) -> bool:
        return False  # a colour reaches a dark device, and one that ignores it stays dark

    @property
    def info(self) -> DeviceInfo | None:
        """What discovery found, if it ran."""
        return self._info

    def discover(self, base_topic: str, seconds: float = 3.0) -> DeviceInfo | None:
        """Look the device up in the coordinator's retained ``bridge/devices`` (the coordinator
        at ``base_topic``; the device is the part of this channel's topic after it). Sets the
        identity to the IEEE address (plus ``/endpoint`` on a multi-endpoint device) unless
        one was given explicitly, and records the firmware. None when the device is not
        listed, in which case the identity stays as it was."""
        prefix = base_topic.rstrip("/") + "/"
        if not self._base.startswith(prefix):
            return None
        name = self._base[len(prefix) :]
        info = bridge_devices(self._link, base_topic.rstrip("/"), seconds).get(name)
        if info is None:
            return None
        self.adopt(info)
        return info

    def adopt(self, info: DeviceInfo) -> None:
        """Take a device record already read (one ``bridge/devices`` read serves a whole
        coordinator) as this channel's device."""
        self._info = info
        self._firmware = info.firmware
        if self._identity is None:
            self._identity = info.ieee_address + (f"/{self._ep}" if self._ep else "")

    # -- snapshot and restore --------------------------------------------------------------

    def snapshot(self, seconds: float = 3.0) -> dict[str, object] | None:
        """Read the light's state once (on/off, colour mode, colour temperature, colour) so it
        can be put back afterwards. Returns the endpoint's view of the state. One read for
        both attributes: two reads answered separately let the second answer, the resting
        colour, arrive during the first sample and pass for its answer."""
        self._link.publish(self._get_topic, json.dumps({"state": "", "color": {"x": "", "y": ""}}))
        for topic, raw in self._link.drain(seconds):
            if topic != self._base:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            view = self._endpoint_view(payload)
            if "state" in view:
                self._before = view
                self._last_seen = _xy(view, "color")
                return view
        return None

    def _endpoint_view(self, payload: dict[str, object]) -> dict[str, object]:
        if self._ep is None:
            return payload
        suffix = f"_{self._ep}"
        out: dict[str, object] = {}
        for k, v in payload.items():
            if k.endswith(suffix):
                out[k[: -len(suffix)]] = v
        return out

    def is_lit(self) -> bool | None:
        view = self._before if self._before is not None else self.snapshot()
        if view is None:
            return None
        return view.get("state") == "ON"

    def restore(self, transition: float = 0.2) -> None:
        before = self._before
        if before is None:
            return
        if before.get("color_mode") == "color_temp" and before.get("color_temp") is not None:
            payload = {"color_temp": before["color_temp"], "transition": transition}
        else:
            xy = _xy(before, "color")
            if xy is None:
                return
            payload = {"color": {"x": xy[0], "y": xy[1]}, "transition": transition}
        self._link.publish(self._set_topic, json.dumps(payload))

    # -- the sample --------------------------------------------------------------------------

    def command(self, xy: XY) -> None:
        payload = json.dumps({"color": {"x": xy[0], "y": xy[1]}, "transition": 0})
        self._sent.append(payload)
        self._command = xy
        self._pre_command = self._last_seen
        self._read_sent = False
        self._seen_unsuffixed = []
        self._link.publish(self._set_topic, payload)

    def read(self) -> None:
        self._read_sent = True
        self._link.publish(self._get_topic, json.dumps({"color": {"x": "", "y": ""}}))

    def observe(self, seconds: float) -> list[Observation]:
        out: list[Observation] = []
        for topic, raw in self._link.drain(seconds):
            if topic == self._set_topic or topic in self._watch:
                if topic == self._set_topic and raw in self._sent:
                    continue
                out.append(Observation("foreign"))
                if _commands_on(raw):
                    out.append(Observation("lit"))
                continue
            if topic in self._occupancy:
                if _means_occupied(raw):
                    out.append(Observation("occupied"))
                continue
            if topic != self._base:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if self._endpoint_view(payload).get("state") == "ON":
                out.append(Observation("lit"))
            ob = self._classify(payload)
            if ob is not None:
                out.append(ob)
        return out

    def _classify(self, payload: dict[str, object]) -> Observation | None:
        """The colour in a state message, as echo or device value.

        Before this sample's read went out, a value equal to the command is the optimistic
        echo, and a value equal to the last colour the device was seen at is not new (the
        transport republishes the whole state on any change, and a late answer to an earlier
        read carries the resting colour). After the read, the message carries what the device
        answered, so even the command's own value means the device reached it and is reported
        as the device's own, trusted (a probe inside the true gamut is answered exactly). On a
        multi-endpoint device the endpoint key keeps the echo throughout and only the
        unsuffixed key carries the read answer, taken once it has moved from what the sample
        saw before the read."""
        cmd = self._command
        if cmd is None:
            return None
        key = f"color_{self._ep}" if self._ep else "color"
        value = _xy(payload, key)
        if self._ep:
            alt = _xy(payload, "color")
            if alt is not None:
                if not self._read_sent:
                    self._seen_unsuffixed.append(alt)
                    if value is None or _near(value, cmd):
                        return Observation("echo")
                elif not any(_near(alt, v) for v in self._seen_unsuffixed):
                    self._last_seen = alt
                    return Observation("device", alt, trusted=True)
            if value is None or _near(value, cmd):
                return None if value is None else Observation("echo")
            self._last_seen = value
            return Observation("device", value)
        if value is None:
            return None
        if not self._read_sent:
            if _near(value, cmd):
                return Observation("echo")
            if self._last_seen is not None and _near(value, self._last_seen):
                return None  # the colour it already showed: not an answer to this command
        elif (
            self._pre_command is not None
            and _near(value, self._pre_command)
            and not _near(value, cmd)
        ):
            # the read landed before the device applied the command: it still shows what it
            # showed before, and the driver reads again
            return None
        self._last_seen = value
        return Observation("device", value, trusted=self._read_sent)
