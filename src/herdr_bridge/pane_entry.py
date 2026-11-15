"""Pane entry logic for transparently hosting Herdr panes inside hc terminals."""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from typing import Any

from .config import BridgeConfig
from .daemon import ensure_daemon_running
from .hc_client import HcCli, HcError
from .herdr_client import HerdrClient, HerdrError
from .mapper import build_hc_labels, build_hc_name
from .state import BindingStore

DETACHED_RETURN_CODE = 10


@dataclass(slots=True)
class PaneContext:
    """Topology metadata for the current Herdr pane."""

    pane_id: str
    workspace_id: str
    tab_id: str
    workspace: dict[str, Any] | None
    tab: dict[str, Any] | None
    pane: dict[str, Any] | None


def should_auto_attach(
    cfg: BridgeConfig,
    env: dict[str, str] | None = None,
    workspace_label: str = "",
) -> tuple[bool, str]:
    """Return (allowed, reason) for automatic hc hosting inside a pane."""
    e = env if env is not None else os.environ
    if cfg.mode != "auto":
        return False, "mode is not auto"
    if e.get("HERDR_ENV") != "1":
        return False, "not inside Herdr (HERDR_ENV != 1)"
    pane_id = e.get("HERDR_PANE_ID", "").strip()
    if not pane_id:
        return False, "HERDR_PANE_ID missing"
    if e.get("HC_TERMINAL_ID", "").strip():
        return False, "already inside hc terminal"
    if e.get("HERDR_BRIDGE_DISABLE", "").strip() == "1":
        return False, "HERDR_BRIDGE_DISABLE=1"

    ws_id = e.get("HERDR_WORKSPACE_ID", "").strip()
    if ws_id in cfg.exclude_workspaces or (
        workspace_label and workspace_label in cfg.exclude_workspaces
    ):
        return False, "workspace excluded"
    return True, "ok"


def _fetch_pane_context(herdr: HerdrClient, pane_id: str, env: dict[str, str]) -> PaneContext:
    ws_id = env.get("HERDR_WORKSPACE_ID", "")
    tab_id = env.get("HERDR_TAB_ID", "")
    try:
        snap = herdr.session_snapshot()
    except (OSError, HerdrError):
        return PaneContext(pane_id=pane_id, workspace_id=ws_id, tab_id=tab_id, workspace=None, tab=None, pane=None)

    panes = {str(p["pane_id"]): p for p in snap.get("panes", []) if isinstance(p, dict) and "pane_id" in p}
    pane = panes.get(pane_id)
    if pane:
        ws_id = str(pane.get("workspace_id", ws_id))
        tab_id = str(pane.get("tab_id", tab_id))
    workspaces = {
        str(w["workspace_id"]): w for w in snap.get("workspaces", []) if isinstance(w, dict) and "workspace_id" in w
    }
    tabs = {str(t["tab_id"]): t for t in snap.get("tabs", []) if isinstance(t, dict) and "tab_id" in t}
    return PaneContext(
        pane_id=pane_id,
        workspace_id=ws_id,
        tab_id=tab_id,
        workspace=workspaces.get(ws_id),
        tab=tabs.get(tab_id),
        pane=pane,
    )


def _find_existing_hc_for_pane(
    store: BindingStore,
    hc: HcCli,
    pane_id: str,
) -> str | None:
    binding = store.get_by_pane(pane_id)
    if binding:
        term = hc.get_terminal(binding.hc_address)
        if term and term.get("state") == "live":
            return binding.hc_address
        store.unbind_pane(pane_id)

    for term in hc.list_terminals(include_finished=False):
        labels = term.get("labels") or {}
        if labels.get("tui.pane") == pane_id and "address" in term:
            return str(term["address"])
    return None


