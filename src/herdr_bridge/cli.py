"""Command-line interface for herdr-bridge (`hb`)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import time

from . import __version__
from .config import BridgeConfig, default_config_path, load_config
from .daemon import ensure_daemon_running, is_pid_alive, read_daemon_pid, run_daemon_blocking
from .hc_client import HcCli, HcError
from .herdr_client import HerdrClient, HerdrError
from .pane_entry import DETACHED_RETURN_CODE, enter_pane_session
from .state import BindingStore

HOOK_START = "# >>> herdr-bridge initialize >>>"
HOOK_END = "# <<< herdr-bridge initialize <<<"


def _hook_snippet(hb_bin: str = "hb") -> str:
    return f"""# Auto-host interactive Herdr panes inside persistent hc terminals
if [[ $- == *i* ]] && [[ "${{HERDR_ENV:-}}" == "1" ]] && [[ -n "${{HERDR_PANE_ID:-}}" ]] && [[ -z "${{HC_TERMINAL_ID:-}}" ]] && [[ "${{HERDR_BRIDGE_DISABLE:-}}" != "1" ]]; then
  if command -v {hb_bin} >/dev/null 2>&1; then
    {hb_bin} pane-entry
    _hb_rc=$?
    if [[ $_hb_rc -eq 0 ]]; then
      exit 0
    fi
    unset _hb_rc
  fi
