#!/usr/bin/env python3
"""
Quick diagnostic script — find switch + port for an AP via Cisco ISE session data.

How it works
------------
When an AP plugs into a switch port configured for 802.1X or MAB authentication,
the switch sends a RADIUS Access-Request to ISE that includes:
  • NAS-IP-Address  — the switch IP
  • NAS-Port-Id     — the exact port (e.g. GigabitEthernet1/0/23)
  • Calling-Station-Id — the AP Ethernet MAC (used as credential in MAB)

ISE stores this in its session database, queryable via the MnT REST API.

Usage
-----
    python debug_ise.py <ap_mac_or_ip>

    MAC accepted in any format:
        aa:bb:cc:dd:ee:ff   /   aabb.ccdd.eeff   /   aabbccddeeff

    If you provide an IP, Step 3 tries to resolve the MAC via the ERS endpoint API.
    The MAC must be the AP wired (Ethernet uplink) MAC, not a radio BSSID.

Required env vars
-----------------
    ISE_HOST   — ISE PAN hostname or IP (must be reachable on ports 443 and 9060)
    ISE_USER   — admin account with ERS and MnT read access
    ISE_PASS   — password

Optional
--------
    ISE_VERIFY_SSL=true   — enable TLS certificate verification (default: disabled)
"""

import os
import re
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST   = os.environ.get("ISE_HOST", "")
USER   = os.environ.get("ISE_USER", "")
PASS   = os.environ.get("ISE_PASS", "")
VERIFY = os.environ.get("ISE_VERIFY_SSL", "").lower() == "true"

if not HOST:
    print("ERROR: ISE_HOST env var is not set.")
    sys.exit(1)
if not USER or not PASS:
    print("ERROR: set ISE_USER and ISE_PASS env vars.")
    sys.exit(1)

RAW_LOOKUP = sys.argv[1] if len(sys.argv) > 1 else ""
if not RAW_LOOKUP:
    print("Usage: python debug_ise.py <ap_mac_or_ip>")
    sys.exit(1)

BASE_MNT = f"https://{HOST}"           # port 443 — MnT / newer REST API
BASE_ERS = f"https://{HOST}:9060/ers"  # port 9060 — External RESTful Services

AUTH     = (USER, PASS)
JSON_HDR = {"Content-Type": "application/json", "Accept": "application/json"}


def ok(msg):   print(f"  \033[32m✓\033[0m  {msg}")
def warn(msg): print(f"  \033[33m⚠\033[0m  {msg}")
def err(msg):  print(f"  \033[31m✕\033[0m  {msg}")
def step(msg): print(f"\n\033[1m{msg}\033[0m")


# ---------------------------------------------------------------------------
# MAC helpers
# ---------------------------------------------------------------------------

def norm_mac(raw: str) -> str:
    """Lowercase hex, no separators: aabbccddeeff"""
    return re.sub(r"[:\-.]", "", raw).lower()


def colon_mac(raw: str) -> str:
    """aa:bb:cc:dd:ee:ff"""
    m = norm_mac(raw)
    if len(m) != 12:
        return raw
    return ":".join(m[i:i+2] for i in range(0, 12, 2))


