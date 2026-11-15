"""Configuration management for herdr-bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

DEFAULT_SOURCE_ID = "herdr-bridge"


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "herdr-bridge" / "config.toml"


def default_state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "herdr-bridge"


def default_herdr_socket() -> Path:
    env_sock = os.environ.get("HERDR_SOCKET_PATH")
    if env_sock:
        return Path(env_sock)
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "herdr" / "herdr.sock"


@dataclass(slots=True)
class BridgeConfig:
    """Runtime settings loaded from ~/.config/herdr-bridge/config.toml."""

    mode: str = "auto"  # "auto" | "opt-in"
    on_pane_close: str = "keep"  # "keep" | "kill"
    hc_keep: bool = True
    notify_on_blocked: bool = True
    error_as: str = "blocked"  # "blocked" | "unknown"
    name_template: str = "{ws_slug}-{pane_short}"
    exclude_workspaces: list[str] = field(default_factory=list)
    herdr_bin: str = field(default_factory=lambda: os.environ.get("HERDR_BIN_PATH", "herdr"))
    hc_bin: str = "hc"
    herdr_socket: Path = field(default_factory=default_herdr_socket)
    state_dir: Path = field(default_factory=default_state_dir)
    source_id: str = DEFAULT_SOURCE_ID

    @property
    def bindings_path(self) -> Path:
        return self.state_dir / "bindings.json"

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "daemon.pid"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "daemon.log"

    @property
    def status_path(self) -> Path:
        return self.state_dir / "daemon-status.json"


def load_config(path: Path | None = None) -> BridgeConfig:
    """Load BridgeConfig from TOML if present, falling back to defaults."""
    cfg_path = path or default_config_path()
    cfg = BridgeConfig()
    if not cfg_path.exists():
        return cfg

    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    if "mode" in raw and raw["mode"] in ("auto", "opt-in"):
        cfg.mode = raw["mode"]
    if "on_pane_close" in raw and raw["on_pane_close"] in ("keep", "kill"):
        cfg.on_pane_close = raw["on_pane_close"]
    if "hc_keep" in raw:
        cfg.hc_keep = bool(raw["hc_keep"])
    if "notify_on_blocked" in raw:
        cfg.notify_on_blocked = bool(raw["notify_on_blocked"])
    if "error_as" in raw and raw["error_as"] in ("blocked", "unknown"):
        cfg.error_as = raw["error_as"]
    if "name_template" in raw and isinstance(raw["name_template"], str):
        cfg.name_template = raw["name_template"]
    if "exclude_workspaces" in raw and isinstance(raw["exclude_workspaces"], list):
        cfg.exclude_workspaces = [str(x) for x in raw["exclude_workspaces"]]
    if "herdr_bin" in raw and isinstance(raw["herdr_bin"], str):
        cfg.herdr_bin = raw["herdr_bin"]
    if "hc_bin" in raw and isinstance(raw["hc_bin"], str):
        cfg.hc_bin = raw["hc_bin"]
    if "herdr_socket" in raw and isinstance(raw["herdr_socket"], str):
        cfg.herdr_socket = Path(os.path.expanduser(raw["herdr_socket"]))
    if "state_dir" in raw and isinstance(raw["state_dir"], str):
        cfg.state_dir = Path(os.path.expanduser(raw["state_dir"]))
    if "source_id" in raw and isinstance(raw["source_id"], str):
        cfg.source_id = raw["source_id"]

    return cfg
