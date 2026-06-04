"""Tests for wlccheck/core/diff.py"""
from __future__ import annotations

from wlccheck.core.diff import (
    APDiff,
    ClientDiff,
    DiffSummary,
    WLANDiff,
    _classify_ap,
    _diff_aps,
    _diff_clients,
    _diff_wlans,
    _sev_rank,
    compute_diff,
)
from wlccheck.core.models import APRecord, ClientRecord, WLANRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ap(mac: str, state: str, name: str = "", wlc: str = "WLC-1",
        policy: str = "", site: str = "", rf: str = "") -> APRecord:
    return APRecord(
        wtp_mac=mac, name=name or f"AP-{mac[-4:]}",
        raw_state=state, state=state,
        ip_addr="10.0.0.1", model="C9115AXI",
        location="", wlc_name=wlc,
        policy_tag=policy, site_tag=site, rf_tag=rf,
    )


def _wlan(wlan_id: int, state: str, clients: int = 0, wlc: str = "WLC-1",
          ssid: str = "Corp") -> WLANRecord:
    return WLANRecord(
        wlan_id=wlan_id, profile_name=f"PROF-{wlan_id}",
        ssid=ssid, state=state, client_count=clients, wlc_name=wlc,
    )


def _client(mac: str, state: str = "run", ipv4: str = "10.0.0.1",
            ipv6: str = "", wlc: str = "WLC-1") -> ClientRecord:
    return ClientRecord(
        mac=mac, ap_name="AP-01", wlan_ssid="Corp",
        ipv4=ipv4, ipv6=ipv6, state=state, username="", wlc_name=wlc,
    )


# ---------------------------------------------------------------------------
# _sev_rank
# ---------------------------------------------------------------------------

class TestSevRank:
    def test_order(self):
        assert _sev_rank("critical") < _sev_rank("warning") < _sev_rank("info")

    def test_unknown(self):
        assert _sev_rank("unknown") == 3


# ---------------------------------------------------------------------------
# _classify_ap
# ---------------------------------------------------------------------------

class TestClassifyAp:
    def test_joined_to_not_joined_is_critical(self):
        ct, sev = _classify_ap("joined", "not_joined")
        assert ct == "lost" and sev == "critical"

    def test_joined_to_downloading_is_warning(self):
        ct, sev = _classify_ap("joined", "downloading")
        assert ct == "degraded" and sev == "warning"

    def test_joined_to_reset_is_warning(self):
        _, sev = _classify_ap("joined", "reset")
        assert sev == "warning"

    def test_joined_to_discovery_is_warning(self):
        _, sev = _classify_ap("joined", "discovery")
        assert sev == "warning"

    def test_joined_to_standby_is_warning(self):
        _, sev = _classify_ap("joined", "standby")
        assert sev == "warning"

    def test_joined_to_other_is_warning(self):
        _, sev = _classify_ap("joined", "other")
        assert sev == "warning"

    def test_not_joined_to_joined_is_recovered(self):
        ct, sev = _classify_ap("not_joined", "joined")
        assert ct == "recovered" and sev == "info"

    def test_any_to_joined_is_recovered(self):
        ct, sev = _classify_ap("downloading", "joined")
        assert ct == "recovered" and sev == "info"

    def test_to_not_joined_is_degraded(self):
        _, sev = _classify_ap("downloading", "not_joined")
        assert sev == "warning"

    def test_to_reset_is_degraded(self):
        _, sev = _classify_ap("standby", "reset")
        assert sev == "warning"

    def test_other_transition(self):
        ct, sev = _classify_ap("standby", "discovery")
        assert ct == "state_change" and sev == "warning"


# ---------------------------------------------------------------------------
# AP diffs
# ---------------------------------------------------------------------------

