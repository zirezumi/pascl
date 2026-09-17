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
    """Devices that clip to a triangle, on a coordinator that queues: every read is airtime,
    and a read answers after base_delay x (reads outstanding, this one included)^2 seconds,
    so the latency climbs steeply with the number of fixtures reading at once."""

    def __init__(self, names: list[str], *, base_delay: float, deaf_until: float = 0.0) -> None:
        self.names = names
        self.base_delay = base_delay
        #: colour reads before this monotonic instant go unanswered: a coordinator that is away
        self.deaf_until = deaf_until
        self.q: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self.current: dict[str, tuple[float, float]] = {}
        self.outstanding = 0
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
            self.q.put((base, json.dumps({"color": body["color"], "state": "OFF"})))
        elif verb == "get":
            if "state" not in body and time.monotonic() < self.deaf_until:
                return  # a sample's colour read goes unanswered; the snapshot still answers
            with self.lock:
                self.outstanding += 1
                on_air = self.outstanding
                self.max_seen = max(self.max_seen, on_air)
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
                    self.outstanding -= 1

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


def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timing rules shortened so the suite stays quick; the retry interval leaves room for
    an answer to land, as the real one does."""
    from pascl.shell import airtime, gamut_measure

    monkeypatch.setattr(gamut_measure, "READ_AFTER", 0.02)
    monkeypatch.setattr(gamut_measure, "READ_RETRY", 0.3)
    monkeypatch.setattr(gamut_measure, "ANSWER_TIMEOUT", 0.8)
    monkeypatch.setattr(gamut_measure, "PAUSE", 0.01)
    monkeypatch.setattr(gamut_measure, "POLL", 0.02)
    monkeypatch.setattr(gamut_measure, "MIN_WAIT", 0.005)
    monkeypatch.setattr(airtime, "LATENCY_BUDGET", 0.1)


def test_measure_adaptively_finds_the_coordinators_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quick(monkeypatch)
    names = [f"f{i}" for i in range(6)]
    # 0.01 s x (reads outstanding)^2: one or two answer inside the budget, three do not
    base = LoadedCoordinator(names, base_delay=0.01)
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
    # the window grew from one on prompt answers and backed off when a third read on the air
    # pushed the latency past the budget, so the air never carried more than three reads at
    # once (four allowed: a retry can join the third before the backoff lands), then climbed
    # again once fixtures finished; every launch respected the window of its moment
    assert w.increases >= 1 and w.peak >= 2 and w.decreases >= 1
    assert base.max_seen <= 4
    assert all(f <= win for _n, f, win in launches)
    assert launches[0] == ("f0", 1, 1)
    assert w.samples >= 24 * len(names)


def test_a_fixture_given_up_on_for_silence_is_tried_again_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator is away for the first two seconds: the first fixture's opening samples
    get no answer and the measurement gives up on it as not reachable. That is what a
    transport the window has just backed off from looks like too, so the fixture goes to
    the back of its queue once, and is measured when its turn comes round again."""
    _quick(monkeypatch)
    names = ["f0", "f1"]
    base = LoadedCoordinator(names, base_delay=0.01, deaf_until=time.monotonic() + 2.0)
    hub = Hub(base)
    channels = {n: Z2MDeviceChannel(hub.view(), f"z2m-1/{n}/set", identity=n) for n in names}
    launches: list[str] = []
    hub.start()
    try:
        results, _report = measure_adaptively(
            channels,
            dict.fromkeys(names, "z2m-1"),
            ceiling=2,
            on_launch=lambda n, _f, _w: launches.append(n),
        )
    finally:
        hub.stop()
    assert launches == ["f0", "f1", "f0"]
    for n in names:
        v = results[n]
        assert not isinstance(v, BaseException) and v is not None
        assert v.polygon is not None and v.aborted is None, (n, v.notes)
