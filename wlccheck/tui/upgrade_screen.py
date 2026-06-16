from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict, List, Optional

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from ..core.inventory import WLCEntry
from ..core.storage import SnapshotDB
from ..core.upgrade import WLCUpgradeClient
from ..core.upgrade_models import (
    APImageStatus,
    PredownloadPoll,
    PredownloadSession,
)


# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------

_S_GREEN  = "bold green"
_S_RED    = "bold red"
_S_YELLOW = "bold yellow"
_S_DIM    = "dim"

_STATE_STYLE: dict[str, tuple[str, str]] = {
    "initiated":      ("◌", "cyan"),
    "predownloading": ("⟳", _S_YELLOW),
    "completed":      ("✓", _S_GREEN),
    "not_supported":  ("—", _S_DIM),
    "failed":         ("✕", _S_RED),
    "idle":           ("·", _S_DIM),
    "unknown":        ("?", _S_DIM),
}

_STATE_LABELS: dict[str, str] = {
    "initiated":      "Initiated",
    "predownloading": "Predownloading",
    "completed":      "Completed",
    "not_supported":  "Not supported",
    "failed":         "Failed",
    "idle":           "Idle",
    "unknown":        "Unknown",
}

_POLL_INTERVAL_S  = 60
_WID_STATUS_BAR   = "#status-bar"
_WID_TRIGGER_BTN  = "#trigger-btn"
_WID_SITE_TAG_LST = "#site-tag-list"
_WID_AP_TABLE     = "#ap-table"
_WID_LOG          = "#upgrade-log"


def _fmt_state(state: str) -> Text:
    icon, style = _STATE_STYLE.get(state, ("?", _S_DIM))
    return Text(f"{icon} {_STATE_LABELS.get(state, state)}", style=style)


# ---------------------------------------------------------------------------
# Inter-thread messages
# ---------------------------------------------------------------------------

