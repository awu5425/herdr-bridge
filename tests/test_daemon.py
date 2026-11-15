"""Tests for daemon.py reconciliation and dual-stream sync."""

from pathlib import Path
from typing import Any

import pytest

from herdr_bridge.config import BridgeConfig
from herdr_bridge.daemon import BridgeDaemon
from herdr_bridge.herdr_client import HerdrError
from herdr_bridge.state import BindingStore


class FakeAsyncHerdr:
    def __init__(self, snapshot: dict[str, Any], *, enforce_hold_for_resume: bool = False) -> None:
        self.snapshot = snapshot
        self.enforce_hold_for_resume = enforce_hold_for_resume
        self.held_panes: dict[tuple[str, str], str] = {}
        self.reported_agents: list[dict[str, Any]] = []
        self.reported_meta: list[dict[str, Any]] = []
        self.released: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []

    async def session_snapshot(self) -> dict[str, Any]:
        return self.snapshot

    async def report_agent(self, **kwargs: Any) -> dict[str, Any]:
        key = (str(kwargs["pane_id"]), str(kwargs["source"]))
        agent = str(kwargs["agent"])
        if self.enforce_hold_for_resume and kwargs.get("resume_argv") is not None:
            if self.held_panes.get(key) != agent:
                raise HerdrError(
                    "resume_not_accepted",
                    "resume_argv requires the reporter to hold the pane; report its state with pane.report_agent first",
                )
        self.held_panes[key] = agent
        self.reported_agents.append(kwargs)
        return {}

    async def report_metadata(self, **kwargs: Any) -> dict[str, Any]:
        self.reported_meta.append(kwargs)
        return {}

    async def release_agent(self, **kwargs: Any) -> dict[str, Any]:
        key = (str(kwargs["pane_id"]), str(kwargs["source"]))
        self.held_panes.pop(key, None)
        self.released.append(kwargs)
        return {}

    async def notify(self, title: str, body: str | None = None) -> dict[str, Any]:
        self.notifications.append({"title": title, "body": body})
        return {}


class FakeAsyncHc:
    def __init__(self, terminals: list[dict[str, Any]]) -> None:
        self.terminals = terminals
        self.labels_set: list[tuple[str, dict[str, str]]] = []
        self.killed: list[str] = []

    async def list_terminals(self, include_all: bool = False) -> list[dict[str, Any]]:
        return list(self.terminals)

    async def set_labels(self, terminal: str, labels: dict[str, str]) -> None:
        self.labels_set.append((terminal, dict(labels)))

    async def kill_terminal(self, terminal: str) -> dict[str, Any]:
        self.killed.append(terminal)
        return {}

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_daemon_reconcile_and_sync_events(tmp_path: Path) -> None:
    cfg = BridgeConfig(state_dir=tmp_path, on_pane_close="keep")
    store = BindingStore(cfg.bindings_path)
    store.bind("w1:p1", "t7", workspace_id="w1", tab_id="w1:t1")

    snap = {
        "workspaces": [{"workspace_id": "w1", "label": "frontend"}],
        "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "Tab 1"}],
        "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "title": "editor"}],
    }
    terminals = [
        {
            "address": "t7",
            "name": "frontend-p1",
            "state": "live",
            "labels": {"tui.pane": "w1:p1"},
            "status": {"state": "working", "app": "claude", "msg": "Editing"},
            "foreground": {"name": "claude"},
        }
    ]
    herdr = FakeAsyncHerdr(snap)
    hc = FakeAsyncHc(terminals)
    daemon = BridgeDaemon(cfg, herdr_client=herdr, hc_rpc=hc, store=store)

    await daemon.reconcile_all()
    assert hc.labels_set[-1][0] == "t7"
    assert hc.labels_set[-1][1]["tui.ws_name"] == "frontend"
    assert herdr.reported_agents[-1]["state"] == "working"
    assert herdr.reported_agents[-1]["resume_argv"] == ["hb", "attach", "t7"]

    # Workspace rename pushes updated label to hc
    await daemon.handle_herdr_event(
        {"event": "workspace_renamed", "data": {"type": "workspace_renamed", "workspace_id": "w1", "label": "web-ui"}}
    )
    assert hc.labels_set[-1][1]["tui.ws_name"] == "web-ui"

    # Pane moved across workspace migrates binding ID and updates labels
    await daemon.handle_herdr_event(
        {
            "event": "pane_moved",
            "data": {
                "type": "pane_moved",
                "previous_pane_id": "w1:p1",
                "pane": {"pane_id": "w2:p4", "workspace_id": "w2", "tab_id": "w2:t1"},
            },
        }
    )
    assert store.get_by_pane("w1:p1") is None
    assert store.get_by_pane("w2:p4").hc_address == "t7"
    assert hc.labels_set[-1][1]["tui.pane"] == "w2:p4"

    # hc blocked event using real hc wire format: {"terminal": "t7", "type": "status", "data": ...}
    await daemon.handle_hc_event(
        {
            "type": "event",
            "params": {
                "terminal": "t7",
                "seq": 10,
                "type": "status",
                "data": {"state": "blocked", "kind": "permission", "msg": "Run rm?", "app": "claude"},
            },
        }
    )
    assert herdr.reported_agents[-1]["pane_id"] == "w2:p4"
    assert herdr.reported_agents[-1]["state"] == "blocked"
    assert len(herdr.notifications) == 1

    # hc title + foreground events merge cleanly
    await daemon.handle_hc_event(
        {"type": "event", "params": {"terminal": "t7", "seq": 11, "type": "title", "data": {"title": "Claude Code"}}}
    )
    assert herdr.reported_meta[-1]["title"] == "Claude Code"

    # pane_closed with on_pane_close="keep" detaches without killing t7
    await daemon.handle_herdr_event(
        {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "w2:p4", "workspace_id": "w2"}}
    )
    assert hc.killed == []
    assert hc.labels_set[-1] == ("t7", {"tui.attached": "0"})
    assert store.get_by_pane("w2:p4") is None


