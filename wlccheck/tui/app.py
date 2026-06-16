from __future__ import annotations

import csv
import datetime as _dt
import os
import pathlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Deque, Dict, List, Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Command, Provider
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Rule,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection

from ..core.diff import DiffSummary, compute_diff
from ..core.dnac import (
    PortInfo as DNACPortInfo,
    get_client as get_dnac_client,
    is_dnac_configured,
    set_inventory_host as set_dnac_host,
)
from ..core.inventory import (
    DNACEntry,
    InventoryError,
    WLCEntry,
    find_inventory,
    load_dnac_entries,
    load_inventory,
)
from ..core.models import (
    APRecord,
    APStats,
    APStateEvent,
    ClientRecord,
    ClientStats,
    LivePollRecord,
    Run,
    WLCResult,
)
from ..core.restconf import WLCAuthError, WLCClient
from ..core.storage import SnapshotDB
from .upgrade_screen import PredownloadScreen


# ===========================================================================
# Inter-thread messages
# ===========================================================================

class StatusUpdate(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class RunComplete(Message):
    def __init__(self, run: Run, run_uuid: str) -> None:
        super().__init__()
        self.run = run
        self.run_uuid = run_uuid


class CollectionError(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class LivePollBatchComplete(Message):
    """Posted from the live-poll worker when all WLCs have been queried."""
    def __init__(self, poll_num: int, results: Dict[str, List[APRecord]]) -> None:
        super().__init__()
        self.poll_num = poll_num
        self.results  = results   # {wlc_name: records}


# ===========================================================================
# Command palette providers
# ===========================================================================

class WLCCheckCommands(Provider):
    """Commands available in the Ctrl+P palette."""

    async def search(self, query: str) -> Command:  # type: ignore[override]
        screen: MainScreen = self.screen  # type: ignore[assignment]
        commands = [
            ("New Snapshot",          "c",  screen.action_snapshot),
            ("Post-Check",            "p",  screen.action_post_check),
            ("EIU Upgrade",           "u",  screen.action_upgrade),
            ("Cycle Severity Filter", "f",  screen.action_filter_diff),
            ("Toggle Changed Only",   "o",  screen.action_toggle_unchanged),
            ("Filter: All",           None, lambda: screen._set_filter("all")),
            ("Filter: Warning+",      None, lambda: screen._set_filter("warning")),
            ("Filter: Critical only", None, lambda: screen._set_filter("critical")),
        ]
        q = query.lower()
        for name, key, cb in commands:
            if q in name.lower():
                hint = f"  [{key}]" if key else ""
                yield self.make_match(
                    candidate=self.Hit(
                        score=1.0,
                        match_display=f"{name}{hint}",
                        command=cb,
                        text=name,
                    )
                )


# ===========================================================================
# Rich formatting helpers
# ===========================================================================

# Style shorthands — single source of truth for repeated style strings
_S_GREEN  = "bold green"
_S_RED    = "bold red"
_S_YELLOW = "bold yellow"
_S_CYAN   = "bold cyan"

# Shared CSS path used by all modal screens
_CSS = "styles.tcss"

# Widget IDs referenced from multiple methods
_WID_ERROR_BAR   = "#error-bar"
_WID_PW_INPUT    = "#password-input"
_WID_LABEL_INPUT = "#label-input"
_WID_WLC_SEL     = "#wlc-selection"

# Column header strings used in both on_mount table setup and CSV export
_COL_PREV_POLL  = "PREV POLL"
_COL_CHANGED_AT = "CHANGED AT"
_COL_NOT_JOINED = "NOT JOINED"
_COL_POLL_NUM   = "POLL #"
_COL_PRE_STATE  = "PRE STATE"
_COL_MAC_ETH    = "MAC ETH"
_CLS_COPYABLE   = "ap-detail-val copyable-val"
_COL_POST_STATE = "POST STATE"

_STATE_STYLE: dict[str, tuple[str, str]] = {
    "joined":      ("●", _S_GREEN),
    "not_joined":  ("✕", _S_RED),
    "downloading": ("⟳", _S_YELLOW),
    "reset":       ("↺", "yellow"),
    "discovery":   ("⌕", "cyan"),
    "standby":     ("◌", "blue"),
    "other":       ("?", "dim"),
}

_CHANGE_FMT: dict[str, tuple[str, str]] = {
    "lost":         ("▼", _S_RED),
    "disappeared":  ("✕", _S_RED),
    "degraded":     ("↓", _S_YELLOW),
    "state_change": ("~", "yellow"),
    "tag_change":   ("⊘", _S_YELLOW),
    "wlc_move":     ("⇄", _S_YELLOW),
    "recovered":    ("▲", _S_GREEN),
    "new":          ("★", _S_CYAN),
    "down":         ("▼", _S_RED),
    "up":           ("▲", _S_GREEN),
    "client_drop":  ("▼", _S_YELLOW),
    "client_gain":  ("▲", "cyan"),
    "removed":      ("✕", _S_RED),
    "lost_ip":      ("✕", _S_RED),
    "got_ip":       ("✓", _S_GREEN),
    "new_no_ip":    ("⚠", _S_YELLOW),
}

_SEV_FMT: dict[str, tuple[str, str]] = {
    "critical": ("⛔", _S_RED),
    "warning":  ("⚠ ", _S_YELLOW),
    "info":     ("ℹ ", _S_CYAN),
}

_FILTER_CYCLE = ("all", "warning", "critical")
_FILTER_LABEL = {
    "all":      "Filter: All",
    "warning":  "Filter: Warning+",
    "critical": "Filter: Critical",
}


def _state_label(state: Optional[str]) -> str:
    """Plain-string state label, safe for CSV and Rich-free contexts."""
    return state.replace("_", " ").title() if state else "—"


def _live_ap_state_texts(
    ap: "APRecord",
    is_missing: bool,
    baseline: "Optional[APRecord]",
    prev: "Optional[APRecord]",
) -> "tuple[Text, Text, Text]":
    bas_txt  = _fmt_state(baseline.state) if baseline else Text("—", style="dim")
    prev_txt = _fmt_state(prev.state)     if prev     else Text("—", style="dim")
    if is_missing:
        cur_txt = Text("✕ Not Joined", style=_S_RED)
        cur_txt.append("  [gone]", style="dim red")
    else:
        cur_txt = _fmt_state(ap.state)
        if baseline and baseline.state != ap.state:
            cur_txt.stylize("bold")
    return bas_txt, prev_txt, cur_txt


def _live_ap_changed(
    is_missing: bool,
    moved: bool,
    baseline: "Optional[APRecord]",
    prev: "Optional[APRecord]",
    eff_state: str,
) -> bool:
    return (
        moved
        or is_missing
        or (baseline is not None and baseline.state != eff_state)
        or (prev is not None and prev.state != eff_state)
    )


def _live_ap_mobility_texts(
    bl_wlc: "Optional[str]",
    cur_wlc: "Optional[str]",
    moved: bool,
) -> "tuple[Text, Text, Text]":
    if moved:
        return (
            Text("→",             style=_S_YELLOW),
            Text(bl_wlc,          style="dim"),         # type: ignore[arg-type]
            Text(cur_wlc,         style=_S_YELLOW),  # type: ignore[arg-type]
        )
    return (
        Text("—",             style="dim"),
        Text(bl_wlc  or "—",  style="dim"),
        Text(cur_wlc or "—",  style="dim"),
    )


def _fmt_state(state: str) -> Text:
    icon, style = _STATE_STYLE.get(state, ("?", "dim"))
    t = Text()
    t.append(f"{icon} ", style=style)
    t.append(_state_label(state), style=style)
    return t


def _fmt_change(change_type: str) -> Text:
    icon, style = _CHANGE_FMT.get(change_type, ("~", "dim"))
    t = Text()
    t.append(f"{icon} ", style=style)
    t.append(change_type.replace("_", " ").title(), style=style)
    return t


def _fmt_severity(severity: str) -> Text:
    icon, style = _SEV_FMT.get(severity, ("?", "dim"))
    t = Text()
    t.append(f"{icon} ", style=style)
    t.append(severity.title(), style=style)
    return t


def _fmt_state_or_dash(state: Optional[str]) -> Text:
    return Text("—", style="dim") if state is None else _fmt_state(state)


def _colored_count(n: int, color: str) -> Text:
    return Text(str(n), style=f"bold {color}" if n > 0 else "dim")


def _apply_tags(records: "List[APRecord]", tags_map: dict, client: "WLCClient") -> None:
    """Merge tag data from the ap-tags API into AP records (non-empty values win)."""
    for r in records:
        norm = client._norm_mac(r.wtp_mac)
        if norm not in tags_map:
            continue
        p, s, rf = tags_map[norm]
        if p:
            r.policy_tag = p
        if s:
            r.site_tag = s
        if rf:
            r.rf_tag = rf


def _collect_wlc_entry(
    entry: "WLCEntry",
    username: str,
    password: str,
    collect_clients: bool,
    status_cb,
) -> "WLCResult":
    """Collect data from a single WLC. Designed to run inside a thread pool."""
    prefix = f"\\[{entry.name}]"
    try:
        status_cb(f"{prefix} Connecting to [bold]{entry.host}[/bold]…")
        client   = WLCClient(entry.host, username, password)
        hostname = client.get_hostname()
        if hostname:
            status_cb(f"{prefix} [green]✓[/green] {hostname}")

        records = client.get_ap_data(status_cb=lambda m: status_cb(f"{prefix} {m}"))
        for r in records:
            r.wlc_name = entry.name

        tags_map = client.get_ap_tags(status_cb=lambda m: status_cb(f"{prefix} {m}"))
        _apply_tags(records, tags_map, client)

        wlans = client.get_wlans(status_cb=lambda m: status_cb(f"{prefix} {m}"))
        for w in wlans:
            w.wlc_name = entry.name

        clients: List[ClientRecord] = []
        if collect_clients:
            clients = client.get_clients(status_cb=lambda m: status_cb(f"{prefix} {m}"))
            for c in clients:
                c.wlc_name = entry.name

        stats        = APStats.from_records(records)
        client_stats = ClientStats.from_records(clients)
        status_cb(
            f"{prefix} [green]✓[/green] Done — "
            f"{stats.total} APs  "
            f"([green]{stats.joined}[/green] joined)"
            + (f"  {len(clients)} clients" if collect_clients else "")
        )
        return WLCResult(
            wlc_name=entry.name, wlc_host=entry.host,
            wlc_hostname=hostname, records=records,
            stats=stats, ok=True,
            wlans=wlans, clients=clients,
            client_stats=client_stats,
        )
    except Exception as exc:
        tag = "Auth error" if isinstance(exc, WLCAuthError) else "Error"
        status_cb(f"{prefix} [bold red]✕ {tag}:[/bold red] {exc}")
        return WLCResult(
            wlc_name=entry.name, wlc_host=entry.host,
            wlc_hostname=None, records=[], stats=APStats(),
            ok=False, error=str(exc),
        )


def _fmt_tags(tags: tuple[str, str, str]) -> str:
    p, s, r = tags
    parts = []
    if p:
        parts.append(f"P:{p}")
    if s:
        parts.append(f"S:{s}")
    if r:
        parts.append(f"R:{r}")
    return "  ".join(parts) if parts else "—"


# ===========================================================================
# Credential modal
# ===========================================================================

class CredentialModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS_PATH = _CSS

    def __init__(self, prefill_user: str = "") -> None:
        super().__init__()
        self._prefill_user = prefill_user

    def compose(self) -> ComposeResult:
        with Vertical(id="cred-dialog"):
            yield Label("  Authentication", classes="dialog-title")
            yield Rule(classes="dialog-rule")
            yield Label("Username", classes="input-label")
            yield Input(value=self._prefill_user, placeholder="tacacs-username", id="username-input")
            yield Label("Password", classes="input-label")
            yield Input(placeholder="••••••••", password=True, id="password-input")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel",     classes="cancel-btn",  id="cancel-btn")
                yield Button("Continue →", classes="primary-btn", id="confirm-btn")

    def on_mount(self) -> None:
        self.query_one(
            _WID_PW_INPUT if self._prefill_user else "#username-input", Input
        ).focus()

    @on(Button.Pressed, "#confirm-btn")
    def _confirm(self) -> None:
        u = self.query_one("#username-input", Input).value.strip()
        p = self.query_one(_WID_PW_INPUT, Input).value
        if u and p:
            self.dismiss((u, p))

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        if event.input.id == "username-input":
            self.query_one(_WID_PW_INPUT, Input).focus()
        else:
            self._confirm()


# ===========================================================================
# Run options modal  (label + collect clients checkbox)
# ===========================================================================

class RunOptionsModal(ModalScreen):
    """Set run label and choose optional data collection."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS_PATH = _CSS

    def __init__(self, prefill_label: str = "", title: str = "Run Options") -> None:
        super().__init__()
        self._prefill_label = prefill_label
        self._title = title

    _INTERVALS = [("60 seconds", 60), ("2 minutes", 120), ("5 minutes", 300), ("10 minutes", 600)]

    def compose(self) -> ComposeResult:
        with Vertical(id="connect-dialog"):
            yield Label(f"  {self._title}", classes="dialog-title")
            yield Rule(classes="dialog-rule")
            yield Label("Label  (optional)", classes="input-label")
            yield Label("e.g.  pre-firewall-upgrade", classes="input-hint")
            yield Input(
                value=self._prefill_label,
                placeholder="leave blank to auto-generate",
                id="label-input",
            )
            yield Rule(classes="dialog-rule")
            yield Label("Data Collection", classes="input-label")
            yield Checkbox(
                "Baseline  (AP state · tags · WLANs)",
                value=True,
                id="cb-baseline",
            )
            yield Checkbox(
                "Clients  ⚠  slow on large infrastructure",
                value=False,
                id="cb-clients",
            )
            yield Rule(classes="dialog-rule")
            yield Label("Live AP Monitor", classes="input-label")
            yield Checkbox(
                "AP Live View  ·  poll APs after collection",
                value=False,
                id="cb-live",
            )
            yield Label("Poll interval  (minimum 60 s)", classes="input-hint")
            yield Select(self._INTERVALS, value=120, id="live-interval", allow_blank=False)
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel",  classes="cancel-btn",  id="cancel-btn")
                yield Button("Start →", classes="primary-btn", id="confirm-btn")

    def on_mount(self) -> None:
        self.query_one(_WID_LABEL_INPUT, Input).focus()

    @on(Button.Pressed, "#confirm-btn")
    def _confirm(self) -> None:
        label           = self.query_one(_WID_LABEL_INPUT, Input).value.strip() or None
        collect_clients = self.query_one("#cb-clients",  Checkbox).value
        cb_live         = self.query_one("#cb-live",     Checkbox).value
        sel             = self.query_one("#live-interval", Select)
        live_interval   = int(sel.value) if cb_live and sel.value != Select.BLANK else 0
        self.dismiss((label, collect_clients, live_interval))

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _on_submit(self, _: Input.Submitted) -> None:
        self._confirm()


# ===========================================================================
# Single-WLC connect modal
# ===========================================================================

class ConnectModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS_PATH = _CSS

    def __init__(self, prefill_host: str = "", prefill_label: str = "") -> None:
        super().__init__()
        self._prefill_host  = prefill_host
        self._prefill_label = prefill_label

    def compose(self) -> ComposeResult:
        with Vertical(id="connect-dialog"):
            yield Label("  Connect to WLC", classes="dialog-title")
            yield Rule(classes="dialog-rule")
            yield Label("WLC Hostname / IP Address", classes="input-label")
            yield Input(value=self._prefill_host,  placeholder="10.0.0.1", id="host-input")
            yield Label("Session Label  (optional)", classes="input-label")
            yield Label("e.g.  pre-maintenance-fw-upgrade", classes="input-hint")
            yield Input(value=self._prefill_label, placeholder="leave blank to auto-generate", id="label-input")
            yield Rule(classes="dialog-rule")
            yield Label("Data Collection", classes="input-label")
            yield Checkbox("Baseline  (AP state · tags · WLANs)", value=True,  id="cb-baseline")
            yield Checkbox("Clients  ⚠  slow on large infrastructure", value=False, id="cb-clients")
            yield Rule(classes="dialog-rule")
            yield Label("Live AP Monitor", classes="input-label")
            yield Checkbox("AP Live View  ·  poll APs after collection", value=False, id="cb-live")
            yield Label("Poll interval  (minimum 60 s)", classes="input-hint")
            yield Select(
                [("60 seconds", 60), ("2 minutes", 120), ("5 minutes", 300), ("10 minutes", 600)],
                value=60, id="live-interval", allow_blank=False,
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel",    classes="cancel-btn",  id="cancel-btn")
                yield Button("Connect →", classes="primary-btn", id="confirm-btn")

    def on_mount(self) -> None:
        self.query_one(
            _WID_LABEL_INPUT if self._prefill_host else "#host-input", Input
        ).focus()

    @on(Button.Pressed, "#confirm-btn")
    def _confirm(self) -> None:
        host  = self.query_one("#host-input",  Input).value.strip()
        label = self.query_one(_WID_LABEL_INPUT, Input).value.strip() or None
        collect_clients = self.query_one("#cb-clients", Checkbox).value
        cb_live         = self.query_one("#cb-live",    Checkbox).value
        sel             = self.query_one("#live-interval", Select)
        live_interval   = int(sel.value) if cb_live and sel.value != Select.BLANK else 0
        if host:
            self.dismiss((host, label, collect_clients, live_interval))

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        if event.input.id == "host-input":
            self.query_one(_WID_LABEL_INPUT, Input).focus()
        else:
            self._confirm()


# ===========================================================================
# WLC picker modal
# ===========================================================================

class WLCPickerModal(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel",      "Cancel"),
        Binding("a",      "select_all",  "All"),
        Binding("n",      "select_none", "None"),
    ]
    CSS_PATH = _CSS

    def __init__(self, entries: List[WLCEntry]) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Label("  Select WLCs", classes="dialog-title")
            yield Label(
                "Space to toggle  ·  \\[a] all  ·  \\[n] none",
                classes="input-hint",
            )
            yield Rule(classes="dialog-rule")
            yield SelectionList(
                *[Selection(str(e), e.host, initial_state=True) for e in self._entries],
                id="wlc-selection",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel",  classes="cancel-btn",  id="cancel-btn")
                yield Button("Next →",  classes="primary-btn", id="confirm-btn")

    def on_mount(self) -> None:
        self.query_one(_WID_WLC_SEL, SelectionList).focus()

    @on(Button.Pressed, "#confirm-btn")
    def _confirm(self) -> None:
        sl = self.query_one(_WID_WLC_SEL, SelectionList)
        chosen = [e for e in self._entries if e.host in set(sl.selected)]
        if chosen:
            self.dismiss(chosen)

    @on(Button.Pressed, "#cancel-btn")
    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_all(self) -> None:
        self.query_one(_WID_WLC_SEL, SelectionList).select_all()

    def action_select_none(self) -> None:
        self.query_one(_WID_WLC_SEL, SelectionList).deselect_all()


# ===========================================================================
# Run picker modal
# ===========================================================================

class RunPickerModal(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel",     "Cancel"),
        Binding("d",      "delete_run", "Delete", show=True),
    ]
    CSS_PATH = _CSS

    def __init__(self, runs: List[Run]) -> None:
        super().__init__()
        self._runs = list(runs)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Label("  Select Baseline Run", classes="dialog-title")
            yield Label(
                "The selected snapshot will be compared against the new collection.",
                classes="input-hint",
            )
            yield Rule(classes="dialog-rule")
            yield DataTable(id="session-table", cursor_type="row", zebra_stripes=True)
            yield Label(
                "\\[Enter] Select   \\[d] Delete   \\[Esc] Cancel",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        self._current_row_uuid: Optional[str] = self._runs[0].uuid if self._runs else None
        table = self.query_one("#session-table", DataTable)
        table.add_columns("LABEL", "WLCs", "APs", "JOINED", "CLIENTS", "FAILED", "COLLECTED")
        for r in self._runs:
            self._add_run_row(table, r)
        table.focus()

    def _add_run_row(self, table: DataTable, r: Run) -> None:
        failed     = len(r.failed_wlcs)
        fail_txt   = Text(str(failed), style=_S_RED) if failed else Text("0", style="dim")
        client_txt = Text("✓", style="bold cyan") if r.has_clients else Text("—", style="dim")
        table.add_row(
            r.display_label,
            str(len(r.wlc_results)),
            str(r.stats.total),
            str(r.stats.joined),
            client_txt,
            fail_txt,
            r.display_time,
            key=r.uuid,
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._current_row_uuid = str(event.row_key.value) if event.row_key else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        uuid = str(event.row_key.value)
        run  = next((r for r in self._runs if r.uuid == uuid), None)
        self.dismiss(run)

    def action_delete_run(self) -> None:
        uuid = self._current_row_uuid
        if not uuid:
            return
        idx = next((i for i, r in enumerate(self._runs) if r.uuid == uuid), -1)
        if idx == -1:
            return
        SnapshotDB().delete_run(uuid)
        self._runs = [r for r in self._runs if r.uuid != uuid]
        self.query_one("#session-table", DataTable).remove_row(uuid)
        if not self._runs:
            self.dismiss(None)
            return
        # RowHighlighted may not fire when cursor index is unchanged (non-last row
        # deleted, next row shifts up into the same slot). Recalculate explicitly.
        self._current_row_uuid = self._runs[min(idx, len(self._runs) - 1)].uuid

    def action_cancel(self) -> None:
        self.dismiss(None)


# ===========================================================================
# Copyable label widget
# ===========================================================================

class CopyLabel(Label):
    """Value label that copies its text to clipboard on click."""

    def __init__(self, value: str, **kwargs) -> None:
        super().__init__(value, **kwargs)
        self._copy_value = value

    def on_click(self) -> None:
        if self._copy_value and self._copy_value != "—":
            self.app.copy_to_clipboard(self._copy_value)
            self.app.notify(f"Copié : {self._copy_value}", timeout=2)


# ===========================================================================
# AP Detail modal
# ===========================================================================

class APDetailModal(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close")]
    CSS_PATH = _CSS

    def __init__(self, ap: APRecord) -> None:
        super().__init__()
        self._ap = ap

    def compose(self) -> ComposeResult:
        ap = self._ap
        dnac_configured = is_dnac_configured() and bool(ap.ip_addr or ap.name)
        net_placeholder = "Chargement…" if dnac_configured else "—"
        net_class       = "ap-detail-loading" if dnac_configured else "ap-detail-placeholder"

        with Vertical(id="ap-detail-dialog"):
            yield Label(ap.name or "Unknown AP", classes="dialog-title")
            yield Rule(classes="dialog-rule")

            yield Static(_fmt_state(ap.state), classes="ap-detail-state")

            yield Rule(classes="dialog-rule")

            yield Label("IDENTITY", classes="ap-detail-section")
            with Horizontal(classes="ap-detail-row"):
                yield Label("WLC",      classes="ap-detail-key")
                yield Label(ap.wlc_name  or "—", classes="ap-detail-val")
            with Horizontal(classes="ap-detail-row"):
                yield Label("MAC Ethernet", classes="ap-detail-key")
                yield CopyLabel(ap.eth_mac  or "—", classes=_CLS_COPYABLE)
            with Horizontal(classes="ap-detail-row"):
                yield Label("MAC WiFi",     classes="ap-detail-key")
                yield CopyLabel(ap.wtp_mac  or "—", classes=_CLS_COPYABLE)
            with Horizontal(classes="ap-detail-row"):
                yield Label("IP",       classes="ap-detail-key")
                yield CopyLabel(ap.ip_addr  or "—", classes=_CLS_COPYABLE)
            with Horizontal(classes="ap-detail-row"):
                yield Label("Model",    classes="ap-detail-key")
                yield Label(ap.model    or "—", classes="ap-detail-val")
            with Horizontal(classes="ap-detail-row"):
                yield Label("Location", classes="ap-detail-key")
                yield Label(ap.location or "—", classes="ap-detail-val")

            yield Rule(classes="dialog-rule")

            yield Label("TAGS", classes="ap-detail-section")
            with Horizontal(classes="ap-detail-row"):
                yield Label("Policy",   classes="ap-detail-key")
                yield Label(ap.policy_tag or "—", classes="ap-detail-val")
            with Horizontal(classes="ap-detail-row"):
                yield Label("Site",     classes="ap-detail-key")
                yield Label(ap.site_tag   or "—", classes="ap-detail-val")
            with Horizontal(classes="ap-detail-row"):
                yield Label("RF",       classes="ap-detail-key")
                yield Label(ap.rf_tag    or "—", classes="ap-detail-val")

            yield Rule(classes="dialog-rule")

            yield Label("NETWORK CONNECTIVITY", classes="ap-detail-section")
            with Horizontal(classes="ap-detail-row"):
                yield Label("Switch",   classes="ap-detail-key")
                yield Label(net_placeholder, id="detail-switch",
                            classes=f"ap-detail-val {net_class}")
            with Horizontal(classes="ap-detail-row"):
                yield Label("Port",     classes="ap-detail-key")
                yield Label(net_placeholder, id="detail-port",
                            classes=f"ap-detail-val {net_class}")

            with Horizontal(id="dialog-buttons"):
                yield Button("Close", classes="cancel-btn", id="close-btn")

    def on_mount(self) -> None:
        ap = self._ap
        if is_dnac_configured() and (ap.ip_addr or ap.name):
            self._fetch_port_info()

    @work(thread=True)
    def _fetch_port_info(self) -> None:
        app    = self.app
        client = get_dnac_client(username=app.username, password=app.password)
        if client is None:
            return
        result: Optional[DNACPortInfo] = client.get_ap_port(
            self._ap.ip_addr or "", self._ap.name or ""
        )
        self.app.call_from_thread(self._apply_port_info, result)

    def _apply_port_info(self, info: Optional[DNACPortInfo]) -> None:
        switch_lbl = self.query_one("#detail-switch", Label)
        port_lbl   = self.query_one("#detail-port",   Label)
        if info is not None:
            switch_lbl.update(info.switch_name)
            port_lbl.update(info.switch_port)
            for lbl in (switch_lbl, port_lbl):
                lbl.remove_class("ap-detail-loading")
                lbl.add_class("ap-detail-val")
        else:
            switch_lbl.update("—")
            port_lbl.update("—")
            for lbl in (switch_lbl, port_lbl):
                lbl.remove_class("ap-detail-loading")
                lbl.add_class("ap-detail-placeholder")

    @on(Button.Pressed, "#close-btn")
    def _close(self) -> None:
        self.dismiss()


# ===========================================================================
# Main screen
# ===========================================================================

class MainScreen(Screen):

    COMMANDS = {WLCCheckCommands}

    BINDINGS = [
        Binding("ctrl+p",    "command_palette",   "Commands"),
        Binding("c",         "snapshot",          "Snapshot"),
        Binding("p",         "post_check",        "Post-Check"),
        Binding("u",         "upgrade",           "EIU Upgrade"),
        Binding("f",         "filter_diff",       "Sev. Filter"),
        Binding("o",         "toggle_unchanged",  "Only Changes"),
        Binding("space",     "live_pause",        "Pause/Resume", show=False),
        Binding("r",         "live_refresh",      "Force Poll",   show=False),
        Binding("x",         "live_export",          "Export CSV",    show=False),
        Binding("m",         "live_toggle_mobility", "WLC Mobility",  show=False),
        Binding("escape",    "live_exit",            "Back",          show=False),
        Binding("q",         "quit",              "Quit"),
    ]

    _collection_mode:        str                      = "snapshot"
    _baseline_run:           Optional[Run]            = None
    _post_run:               Optional[Run]            = None
    _current_diff:           Optional[DiffSummary]    = None
    _diff_filter:            str                      = "all"
    _only_changes:           bool                     = False
    _pending_entries:        Optional[List[WLCEntry]] = None
    _pending_label:          Optional[str]            = None
    _pending_clients:        bool                     = False
    _pending_live_interval:  int                      = 0

    # live monitor state
    _live_mode:         bool         = False
    _live_interval:     int          = 60
    _live_paused:       bool         = False
    _live_poll_num:     int          = 0
    _live_countdown:    int          = 0
    _live_only_changes: bool         = False
    _live_focused_wlc:  Optional[str] = None
    _live_show_mobility: bool        = False

    # ------------------------------------------------------------------ compose

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="error-bar")

        # All view panels share one 1fr container so hint-bar is always visible
        with Vertical(id="content-area"):

            # ── Welcome ───────────────────────────────────────────────────
            with Vertical(id="welcome-panel"):
                with Vertical(id="welcome-box"):
                    yield Label("WLCCheck",                                        id="welcome-title")
                    yield Label("Cisco 9800  ·  Pre / Post Change Verification",   id="welcome-sub")
                    yield Label(
                        "\\[c]  New snapshot    \\[p]  Post-check    Ctrl+P  Commands",
                        id="welcome-hint",
                    )

            # ── Loading ───────────────────────────────────────────────────
            with Vertical(id="loading-panel"):
                with Vertical(id="loading-box"):
                    yield Label("  Collecting Data", id="loading-title")
                    yield Label("Initialising…",     id="loading-status")
                    yield ProgressBar(id="loading-progress", total=100, show_eta=False)
                    yield RichLog(id="loading-log", highlight=True, markup=True, max_lines=200)

            # ── Dashboard (tabbed) ────────────────────────────────────────
            with Vertical(id="dashboard-panel"):
                with Horizontal(id="context-bar"):
                    yield Label("RUN",   classes="ctx-key")
                    yield Label("—",     id="ctx-session", classes="ctx-value")
                    yield Label("│",     classes="ctx-sep")
                    yield Label("WLCs",  classes="ctx-key")
                    yield Label("—",     id="ctx-wlcs",    classes="ctx-value")
                    yield Label("│",     classes="ctx-sep")
                    yield Label("LABEL", classes="ctx-key")
                    yield Label("—",     id="ctx-label",   classes="ctx-value")

                with Horizontal(id="stats-bar"):
                    with Vertical(classes="stat-box", id="stat-total"):
                        yield Label("TOTAL",      classes="stat-label")
                        yield Label("—", id="val-total",  classes="stat-value")
                    with Vertical(classes="stat-box", id="stat-joined"):
                        yield Label("JOINED",     classes="stat-label")
                        yield Label("—", id="val-joined", classes="stat-value")
                    with Vertical(classes="stat-box", id="stat-njoin"):
                        yield Label(_COL_NOT_JOINED, classes="stat-label")
                        yield Label("—", id="val-njoin",  classes="stat-value")
                    with Vertical(classes="stat-box", id="stat-clients"):
                        yield Label("CLIENTS",    classes="stat-label")
                        yield Label("—", id="val-clients", classes="stat-value")

                with TabbedContent(id="dashboard-tabs"):
                    with TabPane("  Access Points", id="tab-aps"):
                        yield DataTable(id="ap-table", zebra_stripes=True, cursor_type="row")
                    with TabPane("  WLANs", id="tab-wlans"):
                        yield DataTable(id="wlan-table", zebra_stripes=True, cursor_type="row")
                    with TabPane("  Clients", id="tab-clients"):
                        yield DataTable(id="client-table", zebra_stripes=True, cursor_type="row")
                    with TabPane("  WLC Status", id="tab-wlc-status"):
                        yield DataTable(id="wlc-status-table", zebra_stripes=True, cursor_type="none")

            # ── Live Monitor ──────────────────────────────────────────────
            with Vertical(id="live-panel"):
                with Horizontal(id="live-wlc-bar"):
                    pass  # WLC buttons mounted dynamically in _enter_live_mode

                with Horizontal(id="live-stats-bar"):
                    with Vertical(classes="stat-box", id="live-stat-baseline"):
                        yield Label("BASELINE",  classes="stat-label")
                        yield Label("—", id="live-val-baseline", classes="stat-value")
                    with Vertical(classes="stat-box", id="live-stat-current"):
                        yield Label("CURRENT",   classes="stat-label")
                        yield Label("—", id="live-val-current",  classes="stat-value")
                    with Vertical(classes="stat-box", id="live-stat-delta"):
                        yield Label("DELTA",     classes="stat-label")
                        yield Label("—", id="live-val-delta",    classes="stat-value")
                    with Vertical(classes="stat-box", id="live-stat-poll"):
                        yield Label(_COL_POLL_NUM,    classes="stat-label")
                        yield Label("—", id="live-val-poll",     classes="stat-value")
                    with Vertical(classes="stat-box", id="live-stat-next"):
                        yield Label("NEXT POLL", classes="stat-label")
                        yield Label("—", id="live-val-next",     classes="stat-value")

                with TabbedContent(id="live-tabs"):
                    with TabPane("  AP Status", id="live-tab-aps"):
                        yield DataTable(id="live-ap-table", zebra_stripes=True, cursor_type="row")
                    with TabPane("  Event Log", id="live-tab-events"):
                        yield DataTable(id="live-event-table", zebra_stripes=True, cursor_type="none")
                    with TabPane("  History", id="live-tab-history"):
                        yield DataTable(id="live-history-table", zebra_stripes=True, cursor_type="none")

            # ── Diff (tabbed) ─────────────────────────────────────────────
            with Vertical(id="diff-panel"):
                with Horizontal(id="diff-header"):
                    yield Label("BASELINE",   classes="ctx-key")
                    yield Label("—", id="diff-baseline-label", classes="ctx-value")
                    yield Label("│",          classes="ctx-sep")
                    yield Label("POST-CHECK", classes="ctx-key")
                    yield Label("—", id="diff-post-label",     classes="ctx-value")
                    yield Label("│",          classes="ctx-sep")
                    yield Label("",  id="diff-filter-label",   classes="diff-filter-badge")

                with Horizontal(id="diff-stats-bar"):
                    with Vertical(classes="stat-box", id="diff-stat-total"):
                        yield Label("POST APs",  classes="stat-label")
                        yield Label("—", id="diff-val-total",    classes="stat-value")
                    with Vertical(classes="stat-box", id="diff-stat-critical"):
                        yield Label("CRITICAL",  classes="stat-label")
                        yield Label("—", id="diff-val-critical", classes="stat-value")
                    with Vertical(classes="stat-box", id="diff-stat-warning"):
                        yield Label("WARNING",   classes="stat-label")
                        yield Label("—", id="diff-val-warning",  classes="stat-value")
                    with Vertical(classes="stat-box", id="diff-stat-info"):
                        yield Label("INFO",      classes="stat-label")
                        yield Label("—", id="diff-val-info",     classes="stat-value")

                with TabbedContent(id="diff-tabs"):
                    with TabPane("  AP Changes", id="diff-tab-aps"):
                        yield DataTable(id="diff-ap-table", zebra_stripes=True, cursor_type="row")
                    with TabPane("  WLAN Changes", id="diff-tab-wlans"):
                        yield DataTable(id="diff-wlan-table", zebra_stripes=True, cursor_type="row")
                    with TabPane("  Client Changes", id="diff-tab-clients"):
                        yield DataTable(id="diff-client-table", zebra_stripes=True, cursor_type="row")

        yield Static("", id="hint-bar")

    # ------------------------------------------------------------------ lifecycle

    def on_mount(self) -> None:
        # Instance-level live state (dicts/lists must not be class vars)
        self._live_polls:           Deque[LivePollRecord]      = deque(maxlen=2000)
        self._live_events:          Deque[APStateEvent]        = deque(maxlen=1000)
        self._live_current:         Dict[str, List[APRecord]]  = {}
        self._live_prev:            Dict[str, List[APRecord]]  = {}
        self._live_baseline:        Dict[str, List[APRecord]]  = {}
        self._live_known:           Dict[str, APRecord]        = {}
        self._live_since_by_mac:    Dict[str, str]             = {}
        self._live_ap_cols_mobility: Optional[bool]            = None  # sentinel
        self._live_wlc_list:        List[str]                  = []
        self._live_entries:         List[WLCEntry]             = []
        self._live_timer:           Optional[Timer]            = None
        self._ap_by_row_key:        Dict[str, APRecord]        = {}
        self._last_ap_row_key:      Optional[str]              = None

        self.query_one("#ap-table", DataTable).add_columns(
            "WLC", "NAME", "STATE", "IP ADDRESS", "POLICY TAG", "SITE TAG", "RF TAG"
        )
        self.query_one("#wlan-table", DataTable).add_columns(
            "WLC", "ID", "SSID", "PROFILE", "STATE"
        )
        self.query_one("#client-table", DataTable).add_columns(
            "WLC", "MAC", "AP", "SSID", "IPv4", "STATE", "USER"
        )
        self.query_one("#wlc-status-table", DataTable).add_columns(
            "WLC", "HOST", "STATUS", "APs", "JOINED", "ERROR"
        )
        self.query_one("#diff-ap-table", DataTable).add_columns(
            "WLC", "NAME", _COL_PRE_STATE, _COL_POST_STATE, "CHANGE", "SEVERITY", "TAGS"
        )
        self.query_one("#diff-wlan-table", DataTable).add_columns(
            "WLC", "SSID", "PROFILE", _COL_PRE_STATE, _COL_POST_STATE, "CHANGE", "SEVERITY"
        )
        self.query_one("#diff-client-table", DataTable).add_columns(
            "WLC", "MAC", "AP", "SSID", "PRE IP", "POST IP",
            _COL_PRE_STATE, _COL_POST_STATE, "CHANGE", "SEVERITY"
        )
        self.query_one("#live-ap-table", DataTable).add_columns(
            "NAME", "MAC", "WLC", "BASELINE", _COL_PREV_POLL, "CURRENT", _COL_CHANGED_AT
        )
        self.query_one("#live-event-table", DataTable).add_columns(
            "TIME", _COL_POLL_NUM, "WLC", "AP NAME", "FROM", "TO"
        )
        self.query_one("#live-history-table", DataTable).add_columns(
            _COL_POLL_NUM, "TIME", "WLC", "TOTAL", "JOINED", _COL_NOT_JOINED, "OTHER"
        )
        self._show_view("welcome")

    # ------------------------------------------------------------------ view switching

    _HINTS = {
        "welcome":   "\\[c] Snapshot  \\[p] Post-Check  \\[Ctrl+P] Command Palette  \\[q] Quit",
        "loading":   "Collection in progress…",
        "dashboard": "\\[c] New Snapshot  \\[p] Post-Check  \\[Enter] AP Detail  \\[Ctrl+P] Commands  \\[q] Quit",
        "diff":      "\\[o] Toggle All/Changes  \\[f] Severity Filter  \\[Enter] AP Detail  \\[c] New Snapshot  \\[p] Post-Check  \\[Ctrl+P] Commands  \\[q] Quit",
        "live":      "\\[Space] Pause/Resume  \\[r] Force Poll  \\[0] All WLCs  \\[1-9] Focus WLC  \\[o] All/Changed  \\[m] WLC Mobility  \\[Enter] AP Detail  \\[x] Export CSV  \\[Esc] Back  \\[q] Quit",
    }

    def _show_view(self, view: str) -> None:
        self.query_one("#welcome-panel").display   = (view == "welcome")
        self.query_one("#loading-panel").display   = (view == "loading")
        self.query_one("#dashboard-panel").display = (view == "dashboard")
        self.query_one("#live-panel").display      = (view == "live")
        self.query_one("#diff-panel").display      = (view == "diff")
        self.query_one(_WID_ERROR_BAR).display       = False
        self.query_one("#hint-bar", Static).update(self._HINTS.get(view, ""))

    # ------------------------------------------------------------------ actions

    def action_snapshot(self) -> None:
        self._collection_mode = "snapshot"
        self._baseline_run    = None
        self._hide_error()
        self._open_wlc_picker()

    def action_post_check(self) -> None:
        self._hide_error()
        db   = SnapshotDB()
        runs = db.get_recent_runs()
        if not runs:
            self._show_error("No snapshots found — take one first with \\[c].")
            return
        self._collection_mode = "post_check"
        self.app.push_screen(RunPickerModal(runs), self._on_run_picked)

    def action_upgrade(self) -> None:
        self._hide_error()
        inv_path = find_inventory()
        if inv_path is None:
            self._show_error("No inventory file found — EIU upgrade requires a WLC inventory.")
            return
        try:
            entries = load_inventory(inv_path)
        except InventoryError as exc:
            self._show_error(str(exc))
            return
        self.app.push_screen(
            WLCPickerModal(entries),
            self._on_wlcs_chosen_for_upgrade,
        )

    def _on_wlcs_chosen_for_upgrade(self, chosen: Optional[List[WLCEntry]]) -> None:
        if not chosen:
            return
        self._pending_entries = chosen
        self._require_credentials(self._launch_upgrade_screen)

    def _launch_upgrade_screen(self) -> None:
        entries  = self._pending_entries or []
        app: WLCCheckApp = self.app  # type: ignore[assignment]
        self._pending_entries = None
        self.app.push_screen(PredownloadScreen(entries, app.username, app.password, SnapshotDB()))

    def action_filter_diff(self) -> None:
        if self._current_diff is None:
            return
        idx = _FILTER_CYCLE.index(self._diff_filter)
        self._diff_filter = _FILTER_CYCLE[(idx + 1) % len(_FILTER_CYCLE)]
        self._render_all_diff_tables(self._current_diff)

    def action_toggle_unchanged(self) -> None:
        if self._live_mode:
            self._live_only_changes = not self._live_only_changes
            self._refresh_live_ap_table()
            return
        if self._current_diff is None:
            return
        self._only_changes = not self._only_changes
        self._render_all_diff_tables(self._current_diff)

    def action_live_pause(self) -> None:
        self._live_paused = not self._live_paused
        self._update_countdown_label()

    def action_live_refresh(self) -> None:
        if not self._live_paused:
            self._live_countdown = 0   # fires on next 1-s tick

    def action_live_toggle_mobility(self) -> None:
        self._live_show_mobility = not self._live_show_mobility
        self._refresh_live_ap_table()

    def action_live_export(self) -> None:
        """Export all three live-monitor tables to timestamped CSV files."""
        ts      = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        outdir  = pathlib.Path.cwd()
        focused = self._live_focused_wlc
        scope   = focused.replace(" ", "_") if focused else "all"

        # ── AP Status ───────────────────────────────────────────────────────
        ap_path         = outdir / f"wlccheck_live_ap_{scope}_{ts}.csv"
        baseline_by_mac = self._live_records_by_mac(self._live_baseline, focused)
        prev_by_mac     = self._live_records_by_mac(self._live_prev,     focused)
        current_by_mac  = self._live_records_by_mac(self._live_current,  focused)

        with ap_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["NAME", _COL_MAC_ETH, "WLC", "BASELINE", _COL_PREV_POLL, "CURRENT", _COL_CHANGED_AT])
            for ap in self._live_ap_union(current_by_mac, focused):
                is_missing = ap.wtp_mac not in current_by_mac
                baseline   = baseline_by_mac.get(ap.wtp_mac)
                prev       = prev_by_mac.get(ap.wtp_mac)
                cur_state  = "Not Joined (gone)" if is_missing else _state_label(ap.state)
                w.writerow([
                    ap.name,
                    ap.eth_mac or ap.wtp_mac or "—",
                    ap.wlc_name or "—",
                    _state_label(baseline.state) if baseline else "—",
                    _state_label(prev.state)     if prev     else "—",
                    cur_state,
                    self._live_since_by_mac.get(ap.wtp_mac, "—"),
                ])

        # ── Event Log ───────────────────────────────────────────────────────
        ev_path = outdir / f"wlccheck_live_events_{scope}_{ts}.csv"
        events  = [e for e in self._live_events
                   if focused is None or e.wlc_name == focused]
        with ev_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["TIME", _COL_POLL_NUM, "WLC", "AP NAME", "MAC", "FROM", "TO"])
            for evt in events:
                w.writerow([
                    evt.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    evt.poll_num,
                    evt.wlc_name,
                    evt.ap_name,
                    evt.wtp_mac,
                    _state_label(evt.from_state),
                    _state_label(evt.to_state),
                ])

        # ── History ─────────────────────────────────────────────────────────
        hist_path = outdir / f"wlccheck_live_history_{scope}_{ts}.csv"
        polls     = [p for p in self._live_polls
                     if focused is None or p.wlc_name == focused]
        with hist_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([_COL_POLL_NUM, "TIME", "WLC", "TOTAL", "JOINED", _COL_NOT_JOINED, "OTHER"])
            for p in polls:
                w.writerow([
                    p.poll_num,
                    p.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    p.wlc_name,
                    p.total,
                    p.joined,
                    p.not_joined,
                    p.other,
                ])

        self._show_error(
            f"Exported → {ap_path.name}  ·  {ev_path.name}  ·  {hist_path.name}"
        )
        # Briefly show confirmation in the error bar, then clear after 4 s
        self.set_timer(4.0, self._hide_error)

    def action_live_exit(self) -> None:
        self._live_mode = False
        if self._live_timer is not None:
            self._live_timer.stop()
            self._live_timer = None
        if self._current_diff is not None:
            self._show_view("diff")
        else:
            self._show_view("welcome")

    def check_action(self, action: str, _parameters: tuple) -> bool | None:
        if action in ("filter_diff", "toggle_unchanged"):
            return self._current_diff is not None or self._live_mode
        if action in ("live_pause", "live_refresh", "live_export",
                      "live_toggle_mobility", "live_exit"):
            return self._live_mode
        return True

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if (event.data_table.id or "") in {"ap-table", "live-ap-table", "diff-ap-table"}:
            self._last_ap_row_key = str(event.row_key.value) if event.row_key else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if (event.data_table.id or "") not in {"ap-table", "live-ap-table", "diff-ap-table"}:
            return
        key = str(event.row_key.value)
        ap = self._ap_record_for_row_key(key)
        if ap is not None:
            self.app.push_screen(APDetailModal(ap))

    def _ap_record_for_row_key(self, key: str) -> Optional[APRecord]:
        if key in self._ap_by_row_key:
            return self._ap_by_row_key[key]
        # Diff table uses wlc|mac|change_type — fall back to wlc|mac
        base = key.rsplit("|", 1)[0]
        return self._ap_by_row_key.get(base)

    def on_key(self, event) -> None:
        """Handle number keys to focus a WLC in live mode."""
        if not self._live_mode:
            return
        key = event.key
        if key == "0":
            self._live_set_focus(None)
            event.stop()
        elif key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(self._live_wlc_list):
                self._live_set_focus(self._live_wlc_list[idx])
                event.stop()

    def action_quit(self) -> None:
        self.app.exit()

    def _set_filter(self, level: str) -> None:
        self._diff_filter = level
        if self._current_diff:
            self._render_all_diff_tables(self._current_diff)

    # ------------------------------------------------------------------ WLC picker flow

    def _open_wlc_picker(self, prefill_label: str = "") -> None:
        inv_path = find_inventory()
        if inv_path is None:
            self.app.push_screen(
                ConnectModal(prefill_label=prefill_label),
                self._on_single_connect,
            )
            return
        try:
            entries = load_inventory(inv_path)
        except InventoryError as exc:
            self._show_error(str(exc))
            return
        self.app.push_screen(
            WLCPickerModal(entries),
            lambda chosen: self._on_wlcs_chosen(chosen, prefill_label),
        )

    def _on_wlcs_chosen(
        self,
        chosen: Optional[List[WLCEntry]],
        prefill_label: str = "",
    ) -> None:
        if not chosen:
            return
        self._pending_entries = chosen
        self.app.push_screen(
            RunOptionsModal(prefill_label=prefill_label, title="Run Options"),
            self._on_run_options,
        )

    def _on_run_options(self, result) -> None:
        if result is None:
            return
        label, collect_clients, live_interval = result
        self._pending_label          = label
        self._pending_clients        = collect_clients
        self._pending_live_interval  = live_interval
        self._require_credentials(self._start_multi_collection)

    def _on_single_connect(self, result: Optional[tuple]) -> None:
        if not result:
            return
        host, label, collect_clients, live_interval = result
        app: WLCCheckApp = self.app  # type: ignore[assignment]
        app.current_host = host
        self._pending_entries        = [WLCEntry(name=host, host=host)]
        self._pending_label          = label
        self._pending_clients        = collect_clients
        self._pending_live_interval  = live_interval
        self._require_credentials(self._start_multi_collection)

    def _on_run_picked(self, run: Optional[Run]) -> None:
        if not run:
            self._collection_mode = "snapshot"
            return
        self._baseline_run = run
        post_label = "post-" + (run.label or run.uuid[:16])
        self._open_wlc_picker(prefill_label=post_label)

    # ------------------------------------------------------------------ credential gate

    def _require_credentials(self, callback) -> None:
        app: WLCCheckApp = self.app  # type: ignore[assignment]
        if app.username and app.password:
            callback()
            return
        self.app.push_screen(
            CredentialModal(prefill_user=app.username),
            lambda creds: self._on_creds(creds, callback),
        )

    def _on_creds(self, creds: Optional[tuple], callback) -> None:
        if not creds:
            return
        app: WLCCheckApp = self.app  # type: ignore[assignment]
        app.username, app.password = creds
        callback()

    # ------------------------------------------------------------------ collection worker

    def _start_multi_collection(self) -> None:
        self._show_view("loading")
        self._log_clear()
        self._set_status("Starting…")
        entries         = self._pending_entries or []
        label           = self._pending_label
        collect_clients = self._pending_clients
        self._live_entries    = list(entries)   # keep for live polling
        self._pending_entries = None
        self._pending_label   = None
        self._pending_clients = False
        self.multi_collect_worker(entries, label, collect_clients)

    @work(thread=True, name="multi-collector")
    def multi_collect_worker(
        self,
        entries: List[WLCEntry],
        label: Optional[str],
        collect_clients: bool,
    ) -> None:
        app: WLCCheckApp = self.app  # type: ignore[assignment]

        def status(msg: str) -> None:
            self.post_message(StatusUpdate(msg))

        username = app.username
        password = app.password
        max_workers = min(len(entries), 8)

        # Credential probe — validate on one WLC before launching parallel threads.
        # Only needed when 2+ WLCs: a single failed attempt per device at once could
        # lock the TACACS account. Skip for a single WLC (no parallel risk).
        if len(entries) > 1:
            probe = entries[0]
            status(f"Validating credentials on [bold]{probe.name}[/bold]…")
            try:
                WLCClient(probe.host, username, password).check_auth()
                status("[green]✓[/green] Credentials OK — starting parallel collection…")
            except WLCAuthError as exc:
                self.post_message(CollectionError(
                    f"Authentication failed on {probe.name}: {exc}\n"
                    "Parallel collection aborted — check your TACACS credentials."
                ))
                return
            except Exception:
                # Network / connectivity issue on the probe WLC — not an auth problem,
                # proceed and let each WLC handle its own error.
                status(
                    f"[yellow]⚠[/yellow] Could not reach {probe.name} for credential "
                    "probe (network error) — proceeding anyway…"
                )

        status(f"Collecting from {len(entries)} WLC(s) — {max_workers} parallel threads…")

        results: List[WLCResult] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_collect_wlc_entry, e, username, password, collect_clients, status): e
                for e in entries
            }
            for future in as_completed(futures):
                results.append(future.result())

        results.sort(key=lambda r: r.wlc_name.lower())

        failed = [r for r in results if not r.ok]
        if failed:
            status(
                f"[yellow]⚠[/yellow] {len(failed)} WLC(s) failed: "
                + ", ".join(r.wlc_name for r in failed)
            )

        status("Saving run to database…")
        db       = SnapshotDB()
        stype    = "post" if self._collection_mode == "post_check" else "snapshot"
        run_uuid = db.save_run(label, stype, results, has_clients=collect_clients)
        db.purge_old_runs()

        run = Run(
            uuid=run_uuid, label=label, session_type=stype,
            created_at=_dt.datetime.now(),
            wlc_results=results,
            has_clients=collect_clients,
        )

        status(f"[green]✓[/green] Run saved — ID: [bold]{run_uuid}[/bold]")
        self.post_message(RunComplete(run, run_uuid))

    # ------------------------------------------------------------------ message handlers

    def on_status_update(self, event: StatusUpdate) -> None:
        self._set_status(event.text)
        self.query_one("#loading-log", RichLog).write(event.text)
        self.query_one("#loading-progress", ProgressBar).advance(3)

    def on_run_complete(self, event: RunComplete) -> None:
        if self._collection_mode == "post_check" and self._baseline_run:
            self._run_diff(event.run)   # computes diff, may switch to diff view
        else:
            self._populate_dashboard(event.run)
        # Enter live monitor if requested (overrides the view set above)
        if self._pending_live_interval > 0:
            self._enter_live_mode(event.run)
            self._pending_live_interval = 0

    def on_collection_error(self, event: CollectionError) -> None:
        self._show_view("welcome")
        self._show_error(event.error)

    # ------------------------------------------------------------------ dashboard

    def _populate_dashboard(self, run: Run) -> None:
        stats = run.stats

        self.query_one("#ctx-session", Label).update(run.uuid)
        self.query_one("#ctx-wlcs",    Label).update(
            f"{len(run.wlc_results)} WLC(s)"
            + (f"  [bold red]{len(run.failed_wlcs)} failed[/bold red]"
               if run.failed_wlcs else "")
        )
        self.query_one("#ctx-label", Label).update(run.label or "—")

        self.query_one("#val-total",  Label).update(str(stats.total))
        self.query_one("#val-joined", Label).update(str(stats.joined))
        self.query_one("#val-njoin",  Label).update(str(stats.not_joined))

        if run.has_clients:
            cs = run.client_stats
            self.query_one("#val-clients", Label).update(
                f"{cs.total}  (no IP: {cs.no_ip})"
            )
        else:
            self.query_one("#val-clients", Label).update("—")

        # AP table
        ap_table = self.query_one("#ap-table", DataTable)
        ap_table.clear()
        self._ap_by_row_key = {}
        self._last_ap_row_key = None
        for ap in sorted(run.all_records, key=lambda r: (r.wlc_name.lower(), r.name.lower())):
            _key = (ap.wlc_name or "") + "|" + (ap.wtp_mac or ap.name)
            self._ap_by_row_key[_key] = ap
            ap_table.add_row(
                ap.wlc_name   or "—",
                ap.name,
                _fmt_state(ap.state),
                ap.ip_addr    or "—",
                ap.policy_tag or "—",
                ap.site_tag   or "—",
                ap.rf_tag     or "—",
                key=_key,
            )

        # WLAN table
        wlan_table = self.query_one("#wlan-table", DataTable)
        wlan_table.clear()
        for w in sorted(run.all_wlans, key=lambda x: (x.wlc_name.lower(), x.wlan_id)):
            state_txt = Text("● Up", style=_S_GREEN) if w.state == "up" \
                else Text("✕ Down", style=_S_RED) if w.state == "down" \
                else Text(w.state, style="dim")
            wlan_table.add_row(
                w.wlc_name     or "—",
                str(w.wlan_id),
                w.ssid         or "—",
                w.profile_name or "—",
                state_txt,
                key=(w.wlc_name or "") + "|" + str(w.wlan_id),
            )

        # Client table
        client_table = self.query_one("#client-table", DataTable)
        client_table.clear()
        if run.has_clients:
            for c in sorted(run.all_clients, key=lambda x: x.mac):
                ip_txt = Text(c.ipv4 or c.ipv6 or "—",
                              style=_S_RED if not c.has_ip else "default")
                state_txt = Text(c.state, style=_S_GREEN if c.state == "run" else "yellow")
                client_table.add_row(
                    c.wlc_name or "—",
                    c.mac,
                    c.ap_name   or "—",
                    c.wlan_ssid or "—",
                    ip_txt,
                    state_txt,
                    c.username  or "—",
                    key=(c.wlc_name or "") + "|" + c.mac,
                )

        # WLC status table
        wt = self.query_one("#wlc-status-table", DataTable)
        wt.clear()
        for r in run.wlc_results:
            st = Text("● OK", style=_S_GREEN) if r.ok else Text("✕ Failed", style=_S_RED)
            wt.add_row(
                r.wlc_name, r.wlc_host, st,
                str(r.stats.total), str(r.stats.joined),
                Text(r.error[:60], style="red") if not r.ok else Text("—", style="dim"),
                key=r.wlc_name,
            )

        self._show_view("dashboard")

    # ------------------------------------------------------------------ diff

    def _run_diff(self, post_run: Run) -> None:
        assert self._baseline_run is not None
        db = SnapshotDB()
        pre_records = db.load_records(self._baseline_run.uuid)
        pre_wlans   = db.load_wlans(self._baseline_run.uuid)
        post_wlans  = post_run.all_wlans

        pre_clients  = None
        post_clients = None
        if self._baseline_run.has_clients and post_run.has_clients:
            pre_clients  = db.load_clients(self._baseline_run.uuid)
            post_clients = post_run.all_clients

        summary = compute_diff(
            pre_records, post_run.all_records,
            pre_wlans=pre_wlans, post_wlans=post_wlans,
            pre_clients=pre_clients, post_clients=post_clients,
        )
        self._current_diff = summary
        self._post_run     = post_run
        self._diff_filter  = "all"
        self._only_changes = False
        self._populate_diff(summary, post_run)

    def _populate_diff(self, summary: DiffSummary, post_run: Run) -> None:
        assert self._baseline_run is not None

        self.query_one("#diff-baseline-label", Label).update(
            self._baseline_run.display_label
            + f"  ({self._baseline_run.display_time})"
        )
        self.query_one("#diff-post-label", Label).update(
            post_run.label or post_run.uuid
        )

        total_crit = summary.critical_count
        total_warn = summary.warning_count
        total_info = summary.info_count

        self.query_one("#diff-val-total",    Label).update(str(summary.post_total))
        self.query_one("#diff-val-critical", Label).update(_colored_count(total_crit, "red"))
        self.query_one("#diff-val-warning",  Label).update(_colored_count(total_warn, "yellow"))
        self.query_one("#diff-val-info",     Label).update(_colored_count(total_info, "cyan"))

        self._render_all_diff_tables(summary)
        self._show_view("diff")
        self.query_one("#diff-filter-label", Label).update(
            f"{_FILTER_LABEL[self._diff_filter]}"
        )

    def _render_all_diff_tables(self, summary: DiffSummary) -> None:
        self._render_diff_ap_table(summary)
        self._render_diff_wlan_table(summary)
        self._render_diff_client_table(summary)
        sev_label = _FILTER_LABEL[self._diff_filter]
        scope_label = "Changed only" if self._only_changes else "All records"
        self.query_one("#diff-filter-label", Label).update(
            f"{sev_label}  ·  {scope_label}"
        )

    def _render_diff_ap_table(self, summary: DiffSummary) -> None:
        table = self.query_one("#diff-ap-table", DataTable)
        table.clear()
        self._ap_by_row_key = {}
        self._last_ap_row_key = None

        # Build AP lookup from post run for detail modal
        post_ap_lookup: Dict[str, APRecord] = {}
        if self._post_run:
            for _ap in self._post_run.all_records:
                post_ap_lookup[((_ap.wlc_name or "") + "|" + (_ap.wtp_mac or _ap.name))] = _ap

        # Build set of MAC/name keys that have a diff entry
        changed_keys: set[str] = {
            (d.wlc_name or "") + "|" + (d.wtp_mac or d.name)
            for d in summary.ap_diffs
        }

        # Render changed rows (respects severity filter)
        for d in summary.filter_ap(self._diff_filter):
            if d.change_type == "tag_change":
                tags_txt = f"{_fmt_tags(d.pre_tags)} → {_fmt_tags(d.post_tags)}"
            else:
                tags_txt = ""
            if d.change_type == "wlc_move" and d.pre_wlc:
                wlc_col = Text()
                wlc_col.append(d.pre_wlc, style="dim yellow")
                wlc_col.append(" ⇄ ", style=_S_YELLOW)
                wlc_col.append(d.wlc_name, style=_S_YELLOW)
            else:
                wlc_col = Text(d.wlc_name or "—")
            _base_key = (d.wlc_name or "") + "|" + (d.wtp_mac or d.name)
            if _base_key in post_ap_lookup:
                self._ap_by_row_key[_base_key] = post_ap_lookup[_base_key]
            table.add_row(
                wlc_col,
                d.name,
                _fmt_state_or_dash(d.pre_state),
                _fmt_state_or_dash(d.post_state),
                _fmt_change(d.change_type),
                _fmt_severity(d.severity),
                tags_txt,
                key=_base_key + "|" + d.change_type,
            )

        # Render unchanged rows when not in "changed only" mode
        if not self._only_changes and self._post_run is not None:
            ok_txt = Text("✓", style="dim green")
            for ap in sorted(
                self._post_run.all_records,
                key=lambda r: (r.wlc_name.lower(), r.name.lower()),
            ):
                row_key = (ap.wlc_name or "") + "|" + (ap.wtp_mac or ap.name)
                if row_key in changed_keys:
                    continue  # already rendered above
                self._ap_by_row_key[row_key] = ap
                state_txt = _fmt_state(ap.state)
                table.add_row(
                    ap.wlc_name   or "—",
                    ap.name,
                    state_txt,
                    state_txt,
                    ok_txt,
                    Text("—", style="dim"),
                    "",
                    key=row_key,
                )

    def _render_diff_wlan_table(self, summary: DiffSummary) -> None:
        table = self.query_one("#diff-wlan-table", DataTable)
        table.clear()

        changed_keys: set[str] = {
            (d.wlc_name or "") + "|" + str(d.wlan_id)
            for d in summary.wlan_diffs
        }

        for d in summary.filter_wlan(self._diff_filter):
            table.add_row(
                d.wlc_name or "—",
                d.ssid or "—",
                d.profile_name or "—",
                _fmt_state_or_dash(d.pre_state),
                _fmt_state_or_dash(d.post_state),
                _fmt_change(d.change_type),
                _fmt_severity(d.severity),
                key=(d.wlc_name or "") + "|" + str(d.wlan_id),
            )

        if not self._only_changes and self._post_run is not None:
            ok_txt = Text("✓", style="dim green")
            for w in sorted(
                self._post_run.all_wlans,
                key=lambda x: (x.wlc_name.lower(), x.wlan_id),
            ):
                row_key = (w.wlc_name or "") + "|" + str(w.wlan_id)
                if row_key in changed_keys:
                    continue
                state_txt = (
                    Text("● Up",   style=_S_GREEN) if w.state == "up"
                    else Text("✕ Down", style=_S_RED)
                )
                table.add_row(
                    w.wlc_name     or "—",
                    w.ssid         or "—",
                    w.profile_name or "—",
                    state_txt,
                    state_txt,
                    ok_txt,
                    Text("—", style="dim"),
                    key=row_key,
                )

    def _render_diff_client_table(self, summary: DiffSummary) -> None:
        table = self.query_one("#diff-client-table", DataTable)
        table.clear()
        if not summary.has_client_data:
            return
        for d in summary.filter_client(self._diff_filter):
            table.add_row(
                d.wlc_name or "—",
                d.mac,
                d.ap_name   or "—",
                d.wlan_ssid or "—",
                d.pre_ipv4  or "—",
                d.post_ipv4 or "—",
                _fmt_state_or_dash(d.pre_state),
                _fmt_state_or_dash(d.post_state),
                _fmt_change(d.change_type),
                _fmt_severity(d.severity),
                key=(d.wlc_name or "") + "|" + d.mac,
            )

    # ------------------------------------------------------------------ live monitor

    def _enter_live_mode(self, run: Run) -> None:
        """Initialise and start the live AP monitor."""
        self._live_mode          = True
        self._live_interval      = self._pending_live_interval or 60
        self._live_paused        = False
        self._live_poll_num      = 0
        self._live_only_changes  = False
        self._live_show_mobility = False
        self._live_focused_wlc   = None
        self._live_polls           = deque(maxlen=2000)
        self._live_events          = deque(maxlen=1000)
        self._live_current         = {}
        self._live_prev            = {}
        self._live_known           = {}
        self._live_since_by_mac    = {}
        self._live_ap_cols_mobility = None

        # Build baseline lookup from pre-check run (if available)
        self._live_baseline = {}
        if self._baseline_run:
            for rec in SnapshotDB().load_records(self._baseline_run.uuid):
                self._live_baseline.setdefault(rec.wlc_name, []).append(rec)

        # Ordered WLC list (only successful results)
        self._live_wlc_list = [r.wlc_name for r in run.wlc_results if r.ok]

        # Pre-populate current data from the just-completed collection so the
        # table renders immediately — no need to wait for a second RESTCONF poll.
        for result in run.wlc_results:
            if result.ok and result.records:
                self._live_current[result.wlc_name] = list(result.records)
                for rec in result.records:
                    self._live_known[rec.wtp_mac] = rec

        # Rebuild WLC bar buttons
        self._rebuild_live_wlc_bar()

        self._show_view("live")

        # Render immediately with pre-populated data
        self._refresh_live_ap_table()
        self._refresh_live_stats()

        # First re-poll fires after the configured interval, not immediately
        self._live_countdown = self._live_interval

        # Start 1-second tick
        if self._live_timer is not None:
            self._live_timer.stop()
        self._live_timer = self.set_interval(1.0, self._live_tick)

    def _live_tick(self) -> None:
        """Called every second by the interval timer."""
        if self._live_paused:
            return
        if self._live_countdown > 0:
            self._live_countdown -= 1
            self._update_countdown_label()
            return
        # Countdown reached 0 — fire a poll
        self._live_countdown = self._live_interval
        self._live_poll_num += 1
        self._update_countdown_label()
        self.live_poll_worker(self._live_poll_num, list(self._live_entries))

    @work(thread=True, name="live-poller", exclusive=True)
    def live_poll_worker(self, poll_num: int, entries: List[WLCEntry]) -> None:
        """Background worker: poll every WLC in parallel and post results."""
        app: WLCCheckApp = self.app   # type: ignore[assignment]
        username = app.username
        password = app.password

        def poll_one(entry: WLCEntry) -> tuple:
            try:
                client = WLCClient(entry.host, username, password)
                records = client.get_ap_data()
                for r in records:
                    r.wlc_name = entry.name
                return (entry.name, records)
            except Exception:
                return (entry.name, [])

        results: Dict[str, List[APRecord]] = {}
        with ThreadPoolExecutor(max_workers=min(len(entries), 8)) as pool:
            futures = {pool.submit(poll_one, e): e for e in entries}
            for future in as_completed(futures):
                wlc_name, records = future.result()
                results[wlc_name] = records

        self.post_message(LivePollBatchComplete(poll_num, results))

    def on_live_poll_batch_complete(self, event: LivePollBatchComplete) -> None:
        """Process completed live poll: detect events, update history, refresh UI."""
        if not self._live_mode:
            return

        now = _dt.datetime.now()

        # Snapshot prev before overwriting current
        self._live_prev = {k: list(v) for k, v in self._live_current.items()}

        for wlc_name, records in event.results.items():
            current_macs = {r.wtp_mac for r in records}
            prev_by_mac  = {r.wtp_mac: r for r in self._live_prev.get(wlc_name, [])}

            # Detect state changes vs previous poll for APs still present
            for rec in records:
                prev = prev_by_mac.get(rec.wtp_mac)
                if prev and prev.state != rec.state:
                    evt = APStateEvent(
                        timestamp=now, poll_num=event.poll_num,
                        wlc_name=wlc_name, ap_name=rec.name,
                        wtp_mac=rec.wtp_mac,
                        from_state=prev.state, to_state=rec.state,
                    )
                    self._live_events.append(evt)
                    self._live_since_by_mac[rec.wtp_mac] = now.strftime("%H:%M:%S")
                self._live_known[rec.wtp_mac] = rec

            # Detect APs that were in the previous poll but are now missing
            for mac, prev_rec in prev_by_mac.items():
                if mac not in current_macs:
                    evt = APStateEvent(
                        timestamp=now, poll_num=event.poll_num,
                        wlc_name=wlc_name, ap_name=prev_rec.name,
                        wtp_mac=mac,
                        from_state=prev_rec.state, to_state="not_joined",
                    )
                    self._live_events.append(evt)
                    self._live_since_by_mac[mac] = now.strftime("%H:%M:%S")

            stats = APStats.from_records(records)
            self._live_polls.append(LivePollRecord(
                poll_num=event.poll_num,
                timestamp=now,
                wlc_name=wlc_name,
                total=stats.total,
                joined=stats.joined,
                not_joined=stats.not_joined,
                other=stats.other,
            ))

            self._live_current[wlc_name] = records

        # Cross-WLC move detection: AP seen on a different WLC than the previous poll
        all_prev = {
            r.wtp_mac: r
            for recs in self._live_prev.values()
            for r in recs if r.wtp_mac
        }
        all_cur = {
            r.wtp_mac: r
            for recs in event.results.values()
            for r in recs if r.wtp_mac
        }
        for mac, cur in all_cur.items():
            prev = all_prev.get(mac)
            if prev and prev.wlc_name and cur.wlc_name and prev.wlc_name != cur.wlc_name:
                ts = now.strftime("%H:%M:%S")
                self._live_events.append(APStateEvent(
                    timestamp=now, poll_num=event.poll_num,
                    wlc_name=cur.wlc_name,
                    ap_name=cur.name, wtp_mac=mac,
                    from_state=f"wlc:{prev.wlc_name}",
                    to_state=f"wlc:{cur.wlc_name}",
                ))
                self._live_since_by_mac[mac] = ts

        self._update_live_ui()

    def _update_live_ui(self) -> None:
        self._refresh_live_stats()
        self._refresh_live_ap_table()
        self._refresh_live_event_table()
        self._refresh_live_history_table()
        self._update_countdown_label()

    def _refresh_live_stats(self) -> None:
        focused = self._live_focused_wlc

        def _filter(d: Dict[str, List[APRecord]]) -> List[APRecord]:
            out: List[APRecord] = []
            for wlc, recs in d.items():
                if focused is None or wlc == focused:
                    out.extend(recs)
            return out

        cur = APStats.from_records(_filter(self._live_current))
        bas_records = _filter(self._live_baseline)
        bas = APStats.from_records(bas_records) if bas_records else None
        prev_records = _filter(self._live_prev)
        prv = APStats.from_records(prev_records) if prev_records else None

        bas_txt = f"{bas.total}  ({bas.joined} joined)" if bas else "—"
        cur_txt = f"{cur.total}  ({cur.joined} joined)"

        if bas:
            d = cur.joined - bas.joined
            delta_txt = Text(
                f"{'▲' if d > 0 else ('▼' if d < 0 else '=')} {d:+d} joined",
                style=_S_GREEN if d >= 0 else _S_RED,
            )
        elif prv:
            d = cur.joined - prv.joined
            delta_txt = Text(
                f"{'▲' if d > 0 else ('▼' if d < 0 else '=')} {d:+d} joined",
                style=_S_GREEN if d >= 0 else _S_RED,
            )
        else:
            delta_txt = Text("—", style="dim")

        self.query_one("#live-val-baseline", Label).update(bas_txt)
        self.query_one("#live-val-current",  Label).update(cur_txt)
        self.query_one("#live-val-delta",    Label).update(delta_txt)
        self.query_one("#live-val-poll",     Label).update(str(self._live_poll_num))

    def _update_countdown_label(self) -> None:
        if self._live_paused:
            txt = Text("PAUSED", style=_S_YELLOW)
        else:
            txt = Text(f"{self._live_countdown}s", style="cyan")
        try:
            self.query_one("#live-val-next", Label).update(txt)
        except Exception:
            pass

    def _refresh_live_ap_table(self) -> None:
        table   = self.query_one("#live-ap-table", DataTable)
        focused = self._live_focused_wlc
        self._ap_by_row_key = {}
        self._last_ap_row_key = None

        # Rebuild columns only when mobility mode actually changes
        if self._live_show_mobility != self._live_ap_cols_mobility:
            table.clear(columns=True)
            if self._live_show_mobility:
                table.add_columns(
                    "NAME", _COL_MAC_ETH, "BL WLC", "CUR WLC", "MOVED",
                    "BASELINE", _COL_PREV_POLL, "CURRENT", _COL_CHANGED_AT,
                )
            else:
                table.add_columns(
                    "NAME", _COL_MAC_ETH, "WLC", "BASELINE", _COL_PREV_POLL, "CURRENT", _COL_CHANGED_AT,
                )
            self._live_ap_cols_mobility = self._live_show_mobility
        else:
            table.clear()

        baseline_map   = self._live_records_by_mac(self._live_baseline, focused)
        prev_map       = self._live_records_by_mac(self._live_prev,     focused)
        current_by_mac = self._live_records_by_mac(self._live_current,  focused)
        with self.app.batch_update():
            for ap in self._live_ap_union(current_by_mac, focused):
                self._add_live_ap_row(table, ap, current_by_mac, baseline_map, prev_map)

    def _refresh_live_event_table(self) -> None:
        table = self.query_one("#live-event-table", DataTable)
        table.clear()
        focused = self._live_focused_wlc
        events = [e for e in reversed(self._live_events)
                  if focused is None or e.wlc_name == focused]
        for evt in events:
            if evt.from_state.startswith("wlc:"):
                from_txt = Text(evt.from_state[4:], style="dim yellow")
                to_txt   = Text()
                to_txt.append("⇄ ", style=_S_YELLOW)
                to_txt.append(evt.to_state[4:], style=_S_YELLOW)
            else:
                from_txt = _fmt_state(evt.from_state) if evt.from_state != "—" \
                           else Text("—", style="dim")
                to_txt = _fmt_state(evt.to_state)
            table.add_row(
                evt.timestamp.strftime("%H:%M:%S"),
                str(evt.poll_num),
                evt.wlc_name,
                evt.ap_name,
                from_txt,
                to_txt,
            )

    def _refresh_live_history_table(self) -> None:
        table = self.query_one("#live-history-table", DataTable)
        table.clear()
        focused = self._live_focused_wlc
        polls = [p for p in reversed(self._live_polls)
                 if focused is None or p.wlc_name == focused]
        for p in polls:
            nj_txt = Text(
                str(p.not_joined),
                style=_S_RED if p.not_joined > 0 else "dim",
            )
            table.add_row(
                str(p.poll_num),
                p.timestamp.strftime("%H:%M:%S"),
                p.wlc_name,
                str(p.total),
                str(p.joined),
                nj_txt,
                str(p.other),
            )

    def _add_live_ap_row(
        self,
        table: DataTable,
        ap: APRecord,
        current_by_mac: Dict[str, APRecord],
        baseline_map:   Dict[str, APRecord],
        prev_map:       Dict[str, APRecord],
    ) -> None:
        """Compute one AP's display values and add it to the live AP table."""
        is_missing = ap.wtp_mac not in current_by_mac
        baseline   = baseline_map.get(ap.wtp_mac)
        prev       = prev_map.get(ap.wtp_mac)
        eff_state  = "not_joined" if is_missing else ap.state
        bl_wlc     = baseline.wlc_name if baseline else None
        cur_wlc    = ap.wlc_name if not is_missing else None
        moved      = bool(bl_wlc and cur_wlc and bl_wlc != cur_wlc)

        if self._live_only_changes and not _live_ap_changed(
            is_missing, moved, baseline, prev, eff_state
        ):
            return

        bas_txt, prev_txt, cur_txt = _live_ap_state_texts(ap, is_missing, baseline, prev)
        since = self._live_since_by_mac.get(ap.wtp_mac, "—")
        _row_key = ap.wtp_mac or ap.name
        self._ap_by_row_key[_row_key] = ap

        if self._live_show_mobility:
            moved_txt, bl_wlc_txt, cur_wlc_txt = _live_ap_mobility_texts(bl_wlc, cur_wlc, moved)
            table.add_row(
                ap.name, ap.eth_mac or ap.wtp_mac or "—",
                bl_wlc_txt, cur_wlc_txt, moved_txt,
                bas_txt, prev_txt, cur_txt, since,
                key=_row_key,
            )
        else:
            if moved:
                wlc_txt = Text()
                wlc_txt.append("⇄ ", style=_S_YELLOW)
                wlc_txt.append(ap.wlc_name or "—", style=_S_YELLOW)
            else:
                wlc_txt = Text(ap.wlc_name or "—", style="default")
            table.add_row(
                ap.name, ap.eth_mac or ap.wtp_mac or "—", wlc_txt,
                bas_txt, prev_txt, cur_txt, since,
                key=_row_key,
            )

    def _live_records_by_mac(
        self,
        store: Dict[str, List[APRecord]],
        focused: Optional[str],
    ) -> Dict[str, APRecord]:
        return {
            r.wtp_mac: r
            for wlc, recs in store.items()
            if focused is None or wlc == focused
            for r in recs
        }

    def _live_ap_union(
        self,
        current_by_mac: Dict[str, APRecord],
        focused: Optional[str],
    ) -> List[APRecord]:
        aps = list(current_by_mac.values())
        seen = set(current_by_mac.keys())

        # APs that disappeared during monitoring (seen in a previous poll)
        for mac, rec in self._live_known.items():
            if mac not in seen and (focused is None or rec.wlc_name == focused):
                aps.append(rec)
                seen.add(mac)

        # APs that were already down before the first poll (in baseline but never polled)
        for mac, rec in self._live_records_by_mac(self._live_baseline, focused).items():
            if mac not in seen:
                aps.append(rec)

        aps.sort(key=lambda r: (r.wlc_name.lower(), r.name.lower()))
        return aps

    def _rebuild_live_wlc_bar(self) -> None:
        bar = self.query_one("#live-wlc-bar")
        bar.remove_children()
        total_wlcs = len(self._live_wlc_list)
        bar.mount(Button(
            f"All  ({total_wlcs} WLC{'s' if total_wlcs != 1 else ''})",
            id="live-wlc-all",
            classes="live-wlc-btn live-wlc-focused",
        ))
        for i, name in enumerate(self._live_wlc_list):
            bar.mount(Button(
                f"[{i + 1}]  {name}",
                id=f"live-wlc-{i}",
                classes="live-wlc-btn",
            ))

    def _live_set_focus(self, wlc_name: Optional[str]) -> None:
        self._live_focused_wlc = wlc_name
        for btn in self.query(".live-wlc-btn"):
            btn.remove_class("live-wlc-focused")
        if wlc_name is None:
            try:
                self.query_one("#live-wlc-all", Button).add_class("live-wlc-focused")
            except Exception:
                pass
        else:
            idx = self._live_wlc_list.index(wlc_name) \
                  if wlc_name in self._live_wlc_list else -1
            if idx >= 0:
                try:
                    self.query_one(f"#live-wlc-{idx}", Button).add_class("live-wlc-focused")
                except Exception:
                    pass
        self._update_live_ui()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route WLC-bar button presses to the focus handler."""
        btn_id = event.button.id or ""
        if btn_id == "live-wlc-all":
            self._live_set_focus(None)
        elif btn_id.startswith("live-wlc-"):
            try:
                idx = int(btn_id.removeprefix("live-wlc-"))
                if 0 <= idx < len(self._live_wlc_list):
                    self._live_set_focus(self._live_wlc_list[idx])
            except ValueError:
                pass

    # ------------------------------------------------------------------ helpers

    def _show_error(self, msg: str) -> None:
        bar = self.query_one(_WID_ERROR_BAR, Static)
        bar.update(f"  ✕  {msg}")
        bar.display = True

    def _hide_error(self) -> None:
        self.query_one(_WID_ERROR_BAR, Static).display = False

    def _set_status(self, msg: str) -> None:
        self.query_one("#loading-status", Label).update(msg)

    def _log_clear(self) -> None:
        self.query_one("#loading-log", RichLog).clear()


# ===========================================================================
# Application
# ===========================================================================

class WLCCheckApp(App):
    CSS_PATH  = "styles.tcss"
    TITLE     = "WLCCheck"
    SUB_TITLE = "Cisco 9800 · RESTCONF · Pre/Post Verification"

    username:     str = ""
    password:     str = ""
    current_host: str = ""

    def on_mount(self) -> None:
        self.username = os.environ.get("WLC_USER", "")
        self.password = os.environ.get("WLC_PASS", "")
        inv_path = find_inventory()
        if inv_path:
            try:
                dnac_entries = load_dnac_entries(inv_path)
                if dnac_entries:
                    set_dnac_host(dnac_entries[0].host)
            except InventoryError:
                pass
        self.push_screen(MainScreen())
