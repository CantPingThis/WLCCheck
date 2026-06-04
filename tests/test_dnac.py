"""Tests for wlccheck/core/dnac.py — HTTP calls are mocked."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

import wlccheck.core.dnac as dnac_module
from wlccheck.core.dnac import (
    DNACClient,
    PortInfo,
    get_client,
    is_dnac_configured,
    set_inventory_host,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status: int, body: dict | list | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.content = json.dumps(body).encode() if body else b""
    resp.json.return_value = body or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


def _token_response() -> MagicMock:
    return _mock_response(200, {"Token": "test-token-abc"})


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level singletons between tests."""
    original_client = dnac_module._client
    original_host   = dnac_module._inventory_host
    yield
    dnac_module._client         = original_client
    dnac_module._inventory_host = original_host


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestModuleHelpers:
    def test_set_inventory_host_clears_client(self):
        dnac_module._client = MagicMock()
        set_inventory_host("dnac.example.com")
        assert dnac_module._inventory_host == "dnac.example.com"
        assert dnac_module._client is None

    def test_is_dnac_configured_with_inventory_host(self):
        set_inventory_host("dnac.example.com")
        assert is_dnac_configured() is True

    def test_is_dnac_configured_with_env_var(self, monkeypatch):
        dnac_module._inventory_host = None
        monkeypatch.setenv("DNAC_HOST", "dnac.env.com")
        assert is_dnac_configured() is True

    def test_is_dnac_configured_with_catalyst_env(self, monkeypatch):
        dnac_module._inventory_host = None
        monkeypatch.delenv("DNAC_HOST", raising=False)
        monkeypatch.setenv("CATALYST_HOST", "catalyst.env.com")
        assert is_dnac_configured() is True

    def test_is_dnac_configured_false_when_nothing_set(self, monkeypatch):
        dnac_module._inventory_host = None
        monkeypatch.delenv("DNAC_HOST", raising=False)
        monkeypatch.delenv("CATALYST_HOST", raising=False)
        assert is_dnac_configured() is False


class TestGetClient:
    def test_returns_none_when_no_host(self, monkeypatch):
        dnac_module._client = None
        dnac_module._inventory_host = None
        monkeypatch.delenv("DNAC_HOST", raising=False)
        monkeypatch.delenv("CATALYST_HOST", raising=False)
        assert get_client("user", "pass") is None

    def test_returns_none_when_no_credentials(self, monkeypatch):
        dnac_module._client = None
        dnac_module._inventory_host = "dnac.example.com"
        monkeypatch.delenv("DNAC_USER", raising=False)
        monkeypatch.delenv("WLC_USER", raising=False)
        monkeypatch.delenv("DNAC_PASS", raising=False)
        monkeypatch.delenv("WLC_PASS", raising=False)
        assert get_client("", "") is None

    def test_builds_client_from_inventory_host(self, monkeypatch):
        dnac_module._client = None
        dnac_module._inventory_host = "dnac.example.com"
        monkeypatch.delenv("DNAC_USER", raising=False)
        monkeypatch.delenv("DNAC_PASS", raising=False)
        monkeypatch.delenv("WLC_USER", raising=False)
        monkeypatch.delenv("WLC_PASS", raising=False)
        client = get_client("admin", "secret")
        assert client is not None
        assert client._host == "dnac.example.com"

    def test_returns_cached_client(self):
        mock_client = MagicMock()
        dnac_module._client = mock_client
        result = get_client()
        assert result is mock_client

    def test_env_var_credentials_take_priority(self, monkeypatch):
        dnac_module._client = None
        dnac_module._inventory_host = "dnac.example.com"
        monkeypatch.setenv("DNAC_USER", "env-user")
        monkeypatch.setenv("DNAC_PASS", "env-pass")
        client = get_client("fallback-user", "fallback-pass")
        assert client is not None
        assert client._user == "env-user"

    def test_env_var_host_used_when_no_inventory(self, monkeypatch):
        dnac_module._client = None
        dnac_module._inventory_host = None
        monkeypatch.setenv("DNAC_HOST", "env-dnac.com")
        monkeypatch.setenv("DNAC_USER", "u")
        monkeypatch.setenv("DNAC_PASS", "p")
        client = get_client()
        assert client is not None
        assert client._host == "env-dnac.com"

    def test_wlc_user_pass_fallback(self, monkeypatch):
        dnac_module._client = None
        dnac_module._inventory_host = "dnac.example.com"
        monkeypatch.delenv("DNAC_USER", raising=False)
        monkeypatch.delenv("DNAC_PASS", raising=False)
        monkeypatch.setenv("WLC_USER", "wlc-user")
        monkeypatch.setenv("WLC_PASS", "wlc-pass")
        client = get_client()
        assert client is not None
        assert client._user == "wlc-user"


# ---------------------------------------------------------------------------
# DNACClient._get_token
# ---------------------------------------------------------------------------

