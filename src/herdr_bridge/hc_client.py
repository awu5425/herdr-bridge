"""Sync CLI and async JSON-RPC clients for hc (herdr-core)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any, AsyncIterator


class HcError(RuntimeError):
    """Error raised when communicating with hc."""


def _clean_hc_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    return env


class HcCli:
    """Synchronous subprocess wrapper around hc --json commands."""

    def __init__(self, hc_bin: str = "hc") -> None:
        self.hc_bin = hc_bin

    def _run_json(self, args: list[str], extra_env: dict[str, str] | None = None) -> Any:
        cmd = [self.hc_bin, *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_clean_hc_env(extra_env),
            check=False,
        )
        if proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise HcError(f"{' '.join(cmd)} failed: {msg}")
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    def server_status(self) -> dict[str, Any]:
        return self._run_json(["server", "status", "--json"])

    def list_terminals(self, include_finished: bool = False) -> list[dict[str, Any]]:
        args = ["ls", "--json"]
        if include_finished:
            args.append("--all")
        res = self._run_json(args)
        if isinstance(res, dict):
            return list(res.get("terminals", []))
        return list(res) if isinstance(res, list) else []

    def get_terminal(self, target: str) -> dict[str, Any] | None:
        try:
            res = self._run_json(["get", "--json", target])
        except HcError:
            return None
        if isinstance(res, dict) and "terminal" in res and isinstance(res["terminal"], dict):
            return res["terminal"]
        return res if isinstance(res, dict) else None

    def set_labels(self, target: str, labels: dict[str, str]) -> None:
        pairs = [f"{k}={v}" for k, v in labels.items()]
        if not pairs:
            return
        self._run_json(["label", "--json", target, *pairs])

    def run_bg_terminal(
        self,
        *,
        cwd: str,
        name: str | None = None,
        labels: dict[str, str] | None = None,
        keep: bool = True,
        argv: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        args = ["run", "--bg", "--json", "--no-parent", "--outlive-parent", "--cwd", cwd]
        if keep:
            args.append("--keep")
        if name:
            args.extend(["--name", name])
        for k, v in (labels or {}).items():
            args.extend(["--label", f"{k}={v}"])
        if argv:
            args.append("--")
            args.extend(argv)
        res = self._run_json(args, extra_env=extra_env)
        if isinstance(res, dict) and "terminal" in res and isinstance(res["terminal"], dict):
            return res["terminal"]
        return res if isinstance(res, dict) else {}

    def attach_interactive(self, target: str, history: int = 0) -> int:
        args = [self.hc_bin, "attach"]
        if history > 0:
            args.extend(["--history", str(history)])
        args.append(target)
        proc = subprocess.run(args, check=False)
        return proc.returncode


class AsyncHcRpc:
    """Persistent JSON-RPC 2.0 client using `hc connect` over stdio."""

    def __init__(self, hc_bin: str = "hc") -> None:
        self.hc_bin = hc_bin
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1

    async def _ensure_connected(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        self._proc = await asyncio.create_subprocess_exec(
            self.hc_bin,
            "connect",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._send_locked("hello", {})
        return self._proc

    async def _send_locked(self, method: str, params: dict[str, Any]) -> Any:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        req_id = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=10.0)
            if not line:
                raise HcError(f"hc connect closed during {method}")
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                err = msg["error"]
                raise HcError(f"{err.get('code')}: {err.get('message')}")
            return msg.get("result", {})

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            try:
                await self._ensure_connected()
                return await self._send_locked(method, params or {})
            except (OSError, asyncio.TimeoutError, HcError):
                await self.close()
                await self._ensure_connected()
                return await self._send_locked(method, params or {})

    async def list_terminals(self, include_all: bool = False) -> list[dict[str, Any]]:
        res = await self.call("terminal.list", {"all": include_all})
        return list(res.get("terminals", []))

    async def set_labels(self, terminal: str, labels: dict[str, str]) -> None:
        if not labels:
            return
        await self.call("terminal.label", {"terminal": terminal, "labels": labels})

    async def kill_terminal(self, terminal: str) -> dict[str, Any]:
        return await self.call("terminal.kill", {"terminal": terminal})

    async def close(self) -> None:
        if self._proc is not None:
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
            self._proc = None


async def subscribe_hc_events(hc_bin: str = "hc") -> AsyncIterator[dict[str, Any]]:
    """Open a dedicated `hc connect` stream subscribed to terminal events."""
    proc = await asyncio.create_subprocess_exec(
        hc_bin,
        "connect",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    try:
        hello_req = {"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {}}
        sub_req = {"jsonrpc": "2.0", "id": 2, "method": "events.subscribe", "params": {}}
        proc.stdin.write(
            (json.dumps(hello_req) + "\n" + json.dumps(sub_req) + "\n").encode("utf-8")
        )
        await proc.stdin.drain()

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") == 1:
                continue
            if msg.get("id") == 2:
                if "error" in msg:
                    raise HcError(str(msg["error"]))
                yield {"type": "snapshot", "result": msg.get("result", {})}
                continue
            method = msg.get("method")
            if method == "heartbeat":
                continue
            if method == "missed":
                yield {"type": "missed", "params": msg.get("params", {})}
                continue
            if method == "event":
                yield {"type": "event", "params": msg.get("params", {})}
                continue
            # Defensive fallback in case hc emits direct notification shapes
            if "params" in msg:
                yield {"type": str(method or "event"), "params": msg["params"]}
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
