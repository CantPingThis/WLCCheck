from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


_SEARCH_PATHS = [
    Path("wlc_inventory.csv"),
    Path.home() / ".wlccheck" / "wlc_inventory.csv",
]

_REQUIRED_COLS = {"name", "host"}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class WLCEntry:
    name: str
    host: str
    datacenter: str = ""

    def __str__(self) -> str:
        if self.datacenter:
            return f"{self.name}  [{self.datacenter}]"
        return self.name


@dataclass
class DNACEntry:
    name: str
    host: str
    datacenter: str = ""

    def __str__(self) -> str:
        if self.datacenter:
            return f"{self.name}  [{self.datacenter}]"
        return self.name


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InventoryError(Exception):
    """Raised for CSV parse or validation errors."""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def find_inventory() -> Optional[Path]:
    """Return the first inventory CSV found in the standard search paths."""
    for p in _SEARCH_PATHS:
        if p.exists():
            return p
    return None


def load_inventory(path: Path) -> List[WLCEntry]:
    """Parse a WLC inventory CSV and return WLC entries only.

    Rows with ``type=dnac`` are silently skipped so the file can contain both
    WLC and DNAC entries without breaking existing callers.

    Raises InventoryError on missing columns or no WLC entries.
    """
    wlcs, _ = _parse_all(path)
    if not wlcs:
        raise InventoryError(f"Inventory CSV has no WLC entries: {path}")
    return wlcs


def load_dnac_entries(path: Path) -> List[DNACEntry]:
    """Return DNAC entries (rows with ``type=dnac``) from the inventory CSV."""
    _, dnacs = _parse_all(path)
    return dnacs


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _parse_all(path: Path) -> Tuple[List[WLCEntry], List[DNACEntry]]:
    wlcs:  List[WLCEntry]  = []
    dnacs: List[DNACEntry] = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise InventoryError(f"CSV is empty: {path}")

            cols = {c.strip().lower() for c in reader.fieldnames}
            missing = _REQUIRED_COLS - cols
            if missing:
                raise InventoryError(
                    f"CSV missing required column(s): {', '.join(sorted(missing))}"
                )

            for i, row in enumerate(reader, start=2):
                name = row.get("name", "").strip()
                host = row.get("host", "").strip()
                dc   = row.get("datacenter", "").strip()
                typ  = row.get("type", "").strip().lower()

                if not name or not host:
                    raise InventoryError(
                        f"Row {i}: 'name' and 'host' must not be empty."
                    )

                if typ == "dnac":
                    dnacs.append(DNACEntry(name=name, host=host, datacenter=dc))
                else:
                    wlcs.append(WLCEntry(name=name, host=host, datacenter=dc))

    except InventoryError:
        raise
    except OSError as exc:
        raise InventoryError(f"Cannot read inventory file: {exc}") from exc

    return wlcs, dnacs
