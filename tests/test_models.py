"""Tests for wlccheck/core/models.py"""
from __future__ import annotations

from datetime import datetime

import pytest

from wlccheck.core.models import (
    APRecord,
    APStats,
    ClientRecord,
    ClientStats,
    Run,
    WLANRecord,
    WLCResult,
    normalize_client_state,
    normalize_state,
)


# ---------------------------------------------------------------------------
# normalize_state
# ---------------------------------------------------------------------------

class TestNormalizeState:
    def test_joined_variants(self):
        for raw in ("ap-state-registered", "ap-state-joined", "registered", "joined"):
            assert normalize_state(raw) == "joined"

    def test_not_joined_variants(self):
        for raw in ("ap-state-not-joined", "not-joined", "not_joined", "not-registered"):
            assert normalize_state(raw) == "not_joined"

    def test_downloading_variants(self):
        for raw in (
            "ap-state-downloading", "ap-state-ap-img-dwnld",
            "ap-state-ap-payload-dwnld", "ap-state-ap-cfg-dwnld",
            "downloading", "downloading-image", "downloading-config",
        ):
            assert normalize_state(raw) == "downloading"

    def test_reset_variants(self):
        for raw in ("ap-state-reset", "reset", "resetting"):
            assert normalize_state(raw) == "reset"

    def test_discovery_variants(self):
        for raw in ("ap-state-discovery", "discovery"):
            assert normalize_state(raw) == "discovery"

    def test_standby_variants(self):
        for raw in ("ap-state-standby-discovery", "ap-state-standby-joined", "standby"):
            assert normalize_state(raw) == "standby"

    def test_unknown_maps_to_other(self):
        assert normalize_state("totally-unknown-state") == "other"
        assert normalize_state("") == "other"

    def test_strips_whitespace(self):
        assert normalize_state("  joined  ") == "joined"

    def test_case_insensitive(self):
        assert normalize_state("JOINED") == "joined"
        assert normalize_state("Registered") == "joined"


# ---------------------------------------------------------------------------
# normalize_client_state
# ---------------------------------------------------------------------------

class TestNormalizeClientState:
    def test_run_variants(self):
        for raw in ("run", "client-state-run", "ms-run"):
            assert normalize_client_state(raw) == "run"

    def test_associated_variants(self):
        for raw in ("associated", "client-state-associated"):
            assert normalize_client_state(raw) == "associated"

    def test_authenticating_variants(self):
        for raw in ("authenticating", "client-state-authenticating"):
            assert normalize_client_state(raw) == "authenticating"

    def test_authenticated_variants(self):
        for raw in ("authenticated", "client-state-authenticated"):
            assert normalize_client_state(raw) == "authenticated"

    def test_idle_variants(self):
        for raw in ("idle", "client-state-idle"):
            assert normalize_client_state(raw) == "idle"

    def test_disassociated_variants(self):
        for raw in ("disassociated", "client-state-disassociated"):
            assert normalize_client_state(raw) == "disassociated"

    def test_dhcp_pending_variants(self):
        for raw in ("dhcp-pending", "ip-learn"):
            assert normalize_client_state(raw) == "dhcp_pending"

    def test_unknown_maps_to_other(self):
        assert normalize_client_state("weird-state") == "other"

    def test_strips_and_case_insensitive(self):
        assert normalize_client_state("  RUN  ") == "run"


# ---------------------------------------------------------------------------
# APRecord
# ---------------------------------------------------------------------------

def _make_ap(state: str = "joined", **kwargs) -> APRecord:
    return APRecord(
        wtp_mac="aabbccddeeff",
        name="AP-01",
        raw_state=state,
        state=state,
        ip_addr="10.0.0.1",
        model="C9115AXI",
        location="Floor 1",
        **kwargs,
    )


class TestAPRecord:
    def test_is_joined_true(self):
        assert _make_ap("joined").is_joined is True

    def test_is_joined_false(self):
        assert _make_ap("not_joined").is_joined is False

    def test_tags_key(self):
        ap = _make_ap(policy_tag="PT", site_tag="ST", rf_tag="RF")
        assert ap.tags_key == ("PT", "ST", "RF")

    def test_tags_key_defaults_empty(self):
        assert _make_ap().tags_key == ("", "", "")


# ---------------------------------------------------------------------------
# ClientRecord
# ---------------------------------------------------------------------------

def _make_client(ipv4: str = "10.0.0.1", ipv6: str = "", state: str = "run") -> ClientRecord:
    return ClientRecord(
        mac="aa:bb:cc:dd:ee:ff",
        ap_name="AP-01",
        wlan_ssid="Corp",
        ipv4=ipv4,
        ipv6=ipv6,
        state=state,
        username="user1",
    )