def is_mac(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{12}", re.sub(r"[:\-.]", "", s)))


def is_ip(s: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", s))


# Normalise input
if is_mac(RAW_LOOKUP):
    AP_MAC = colon_mac(RAW_LOOKUP)
    AP_IP  = None
    print(f"Input detected as MAC: {AP_MAC}")
elif is_ip(RAW_LOOKUP):
    AP_MAC = None
    AP_IP  = RAW_LOOKUP
    print(f"Input detected as IP: {AP_IP}")
else:
    AP_MAC = None
    AP_IP  = None
    warn(f"'{RAW_LOOKUP}' is not a recognized MAC or IP — will attempt lookup anyway.")

# ---------------------------------------------------------------------------
# Step 1 — ERS API auth check  (port 9060)
# ---------------------------------------------------------------------------

step("Step 1 — ERS API auth check  (port 9060)")
t0 = time.monotonic()
ers_ok = False
try:
    r = requests.get(
        f"{BASE_ERS}/config/version",
        auth=AUTH, headers=JSON_HDR, verify=VERIFY, timeout=10,
    )
    if r.status_code == 401:
        err("ERS auth rejected — check ISE_USER / ISE_PASS and ERS access rights.")
        print("  (Administration > System > Settings > ERS Settings — ensure ERS is ON)")
    elif r.status_code == 403:
        err("ERS account lacks read permission.")
    else:
        r.raise_for_status()
        ers_ok = True
        vinfo = r.json().get("VersionInfo", {})
        ok(f"ERS reachable in {time.monotonic()-t0:.1f}s")
        if vinfo:
            print(f"  ISE version  : {vinfo.get('currentResourceVersion', '?')}")
            print(f"  Patch bundle : {vinfo.get('patchbundle', '?')}")
except requests.exceptions.ConnectionError:
    warn("Cannot reach ISE on port 9060 — ERS steps will be skipped.")
    print("  Check: Administration > System > Settings > ERS Settings")
except Exception as exc:
    warn(f"ERS check failed: {exc}")

# ---------------------------------------------------------------------------
# Step 2 — MnT API auth check  (port 443)
# ---------------------------------------------------------------------------

step("Step 2 — MnT API auth check  (port 443)")
t0 = time.monotonic()
mnt_ok = False
try:
    r = requests.get(
        f"{BASE_MNT}/admin/API/mnt/Version",
        auth=AUTH, verify=VERIFY, timeout=10,
    )
    if r.status_code == 401:
        err("MnT auth rejected.")
        sys.exit(1)
    ok(f"MnT reachable in {time.monotonic()-t0:.1f}s  (HTTP {r.status_code})")
    mnt_ok = True
    print(f"  Content-Type : {r.headers.get('Content-Type', '?')!r}")
    print(f"  Body preview : {r.text[:200]!r}")
except requests.exceptions.ConnectionError as exc:
    err(f"Cannot reach ISE MnT on port 443: {exc}")
    sys.exit(1)
except Exception as exc:
    warn(f"MnT check error: {exc}")

if not mnt_ok:
    err("MnT API unavailable — cannot retrieve session data.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 3 — Resolve MAC from IP via ERS endpoint  (skip if MAC was given)
# ---------------------------------------------------------------------------

step("Step 3 — Endpoint lookup via ERS API")

lookup_mac = AP_MAC

if AP_MAC is None and AP_IP is not None and ers_ok:
    warn(f"No MAC given — searching ERS endpoint by IP {AP_IP}…")
    try:
        r = requests.get(
            f"{BASE_ERS}/config/endpoint",
            auth=AUTH, headers=JSON_HDR,
            params={"filter": f"ipAddress.EQ.{AP_IP}"},
            verify=VERIFY, timeout=10,
        )
        r.raise_for_status()
        resources = r.json().get("SearchResult", {}).get("resources", [])
        if resources:
            ep_id = resources[0]["id"]
            r2 = requests.get(
                f"{BASE_ERS}/config/endpoint/{ep_id}",
                auth=AUTH, headers=JSON_HDR, verify=VERIFY, timeout=10,
            )
            r2.raise_for_status()
            ep = r2.json().get("ERSEndPoint", {})
            lookup_mac = ep.get("mac", "")
            ok(f"Resolved MAC from ERS: {lookup_mac}")
        else:
            warn(f"No ERS endpoint found for IP {AP_IP}.")
    except Exception as exc:
        warn(f"ERS IP→MAC lookup failed: {exc}")

if lookup_mac and ers_ok:
    try:
        r = requests.get(
            f"{BASE_ERS}/config/endpoint",
            auth=AUTH, headers=JSON_HDR,
            params={"filter": f"mac.EQ.{lookup_mac}"},
            verify=VERIFY, timeout=10,
        )
        r.raise_for_status()
        resources = r.json().get("SearchResult", {}).get("resources", [])
        if resources:
            ep_id = resources[0]["id"]
            ok(f"ERS endpoint found: id={ep_id}  name={resources[0].get('name')!r}")
            r2 = requests.get(
                f"{BASE_ERS}/config/endpoint/{ep_id}",
                auth=AUTH, headers=JSON_HDR, verify=VERIFY, timeout=10,
            )
            r2.raise_for_status()
            ep = r2.json().get("ERSEndPoint", {})
            print("\n  ERS endpoint record:")
            for k in ("mac", "profileId", "groupId", "staticGroupAssignment",
                      "staticProfileAssignment", "identityStore"):
                if k in ep:
                    print(f"    {k:40s} = {ep[k]!r}")
        else:
            warn(f"MAC {lookup_mac} not found in ERS endpoint database.")
            print("  This is normal if the AP is not profiled in ISE.")
    except Exception as exc:
        warn(f"ERS endpoint MAC lookup failed: {exc}")
elif lookup_mac is None and not ers_ok:
    warn("ERS unavailable and no MAC given — session lookup may fail.")

# ---------------------------------------------------------------------------
# Step 4 — Active session lookup (MnT)
# ---------------------------------------------------------------------------
# The MnT session data includes the RADIUS attributes sent by the switch:
#   NAS-IP-Address  = switch management IP
#   NAS-Port-Id     = "GigabitEthernet1/0/23"  or  "Te1/1/0"
# This is the fastest path to switch + port info.

step("Step 4 — Active session lookup via MnT API  (/mnt/Session/MACAddress)")

session_raw = None
t0 = time.monotonic()

if lookup_mac:
    mac_fmt = colon_mac(lookup_mac)
    print(f"  Querying MnT for MAC: {mac_fmt}")
    try:
        r = requests.get(
            f"{BASE_MNT}/admin/API/mnt/Session/MACAddress/{mac_fmt}",
            auth=AUTH,
            headers={"Accept": "application/json"},
            verify=VERIFY, timeout=15,
        )
        if r.status_code == 404:
            warn("No active session found — will try auth history in Step 4b.")
        elif r.status_code == 200:
            ok(f"Session response in {time.monotonic()-t0:.1f}s")
            print(f"  Content-Type : {r.headers.get('Content-Type', '?')!r}")
            print(f"  Body size    : {len(r.content)} bytes")
            session_raw = r
        else:
            r.raise_for_status()
    except Exception as exc:
        warn(f"MnT session lookup failed: {exc}")

elif AP_IP:
    print(f"  No MAC — trying MnT session by IP: {AP_IP}")
    try:
        r = requests.get(
            f"{BASE_MNT}/admin/API/mnt/Session/IPAddress/{AP_IP}",
            auth=AUTH,
            headers={"Accept": "application/json"},
            verify=VERIFY, timeout=15,
        )
        if r.status_code == 404:
            warn("No active session found by IP.")
        elif r.status_code == 200:
            ok(f"Session response in {time.monotonic()-t0:.1f}s")
            session_raw = r
        else:
            r.raise_for_status()
    except Exception as exc:
        warn(f"MnT IP session lookup failed: {exc}")

# ---------------------------------------------------------------------------
# Step 4b — Last auth status  (MnT) — fallback for terminated sessions
# ---------------------------------------------------------------------------

if session_raw is None and lookup_mac:
    step("Step 4b — Last auth status  (/mnt/AuthStatus/MACAddress) — historical fallback")
    mac_fmt = colon_mac(lookup_mac)
    try:
        r = requests.get(
            f"{BASE_MNT}/admin/API/mnt/AuthStatus/MACAddress/{mac_fmt}",
            auth=AUTH,
            headers={"Accept": "application/json"},
            verify=VERIFY, timeout=15,
        )
        if r.status_code == 404:
            warn(f"No auth history for {mac_fmt}.")
        elif r.status_code == 200:
            ok(f"Auth history found in {time.monotonic()-t0:.1f}s")
            print(f"  Content-Type : {r.headers.get('Content-Type', '?')!r}")
            print(f"  Body size    : {len(r.content)} bytes")
            session_raw = r
        else:
            r.raise_for_status()
    except Exception as exc:
        warn(f"MnT auth history lookup failed: {exc}")

# ---------------------------------------------------------------------------
# Step 4c — ISE 3.2+ REST endpoint  (newer API path)
# ---------------------------------------------------------------------------

if session_raw is None and lookup_mac:
    step("Step 4c — ISE 3.2+ endpoint REST API  (/api/v1/endpoint/…)")
    mac_fmt = colon_mac(lookup_mac)
    for path in (
        f"/api/v1/endpoint/{mac_fmt}/policy",
        f"/api/v1/endpoint/{norm_mac(lookup_mac)}/policy",
    ):
        try:
            r = requests.get(
                f"{BASE_MNT}{path}",
                auth=AUTH, headers=JSON_HDR, verify=VERIFY, timeout=10,
            )
            if r.status_code == 200:
                ok(f"REST endpoint response: {path}")
                print(f"  Body: {r.text[:500]}")
                break
            elif r.status_code == 404:
                continue
            else:
                warn(f"REST path {path}: HTTP {r.status_code}")
        except Exception as exc:
            warn(f"REST path {path} failed: {exc}")

# ---------------------------------------------------------------------------
# Step 5 — Parse session data
# ---------------------------------------------------------------------------

step("Step 5 — Parsing session data for NAS-IP-Address / NAS-Port-Id")

if session_raw is None:
    warn("No session data obtained from any ISE API.")
    print("""
  Likely causes:
    1. AP switch port is NOT configured for 802.1X or MAB authentication
       → ISE has no session record for this AP (most common cause)
    2. Wrong MAC: AP wired Ethernet MAC ≠ MAC provided
       → Confirm with: show cdp neighbor <port> detail  (on the switch)
    3. AP is on a PSN that has not replicated to this MnT node
    4. Port is in monitor/open mode — authentication succeeded silently without ISE

  In this case DNAC topology remains the best automated fallback.
""")
    sys.exit(0)

# Parse JSON or display raw XML
ct = session_raw.headers.get("Content-Type", "")
if "json" not in ct.lower():
    warn(f"MnT returned non-JSON (Content-Type: {ct!r}).")
    print("  Raw response (first 1000 chars):")
    print("  " + session_raw.text[:1000])
    print()
    print("  Tip: ISE versions < 3.0 return XML from MnT. Upgrade or parse XML manually.")
    sys.exit(0)

try:
    data = session_raw.json()
except Exception as exc:
    warn(f"Could not parse JSON: {exc}")
    print(f"  Raw (first 500): {session_raw.text[:500]!r}")
    sys.exit(0)

print(f"\n  Top-level JSON keys: "
      f"{list(data.keys()) if isinstance(data, dict) else f'list of {len(data)}'}")


def unwrap_sessions(obj) -> list:
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return [obj]
    for key in ("activeSessionList", "AuthStatusList", "authStatusList",
                "sessionParameters", "sessions", "ERSActiveSession"):
        if key not in obj:
            continue
        val = obj[key]
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for sub in ("authStatusElements", "sessionList", "sessions"):
                if sub in val and isinstance(val[sub], list):
                    return val[sub]
            return [val]
    return [obj]


# Field name variants across ISE versions / API endpoints
_FIELD_ALIASES: dict[str, list[str]] = {
    "nas_ip":    ["nas_ip_address", "NAS-IP-Address", "nasIpAddress",
                  "nas_ipaddress", "NASIPAddress"],
    "nas_port":  ["nas_port_id", "NAS-Port-Id", "nasPortId", "nas_portid",
                  "NASPortId", "interface"],
    "called":    ["called_station_id", "Called-Station-Id", "calledStationId",
                  "CalledStationId"],
    "calling":   ["calling_station_id", "Calling-Station-Id", "callingStationId",
                  "CallingStationId", "mac"],
    "ep_ip":     ["framed_ip_address", "Framed-IP-Address", "framedIpAddress",
                  "endpointIp", "endpoint_ip", "ipAddress"],
    "username":  ["user_name", "userName", "User-Name", "username"],
    "auth_type": ["authentication_method", "authen_method", "authMethod",
                  "Authentication-Method"],
    "timestamp": ["timestamp", "event_timestamp", "cts_security_group"],
}


def get_field(d: dict, aliases: list[str]):
    for k in aliases:
        if k in d:
            return d[k]
        kl = k.lower().replace("-", "").replace("_", "")
        for dk, dv in d.items():
            if dk.lower().replace("-", "").replace("_", "") == kl:
                return dv
    return None


sessions = unwrap_sessions(data)
print(f"  Unwrapped to {len(sessions)} session(s).")

found_result = False
for i, sess in enumerate(sessions[:5]):
    print(f"\n  {'='*60}")
    print(f"  Session {i+1}/{min(len(sessions), 5)}")
    print(f"  {'='*60}")

    fields = {name: get_field(sess, aliases)
              for name, aliases in _FIELD_ALIASES.items()}

    print(f"  NAS-IP-Address  (switch IP)    : {fields['nas_ip']!r}")
    print(f"  NAS-Port-Id     (switch port)  : {fields['nas_port']!r}")
    print(f"  Called-Station-Id              : {fields['called']!r}")
    print(f"  Calling-Station-Id  (AP MAC)   : {fields['calling']!r}")
    print(f"  Framed-IP-Address   (AP IP)    : {fields['ep_ip']!r}")
    print(f"  Username                       : {fields['username']!r}")
    print(f"  Auth method                    : {fields['auth_type']!r}")
    print(f"  Timestamp                      : {fields['timestamp']!r}")

    if fields["nas_ip"] and fields["nas_port"]:
        ok("\n  *** RESULT ***")
        print(f"  Switch IP   : {fields['nas_ip']}")
        print(f"  Switch port : {fields['nas_port']}")
        if fields["called"]:
            print(f"  Called-Station-Id : {fields['called']}")
        found_result = True
    else:
        warn("NAS-IP-Address or NAS-Port-Id missing — dumping all session fields:")
        for k, v in sorted(sess.items()):
            if isinstance(v, (str, int, float, bool, type(None))):
                print(f"    {str(k):45s} = {v!r}")
            else:
                print(f"    {str(k):45s} = [{type(v).__name__}]")

# ---------------------------------------------------------------------------
# Step 6 — Resolve switch name via ISE NAD list (ERS)
# ---------------------------------------------------------------------------

step("Step 6 — Resolving switch name via ISE Network Access Devices  (ERS)")

if not ers_ok:
    warn("ERS unavailable — skipping switch name resolution.")
else:
    nas_ips: set[str] = set()
    for sess in sessions[:5]:
        ip = get_field(sess, _FIELD_ALIASES["nas_ip"])
        if ip:
            nas_ips.add(str(ip))

    if not nas_ips:
        warn("No NAS-IP found in sessions — cannot resolve switch name.")
    else:
        for nas_ip in nas_ips:
            try:
                r = requests.get(
                    f"{BASE_ERS}/config/networkdevice",
                    auth=AUTH, headers=JSON_HDR,
                    params={"filter": f"ipaddress.EQ.{nas_ip}"},
                    verify=VERIFY, timeout=10,
                )
                r.raise_for_status()
                nd_list = r.json().get("SearchResult", {}).get("resources", [])
                if nd_list:
                    nd_name = nd_list[0].get("name", "?")
                    nd_id   = nd_list[0].get("id", "?")
                    ok(f"Switch {nas_ip} → ISE NAD name : '{nd_name}'  (id={nd_id})")
                else:
                    warn(f"Switch {nas_ip} not in ISE NAD list.")
                    print("  The switch may be authenticated via a subnet NAD group.")
            except Exception as exc:
                warn(f"NAD lookup for {nas_ip} failed: {exc}")

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

print()
if found_result:
    ok("Done — switch + port found via ISE session data.")
else:
    warn("Done — could not extract switch/port from session data.")
    print("  Check the raw field dump above for port-related keys.")
    print("  If all fields are missing, the AP port is likely not doing 802.1X/MAB.")
