"""Tests for wlccheck/core/restconf.py — HTTP calls are mocked."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from wlccheck.core.restconf import (
    WLCAuthError,
    WLCClient,
    WLCConnectionError,
    WLCDataError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status: int, body: dict | list | None = None,
                   content: bytes | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.content = content if content is not None else (
        json.dumps(body).encode() if body is not None else b""
    )
    resp.json.return_value = body or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


def _client() -> WLCClient:
    return WLCClient("10.0.0.1", "admin", "secret")


# ---------------------------------------------------------------------------
# _get / HTTP error handling
# ---------------------------------------------------------------------------

class TestGet:
    def test_401_raises_auth_error(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(401)):
            with pytest.raises(WLCAuthError):
                c._get("some/path")

    def test_403_raises_auth_error(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(403)):
            with pytest.raises(WLCAuthError):
                c._get("some/path")

    def test_404_raises_data_error(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(404)):
            with pytest.raises(WLCDataError):
                c._get("some/path")

    def test_200_returns_response(self):
        c = _client()
        resp = _mock_response(200, {"key": "value"})
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            result = c._get("some/path")
            assert result is resp

    def test_connection_error_raises_wlc_connection_error(self):
        c = _client()
        with patch.object(c._session, "get",
                          side_effect=requests.exceptions.ConnectionError("refused")):
            with pytest.raises(WLCConnectionError, match="check IP"):
                c._get("some/path")

    def test_timeout_raises_wlc_connection_error(self):
        c = _client()
        with patch.object(c._session, "get",
                          side_effect=requests.exceptions.Timeout()):
            with pytest.raises(WLCConnectionError, match="Timed out"):
                c._get("some/path")

    def test_custom_timeout_used(self):
        c = _client()
        resp = _mock_response(200, {})
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp) as mock_get:
            c._get("some/path", timeout=5)
            mock_get.assert_called_once()
            _, kwargs = mock_get.call_args
            assert kwargs["timeout"] == 5


# ---------------------------------------------------------------------------
# check_auth
# ---------------------------------------------------------------------------

class TestCheckAuth:
    def test_success_does_not_raise(self):
        c = _client()
        resp = _mock_response(200, {"Cisco-IOS-XE-native:hostname": "WLC-1"})
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            c.check_auth()

    def test_401_propagates(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(401)):
            with pytest.raises(WLCAuthError):
                c.check_auth()


# ---------------------------------------------------------------------------
# get_hostname
# ---------------------------------------------------------------------------

class TestGetHostname:
    def test_returns_hostname(self):
        c = _client()
        resp = _mock_response(200, {"Cisco-IOS-XE-native:hostname": "MY-WLC"})
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            assert c.get_hostname() == "MY-WLC"

    def test_returns_none_on_connection_error(self):
        c = _client()
        with patch.object(c._session, "get",
                          side_effect=requests.exceptions.ConnectionError()):
            assert c.get_hostname() is None


# ---------------------------------------------------------------------------
# get_ap_data
# ---------------------------------------------------------------------------

def _capwap_entry(name: str = "AP-01", state: str = "ap-state-registered") -> dict:
    return {
        "wtp-mac": "aa:bb:cc:dd:ee:ff",
        "name": name,
        "ip-addr": "10.0.0.1",
        "ap-model": "C9115AXI",
        "location": "Floor 1",
        "ap-oper-data": {"ap-state": state},
        "wtp-oper-data": {"ap-model": "C9115AXI"},
    }


class TestGetApData:
    def test_returns_ap_records(self):
        c = _client()
        body = {"Cisco-IOS-XE-wireless-access-point-oper:capwap-data": [_capwap_entry()]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            records = c.get_ap_data()
        assert len(records) == 1
        assert records[0].name == "AP-01"
        assert records[0].state == "joined"

    def test_empty_response_returns_empty_list(self):
        c = _client()
        resp = _mock_response(204, content=b"")
        with patch.object(c._session, "get", return_value=resp):
            records = c.get_ap_data()
        assert records == []

    def test_auth_error_propagates(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(401)):
            with pytest.raises(WLCAuthError):
                c.get_ap_data()

    def test_connection_error_propagates(self):
        c = _client()
        with patch.object(c._session, "get",
                          side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(WLCConnectionError):
                c.get_ap_data()

    def test_status_callback_called(self):
        c = _client()
        body = {"Cisco-IOS-XE-wireless-access-point-oper:capwap-data": [_capwap_entry()]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        messages = []
        with patch.object(c._session, "get", return_value=resp):
            c.get_ap_data(status_cb=messages.append)
        assert len(messages) >= 2

    def test_wlc_error_propagates(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(404)):
            with pytest.raises(WLCDataError):
                c.get_ap_data()


# ---------------------------------------------------------------------------
# _parse_ap — various state formats
# ---------------------------------------------------------------------------

class TestParseAp:
    def _get_records(self, entry: dict) -> list:
        c = _client()
        body = {"Cisco-IOS-XE-wireless-access-point-oper:capwap-data": [entry]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            return c.get_ap_data()

    def test_dict_state_unwrapped(self):
        entry = _capwap_entry()
        entry["ap-oper-data"]["ap-state"] = {"ap-state": "ap-state-registered"}
        records = self._get_records(entry)
        assert records[0].state == "joined"

    def test_ip_addr_as_non_string(self):
        entry = _capwap_entry()
        entry["ip-addr"] = 167772161
        records = self._get_records(entry)
        assert records[0].ip_addr == "167772161"

    def test_tags_from_tag_info(self):
        entry = _capwap_entry()
        entry["tag-info"] = {
            "policy-tag-info": {"policy-tag-name": "PT1"},
            "site-tag": {"site-tag-name": "ST1"},
            "rf-tag": {"rf-tag-name": "RF1"},
        }
        records = self._get_records(entry)
        r = records[0]
        assert r.policy_tag == "PT1"
        assert r.site_tag == "ST1"
        assert r.rf_tag == "RF1"

    def test_tags_from_resolved_fallback(self):
        entry = _capwap_entry()
        entry["tag-info"] = {
            "resolved-tag-info": {
                "resolved-policy-tag": "RPT",
                "resolved-site-tag": "RST",
                "resolved-rf-tag": "RRF",
            }
        }
        records = self._get_records(entry)
        r = records[0]
        assert r.policy_tag == "RPT"
        assert r.site_tag == "RST"
        assert r.rf_tag == "RRF"

    def test_ap_operation_state_fallback(self):
        entry = _capwap_entry()
        del entry["ap-oper-data"]["ap-state"]
        entry["ap-oper-data"]["ap-operation-state"] = "not-registered"
        records = self._get_records(entry)
        assert records[0].state == "not_joined"


# ---------------------------------------------------------------------------
# get_ap_tags
# ---------------------------------------------------------------------------

class TestGetApTags:
    def test_returns_tag_dict(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-access-point-oper:ap-tag": [
                {"wtp-mac": "aa:bb:cc:dd:ee:ff",
                 "policy-tag": "PT", "site-tag": "ST", "rf-tag": "RF"}
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            tags = c.get_ap_tags()
        assert "aabbccddeeff" in tags
        assert tags["aabbccddeeff"] == ("PT", "ST", "RF")

    def test_nested_container_fallback(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-access-point-oper:access-point-oper-data": {
                "ap-tag": [
                    {"wtp-mac": "aa:bb:cc:dd:ee:ff",
                     "policy-tag": "PT2", "site-tag": "ST2", "rf-tag": "RF2"}
                ]
            }
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            tags = c.get_ap_tags()
        assert "aabbccddeeff" in tags

    def test_empty_on_204(self):
        c = _client()
        resp = _mock_response(204, content=b"")
        with patch.object(c._session, "get", return_value=resp):
            tags = c.get_ap_tags()
        assert tags == {}

    def test_skips_entries_without_mac(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-access-point-oper:ap-tag": [
                {"policy-tag": "PT"}
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            tags = c.get_ap_tags()
        assert tags == {}

    def test_wlc_error_returns_empty(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(404)):
            tags = c.get_ap_tags()
        assert tags == {}

    def test_unknown_top_level_keys_logged(self):
        c = _client()
        body = {"unknown-key": []}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        messages = []
        with patch.object(c._session, "get", return_value=resp):
            tags = c.get_ap_tags(status_cb=messages.append)
        assert tags == {}


# ---------------------------------------------------------------------------
# get_wlans
# ---------------------------------------------------------------------------

class TestGetWlans:
    def test_returns_wlan_records(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-entry": [
                {
                    "wlan-id": 1, "profile-name": "PROF-1",
                    "apf-vap-id-data": {"ssid": "Corp", "wlan-status": True},
                }
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            wlans = c.get_wlans()
        assert len(wlans) == 1
        assert wlans[0].ssid == "Corp"
        assert wlans[0].state == "up"

    def test_wlan_status_false_is_down(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-entry": [
                {
                    "wlan-id": 2, "profile-name": "PROF-2",
                    "apf-vap-id-data": {"ssid": "Guest", "wlan-status": False},
                }
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            wlans = c.get_wlans()
        assert wlans[0].state == "down"

    def test_wlan_status_none_defaults_up(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-entry": [
                {"wlan-id": 3, "profile-name": "P3",
                 "apf-vap-id-data": {"ssid": "Test"}}
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            wlans = c.get_wlans()
        assert wlans[0].state == "up"

    def test_wlan_status_string_enabled(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-entry": [
                {"wlan-id": 4, "profile-name": "P4",
                 "apf-vap-id-data": {"ssid": "S4", "wlan-status": "enabled"}}
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            wlans = c.get_wlans()
        assert wlans[0].state == "up"

    def test_wlan_status_string_down(self):
        c = _client()
        body = {
            "Cisco-IOS-XE-wireless-wlan-cfg:wlan-cfg-entry": [
                {"wlan-id": 5, "profile-name": "P5",
                 "apf-vap-id-data": {"ssid": "S5", "wlan-status": "disabled"}}
            ]
        }
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            wlans = c.get_wlans()
        assert wlans[0].state == "down"

    def test_error_returns_empty(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(404)):
            wlans = c.get_wlans()
        assert wlans == []


# ---------------------------------------------------------------------------
# get_clients
# ---------------------------------------------------------------------------

class TestGetClients:
    def _entry(self, mac: str = "aa:bb:cc:dd:ee:ff", ipv4: str = "10.0.0.1") -> dict:
        return {
            "client-mac": mac, "ap-name": "AP-01",
            "ssid": "Corp", "ipv4-addr": ipv4,
            "ms-assoc-state": "ms-run",
            "username": "user1",
        }

    def test_returns_client_records(self):
        c = _client()
        body = {"Cisco-IOS-XE-wireless-client-oper:common-oper-data": [self._entry()]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            clients = c.get_clients()
        assert len(clients) == 1
        assert clients[0].ipv4 == "10.0.0.1"
        assert clients[0].state == "run"

    def test_ipv4_0000_cleared(self):
        c = _client()
        body = {"Cisco-IOS-XE-wireless-client-oper:common-oper-data": [self._entry(ipv4="0.0.0.0")]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            clients = c.get_clients()
        assert clients[0].ipv4 == ""

    def test_nested_ipv4_dict(self):
        c = _client()
        entry = self._entry()
        entry["ipv4-addr"] = {"value": "192.168.1.100"}
        body = {"Cisco-IOS-XE-wireless-client-oper:common-oper-data": [entry]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            clients = c.get_clients()
        assert clients[0].ipv4 == "192.168.1.100"

    def test_ipv6_as_list(self):
        c = _client()
        entry = self._entry()
        entry["ipv6-addr"] = ["2001:db8::1", "2001:db8::2"]
        body = {"Cisco-IOS-XE-wireless-client-oper:common-oper-data": [entry]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            clients = c.get_clients()
        assert clients[0].ipv6 == "2001:db8::1"

    def test_ipv6_double_colon_cleared(self):
        c = _client()
        entry = self._entry()
        entry["ipv6-addr"] = "::"
        body = {"Cisco-IOS-XE-wireless-client-oper:common-oper-data": [entry]}
        resp = _mock_response(200, body)
        resp.raise_for_status = MagicMock()
        with patch.object(c._session, "get", return_value=resp):
            clients = c.get_clients()
        assert clients[0].ipv6 == ""

    def test_error_returns_empty(self):
        c = _client()
        with patch.object(c._session, "get", return_value=_mock_response(404)):
            clients = c.get_clients()
        assert clients == []


# ---------------------------------------------------------------------------
# _safe_list
# ---------------------------------------------------------------------------

class TestSafeList:
    def test_204_returns_empty(self):
        resp = _mock_response(204, content=b"")
        assert WLCClient._safe_list(resp) == []

    def test_empty_content_returns_empty(self):
        resp = _mock_response(200, content=b"   ")
        assert WLCClient._safe_list(resp) == []

    def test_invalid_json_returns_empty(self):
        resp = _mock_response(200, content=b"not-json")
        resp.json.side_effect = ValueError("no JSON")
        assert WLCClient._safe_list(resp) == []

    def test_key_not_found_returns_empty(self):
        resp = _mock_response(200, {"other-key": []})
        assert WLCClient._safe_list(resp) == []

    def test_value_not_list_returns_empty(self):
        resp = _mock_response(200,
                              {"Cisco-IOS-XE-wireless-access-point-oper:capwap-data": {}})
        resp.raise_for_status = MagicMock()
        assert WLCClient._safe_list(resp) == []

    def test_returns_list_from_first_matching_key(self):
        items = [{"name": "AP-01"}]
        resp = _mock_response(200,
                              {"Cisco-IOS-XE-wireless-access-point-oper:capwap-data": items})
        resp.raise_for_status = MagicMock()
        assert WLCClient._safe_list(resp) == items

    def test_fallback_key_used(self):
        items = [{"name": "AP-02"}]
        resp = _mock_response(200, {"capwap-data": items})
        resp.raise_for_status = MagicMock()
        assert WLCClient._safe_list(resp) == items


# ---------------------------------------------------------------------------
# _norm_mac
# ---------------------------------------------------------------------------

class TestNormMac:
    def test_colon_separated(self):
        assert WLCClient._norm_mac("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"

    def test_dash_separated(self):
        assert WLCClient._norm_mac("AA-BB-CC-DD-EE-FF") == "aabbccddeeff"

    def test_dot_separated(self):
        assert WLCClient._norm_mac("AABB.CCDD.EEFF") == "aabbccddeeff"

    def test_already_plain(self):
        assert WLCClient._norm_mac("aabbccddeeff") == "aabbccddeeff"
