"""Assembling a golden trace from a recording.

The recording is the raw, host-keyed export of an observation store: an optional opening
snapshot of every entity's state at the window start, then one line per state change or wire
command in time order. Under a binding and a model it becomes a trace in the engine's own
vocabulary, and everything the binding could not place is counted rather than dropped silently,
because an incomplete input vector is the failure mode a replay cannot see.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pascl.harness.binding import Binding, Index, expand, transform_value
from pascl.harness.trace import Record, normalize_command
from pascl.model import HomeModel


@dataclass
class AssembleResult:
    records: list[Record] = field(default_factory=list)
    unmapped_entities: Counter[str] = field(default_factory=Counter)
    unmapped_topics: Counter[str] = field(default_factory=Counter)
    kinds: Counter[str] = field(default_factory=Counter)

    @property
    def coverage(self) -> dict[str, int]:
        return {
            "records": len(self.records),
            "unmapped_entities": sum(self.unmapped_entities.values()),
            "unmapped_topics": sum(self.unmapped_topics.values()),
        }


def _parse_t(raw: str) -> datetime:
    text = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _attr_or_state(target_attr: str | None, state: Any, attrs: dict[str, Any] | None) -> Any:
    if target_attr is None:
        return state
    return (attrs or {}).get(target_attr)


def assemble(model: HomeModel, binding: Binding, lines: Iterable[str]) -> AssembleResult:
    index: Index = expand(binding, model)
    result = AssembleResult()
    t0: datetime | None = None

    def mono(t: datetime) -> float:
        return 0.0 if t0 is None else (t - t0).total_seconds()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        t = _parse_t(obj["t"])
        if t0 is None:
            t0 = t
        kind = obj.get("kind")
        if kind == "snapshot":
            data: dict[str, Any] = {}
            for eid, st in (obj.get("states") or {}).items():
                target = index.entities.get(eid)
                if target is None:
                    result.unmapped_entities[eid] += 1
                    continue
                state = st.get("state") if isinstance(st, dict) else st
                attrs = st.get("attrs") if isinstance(st, dict) else None
                data[target.path] = transform_value(
                    target.transform,
                    _attr_or_state(target.attribute, state, attrs),
                    attrs,
                    index.palette_ids,
                )
            result.records.append(Record(t=t, kind="snapshot", mono=mono(t), data=data))
            result.kinds["snapshot"] += 1
        elif kind == "state":
            eid = obj.get("id", "")
            target = index.entities.get(eid)
            if target is None:
                result.unmapped_entities[eid] += 1
                continue
            attrs = obj.get("attrs")
            value = transform_value(
                target.transform,
                _attr_or_state(target.attribute, obj.get("state"), attrs),
                attrs,
                index.palette_ids,
            )
            result.records.append(
                Record(t=t, kind=target.kind, path=target.path, value=value, mono=mono(t))
            )
            result.kinds[target.kind] += 1
        elif kind == "set":
            topic = obj.get("topic", "")
            target = index.topics.get(topic)
            if target is None:
                result.unmapped_topics[topic] += 1
                continue
            command = normalize_command(obj.get("payload"), obj.get("raw"))
            result.records.append(
                Record(t=t, kind="output", path=target.path, mono=mono(t), data=command)
            )
            result.kinds["output"] += 1
        else:
            result.kinds[f"ignored:{kind}"] += 1
    return result