def enter_pane_session(
    cfg: BridgeConfig,
    *,
    auto: bool = True,
    target_hc: str | None = None,
    command_argv: list[str] | None = None,
    cwd: str | None = None,
    history: int = 0,
    herdr_client: HerdrClient | None = None,
    hc_cli: HcCli | None = None,
    store: BindingStore | None = None,
    start_daemon: bool = True,
) -> int:
    """Bind current Herdr pane to an hc terminal and attach interactively.

    Returns:
        0 if the inner hc terminal finished (caller hook should exit outer shell).
        DETACHED_RETURN_CODE (10) if user detached (ctrl+\\) or bridge is skipped/unavailable,
        signaling outer shell to stay open interactively.
    """
    env = dict(os.environ)
    pane_id = env.get("HERDR_PANE_ID", "").strip()

    if auto:
        allowed, _ = should_auto_attach(cfg, env=env)
        if not allowed:
            return DETACHED_RETURN_CODE

    if not pane_id:
        print("[herdr-bridge] Not inside a Herdr pane (HERDR_PANE_ID unset).", file=sys.stderr)
        return 1

    if env.get("HC_TERMINAL_ID", "").strip():
        print("[herdr-bridge] Already inside an hc terminal; skipping nested attach.", file=sys.stderr)
        return DETACHED_RETURN_CODE

    herdr = herdr_client or HerdrClient(cfg.herdr_socket)
    hc = hc_cli or HcCli(cfg.hc_bin)
    bindings = store or BindingStore(cfg.bindings_path)

    ctx = _fetch_pane_context(herdr, pane_id, env)
    ws_label = str((ctx.workspace or {}).get("label") or ctx.workspace_id)
    if auto:
        allowed, _ = should_auto_attach(cfg, env=env, workspace_label=ws_label)
        if not allowed:
            return DETACHED_RETURN_CODE

    if start_daemon:
        try:
            ensure_daemon_running(cfg)
        except OSError as exc:
            print(f"[herdr-bridge] Warning: could not start daemon: {exc}", file=sys.stderr)

    try:
        hc_address: str | None = None
        if target_hc:
            term = hc.get_terminal(target_hc)
            if not term or term.get("state") != "live":
                print(f"[herdr-bridge] hc terminal '{target_hc}' not found or not live.", file=sys.stderr)
                return 1
            hc_address = str(term["address"])
            labels = build_hc_labels(ctx.workspace, ctx.tab, ctx.pane, pane_id=pane_id, attached=True)
            hc.set_labels(hc_address, labels)
        else:
            if not command_argv:
                hc_address = _find_existing_hc_for_pane(bindings, hc, pane_id)
            if not hc_address:
                live_terms = hc.list_terminals(include_finished=False)
                existing_names = {str(t["name"]) for t in live_terms if t.get("name")}
                name = build_hc_name(
                    ws_label=ws_label,
                    ws_id=ctx.workspace_id or "ws",
                    pane_id=pane_id,
                    existing_names=existing_names,
                    template=cfg.name_template,
                )
                labels = build_hc_labels(ctx.workspace, ctx.tab, ctx.pane, pane_id=pane_id, attached=True)
                created = hc.run_bg_terminal(
                    cwd=cwd or os.getcwd(),
                    name=name,
                    labels=labels,
                    keep=cfg.hc_keep,
                    argv=command_argv,
                    extra_env={"HERDR_BRIDGE_DISABLE": "1"},
                )
                hc_address = str(created["address"])
            else:
                labels = build_hc_labels(ctx.workspace, ctx.tab, ctx.pane, pane_id=pane_id, attached=True)
                hc.set_labels(hc_address, labels)

        bindings.bind(
            pane_id=pane_id,
            hc_address=hc_address,
            workspace_id=ctx.workspace_id,
            tab_id=ctx.tab_id,
        )
    except (OSError, HcError) as exc:
        print(f"[herdr-bridge] hc unavailable ({exc}); falling back to local shell.", file=sys.stderr)
        return DETACHED_RETURN_CODE

    hc.attach_interactive(hc_address, history=history)

    # Check if inner terminal ended or user detached via ctrl+\
    try:
        after = hc.get_terminal(hc_address)
    except HcError:
        after = None

    if after and after.get("state") == "live":
        try:
            hc.set_labels(hc_address, {"tui.attached": "0"})
        except HcError:
            pass
        print(
            f"\n[herdr-bridge] Detached from hc {hc_address} (still running in background). "
            f"Run 'hb attach {hc_address}' to return, or 'exit' to close pane.",
            file=sys.stderr,
        )
        return DETACHED_RETURN_CODE

    bindings.unbind_pane(pane_id)
    return 0
