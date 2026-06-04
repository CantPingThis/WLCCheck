from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .models import APRecord, ClientRecord, WLANRecord


# ---------------------------------------------------------------------------
# AP diff
# ---------------------------------------------------------------------------

@dataclass
class APDiff:
    name: str
    wtp_mac: str
    ip_addr: str
    model: str
    pre_state: Optional[str]
    post_state: Optional[str]
    change_type: str
    severity: str
    wlc_name: str = ""
    # Tag changes (populated when change_type == "tag_change")
    pre_tags: tuple[str, str, str] = ("", "", "")
    post_tags: tuple[str, str, str] = ("", "", "")
    # WLC move (populated when change_type == "wlc_move")
    pre_wlc: str = ""


# ---------------------------------------------------------------------------
# WLAN diff
# ---------------------------------------------------------------------------

@dataclass
class WLANDiff:
    wlan_id: int
    profile_name: str
    ssid: str
    wlc_name: str
    pre_state: Optional[str]
    post_state: Optional[str]
    pre_clients: Optional[int]
    post_clients: Optional[int]
    change_type: str   # "down", "up", "client_drop", "client_gain", "new", "removed"
    severity: str      # "critical" | "warning" | "info"

    @property
    def client_delta(self) -> Optional[int]:
        if self.pre_clients is not None and self.post_clients is not None:
            return self.post_clients - self.pre_clients
        return None


# ---------------------------------------------------------------------------
# Client diff
# ---------------------------------------------------------------------------

@dataclass
class ClientDiff:
    mac: str
    ap_name: str
    wlan_ssid: str
    wlc_name: str
    pre_state: Optional[str]
    post_state: Optional[str]
    pre_ipv4: str
    post_ipv4: str
    change_type: str   # "lost_ip" | "got_ip" | "state_change" | "new_no_ip" | "disappeared"
    severity: str


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@dataclass
class DiffSummary:
    # AP
    ap_diffs: List[APDiff] = field(default_factory=list)
    pre_total: int = 0
    post_total: int = 0
    # WLAN
    wlan_diffs: List[WLANDiff] = field(default_factory=list)
    pre_wlan_count: int = 0
    post_wlan_count: int = 0
    # Client
    client_diffs: List[ClientDiff] = field(default_factory=list)
    pre_client_total: int = 0
    post_client_total: int = 0
    post_no_ip_pct: float = 0.0
    has_client_data: bool = False

    # ---- AP counts ----
    @property
    def ap_critical(self) -> int:
        return sum(1 for d in self.ap_diffs if d.severity == "critical")

    @property
    def ap_warning(self) -> int:
        return sum(1 for d in self.ap_diffs if d.severity == "warning")

    @property
    def ap_info(self) -> int:
        return sum(1 for d in self.ap_diffs if d.severity == "info")

    # ---- WLAN counts ----
    @property
    def wlan_critical(self) -> int:
        return sum(1 for d in self.wlan_diffs if d.severity == "critical")

    @property
    def wlan_warning(self) -> int:
        return sum(1 for d in self.wlan_diffs if d.severity == "warning")

    # ---- Client counts ----
    @property
    def client_critical(self) -> int:
        return sum(1 for d in self.client_diffs if d.severity == "critical")

    @property
    def client_warning(self) -> int:
        return sum(1 for d in self.client_diffs if d.severity == "warning")

    # ---- Combined severity ----
    @property
    def critical_count(self) -> int:
        return self.ap_critical + self.wlan_critical + self.client_critical

    @property
    def warning_count(self) -> int:
        return self.ap_warning + self.wlan_warning + self.client_warning

    @property
    def info_count(self) -> int:
        return self.ap_info

    @property
    def has_issues(self) -> bool:
        return self.critical_count > 0 or self.warning_count > 0

    # ---- AP filter ----
    def filter_ap(self, level: str) -> List[APDiff]:
        if level == "all":
            return self.ap_diffs
        if level == "critical":
            return [d for d in self.ap_diffs if d.severity == "critical"]
        return [d for d in self.ap_diffs if d.severity in ("critical", "warning")]

    # ---- WLAN filter ----
    def filter_wlan(self, level: str) -> List[WLANDiff]:
        if level == "all":
            return self.wlan_diffs
        if level == "critical":
            return [d for d in self.wlan_diffs if d.severity == "critical"]
        return [d for d in self.wlan_diffs if d.severity in ("critical", "warning")]

    # ---- Client filter ----
    def filter_client(self, level: str) -> List[ClientDiff]:
        if level == "all":
            return self.client_diffs
        if level == "critical":
            return [d for d in self.client_diffs if d.severity == "critical"]
        return [d for d in self.client_diffs if d.severity in ("critical", "warning")]

    # ---- Legacy: combined diffs for old code paths ----
    @property
    def diffs(self) -> List[APDiff]:
        return self.ap_diffs

    def filter(self, level: str) -> List[APDiff]:
        return self.filter_ap(level)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def compute_diff(
    pre_records: List[APRecord],
    post_records: List[APRecord],
    pre_wlans: Optional[List[WLANRecord]] = None,
    post_wlans: Optional[List[WLANRecord]] = None,
    pre_clients: Optional[List[ClientRecord]] = None,
    post_clients: Optional[List[ClientRecord]] = None,
) -> DiffSummary:
    summary = DiffSummary(
        pre_total=len(pre_records),
        post_total=len(post_records),
        pre_wlan_count=len(pre_wlans or []),
        post_wlan_count=len(post_wlans or []),
        pre_client_total=len(pre_clients or []),
        post_client_total=len(post_clients or []),
        has_client_data=(pre_clients is not None and post_clients is not None),
    )

    summary.ap_diffs    = _diff_aps(pre_records, post_records)
    summary.wlan_diffs  = _diff_wlans(pre_wlans or [], post_wlans or [])

    if pre_clients is not None and post_clients is not None:
        summary.client_diffs = _diff_clients(pre_clients, post_clients)
        if post_clients:
            no_ip = sum(1 for c in post_clients if not c.has_ip)
            summary.post_no_ip_pct = (no_ip / len(post_clients)) * 100

    return summary


