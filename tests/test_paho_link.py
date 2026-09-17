"""The direct broker link over an injected client: routing and the drain window."""

from __future__ import annotations

from typing import Any

from pascl.shell.paho_mqtt import PahoMqttLink


class FakeClient:
    def __init__(self) -> None:
        self.on_message: Any = None
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.connected: tuple[str, int] | None = None
        self.looping = False

    def connect(self, host: str, port: int) -> None:
        self.connected = (host, port)

    def loop_start(self) -> None:
        self.looping = True

    def loop_stop(self) -> None:
        self.looping = False

    def disconnect(self) -> None:
        self.connected = None

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))

    def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    def deliver(self, topic: str, payload: bytes | str) -> None:
        class Msg:
            pass

        m = Msg()
        m.topic = topic  # type: ignore[attr-defined]
        m.payload = payload  # type: ignore[attr-defined]
        self.on_message(self, None, m)


def test_link_routes_publish_subscribe_and_drain() -> None:
    client = FakeClient()
    link = PahoMqttLink("broker.test", 1884, client=client)
    assert client.connected == ("broker.test", 1884) and client.looping
    link.subscribe("z2m-4/strip")
    link.publish("z2m-4/strip/set", '{"color": {"x": 0.95, "y": 0.04}, "transition": 0}')
    assert client.subscribed == ["z2m-4/strip"]
    assert client.published[-1][0] == "z2m-4/strip/set"
    client.deliver("z2m-4/strip", b'{"state": "OFF"}')
    client.deliver("z2m-4/strip", '{"color": {"x": 0.69, "y": 0.31}}')
    assert list(link.drain(0.05)) == [
        ("z2m-4/strip", '{"state": "OFF"}'),
        ("z2m-4/strip", '{"color": {"x": 0.69, "y": 0.31}}'),
    ]
    assert list(link.drain(0.01)) == []  # the window passes with nothing to yield
    link.close()
    assert not client.looping and client.connected is None
