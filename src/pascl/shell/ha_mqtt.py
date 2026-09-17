"""An MQTT link through Home Assistant: publish over its REST API, subscribe over its WebSocket.

For an installation where Home Assistant already owns the broker connection this needs no
broker credentials, only a long-lived access token. Commands go out as ``mqtt.publish`` service
calls (a service call carries the token user's context, but a device REPORT arrives on MQTT with
a fresh, user-less context, which is why colour is never sent through ``light.turn_on`` here:
that call would be captured as a human colour pick). Messages come back over the
``mqtt/subscribe`` WebSocket command, retained ones included.

The link can also watch an entity (``watch_entity``): the state changes of, say, a room's
presence sensor arrive on the same socket as the MQTT traffic and are relayed as messages on a
synthetic topic (``ha/state/<entity_id>`` with the new state as payload), so a channel can
treat "the room became occupied" like any other thing it observes.

Requires the ``websockets`` package (``pip install pascl[ha]``).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any


def entity_payload(state: dict[str, Any]) -> str:
    """How an entity's state is relayed on its synthetic topic: JSON with the state and the
    attributes, so a watcher of a presence sensor reads ``state`` and a channel over a light
    entity reads ``attributes.xy_color`` from the same message."""
    return json.dumps(
        {"state": state.get("state", ""), "attributes": state.get("attributes") or {}}
    )


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
        self._entity_subs: dict[int, str] = {}
        self._backlog: list[dict[str, Any]] = []

    @staticmethod
    def entity_topic(entity_id: str) -> str:
        """The synthetic topic an entity's state changes are relayed on."""
        return f"ha/state/{entity_id}"

    def watch_entity(self, entity_id: str) -> str:
        """Relay the entity's state changes as messages on :meth:`entity_topic`; returns
        that topic. Idempotent per entity."""
        topic = self.entity_topic(entity_id)
        if topic in self._entity_subs.values():
            return topic
        self._id += 1
        sid = self._id
        self._ws.send(
            json.dumps(
                {
                    "id": sid,
                    "type": "subscribe_trigger",
                    "trigger": {"platform": "state", "entity_id": entity_id},
                }
            )
        )
        self._await_result(sid, f"watch {entity_id}")
        self._entity_subs[sid] = topic
        return topic

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
        self._await_result(sid, f"subscribe {topic}")
        self._subs[sid] = topic

    def _await_result(self, sid: int, what: str) -> None:
        """Wait for the command's result; events that arrive meanwhile are kept for drain."""
        while True:
            msg = json.loads(self._ws.recv(timeout=10))
            if msg.get("type") == "result" and msg.get("id") == sid:
                if not msg.get("success"):
                    raise RuntimeError(f"{what} failed: {msg.get('error')}")
                return
            if msg.get("type") == "event":
                self._backlog.append(msg)

    def _relay(self, msg: dict[str, Any]) -> tuple[str, str] | None:
        """An event message as (topic, payload), or None when it is not one this link relays."""
        if msg.get("type") != "event":
            return None
        sid = msg.get("id")
        ev = msg.get("event") or {}
        if sid in self._subs:
            return str(ev.get("topic")), str(ev.get("payload", ""))
        if sid in self._entity_subs:
            to_state = ((ev.get("variables") or {}).get("trigger") or {}).get("to_state") or {}
            return self._entity_subs[sid], entity_payload(to_state)
        return None

    def drain(self, seconds: float) -> Iterator[tuple[str, str]]:
        while self._backlog:
            relayed = self._relay(self._backlog.pop(0))
            if relayed is not None:
                yield relayed
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            try:
                msg: dict[str, Any] = json.loads(self._ws.recv(timeout=left))
            except TimeoutError:
                return
            relayed = self._relay(msg)
            if relayed is not None:
                yield relayed

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

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        """A service call over REST (``light.turn_on`` for a light the host bridges)."""
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self._url}/api/services/{domain}/{service}",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"{domain}.{service} HTTP {r.status}")

    def _command(self, msg: dict[str, Any]) -> Any:
        """A WebSocket command's result (the registries), taken while nothing else reads."""
        self._id += 1
        sid = self._id
        self._ws.send(json.dumps({"id": sid, **msg}))
        while True:
            reply = json.loads(self._ws.recv(timeout=10))
            if reply.get("type") == "result" and reply.get("id") == sid:
                if not reply.get("success"):
                    raise RuntimeError(f"{msg.get('type')} failed: {reply.get('error')}")
                return reply.get("result")
            if reply.get("type") == "event":
                self._backlog.append(reply)

    def entity_registry(self, entity_id: str) -> dict[str, Any] | None:
        """The entity registry entry (``unique_id``, ``device_id`` ...), or None."""
        try:
            result: dict[str, Any] = self._command(
                {"type": "config/entity_registry/get", "entity_id": entity_id}
            )
            return result
        except RuntimeError:
            return None

    def device_registry(self, device_id: str) -> dict[str, Any] | None:
        """The device registry entry (``sw_version``, ``model``, ``manufacturer`` ...)."""
        devices = self._command({"type": "config/device_registry/list"})
        for d in devices or []:
            if isinstance(d, dict) and d.get("id") == device_id:
                return d
        return None

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
            for sid in [*self._subs, *self._entity_subs]:
                self._id += 1
                self._ws.send(
                    json.dumps({"id": self._id, "type": "unsubscribe_events", "subscription": sid})
                )
            self._ws.close()
        except Exception:
            pass
