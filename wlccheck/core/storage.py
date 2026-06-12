from __future__ import annotations

import secrets
import sqlite3
import string
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .models import (
    APRecord,
    APStats,
    ClientRecord,
    ClientStats,
    Run,
    Session,
    WLANRecord,
    WLCResult,
)


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_DATA_DIR       = Path.home() / ".wlccheck"
_DB_FILE        = _DATA_DIR / "wlccheck.db"
_RETENTION_DAYS = 30


def _short_id(n: int = 6) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _make_uuid(prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{ts}-{_short_id()}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    uuid         TEXT PRIMARY KEY,
    label        TEXT,
    session_type TEXT NOT NULL DEFAULT 'snapshot',
    created_at   TEXT NOT NULL,
    has_clients  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_wlcs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid       TEXT NOT NULL,
    wlc_name       TEXT NOT NULL,
    wlc_host       TEXT NOT NULL,
    wlc_hostname   TEXT,
    ok             INTEGER NOT NULL DEFAULT 1,
    error          TEXT    DEFAULT '',
    ap_total       INTEGER DEFAULT 0,
    ap_joined      INTEGER DEFAULT 0,
    ap_not_joined  INTEGER DEFAULT 0,
    ap_downloading INTEGER DEFAULT 0,
    ap_other       INTEGER DEFAULT 0,
    FOREIGN KEY (run_uuid) REFERENCES runs(uuid)
);

CREATE TABLE IF NOT EXISTS ap_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid    TEXT NOT NULL,
    wlc_name    TEXT NOT NULL DEFAULT '',
    wtp_mac     TEXT,
    name        TEXT,
    raw_state   TEXT,
    state       TEXT,
    ip_addr     TEXT,
    model       TEXT,
    location    TEXT,
    policy_tag  TEXT DEFAULT '',
    site_tag    TEXT DEFAULT '',
    rf_tag      TEXT DEFAULT '',
    eth_mac     TEXT DEFAULT '',
    FOREIGN KEY (run_uuid) REFERENCES runs(uuid)
);

CREATE TABLE IF NOT EXISTS wlan_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid     TEXT NOT NULL,
    wlc_name     TEXT NOT NULL DEFAULT '',
    wlan_id      INTEGER,
    profile_name TEXT,
    ssid         TEXT,
    state        TEXT,
    client_count INTEGER DEFAULT 0,
    FOREIGN KEY (run_uuid) REFERENCES runs(uuid)
);

CREATE TABLE IF NOT EXISTS client_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid   TEXT NOT NULL,
    wlc_name   TEXT NOT NULL DEFAULT '',
    mac        TEXT,
    ap_name    TEXT,
    wlan_ssid  TEXT,
    ipv4       TEXT DEFAULT '',
    ipv6       TEXT DEFAULT '',
    state      TEXT,
    username   TEXT DEFAULT '',
    FOREIGN KEY (run_uuid) REFERENCES runs(uuid)
);

