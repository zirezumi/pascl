"""The airtime controller: the number in flight per coordinator is found from the samples,
not assumed. A simulated coordinator answers a read-back later the more fixtures are on the
air at once, the way a transmit queue does; the window must grow while answers are prompt,
back off when they are not, and every fixture must still be measured exactly."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Iterator

import pytest

from pascl.core.gamut import Polygon, project
from pascl.shell.airtime import GOOD_STREAK, Window, measure_adaptively
from pascl.shell.gamut_measure import Sample
from pascl.shell.hub import Hub
from pascl.shell.z2m import Z2MDeviceChannel

TRIANGLE: Polygon = ((0.153185, 0.047547), (0.691493, 0.308293), (0.169986, 0.699992))


class LoadedCoordinator:
    """Devices that clip to a triangle, on a coordinator that queues: a read answers after
    base_delay x (fixtures on the air)^2 seconds, so the latency climbs steeply with the
    number in flight."""

    def __init__(self, names: list[str], *, base_delay: float) -> None:
        self.names = names
        self.base_delay = base_delay
        self.q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self.current: dict[str, tuple[float, float]] = {}
        self.busy: set[str] = set()
        self.lock = threading.Lock()
        self.max_seen = 0
        self.subscribed: list[str] = []

    def publish(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        base, name, verb = "/".join(parts[:2]), parts[1], parts[-1]
        body = json.loads(payload)
        if name not in self.names:
            return
        if verb == "set" and "color" in body:
            xy = (float(body["color"]["x"]), float(body["color"]["y"]))
            with self.lock:
                self.current[name] = project(xy, TRIANGLE)
                # a probe (integer zero transition) puts the fixture on the air until its read
                # is answered; the restore at the end of a measurement is not a probe
                if isinstance(body.get("transition"), int):
                    self.busy.add(name)
                    self.max_seen = max(self.max_seen, len(self.busy))
            self.q.put((base, json.dumps({"color": body["color"], "state": "OFF"})))
        elif verb == "get":
            with self.lock:
                on_air = len(self.busy)
                x, y = self.current.get(name, (0.4, 0.4))
            delay = self.base_delay * on_air * on_air

            def answer() -> None:
                time.sleep(delay)
                self.q.put(
                    (
                        base,
                        json.dumps({"state": "OFF", "color_mode": "xy", "color": {"x": x, "y": y}}),
                    )
                )
                with self.lock:
                    self.busy.discard(name)

            threading.Thread(target=answer, daemon=True).start()

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


def test_window_grows_on_prompt_answers_and_backs_off_on_a_retry() -> None:
    w = Window(ceiling=4)
    # two reads went out for a good sample: the first answered, the second confirmed it
    good = Sample((0.5, 0.4), False, False, trusted=True, reads=2, latency=0.1, confirmed=True)
    slow = Sample((0.5, 0.4), False, False, trusted=True, reads=3, latency=0.9, retries=1)
    interrupted = Sample(None, False, True)
    unapplied = Sample(None, True, False, reads=7, unapplied=True)
    settling = Sample(None, False, False, reads=6, latency=0.1, moved=True)
    for _ in range(GOOD_STREAK - 1):
        w.observe(good)
    assert w.slots == 1
    w.observe(good)
    assert w.slots == 2 and w.increases == 1
    for _ in range(GOOD_STREAK):
        w.observe(good)
    assert w.slots == 3
    w.observe(interrupted)  # a foreign command says nothing about airtime
    w.observe(unapplied)  # nor a device that answered every read with its resting colour
    w.observe(settling)  # nor one still changing colour: its reads were answered
    assert w.slots == 3 and w.decreases == 0
    w.observe(slow)
    assert w.slots == 1 and w.decreases == 1 and w.history == [2, 3, 1]
    for _ in range(GOOD_STREAK * 3):
        w.observe(good)
    assert w.slots == 4 and w.peak == 4  # the ceiling holds


def test_measure_adaptively_finds_the_coordinators_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pascl.shell import airtime, gamut_measure

    monkeypatch.setattr(gamut_measure, "READ_AFTER", 0.02)
    monkeypatch.setattr(gamut_measure, "READ_RETRY", 0.12)
    monkeypatch.setattr(gamut_measure, "ANSWER_TIMEOUT", 0.8)
    monkeypatch.setattr(gamut_measure, "PAUSE", 0.01)
    monkeypatch.setattr(gamut_measure, "POLL", 0.02)
    monkeypatch.setattr(gamut_measure, "MIN_WAIT", 0.005)
    monkeypatch.setattr(airtime, "LATENCY_BUDGET", 0.1)
    names = [f"f{i}" for i in range(6)]
    # 0.02 s x (on the air)^2: one or two on the air answer inside the budget, three do not
    base = LoadedCoordinator(names, base_delay=0.02)
    hub = Hub(base)
    channels = {n: Z2MDeviceChannel(hub.view(), f"z2m-1/{n}/set", identity=n) for n in names}
    launches: list[tuple[str, int, int]] = []
    hub.start()
    try:
        results, report = measure_adaptively(
            channels,
            dict.fromkeys(names, "z2m-1"),
            ceiling=6,
            on_launch=lambda n, f, w: launches.append((n, f, w)),
        )
    finally:
        hub.stop()
    for n in names:
        v = results[n]
        assert not isinstance(v, BaseException) and v is not None and v.polygon is not None
        for hidden in TRIANGLE:
            assert min(abs(hidden[0] - p[0]) + abs(hidden[1] - p[1]) for p in v.polygon) < 1e-5
    w = report["z2m-1"]
    # the window grew from one on prompt answers and backed off when three or four on the air
    # pushed the latency past the budget, so the air never carried more than four (a read
    # issued while others sit between samples sees a quieter air, and once the last fixtures
    # are alone the window may climb again with nothing left to launch); every launch
    # respected the window of its moment
    assert w.increases >= 1 and w.peak >= 2 and w.decreases >= 1
    assert base.max_seen <= 4
    assert all(f <= win for _n, f, win in launches)
    assert launches[0] == ("f0", 1, 1)
    assert w.samples >= 24 * len(names)
