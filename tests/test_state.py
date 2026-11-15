"""Tests for state.py."""

from pathlib import Path

from herdr_bridge.state import BindingStore


def test_binding_store_roundtrip_migrate_and_unbind(tmp_path: Path) -> None:
    store = BindingStore(tmp_path / "bindings.json")
    b1 = store.bind("w1:p1", "t10", workspace_id="w1", tab_id="w1:t1")
    assert b1.pane_id == "w1:p1"
    assert store.get_by_hc("t10").pane_id == "w1:p1"

    assert store.next_seq("w1:p1") == 1
    assert store.next_seq("w1:p1") == 2

    # Binding another pane to the same hc_address replaces the stale pane binding
    store.bind("w1:p2", "t10", workspace_id="w1", tab_id="w1:t1")
    assert store.get_by_pane("w1:p1") is None
    assert store.get_by_pane("w1:p2").hc_address == "t10"

    # Migrating pane across workspaces preserves hc_address and last_seq
    migrated = store.migrate_pane("w1:p2", "w2:p5", workspace_id="w2", tab_id="w2:t1")
    assert migrated is not None
    assert migrated.pane_id == "w2:p5"
    assert migrated.hc_address == "t10"
    assert migrated.workspace_id == "w2"
    assert store.get_by_pane("w1:p2") is None

    removed = store.unbind_hc("t10")
    assert len(removed) == 1
    assert removed[0].pane_id == "w2:p5"
    assert store.all_bindings() == []
