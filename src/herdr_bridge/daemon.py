"""Background dual-stream daemon synchronizing Herdr TUI and hc terminals."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any

from .config import BridgeConfig
from .hc_client import AsyncHcRpc, HcError, subscribe_hc_events
from .herdr_client import AsyncHerdrClient, HerdrError
from .mapper import HerdrAgentReport, MappedHcUpdate, build_hc_labels, map_hc_terminal_to_herdr
from .state import BindingStore

log = logging.getLogger("herdr_bridge.daemon")

HERDR_SUBSCRIPTIONS = [
    "workspace.created",
    "workspace.updated",
    "workspace.renamed",
    "workspace.closed",
    "tab.created",
    "tab.renamed",
    "tab.closed",
    "pane.created",
    "pane.updated",
    "pane.moved",
    "pane.closed",
    "pane.exited",
]


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_daemon_pid(cfg: BridgeConfig) -> int | None:
    if not cfg.pid_path.exists():
        return None
    try:
        pid = int(cfg.pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if is_pid_alive(pid) else None


def ensure_daemon_running(cfg: BridgeConfig, wait_timeout: float = 2.0) -> int:
    """Start the background daemon if not running; safe under concurrent calls."""
    existing = read_daemon_pid(cfg)
    if existing is not None:
        return existing

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.state_dir / "daemon-start.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            existing = read_daemon_pid(cfg)
            if existing is not None:
                return existing

            log_fh = cfg.log_path.open("a", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-m", "herdr_bridge.cli", "daemon", "run"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
                close_fds=True,
            )
            deadline = time.monotonic() + wait_timeout
            while time.monotonic() < deadline:
                pid = read_daemon_pid(cfg)
                if pid is not None:
                    return pid
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            return proc.pid
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class BridgeDaemon:
    """Coordinates bidirectional metadata/lifecycle sync between Herdr and hc."""

    def __init__(
        self,
        cfg: BridgeConfig,
        *,
        herdr_client: AsyncHerdrClient | None = None,
        hc_rpc: AsyncHcRpc | None = None,
        store: BindingStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.herdr = herdr_client or AsyncHerdrClient(cfg.herdr_socket)
        self.hc = hc_rpc or AsyncHcRpc(cfg.hc_bin)
        self.store = store or BindingStore(cfg.bindings_path)

        self.workspaces: dict[str, dict[str, Any]] = {}
        self.tabs: dict[str, dict[str, Any]] = {}
        self.panes: dict[str, dict[str, Any]] = {}
        self.hc_terminals: dict[str, dict[str, Any]] = {}

        self._reported_agent: dict[str, str] = {}
        self._last_hc_state: dict[str, str] = {}
        self._last_sent_digest: dict[str, str] = {}
        self._herdr_connected = False
        self._hc_connected = False
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    def _write_status(self) -> None:
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": os.getpid(),
                "herdr_connected": self._herdr_connected,
                "hc_connected": self._hc_connected,
                "bindings": len(self.store.all_bindings()),
                "updated_ms": int(time.time() * 1000),
            }
            tmp = self.cfg.status_path.with_name(f".{self.cfg.status_path.name}.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self.cfg.status_path)
        except OSError:
            pass

    def _update_snapshot_cache(self, snap: dict[str, Any]) -> None:
        self.workspaces = {
            str(w["workspace_id"]): w
            for w in snap.get("workspaces", [])
            if isinstance(w, dict) and "workspace_id" in w
        }
        self.tabs = {
            str(t["tab_id"]): t
            for t in snap.get("tabs", [])
            if isinstance(t, dict) and "tab_id" in t
        }
        self.panes = {
            str(p["pane_id"]): p
            for p in snap.get("panes", [])
            if isinstance(p, dict) and "pane_id" in p
        }

    async def reconcile_all(self) -> None:
        """Fetch fresh snapshots from both sides and align bindings & labels."""
        try:
            snap = await self.herdr.session_snapshot()
            self._update_snapshot_cache(snap)
            self._herdr_connected = True
        except (OSError, HerdrError) as exc:
            log.warning("Herdr snapshot failed during reconcile: %s", exc)
            self._herdr_connected = False
            return

        try:
            terminals = await self.hc.list_terminals(include_all=False)
            self.hc_terminals = {str(t["address"]): t for t in terminals if "address" in t}
            self._hc_connected = True
        except (OSError, HcError) as exc:
            log.warning("hc terminal.list failed during reconcile: %s", exc)
            self._hc_connected = False
            return

        # Re-link terminals tagged with tui.pane if missing from bindings.json
        for addr, term in self.hc_terminals.items():
            labels = term.get("labels") or {}
            pane_id = labels.get("tui.pane")
            if pane_id and pane_id in self.panes and self.store.get_by_pane(pane_id) is None:
                pane = self.panes[pane_id]
                self.store.bind(
                    pane_id=pane_id,
                    hc_address=addr,
                    workspace_id=str(pane.get("workspace_id", "")),
                    tab_id=str(pane.get("tab_id", "")),
                )

        # Clean up stale bindings and push current labels/states
        for binding in self.store.all_bindings():
            if binding.pane_id not in self.panes:
                await self._handle_pane_closed(binding.pane_id)
                continue
            if binding.hc_address not in self.hc_terminals:
                self.store.unbind_pane(binding.pane_id)
                continue
            await self._sync_pane_labels_to_hc(binding.pane_id, binding.hc_address)
            await self._sync_hc_terminal_to_herdr(self.hc_terminals[binding.hc_address])

        self._write_status()

    async def _sync_pane_labels_to_hc(self, pane_id: str, hc_address: str) -> None:
        pane = self.panes.get(pane_id)
        if not pane:
            return
        ws = self.workspaces.get(str(pane.get("workspace_id", "")))
        tab = self.tabs.get(str(pane.get("tab_id", "")))
        labels = build_hc_labels(ws, tab, pane, pane_id=pane_id, attached=True)
        try:
            await self.hc.set_labels(hc_address, labels)
        except (OSError, HcError) as exc:
            log.warning("Failed to set hc labels on %s: %s", hc_address, exc)

    async def _handle_pane_closed(self, pane_id: str) -> None:
        binding = self.store.unbind_pane(pane_id)
        prev_agent = self._reported_agent.pop(pane_id, None)
        self._last_hc_state.pop(pane_id, None)
        self._last_sent_digest.pop(pane_id, None)
        if not binding:
            return
        addr = binding.hc_address
        if self.cfg.on_pane_close == "kill":
            try:
                await self.hc.kill_terminal(addr)
                log.info("Killed hc terminal %s on closed pane %s", addr, pane_id)
            except (OSError, HcError) as exc:
                log.warning("Failed to kill hc terminal %s: %s", addr, exc)
        else:
            try:
                await self.hc.set_labels(addr, {"tui.attached": "0"})
            except (OSError, HcError):
                pass
        if prev_agent:
            # Pane is already closed, ignore errors if socket rejects stale pane_id
            try:
                await self.herdr.release_agent(
                    pane_id=pane_id,
                    source=self.cfg.source_id,
                    agent=prev_agent,
                )
            except (OSError, HerdrError):
                pass

    async def handle_herdr_event(self, event_msg: dict[str, Any]) -> None:
        """Process a single event payload from Herdr's events.subscribe stream."""
        data = event_msg.get("data")
        if not isinstance(data, dict):
            return
        etype = data.get("type")

        if etype in ("workspace_created", "workspace_updated", "workspace_metadata_updated"):
            ws = data.get("workspace")
            if isinstance(ws, dict) and "workspace_id" in ws:
                ws_id = str(ws["workspace_id"])
                self.workspaces[ws_id] = ws
                await self._sync_workspace_panes(ws_id)

        elif etype == "workspace_renamed":
            ws_id = str(data.get("workspace_id", ""))
            if ws_id in self.workspaces and data.get("label") is not None:
                self.workspaces[ws_id]["label"] = data["label"]
            await self._sync_workspace_panes(ws_id)

        elif etype == "workspace_closed":
            ws_id = str(data.get("workspace_id", ""))
            self.workspaces.pop(ws_id, None)

        elif etype == "tab_created":
            tab = data.get("tab")
            if isinstance(tab, dict) and "tab_id" in tab:
                self.tabs[str(tab["tab_id"])] = tab

        elif etype == "tab_renamed":
            tab_id = str(data.get("tab_id", ""))
            if tab_id in self.tabs and data.get("label") is not None:
                self.tabs[tab_id]["label"] = data["label"]
            for b in self.store.all_bindings():
                if b.tab_id == tab_id:
                    await self._sync_pane_labels_to_hc(b.pane_id, b.hc_address)

        elif etype == "tab_closed":
            tab_id = str(data.get("tab_id", ""))
            self.tabs.pop(tab_id, None)

        elif etype in ("pane_created", "pane_updated"):
            pane = data.get("pane")
            if isinstance(pane, dict) and "pane_id" in pane:
                pid = str(pane["pane_id"])
                self.panes[pid] = pane
                b = self.store.get_by_pane(pid)
                if b:
                    await self._sync_pane_labels_to_hc(pid, b.hc_address)

        elif etype == "pane_moved":
            pane = data.get("pane")
            old_pid = data.get("previous_pane_id")
            if isinstance(pane, dict) and "pane_id" in pane:
                new_pid = str(pane["pane_id"])
                ws_id = str(pane.get("workspace_id", ""))
                tab_id = str(pane.get("tab_id", ""))
                if old_pid and str(old_pid) != new_pid:
                    self.panes.pop(str(old_pid), None)
                    migrated = self.store.migrate_pane(str(old_pid), new_pid, ws_id, tab_id)
                    if str(old_pid) in self._reported_agent:
                        self._reported_agent[new_pid] = self._reported_agent.pop(str(old_pid))
                    if str(old_pid) in self._last_hc_state:
                        self._last_hc_state[new_pid] = self._last_hc_state.pop(str(old_pid))
                    self._last_sent_digest.pop(str(old_pid), None)
                else:
                    migrated = self.store.get_by_pane(new_pid)
                self.panes[new_pid] = pane
                if migrated:
                    await self._sync_pane_labels_to_hc(new_pid, migrated.hc_address)

        elif etype == "pane_closed":
            pid = str(data.get("pane_id", ""))
            self.panes.pop(pid, None)
            if pid:
                await self._handle_pane_closed(pid)

        self._write_status()

    async def _sync_workspace_panes(self, workspace_id: str) -> None:
        for binding in self.store.all_bindings():
            pane = self.panes.get(binding.pane_id)
            ws_id = str(pane.get("workspace_id", "")) if pane else binding.workspace_id
            if ws_id == workspace_id:
                await self._sync_pane_labels_to_hc(binding.pane_id, binding.hc_address)

    async def handle_hc_event(self, evt: dict[str, Any]) -> None:
        """Process a snapshot/missed/event frame from hc."""
        kind = evt.get("type")
        if kind == "snapshot":
            result = evt.get("result", {})
            terminals = result.get("terminals", [])
            self.hc_terminals = {str(t["address"]): t for t in terminals if "address" in t}
            for term in self.hc_terminals.values():
                await self._sync_hc_terminal_to_herdr(term)
            return

        if kind == "missed":
            await self.reconcile_all()
            return

        params = evt.get("params") or {}
        if not isinstance(params, dict):
            return

        ev_type = str(params.get("type") or params.get("kind") or params.get("event") or "")
        addr = str(params.get("terminal") or params.get("address") or "")
        if ev_type == "removed" and addr:
            self.hc_terminals.pop(addr, None)
            await self._handle_hc_removed(addr)
            return

        term = self._merge_hc_event_params(params)
        if term is not None:
            await self._sync_hc_terminal_to_herdr(term)

    def _merge_hc_event_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        ev_type = str(params.get("type") or params.get("kind") or params.get("event") or "")
        data = params.get("data") if isinstance(params.get("data"), dict) else None

        # Real hc `events.subscribe` format: {"terminal": "t4", "type": "...", "data": {...}, "seq": N}
        if data is not None:
            addr = str(params.get("terminal") or data.get("address") or "")
            if not addr:
                return None
            if ev_type == "removed":
                return None
            if ev_type == "created" or ("address" in data and "state" in data and "created_ms" in data):
                term = dict(data)
                term.setdefault("address", addr)
                self.hc_terminals[addr] = term
                return term

            existing = dict(
                self.hc_terminals.get(addr, {"address": addr, "state": "live", "labels": {}})
            )
            if ev_type == "foreground":
                existing["foreground"] = data
            elif ev_type == "status":
                if "status" in data:
                    existing["status"] = data["status"]
                    if "status_ms" in data:
                        existing["status_ms"] = data["status_ms"]
                    if "reported" in data:
                        existing["reported"] = data["reported"]
                    if "turn" in data:
                        existing["turn"] = data["turn"]
                else:
                    existing["status"] = data
                    existing["reported"] = True
            elif ev_type in ("title", "labels", "keep", "turn"):
                existing.update(data)
            elif ev_type in ("finished", "lost"):
                existing["state"] = "finished"
                existing["status"] = None
                existing.update(data)
            elif ev_type == "restarted":
                existing["state"] = "live"
                existing.update(data)
            else:
                existing.update(data)

            self.hc_terminals[addr] = existing
            return existing

        # Fallback for embedded terminal object or flat delta
        if isinstance(params.get("terminal"), dict) and "address" in params["terminal"]:
            term = params["terminal"]
            addr = str(term["address"])
            self.hc_terminals[addr] = term
            return term

        addr = str(params.get("address") or params.get("terminal") or "")
        if not addr or ev_type == "removed":
            return None

        existing = dict(self.hc_terminals.get(addr, {"address": addr, "state": "live", "labels": {}}))
        for field in (
            "state",
            "status",
            "status_ms",
            "title",
            "labels",
            "name",
            "foreground",
            "turn",
            "exit_code",
            "reason",
            "reported",
        ):
            if field in params:
                existing[field] = params[field]
        if ev_type in ("finished", "lost"):
            existing["state"] = "finished"
        elif ev_type in ("created", "restarted"):
            existing["state"] = "live"
        self.hc_terminals[addr] = existing
        return existing

    async def _handle_hc_removed(self, hc_address: str) -> None:
        removed = self.store.unbind_hc(hc_address)
        for b in removed:
            prev_agent = self._reported_agent.pop(b.pane_id, None)
            self._last_hc_state.pop(b.pane_id, None)
            self._last_sent_digest.pop(b.pane_id, None)
            if prev_agent:
                try:
                    await self.herdr.release_agent(
                        pane_id=b.pane_id,
                        source=self.cfg.source_id,
                        agent=prev_agent,
                    )
                except (OSError, HerdrError):
                    pass

    async def _push_agent_report(
        self,
        pane_id: str,
        report: HerdrAgentReport,
        *,
        already_held: bool,
    ) -> None:
        """Report agent state to Herdr, claiming the pane before sending resume_argv."""
        if not already_held:
            await self.herdr.report_agent(
                pane_id=pane_id,
                source=self.cfg.source_id,
                agent=report.agent,
                state=report.state,
                message=report.message,
                seq=self.store.next_seq(pane_id),
                resume_argv=None,
            )
            self._reported_agent[pane_id] = report.agent
            if not report.resume_argv:
                return
            try:
                await self.herdr.report_agent(
                    pane_id=pane_id,
                    source=self.cfg.source_id,
                    agent=report.agent,
                    state=report.state,
                    message=report.message,
                    seq=self.store.next_seq(pane_id),
                    resume_argv=report.resume_argv,
                )
            except HerdrError as exc:
                if exc.code in ("resume_not_accepted", "invalid_resume_argv"):
                    log.debug("Ignoring optional resume_argv rejection on %s: %s", pane_id, exc)
                else:
                    raise
            return

        try:
            await self.herdr.report_agent(
                pane_id=pane_id,
                source=self.cfg.source_id,
                agent=report.agent,
                state=report.state,
                message=report.message,
                seq=self.store.next_seq(pane_id),
                resume_argv=report.resume_argv,
            )
        except HerdrError as exc:
            if exc.code != "resume_not_accepted":
                raise
            # Herdr may have restarted or dropped the hold; re-claim without resume_argv first.
            await self._push_agent_report(pane_id, report, already_held=False)

    async def _sync_hc_terminal_to_herdr(self, terminal: dict[str, Any]) -> None:
        addr = str(terminal["address"])
        binding = self.store.get_by_hc(addr)
        if binding is None:
            labels = terminal.get("labels") or {}
            pane_id = labels.get("tui.pane")
            if pane_id and (pane_id in self.panes or not self.panes):
                pane = self.panes.get(pane_id, {})
                binding = self.store.bind(
                    pane_id=pane_id,
                    hc_address=addr,
                    workspace_id=str(pane.get("workspace_id", labels.get("tui.ws", ""))),
                    tab_id=str(pane.get("tab_id", labels.get("tui.tab", ""))),
                )
            else:
                return

        pane_id = binding.pane_id
        prev_agent = self._reported_agent.get(pane_id)
        prev_hc_state = self._last_hc_state.get(pane_id)

        mapped: MappedHcUpdate = map_hc_terminal_to_herdr(
            terminal,
            error_as=self.cfg.error_as,
            prev_agent=prev_agent,
            prev_hc_state=prev_hc_state,
        )

        digest = json.dumps(
            {
                "agent": asdict(mapped.agent_report) if mapped.agent_report else None,
                "release": mapped.release_agent,
                "meta": asdict(mapped.metadata_report),
            },
            sort_keys=True,
        )
        if self._last_sent_digest.get(pane_id) == digest:
            return

        status_obj = terminal.get("status")
        if isinstance(status_obj, dict) and status_obj.get("state"):
            self._last_hc_state[pane_id] = str(status_obj["state"])
        else:
            self._last_hc_state.pop(pane_id, None)

        try:
            if mapped.agent_report is not None:
                # If agent label changed, release the old one first
                if prev_agent and prev_agent != mapped.agent_report.agent:
                    await self.herdr.release_agent(
                        pane_id=pane_id,
                        source=self.cfg.source_id,
                        agent=prev_agent,
                        seq=self.store.next_seq(pane_id),
                    )
                    self._reported_agent.pop(pane_id, None)
                await self._push_agent_report(
                    pane_id,
                    mapped.agent_report,
                    already_held=(prev_agent == mapped.agent_report.agent),
                )
            elif mapped.release_agent:
                await self.herdr.release_agent(
                    pane_id=pane_id,
                    source=self.cfg.source_id,
                    agent=mapped.release_agent,
                    seq=self.store.next_seq(pane_id),
                )
                self._reported_agent.pop(pane_id, None)

            meta = mapped.metadata_report
            await self.herdr.report_metadata(
                pane_id=pane_id,
                source=self.cfg.source_id,
                title=meta.title,
                display_agent=meta.display_agent,
                clear_display_agent=meta.display_agent is None,
                state_labels=meta.state_labels or None,
                clear_state_labels=not bool(meta.state_labels),
                tokens=meta.tokens,
                seq=self.store.next_seq(pane_id),
            )

            if mapped.notify_blocked and self.cfg.notify_on_blocked:
                ws_name = (terminal.get("labels") or {}).get("tui.ws_name") or binding.workspace_id
                title = f"Approval needed ({ws_name} / {pane_id})"
                await self.herdr.notify(title=title, body=mapped.blocked_message)

            self._last_sent_digest[pane_id] = digest
        except (OSError, HerdrError) as exc:
            log.warning("Failed pushing update to Herdr pane %s: %s", pane_id, exc)
            if isinstance(exc, HerdrError) and "not_found" in exc.code:
                await self._handle_pane_closed(pane_id)

        if not mapped.is_live:
            self.store.unbind_hc(addr)

    async def _watch_herdr_loop(self) -> None:
        backoff = 0.5
        while not self._stop_event.is_set():
            try:
                await self.reconcile_all()
                backoff = 0.5
                async for evt in self.herdr.subscribe_events(HERDR_SUBSCRIPTIONS):
                    if self._stop_event.is_set():
                        break
                    await self.handle_herdr_event(evt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._herdr_connected = False
                self._write_status()
                log.warning("Herdr event loop disconnected: %s; retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)

    async def _watch_hc_loop(self) -> None:
        backoff = 0.5
        while not self._stop_event.is_set():
            try:
                self._hc_connected = True
                backoff = 0.5
                async for evt in subscribe_hc_events(self.cfg.hc_bin):
                    if self._stop_event.is_set():
                        break
                    await self.handle_hc_event(evt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._hc_connected = False
                self._write_status()
                log.warning("hc event loop disconnected: %s; retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)

    async def run(self) -> None:
        """Run both event loops until stopped."""
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        self._write_status()
        tasks = [
            asyncio.create_task(self._watch_herdr_loop(), name="watch_herdr"),
            asyncio.create_task(self._watch_hc_loop(), name="watch_hc"),
        ]
        try:
            await self._stop_event.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.hc.close()
            try:
                self.cfg.pid_path.unlink(missing_ok=True)
                self.cfg.status_path.unlink(missing_ok=True)
            except OSError:
                pass


def run_daemon_blocking(cfg: BridgeConfig) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    daemon = BridgeDaemon(cfg)

    async def _runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, daemon.request_stop)
            except NotImplementedError:
                pass
        await daemon.run()

    asyncio.run(_runner())
    return 0