@pytest.mark.asyncio
async def test_daemon_kills_hc_when_policy_is_kill(tmp_path: Path) -> None:
    cfg = BridgeConfig(state_dir=tmp_path, on_pane_close="kill")
    store = BindingStore(cfg.bindings_path)
    store.bind("w1:p1", "t8", workspace_id="w1", tab_id="w1:t1")
    hc = FakeAsyncHc([])
    herdr = FakeAsyncHerdr({"workspaces": [], "tabs": [], "panes": []})
    daemon = BridgeDaemon(cfg, herdr_client=herdr, hc_rpc=hc, store=store)

    await daemon.handle_herdr_event(
        {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "w1:p1", "workspace_id": "w1"}}
    )
    assert hc.killed == ["t8"]


@pytest.mark.asyncio
async def test_daemon_claims_pane_before_sending_resume_argv(tmp_path: Path) -> None:
    cfg = BridgeConfig(state_dir=tmp_path)
    store = BindingStore(cfg.bindings_path)
    store.bind("w1:p1", "t11", workspace_id="w1", tab_id="w1:t1")

    snap = {
        "workspaces": [{"workspace_id": "w1", "label": "herdr-bridge"}],
        "tabs": [{"tab_id": "w1:t1", "workspace_id": "w1", "label": "Tab 1"}],
        "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}],
    }
    terminals = [
        {
            "address": "t11",
            "name": "herdr-bridge-p1",
            "state": "live",
            "labels": {"tui.pane": "w1:p1"},
            "status": {"app": "antigravity", "state": "working"},
        }
    ]
    herdr = FakeAsyncHerdr(snap, enforce_hold_for_resume=True)
    hc = FakeAsyncHc(terminals)
    daemon = BridgeDaemon(cfg, herdr_client=herdr, hc_rpc=hc, store=store)

    await daemon.reconcile_all()
    assert len(herdr.reported_agents) == 2
    assert herdr.reported_agents[0]["resume_argv"] is None
    assert herdr.reported_agents[1]["resume_argv"] == ["hb", "attach", "t11"]
    assert len(herdr.reported_meta) == 1
    assert herdr.reported_meta[0]["display_agent"] == "antigravity"
    assert "state_labels" not in herdr.reported_meta[0] or herdr.reported_meta[0]["state_labels"] is None

    # Simulate Herdr dropping hold externally; daemon should recover cleanly
    herdr.held_panes.clear()
    await daemon.handle_hc_event(
        {
            "type": "event",
            "params": {
                "terminal": "t11",
                "seq": 20,
                "type": "status",
                "data": {"app": "antigravity", "state": "idle"},
            },
        }
    )
    assert herdr.reported_agents[-1]["state"] == "idle"
    assert herdr.reported_agents[-1]["resume_argv"] == ["hb", "attach", "t11"]