fi
"""


def _rc_file_for_shell(shell: str) -> Path:
    if shell == "zsh":
        return Path.home() / ".zshrc"
    return Path.home() / ".bashrc"


def _install_hook(shell: str, hb_bin: str) -> int:
    rc_path = _rc_file_for_shell(shell)
    existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    block = f'{HOOK_START}\neval "$({hb_bin} hook-init {shell})"\n{HOOK_END}\n'

    if HOOK_START in existing and HOOK_END in existing:
        start_idx = existing.index(HOOK_START)
        end_idx = existing.index(HOOK_END) + len(HOOK_END)
        if end_idx < len(existing) and existing[end_idx : end_idx + 1] == "\n":
            end_idx += 1
        updated = existing[:start_idx] + block + existing[end_idx:]
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        updated = existing + sep + block

    rc_path.write_text(updated, encoding="utf-8")
    print(f"Installed herdr-bridge hook into {rc_path}")
    return 0


def _uninstall_hook(shell: str) -> int:
    rc_path = _rc_file_for_shell(shell)
    if not rc_path.exists():
        print(f"No {rc_path} found.")
        return 0
    existing = rc_path.read_text(encoding="utf-8")
    if HOOK_START not in existing or HOOK_END not in existing:
        print(f"Hook not present in {rc_path}")
        return 0
    start_idx = existing.index(HOOK_START)
    end_idx = existing.index(HOOK_END) + len(HOOK_END)
    if end_idx < len(existing) and existing[end_idx : end_idx + 1] == "\n":
        end_idx += 1
    updated = existing[:start_idx] + existing[end_idx:]
    rc_path.write_text(updated, encoding="utf-8")
    print(f"Removed herdr-bridge hook from {rc_path}")
    return 0


def _extract_hc_state(term: dict) -> str:
    """Return agent or terminal state from either TerminalSummary (str status) or Terminal (dict status)."""
    raw_status = term.get("status")
    if isinstance(raw_status, dict):
        agent_state = raw_status.get("state")
    elif isinstance(raw_status, str):
        agent_state = raw_status
    else:
        agent_state = None
    return str(agent_state or term.get("state") or "missing")


def _cmd_ls(cfg: BridgeConfig, as_json: bool) -> int:
    herdr = HerdrClient(cfg.herdr_socket)
    hc = HcCli(cfg.hc_bin)
    store = BindingStore(cfg.bindings_path)

    workspaces: dict[str, dict] = {}
    tabs: dict[str, dict] = {}
    panes: dict[str, dict] = {}
    try:
        snap = herdr.session_snapshot()
        workspaces = {str(w["workspace_id"]): w for w in snap.get("workspaces", [])}
        tabs = {str(t["tab_id"]): t for t in snap.get("tabs", [])}
        panes = {str(p["pane_id"]): p for p in snap.get("panes", [])}
    except (OSError, HerdrError):
        pass

    try:
        terms = {str(t["address"]): t for t in hc.list_terminals(include_finished=False)}
    except (OSError, HcError):
        terms = {}

    rows = []
    for b in store.all_bindings():
        pane = panes.get(b.pane_id, {})
        ws = workspaces.get(str(pane.get("workspace_id", b.workspace_id)), {})
        tab = tabs.get(str(pane.get("tab_id", b.tab_id)), {})
        term = terms.get(b.hc_address, {})
        rows.append(
            {
                "workspace_id": ws.get("workspace_id", b.workspace_id),
                "workspace": ws.get("label", b.workspace_id),
                "tab": tab.get("label", b.tab_id),
                "pane_id": b.pane_id,
                "hc_address": b.hc_address,
                "hc_name": term.get("name") or "",
                "hc_state": _extract_hc_state(term),
            }
        )

    if as_json:
        print(json.dumps({"bindings": rows}, indent=2))
        return 0

    if not rows:
        print("No active herdr-bridge pane bindings.")
        return 0

    header = f"{'WORKSPACE':<18} {'TAB':<12} {'PANE':<10} {'HC':<6} {'HC_NAME':<24} {'STATE':<10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{str(r['workspace'])[:18]:<18} "
            f"{str(r['tab'])[:12]:<12} "
            f"{str(r['pane_id'])[:10]:<10} "
            f"{str(r['hc_address'])[:6]:<6} "
            f"{str(r['hc_name'])[:24]:<24} "
            f"{str(r['hc_state'])[:10]:<10}"
        )
    return 0


def _cmd_doctor(cfg: BridgeConfig) -> int:
    ok = True
    herdr_bin = shutil.which(cfg.herdr_bin) or (cfg.herdr_bin if Path(cfg.herdr_bin).exists() else None)
    hc_bin = shutil.which(cfg.hc_bin) or (cfg.hc_bin if Path(cfg.hc_bin).exists() else None)
    print(f"herdr-bridge : {__version__}")
    print(f"config       : {default_config_path()} ({'exists' if default_config_path().exists() else 'defaults'})")
    print(f"mode         : {cfg.mode} (on_pane_close={cfg.on_pane_close})")
    print(f"herdr bin    : {herdr_bin or 'MISSING'}")
    print(f"hc bin       : {hc_bin or 'MISSING'}")

    try:
        pong = HerdrClient(cfg.herdr_socket).ping()
        print(f"herdr socket : OK ({cfg.herdr_socket}, v{pong.get('version')})")
    except (OSError, HerdrError) as exc:
        print(f"herdr socket : FAIL ({cfg.herdr_socket}: {exc})")
        ok = False

    try:
        st = HcCli(cfg.hc_bin).server_status()
        print(f"hc server    : OK ({st.get('core', {}).get('name', 'running')})")
    except (OSError, HcError) as exc:
        print(f"hc server    : FAIL ({exc})")
        ok = False

    pid = read_daemon_pid(cfg)
    print(f"daemon       : {'running (pid ' + str(pid) + ')' if pid else 'stopped'}")
    if cfg.status_path.exists():
        try:
            ds = json.loads(cfg.status_path.read_text(encoding="utf-8"))
            print(
                f"daemon sync  : herdr={ds.get('herdr_connected')} "
                f"hc={ds.get('hc_connected')} bindings={ds.get('bindings')}"
            )
        except (OSError, json.JSONDecodeError):
            pass

    rc = _rc_file_for_shell("zsh" if "zsh" in os.environ.get("SHELL", "") else "bash")
    hooked = rc.exists() and HOOK_START in rc.read_text(encoding="utf-8")
    print(f"shell hook   : {'installed' if hooked else 'not installed'} ({rc})")
    return 0 if ok else 1


def _cmd_daemon(cfg: BridgeConfig, action: str) -> int:
    if action == "run":
        return run_daemon_blocking(cfg)
    if action == "start":
        pid = ensure_daemon_running(cfg)
        print(f"herdr-bridge daemon running (pid {pid})")
        return 0
    if action == "status":
        pid = read_daemon_pid(cfg)
        if pid is None:
            print("herdr-bridge daemon is stopped.")
            return 1
        print(f"herdr-bridge daemon running (pid {pid})")
        if cfg.status_path.exists():
            print(cfg.status_path.read_text(encoding="utf-8"))
        return 0
    if action == "stop":
        pid = read_daemon_pid(cfg)
        if pid is None:
            print("herdr-bridge daemon is already stopped.")
            return 0
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and is_pid_alive(pid):
            time.sleep(0.05)
        if is_pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        print(f"Stopped herdr-bridge daemon (pid {pid}).")
        return 0
    if action == "logs":
        if not cfg.log_path.exists():
            print(f"No daemon log at {cfg.log_path}")
            return 0
        lines = cfg.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-80:]:
            print(line)
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hb",
        description="Bridge Herdr TUI workspaces/panes with hc persistent terminals.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("pane-entry", help="Internal shell-hook entrypoint for Herdr panes")

    p_hook = sub.add_parser("hook-init", help="Print shell hook snippet")
    p_hook.add_argument("shell", choices=["bash", "zsh"], nargs="?", default="bash")

    p_inst = sub.add_parser("install-hook", help="Install shell hook into ~/.bashrc or ~/.zshrc")
    p_inst.add_argument("--shell", choices=["bash", "zsh"], default="zsh" if "zsh" in os.environ.get("SHELL", "") else "bash")
    p_inst.add_argument("--bin", default="hb")

    p_uninst = sub.add_parser("uninstall-hook", help="Remove shell hook from ~/.bashrc or ~/.zshrc")
    p_uninst.add_argument("--shell", choices=["bash", "zsh"], default="zsh" if "zsh" in os.environ.get("SHELL", "") else "bash")

    p_att = sub.add_parser("attach", help="Attach current Herdr pane to an hc terminal")
    p_att.add_argument("target", nargs="?", help="hc terminal address or name (e.g. t3)")
    p_att.add_argument("--history", type=int, default=0)

    p_run = sub.add_parser("run", help="Run a command inside a new hc terminal bound to this pane")
    p_run.add_argument("argv", nargs=argparse.REMAINDER, help="Command and arguments")

    p_ls = sub.add_parser("ls", help="List Herdr pane <-> hc terminal bindings")
    p_ls.add_argument("--json", action="store_true")

    sub.add_parser("status", help="Show bridge health and status")
    sub.add_parser("doctor", help="Diagnose Herdr, hc, daemon, and shell hook")

    p_daemon = sub.add_parser("daemon", help="Manage the background sync daemon")
    p_daemon.add_argument("action", choices=["start", "stop", "status", "run", "logs"])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()

    if args.cmd == "pane-entry":
        return enter_pane_session(cfg, auto=True)
    if args.cmd == "hook-init":
        print(_hook_snippet(), end="")
        return 0
    if args.cmd == "install-hook":
        return _install_hook(args.shell, args.bin)
    if args.cmd == "uninstall-hook":
        return _uninstall_hook(args.shell)
    if args.cmd == "attach":
        rc = enter_pane_session(cfg, auto=False, target_hc=args.target, history=args.history)
        return 0 if rc == DETACHED_RETURN_CODE else rc
    if args.cmd == "run":
        cmd_argv = list(args.argv)
        if cmd_argv and cmd_argv[0] == "--":
            cmd_argv = cmd_argv[1:]
        rc = enter_pane_session(cfg, auto=False, command_argv=cmd_argv or None)
        return 0 if rc == DETACHED_RETURN_CODE else rc
    if args.cmd == "ls":
        return _cmd_ls(cfg, as_json=args.json)
    if args.cmd in ("status", "doctor"):
        return _cmd_doctor(cfg)
    if args.cmd == "daemon":
        return _cmd_daemon(cfg, args.action)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
