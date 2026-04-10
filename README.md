# WLCCheck

A terminal UI for pre/post change verification on **Cisco 9800 Wireless LAN Controllers** using RESTCONF.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Textual](https://img.shields.io/badge/TUI-Textual-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Features

- **Snapshot** — collect AP state, tags, WLANs, and optionally clients from one or more WLCs
- **Post-check** — compare a live collection against any stored baseline and view a structured diff
- **Diff view** — severity-filtered change table (critical / warning / info) with full AP, WLAN, and client tabs
- **Live AP Monitor** — periodic polling (configurable interval ≥ 60 s) with real-time state tracking:
  - Per-WLC selector bar with keyboard shortcuts (`0`–`9`)
  - Stats bar: baseline / current / delta / poll # / next-poll countdown
  - AP Status tab: baseline · previous poll · current state (missing APs shown as Not Joined)
  - Event Log tab: timestamped state-change history
  - History tab: per-poll aggregate counts per WLC
- **Multi-WLC** — parallel collection and polling via an inventory CSV
- **Command palette** — `Ctrl+P` for quick command access

---

## Requirements

- Python 3.10+
- Cisco 9800 WLC running IOS-XE 17.x with RESTCONF enabled

---

## Installation

```bash
git clone https://github.com/<you>/WLCCheck.git
cd WLCCheck
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Or without installing:

```bash
pip install -r requirements.txt
python -m wlccheck
```

---

## WLC Prerequisites

RESTCONF must be enabled on each controller:

```
ip http secure-server
restconf
```

The user account needs the `netconf-yang` privilege or equivalent read access to the wireless YANG models.

---

## Inventory file

For multi-WLC environments, create `wlc_inventory.csv` in the working directory (see `wlc_inventory.example.csv`):

```csv
name,host,datacenter
WLC-CORE-1,10.0.0.1,DC1
WLC-CORE-2,10.0.0.2,DC1
```

Without an inventory file the tool prompts for a single host on startup.

---

## Credentials

Credentials can be provided via environment variables to skip the login prompt:

```bash
export WLC_USER=admin
export WLC_PASS=secret
python -m wlccheck
```

---

## Key bindings

| Key | Action |
|-----|--------|
| `c` | New snapshot |
| `p` | Post-check (compare against baseline) |
| `f` | Cycle severity filter (all / warning+ / critical) |
| `o` | Toggle all records / changed only |
| `Ctrl+P` | Command palette |
| `q` | Quit |

**In Live AP Monitor:**

| Key | Action |
|-----|--------|
| `Space` | Pause / resume polling |
| `r` | Force poll immediately |
| `0` | Show all WLCs |
| `1`–`9` | Focus a specific WLC |
| `o` | Toggle all APs / changed only |
| `Esc` | Exit live monitor |

---

## Debug / development

`debug_restconf.py` is a standalone script that dumps raw RESTCONF responses — useful when adding support for a new IOS-XE version:

```bash
python debug_restconf.py <host> <username> <password>
```

---

## License

MIT
