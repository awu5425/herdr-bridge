"""Sync and async Unix socket clients for the Herdr server API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
from typing import Any, AsyncIterator
import uuid


class HerdrError(RuntimeError):
    """Error returned by the Herdr Unix socket API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _decode_response(line: bytes) -> dict[str, Any]:
    payload = json.loads(line.decode("utf-8"))
    if "error" in payload:
        err = payload["error"]
        raise HerdrError(str(err.get("code", "unknown")), str(err.get("message", "")))
    return payload.get("result", {})


class HerdrClient:
    """Blocking one-request-per-connection client for Herdr's Unix socket."""

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req = {
            "id": uuid.uuid4().hex[:12],
            "method": method,
            "params": params or {},
        }
        raw = (json.dumps(req) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(str(self.socket_path))
            sock.sendall(raw)
            buf = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    line = bytes(buf.split(b"\n", 1)[0])
                    return _decode_response(line)
        raise HerdrError("eof", f"Herdr socket closed before responding to {method}")

    def ping(self) -> dict[str, Any]:
        return self.call("ping")

    def session_snapshot(self) -> dict[str, Any]:
        res = self.call("session.snapshot")
        return res.get("snapshot", res)


class AsyncHerdrClient:
    """Async Unix socket client for Herdr RPCs and event subscriptions."""

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req = {
            "id": uuid.uuid4().hex[:12],
            "method": method,
            "params": params or {},
        }
        raw = (json.dumps(req) + "\n").encode("utf-8")
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path)),
            timeout=self.timeout,
        )
        try:
            writer.write(raw)
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not line:
                raise HerdrError("eof", f"Herdr socket closed before responding to {method}")
            return _decode_response(line)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def ping(self) -> dict[str, Any]:
        return await self.call("ping")

    async def session_snapshot(self) -> dict[str, Any]:
        res = await self.call("session.snapshot")
        return res.get("snapshot", res)

    async def report_agent(
        self,
        *,
        pane_id: str,
        source: str,
        agent: str,
        state: str,
        message: str | None = None,
        seq: int | None = None,
        resume_argv: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pane_id": pane_id,
            "source": source,
            "agent": agent,
            "state": state,
        }
        if message is not None:
            params["message"] = message
        if seq is not None:
            params["seq"] = seq
        if resume_argv is not None:
            params["resume_argv"] = resume_argv
        return await self.call("pane.report_agent", params)

    async def report_metadata(
        self,
        *,
        pane_id: str,
        source: str,
        title: str | None = None,
        display_agent: str | None = None,
        clear_display_agent: bool = False,
        state_labels: dict[str, str] | None = None,
        clear_state_labels: bool = False,
        tokens: dict[str, str | None] | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pane_id": pane_id,
            "source": source,
        }
        if title is not None:
            params["title"] = title
        if display_agent is not None:
            params["display_agent"] = display_agent
        if clear_display_agent:
            params["clear_display_agent"] = True
        if state_labels is not None:
            params["state_labels"] = state_labels
        if clear_state_labels:
            params["clear_state_labels"] = True
        if tokens is not None:
            params["tokens"] = tokens
        if seq is not None:
            params["seq"] = seq
        return await self.call("pane.report_metadata", params)

    async def release_agent(
        self,
        *,
        pane_id: str,
        source: str,
        agent: str,
        seq: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pane_id": pane_id,
            "source": source,
            "agent": agent,
        }
        if seq is not None:
            params["seq"] = seq
        return await self.call("pane.release_agent", params)

    async def notify(self, title: str, body: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"title": title}
        if body is not None:
            params["body"] = body
        return await self.call("notification.show", params)

    async def subscribe_events(self, subscription_types: list[str]) -> AsyncIterator[dict[str, Any]]:
        req = {
            "id": uuid.uuid4().hex[:12],
            "method": "events.subscribe",
            "params": {"subscriptions": [{"type": t} for t in subscription_types]},
        }
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path)),
            timeout=self.timeout,
        )
        try:
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            ack_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not ack_line:
                raise HerdrError("eof", "Herdr socket closed before subscription ack")
            _decode_response(ack_line)

            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line.decode("utf-8"))
                if "event" in msg:
                    yield msg
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
