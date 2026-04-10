from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


# ---------------------------------------------------------------------------
# AP state normalisation
# ---------------------------------------------------------------------------

_STATE_MAP: dict[str, str] = {
    # IOS-XE ap-state enum variants (17.3–17.6 style)
    "ap-state-registered": "joined",
    "ap-state-joined": "joined",
    "registered": "joined",
    "joined": "joined",
    "ap-state-not-joined": "not_joined",
    "not-joined": "not_joined",
    "not_joined": "not_joined",
    "ap-state-downloading": "downloading",
    "ap-state-ap-img-dwnld": "downloading",
    "ap-state-ap-payload-dwnld": "downloading",
    "ap-state-ap-cfg-dwnld": "downloading",
    "downloading": "downloading",
    "ap-state-reset": "reset",
    "reset": "reset",
    "ap-state-discovery": "discovery",
    "discovery": "discovery",
    "ap-state-standby-discovery": "standby",
    "ap-state-standby-joined": "standby",
    "standby": "standby",
    # IOS-XE ap-operation-state enum variants (17.9+ style)
    "not-registered": "not_joined",
    "downloading-image": "downloading",
    "downloading-config": "downloading",
    "resetting": "reset",
}

# Client state normalisation
_CLIENT_STATE_MAP: dict[str, str] = {
    "run": "run",
    "client-state-run": "run",
    "ms-run": "run",
    "associated": "associated",
    "client-state-associated": "associated",
    "authenticating": "authenticating",
    "client-state-authenticating": "authenticating",
    "authenticated": "authenticated",
    "client-state-authenticated": "authenticated",
    "idle": "idle",
    "client-state-idle": "idle",
    "disassociated": "disassociated",
    "client-state-disassociated": "disassociated",
    "dhcp-pending": "dhcp_pending",
    "ip-learn": "dhcp_pending",
}

NORMALIZED_STATES = (
    "joined", "not_joined", "downloading", "reset", "discovery", "standby", "other"
)


def normalize_state(raw: str) -> str:
    return _STATE_MAP.get(raw.strip().lower(), "other")


def normalize_client_state(raw: str) -> str:
    return _CLIENT_STATE_MAP.get(raw.strip().lower(), "other")


# ---------------------------------------------------------------------------
# AP record
# ---------------------------------------------------------------------------

@dataclass
class APRecord:
    wtp_mac: str
    name: str
    raw_state: str
    state: str
    ip_addr: str
    model: str
    location: str
    wlc_name:   str = ""
    policy_tag: str = ""
    site_tag:   str = ""
    rf_tag:     str = ""

    @property
    def is_joined(self) -> bool:
        return self.state == "joined"

    @property
    def tags_key(self) -> tuple[str, str, str]:
        return (self.policy_tag, self.site_tag, self.rf_tag)


# ---------------------------------------------------------------------------
# WLAN record
# ---------------------------------------------------------------------------

@dataclass
class WLANRecord:
    wlan_id:      int
    profile_name: str
    ssid:         str
    state:        str    # "up" | "down" | "other"
    client_count: int
    wlc_name:     str = ""


# ---------------------------------------------------------------------------
# Client record
# ---------------------------------------------------------------------------

@dataclass
class ClientRecord:
    mac:       str
    ap_name:   str
    wlan_ssid: str
    ipv4:      str   # "" = no IP
    ipv6:      str   # "" = no IP
    state:     str   # normalised via normalize_client_state
    username:  str
    wlc_name:  str = ""

    @property
    def has_ip(self) -> bool:
        return bool(self.ipv4 or self.ipv6)

    @property
    def is_healthy(self) -> bool:
        return self.state == "run" and self.has_ip


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------

