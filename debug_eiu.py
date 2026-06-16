#!/usr/bin/env python3
"""
Efficient Image Upgrade (EIU) debug / trigger script for Cisco 9800 WLC.

What this script does
---------------------
  Step 1  — RESTCONF auth + connectivity check
  Step 2  — WLC software version (EIU requires IOS-XE 17.3+)
  Step 3  — Explore ap-img-dwnld-stat-data (raw dump to confirm YANG structure)
  Step 4  — Read all APs and extract site-tags with AP counts + current image versions
  Step 5  — Display site-tag summary
  Step 6  — Interactive selection of site-tags for pre-download
  Step 7  — Dry-run: show SSH commands that WOULD be sent
  Step 8  — Trigger pre-download via SSH (requires --execute flag)

Usage
-----
    # Explore only (default — no change to the WLC)
    python debug_eiu.py

    # Also trigger the pre-download on selected site-tags via SSH
    python debug_eiu.py --execute

Required env vars
-----------------
    WLC_HOST   — WLC hostname or IP
    WLC_USER   — RESTCONF user (must have netconf / restconf privilege)
    WLC_PASS   — password

Optional
--------
    WLC_VERIFY_SSL=true   — enable TLS certificate verification (default: disabled)
"""

import json
import os
import sys
import time
from collections import defaultdict

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST    = os.environ.get("WLC_HOST", "")
USER    = os.environ.get("WLC_USER", "")
PASS    = os.environ.get("WLC_PASS", "")
VERIFY  = os.environ.get("WLC_VERIFY_SSL", "").lower() == "true"
EXECUTE = "--execute" in sys.argv

if not HOST:
    print("ERROR: WLC_HOST env var is not set.")
    sys.exit(1)
if not USER or not PASS:
    print("ERROR: set WLC_USER and WLC_PASS env vars.")
    sys.exit(1)

BASE_DATA = f"https://{HOST}/restconf/data"