# ---------------------------------------------------------------------------
# AP diff
# ---------------------------------------------------------------------------

def _diff_aps(
    pre_records: List[APRecord],
    post_records: List[APRecord],
) -> List[APDiff]:
    pre_by_mac  = {r.wtp_mac: r for r in pre_records  if r.wtp_mac}
    post_by_mac = {r.wtp_mac: r for r in post_records if r.wtp_mac}
    pre_by_name  = {r.name: r for r in pre_records  if r.name and not r.wtp_mac}
    post_by_name = {r.name: r for r in post_records if r.name and not r.wtp_mac}

    diffs: List[APDiff] = []
    _process_ap_pairs(pre_by_mac, post_by_mac, diffs)
    _process_ap_pairs(pre_by_name, post_by_name, diffs)
    diffs.sort(key=lambda d: (_sev_rank(d.severity), d.name.lower()))
    return diffs


def _process_ap_pairs(pre_map: dict, post_map: dict, diffs: List[APDiff]) -> None:
    for key in set(pre_map) | set(post_map):
        pre  = pre_map.get(key)
        post = post_map.get(key)
        if pre and post:
            _process_ap_pair(pre, post, diffs)
        elif pre:
            diffs.append(_ap_disappeared(pre))
        else:
            assert post is not None
            diffs.append(_ap_new(post))


def _process_ap_pair(pre: APRecord, post: APRecord, diffs: List[APDiff]) -> None:
    if pre.state != post.state:
        diffs.append(_ap_state_change(pre, post))
    if pre.tags_key != post.tags_key:
        diffs.append(_ap_tag_change(pre, post))
    if pre.wlc_name and post.wlc_name and pre.wlc_name != post.wlc_name:
        diffs.append(_ap_wlc_move(pre, post))


