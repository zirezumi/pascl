"""How many fixtures to measure at once: measured, not assumed.

A coordinator (or any transport) has an airtime budget the engine cannot know in advance: it
depends on the radio, the mesh, the firmware, and whatever else is on the air. The evidence is
in every sample, though. A device that answers its first read-back within a few hundred
milliseconds is on a transport with room to spare; one that needs a second read, or none, is
on one that is queuing. So the number in flight per key (a coordinator's base topic, a
transport id) is a window driven the way TCP drives its own: additive increase after a streak
of first-read answers under the latency budget, multiplicative decrease on the first retry or
timeout. The window starts at one, never exceeds the ceiling, and a decrease never interrupts a
measurement already running; it only stops the next launch until the window has room again.

:func:`measure_adaptively` is :func:`pascl.shell.hub.measure_many` with this controller
between the pending fixtures and the thread pool, and it reports what the windows did so a
reader can see the budget the run found.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Final

from pascl.estimator.gamut import Verdict
from pascl.shell.gamut_measure import DeviceChannel, Sample, measure

#: A first-read answer slower than this is a transport that is starting to queue.
LATENCY_BUDGET: Final = 0.6
#: Consecutive good samples on a key before its window grows by one.
GOOD_STREAK: Final = 8
#: The window never grows past this, whatever the evidence: past it a single decrease would
#: strand too many measurements mid-flight on a transport that has just shown it is full.
CEILING: Final = 6
#: The multiplicative decrease.
BACKOFF: Final = 0.5


@dataclass
class Window:
    """One key's controller."""

    size: float = 1.0
    ceiling: int = CEILING
    streak: int = 0
    samples: int = 0
    good: int = 0
    increases: int = 0
    decreases: int = 0
    peak: int = 1
    history: list[int] = field(default_factory=list)

    @property
    def slots(self) -> int:
        return max(1, int(self.size))

    def observe(self, sample: Sample) -> None:
        """Feed one sample's evidence. Good: answered on the first read within the budget."""
        self.samples += 1
        good = (
            sample.trusted
            and sample.reads <= 1
            and sample.latency is not None
            and sample.latency <= LATENCY_BUDGET
        )
        if good:
            self.good += 1
            self.streak += 1
            if self.streak >= GOOD_STREAK and self.slots < self.ceiling:
                self.size = float(self.slots + 1)
                self.increases += 1
                self.streak = 0
                self.history.append(self.slots)
                self.peak = max(self.peak, self.slots)
        elif sample.foreign or sample.lit or sample.occupied:
            # a foreign command or an interruption says nothing about airtime
            return
        else:
            self.streak = 0
            before = self.slots
            self.size = max(1.0, self.size * BACKOFF)
            if self.slots != before:
                self.decreases += 1
                self.history.append(self.slots)


@dataclass(frozen=True)
class WindowReport:
    key: str
    final: int
    peak: int
    samples: int
    good: int
    increases: int
    decreases: int
    history: tuple[int, ...]


def measure_adaptively(
    channels: Mapping[str, DeviceChannel],
    keys: Mapping[str, str],
    *,
    allow_lit: bool = False,
    abort_when: Callable[[str], str | None] | None = None,
    ceiling: int = CEILING,
    start: int = 1,
    on_launch: Callable[[str, int, int], None] | None = None,
    poll_s: float = 0.05,
) -> tuple[dict[str, Verdict | BaseException | None], dict[str, WindowReport]]:
    """Measure every channel, admitting fixtures per key as that key's window allows. Returns
    each fixture's verdict (None for a lit fixture not allowed, the exception when one was
    raised) and a report per key. ``on_launch(name, in_flight_on_key, window)`` is told of
    every launch. The hub carrying the channels must be running."""
    windows: dict[str, Window] = {
        k: Window(size=float(max(1, start)), ceiling=max(1, ceiling)) for k in set(keys.values())
    }
    lock = threading.Lock()
    results: dict[str, Verdict | BaseException | None] = {}
    pending: dict[str, list[str]] = {}
    for name in channels:
        pending.setdefault(keys.get(name, ""), []).append(name)
    windows.setdefault("", Window(size=float(max(1, start)), ceiling=max(1, ceiling)))
    in_flight: dict[str, int] = dict.fromkeys(windows, 0)

    def one(name: str, channel: DeviceChannel) -> Verdict | BaseException | None:
        key = keys.get(name, "")

        def feed(sample: Sample) -> None:
            with lock:
                windows[key].observe(sample)

        try:
            when = (lambda: abort_when(name)) if abort_when is not None else None
            return measure(channel, allow_lit=allow_lit, abort_when=when, on_sample=feed)
        except BaseException as exc:
            return exc

    running: dict[Future[Verdict | BaseException | None], str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(channels))) as pool:
        while pending or running:
            launched = False
            with lock:
                for key, names in pending.items():
                    while names and in_flight[key] < windows[key].slots:
                        name = names.pop(0)
                        in_flight[key] += 1
                        fut = pool.submit(one, name, channels[name])
                        running[fut] = name
                        launched = True
                        if on_launch is not None:
                            on_launch(name, in_flight[key], windows[key].slots)
                for key in [k for k, names in pending.items() if not names]:
                    del pending[key]
            if not running:
                if pending and not launched:
                    time.sleep(poll_s)
                continue
            done, _ = wait(list(running), timeout=poll_s, return_when=FIRST_COMPLETED)
            for fut in done:
                name = running.pop(fut)
                results[name] = fut.result()
                with lock:
                    in_flight[keys.get(name, "")] -= 1
    report = {
        key: WindowReport(
            key,
            w.slots,
            w.peak,
            w.samples,
            w.good,
            w.increases,
            w.decreases,
            tuple(w.history),
        )
        for key, w in windows.items()
        if w.samples or key in {keys.get(n, "") for n in channels}
    }
    return results, report