class TestClientRecord:
    def test_has_ip_with_ipv4(self):
        assert _make_client(ipv4="10.0.0.1").has_ip is True

    def test_has_ip_with_ipv6(self):
        assert _make_client(ipv4="", ipv6="2001:db8::1").has_ip is True

    def test_has_ip_no_ip(self):
        assert _make_client(ipv4="", ipv6="").has_ip is False

    def test_is_healthy_true(self):
        assert _make_client().is_healthy is True

    def test_is_healthy_no_ip(self):
        assert _make_client(ipv4="", ipv6="").is_healthy is False

    def test_is_healthy_wrong_state(self):
        assert _make_client(state="associated").is_healthy is False


# ---------------------------------------------------------------------------
# APStats
# ---------------------------------------------------------------------------

class TestAPStats:
    def test_from_empty(self):
        s = APStats.from_records([])
        assert s.total == 0
        assert s.joined == 0

    def test_from_records_counts(self):
        aps = [
            _make_ap("joined"),
            _make_ap("joined"),
            _make_ap("not_joined"),
            _make_ap("downloading"),
            _make_ap("other"),
        ]
        s = APStats.from_records(aps)
        assert s.total == 5
        assert s.joined == 2
        assert s.not_joined == 1
        assert s.downloading == 1
        assert s.other == 1

    def test_add(self):
        a = APStats(total=3, joined=2, not_joined=1, downloading=0, other=0)
        b = APStats(total=2, joined=1, not_joined=0, downloading=1, other=0)
        c = a + b
        assert c.total == 5
        assert c.joined == 3
        assert c.not_joined == 1
        assert c.downloading == 1


# ---------------------------------------------------------------------------
# ClientStats
# ---------------------------------------------------------------------------

class TestClientStats:
    def test_from_empty(self):
        s = ClientStats.from_records([])
        assert s.total == 0
        assert s.no_ip_pct == 0.0

    def test_from_records(self):
        clients = [
            _make_client(ipv4="1.1.1.1", state="run"),
            _make_client(ipv4="", state="associated"),
            _make_client(ipv4="2.2.2.2", state="run"),
        ]
        s = ClientStats.from_records(clients)
        assert s.total == 3
        assert s.with_ip == 2
        assert s.no_ip == 1
        assert s.run_state == 2
        assert s.other_state == 1

    def test_no_ip_pct(self):
        clients = [_make_client(ipv4=""), _make_client(ipv4="1.1.1.1")]
        s = ClientStats.from_records(clients)
        assert s.no_ip_pct == 50.0

    def test_no_ip_pct_zero_when_empty(self):
        s = ClientStats.from_records([])
        assert s.no_ip_pct == 0.0


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _make_wlc_result(name: str, joined: int = 2, total: int = 3, ok: bool = True) -> WLCResult:
    stats = APStats(total=total, joined=joined, not_joined=total - joined)
    records = [_make_ap("joined", wlc_name=name) for _ in range(joined)]
    records += [_make_ap("not_joined", wlc_name=name) for _ in range(total - joined)]
    return WLCResult(
        wlc_name=name, wlc_host=f"{name}.local",
        wlc_hostname=name, records=records, stats=stats, ok=ok,
    )


def _make_run(label: str | None = "test-run") -> Run:
    return Run(
        uuid="snap-20240101-abc123",
        label=label,
        session_type="snapshot",
        created_at=datetime(2024, 1, 1, 12, 0, 0),
        wlc_results=[_make_wlc_result("WLC-1"), _make_wlc_result("WLC-2")],
    )


class TestRun:
    def test_stats_aggregates(self):
        run = _make_run()
        assert run.stats.total == 6
        assert run.stats.joined == 4

    def test_all_records(self):
        run = _make_run()
        assert len(run.all_records) == 6

    def test_all_wlans_empty(self):
        run = _make_run()
        assert run.all_wlans == []

    def test_all_clients_empty(self):
        run = _make_run()
        assert run.all_clients == []

    def test_client_stats_from_clients(self):
        result = _make_wlc_result("WLC-1")
        result.clients = [_make_client(), _make_client(ipv4="")]
        run = Run(
            uuid="x", label=None, session_type="snapshot",
            created_at=datetime.now(), wlc_results=[result],
        )
        cs = run.client_stats
        assert cs.total == 2

    def test_failed_wlcs(self):
        run = Run(
            uuid="x", label=None, session_type="snapshot",
            created_at=datetime.now(),
            wlc_results=[
                _make_wlc_result("WLC-1", ok=True),
                _make_wlc_result("WLC-2", ok=False),
            ],
        )
        assert len(run.failed_wlcs) == 1
        assert run.failed_wlcs[0].wlc_name == "WLC-2"

    def test_display_label_with_label(self):
        run = _make_run(label="my-label")
        assert run.display_label == "my-label"

    def test_display_label_fallback_to_uuid(self):
        run = _make_run(label=None)
        assert run.display_label == run.uuid

    def test_display_time_format(self):
        run = _make_run()
        assert run.display_time == "2024-01-01 12:00:00"
