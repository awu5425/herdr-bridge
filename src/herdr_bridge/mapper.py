"""Pure mapping logic between Herdr topology and hc terminal state/labels."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

_HC_NAME_DISALLOWED = re.compile(r"[^a-z0-9_-]+")
_MULTI_DASH = re.compile(r"[-_]{2,}")
# Herdr 0.9.5 ignores pane.report_agent lifecycle updates for Antigravity aliases
# (expecting native screen detection, which hc attach obscures); report as "hc"
# while keeping the real name in display_agent metadata.
_HERDR_REPORT_AGENT_FALLBACKS = frozenset({"agy", "antigravity", "antigravity-cli"})


@dataclass(slots=True)
class HerdrAgentReport:
    """Mapped agent lifecycle payload for Herdr pane.report_agent."""

    agent: str
    state: str  # "idle" | "working" | "blocked" | "unknown"
    message: str | None
    resume_argv: list[str]


@dataclass(slots=True)
class HerdrMetadataReport:
    """Mapped display-only metadata payload for Herdr pane.report_metadata."""

    title: str | None
    display_agent: str | None
    state_labels: dict[str, str]
    tokens: dict[str, str]


@dataclass(slots=True)
class MappedHcUpdate:
    """Combined update derived from an hc Terminal object."""

    hc_address: str
    is_live: bool
    agent_report: HerdrAgentReport | None
    release_agent: str | None
    metadata_report: HerdrMetadataReport
    notify_blocked: bool
    blocked_message: str | None


def slugify_hc_component(text: str, fallback: str = "ws") -> str:
    """Sanitize arbitrary text into lowercase [a-z0-9_-] suitable for hc names."""
    cleaned = _HC_NAME_DISALLOWED.sub("-", text.strip().lower())
    cleaned = _MULTI_DASH.sub("-", cleaned).strip("-_")
    return cleaned or fallback


def build_hc_name(
    ws_label: str,
    ws_id: str,
    pane_id: str,
    existing_names: set[str],
    template: str = "{ws_slug}-{pane_short}",
) -> str:
    """Generate a unique <=32 char hc terminal name matching [a-z0-9_-]{1,32}."""
    ws_slug = slugify_hc_component(ws_label, fallback=slugify_hc_component(ws_id, "ws"))
    pane_short = slugify_hc_component(pane_id.split(":")[-1], fallback="pane")
    ws_id_slug = slugify_hc_component(ws_id, fallback="ws")

    try:
        base = template.format(ws_slug=ws_slug, pane_short=pane_short, ws_id=ws_id_slug)
    except (KeyError, ValueError):
        base = f"{ws_slug}-{pane_short}"

    base = slugify_hc_component(base, fallback=f"{ws_id_slug}-{pane_short}")
    candidate = base[:32].strip("-_") or "pane"
    if candidate not in existing_names:
        return candidate

    for idx in range(2, 100):
        suffix = f"-{idx}"
        trimmed = base[: 32 - len(suffix)].strip("-_") or "p"
        cand = f"{trimmed}{suffix}"
        if cand not in existing_names:
            return cand
    return f"p-{pane_short}"[:32]


def build_hc_labels(
    workspace: dict[str, Any] | None,
    tab: dict[str, Any] | None,
    pane: dict[str, Any] | None,
    pane_id: str,
    attached: bool = True,
) -> dict[str, str]:
    """Build tui.* labels for an hc terminal from Herdr topology objects."""
    ws_id = str((workspace or {}).get("workspace_id") or (pane or {}).get("workspace_id") or "")
    ws_name = str((workspace or {}).get("label") or ws_id)
    tab_id = str((tab or {}).get("tab_id") or (pane or {}).get("tab_id") or "")
    tab_name = str((tab or {}).get("label") or tab_id)
    pane_title = str(
        (pane or {}).get("label")
        or (pane or {}).get("title")
        or (pane or {}).get("terminal_title_stripped")
        or ""
    )

    labels: dict[str, str] = {
        "tui.ws": ws_id,
        "tui.ws_name": ws_name,
        "tui.tab": tab_id,
        "tui.tab_name": tab_name,
        "tui.pane": pane_id,
        "tui.attached": "1" if attached else "0",
    }
    if pane_title:
        labels["tui.pane_title"] = pane_title[:128]
    return labels


def _resolve_report_agent(agent_label: str) -> str:
    """Return a Herdr-accepted pane.report_agent label for lifecycle ownership."""
    cleaned = slugify_hc_component(agent_label, fallback="hc")[:32]
    if not cleaned or not cleaned[0].isalpha() or cleaned in _HERDR_REPORT_AGENT_FALLBACKS:
        return "hc"
    return cleaned


def _resolve_agent_label(terminal: dict[str, Any]) -> str:
    status = terminal.get("status")
    if isinstance(status, dict) and status.get("app"):
        return str(status["app"])
    fg = terminal.get("foreground")
    if isinstance(fg, dict) and fg.get("name"):
        return str(fg["name"])
    if terminal.get("template"):
        return str(terminal["template"])
    return "hc"


def map_hc_terminal_to_herdr(
    terminal: dict[str, Any],
    error_as: str = "blocked",
    prev_agent: str | None = None,
    prev_hc_state: str | None = None,
) -> MappedHcUpdate:
    """Translate an hc Terminal dict into Herdr agent/metadata updates."""
    addr = str(terminal["address"])
    is_live = terminal.get("state") == "live"
    status = terminal.get("status") if isinstance(terminal.get("status"), dict) else None
    fg = terminal.get("foreground") if isinstance(terminal.get("foreground"), dict) else None

    hc_name = terminal.get("name") or addr
    fg_name = str(fg["name"]) if fg and fg.get("name") else None
    term_title = str(terminal.get("title") or "").strip()

    tokens: dict[str, str] = {
        "hc_addr": addr,
        "hc_state": str(terminal.get("state") or "unknown"),
    }
    if terminal.get("name"):
        tokens["hc_name"] = str(terminal["name"])[:64]
    if fg_name:
        tokens["hc_fg"] = fg_name[:64]

    if not is_live:
        metadata = HerdrMetadataReport(
            title=f"[hc:{hc_name} exited]",
            display_agent=None,
            state_labels={},
            tokens=tokens,
        )
        return MappedHcUpdate(
            hc_address=addr,
            is_live=False,
            agent_report=None,
            release_agent=prev_agent,
            metadata_report=metadata,
            notify_blocked=False,
            blocked_message=None,
        )

    display_title = term_title or f"[hc:{hc_name}]"
    resume_argv = ["hb", "attach", addr]

    if status is None:
        metadata = HerdrMetadataReport(
            title=display_title,
            display_agent=None,
            state_labels={},
            tokens=tokens,
        )
        return MappedHcUpdate(
            hc_address=addr,
            is_live=True,
            agent_report=None,
            release_agent=prev_agent,
            metadata_report=metadata,
            notify_blocked=False,
            blocked_message=None,
        )

    raw_state = str(status.get("state") or "unknown")
    tokens["hc_agent_state"] = raw_state
    agent_label = _resolve_agent_label(terminal)

    if raw_state == "working":
        herdr_state = "working"
    elif raw_state == "blocked":
        herdr_state = "blocked"
    elif raw_state in ("idle", "done"):
        herdr_state = "idle"
    elif raw_state == "error":
        herdr_state = error_as if error_as in ("blocked", "unknown") else "blocked"
    else:
        herdr_state = "unknown"

    msg_parts: list[str] = []
    if status.get("kind"):
        msg_parts.append(str(status["kind"]))
    if status.get("msg"):
        msg_parts.append(str(status["msg"]))
    if status.get("progress") is not None:
        msg_parts.append(f"{status['progress']}%")
    message = " · ".join(msg_parts) if msg_parts else None

    state_labels: dict[str, str] = {}
    if raw_state == "done":
        state_labels["idle"] = "Done (hc)"
    elif raw_state == "error":
        state_labels[herdr_state] = "Error (hc)"
    elif raw_state == "blocked" and status.get("kind"):
        state_labels["blocked"] = f"Blocked: {status['kind']}"

    status_title = str(status.get("title") or "").strip()
    if status_title:
        display_title = status_title

    agent_report = HerdrAgentReport(
        agent=_resolve_report_agent(agent_label),
        state=herdr_state,
        message=message,
        resume_argv=resume_argv,
    )
    metadata = HerdrMetadataReport(
        title=display_title,
        display_agent=agent_label,
        state_labels=state_labels,
        tokens=tokens,
    )

    newly_blocked = raw_state == "blocked" and prev_hc_state != "blocked"
    return MappedHcUpdate(
        hc_address=addr,
        is_live=True,
        agent_report=agent_report,
        release_agent=None,
        metadata_report=metadata,
        notify_blocked=newly_blocked,
        blocked_message=message or f"{agent_label} requires attention in hc {hc_name}",
    )