class TestDiffAps:
    def test_no_changes_no_diffs(self):
        aps = [_ap("aa:bb:cc:dd:ee:01", "joined")]
        assert _diff_aps(aps, aps) == []

    def test_state_change_detected(self):
        pre  = [_ap("aa", "joined")]
        post = [_ap("aa", "not_joined")]
        diffs = _diff_aps(pre, post)
        assert len(diffs) == 1
        assert diffs[0].change_type == "lost"
        assert diffs[0].severity == "critical"

    def test_ap_disappeared(self):
        pre  = [_ap("aa", "joined")]
        diffs = _diff_aps(pre, [])
        assert len(diffs) == 1
        assert diffs[0].change_type == "disappeared"

    def test_ap_new(self):
        post = [_ap("bb", "joined")]
        diffs = _diff_aps([], post)
        assert len(diffs) == 1
        assert diffs[0].change_type == "new"
        assert diffs[0].severity == "info"

    def test_tag_change_detected(self):
        pre  = [_ap("aa", "joined", policy="P1", site="S1", rf="RF1")]
        post = [_ap("aa", "joined", policy="P2", site="S1", rf="RF1")]
        diffs = _diff_aps(pre, post)
        assert any(d.change_type == "tag_change" for d in diffs)

    def test_wlc_move_detected(self):
        pre  = [_ap("aa", "joined", wlc="WLC-1")]
        post = [_ap("aa", "joined", wlc="WLC-2")]
        diffs = _diff_aps(pre, post)
        assert any(d.change_type == "wlc_move" for d in diffs)

    def test_sorted_by_severity_then_name(self):
        pre  = [_ap("aa", "joined", name="ZZZ"), _ap("bb", "joined", name="AAA")]
        post = [_ap("aa", "not_joined", name="ZZZ"), _ap("bb", "not_joined", name="AAA")]
        diffs = _diff_aps(pre, post)
        assert diffs[0].name == "AAA"

    def test_fallback_to_name_when_no_mac(self):
        pre  = [APRecord(wtp_mac="", name="AP-NOMAC", raw_state="joined", state="joined",
                         ip_addr="", model="", location="")]
        post = [APRecord(wtp_mac="", name="AP-NOMAC", raw_state="not_joined", state="not_joined",
                         ip_addr="", model="", location="")]
        diffs = _diff_aps(pre, post)
        assert len(diffs) == 1

    def test_wlc_move_pre_wlc_field(self):
        pre  = [_ap("aa", "joined", wlc="WLC-1")]
        post = [_ap("aa", "joined", wlc="WLC-2")]
        diffs = _diff_aps(pre, post)
        wlc_moves = [d for d in diffs if d.change_type == "wlc_move"]
        assert wlc_moves[0].pre_wlc == "WLC-1"


# ---------------------------------------------------------------------------
# WLAN diffs
# ---------------------------------------------------------------------------

class TestDiffWlans:
    def test_no_changes_no_diffs(self):
        wlans = [_wlan(1, "up", 10)]
        assert _diff_wlans(wlans, wlans) == []

    def test_wlan_went_down(self):
        pre  = [_wlan(1, "up")]
        post = [_wlan(1, "down")]
        diffs = _diff_wlans(pre, post)
        assert len(diffs) == 1
        assert diffs[0].change_type == "down"
        assert diffs[0].severity == "critical"

    def test_wlan_came_up(self):
        pre  = [_wlan(1, "down")]
        post = [_wlan(1, "up")]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].change_type == "up"
        assert diffs[0].severity == "info"

    def test_state_other_change(self):
        pre  = [_wlan(1, "up")]
        post = [_wlan(1, "other")]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].change_type == "state_change"

    def test_client_drop_to_zero_is_critical(self):
        pre  = [_wlan(1, "up", clients=10)]
        post = [_wlan(1, "up", clients=0)]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].change_type == "client_drop"
        assert diffs[0].severity == "critical"

    def test_client_drop_50pct_is_critical(self):
        pre  = [_wlan(1, "up", clients=100)]
        post = [_wlan(1, "up", clients=50)]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].severity == "critical"

    def test_client_drop_20pct_is_warning(self):
        pre  = [_wlan(1, "up", clients=100)]
        post = [_wlan(1, "up", clients=79)]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].severity == "warning"

    def test_client_gain_is_info(self):
        pre  = [_wlan(1, "up", clients=10)]
        post = [_wlan(1, "up", clients=15)]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].change_type == "client_gain"
        assert diffs[0].severity == "info"

    def test_small_drop_is_info(self):
        pre  = [_wlan(1, "up", clients=100)]
        post = [_wlan(1, "up", clients=95)]
        diffs = _diff_wlans(pre, post)
        assert diffs[0].severity == "info"

    def test_wlan_removed(self):
        pre  = [_wlan(1, "up")]
        diffs = _diff_wlans(pre, [])
        assert diffs[0].change_type == "removed"

    def test_wlan_new(self):
        post = [_wlan(99, "up")]
        diffs = _diff_wlans([], post)
        assert diffs[0].change_type == "new"

    def test_client_drop_from_zero_pre(self):
        pre  = [_wlan(1, "up", clients=0)]
        post = [_wlan(1, "up", clients=0)]
        diffs = _diff_wlans(pre, post)
        assert diffs == []

    def test_client_delta_property(self):
        d = WLANDiff(wlan_id=1, profile_name="P", ssid="S", wlc_name="W",
                     pre_state="up", post_state="up",
                     pre_clients=10, post_clients=8,
                     change_type="client_drop", severity="info")
        assert d.client_delta == -2

    def test_client_delta_none_when_missing(self):
        d = WLANDiff(wlan_id=1, profile_name="P", ssid="S", wlc_name="W",
                     pre_state=None, post_state="up",
                     pre_clients=None, post_clients=5,
                     change_type="new", severity="info")
        assert d.client_delta is None


