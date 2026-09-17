"""Many fixtures measured at once over one link: the hub routes each device's messages to
its own channel, the threads never see each other's traffic, and the batching keeps a
coordinator's airtime budget."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator

import pytest

from pascl.core.gamut import Polygon, project
from pascl.shell.hub import Hub, batches, measure_many
from pascl.shell.z2m import Z2MDeviceChannel

TRIANGLE: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))
QUAD: Polygon = ((0.1532, 0.0475), (0.40, 0.12), (0.6915, 0.3083), (0.17, 0.70))


class Coordinator:
    """A thread-safe link with devices that clip to hidden polygons: every publish answers
    on the base topic through one queue, the way one socket delivers everything."""

    def __init__(self, polygons: dict[str, Polygon]) -> None:
        self.polygons = polygons
        self.q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self.current: dict[str, tuple[float, float]] = {}
        self.subscribed: list[str] = []
        self.lock = threading.Lock()

    def publish(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        base, name, verb = "/".join(parts[:2]), parts[1], parts[-1]
        body = json.loads(payload)
        with self.lock:
            if name not in self.polygons:
                return
            if verb == "set" and "color" in body:
                xy = (float(body["color"]["x"]), float(body["color"]["y"]))
                self.current[name] = project(xy, self.polygons[name])
                self.q.put((base, json.dumps({"color": body["color"], "state": "OFF"})))
            elif verb == "get":
                x, y = self.current.get(name, (0.4, 0.4))
                self.q.put(
                    (
                        base,
                        json.dumps({"state": "OFF", "color_mode": "xy", "color": {"x": x, "y": y}}),
                    )
                )

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        try:
            yield self.q.get(timeout=seconds)
        except queue.Empty:
            return
        while True:
            try:
                yield self.q.get_nowait()
            except queue.Empty:
                return


def test_measure_many_keeps_each_devices_messages_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    from pascl.shell import gamut_measure

    # real threads and a real clock; the timing rules shortened so the suite stays quick
    monkeypatch.setattr(gamut_measure, "READ_AFTER", 0.03)
    monkeypatch.setattr(gamut_measure, "READ_RETRY", 0.03)
    monkeypatch.setattr(gamut_measure, "PAUSE", 0.01)
    monkeypatch.setattr(gamut_measure, "POLL", 0.02)
    monkeypatch.setattr(gamut_measure, "MIN_WAIT", 0.005)
    base = Coordinator({"a": TRIANGLE, "b": QUAD, "c": TRIANGLE})
    hub = Hub(base)
    channels = {
        name: Z2MDeviceChannel(hub.view(), f"z2m-1/{name}/set", identity=name) for name in "abc"
    }
    # one base subscription per topic, however many views ask
    assert base.subscribed.count("z2m-1/a") == 1
    hub.start()
    try:
        with pytest.raises(RuntimeError):
            hub.view().subscribe("z2m-1/d")
        results = measure_many(channels)
    finally:
        hub.stop()
    for name, hidden in (("a", TRIANGLE), ("b", QUAD), ("c", TRIANGLE)):
        v = results[name]
        assert not isinstance(v, BaseException) and v is not None and v.polygon is not None
        assert len(v.polygon) == len(hidden), (name, v.notes)
        for h in hidden:
            assert min(abs(h[0] - p[0]) + abs(h[1] - p[1]) for p in v.polygon) < 1e-5
        assert v.unanswered == 0, (name, v.notes)


def test_batches_cap_each_coordinator() -> None:
    items = [("a", "z2m-1"), ("b", "z2m-1"), ("c", "z2m-1"), ("d", "z2m-2"), ("e", "z2m-3")]
    assert batches(items, 2) == [["a", "b", "d", "e"], ["c"]]
    assert batches(items, 1) == [["a", "d", "e"], ["b"], ["c"]]
    assert batches([], 3) == []