def _ap_state_change(pre: APRecord, post: APRecord) -> APDiff:
    change_type, severity = _classify_ap(pre.state, post.state)
    return APDiff(
        name=pre.name, wtp_mac=pre.wtp_mac,
        ip_addr=post.ip_addr or pre.ip_addr,
        model=post.model or pre.model,
        pre_state=pre.state, post_state=post.state,
        change_type=change_type, severity=severity,
        wlc_name=post.wlc_name or pre.wlc_name,
    )


def _ap_tag_change(pre: APRecord, post: APRecord) -> APDiff:
    return APDiff(
        name=pre.name, wtp_mac=pre.wtp_mac,
        ip_addr=post.ip_addr or pre.ip_addr,
        model=post.model or pre.model,
        pre_state=pre.state, post_state=post.state,
        change_type="tag_change", severity="warning",
        wlc_name=post.wlc_name or pre.wlc_name,
        pre_tags=pre.tags_key,
        post_tags=post.tags_key,
    )


def _ap_disappeared(pre: APRecord) -> APDiff:
    return APDiff(
        name=pre.name, wtp_mac=pre.wtp_mac,
        ip_addr=pre.ip_addr, model=pre.model,
        pre_state=pre.state, post_state=None,
        change_type="disappeared", severity="critical",
        wlc_name=pre.wlc_name,
    )


def _ap_new(post: APRecord) -> APDiff:
    return APDiff(
        name=post.name, wtp_mac=post.wtp_mac,
        ip_addr=post.ip_addr, model=post.model,
        pre_state=None, post_state=post.state,
        change_type="new", severity="info",
        wlc_name=post.wlc_name,
    )


def _ap_wlc_move(pre: APRecord, post: APRecord) -> APDiff:
    return APDiff(
        name=pre.name, wtp_mac=pre.wtp_mac,
        ip_addr=post.ip_addr or pre.ip_addr,
        model=post.model or pre.model,
        pre_state=pre.state, post_state=post.state,
        change_type="wlc_move", severity="warning",
        wlc_name=post.wlc_name,
        pre_wlc=pre.wlc_name,
    )


def _classify_ap(pre: str, post: str) -> tuple[str, str]:
    if pre == "joined" and post == "not_joined":
        return "lost", "critical"
    if pre == "joined" and post in ("downloading", "reset", "discovery", "standby", "other"):
        return "degraded", "warning"
    if pre == "not_joined" and post == "joined":
        return "recovered", "info"
    if post == "joined":
        return "recovered", "info"
    if post in ("not_joined", "reset"):
        return "degraded", "warning"
    return "state_change", "warning"


# ---------------------------------------------------------------------------
# WLAN diff
# ---------------------------------------------------------------------------

def _classify_wlan_state(pre_state: str, post_state: str) -> tuple[str, str]:
    if pre_state == "up" and post_state == "down":
        return "down", "critical"
    if pre_state == "down" and post_state == "up":
        return "up", "info"
    return "state_change", "warning"


def _classify_wlan_clients(pre_count: int, post_count: int) -> tuple[str, str]:
    delta = post_count - pre_count
    if post_count == 0 and pre_count > 0:
        return "client_drop", "critical"
    pct_drop = (pre_count - post_count) / pre_count * 100 if pre_count > 0 else 0
    if pct_drop >= 50:
        return "client_drop", "critical"
    if pct_drop >= 20:
        return "client_drop", "warning"
    return ("client_gain" if delta > 0 else "client_drop"), "info"


