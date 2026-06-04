from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import requests
import urllib3

# DNAC/Catalyst Center typically uses self-signed certificates in enterprise deployments.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # NOSONAR python:S4830


@dataclass
class PortInfo:
    switch_name: str
    switch_port: str


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_client:         Optional[DNACClient] = None
_inventory_host: Optional[str]        = None   # set from wlc_inventory.csv at app start


def set_inventory_host(host: str) -> None:
    """Called at app startup when a dnac row is found in the inventory CSV."""
    global _inventory_host, _client
    _inventory_host = host
    _client = None  # force rebuild on next get_client() call


def is_dnac_configured() -> bool:
    """Return True if a DNAC host is known (inventory or env var)."""
    return bool(
        _inventory_host
        or os.environ.get("DNAC_HOST")
        or os.environ.get("CATALYST_HOST")
    )


def get_client(username: str = "", password: str = "") -> Optional[DNACClient]:
    """Return the shared DNACClient, building it lazily on first call.

    ``username`` and ``password`` are used when env vars are absent — pass
    ``app.username`` / ``app.password`` from the Textual app so interactive
    credential entry works even without WLC_USER / WLC_PASS in the environment.
    """
    global _client
    if _client is not None:
        return _client
    host = _inventory_host or os.environ.get("DNAC_HOST") or os.environ.get("CATALYST_HOST", "")
    if not host:
        return None
    user = os.environ.get("DNAC_USER") or os.environ.get("WLC_USER") or username
    pwd  = os.environ.get("DNAC_PASS") or os.environ.get("WLC_PASS") or password
    if not (user and pwd):
        return None
    _client = DNACClient(host=host, username=user, password=pwd)
    return _client


class DNACClient:
    _TOKEN_TTL    = 3540   # refresh token 1 minute before DNAC's 1-hour expiry
    _TOPOLOGY_TTL = 300    # cache full topology for 5 minutes

    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False) -> None:  # NOSONAR python:S4830
        self._host   = host
        self._user   = username
        self._pass   = password
        self._verify = verify_ssl

        self._token:    Optional[str]   = None
        self._token_ts: float           = 0.0
        self._topo:     Optional[dict]  = None
        self._topo_ts:  float           = 0.0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_ap_port(self, ip_addr: str, ap_name: str = "") -> Optional[PortInfo]:
        """Return switch name + switch port for an AP, or None on any failure."""
        try:
            token  = self._get_token()
            device = self._get_device_by_ip(token, ip_addr)
            if device is None and ap_name:
                device = self._get_device_by_hostname(token, ap_name)
            if device is None:
                return None
            return self._find_port(
                token,
                device_id=device["id"],
                ip_addr=ip_addr,
                hostname=device.get("hostname", ""),
            )
        except Exception:  # NOSONAR python:S112 — any DNAC failure is non-fatal, returns None
            return None

    # ------------------------------------------------------------------
    # Private — network calls
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token and (time.monotonic() - self._token_ts) < self._TOKEN_TTL:
            return self._token
        r = requests.post(
            f"https://{self._host}/dna/system/api/v1/auth/token",
            auth=(self._user, self._pass),
            verify=self._verify,
            timeout=10,
        )
        r.raise_for_status()
        self._token    = r.json()["Token"]
        self._token_ts = time.monotonic()
        return self._token

    def _get_device_by_ip(self, token: str, ip: str) -> Optional[dict]:
        r = requests.get(
            f"https://{self._host}/dna/intent/api/v1/network-device/ip-address/{ip}",
            headers={"X-Auth-Token": token},
            verify=self._verify,
            timeout=10,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("response")

    def _get_device_by_hostname(self, token: str, hostname: str) -> Optional[dict]:
        r = requests.get(
            f"https://{self._host}/dna/intent/api/v1/network-device",
            headers={"X-Auth-Token": token},
            params={"hostname": hostname},
            verify=self._verify,
            timeout=10,
        )
        r.raise_for_status()
        devices = r.json().get("response", [])
        return devices[0] if devices else None

    def _get_topology(self, token: str) -> dict:
        if self._topo and (time.monotonic() - self._topo_ts) < self._TOPOLOGY_TTL:
            return self._topo
        r = requests.get(
            f"https://{self._host}/dna/intent/api/v1/topology/physical-topology",
            headers={"X-Auth-Token": token},
            verify=self._verify,
            timeout=20,
        )
        r.raise_for_status()
        self._topo    = r.json().get("response", {})
        self._topo_ts = time.monotonic()
        return self._topo

    def _find_port(
        self,
        token:     str,
        device_id: str,
        ip_addr:   str = "",
        hostname:  str = "",
    ) -> Optional[PortInfo]:
        topo      = self._get_topology(token)
        raw_nodes = topo.get("nodes", [])
        node_map  = {n["id"]: n.get("label") or n.get("ip") or "?" for n in raw_nodes}
        eid       = self._resolve_topology_id(raw_nodes, device_id, ip_addr, hostname)

        for link in topo.get("links", []):
            src = link.get("source")
            tgt = link.get("target")
            if src == eid:
                return PortInfo(
                    switch_name=node_map.get(tgt, "—"),
                    switch_port=link.get("endPortName") or "—",
                )
            if tgt == eid:
                return PortInfo(
                    switch_name=node_map.get(src, "—"),
                    switch_port=link.get("startPortName") or "—",
                )
        return None

    @staticmethod
    def _resolve_topology_id(
        nodes:     list,
        device_id: str,
        ip_addr:   str,
        hostname:  str,
    ) -> str:
        """Return the topology node ID that matches this device.

        The UUID from the network-device API sometimes differs from the node ID
        used in topology links; fall back to matching by IP then by hostname.
        """
        known_ids = {n["id"] for n in nodes}
        if device_id in known_ids:
            return device_id
        for n in nodes:
            if ip_addr and n.get("ip") == ip_addr:
                return n["id"]
            if hostname and n.get("label", "").lower() == hostname.lower():
                return n["id"]
        return device_id
