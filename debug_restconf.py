"""
Quick RESTCONF dump — run with:
  python debug_restconf.py <host> <username> <password>

Prints the first AP entry from capwap-data and the first entry from ap-tag
so we can see the exact field names the WLC returns.
"""
import json
import sys
import warnings

import requests
warnings.filterwarnings("ignore")   # suppress SSL warnings

if len(sys.argv) < 4:
    print("Usage: python debug_restconf.py <host> <user> <pass>")
    sys.exit(1)

host, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
base = f"https://{host}/restconf/data"
session = requests.Session()
session.auth = (user, pw)
session.verify = False
session.headers["Accept"] = "application/yang-data+json"

PATHS = [
    # WLAN candidates — trying multiple paths to find what this IOS-XE version exposes
    "Cisco-IOS-XE-wireless-oper:wireless-oper-data",
    "Cisco-IOS-XE-wireless-oper:wireless-oper-data/wlan-oper-data",
    "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-data/wlan-cfg-entries/wlan-cfg-entry",
    "Cisco-IOS-XE-wireless-oper:wireless-oper-data/ssid-oper-data",
]

for path in PATHS:
    url = f"{base}/{path}"
    print(f"\n{'='*60}")
    print(f"GET {path}")
    print('='*60)
    try:
        r = session.get(url, timeout=30)
        print(f"HTTP {r.status_code}")
        if r.status_code == 200 and r.content.strip():
            body = r.json()
            # Print top-level keys
            print(f"Top-level keys: {list(body.keys())}")
            # Print first entry of the first list found
            for k, v in body.items():
                if isinstance(v, list) and v:
                    print(f"\nFirst entry of '{k}':")
                    print(json.dumps(v[0], indent=2))
                    break
                elif isinstance(v, dict):
                    print(f"\nValue of '{k}' (dict):")
                    # look one level deeper for a list
                    for k2, v2 in v.items():
                        if isinstance(v2, list) and v2:
                            print(f"  First entry of '{k2}':")
                            print(json.dumps(v2[0], indent=2))
                            break
                    else:
                        print(json.dumps(v, indent=2))
                    break
        else:
            print(r.text[:400] if r.text else "(empty body)")
    except Exception as exc:
        print(f"ERROR: {exc}")