_session = requests.Session()
_session.auth = (USER, PASS)
_session.verify = VERIFY
_session.headers.update({
    "Accept":       "application/yang-data+json",
    "Content-Type": "application/yang-data+json",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ok(msg):   print(f"  \033[32m✓\033[0m  {msg}")
def warn(msg): print(f"  \033[33m⚠\033[0m  {msg}")
def err(msg):  print(f"  \033[31m✕\033[0m  {msg}")
def step(msg): print(f"\n\033[1m{msg}\033[0m")
def info(msg): print(f"      {msg}")


def get(path: str, timeout: int = 30) -> requests.Response | None:
    url = f"{BASE_DATA}/{path}"
    try:
        return _session.get(url, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        err(f"Connection error: {exc}")
        return None
    except requests.exceptions.Timeout:
        err(f"Timeout after {timeout}s on GET {path}")
        return None



def safe_list(body: dict, *keys: str) -> list:
    for k in keys:
        if k in body:
            v = body[k]
            return v if isinstance(v, list) else []
    return []


# ---------------------------------------------------------------------------
# Step 1 — Auth + connectivity
# ---------------------------------------------------------------------------

step("Step 1 — RESTCONF auth + connectivity check")

t0 = time.monotonic()
r = get("Cisco-IOS-XE-native:native/hostname", timeout=10)
if r is None:
    err("Cannot reach WLC — check WLC_HOST and network connectivity.")
    sys.exit(1)

if r.status_code == 401:
    err("Authentication rejected (HTTP 401) — check WLC_USER / WLC_PASS.")
    info("Ensure the account has 'privilege 15' and RESTCONF is enabled:")
    info("  restconf")
    info("  aaa authorization exec default local")
    sys.exit(1)
elif r.status_code == 403:
    err("Authorisation denied (HTTP 403) — user lacks RESTCONF privilege.")
    sys.exit(1)
elif r.status_code == 200:
    try:
        hostname = r.json().get("Cisco-IOS-XE-native:hostname", HOST)
    except ValueError:
        hostname = HOST
    ok(f"Connected to '{hostname}' in {time.monotonic()-t0:.1f}s")
else:
    warn(f"Unexpected HTTP {r.status_code} on hostname check — continuing.")
    hostname = HOST


# ---------------------------------------------------------------------------
# Step 2 — WLC software version  (EIU requires IOS-XE 17.3+)
# ---------------------------------------------------------------------------

step("Step 2 — WLC software version (EIU requires 17.3+)")

wlc_version = ""

r = get("Cisco-IOS-XE-install-oper:install-oper-data", timeout=15)
if r and r.status_code == 200 and r.content.strip():
    try:
        body = r.json()
        info(f"install-oper-data top keys: {list(body.keys())}")
        for k, v in body.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if "version" in k2.lower() and isinstance(v2, str):
                        info(f"  {k}.{k2} = {v2!r}")
                        if not wlc_version:
                            wlc_version = v2
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                info(f"  First entry of {k!r}:")
                for k2, v2 in list(v[0].items())[:8]:
                    info(f"    {k2} = {v2!r}")
                    if "version" in k2.lower() and isinstance(v2, str) and not wlc_version:
                        wlc_version = v2
    except ValueError:
        warn("Could not parse install-oper-data as JSON.")
elif r:
    info(f"install-oper-data: HTTP {r.status_code}")

if not wlc_version:
    r2 = get("Cisco-IOS-XE-native:native/version", timeout=10)
    if r2 and r2.status_code == 200 and r2.content.strip():
        try:
            for v in r2.json().values():
                if isinstance(v, str):
                    wlc_version = v
                    break
        except ValueError:
            pass

if wlc_version:
    ok(f"IOS-XE version: {wlc_version}")
    try:
        parts = wlc_version.split(".")
        major, minor = int(parts[0]), int(parts[1])
        if major > 17 or (major == 17 and minor >= 3):
            ok("Version meets EIU minimum requirement (17.3+)")
        else:
            warn(f"Version {wlc_version} may not fully support EIU — requires 17.3+")
    except (ValueError, IndexError):
        warn(f"Could not parse version string {wlc_version!r} — proceeding.")
else:
    warn("Could not determine WLC version — proceeding.")


# ---------------------------------------------------------------------------
# Step 3 — Explore ap-img-dwnld-stat-data (raw structure dump)
# ---------------------------------------------------------------------------

step("Step 3 — Explore ap-img-dwnld-stat-data (raw YANG structure)")

AP_IMG_PREDOWNLOAD_STATS_PATH = (
    "Cisco-IOS-XE-wireless-ap-global-oper:"
    "ap-global-oper-data/ap-img-predownload-stats"
)
AP_GLOBAL_OPER_PATH = (
    "Cisco-IOS-XE-wireless-ap-global-oper:ap-global-oper-data"
)

img_stat_data: list = []

r = get(AP_IMG_PREDOWNLOAD_STATS_PATH, timeout=30)
if r is None:
    warn("Request failed for ap-img-predownload-stats.")
elif r.status_code == 404:
    warn("ap-img-predownload-stats: HTTP 404 — dumping full ap-global-oper-data to discover sub-nodes…")
    r2 = get(AP_GLOBAL_OPER_PATH, timeout=30)
    if r2 and r2.status_code == 200 and r2.content.strip():
        try:
            body2 = r2.json()
            container = (
                body2.get("Cisco-IOS-XE-wireless-ap-global-oper:ap-global-oper-data")
                or body2.get("ap-global-oper-data")
                or body2
            )
            if isinstance(container, dict):
                info(f"ap-global-oper-data sub-nodes: {list(container.keys())}")
                for k, v in container.items():
                    if isinstance(v, list):
                        info(f"  {k!r}: list of {len(v)}")
                        if v and isinstance(v[0], dict):
                            info(f"    first entry keys: {list(v[0].keys())}")
                    elif isinstance(v, dict):
                        info(f"  {k!r}: dict keys={list(v.keys())}")
                    else:
                        info(f"  {k!r}: {v!r}")
            else:
                print(json.dumps(body2, indent=2))
        except ValueError:
            info(f"ap-global-oper-data raw (first 1000): {r2.text[:1000]}")
    elif r2:
        info(f"ap-global-oper-data: HTTP {r2.status_code}")
elif r.status_code == 204:
    ok("HTTP 204 — path exists, no pre-download currently in progress.")
    img_stat_data = []
elif r.status_code == 200 and r.content.strip():
    try:
        body = r.json()
        ok("ap-img-predownload-stats found — raw dump:")
        print(json.dumps(body, indent=2))
    except ValueError:
        warn("Could not parse as JSON.")
        info(f"Raw (first 500): {r.text[:500]}")
else:
    warn(f"ap-img-predownload-stats: HTTP {r.status_code}")
    info(r.text[:400] if r.text else "(empty)")


# ---------------------------------------------------------------------------
# Step 4 — Read all APs + extract site-tags
# ---------------------------------------------------------------------------

step("Step 4 — Reading all APs from capwap-data + extracting site-tags")

CAPWAP_PATH = (
    "Cisco-IOS-XE-wireless-access-point-oper:"
    "access-point-oper-data/capwap-data"
)

r = get(CAPWAP_PATH, timeout=60)
if r is None or r.status_code not in (200, 204):
    err(f"Could not fetch capwap-data: HTTP {r.status_code if r else 'N/A'}")
    sys.exit(1)

all_aps: list = []
if r.status_code == 200 and r.content.strip():
    try:
        body = r.json()
        all_aps = safe_list(
            body,
            "Cisco-IOS-XE-wireless-access-point-oper:capwap-data",
            "capwap-data",
        )
    except ValueError:
        err("Could not parse capwap-data response.")
        sys.exit(1)

if not all_aps:
    warn("No APs returned from capwap-data — nothing to pre-download.")
    sys.exit(0)

ok(f"{len(all_aps)} APs retrieved.")

# Build map: site_tag → list of AP info dicts
site_tag_aps: dict[str, list] = defaultdict(list)
version_unknown = 0

for ap in all_aps:
    tag_info = ap.get("tag-info") or {}
    resolved = tag_info.get("resolved-tag-info") or {}
    site_tag = (
        tag_info.get("site-tag", {}).get("site-tag-name")
        or resolved.get("resolved-site-tag")
        or "default-site-tag"
    )

    # AP running version: device-detail.wtp-version.sw-version (confirmed IOS-XE 17.15)
    ap_version = (
        ap.get("device-detail", {})
          .get("wtp-version", {})
          .get("sw-version", "")
    )
    # Backup version (same struct, different key)
    backup_version = ""
    bsv = ap.get("device-detail", {}).get("wtp-version", {}).get("backup-sw-version", {})
    if isinstance(bsv, dict) and bsv.get("version"):
        backup_version = (
            f"{bsv['version']}.{bsv['release']}.{bsv.get('maint', 0)}.{bsv.get('build', 0)}"
        )

    # Per-AP pre-download progress (present in capwap-data during active EIU)
    img_pct     = ap.get("image-size-percentage", 0)
    img_eta     = ap.get("image-size-eta", 0)
    wlc_img_pct = ap.get("wlc-image-size-percentage", 0)

    if not ap_version:
        version_unknown += 1

    site_tag_aps[site_tag].append({
        "name":           ap.get("name", ""),
        "mac":            ap.get("wtp-mac", ""),
        "version":        ap_version,
        "backup_version": backup_version,
        "img_pct":        img_pct,
        "wlc_img_pct":   wlc_img_pct,
        "img_eta":        img_eta,
        "site_tag":       site_tag,
    })

if version_unknown:
    warn(
        f"{version_unknown}/{len(all_aps)} APs have no image version in capwap-data "
        "(may be in a different leaf on this IOS-XE version)."
    )
    if all_aps:
        info("Full first AP entry (capwap-data[0]) — look for image/version fields:")
        print(json.dumps(all_aps[0], indent=2))


# ---------------------------------------------------------------------------
# Step 5 — Site-tag summary
# ---------------------------------------------------------------------------

step("Step 5 — Site-tag summary")

sorted_tags = sorted(site_tag_aps.keys())
info(f"  {'SITE-TAG':<40}  {'APs':>5}  CURRENT VERSION    BACKUP VERSION")
info("  " + "-" * 85)
for tag in sorted_tags:
    aps = site_tag_aps[tag]
    versions        = sorted({a["version"] for a in aps if a["version"]})
    backup_versions = sorted({a["backup_version"] for a in aps if a["backup_version"]})
    ver_str    = ", ".join(versions) if versions else "(unknown)"
    backup_str = ", ".join(backup_versions) if backup_versions else ""
    info(f"  {tag:<40}  {len(aps):>5}  {ver_str:<18} {backup_str}")


# ---------------------------------------------------------------------------
# Step 6 — Interactive selection of site-tags
# ---------------------------------------------------------------------------

step("Step 6 — Select site-tags for EIU pre-download")

print()
for i, tag in enumerate(sorted_tags, 1):
    count = len(site_tag_aps[tag])
    print(f"  [{i:2d}] {tag}  ({count} APs)")

print()
print("  Enter site-tag numbers (comma-separated) or 'all':")
print("  Example:  1,3   or   all")
print()

try:
    raw_input = input("  Selection: ").strip()
except (EOFError, KeyboardInterrupt):
    print()
    warn("Interrupted — exiting.")
    sys.exit(0)

if not raw_input:
    warn("No selection — exiting.")
    sys.exit(0)

if raw_input.lower() == "all":
    selected_tags = sorted_tags[:]
else:
    selected_tags = []
    for part in raw_input.split(","):
        part = part.strip()
        if not part.isdigit():
            err(f"Invalid selection {part!r} — must be a number.")
            sys.exit(1)
        idx = int(part) - 1
        if not 0 <= idx < len(sorted_tags):
            err(f"Index {part} out of range (1–{len(sorted_tags)}).")
            sys.exit(1)
        selected_tags.append(sorted_tags[idx])

if not selected_tags:
    warn("No valid site-tags selected — exiting.")
    sys.exit(0)

ok(f"Selected {len(selected_tags)} site-tag(s):")
for tag in selected_tags:
    info(f"  • {tag}  ({len(site_tag_aps[tag])} APs)")


# ---------------------------------------------------------------------------
# Step 7 — Dry-run: SSH commands that will be sent
# ---------------------------------------------------------------------------

step("Step 7 — Dry-run: SSH commands that will be sent")

info(f"SSH target : {HOST}:22  (user: {USER})")
info("Platform   : cisco_iosxe  (scrapli)")
print()
for tag in selected_tags:
    ap_count = len(site_tag_aps[tag])
    info(f"  ap image predownload site-tag {tag}   ({ap_count} APs)")
print()

if not EXECUTE:
    warn("Dry-run mode — no SSH connection opened.")
    warn("Re-run with --execute to trigger pre-download.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Step 8 — Trigger EIU pre-download via SSH
# ---------------------------------------------------------------------------

step("Step 8 — Triggering EIU pre-download via SSH")

print()
warn("*** WRITE OPERATION — this will trigger AP image pre-download on the WLC ***")
warn(f"    WLC       : {hostname} ({HOST})")
warn(f"    Site-tags : {', '.join(selected_tags)}")
warn(f"    Total APs : {sum(len(site_tag_aps[t]) for t in selected_tags)}")
print()
print("  Type 'yes' to confirm, anything else to abort:")

try:
    confirm = input("  Confirm: ").strip().lower()
except (EOFError, KeyboardInterrupt):
    print()
    warn("Interrupted — aborted.")
    sys.exit(0)

if confirm != "yes":
    warn("Aborted — no changes made.")
    sys.exit(0)

try:
    from scrapli import Scrapli
except ImportError:
    err("scrapli not installed — run:  uv add 'scrapli[paramiko]'")
    sys.exit(1)

ENABLE_PASS = os.environ.get("WLC_ENABLE_PASS", PASS)

info(f"Opening SSH connection to {HOST}…")
try:
    ssh_conn = Scrapli(
        host=HOST,
        auth_username=USER,
        auth_password=PASS,
        auth_secondary=ENABLE_PASS,
        auth_strict_key=False,
        platform="cisco_iosxe",
        transport="paramiko",
        timeout_socket=10,
        timeout_transport=30,
        timeout_ops=60,
    )
    ssh_conn.open()
except Exception as ssh_exc:
    err(f"SSH connection failed: {ssh_exc}")
    sys.exit(1)

ok(f"SSH connected to {hostname} ({HOST})")

# Verify privilege level — ap image commands require privilege 15
priv_resp = ssh_conn.send_command("show privilege")
priv_out  = priv_resp.result.strip()
info(f"Privilege check: {priv_out!r}")
if "15" not in priv_out:
    warn("Not in privilege 15 — ap image commands may fail. Set WLC_ENABLE_PASS if needed.")

# Discover accepted syntax for ap image predownload on this firmware
info("Discovering command syntax: ap image predownload ?")
try:
    help_resp = ssh_conn.send_command("ap image predownload ?", timeout_ops=10)
    help_out  = help_resp.result.strip()
    if help_out:
        info("  Available options:")
        for line in help_out.splitlines():
            info(f"    {line}")
except Exception:
    pass

# Command candidates in priority order (first success wins)
# IOS-XE 17.x: "ap image predownload site-tag <name> start"
_CMD_CANDIDATES = [
    "ap image predownload site-tag {tag} start",
    "ap image predownload site-tag-name {tag} start",
    "ap image predownload site-tag {tag}",
    "wireless ap image predownload site-tag {tag} start",
]

def _is_cisco_error(output: str) -> bool:
    return output.startswith("%") or "Invalid input" in output or "Incomplete command" in output

# Probe with first selected tag to find the working syntax
probe_tag  = selected_tags[0]
working_cmd: str | None = None

info(f"Probing command syntax with tag '{probe_tag}'…")
for template in _CMD_CANDIDATES:
    cmd = template.format(tag=probe_tag)
    info(f"  Trying: {cmd!r}")
    try:
        resp = ssh_conn.send_command(cmd, timeout_ops=30)
        out  = resp.result.strip()
        if out:
            info(f"    Output: {out}")
        if _is_cisco_error(out):
            info("    → error, trying next variant")
        else:
            ok("    → accepted (no error output)")
            working_cmd = template
            break
    except Exception as probe_exc:
        info(f"    → exception: {probe_exc}")

if working_cmd is None:
    err("No working command syntax found — cannot trigger pre-download via SSH.")
    err("Check WLC privilege level and firmware version.")
    ssh_conn.close()
    sys.exit(1)

info(f"Working syntax: {working_cmd!r}")
print()

# Trigger for all selected site-tags
try:
    for tag in selected_tags:
        if tag == probe_tag and working_cmd != "ap image predownload":
            ok(f"  Pre-download already triggered on '{probe_tag}' during probe")
            continue
        ap_count = len(site_tag_aps[tag])
        info(f"Triggering pre-download on '{tag}' ({ap_count} APs)…")
        t0  = time.monotonic()
        cmd = working_cmd.format(tag=tag)
        try:
            response = ssh_conn.send_command(cmd, timeout_ops=60)
            elapsed  = time.monotonic() - t0
            result   = response.result.strip()
            if result:
                info(f"  WLC output: {result}")
            if _is_cisco_error(result):
                err(f"  Pre-download FAILED on '{tag}': {result}")
            else:
                ok(f"  Pre-download triggered on '{tag}' in {elapsed:.1f}s")
        except Exception as cmd_exc:
            err(f"  Command failed for '{tag}': {cmd_exc}")
finally:
    ssh_conn.close()
    info("SSH connection closed.")


# ---------------------------------------------------------------------------
# Step 9 — Poll current download progress
# ---------------------------------------------------------------------------

step("Step 9 — Checking pre-download progress (ap-img-predownload-stats)")

r = get(AP_IMG_PREDOWNLOAD_STATS_PATH, timeout=30)
if r and r.status_code == 200 and r.content.strip():
    try:
        ok("ap-img-predownload-stats (post-trigger):")
        print(json.dumps(r.json(), indent=2))
    except ValueError:
        info(f"Raw: {r.text[:500]}")
    img_stat_data = []
elif r and r.status_code == 204:
    img_stat_data = []
else:
    img_stat_data = []

info("Re-run the script (without --execute) to poll progress.")

print()
ok("Done.")
