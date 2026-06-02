#!/usr/bin/env python3
"""
Quick diagnostic script for the DNAC switch/port lookup.

Usage:
    python debug_dnac.py <ap_ip_or_hostname>

Required env vars:
    DNAC_HOST   — DNAC/Catalyst Center hostname or IP
    DNAC_USER   — username  (falls back to WLC_USER)
    DNAC_PASS   — password  (falls back to WLC_PASS)

Optional:
    DNAC_VERIFY_SSL=true  — enable SSL verification (default: disabled)
"""

import os
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------

HOST   = os.environ.get("DNAC_HOST") or os.environ.get("CATALYST_HOST", "")
USER   = os.environ.get("DNAC_USER") or os.environ.get("WLC_USER", "")
PASS   = os.environ.get("DNAC_PASS") or os.environ.get("WLC_PASS", "")
VERIFY = os.environ.get("DNAC_VERIFY_SSL", "").lower() == "true"

if not HOST:
    print("ERROR: DNAC_HOST (or CATALYST_HOST) env var is not set.")
    sys.exit(1)
if not USER or not PASS:
    print("ERROR: set DNAC_USER/DNAC_PASS (or WLC_USER/WLC_PASS) env vars.")
    sys.exit(1)

AP_LOOKUP = sys.argv[1] if len(sys.argv) > 1 else ""
if not AP_LOOKUP:
    print("Usage: python debug_dnac.py <ap_ip_or_hostname>")
    sys.exit(1)

BASE = f"https://{HOST}"


def ok(msg):   print(f"  \033[32m✓\033[0m  {msg}")
def warn(msg): print(f"  \033[33m⚠\033[0m  {msg}")
def err(msg):  print(f"  \033[31m✕\033[0m  {msg}")
def step(msg): print(f"\n\033[1m{msg}\033[0m")

# ---------------------------------------------------------------------------
# Step 1 — Auth
# ---------------------------------------------------------------------------

step("Step 1 — Authentication")
t0 = time.monotonic()
try:
    r = requests.post(
        f"{BASE}/dna/system/api/v1/auth/token",
        auth=(USER, PASS), verify=VERIFY, timeout=10,
    )
    r.raise_for_status()
    token = r.json()["Token"]
    ok(f"Token obtained in {time.monotonic()-t0:.1f}s  ({token[:20]}…)")
except Exception as exc:
    err(f"Auth failed: {exc}")
    sys.exit(1)

HDR = {"X-Auth-Token": token}

# ---------------------------------------------------------------------------
# Step 2 — Device lookup
# ---------------------------------------------------------------------------

step(f"Step 2 — Device lookup for '{AP_LOOKUP}'")
device = None

try:
    r = requests.get(
        f"{BASE}/dna/intent/api/v1/network-device/ip-address/{AP_LOOKUP}",
        headers=HDR, verify=VERIFY, timeout=10,
    )
    if r.status_code == 404:
        warn("Not found by IP — trying by hostname…")
    else:
        r.raise_for_status()
        device = r.json().get("response")
        ok(f"Found by IP: id={device['id']}  hostname={device.get('hostname')}  "
           f"family={device.get('family')}  platform={device.get('platformId')}")
except Exception as exc:
    warn(f"IP lookup error: {exc}")

if device is None:
    try:
        r = requests.get(
            f"{BASE}/dna/intent/api/v1/network-device",
            headers=HDR, params={"hostname": AP_LOOKUP},
            verify=VERIFY, timeout=10,
        )
        r.raise_for_status()
        devices = r.json().get("response", [])
        if devices:
            device = devices[0]
            ok(f"Found by hostname: id={device['id']}  ip={device.get('managementIpAddress')}  "
               f"family={device.get('family')}")
        else:
            warn("Not found by hostname either.")
    except Exception as exc:
        warn(f"Hostname lookup error: {exc}")

if device is None:
    err("AP not found in DNAC inventory as a network device.")
    print("  Tip: check the AP exists in DNAC > Provision > Inventory.")
    sys.exit(1)

device_id  = device["id"]
device_ip  = device.get("managementIpAddress", "")
device_host = device.get("hostname", "")

# ---------------------------------------------------------------------------
# Step 3 — Physical topology
# ---------------------------------------------------------------------------

step("Step 3 — Fetching physical topology (may take a few seconds…)")
t0 = time.monotonic()
try:
    r = requests.get(
        f"{BASE}/dna/intent/api/v1/topology/physical-topology",
        headers=HDR, verify=VERIFY, timeout=30,
    )
    r.raise_for_status()
    topo  = r.json().get("response", {})
    nodes = topo.get("nodes", [])
    links = topo.get("links", [])
    ok(f"Topology fetched in {time.monotonic()-t0:.1f}s — "
       f"{len(nodes)} nodes, {len(links)} links")
