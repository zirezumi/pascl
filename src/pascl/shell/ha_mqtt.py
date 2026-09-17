"""An MQTT link through Home Assistant: publish over its REST API, subscribe over its WebSocket.

For an installation where Home Assistant already owns the broker connection this needs no
broker credentials, only a long-lived access token. Commands go out as ``mqtt.publish`` service
calls (a service call carries the token user's context, but a device REPORT arrives on MQTT with
a fresh, user-less context, which is why colour is never sent through ``light.turn_on`` here:
that call would be captured as a human colour pick). Messages come back over the
``mqtt/subscribe`` WebSocket command, retained ones included.

Requires the ``websockets`` package (``pip install pascl[ha]``).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any


class HAMqttLink:
    """A :class:`pascl.shell.z2m.MqttLink` backed by a Home Assistant instance."""

    def __init__(self, url: str, token: str) -> None:
        from websockets.sync.client import connect  # imported here: an optional dependency

        self._url = url.rstrip("/")
        self._token = token
        ws_url = self._url.replace("https://", "wss://").replace("http://", "ws://")
        self._ws = connect(ws_url + "/api/websocket", max_size=2**26)
        msg = json.loads(self._ws.recv())
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"unexpected websocket greeting: {msg}")
        self._ws.send(json.dumps({"type": "auth", "access_token": token}))
        msg = json.loads(self._ws.recv())
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"websocket auth failed: {msg}")
        self._id = 0
        self._subs: dict[int, str] = {}

    def publish(self, topic: str, payload: str) -> None:
        body = json.dumps({"topic": topic, "payload": payload}).encode()
        req = urllib.request.Request(
            f"{self._url}/api/services/mqtt/publish",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"mqtt.publish HTTP {r.status}")

    def subscribe(self, topic: str) -> None:
        self._id += 1
        sid = self._id
        self._ws.send(json.dumps({"id": sid, "type": "mqtt/subscribe", "topic": topic}))
        while True:
            msg = json.loads(self._ws.recv(timeout=10))
            if msg.get("type") == "result" and msg.get("id") == sid:
                if not msg.get("success"):
                    raise RuntimeError(f"subscribe {topic} failed: {msg.get('error')}")
                break
        self._subs[sid] = topic

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                msg: dict[str, Any] = json.loads(self._ws.recv(timeout=left))
            except TimeoutError:
                return
            if msg.get("type") == "event" and msg.get("id") in self._subs:
                ev = msg["event"]
                yield str(ev.get("topic")), str(ev.get("payload", ""))

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """One entity's state from the REST API, or None when it does not exist."""
        req = urllib.request.Request(
            f"{self._url}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data: dict[str, Any] = json.loads(r.read().decode())
                return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def get_states(self) -> dict[str, str]:
        """entity_id -> state for every entity, in one request."""
        req = urllib.request.Request(
            f"{self._url}/api/states", headers={"Authorization": f"Bearer {self._token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            rows: list[dict[str, Any]] = json.loads(r.read().decode())
        return {str(row["entity_id"]): str(row["state"]) for row in rows if "entity_id" in row}

    def close(self) -> None:
        try:
            for sid in list(self._subs):
                self._id += 1
                self._ws.send(
                    json.dumps({"id": self._id, "type": "unsubscribe_events", "subscription": sid})
                )
            self._ws.close()
        except Exception:
            pass
