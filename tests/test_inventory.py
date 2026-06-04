"""Tests for wlccheck/core/inventory.py"""
from __future__ import annotations

from pathlib import Path

import pytest

from wlccheck.core.inventory import (
    DNACEntry,
    InventoryError,
    WLCEntry,
    find_inventory,
    load_dnac_entries,
    load_inventory,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _write_csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# WLCEntry / DNACEntry __str__
# ---------------------------------------------------------------------------

class TestEntryStr:
    def test_wlc_with_datacenter(self):
        e = WLCEntry(name="WLC-1", host="10.0.0.1", datacenter="DC1")
        assert "WLC-1" in str(e)
        assert "DC1" in str(e)

    def test_wlc_without_datacenter(self):
        e = WLCEntry(name="WLC-1", host="10.0.0.1")
        assert str(e) == "WLC-1"

    def test_dnac_with_datacenter(self):
        e = DNACEntry(name="DNAC-1", host="dnac.example.com", datacenter="DC1")
        assert "DNAC-1" in str(e)
        assert "DC1" in str(e)

    def test_dnac_without_datacenter(self):
        e = DNACEntry(name="DNAC-1", host="dnac.example.com")
        assert str(e) == "DNAC-1"


# ---------------------------------------------------------------------------
# find_inventory
# ---------------------------------------------------------------------------

class TestFindInventory:
    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = find_inventory()
        assert result is None

    def test_finds_local_file(self, tmp_path, monkeypatch):
        csv = tmp_path / "wlc_inventory.csv"
        csv.write_text("name,host\nWLC-1,10.0.0.1\n")
        monkeypatch.chdir(tmp_path)
        result = find_inventory()
        assert result is not None
        assert result.resolve() == csv.resolve()


# ---------------------------------------------------------------------------
# load_inventory
# ---------------------------------------------------------------------------

class TestLoadInventory:
    def test_parses_minimal_csv(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "name,host\nWLC-1,10.0.0.1\n")
        entries = load_inventory(csv)
        assert len(entries) == 1
        assert entries[0].name == "WLC-1"
        assert entries[0].host == "10.0.0.1"
        assert entries[0].datacenter == ""

    def test_parses_full_csv_with_datacenter(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host,datacenter\nWLC-1,10.0.0.1,DC1\n")
        entries = load_inventory(csv)
        assert entries[0].datacenter == "DC1"

    def test_skips_dnac_rows(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host,datacenter,type\n"
                         "WLC-1,10.0.0.1,DC1,\n"
                         "DNAC-1,dnac.example.com,DC1,dnac\n")
        entries = load_inventory(csv)
        assert len(entries) == 1
        assert entries[0].name == "WLC-1"

    def test_raises_when_missing_name_column(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "host\n10.0.0.1\n")
        with pytest.raises(InventoryError, match="name"):
            load_inventory(csv)

    def test_raises_when_missing_host_column(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "name\nWLC-1\n")
        with pytest.raises(InventoryError, match="host"):
            load_inventory(csv)

    def test_raises_when_no_wlc_entries(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host,type\nDNAC-1,dnac.example.com,dnac\n")
        with pytest.raises(InventoryError):
            load_inventory(csv)

    def test_raises_on_empty_csv(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "")
        with pytest.raises(InventoryError):
            load_inventory(csv)

    def test_raises_when_row_missing_name(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "name,host\n,10.0.0.1\n")
        with pytest.raises(InventoryError, match="name"):
            load_inventory(csv)

    def test_raises_when_row_missing_host(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "name,host\nWLC-1,\n")
        with pytest.raises(InventoryError, match="host"):
            load_inventory(csv)

    def test_raises_on_file_not_found(self, tmp_path):
        missing = tmp_path / "nonexistent.csv"
        with pytest.raises(InventoryError, match="Cannot read"):
            load_inventory(missing)

    def test_multiple_wlcs(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host\nWLC-1,10.0.0.1\nWLC-2,10.0.0.2\n")
        entries = load_inventory(csv)
        assert len(entries) == 2

    def test_strips_whitespace_from_values(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv", "name,host\n  WLC-1  ,  10.0.0.1  \n")
        entries = load_inventory(csv)
        assert entries[0].name == "WLC-1"
        assert entries[0].host == "10.0.0.1"


# ---------------------------------------------------------------------------
# load_dnac_entries
# ---------------------------------------------------------------------------

class TestLoadDnacEntries:
    def test_returns_dnac_entries(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host,datacenter,type\n"
                         "WLC-1,10.0.0.1,DC1,\n"
                         "DNAC-PROD,dnac.example.com,DC1,dnac\n")
        entries = load_dnac_entries(csv)
        assert len(entries) == 1
        assert entries[0].name == "DNAC-PROD"
        assert entries[0].host == "dnac.example.com"

    def test_returns_empty_when_no_dnac(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host\nWLC-1,10.0.0.1\n")
        entries = load_dnac_entries(csv)
        assert entries == []

    def test_multiple_dnac_entries(self, tmp_path):
        csv = _write_csv(tmp_path / "inv.csv",
                         "name,host,type\n"
                         "DNAC-1,dnac1.example.com,dnac\n"
                         "DNAC-2,dnac2.example.com,dnac\n")
        entries = load_dnac_entries(csv)
        assert len(entries) == 2