except Exception as exc:
    err(f"Topology fetch failed: {exc}")
    sys.exit(1)

node_by_id = {n["id"]: n for n in nodes}
node_map   = {n["id"]: n.get("label") or n.get("ip") or "?" for n in nodes}

# ---------------------------------------------------------------------------
# Step 4 — Resolve AP node ID in topology
# ---------------------------------------------------------------------------

step("Step 4 — Resolving AP node in topology")

effective_id = device_id
ap_node      = node_by_id.get(device_id)

if ap_node:
    ok(f"device_id matches topology node: label={ap_node.get('label')}  ip={ap_node.get('ip')}")
else:
    warn(f"device_id '{device_id}' not found in topology node list — trying fallback…")

    # Fallback 1: match by management IP
    for n in nodes:
        if device_ip and n.get("ip") == device_ip:
            effective_id = n["id"]
            ap_node      = n
            ok(f"Fallback matched by IP ({device_ip}): "
               f"topology node id={n['id']}  label={n.get('label')}")
            break

    # Fallback 2: match by hostname/label
    if ap_node is None:
        for n in nodes:
            node_label = n.get("label", "")
            if device_host and (node_label == device_host or
                                node_label.lower() == device_host.lower()):
                effective_id = n["id"]
                ap_node      = n
                ok(f"Fallback matched by hostname ({device_host}): "
                   f"topology node id={n['id']}  ip={n.get('ip')}")
                break

    if ap_node is None:
        err("AP node not found in topology by id, IP, or hostname.")
        print(f"\n  device_id (network-device API) : {device_id}")
        print(f"  managementIpAddress            : {device_ip}")
        print(f"  hostname                       : {device_host}")
        print("\n  Sample topology nodes (first 5):")
        for n in nodes[:5]:
            print(f"    id={n['id'][:36]}  label={n.get('label')}  ip={n.get('ip')}")
        print("\n  → AP exists in inventory but DNAC has no topology node for it.")
        print("    Physical connectivity cannot be retrieved via this API.")
        sys.exit(1)

    if effective_id != device_id:
        warn(f"Using topology node id={effective_id} (differs from network-device id)")

# ---------------------------------------------------------------------------
# Step 5 — Find matching links
# ---------------------------------------------------------------------------

step(f"Step 5 — Searching links for node id={effective_id}")

matching = [
    lnk for lnk in links
    if lnk.get("source") == effective_id or lnk.get("target") == effective_id
]

if not matching:
    err("No link found in topology for this node.")
    print("\n  Node details:")
    for k, v in sorted(ap_node.items()):
        print(f"    {k:35s} = {v!r}")
    print("\n  → AP is in the topology node list but has no physical link.")
    print("    The switch connection may not be captured via CDP/LLDP in DNAC.")
    sys.exit(1)

ok(f"Found {len(matching)} link(s).")

# ---------------------------------------------------------------------------
# Step 6 — Dump ALL fields of each matching link
# ---------------------------------------------------------------------------

step("Step 6 — Raw link data (all fields)")
for i, lnk in enumerate(matching, 1):
    src_id  = lnk.get("source", "")
    tgt_id  = lnk.get("target", "")
    src_lbl = node_map.get(src_id, src_id)
    tgt_lbl = node_map.get(tgt_id, tgt_id)
    role    = "AP=source → switch=target" if src_id == effective_id else "switch=source → AP=target"

    print(f"\n  --- Link {i}/{len(matching)}  ({role}) ---")
    print(f"  source : {src_lbl}  ({src_id})")
    print(f"  target : {tgt_lbl}  ({tgt_id})")
    print()
    print("  All link fields:")
    for k, v in sorted(lnk.items()):
        print(f"    {k:40s} = {v!r}")

# ---------------------------------------------------------------------------
# Step 7 — Summary
# ---------------------------------------------------------------------------

step("Step 7 — Summary (what the app reads)")
for lnk in matching:
    src_id = lnk.get("source", "")
    tgt_id = lnk.get("target", "")
    if src_id == effective_id:
        switch_name = node_map.get(tgt_id, "—")
        switch_port = lnk.get("endPortName") or "—"
        ap_port     = lnk.get("startPortName") or "—"
    else:
        switch_name = node_map.get(src_id, "—")
        switch_port = lnk.get("startPortName") or "—"
        ap_port     = lnk.get("endPortName") or "—"

    print(f"\n  Switch name   : {switch_name}")
    print(f"  Switch port   : {switch_port}")
    print(f"  AP uplink port: {ap_port}")

    if switch_port == "—":
        warn("switch_port is empty — inspecting link fields for port candidates:")
        candidates = {k: v for k, v in lnk.items()
                      if v and any(x in k.lower() for x in ("port", "interface", "intf"))}
        for k, v in candidates.items():
            print(f"    {k:40s} = {v!r}")
