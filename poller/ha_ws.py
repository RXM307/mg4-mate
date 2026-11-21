"""Minimal Home Assistant websocket client (standard library only).

Long-term statistics are only available over the websocket API, not REST. This is just
enough of RFC 6455 to authenticate and run request/response commands, so the Docker image
needs no extra dependency.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import urllib.parse
from datetime import datetime, timezone
from typing import Any

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


def accept_key(key: str) -> str:
    """The Sec-WebSocket-Accept value a server must return for a given client key."""
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


class HomeAssistantWebSocket:
    def __init__(self, ha_url: str, token: str, timeout: float = 60.0):
        self._ha_url = ha_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 0

    def __enter__(self) -> "HomeAssistantWebSocket":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── connection ────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            self._connect()
        except BaseException:
            self.close()      # don't leak the socket when the handshake or login fails
            raise

    def _connect(self) -> None:
        url = urllib.parse.urlparse(self._ha_url)
        secure = url.scheme == "https"
        host = url.hostname
        if not host:
            raise WebSocketError("HA_URL is not a valid URL")
        port = url.port or (443 if secure else 80)
        path = url.path.rstrip("/") + "/api/websocket"

        sock = socket.create_connection((host, port), self._timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(self._timeout)
        self._sock = sock

        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())

        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            raw += chunk
        head, _, self._buf = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise WebSocketError(f"websocket upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("websocket handshake failed (bad accept key)")

        if self._recv_json().get("type") != "auth_required":
            raise WebSocketError("unexpected greeting from Home Assistant")
        self._send_json({"type": "auth", "access_token": self._token})
        reply = self._recv_json()
        if reply.get("type") != "auth_ok":
            raise WebSocketError(f"Home Assistant rejected the token: {reply.get('message', reply.get('type'))}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._send_frame(0x8, b"")
            except Exception:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ── commands ──────────────────────────────────────────────────────────

    def call(self, payload: dict[str, Any]) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        self._send_json({"id": msg_id, **payload})
        while True:
            msg = self._recv_json()
            if msg.get("id") != msg_id or msg.get("type") != "result":
                continue  # events or replies to something else
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise WebSocketError(f"{payload.get('type')} failed: {err.get('message', err)}")
            return msg.get("result")

    def statistics_during_period(self, statistic_ids: list[str], start: datetime, end: datetime,
                                 period: str = "hour", types: tuple[str, ...] = ("mean", "min", "max")) -> dict[str, list[dict]]:
        return self.call({
            "type": "recorder/statistics_during_period",
            "start_time": start.astimezone(timezone.utc).isoformat(),
            "end_time": end.astimezone(timezone.utc).isoformat(),
            "statistic_ids": list(statistic_ids),
            "period": period,
            "types": list(types),
        }) or {}

    def list_statistic_ids(self) -> list[dict]:
        return self.call({"type": "recorder/list_statistic_ids"}) or []

    # ── framing ───────────────────────────────────────────────────────────

    def _send_json(self, data: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(data).encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("not connected")
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, n: int) -> bytes:
        assert self._sock is not None
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_json(self) -> dict[str, Any]:
        message = b""
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise WebSocketError("connection closed by Home Assistant")
            if opcode == 0x9:                       # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                       # pong
                continue
            message += payload
            if fin:
                return json.loads(message.decode("utf-8"))


class HomeAssistantStatistics:
    """Source of hourly long-term statistics (what ha_stats consumes)."""

    def __init__(self, ha_url: str, token: str):
        self._ha_url, self._token = ha_url, token

    def fetch(self, statistic_ids: list[str], start: datetime, end: datetime) -> dict[str, list[dict]]:
        with HomeAssistantWebSocket(self._ha_url, self._token) as ws:
            return ws.statistics_during_period(statistic_ids, start, end)

    def statistic_ids(self) -> set[str]:
        with HomeAssistantWebSocket(self._ha_url, self._token) as ws:
            return {r.get("statistic_id") for r in ws.list_statistic_ids() if isinstance(r, dict)}
