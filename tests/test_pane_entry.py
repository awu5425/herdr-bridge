"""Tests for pane_entry.py."""

from pathlib import Path
from typing import Any

from herdr_bridge.config import BridgeConfig
from herdr_bridge.pane_entry import DETACHED_RETURN_CODE, enter_pane_session, should_auto_attach
from herdr_bridge.state import BindingStore


class FakeHerdrClient:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    def session_snapshot(self) -> dict[str, Any]:
        return self._snapshot


class FakeHcCli:
    def __init__(self, initial_terms: list[dict[str, Any]] | None = None, state_after_attach: str = "live") -> None:
        self.terminals = {t["address"]: dict(t) for t in (initial_terms or [])}
        self.state_after_attach = state_after_attach
        self.run_calls: list[dict[str, Any]] = []
        self.attach_calls: list[str] = []
        self.label_calls: list[tuple[str, dict[str, str]]] = []

    def list_terminals(self, include_finished: bool = False) -> list[dict[str, Any]]:
        return list(self.terminals.values())

    def get_terminal(self, target: str) -> dict[str, Any] | None:
        return self.terminals.get(target)

    def set_labels(self, target: str, labels: dict[str, str]) -> None:
        self.label_calls.append((target, dict(labels)))
        if target in self.terminals:
            self.terminals[target].setdefault("labels", {}).update(labels)

    def run_bg_terminal(self, **kwargs: Any) -> dict[str, Any]:
        self.run_calls.append(kwargs)
        term = {
            "address": "t99",
            "name": kwargs.get("name"),
            "state": "live",
            "labels": dict(kwargs.get("labels") or {}),
        }
        self.terminals["t99"] = term
        return term

    def attach_interactive(self, target: str, history: int = 0) -> int:
        self.attach_calls.append(target)
        if target in self.terminals:
            self.terminals[target]["state"] = self.state_after_attach
        return 0


def test_should_auto_attach_guards() -> None:
    cfg = BridgeConfig(mode="auto", exclude_workspaces=["scratch"])
    assert should_auto_attach(cfg, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}) == (True, "ok")
    assert should_auto_attach(cfg, {"HERDR_ENV": "0", "HERDR_PANE_ID": "w1:p1"})[0] is False
    assert should_auto_attach(cfg, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HC_TERMINAL_ID": "t3"})[0] is False
    assert should_auto_attach(cfg, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1", "HERDR_BRIDGE_DISABLE": "1"})[0] is False
    assert should_auto_attach(cfg, {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"}, workspace_label="scratch")[0] is False


def test_enter_pane_creates_hc_and_handles_detach_vs_exit(tmp_path: Path, monkeypatch) -> None:
    cfg = BridgeConfig(state_dir=tmp_path)
    store = BindingStore(cfg.bindings_path)
    snap = {
        "workspaces": [{"workspace_id": "w1", "label": "app"}],
        "tabs": [{"tab_id": "w1:t1", "label": "main"}],
        "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}],
    }
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w1")
    monkeypatch.setenv("HERDR_TAB_ID", "w1:t1")
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    monkeypatch.delenv("HERDR_BRIDGE_DISABLE", raising=False)

    # Case 1: ctrl+\ detach leaves hc live and returns DETACHED_RETURN_CODE
    hc_detach = FakeHcCli(state_after_attach="live")
    rc = enter_pane_session(
        cfg,
        auto=True,
        herdr_client=FakeHerdrClient(snap),
        hc_cli=hc_detach,
        store=store,
        start_daemon=False,
    )
    assert rc == DETACHED_RETURN_CODE
    assert hc_detach.attach_calls == ["t99"]
    assert hc_detach.run_calls[0]["extra_env"] == {"HERDR_BRIDGE_DISABLE": "1"}
    assert store.get_by_pane("w1:p1").hc_address == "t99"
    assert ("t99", {"tui.attached": "0"}) in hc_detach.label_calls

    # Case 2: Re-entering same pane re-attaches existing t99, then exiting shell returns 0 and unbinds
    hc_detach.state_after_attach = "finished"
    rc2 = enter_pane_session(
        cfg,
        auto=True,
        herdr_client=FakeHerdrClient(snap),
        hc_cli=hc_detach,
        store=store,
        start_daemon=False,
    )
    assert rc2 == 0
    assert len(hc_detach.run_calls) == 1  # did not create a second terminal
    assert store.get_by_pane("w1:p1") is None
