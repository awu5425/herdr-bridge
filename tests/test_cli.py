"""Tests for cli.py helpers and listing."""

from __future__ import annotations

import json
from pathlib import Path

from herdr_bridge import cli
from herdr_bridge.config import BridgeConfig
from herdr_bridge.state import BindingStore


def test_extract_hc_state_handles_str_and_dict_status() -> None:
    assert cli._extract_hc_state({"state": "live", "status": "working"}) == "working"
    assert cli._extract_hc_state({"state": "live", "status": {"state": "blocked"}}) == "blocked"
    assert cli._extract_hc_state({"state": "live", "status": None}) == "live"
    assert cli._extract_hc_state({}) == "missing"


def test_cmd_ls_json_with_string_status(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = BridgeConfig(state_dir=tmp_path)
    store = BindingStore(cfg.bindings_path)
    store.bind("wH:p1", "t11", workspace_id="wH", tab_id="wH:t1")

    class FakeHerdr:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def session_snapshot(self) -> dict:
            return {
                "workspaces": [{"workspace_id": "wH", "label": "herdr-bridge"}],
                "tabs": [{"tab_id": "wH:t1", "label": "main"}],
                "panes": [{"pane_id": "wH:p1", "workspace_id": "wH", "tab_id": "wH:t1"}],
            }

    class FakeHc:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def list_terminals(self, include_finished: bool = False) -> list[dict]:
            return [
                {
                    "address": "t11",
                    "name": "herdr-bridge-p1",
                    "state": "live",
                    "status": "working",
                }
            ]

    monkeypatch.setattr(cli, "HerdrClient", FakeHerdr)
    monkeypatch.setattr(cli, "HcCli", FakeHc)

    rc = cli._cmd_ls(cfg, as_json=True)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["bindings"] == [
        {
            "workspace_id": "wH",
            "workspace": "herdr-bridge",
            "tab": "main",
            "pane_id": "wH:p1",
            "hc_address": "t11",
            "hc_name": "herdr-bridge-p1",
            "hc_state": "working",
        }
    ]
