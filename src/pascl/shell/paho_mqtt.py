"""An MQTT link straight to a broker, for a runtime that owns its own connection.

The add-on talks to the broker itself rather than through Home Assistant's API; this is the
same :class:`pascl.shell.z2m.MqttLink` shape over ``paho-mqtt`` (``pip install pascl[mqtt]``).
Messages arrive on paho's network thread and are queued; ``drain`` hands them over on the
caller's thread, so the measurement driver stays single-threaded and clock-driven.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Iterator
from typing import Any


class PahoMqttLink:
    """A :class:`pascl.shell.z2m.MqttLink` over a paho client.

    ``client`` may be supplied ready-made (a test double, or a client the caller configured
    with TLS and its own options); otherwise one is built from the host, port and credentials.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        *,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "pascl-gamut",
        client: Any | None = None,
    ) -> None:
        self._inbox: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        if client is None:
            import paho.mqtt.client as mqtt  # imported here: an optional dependency

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
            if username is not None:
                client.username_pw_set(username, password)
        self._client = client
        client.on_message = self._on_message
        if hasattr(client, "connect"):
            client.connect(host, port)
        if hasattr(client, "loop_start"):
            client.loop_start()

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        payload = (
            msg.payload.decode("utf-8", "replace")
            if isinstance(msg.payload, bytes)
            else str(msg.payload)
        )
        self._inbox.put((str(msg.topic), payload))

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload)

    def subscribe(self, topic: str) -> None:
        self._client.subscribe(topic)

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                yield self._inbox.get(timeout=left)
            except queue.Empty:
                return

    def close(self) -> None:
        try:
            if hasattr(self._client, "loop_stop"):
                self._client.loop_stop()
            if hasattr(self._client, "disconnect"):
                self._client.disconnect()
        except Exception:
            pass