def _diff_wlans(
    pre_wlans: List[WLANRecord],
    post_wlans: List[WLANRecord],
) -> List[WLANDiff]:
    pre_map  = {(w.wlc_name, w.wlan_id): w for w in pre_wlans}
    post_map = {(w.wlc_name, w.wlan_id): w for w in post_wlans}

    diffs: List[WLANDiff] = []

    for key in set(pre_map) | set(post_map):
        pre  = pre_map.get(key)
        post = post_map.get(key)

        if pre and post:
            if pre.state != post.state:
                ct, sev = _classify_wlan_state(pre.state, post.state)
                diffs.append(WLANDiff(
                    wlan_id=pre.wlan_id, profile_name=pre.profile_name,
                    ssid=pre.ssid, wlc_name=pre.wlc_name,
                    pre_state=pre.state, post_state=post.state,
                    pre_clients=pre.client_count, post_clients=post.client_count,
                    change_type=ct, severity=sev,
                ))
            elif pre.client_count != post.client_count:
                ct, sev = _classify_wlan_clients(pre.client_count, post.client_count)
                diffs.append(WLANDiff(
                    wlan_id=pre.wlan_id, profile_name=pre.profile_name,
                    ssid=pre.ssid, wlc_name=pre.wlc_name,
                    pre_state=pre.state, post_state=post.state,
                    pre_clients=pre.client_count, post_clients=post.client_count,
                    change_type=ct, severity=sev,
                ))
        elif pre:
            diffs.append(WLANDiff(
                wlan_id=pre.wlan_id, profile_name=pre.profile_name,
                ssid=pre.ssid, wlc_name=pre.wlc_name,
                pre_state=pre.state, post_state=None,
                pre_clients=pre.client_count, post_clients=None,
                change_type="removed", severity="warning",
            ))
        else:
            assert post is not None
            diffs.append(WLANDiff(
                wlan_id=post.wlan_id, profile_name=post.profile_name,
                ssid=post.ssid, wlc_name=post.wlc_name,
                pre_state=None, post_state=post.state,
                pre_clients=None, post_clients=post.client_count,
                change_type="new", severity="info",
            ))

    diffs.sort(key=lambda d: (_sev_rank(d.severity), d.ssid.lower()))
    return diffs


# ---------------------------------------------------------------------------
# Client diff
# ---------------------------------------------------------------------------

def _diff_clients(
    pre_clients: List[ClientRecord],
    post_clients: List[ClientRecord],
) -> List[ClientDiff]:
    pre_map  = {c.mac: c for c in pre_clients  if c.mac}
    post_map = {c.mac: c for c in post_clients if c.mac}

    diffs: List[ClientDiff] = []

    for mac in set(pre_map) | set(post_map):
        pre  = pre_map.get(mac)
        post = post_map.get(mac)

        if pre and post:
            ip_lost   = pre.has_ip  and not post.has_ip
            ip_gained = not pre.has_ip and post.has_ip
            state_degraded = pre.state == "run" and post.state != "run"

            if ip_lost:
                diffs.append(ClientDiff(
                    mac=mac, ap_name=post.ap_name, wlan_ssid=post.wlan_ssid,
                    wlc_name=post.wlc_name,
                    pre_state=pre.state, post_state=post.state,
                    pre_ipv4=pre.ipv4, post_ipv4=post.ipv4,
                    change_type="lost_ip", severity="critical",
                ))
            elif ip_gained:
                diffs.append(ClientDiff(
                    mac=mac, ap_name=post.ap_name, wlan_ssid=post.wlan_ssid,
                    wlc_name=post.wlc_name,
                    pre_state=pre.state, post_state=post.state,
                    pre_ipv4=pre.ipv4, post_ipv4=post.ipv4,
                    change_type="got_ip", severity="info",
                ))
            elif state_degraded:
                diffs.append(ClientDiff(
                    mac=mac, ap_name=post.ap_name, wlan_ssid=post.wlan_ssid,
                    wlc_name=post.wlc_name,
                    pre_state=pre.state, post_state=post.state,
                    pre_ipv4=pre.ipv4, post_ipv4=post.ipv4,
                    change_type="state_change", severity="warning",
                ))
        elif pre:
            # Client disappeared — only flag if it had no IP (suspicious)
            # Normal roaming/disconnect is expected, not worth flagging
            pass
        else:
            assert post is not None
            if not post.has_ip:
                diffs.append(ClientDiff(
                    mac=mac, ap_name=post.ap_name, wlan_ssid=post.wlan_ssid,
                    wlc_name=post.wlc_name,
                    pre_state=None, post_state=post.state,
                    pre_ipv4="", post_ipv4=post.ipv4,
                    change_type="new_no_ip", severity="warning",
                ))

    diffs.sort(key=lambda d: (_sev_rank(d.severity), d.mac))
    return diffs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sev_rank(s: str) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(s, 3)