# ---------------------------------------------------------------------------
# Client diffs
# ---------------------------------------------------------------------------

class TestDiffClients:
    def test_no_changes_no_diffs(self):
        c = [_client("aa")]
        assert _diff_clients(c, c) == []

    def test_lost_ip(self):
        pre  = [_client("aa", ipv4="10.0.0.1")]
        post = [_client("aa", ipv4="")]
        diffs = _diff_clients(pre, post)
        assert diffs[0].change_type == "lost_ip"
        assert diffs[0].severity == "critical"

    def test_got_ip(self):
        pre  = [_client("aa", ipv4="")]
        post = [_client("aa", ipv4="10.0.0.1")]
        diffs = _diff_clients(pre, post)
        assert diffs[0].change_type == "got_ip"
        assert diffs[0].severity == "info"

    def test_state_degraded(self):
        pre  = [_client("aa", state="run")]
        post = [_client("aa", state="associated")]
        diffs = _diff_clients(pre, post)
        assert diffs[0].change_type == "state_change"
        assert diffs[0].severity == "warning"

    def test_disappeared_is_ignored(self):
        pre  = [_client("aa")]
        diffs = _diff_clients(pre, [])
        assert diffs == []

    def test_new_client_without_ip_flagged(self):
        post = [_client("bb", ipv4="")]
        diffs = _diff_clients([], post)
        assert diffs[0].change_type == "new_no_ip"

    def test_new_client_with_ip_not_flagged(self):
        post = [_client("bb", ipv4="10.0.0.1")]
        diffs = _diff_clients([], post)
        assert diffs == []

    def test_lost_ip_and_state_degraded_is_critical(self):
        pre  = [_client("aa", state="run", ipv4="10.0.0.1")]
        post = [_client("aa", state="associated", ipv4="")]
        diffs = _diff_clients(pre, post)
        assert diffs[0].severity == "critical"


# ---------------------------------------------------------------------------
# compute_diff integration
# ---------------------------------------------------------------------------

class TestComputeDiff:
    def test_empty_inputs(self):
        s = compute_diff([], [])
        assert s.pre_total == 0
        assert s.post_total == 0
        assert s.ap_diffs == []

    def test_counts_set_correctly(self):
        pre  = [_ap("aa", "joined")]
        post = [_ap("aa", "not_joined"), _ap("bb", "joined")]
        s = compute_diff(pre, post)
        assert s.pre_total == 1
        assert s.post_total == 2

    def test_no_client_data_flag(self):
        s = compute_diff([], [])
        assert s.has_client_data is False

    def test_with_client_data_flag(self):
        s = compute_diff([], [], pre_clients=[], post_clients=[])
        assert s.has_client_data is True

    def test_post_no_ip_pct_computed(self):
        post_clients = [_client("aa", ipv4=""), _client("bb", ipv4="10.0.0.1")]
        s = compute_diff([], [], pre_clients=[], post_clients=post_clients)
        assert s.post_no_ip_pct == 50.0

    def test_wlan_diffs_included(self):
        pre_wlans  = [_wlan(1, "up")]
        post_wlans = [_wlan(1, "down")]
        s = compute_diff([], [], pre_wlans=pre_wlans, post_wlans=post_wlans)
        assert len(s.wlan_diffs) == 1


