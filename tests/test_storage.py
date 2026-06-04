"""Tests for wlccheck/core/storage.py — uses a tmp_path SQLite DB."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from wlccheck.core.models import APRecord, APStats, ClientRecord, WLANRecord, WLCResult
from wlccheck.core.storage import SnapshotDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path) -> SnapshotDB:
    return SnapshotDB(db_path=tmp_path / "test.db")


def _ap(name: str = "AP-01", state: str = "joined", wlc: str = "WLC-1") -> APRecord:
    return APRecord(
        wtp_mac="aabbccddeeff", name=name,
        raw_state=state, state=state,
        ip_addr="10.0.0.1", model="C9115AXI",
        location="Floor 1", wlc_name=wlc,
        policy_tag="PT", site_tag="ST", rf_tag="RF",
    )


def _wlc_result(name: str = "WLC-1", aps: list | None = None,
                ok: bool = True) -> WLCResult:
    records = aps or [_ap(wlc=name)]
    stats = APStats.from_records(records)
    return WLCResult(
        wlc_name=name, wlc_host=f"{name}.local",
        wlc_hostname=name, records=records, stats=stats, ok=ok,
    )


# ---------------------------------------------------------------------------
# Bootstrap / schema
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_db_file_created(self, tmp_path):
        db_path = tmp_path / "sub" / "test.db"
        SnapshotDB(db_path=db_path)
        assert db_path.exists()

    def test_double_bootstrap_is_idempotent(self, tmp_path):
        db_path = tmp_path / "test.db"
        SnapshotDB(db_path=db_path)
        SnapshotDB(db_path=db_path)


# ---------------------------------------------------------------------------
# save_run / get_recent_runs
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_save_returns_uuid(self, db):
        uuid = db.save_run("my-label", "snapshot", [_wlc_result()])
        assert uuid.startswith("snapshot-")

    def test_recent_runs_empty_initially(self, db):
        assert db.get_recent_runs() == []

    def test_save_and_retrieve_run(self, db):
        db.save_run("test", "snapshot", [_wlc_result()])
        runs = db.get_recent_runs()
        assert len(runs) == 1
        assert runs[0].label == "test"

    def test_run_label_none(self, db):
        db.save_run(None, "snapshot", [_wlc_result()])
        runs = db.get_recent_runs()
        assert runs[0].label is None

    def test_run_ordered_newest_first(self, db):
        uuid_first = db.save_run("first", "snapshot", [_wlc_result()])
        # Force created_at to be older so ordering is deterministic
        conn = sqlite3.connect(db._path)
        conn.execute("UPDATE runs SET created_at=? WHERE uuid=?",
                     ("2024-01-01T10:00:00", uuid_first))
        conn.commit()
        conn.close()
        db.save_run("second", "snapshot", [_wlc_result()])
        runs = db.get_recent_runs()
        assert runs[0].label == "second"

    def test_get_recent_runs_with_limit(self, db):
        for i in range(5):
            db.save_run(f"run-{i}", "snapshot", [_wlc_result()])
        runs = db.get_recent_runs(limit=3)
        assert len(runs) == 3

    def test_has_clients_flag_persisted(self, db):
        db.save_run("test", "snapshot", [_wlc_result()], has_clients=True)
        runs = db.get_recent_runs()
        assert runs[0].has_clients is True

    def test_has_clients_false_by_default(self, db):
        db.save_run("test", "snapshot", [_wlc_result()])
        runs = db.get_recent_runs()
        assert runs[0].has_clients is False

    def test_wlc_result_ok_flag_false(self, db):
        db.save_run("test", "snapshot", [_wlc_result(ok=False)])
        runs = db.get_recent_runs()
        assert runs[0].wlc_results[0].ok is False

    def test_multiple_wlc_results(self, db):
        db.save_run("test", "snapshot", [_wlc_result("WLC-1"), _wlc_result("WLC-2")])
        runs = db.get_recent_runs()
        assert len(runs[0].wlc_results) == 2

    def test_stats_preserved(self, db):
        ap_joined    = _ap("AP-J", "joined")
        ap_not_joined = _ap("AP-NJ", "not_joined")
        result = _wlc_result(aps=[ap_joined, ap_not_joined])
        db.save_run("test", "snapshot", [result])
        runs = db.get_recent_runs()
        stats = runs[0].wlc_results[0].stats
        assert stats.total == 2
        assert stats.joined == 1
        assert stats.not_joined == 1


# ---------------------------------------------------------------------------
# load_records
# ---------------------------------------------------------------------------

class TestLoadRecords:
    def test_load_records_roundtrip(self, db):
        ap = _ap("AP-TEST", "joined")
        uuid = db.save_run("test", "snapshot", [_wlc_result(aps=[ap])])
        records = db.load_records(uuid)
        assert len(records) == 1
        r = records[0]
        assert r.name == "AP-TEST"
        assert r.state == "joined"
        assert r.policy_tag == "PT"
        assert r.site_tag == "ST"
        assert r.rf_tag == "RF"
        assert r.wlc_name == "WLC-1"

    def test_load_records_empty_run(self, db):
        result = WLCResult(wlc_name="WLC-1", wlc_host="10.0.0.1",
                           wlc_hostname="WLC-1", records=[],
                           stats=APStats(), ok=True)
        uuid = db.save_run("empty", "snapshot", [result])
        assert db.load_records(uuid) == []

    def test_load_records_unknown_uuid(self, db):
        assert db.load_records("nonexistent") == []


# ---------------------------------------------------------------------------
# load_wlans
# ---------------------------------------------------------------------------

class TestLoadWlans:
    def test_load_wlans_roundtrip(self, db):
        wlan = WLANRecord(wlan_id=1, profile_name="PROF-1", ssid="Corp",
                          state="up", client_count=5, wlc_name="WLC-1")
        result = _wlc_result()
        result.wlans = [wlan]
        uuid = db.save_run("test", "snapshot", [result])
        wlans = db.load_wlans(uuid)
        assert len(wlans) == 1
        assert wlans[0].ssid == "Corp"
        assert wlans[0].client_count == 5

    def test_load_wlans_empty(self, db):
        uuid = db.save_run("test", "snapshot", [_wlc_result()])
        assert db.load_wlans(uuid) == []


# ---------------------------------------------------------------------------
# load_clients
# ---------------------------------------------------------------------------

class TestLoadClients:
    def test_load_clients_roundtrip(self, db):
        client = ClientRecord(
            mac="aa:bb:cc:dd:ee:ff", ap_name="AP-01",
            wlan_ssid="Corp", ipv4="10.0.0.100",
            ipv6="", state="run", username="user1", wlc_name="WLC-1",
        )
        result = _wlc_result()
        result.clients = [client]
        uuid = db.save_run("test", "snapshot", [result], has_clients=True)
        clients = db.load_clients(uuid)
        assert len(clients) == 1
        assert clients[0].mac == "aa:bb:cc:dd:ee:ff"
        assert clients[0].ipv4 == "10.0.0.100"
        assert clients[0].username == "user1"

    def test_load_clients_empty(self, db):
        uuid = db.save_run("test", "snapshot", [_wlc_result()])
        assert db.load_clients(uuid) == []


# ---------------------------------------------------------------------------
# purge_old_runs
# ---------------------------------------------------------------------------

class TestPurgeOldRuns:
    def test_purge_removes_old_runs(self, db):
        uuid = db.save_run("old", "snapshot", [_wlc_result()])
        old_ts = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db._path)
        conn.execute("UPDATE runs SET created_at=? WHERE uuid=?", (old_ts, uuid))
        conn.commit()
        conn.close()

        deleted = db.purge_old_runs(days=30)
        assert deleted == 1
        assert db.get_recent_runs() == []

    def test_purge_keeps_recent_runs(self, db):
        db.save_run("recent", "snapshot", [_wlc_result()])
        deleted = db.purge_old_runs(days=30)
        assert deleted == 0
        assert len(db.get_recent_runs()) == 1

    def test_purge_returns_zero_when_empty(self, db):
        assert db.purge_old_runs(days=30) == 0


# ---------------------------------------------------------------------------
# delete_run
# ---------------------------------------------------------------------------

class TestDeleteRun:
    def test_delete_removes_run(self, db):
        uuid = db.save_run("test", "snapshot", [_wlc_result()])
        db.delete_run(uuid)
        assert db.get_recent_runs() == []

    def test_delete_removes_ap_snapshots(self, db):
        uuid = db.save_run("test", "snapshot", [_wlc_result()])
        db.delete_run(uuid)
        assert db.load_records(uuid) == []

    def test_delete_nonexistent_is_noop(self, db):
        db.delete_run("nonexistent-uuid")


# ---------------------------------------------------------------------------
# get_recent_sessions (legacy table)
# ---------------------------------------------------------------------------

class TestGetRecentSessions:
    def test_returns_empty_when_no_sessions_table(self, db):
        result = db.get_recent_sessions()
        assert result == []