class TestGetToken:
    def test_fetches_token(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.post", return_value=_token_response()):
            token = c._get_token()
        assert token == "test-token-abc"
        assert c._token == "test-token-abc"

    def test_token_cached(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.post", return_value=_token_response()) as mock_post:
            c._get_token()
            c._get_token()
        assert mock_post.call_count == 1

    def test_token_refreshed_when_expired(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.post", return_value=_token_response()) as mock_post:
            c._get_token()
            c._token_ts = time.monotonic() - 4000
            c._get_token()
        assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# DNACClient._get_device_by_ip
# ---------------------------------------------------------------------------

class TestGetDeviceByIp:
    def test_returns_device_dict(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        body = {"response": {"id": "uuid-1", "hostname": "AP-01"}}
        with patch("wlccheck.core.dnac.requests.get", return_value=_mock_response(200, body)):
            device = c._get_device_by_ip("test-token", "10.0.0.1")
        assert device["id"] == "uuid-1"

    def test_returns_none_on_404(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.get", return_value=_mock_response(404)):
            device = c._get_device_by_ip("test-token", "10.0.0.1")
        assert device is None


# ---------------------------------------------------------------------------
# DNACClient._get_device_by_hostname
# ---------------------------------------------------------------------------

class TestGetDeviceByHostname:
    def test_returns_first_device(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        body = {"response": [{"id": "uuid-2", "hostname": "AP-LAB"}]}
        with patch("wlccheck.core.dnac.requests.get", return_value=_mock_response(200, body)):
            device = c._get_device_by_hostname("test-token", "AP-LAB")
        assert device["id"] == "uuid-2"

    def test_returns_none_when_empty(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        body = {"response": []}
        with patch("wlccheck.core.dnac.requests.get", return_value=_mock_response(200, body)):
            device = c._get_device_by_hostname("test-token", "UNKNOWN")
        assert device is None


# ---------------------------------------------------------------------------
# DNACClient._get_topology
# ---------------------------------------------------------------------------

class TestGetTopology:
    def _topo_body(self) -> dict:
        return {
            "response": {
                "nodes": [
                    {"id": "node-1", "label": "AP-01", "ip": "10.0.0.1"},
                    {"id": "node-2", "label": "SW-01", "ip": "10.0.1.1"},
                ],
                "links": [
                    {"source": "node-1", "target": "node-2",
                     "startPortName": "GE0", "endPortName": "Gi1/0/1"}
                ],
            }
        }

    def test_fetches_topology(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, self._topo_body())):
            topo = c._get_topology("token")
        assert len(topo["nodes"]) == 2
        assert len(topo["links"]) == 1

    def test_topology_cached(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, self._topo_body())) as mock_get:
            c._get_topology("token")
            c._get_topology("token")
        assert mock_get.call_count == 1

    def test_topology_refreshed_when_expired(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, self._topo_body())) as mock_get:
            c._get_topology("token")
            c._topo_ts = time.monotonic() - 400
            c._get_topology("token")
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# DNACClient._resolve_topology_id
# ---------------------------------------------------------------------------

class TestResolveTopologyId:
    def _nodes(self):
        return [
            {"id": "node-1", "label": "AP-01", "ip": "10.0.0.1"},
            {"id": "node-2", "label": "SW-01", "ip": "10.0.1.1"},
        ]

    def test_direct_match(self):
        result = DNACClient._resolve_topology_id(self._nodes(), "node-1", "", "")
        assert result == "node-1"

    def test_ip_fallback(self):
        result = DNACClient._resolve_topology_id(self._nodes(), "unknown-uuid", "10.0.0.1", "")
        assert result == "node-1"

    def test_hostname_fallback(self):
        result = DNACClient._resolve_topology_id(self._nodes(), "unknown-uuid", "", "ap-01")
        assert result == "node-1"

    def test_hostname_case_insensitive(self):
        result = DNACClient._resolve_topology_id(self._nodes(), "unknown-uuid", "", "AP-01")
        assert result == "node-1"

    def test_fallback_to_device_id_when_nothing_matches(self):
        result = DNACClient._resolve_topology_id(self._nodes(), "fallback-id", "9.9.9.9", "NOPE")
        assert result == "fallback-id"

    def test_ip_takes_priority_over_hostname(self):
        result = DNACClient._resolve_topology_id(self._nodes(), "unknown", "10.0.0.1", "SW-01")
        assert result == "node-1"


# ---------------------------------------------------------------------------
# DNACClient._find_port
# ---------------------------------------------------------------------------

class TestFindPort:
    def _topo(self) -> dict:
        return {
            "nodes": [
                {"id": "ap-id", "label": "AP-01", "ip": "10.0.0.1"},
                {"id": "sw-id", "label": "SW-CORE", "ip": "10.0.1.1"},
            ],
            "links": [
                {"source": "ap-id", "target": "sw-id",
                 "startPortName": "GE0", "endPortName": "Gi1/0/1"}
            ],
        }

    def test_ap_as_source(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, {"response": self._topo()})):
            result = c._find_port("token", "ap-id")
        assert result is not None
        assert result.switch_name == "SW-CORE"
        assert result.switch_port == "Gi1/0/1"

    def test_ap_as_target(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        topo = self._topo()
        topo["links"][0]["source"] = "sw-id"
        topo["links"][0]["target"] = "ap-id"
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, {"response": topo})):
            result = c._find_port("token", "ap-id")
        assert result is not None
        assert result.switch_name == "SW-CORE"
        assert result.switch_port == "GE0"

    def test_returns_none_when_no_link(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        topo = {"nodes": [], "links": []}
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, {"response": topo})):
            result = c._find_port("token", "no-such-id")
        assert result is None

    def test_uses_ip_fallback_for_topology_id(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, {"response": self._topo()})):
            result = c._find_port("token", "different-uuid", ip_addr="10.0.0.1")
        assert result is not None
        assert result.switch_name == "SW-CORE"

    def test_missing_port_name_defaults_to_dash(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        topo = self._topo()
        del topo["links"][0]["endPortName"]
        del topo["links"][0]["startPortName"]
        with patch("wlccheck.core.dnac.requests.get",
                   return_value=_mock_response(200, {"response": topo})):
            result = c._find_port("token", "ap-id")
        assert result is not None
        assert result.switch_port == "—"


# ---------------------------------------------------------------------------
# DNACClient.get_ap_port — integration
# ---------------------------------------------------------------------------

class TestGetApPort:
    def _make_side_effect(self, device: dict | None, topo: dict) -> callable:
        def side_effect(url, **kwargs):
            if "ip-address" in url:
                if device is None:
                    return _mock_response(404)
                return _mock_response(200, {"response": device})
            if "network-device" in url:
                if device:
                    return _mock_response(200, {"response": [device]})
                return _mock_response(200, {"response": []})
            if "topology" in url:
                return _mock_response(200, {"response": topo})
            return _mock_response(404)
        return side_effect

    def test_returns_port_info(self):
        topo = {
            "nodes": [
                {"id": "ap-id", "label": "AP-01", "ip": "10.0.0.1"},
                {"id": "sw-id", "label": "SW-CORE", "ip": "10.0.1.1"},
            ],
            "links": [{"source": "ap-id", "target": "sw-id",
                       "startPortName": "GE0", "endPortName": "Gi1/0/1"}]
        }
        device = {"id": "ap-id", "hostname": "AP-01", "managementIpAddress": "10.0.0.1"}

        c = DNACClient("dnac.example.com", "admin", "secret")
        c._token = "cached-token"
        c._token_ts = time.monotonic()

        with patch("wlccheck.core.dnac.requests.get",
                   side_effect=self._make_side_effect(device, topo)):
            result = c.get_ap_port("10.0.0.1", "AP-01")

        assert result is not None
        assert result.switch_name == "SW-CORE"
        assert result.switch_port == "Gi1/0/1"

    def test_returns_none_when_device_not_found(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        c._token = "cached-token"
        c._token_ts = time.monotonic()

        with patch("wlccheck.core.dnac.requests.get",
                   side_effect=self._make_side_effect(None, {"nodes": [], "links": []})):
            result = c.get_ap_port("10.0.0.99")
        assert result is None

    def test_exception_returns_none(self):
        c = DNACClient("dnac.example.com", "admin", "secret")
        with patch("wlccheck.core.dnac.requests.post",
                   side_effect=Exception("network failure")):
            result = c.get_ap_port("10.0.0.1")
        assert result is None

    def test_falls_back_to_hostname_lookup(self):
        topo = {
            "nodes": [
                {"id": "ap-id", "label": "AP-FB", "ip": "10.0.0.5"},
                {"id": "sw-id", "label": "SW", "ip": "10.0.1.1"},
            ],
            "links": [{"source": "ap-id", "target": "sw-id",
                       "startPortName": "GE0", "endPortName": "Gi2/0/1"}]
        }
        device = {"id": "ap-id", "hostname": "AP-FB", "managementIpAddress": "10.0.0.5"}

        c = DNACClient("dnac.example.com", "admin", "secret")
        c._token = "cached-token"
        c._token_ts = time.monotonic()

        def side_effect(url, **kwargs):
            if "ip-address" in url:
                return _mock_response(404)
            if "network-device" in url:
                return _mock_response(200, {"response": [device]})
            if "topology" in url:
                return _mock_response(200, {"response": topo})
            return _mock_response(404)

        with patch("wlccheck.core.dnac.requests.get", side_effect=side_effect):
            result = c.get_ap_port("10.0.0.99", "AP-FB")

        assert result is not None
        assert result.switch_port == "Gi2/0/1"
