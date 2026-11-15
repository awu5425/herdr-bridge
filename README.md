# herdr-bridge (`hb`)

Transparent bridge uniting **Herdr TUI** (local workspace/tab/pane layout & keyboard ergonomics) with **`hc` / `herdr-core`** (persistent PTY runtime, OSC 7501 status, and mobile/remote streaming).

## Why `herdr-bridge`?

- **Unified PTY**: Every Herdr pane auto-hosts an interactive `hc` terminal (`hc run --bg --keep` + `hc attach`), so local Herdr TUI and mobile/remote `hc` clients share the exact same terminal byte stream and accept bidirectional input.
- **Herdr → `hc` Sync**: Workspace/tab/pane topology, renames, moves, and closes are synced to `hc` terminal names and `tui.*` labels (`tui.ws`, `tui.ws_name`, `tui.tab`, `tui.tab_name`, `tui.pane`, `tui.pane_title`, `tui.attached`).
- **`hc` → Herdr Sync**: `hc` OSC 7501 status (`idle`, `working`, `blocked`, `done`, `error`) and foreground metadata are pushed back to Herdr's sidebar (`pane.report_agent`, `pane.report_metadata`, `pane.release_agent`, and `notification.show`).
- **Safe Escape Hatches**: Recursion guards (`HERDR_ENV=1`, `HERDR_PANE_ID`, `!HC_TERMINAL_ID`, `HERDR_BRIDGE_DISABLE!=1`) and automatic fallback to a local shell ensure a pane is never locked out; `ctrl+\` detaches back to the outer shell (`hb attach <addr>` to re-enter), while inner `exit` closes the pane cleanly.
- **Zero Third-Party Runtime Dependencies**: Pure Python 3.12 stdlib + `asyncio`.

## Requirements

- Python `>= 3.12`
- `herdr` TUI / server (tested against `0.9.5`)
- `hc` / `herdr-core` (tested against `0.1.17`)

## Installation

Using `uv` (recommended):

```bash
git clone https://github.com/awu5425/herdr-bridge.git
cd herdr-bridge
uv tool install --editable .
```

Or with `pip` in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Enable the guarded shell hook (`~/.bashrc` or `~/.zshrc`) and verify connectivity:

```bash
hb install-hook
hb doctor
```

## Usage

```bash
hb install-hook         # Enable auto-hosting in ~/.bashrc or ~/.zshrc
hb uninstall-hook       # Remove the shell hook
hb doctor               # Verify herdr.sock, hc core, daemon, and hook
hb ls                   # List active Herdr pane <-> hc terminal bindings
hb ls --json            # Machine-readable binding list
hb attach <hc_addr>     # Re-attach current pane to an existing hc terminal
hb run -- <cmd>         # Run a command in a new hc terminal bound to this pane
hb daemon start|stop|status|logs
```

## Configuration

Optional configuration lives at `~/.config/herdr-bridge/config.toml`:

```toml
mode = "auto"                 # "auto" | "opt-in"
on_pane_close = "keep"        # "keep" | "kill"
hc_keep = true                # pass --keep to hc run
notify_on_blocked = true      # show Herdr notification on blocked state
error_as = "blocked"          # "blocked" | "unknown"
name_template = "{ws_slug}-{pane_short}"
exclude_workspaces = []       # workspace IDs or labels to skip in auto mode
```

## Development

```bash
uv run --with pytest --with pytest-asyncio pytest -v
ruff check src tests
```

## License

[MIT](LICENSE)
