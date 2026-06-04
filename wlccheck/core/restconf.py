from __future__ import annotations

import urllib3
from typing import Callable, Dict, List, Optional

import requests

from .models import (
    APRecord,
    ClientRecord,
    WLANRecord,
    normalize_client_state,
    normalize_state,
)

# Cisco 9800 WLC uses self-signed certificates in most enterprise deployments.
# SSL verification is intentionally disabled; caller controls verify_ssl flag.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # NOSONAR python:S4830


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WLCError(Exception):
    """Base exception for all WLC errors."""

class WLCConnectionError(WLCError):
    """Could not reach the WLC."""

class WLCAuthError(WLCError):
    """Authentication rejected by the WLC."""

class WLCDataError(WLCError):
    """Unexpected / missing data in the RESTCONF response."""


# ---------------------------------------------------------------------------
# RESTCONF paths (Cisco-IOS-XE YANG)
# ---------------------------------------------------------------------------

_HOSTNAME_PATH = "Cisco-IOS-XE-native:native/hostname"

_CAPWAP_PATH = (
    "Cisco-IOS-XE-wireless-access-point-oper:"
    "access-point-oper-data/capwap-data"
)
_AP_TAG_PATH = (
    "Cisco-IOS-XE-wireless-access-point-oper:"
    "access-point-oper-data/ap-tag"
)
_WLAN_PATH = (
    "Cisco-IOS-XE-wireless-wlan-cfg:"
    "wlan-cfg-data/wlan-cfg-entries/wlan-cfg-entry"
)
_CLIENT_PATH = (
    "Cisco-IOS-XE-wireless-client-oper:"
    "client-oper-data/common-oper-data"
)


# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

StatusCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class WLCClient:
    """Thin RESTCONF client for a single Cisco 9800 WLC."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,  # NOSONAR python:S4830 — enterprise WLC certs are self-signed
        timeout: int = 60,
    ) -> None:
        self.host = host.strip()
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._base = f"https://{self.host}/restconf/data"
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.verify = verify_ssl  # NOSONAR python:S4830
        self._session.headers.update({"Accept": "application/yang-data+json"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_auth(self) -> None:
        """Lightweight auth probe — raises WLCAuthError on 401/403, ignores response body."""
        self._get(_HOSTNAME_PATH, timeout=10)

    def get_hostname(self) -> Optional[str]:
        try:
            resp = self._get(_HOSTNAME_PATH, timeout=10)
            return resp.json().get("Cisco-IOS-XE-native:hostname")
        except Exception:  # NOSONAR python:S112 — graceful degradation for optional hostname
            return None

    def get_ap_data(
        self, status_cb: Optional[StatusCallback] = None
    ) -> List[APRecord]:
        """Fetch all AP CAPWAP operational data (join state, model, location)."""
        self._notify(status_cb, "Fetching AP operational data…")
        try:
            resp = self._get(_CAPWAP_PATH)
        except WLCAuthError:
            raise
        except WLCError:
            raise
        except Exception as exc:  # NOSONAR python:S112 — wraps unknown requests errors into typed WLC exception
            raise WLCConnectionError(f"Request failed: {exc}") from exc

        raw_list = self._safe_list(resp)
        if not raw_list:
            self._notify(status_cb, "[yellow]⚠[/yellow] WLC returned 0 access points.")
            return []

        self._notify(status_cb, f"Processing {len(raw_list)} access points…")
        records = [self._parse_ap(e) for e in raw_list]
        self._notify(status_cb, f"[green]✓[/green] {len(records)} APs collected.")
        return records

    def get_ap_tags(
        self,
        status_cb: Optional[StatusCallback] = None,
    ) -> Dict[str, tuple[str, str, str]]:
        """Return {wtp_mac: (policy_tag, site_tag, rf_tag)} for all APs."""
        self._notify(status_cb, "Fetching AP tag assignments…")
        try:
            resp = self._get(_AP_TAG_PATH)
        except WLCAuthError:
            raise
        except WLCError as exc:
            self._notify(status_cb, f"[yellow]⚠[/yellow] AP tags skipped: {exc}")
            return {}
        except Exception as exc:  # NOSONAR python:S112 — optional feature, degrades gracefully
            self._notify(status_cb, f"[yellow]⚠[/yellow] AP tags failed: {exc}")
            return {}

        raw_list = self._safe_list(resp, keys=(
            "Cisco-IOS-XE-wireless-access-point-oper:ap-tag",
            "ap-tag",
        ))
        # Fallback: nested under parent container
        if not raw_list and resp.status_code not in (204,) and resp.content.strip():
            try:
                body = resp.json()
                container = (
                    body.get("Cisco-IOS-XE-wireless-access-point-oper:access-point-oper-data")
                    or body.get("access-point-oper-data")
                )
                if isinstance(container, dict):
                    nested = container.get("ap-tag", [])
                    if isinstance(nested, list):
                        raw_list = nested
                if not raw_list:
                    top_keys = list(body.keys())[:5]
                    self._notify(
                        status_cb,
                        f"[yellow]⚠[/yellow] AP tag response keys: {top_keys}",
                    )
            except Exception:  # NOSONAR python:S112 — JSON parse fallback, failure is non-fatal
                pass

        tags: Dict[str, tuple[str, str, str]] = {}
        for entry in raw_list:
            mac = entry.get("wtp-mac", "")
            if not mac:
                continue
            tags[self._norm_mac(mac)] = (
                entry.get("policy-tag", ""),
                entry.get("site-tag", ""),
                entry.get("rf-tag", ""),
            )
        self._notify(status_cb, f"[green]✓[/green] Tags fetched for {len(tags)} APs.")
        return tags

    def get_wlans(
        self, status_cb: Optional[StatusCallback] = None
    ) -> List[WLANRecord]:
        """Fetch WLAN operational state and client counts."""
        self._notify(status_cb, "Fetching WLAN operational data…")
        try:
            resp = self._get(_WLAN_PATH)
        except WLCAuthError:
            raise
        except WLCError as exc:
            self._notify(status_cb, f"[yellow]⚠[/yellow] WLAN fetch skipped: {exc}")
            return []
        except Exception as exc:  # NOSONAR python:S112 — optional feature, degrades gracefully
            self._notify(status_cb, f"[yellow]⚠[/yellow] WLAN fetch failed: {exc}")
            return []

        raw_list = self._safe_list(resp, keys=(
            "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-entry",
            "wlan-cfg-entry",
        ))
        wlans = [self._parse_wlan(e) for e in raw_list]
        self._notify(status_cb, f"[green]✓[/green] {len(wlans)} WLANs collected.")
        return wlans

    def get_clients(
        self, status_cb: Optional[StatusCallback] = None
    ) -> List[ClientRecord]:
        """Fetch full wireless client list (can be large on busy WLCs)."""
        self._notify(status_cb, "Fetching client data (may be slow)…")
        try:
            resp = self._get(_CLIENT_PATH, timeout=120)
        except WLCAuthError:
            raise
        except WLCError as exc:
            self._notify(status_cb, f"[yellow]⚠[/yellow] Client fetch skipped: {exc}")
            return []
        except Exception as exc:  # NOSONAR python:S112 — optional feature, degrades gracefully
            self._notify(status_cb, f"[yellow]⚠[/yellow] Client fetch failed: {exc}")
            return []

        raw_list = self._safe_list(resp, keys=(
            "Cisco-IOS-XE-wireless-client-oper:common-oper-data",
            "common-oper-data",
        ))
        clients = [self._parse_client(e) for e in raw_list]
        no_ip = sum(1 for c in clients if not c.has_ip)
        self._notify(
            status_cb,
            f"[green]✓[/green] {len(clients)} clients "
            f"({no_ip} without IP).",
        )
        return clients

    # ------------------------------------------------------------------
    # Private helpers — HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, timeout: Optional[int] = None) -> requests.Response:
        url = f"{self._base}/{path}"
        t = timeout or self.timeout
        try:
            resp = self._session.get(url, timeout=t)
        except requests.exceptions.ConnectionError as exc:
            raise WLCConnectionError(
                f"Cannot reach {self.host} — check IP / RESTCONF enabled."
            ) from exc
        except requests.exceptions.Timeout:
            raise WLCConnectionError(
                f"Timed out connecting to {self.host} after {t}s."
            )

        if resp.status_code == 401:
            raise WLCAuthError("Authentication failed — check credentials.")
        if resp.status_code == 403:
            raise WLCAuthError("Authorisation denied — user may lack RESTCONF privileges.")
        if resp.status_code == 404:
            raise WLCDataError(f"RESTCONF path not found: {path}")
        resp.raise_for_status()
        return resp

    # ------------------------------------------------------------------
    # Private helpers — parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_mac(mac: str) -> str:
        """Normalise MAC to lowercase hex without separators: aabbccddeeff."""
        return mac.lower().replace(":", "").replace("-", "").replace(".", "")

    @staticmethod
    def _safe_list(
        resp: requests.Response,
        keys: tuple = (
            "Cisco-IOS-XE-wireless-access-point-oper:capwap-data",
            "capwap-data",
        ),
    ) -> list:
        """Extract a list from a RESTCONF response, returning [] on any empty/error."""
        if resp.status_code == 204 or not resp.content.strip():
            return []
        try:
            body = resp.json()
        except ValueError:
            return []
        for key in keys:
            if key in body:
                val = body[key]
                return val if isinstance(val, list) else []
        return []

    @staticmethod
    def _parse_ap(entry: dict) -> APRecord:
        ap_oper = entry.get("ap-oper-data", {})
        raw_state = (
            ap_oper.get("ap-state")
            or ap_oper.get("ap-operation-state")
            or entry.get("ap-state")
            or entry.get("ap-operation-state")
            or "unknown"
        )
        if isinstance(raw_state, dict):
            raw_state = (
                raw_state.get("ap-state")
                or raw_state.get("ap-operation-state")
                or "unknown"
            )

        wtp_oper = entry.get("wtp-oper-data", {})
        model = wtp_oper.get("ap-model") or entry.get("ap-model") or ""

        ip_raw  = entry.get("ip-addr", "")
        ip_addr = ip_raw if isinstance(ip_raw, str) else str(ip_raw)

        # Tags are embedded in capwap-data under "tag-info".
        # Field names confirmed on IOS-XE 17.12:
        #   policy-tag-info.policy-tag-name
        #   site-tag.site-tag-name
        #   rf-tag.rf-tag-name
        # resolved-tag-info gives the effective (resolved) names and is used
        # as a fallback.
        tag_info = entry.get("tag-info") or {}
        resolved = tag_info.get("resolved-tag-info") or {}
        policy_tag = (
            tag_info.get("policy-tag-info", {}).get("policy-tag-name")
            or resolved.get("resolved-policy-tag")
            or ""
        )
        site_tag = (
            tag_info.get("site-tag", {}).get("site-tag-name")
            or resolved.get("resolved-site-tag")
            or ""
        )
        rf_tag = (
            tag_info.get("rf-tag", {}).get("rf-tag-name")
            or resolved.get("resolved-rf-tag")
            or ""
        )

        return APRecord(
            wtp_mac=entry.get("wtp-mac", ""),
            name=entry.get("name", ""),
            raw_state=str(raw_state),
            state=normalize_state(str(raw_state)),
            ip_addr=ip_addr,
            model=model,
            location=entry.get("location", ""),
            policy_tag=policy_tag,
            site_tag=site_tag,
            rf_tag=rf_tag,
        )

    @staticmethod
    def _parse_wlan(entry: dict) -> WLANRecord:
        # State can be a boolean, an enum string, or missing
        # Config schema (wlan-cfg-data): SSID and state live under apf-vap-id-data.
        # wlan-status is a boolean (true = enabled/up).
        vap = entry.get("apf-vap-id-data") or {}
        ssid = vap.get("ssid") or entry.get("ssid") or entry.get("profile-name", "")
        raw_status = vap.get("wlan-status", entry.get("wlan-status"))
        if isinstance(raw_status, bool):
            state = "up" if raw_status else "down"
        elif raw_status is None:
            state = "up"  # absent means no explicit disable
        else:
            s = str(raw_status).lower()
            state = "up" if s in ("true", "up", "enabled", "active") else "down"

        return WLANRecord(
            wlan_id=int(entry.get("wlan-id", 0)),
            profile_name=entry.get("profile-name", ""),
            ssid=ssid,
            state=state,
            client_count=0,  # not available from config endpoint
        )

    @staticmethod
    def _parse_client(entry: dict) -> ClientRecord:
        # IPv4 address — may be nested
        ipv4_raw = entry.get("ipv4-addr", entry.get("ipv4", ""))
        if isinstance(ipv4_raw, dict):
            ipv4 = ipv4_raw.get("value", "")
        else:
            ipv4 = str(ipv4_raw) if ipv4_raw else ""
        # Exclude 0.0.0.0 (DHCP not yet assigned)
        if ipv4 == "0.0.0.0":
            ipv4 = ""

        # IPv6 — may be a list of addresses
        ipv6_raw = entry.get("ipv6-addr", entry.get("ipv6", ""))
        if isinstance(ipv6_raw, list):
            ipv6 = ipv6_raw[0] if ipv6_raw else ""
        else:
            ipv6 = str(ipv6_raw) if ipv6_raw else ""
        if ipv6 in ("::", ""):
            ipv6 = ""

        raw_state = entry.get("ms-assoc-state", entry.get("client-state", ""))
        state = normalize_client_state(str(raw_state).lower())

        return ClientRecord(
            mac=entry.get("client-mac", entry.get("mac", "")),
            ap_name=entry.get("ap-name", ""),
            wlan_ssid=entry.get("ssid", entry.get("wlan-ssid", "")),
            ipv4=ipv4,
            ipv6=ipv6,
            state=state,
            username=entry.get("username", ""),
        )

    @staticmethod
    def _notify(cb: Optional[StatusCallback], msg: str) -> None:
        if cb:
            cb(msg)