@dataclass
class APStats:
    total: int = 0
    joined: int = 0
    not_joined: int = 0
    downloading: int = 0
    other: int = 0

    @classmethod
    def from_records(cls, records: List[APRecord]) -> APStats:
        s = cls(total=len(records))
        for ap in records:
            if ap.state == "joined":
                s.joined += 1
            elif ap.state == "not_joined":
                s.not_joined += 1
            elif ap.state == "downloading":
                s.downloading += 1
            else:
                s.other += 1
        return s

    def __add__(self, other: APStats) -> APStats:
        return APStats(
            total=self.total + other.total,
            joined=self.joined + other.joined,
            not_joined=self.not_joined + other.not_joined,
            downloading=self.downloading + other.downloading,
            other=self.other + other.other,
        )


@dataclass
class ClientStats:
    total: int = 0
    with_ip: int = 0
    no_ip: int = 0
    run_state: int = 0
    other_state: int = 0

    @classmethod
    def from_records(cls, records: List[ClientRecord]) -> ClientStats:
        s = cls(total=len(records))
        for c in records:
            if c.has_ip:
                s.with_ip += 1
            else:
                s.no_ip += 1
            if c.state == "run":
                s.run_state += 1
            else:
                s.other_state += 1
        return s

    @property
    def no_ip_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.no_ip / self.total) * 100


# ---------------------------------------------------------------------------
# Per-WLC collection result
# ---------------------------------------------------------------------------

@dataclass
class WLCResult:
    wlc_name: str
    wlc_host: str
    wlc_hostname: Optional[str]
    records: List[APRecord]
    stats: APStats
    ok: bool
    error: str = ""
    wlans: List[WLANRecord] = field(default_factory=list)
    clients: List[ClientRecord] = field(default_factory=list)
    client_stats: ClientStats = field(default_factory=ClientStats)


# ---------------------------------------------------------------------------
# Run (aggregate of one or more WLCResults)
# ---------------------------------------------------------------------------

@dataclass
class Run:
    uuid: str
    label: Optional[str]
    session_type: str
    created_at: datetime
    wlc_results: List[WLCResult]
    has_clients: bool = False

    @property
    def stats(self) -> APStats:
        result = APStats()
        for w in self.wlc_results:
            result = result + w.stats
        return result

    @property
    def all_records(self) -> List[APRecord]:
        records: List[APRecord] = []
        for w in self.wlc_results:
            records.extend(w.records)
        return records

    @property
    def all_wlans(self) -> List[WLANRecord]:
        wlans: List[WLANRecord] = []
        for w in self.wlc_results:
            wlans.extend(w.wlans)
        return wlans

    @property
    def all_clients(self) -> List[ClientRecord]:
        clients: List[ClientRecord] = []
        for w in self.wlc_results:
            clients.extend(w.clients)
        return clients

    @property
    def client_stats(self) -> ClientStats:
        return ClientStats.from_records(self.all_clients)

    @property
    def failed_wlcs(self) -> List[WLCResult]:
        return [w for w in self.wlc_results if not w.ok]

    @property
    def display_label(self) -> str:
        return self.label or self.uuid

    @property
    def display_time(self) -> str:
        return self.created_at.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Live monitor data
# ---------------------------------------------------------------------------

@dataclass
class LivePollRecord:
    """Aggregate AP counts for one WLC captured during one live poll."""
    poll_num:   int
    timestamp:  datetime
    wlc_name:   str
    total:      int
    joined:     int
    not_joined: int
    other:      int


@dataclass
class APStateEvent:
    """A single AP state transition detected during live monitoring."""
    timestamp:  datetime
    poll_num:   int
    wlc_name:   str
    ap_name:    str
    wtp_mac:    str
    from_state: str   # "—" when no prior data exists
    to_state:   str


# ---------------------------------------------------------------------------
# Legacy alias
# ---------------------------------------------------------------------------

@dataclass
class Session:
    uuid: str
    wlc_host: str
    wlc_hostname: Optional[str]
    session_type: str
    label: Optional[str]
    created_at: datetime
    stats: APStats
    records: List[APRecord] = field(default_factory=list)

    @property
    def display_label(self) -> str:
        return self.label or self.uuid

    @property
    def display_time(self) -> str:
        return self.created_at.strftime("%Y-%m-%d %H:%M:%S")
