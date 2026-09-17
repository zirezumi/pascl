"""Many measurements over one link.

One MQTT link is one socket, and a channel drains it, so two channels on one link would eat
each other's messages. The hub is a link that fans out: each channel gets a view with its own
queue, one reader thread pulls everything the base link delivers and routes each message to
the views subscribed to its topic, and measurements run one thread per fixture
(:func:`measure_many`). Subscriptions are taken while the reader is stopped (the base link's
subscribe waits for its answer on the same socket the reader reads), which is the order the
callers already follow: build the batch's channels, then measure, then the next batch.

How many at once is a question of Zigbee airtime, not of this module: a sample is a command,
a read and the device's answer, a fixture in flight is four to five unicasts a second, and a
coordinator sustains about ten before reads start to time out and the render's own traffic
queues behind them. The caller sizes a batch per coordinator accordingly.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pascl.estimator.gamut import Verdict
from pascl.shell.gamut_measure import DeviceChannel, measure


class Hub:
    """A fan-out over one base link."""

    def __init__(self, base: Any) -> None:
        self._base = base
        self._routes: dict[str, list[HubView]] = {}
        self._subscribed: set[str] = set()
        self._entity_topics: dict[str, str] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def view(self) -> HubView:
        return HubView(self)

    # -- what views ask of the hub ---------------------------------------------------------

    def _subscribe(self, view: HubView, topic: str) -> None:
        if self.running:
            raise RuntimeError("subscribe while the hub is running: build channels first")
        with self._lock:
            self._routes.setdefault(topic, []).append(view)
            if topic not in self._subscribed:
                self._base.subscribe(topic)
                self._subscribed.add(topic)

    def _watch_entity(self, view: HubView, entity_id: str) -> str:
        if self.running:
            raise RuntimeError("watch_entity while the hub is running: build channels first")
        with self._lock:
            topic = self._entity_topics.get(entity_id)
            if topic is None:
                topic = str(self._base.watch_entity(entity_id))
                self._entity_topics[entity_id] = topic
            self._routes.setdefault(topic, []).append(view)
            return topic

    def _release(self, view: HubView) -> None:
        with self._lock:
            for topic, views in list(self._routes.items()):
                self._routes[topic] = [v for v in views if v is not view]
                if not self._routes[topic]:
                    del self._routes[topic]

    def publish(self, topic: str, payload: str) -> None:
        self._base.publish(topic, payload)

    # -- the reader ----------------------------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._read, name="pascl-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _read(self) -> None:
        while not self._stop.is_set():
            delivered = 0
            for topic, payload in self._base.drain(0.25):
                with self._lock:
                    views = list(self._routes.get(topic, ()))
                for v in views:
                    v.deliver(topic, payload)
                delivered += 1
            if not delivered:
                time.sleep(0.005)  # a link that returns at once (a test double) must not spin

    def base(self) -> Any:
        """The link underneath, for the calls that need no routing (state reads)."""
        return self._base


class HubView:
    """One channel's link: its own queue, the hub's socket."""

    def __init__(self, hub: Hub) -> None:
        self._hub = hub
        self._queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()

    def subscribe(self, topic: str) -> None:
        self._hub._subscribe(self, topic)

    def watch_entity(self, entity_id: str) -> str:
        return self._hub._watch_entity(self, entity_id)

    def publish(self, topic: str, payload: str) -> None:
        self._hub.publish(topic, payload)

    def deliver(self, topic: str, payload: str) -> None:
        self._queue.put((topic, payload))

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                yield self._queue.get(timeout=left)
            except queue.Empty:
                return

    def get_state(self, entity_id: str) -> Any:
        return self._hub.base().get_state(entity_id)

    def get_states(self) -> Any:
        return self._hub.base().get_states()

    def close(self) -> None:
        self._hub._release(self)


def measure_many(
    channels: Mapping[str, DeviceChannel],
    *,
    allow_lit: bool = False,
    abort_when: Callable[[str], str | None] | None = None,
) -> dict[str, Verdict | BaseException | None]:
    """Measure every channel at once, one thread each, and collect each verdict (None for a
    lit fixture not allowed, the exception when one was raised). The hub must be running."""
    out: dict[str, Verdict | BaseException | None] = {}

    def one(name: str, channel: DeviceChannel) -> Verdict | BaseException | None:
        try:
            when = (lambda: abort_when(name)) if abort_when is not None else None
            return measure(channel, allow_lit=allow_lit, abort_when=when)
        except BaseException as exc:
            return exc

    if not channels:
        return out
    with ThreadPoolExecutor(max_workers=len(channels)) as pool:
        futures = {name: pool.submit(one, name, ch) for name, ch in channels.items()}
        for name, fut in futures.items():
            out[name] = fut.result()
    return out


def batches(items: Iterable[tuple[str, str]], per_key: int) -> list[list[str]]:
    """Rounds of names, at most ``per_key`` per key in each round: (name, key) pairs in, the
    rounds out. Sizing a batch per coordinator is the caller's airtime budget."""
    by_key: dict[str, list[str]] = {}
    for name, key in items:
        by_key.setdefault(key, []).append(name)
    rounds: list[list[str]] = []
    while any(by_key.values()):
        this: list[str] = []
        for key, names in by_key.items():
            take, by_key[key] = names[:per_key], names[per_key:]
            this.extend(take)
        rounds.append(this)
    return rounds


__all__ = ["Hub", "HubView", "batches", "measure_many"]
