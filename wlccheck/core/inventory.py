from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


_SEARCH_PATHS = [
    Path("wlc_inventory.csv"),
    Path.home() / ".wlccheck" / "wlc_inventory.csv",
]

_REQUIRED_COLS = {"name", "host"}


# ---------------------------------------------------------------------------
# Model
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
    """Parse a WLC inventory CSV and return a list of WLCEntry objects.

    Raises InventoryError on missing columns or empty file.
    """
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

            entries: List[WLCEntry] = []
            for i, row in enumerate(reader, start=2):  # row 1 = header
                name = row.get("name", "").strip()
                host = row.get("host", "").strip()
                dc   = row.get("datacenter", "").strip()

                if not name or not host:
                    raise InventoryError(
                        f"Row {i}: 'name' and 'host' must not be empty."
                    )
                entries.append(WLCEntry(name=name, host=host, datacenter=dc))

    except InventoryError:
        raise
    except OSError as exc:
        raise InventoryError(f"Cannot read inventory file: {exc}") from exc

    if not entries:
        raise InventoryError(f"Inventory CSV has no WLC entries: {path}")

    return entries