# ---------------------------------------------------------------------------
# DiffSummary properties and filters
# ---------------------------------------------------------------------------

class TestDiffSummary:
    def _make_summary(self) -> DiffSummary:
        ap_critical = APDiff("AP-01", "aa", "10.0.0.1", "M",
                             "joined", "not_joined", "lost", "critical")
        ap_warning  = APDiff("AP-02", "bb", "10.0.0.2", "M",
                             "joined", "downloading", "degraded", "warning")
        ap_info     = APDiff("AP-03", "cc", "10.0.0.3", "M",
                             None, "joined", "new", "info")
        wlan_crit   = WLANDiff(1, "P1", "S1", "W", "up", "down", 10, 0, "down", "critical")
        wlan_warn   = WLANDiff(2, "P2", "S2", "W", "up", "up", 10, 5, "client_drop", "warning")
        cli_crit    = ClientDiff("aa", "AP-01", "S", "W", "run", "idle",
                                 "10.0.0.1", "", "lost_ip", "critical")
        cli_warn    = ClientDiff("bb", "AP-01", "S", "W", "run", "associated",
                                 "10.0.0.1", "10.0.0.1", "state_change", "warning")
        return DiffSummary(
            ap_diffs=[ap_critical, ap_warning, ap_info],
            wlan_diffs=[wlan_crit, wlan_warn],
            client_diffs=[cli_crit, cli_warn],
            pre_total=3, post_total=3,
        )

    def test_ap_severity_counts(self):
        s = self._make_summary()
        assert s.ap_critical == 1
        assert s.ap_warning == 1
        assert s.ap_info == 1

    def test_wlan_severity_counts(self):
        s = self._make_summary()
        assert s.wlan_critical == 1
        assert s.wlan_warning == 1

    def test_client_severity_counts(self):
        s = self._make_summary()
        assert s.client_critical == 1
        assert s.client_warning == 1

    def test_critical_count_combined(self):
        s = self._make_summary()
        assert s.critical_count == 3

    def test_warning_count_combined(self):
        s = self._make_summary()
        assert s.warning_count == 3

    def test_info_count_is_ap_only(self):
        s = self._make_summary()
        assert s.info_count == 1

    def test_has_issues(self):
        s = self._make_summary()
        assert s.has_issues is True

    def test_has_issues_false_when_empty(self):
        s = DiffSummary()
        assert s.has_issues is False

    def test_filter_ap_all(self):
        s = self._make_summary()
        assert len(s.filter_ap("all")) == 3

    def test_filter_ap_critical(self):
        s = self._make_summary()
        result = s.filter_ap("critical")
        assert all(d.severity == "critical" for d in result)

    def test_filter_ap_warning(self):
        s = self._make_summary()
        result = s.filter_ap("warning")
        assert all(d.severity in ("critical", "warning") for d in result)

    def test_filter_wlan_all(self):
        s = self._make_summary()
        assert len(s.filter_wlan("all")) == 2

    def test_filter_wlan_critical(self):
        s = self._make_summary()
        result = s.filter_wlan("critical")
        assert all(d.severity == "critical" for d in result)

    def test_filter_wlan_warning(self):
        s = self._make_summary()
        result = s.filter_wlan("warning")
        assert len(result) == 2

    def test_filter_client_all(self):
        s = self._make_summary()
        assert len(s.filter_client("all")) == 2

    def test_filter_client_critical(self):
        s = self._make_summary()
        result = s.filter_client("critical")
        assert all(d.severity == "critical" for d in result)

    def test_filter_client_warning(self):
        s = self._make_summary()
        result = s.filter_client("warning")
        assert len(result) == 2

    def test_legacy_diffs_property(self):
        s = self._make_summary()
        assert s.diffs is s.ap_diffs

    def test_legacy_filter(self):
        s = self._make_summary()
        assert s.filter("all") == s.filter_ap("all")