class UpgradeStatus(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class SiteTagsLoaded(Message):
    def __init__(self, site_tags: List[str], site_tag_map: Dict[str, str]) -> None:
        super().__init__()
        self.site_tags    = site_tags
        self.site_tag_map = site_tag_map


class TriggerComplete(Message):
    def __init__(self, session: PredownloadSession) -> None:
        super().__init__()
        self.session = session


class PollComplete(Message):
    def __init__(self, poll: PredownloadPoll) -> None:
        super().__init__()
        self.poll = poll


class UpgradeError(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


# ---------------------------------------------------------------------------
# Confirmation modals
# ---------------------------------------------------------------------------

class ConfirmExitModal(ModalScreen[bool]):
    """Ask the user to confirm before leaving the upgrade screen."""

    DEFAULT_CSS = """
    ConfirmExitModal { align: center middle; }
    ConfirmExitModal > Vertical {
        width: 50;
        height: auto;
        background: $surface;
        border: round $warning;
        padding: 1 2;
    }
    ConfirmExitModal #exit-title { text-style: bold; margin-bottom: 1; }
    ConfirmExitModal Horizontal  { height: auto; align: center middle; margin-top: 1; }
    ConfirmExitModal Button      { margin: 0 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Leave upgrade screen?", id="exit-title")
            yield Static("Any pre-download already triggered will continue on the WLC.")
            with Horizontal():
                yield Button("Leave",  variant="warning", id="btn-leave")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-leave")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmTriggerModal(ModalScreen[bool]):
    """Ask the user to confirm before triggering EIU pre-download."""

    DEFAULT_CSS = """
    ConfirmTriggerModal {
        align: center middle;
    }
    ConfirmTriggerModal > Vertical {
        width: 60;
        height: auto;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    ConfirmTriggerModal #confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfirmTriggerModal #confirm-tags {
        margin-bottom: 1;
    }
    ConfirmTriggerModal #confirm-note {
        color: $text-muted;
        margin-bottom: 1;
    }
    ConfirmTriggerModal Horizontal {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    ConfirmTriggerModal Button {
        margin: 0 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, selected_tags: List[str], ap_counts: Dict[str, int]) -> None:
        super().__init__()
        self._selected_tags = selected_tags
        self._ap_counts     = ap_counts

    def compose(self) -> ComposeResult:
        total = sum(self._ap_counts.get(t, 0) for t in self._selected_tags)
        lines = "\n".join(
            f"  • {tag}  ({self._ap_counts.get(tag, '?')} APs)"
            for tag in self._selected_tags
        )
        with Vertical():
            yield Static("⚠  Confirm EIU Pre-download", id="confirm-title")
            yield Static(
                f"This will trigger image pre-download on:\n\n{lines}\n\nTotal: {total} APs",
                id="confirm-tags",
            )
            yield Static(
                "APs will remain joined during pre-download.",
                id="confirm-note",
            )
            with Horizontal():
                yield Button("Confirm", variant="error",   id="btn-confirm")
                yield Button("Cancel",  variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

class PredownloadScreen(Screen):
    """
    EIU pre-download management screen.

    Flow:
      1. Load site-tags from WLC via RESTCONF capwap-data
      2. User selects site-tags + confirms
      3. Trigger via SSH (ap image predownload site-tag <name> start)
      4. Poll every 60s — show per-AP progress until completion or user exits
    """

    BINDINGS = [
        Binding("escape", "back",         "Back"),
        Binding("q",      "back",         "Back",        show=False),
        Binding("r",      "refresh_poll", "Refresh now", show=True),
    ]

    DEFAULT_CSS = """
    PredownloadScreen { background: $surface; }
    #upgrade-layout   { height: 1fr; }
    #left-panel       { width: 36; border: solid $primary; padding: 1; }
    #right-panel      { width: 1fr; border: solid $primary; }
    #ap-table         { height: 1fr; }
    #log-panel        { height: 14; border-top: solid $primary; }
    #status-bar       { height: 1; background: $primary-darken-2; padding: 0 1; }
    #trigger-btn      { margin-top: 1; }
    .section-title    { text-style: bold; padding: 0 0 1 0; }
    """

    def __init__(
        self,
        wlc_entries: List[WLCEntry],
        username: str,
        password: str,
        db: SnapshotDB,
        verify_ssl: bool = False,
    ) -> None:
        super().__init__()
        self._wlc_entries    = wlc_entries
        self._username       = username
        self._password       = password
        self._db             = db
        self._verify_ssl     = verify_ssl
        self._site_tag_map:   Dict[str, str] = {}
        self._ap_counts_by_tag: Dict[str, int] = {}
        self._session:        Optional[PredownloadSession] = None
        self._poll_num:       int = 0
        self._stop_poll       = threading.Event()

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("EIU Pre-Download", id="status-bar")
        with Horizontal(id="upgrade-layout"):
            with Vertical(id="left-panel"):
                yield Label("Site-tags", classes="section-title")
                yield SelectionList(id="site-tag-list")
                yield Button(
                    "▶  Trigger pre-download",
                    id="trigger-btn",
                    variant="primary",
                    disabled=True,
                )
        with Vertical(id="right-panel"):
            yield DataTable(id="ap-table", show_cursor=False)
            with Vertical(id="log-panel"):
                yield RichLog(id="upgrade-log", highlight=True, markup=True, wrap=True)
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        table = self.query_one(_WID_AP_TABLE, DataTable)
        table.add_columns(
            "AP Name", "Site-Tag", "State", "Pct",
            "Current Version", "Backup / Target", "Method",
        )
        self._log("Loading site-tags from WLC…")
        self._load_site_tags()

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=False)
    def _load_site_tags(self) -> None:
        from ..core.restconf import WLCClient

        site_tag_map: Dict[str, str] = {}
        all_tags: set[str] = set()

        for entry in self._wlc_entries:
            try:
                client = WLCClient(
                    host=entry.host,
                    username=self._username,
                    password=self._password,
                    verify_ssl=self._verify_ssl,
                )
                self.post_message(UpgradeStatus(f"Reading APs from {entry.name}…"))
                for rec in client.get_ap_data():
                    tag = rec.site_tag or "default-site-tag"
                    site_tag_map[rec.name] = tag
                    all_tags.add(tag)
            except Exception as exc:
                self.post_message(UpgradeStatus(f"[red]✕[/red] {entry.name}: {exc}"))

        self.post_message(SiteTagsLoaded(
            site_tags=sorted(all_tags),
            site_tag_map=site_tag_map,
        ))

    @work(thread=True, exclusive=False)
    def _trigger_worker(self, selected_tags: List[str]) -> None:
        import secrets as _sec
        import string as _str

        uid = "".join(_sec.choice(_str.ascii_lowercase + _str.digits) for _ in range(6))
        session = PredownloadSession(
            uuid=f"upgrade-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uid}",
            wlc_name=", ".join(e.name for e in self._wlc_entries),
            wlc_host=", ".join(e.host for e in self._wlc_entries),
            site_tags=selected_tags,
            started_at=datetime.now(),
        )

        def _cb(msg: str) -> None:
            self.post_message(UpgradeStatus(msg))

        for entry in self._wlc_entries:
            client = WLCUpgradeClient(
                host=entry.host,
                username=self._username,
                password=self._password,
                wlc_name=entry.name,
                verify_ssl=self._verify_ssl,
            )
            try:
                client.trigger_predownload(selected_tags, status_cb=_cb)
            except Exception as exc:
                self.post_message(UpgradeError(f"{entry.name}: {exc}"))
                return

        self._db.save_upgrade_session(session)
        self.post_message(TriggerComplete(session=session))

    @work(thread=True, exclusive=False)
    def _poll_worker(self) -> None:
        self._poll_num += 1

        def _cb(msg: str) -> None:
            self.post_message(UpgradeStatus(msg))

        for entry in self._wlc_entries:
            client = WLCUpgradeClient(
                host=entry.host,
                username=self._username,
                password=self._password,
                wlc_name=entry.name,
                verify_ssl=self._verify_ssl,
            )
            try:
                poll = client.poll(
                    poll_num=self._poll_num,
                    site_tags=self._session.site_tags if self._session else [],
                    site_tag_map=self._site_tag_map,
                    status_cb=_cb,
                )
                if self._session:
                    self._db.save_upgrade_poll(self._session.uuid, poll)
                self.post_message(PollComplete(poll=poll))
            except Exception as exc:
                self.post_message(UpgradeError(f"Poll failed on {entry.name}: {exc}"))

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def on_upgrade_status(self, msg: UpgradeStatus) -> None:
        self._log(msg.text)
        self.query_one(_WID_STATUS_BAR, Static).update(msg.text)

    def on_upgrade_error(self, msg: UpgradeError) -> None:
        self._log(f"[red bold]ERROR:[/red bold] {msg.error}")

    def on_site_tags_loaded(self, msg: SiteTagsLoaded) -> None:
        self._site_tag_map = msg.site_tag_map
        counts: Dict[str, int] = {}
        for tag in msg.site_tag_map.values():
            counts[tag] = counts.get(tag, 0) + 1
        self._ap_counts_by_tag = counts
        lst = self.query_one(_WID_SITE_TAG_LST, SelectionList)
        lst.clear_options()
        for tag in msg.site_tags:
            lst.add_option(Selection(tag, tag, initial_state=False))
        self.query_one(_WID_TRIGGER_BTN, Button).disabled = len(msg.site_tags) == 0
        self._log(f"[green]✓[/green] {len(msg.site_tags)} site-tag(s) loaded.")
        self.query_one(_WID_STATUS_BAR, Static).update(
            f"{len(msg.site_tags)} site-tag(s) — select and trigger"
        )

    def on_trigger_complete(self, msg: TriggerComplete) -> None:
        self._session = msg.session
        self._log(f"[green]✓[/green] Pre-download triggered — session {msg.session.uuid}")
        self._log(f"Polling every {_POLL_INTERVAL_S}s. Press [bold]R[/bold] to refresh.")
        self.query_one(_WID_TRIGGER_BTN, Button).disabled = True
        self.set_interval(_POLL_INTERVAL_S, self._on_poll_tick)

    def on_poll_complete(self, msg: PollComplete) -> None:
        self._refresh_table(msg.poll)
        if all(stp.is_done for stp in msg.poll.site_tag_progresses) and msg.poll.site_tag_progresses:
            self._log("[green bold]All eligible APs completed pre-downloading.[/green bold]")
            self._stop_poll.set()

    # ------------------------------------------------------------------
    # Button
    # ------------------------------------------------------------------

    @on(Button.Pressed, _WID_TRIGGER_BTN)
    def _on_trigger_pressed(self) -> None:
        selected = list(self.query_one(_WID_SITE_TAG_LST, SelectionList).selected)
        if not selected:
            self._log("[yellow]⚠[/yellow] Select at least one site-tag first.")
            return
        self.app.push_screen(
            ConfirmTriggerModal(selected, self._ap_counts_by_tag),
            self._on_trigger_confirmed,
        )

    def _on_trigger_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            return
        selected = list(self.query_one(_WID_SITE_TAG_LST, SelectionList).selected)
        self.query_one(_WID_TRIGGER_BTN, Button).disabled = True
        self._log(f"Triggering pre-download on {len(selected)} site-tag(s)…")
        self._trigger_worker(selected)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh_poll(self) -> None:
        if self._session:
            self._log("Manual refresh…")
            self._poll_worker()

    def action_back(self) -> None:
        self.app.push_screen(ConfirmExitModal(), self._on_exit_confirmed)

    def _on_exit_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            self._stop_poll.set()
            self.dismiss()

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _refresh_table(self, poll: PredownloadPoll) -> None:
        table = self.query_one(_WID_AP_TABLE, DataTable)
        table.clear()
        for stp in poll.site_tag_progresses:
            for ap in stp.ap_statuses:
                icon, style = _STATE_STYLE.get(ap.predownload_state, ("?", _S_DIM))
                pct_text = (
                    Text(f"{ap.img_pct}%", style=_S_YELLOW)
                    if ap.is_active and ap.img_pct > 0
                    else Text("—", style=_S_DIM)
                )
                table.add_row(
                    ap.name,
                    ap.site_tag or "—",
                    Text(f"{icon} {ap.display_state}", style=style),
                    pct_text,
                    ap.current_version or "—",
                    ap.predownload_version or ap.backup_version or "—",
                    ap.method or "—",
                )
        stats = poll.global_stats
        self.query_one(_WID_STATUS_BAR, Static).update(
            f"Poll #{poll.poll_num} @ {poll.timestamp.strftime('%H:%M:%S')}  |  "
            f"In progress: {stats.num_in_progress}  "
            f"Complete: {stats.num_complete}  "
            f"Failed: {stats.num_failed}"
        )

    # ------------------------------------------------------------------
    # Timer callback
    # ------------------------------------------------------------------

    def _on_poll_tick(self) -> None:
        if not self._stop_poll.is_set() and self._session:
            self._poll_worker()

    # ------------------------------------------------------------------
    # Log helper
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.query_one(_WID_LOG, RichLog).write(f"[dim]{ts}[/dim]  {msg}")