CREATE INDEX IF NOT EXISTS idx_snap_run    ON ap_snapshots(run_uuid);
CREATE INDEX IF NOT EXISTS idx_wlan_run    ON wlan_snapshots(run_uuid);
CREATE INDEX IF NOT EXISTS idx_client_run  ON client_snapshots(run_uuid);
CREATE INDEX IF NOT EXISTS idx_rwlc_run    ON run_wlcs(run_uuid);
"""

_MIGRATIONS = [
    # v0.3 — ap_snapshots wlc_name
    "ALTER TABLE ap_snapshots ADD COLUMN wlc_name TEXT NOT NULL DEFAULT ''",
    # v0.4 — tag columns
    "ALTER TABLE ap_snapshots ADD COLUMN policy_tag TEXT DEFAULT ''",
    "ALTER TABLE ap_snapshots ADD COLUMN site_tag   TEXT DEFAULT ''",
    "ALTER TABLE ap_snapshots ADD COLUMN rf_tag     TEXT DEFAULT ''",
    # v0.4 — has_clients flag on runs
    "ALTER TABLE runs ADD COLUMN has_clients INTEGER NOT NULL DEFAULT 0",
    # v0.5 — ethernet MAC column
    "ALTER TABLE ap_snapshots ADD COLUMN eth_mac TEXT DEFAULT ''",
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class SnapshotDB:

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._path = db_path or _DB_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_run(
        self,
        label: Optional[str],
        session_type: str,
        wlc_results: List[WLCResult],
        has_clients: bool = False,
    ) -> str:
        run_uuid = _make_uuid(session_type)
        now = datetime.now().isoformat(timespec="seconds")

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (uuid, label, session_type, created_at, has_clients) "
                "VALUES (?,?,?,?,?)",
                (run_uuid, label, session_type, now, 1 if has_clients else 0),
            )
            for result in wlc_results:
                stats = result.stats
                conn.execute(
                    """INSERT INTO run_wlcs
                        (run_uuid, wlc_name, wlc_host, wlc_hostname, ok, error,
                         ap_total, ap_joined, ap_not_joined, ap_downloading, ap_other)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_uuid,
                        result.wlc_name, result.wlc_host, result.wlc_hostname,
                        1 if result.ok else 0, result.error,
                        stats.total, stats.joined, stats.not_joined,
                        stats.downloading, stats.other,
                    ),
                )
                # AP snapshots
                conn.executemany(
                    """INSERT INTO ap_snapshots
                        (run_uuid, wlc_name, wtp_mac, name, raw_state, state,
                         ip_addr, model, location, policy_tag, site_tag, rf_tag,
                         eth_mac)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            run_uuid, r.wlc_name, r.wtp_mac, r.name,
                            r.raw_state, r.state, r.ip_addr, r.model, r.location,
                            r.policy_tag, r.site_tag, r.rf_tag, r.eth_mac,
                        )
                        for r in result.records
                    ],
                )
                # WLAN snapshots
                if result.wlans:
                    conn.executemany(
                        """INSERT INTO wlan_snapshots
                            (run_uuid, wlc_name, wlan_id, profile_name, ssid,
                             state, client_count)
                           VALUES (?,?,?,?,?,?,?)""",
                        [
                            (
                                run_uuid, w.wlc_name, w.wlan_id,
                                w.profile_name, w.ssid, w.state, w.client_count,
                            )
                            for w in result.wlans
                        ],
                    )
                # Client snapshots
                if result.clients:
                    conn.executemany(
                        """INSERT INTO client_snapshots
                            (run_uuid, wlc_name, mac, ap_name, wlan_ssid,
                             ipv4, ipv6, state, username)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        [
                            (
                                run_uuid, c.wlc_name, c.mac, c.ap_name,
                                c.wlan_ssid, c.ipv4, c.ipv6, c.state, c.username,
                            )
                            for c in result.clients
                        ],
                    )

        return run_uuid

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_recent_runs(self, limit: Optional[int] = None) -> List[Run]:
        with self._connect() as conn:
            if limit is not None:
                run_rows = conn.execute(
                    "SELECT uuid, label, session_type, created_at, has_clients "
                    "FROM runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                run_rows = conn.execute(
                    "SELECT uuid, label, session_type, created_at, has_clients "
                    "FROM runs ORDER BY created_at DESC",
                ).fetchall()

        runs: List[Run] = []
        for uuid, label, stype, created_at, has_clients in run_rows:
            wlc_results = self._load_wlc_results(uuid)
            runs.append(Run(
                uuid=uuid,
                label=label,
                session_type=stype,
                created_at=datetime.fromisoformat(created_at),
                wlc_results=wlc_results,
                has_clients=bool(has_clients),
            ))
        return runs

    def load_records(self, run_uuid: str) -> List[APRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT wlc_name, wtp_mac, name, raw_state, state,
                          ip_addr, model, location,
                          policy_tag, site_tag, rf_tag, eth_mac
                   FROM ap_snapshots WHERE run_uuid = ? ORDER BY name""",
                (run_uuid,),
            ).fetchall()
        return [
            APRecord(
                wlc_name=r[0], wtp_mac=r[1], name=r[2],
                raw_state=r[3], state=r[4],
                ip_addr=r[5], model=r[6], location=r[7],
                policy_tag=r[8] or "", site_tag=r[9] or "", rf_tag=r[10] or "",
                eth_mac=r[11] or "",
            )
            for r in rows
        ]

    def load_wlans(self, run_uuid: str) -> List[WLANRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT wlc_name, wlan_id, profile_name, ssid, state, client_count
                   FROM wlan_snapshots WHERE run_uuid = ? ORDER BY wlc_name, wlan_id""",
                (run_uuid,),
            ).fetchall()
        return [
            WLANRecord(
                wlc_name=r[0], wlan_id=r[1], profile_name=r[2],
                ssid=r[3], state=r[4], client_count=r[5],
            )
            for r in rows
        ]

    def load_clients(self, run_uuid: str) -> List[ClientRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT wlc_name, mac, ap_name, wlan_ssid,
                          ipv4, ipv6, state, username
                   FROM client_snapshots WHERE run_uuid = ? ORDER BY mac""",
                (run_uuid,),
            ).fetchall()
        return [
            ClientRecord(
                wlc_name=r[0], mac=r[1], ap_name=r[2], wlan_ssid=r[3],
                ipv4=r[4] or "", ipv6=r[5] or "",
                state=r[6], username=r[7] or "",
            )
            for r in rows
        ]

    def purge_old_runs(self, days: int = _RETENTION_DAYS) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            old = [
                r[0] for r in conn.execute(
                    "SELECT uuid FROM runs WHERE created_at < ?", (cutoff,)
                ).fetchall()
            ]
            if old:
                ph = ",".join("?" * len(old))
                for tbl in ("client_snapshots", "wlan_snapshots",
                            "ap_snapshots", "run_wlcs", "runs"):
                    col = "run_uuid" if tbl != "runs" else "uuid"
                    conn.execute(f"DELETE FROM {tbl} WHERE {col} IN ({ph})", old)
        return len(old)

    def delete_run(self, run_uuid: str) -> None:
        with self._connect() as conn:
            for tbl in ("client_snapshots", "wlan_snapshots", "ap_snapshots", "run_wlcs"):
                conn.execute(f"DELETE FROM {tbl} WHERE run_uuid = ?", (run_uuid,))
            conn.execute("DELETE FROM runs WHERE uuid = ?", (run_uuid,))

    # Legacy
    def get_recent_sessions(self, limit: int = 8) -> List[Session]:
        with self._connect() as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "sessions" not in tables:
                return []
            rows = conn.execute(
                """SELECT uuid, wlc_host, wlc_hostname, session_type, label,
                          created_at, ap_total, ap_joined, ap_not_joined,
                          ap_downloading, ap_other
                   FROM sessions ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            Session(
                uuid=r[0], wlc_host=r[1], wlc_hostname=r[2],
                session_type=r[3], label=r[4],
                created_at=datetime.fromisoformat(r[5]),
                stats=APStats(
                    total=r[6], joined=r[7], not_joined=r[8],
                    downloading=r[9], other=r[10],
                ),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_wlc_results(self, run_uuid: str) -> List[WLCResult]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT wlc_name, wlc_host, wlc_hostname, ok, error,
                          ap_total, ap_joined, ap_not_joined, ap_downloading, ap_other
                   FROM run_wlcs WHERE run_uuid = ? ORDER BY wlc_name""",
                (run_uuid,),
            ).fetchall()
        return [
            WLCResult(
                wlc_name=r[0], wlc_host=r[1], wlc_hostname=r[2],
                records=[], stats=APStats(
                    total=r[5], joined=r[6], not_joined=r[7],
                    downloading=r[8], other=r[9],
                ),
                ok=bool(r[3]), error=r[4] or "",
            )
            for r in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _bootstrap(self) -> None:
        with self._connect() as conn:
            # Migrate old ap_snapshots schema (session_uuid → run_uuid)
            existing_cols = {
                r[1] for r in conn.execute(
                    "PRAGMA table_info(ap_snapshots)"
                ).fetchall()
            }
            if existing_cols and "run_uuid" not in existing_cols:
                conn.execute(
                    "ALTER TABLE ap_snapshots RENAME TO ap_snapshots_v1"
                )

            conn.executescript(_DDL)

            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists
