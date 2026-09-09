"""The golden trace.

A trace is a time-ordered sequence of records in the engine's own vocabulary. It opens with a
snapshot of every input and latent value at the window's start, then carries one record per
change: an input the render reads, a latent value the reference wrote (its intent caches, its
baseline targets), an output it put on the wire, or a raw sensor observation for the fusion
harness. Replay walks the records, re-evaluates the pure render at each instant, and compares
its frame with the recorded outputs within the contract's tolerances.

Paths name things the way the Home Model does, never the way any host does:

* ``solar.progress``, ``solar.factor``, ``solar.ready``, ``solar.pct.<anchor>``
* ``sleep``, ``scene.offset``, ``scene.palette.<palette>``, ``scene.transition_s``,
  ``scene.linger_s``
* ``ambient.accumulator``, ``ambient.weather``, ``ambient.scope.<scope>.drive``,
  ``ambient.scope.<scope>.coupling``
* ``vacancy.main_space``
* ``rooms.<room>.presence`` / ``.switch_zone`` / ``.multiplier`` / ``.vacancy_timer`` /
  ``.hold_active`` / ``.on_override`` / ``.hold_ms``
* ``scene_groups.<group>`` (the palette bound to a render group)
* ``fixtures.<fixture>.factor`` / ``.color_factor`` / ``.override_x`` / ``.override_y`` /
  ``.override_kelvin`` / ``.override_is_ct`` / ``.held_off`` / ``.scene_x`` / ``.scene_y`` /
  ``.light`` (the host's settled view of the light)
* latent: ``fixtures.<fixture>.intent.brightness`` / ``.intent.x`` / ``.intent.y`` /
  ``.intent.ct_mode`` / ``.intent.kelvin`` / ``.baseline_target`` / ``.last_published``
* ``sensors.<sensor>`` (raw presence and lux observations)
* outputs: ``fixtures.<fixture>`` or ``groups.<room>.<group>`` with a normalised command

The file form is newline-delimited JSON, one record per line, UTC timestamps, plus a monotonic
offset in seconds from the first record so a replay can drive a ManualClock.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

RecordKind = Literal["snapshot", "input", "latent", "sensor", "output"]


@dataclass(frozen=True)
class Record:
    t: datetime
    kind: RecordKind
    path: str = ""
    value: Any = None
    mono: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    """For a snapshot: every path and its value. For an output: the normalised command."""

    def to_json(self) -> str:
        obj: dict[str, Any] = {
            "t": self.t.astimezone(UTC).isoformat(),
            "mono": self.mono,
            "kind": self.kind,
        }
        if self.path:
            obj["path"] = self.path
        if self.value is not None:
            obj["value"] = self.value
        if self.data:
            obj["data"] = self.data
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Record:
        obj = json.loads(line)
        return cls(
            t=datetime.fromisoformat(obj["t"]),
            kind=obj["kind"],
            path=obj.get("path", ""),
            value=obj.get("value"),
            mono=float(obj.get("mono", 0.0)),
            data=obj.get("data") or {},
        )


def write_trace(path: Path, records: Iterable[Record]) -> int:
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.to_json())
            f.write("\n")
            n += 1
    return n


def read_trace(path: Path) -> Iterator[Record]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield Record.from_json(line)


def normalize_command(payload: dict[str, Any] | None, raw: str | None) -> dict[str, Any]:
    """A Zigbee2MQTT ``/set`` payload in the engine's output vocabulary: ``on``, ``brightness``
    (device units), ``xy``, ``ct_mired``, ``transition_s``; the rest is kept under ``extra``."""
    out: dict[str, Any] = {}
    if payload is None:
        out["raw"] = raw
        return out
    extra: dict[str, Any] = {}
    for k, v in payload.items():
        if k == "state" and isinstance(v, str):
            out["on"] = v.upper() == "ON"
        elif k == "brightness" and isinstance(v, int | float):
            out["brightness"] = int(v)
        elif k == "color" and isinstance(v, dict) and "x" in v and "y" in v:
            out["xy"] = [float(v["x"]), float(v["y"])]
        elif k == "color_temp" and isinstance(v, int | float):
            out["ct_mired"] = int(v)
        elif k == "transition" and isinstance(v, int | float):
            out["transition_s"] = float(v)
        else:
            extra[k] = v
    if extra:
        out["extra"] = extra
    return out
