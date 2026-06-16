from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# ---------------------------------------------------------------------------
# Predownload state normalisation
# ---------------------------------------------------------------------------

_PREDOWNLOAD_STATE_MAP: dict[str, str] = {
    "predownloading": "predownloading",
    "not-supported": "not_supported",
    "not_supported": "not_supported",
    "not supported": "not_supported",
    "initiated": "initiated",
    "completed-predownloading": "completed",
    "completed_predownloading": "completed",
    "completed predownloading": "completed",
    "completed": "completed",
    "failed": "failed",
    "failed-to-predownload": "failed",
    "failed_to_predownload": "failed",
    "failed to predownload": "failed",
    "idle": "idle",
}

PREDOWNLOAD_STATES = (
    "initiated", "predownloading", "completed", "not_supported", "failed", "idle", "unknown"
)

_PREDOWNLOAD_LABELS: dict[str, str] = {
    "initiated":      "Initiated",
    "predownloading": "Predownloading",
    "completed":      "Completed",
    "not_supported":  "Not supported",
    "failed":         "Failed",
    "idle":           "Idle",
    "unknown":        "Unknown",
}


def normalize_predownload_state(raw: str) -> str:
    return _PREDOWNLOAD_STATE_MAP.get(raw.strip().lower(), "unknown")


# ---------------------------------------------------------------------------
# Per-AP image status
# ---------------------------------------------------------------------------

@dataclass
class APImageStatus:
    name:                str
    wtp_mac:             str
    wlc_name:            str
    site_tag:            str
    current_version:     str       # running image
    backup_version:      str       # backup slot
    predownload_state:   str       # normalised via normalize_predownload_state
    predownload_version: str       # target version being downloaded
    img_pct:             int = 0   # AP-side download percentage (capwap-data)
    wlc_img_pct:         int = 0   # WLC-side percentage (capwap-data)
    img_eta:             int = 0   # ETA in seconds (capwap-data)
    retry_count:         int = 0
    method:              str = ""  # "CAPWAP" | "N/A"

    @property
    def is_supported(self) -> bool:
        return self.predownload_state != "not_supported"

    @property
    def is_done(self) -> bool:
        return self.predownload_state in ("completed", "not_supported")

    @property
    def is_active(self) -> bool:
        return self.predownload_state in ("initiated", "predownloading")

    @property
    def has_failed(self) -> bool:
        return self.predownload_state == "failed"

    @property
    def display_state(self) -> str:
        return _PREDOWNLOAD_LABELS.get(self.predownload_state, self.predownload_state)


# ---------------------------------------------------------------------------
# Site-tag aggregate progress
# ---------------------------------------------------------------------------

@dataclass
class SiteTagProgress:
    site_tag:    str
    wlc_name:    str
    ap_statuses: List[APImageStatus] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.ap_statuses)

    @property
    def initiated(self) -> int:
        return sum(1 for a in self.ap_statuses if a.predownload_state == "initiated")

    @property
    def predownloading(self) -> int:
        return sum(1 for a in self.ap_statuses if a.predownload_state == "predownloading")

    @property
    def completed(self) -> int:
        return sum(1 for a in self.ap_statuses if a.predownload_state == "completed")

    @property
    def not_supported(self) -> int:
        return sum(1 for a in self.ap_statuses if a.predownload_state == "not_supported")

    @property
    def failed(self) -> int:
        return sum(1 for a in self.ap_statuses if a.predownload_state == "failed")

    @property
    def eligible(self) -> int:
        """APs that can predownload (excludes not_supported)."""
        return sum(1 for a in self.ap_statuses if a.is_supported)

    @property
    def in_progress(self) -> bool:
        return any(a.is_active for a in self.ap_statuses)

    @property
    def is_done(self) -> bool:
        """True when no AP is still actively downloading."""
        return self.total > 0 and not self.in_progress

    @property
    def pct_complete(self) -> float:
        if self.eligible == 0:
            return 100.0
        return (self.completed / self.eligible) * 100


# ---------------------------------------------------------------------------
# Global aggregate from ap-img-predownload-stats (RESTCONF)
# ---------------------------------------------------------------------------

@dataclass
class GlobalPredownloadStats:
    wlc_name:        str
    num_initiated:   int  = 0
    num_in_progress: int  = 0
    num_complete:    int  = 0
    num_failed:      int  = 0
    is_active:       bool = False

    @property
    def total(self) -> int:
        return (
            self.num_initiated + self.num_in_progress
            + self.num_complete + self.num_failed
        )


# ---------------------------------------------------------------------------
# One poll snapshot
# ---------------------------------------------------------------------------

@dataclass
class PredownloadPoll:
    poll_num:            int
    timestamp:           datetime
    wlc_name:            str
    global_stats:        GlobalPredownloadStats
    site_tag_progresses: List[SiteTagProgress] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.global_stats.is_active

    @property
    def all_ap_statuses(self) -> List[APImageStatus]:
        statuses: List[APImageStatus] = []
        for stp in self.site_tag_progresses:
            statuses.extend(stp.ap_statuses)
        return statuses


# ---------------------------------------------------------------------------
# Full pre-download session (trigger + multiple polls)
# ---------------------------------------------------------------------------

@dataclass
class PredownloadSession:
    uuid:           str
    wlc_name:       str
    wlc_host:       str
    site_tags:      List[str]
    started_at:     datetime
    target_version: str                   = ""
    completed_at:   Optional[datetime]    = None
    polls:          List[PredownloadPoll] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    @property
    def latest_poll(self) -> Optional[PredownloadPoll]:
        return self.polls[-1] if self.polls else None

    @property
    def elapsed_seconds(self) -> float:
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()

    @property
    def elapsed_display(self) -> str:
        s = int(self.elapsed_seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m{sec:02d}s"
        if m:
            return f"{m}m{sec:02d}s"
        return f"{sec}s"
