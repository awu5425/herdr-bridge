"""Tests for mapper.py."""

from herdr_bridge.mapper import (
    build_hc_labels,
    build_hc_name,
    map_hc_terminal_to_herdr,
    slugify_hc_component,
)


def test_slugify_and_build_hc_name_handles_unicode_and_collisions() -> None:
    assert slugify_hc_component("测试项目", fallback="w1") == "w1"
    assert slugify_hc_component("My API / v2!", fallback="ws") == "my-api-v2"

    existing = {"api-p1", "api-p1-2"}
    name = build_hc_name("API", "w1", "w1:p1", existing)
    assert name == "api-p1-3"
    assert len(name) <= 32

    long_name = build_hc_name("very-long-workspace-name-exceeding-limit", "w1", "w1:p12", {"very-long-workspace-name-exc-p12"})
    assert len(long_name) <= 32


def test_build_hc_labels_uses_tui_namespace() -> None:
    labels = build_hc_labels(
        workspace={"workspace_id": "w2", "label": "后端服务"},
        tab={"tab_id": "w2:t1", "label": "Agents"},
        pane={"pane_id": "w2:p3", "title": "claude"},
        pane_id="w2:p3",
        attached=True,
    )
    assert labels == {
        "tui.ws": "w2",
        "tui.ws_name": "后端服务",
        "tui.tab": "w2:t1",
        "tui.tab_name": "Agents",
        "tui.pane": "w2:p3",
        "tui.attached": "1",
        "tui.pane_title": "claude",
    }


def test_map_hc_terminal_states() -> None:
    # Plain shell with no OSC 7501 status does not claim agent authority
    plain = {
        "address": "t5",
        "name": "ws-p1",
        "state": "live",
        "title": "bash",
        "status": None,
        "foreground": {"name": "bash"},
    }
    res_plain = map_hc_terminal_to_herdr(plain)
    assert res_plain.agent_report is None
    assert res_plain.release_agent is None
    assert res_plain.metadata_report.tokens["hc_addr"] == "t5"

    # Blocked agent triggers blocked state + notification on transition
    blocked = {
        "address": "t5",
        "name": "ws-p1",
        "state": "live",
        "title": "Claude",
        "status": {
            "state": "blocked",
            "kind": "permission",
            "msg": "Allow bash tool?",
            "app": "claude",
        },
        "foreground": {"name": "claude"},
    }
    res_blocked = map_hc_terminal_to_herdr(blocked, prev_hc_state="working")
    assert res_blocked.agent_report is not None
    assert res_blocked.agent_report.agent == "claude"
    assert res_blocked.agent_report.state == "blocked"
    assert res_blocked.agent_report.resume_argv == ["hb", "attach", "t5"]
    assert res_blocked.notify_blocked is True

    # Already blocked should not re-notify
    res_still_blocked = map_hc_terminal_to_herdr(blocked, prev_hc_state="blocked")
    assert res_still_blocked.notify_blocked is False

    # Done maps to idle with state_label
    done = {
        "address": "t5",
        "name": "ws-p1",
        "state": "live",
        "status": {"state": "done", "app": "claude", "msg": "Finished"},
    }
    res_done = map_hc_terminal_to_herdr(done, prev_hc_state="working")
    assert res_done.agent_report is not None
    assert res_done.agent_report.state == "idle"
    assert res_done.metadata_report.state_labels["idle"] == "Done (hc)"

    # Finished terminal releases previous agent
    finished = {"address": "t5", "name": "ws-p1", "state": "finished"}
    res_fin = map_hc_terminal_to_herdr(finished, prev_agent="claude")
    assert res_fin.is_live is False
    assert res_fin.agent_report is None
    assert res_fin.release_agent == "claude"

    # Antigravity aliases report lifecycle ownership as "hc" while preserving display_agent
    agy = {
        "address": "t11",
        "name": "herdr-bridge-p1",
        "state": "live",
        "status": {"state": "working", "app": "antigravity"},
    }
    res_agy = map_hc_terminal_to_herdr(agy)
    assert res_agy.agent_report is not None
    assert res_agy.agent_report.agent == "hc"
    assert res_agy.agent_report.state == "working"
    assert res_agy.metadata_report.display_agent == "antigravity"
