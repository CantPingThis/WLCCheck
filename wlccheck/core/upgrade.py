from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from .restconf import StatusCallback, WLCAuthError, WLCClient, WLCConnectionError, WLCDataError
from .upgrade_models import (
    APImageStatus,
    GlobalPredownloadStats,
    PredownloadPoll,
    SiteTagProgress,
    normalize_predownload_state,
)

# ---------------------------------------------------------------------------
# RESTCONF paths
# ---------------------------------------------------------------------------

_AP_IMG_STATS_PATH = (
    "Cisco-IOS-XE-wireless-ap-global-oper:"
    "ap-global-oper-data/ap-img-predownload-stats"
)
_CAPWAP_PATH = (
    "Cisco-IOS-XE-wireless-access-point-oper:"
    "access-point-oper-data/capwap-data"
)

# SSH command confirmed on IOS-XE 17.15
_PREDOWNLOAD_CMD = "ap image predownload site-tag {tag} start"


# ---------------------------------------------------------------------------
# SSH output parser
# ---------------------------------------------------------------------------

def _parse_show_ap_image(output: str, wlc_name: str) -> List[APImageStatus]:
    """Parse 'show ap image' fixed-width CLI table into APImageStatus list."""
    lines = output.splitlines()

    header_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if "AP Name" in line and "Primary Image" in line:
            header_idx = i
            break
    if header_idx is None:
        return []

    header = lines[header_idx]

    def _col(label: str) -> int:
        return header.index(label)

    try:
        cols = {
            "name":    _col("AP Name"),
            "primary": _col("Primary Image"),
            "backup":  _col("Backup Image"),
            "state":   _col("Predownload Status"),
            "version": _col("Predownload Version"),
            "retry":   _col("Retry Count"),
            "method":  _col("Method"),
        }
    except ValueError:
        return []

    statuses: List[APImageStatus] = []
    for line in lines[header_idx + 2:]:   # skip header + dashes line
        if not line.strip():
            continue
        try:
            name    = line[cols["name"]   : cols["primary"]].strip()
            primary = line[cols["primary"]: cols["backup"]].strip()
            backup  = line[cols["backup"] : cols["state"]].strip()
            state_r = line[cols["state"]  : cols["version"]].strip()
            version = line[cols["version"]: cols["retry"]].strip()
            method  = line[cols["method"] :].strip()
            if not name:
                continue
            statuses.append(APImageStatus(
                name=name,
                wtp_mac="",
                wlc_name=wlc_name,
                site_tag="",           # filled by caller from site_tag_map
                current_version=primary,
                backup_version=backup,
                predownload_state=normalize_predownload_state(state_r),
                predownload_version=version if version != "0.0.0.0" else "",
                method=method,
            ))
        except (ValueError, IndexError):
            continue
    return statuses


def _is_cisco_error(output: str) -> bool:
    return bool(re.match(r"^%", output)) or "Invalid input" in output or "Incomplete command" in output


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class WLCUpgradeClient:
    """
    Orchestrates EIU pre-download for a single Cisco 9800 WLC.

    Trigger   : SSH (scrapli sync)  — ap image predownload site-tag <name> start
    Monitoring: RESTCONF (requests) — ap-img-predownload-stats + capwap-data
    Per-AP    : SSH (scrapli sync)  — show ap image
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        wlc_name: str = "",
        verify_ssl: bool = False,
        restconf_timeout: int = 60,
        enable_pass: str = "",
    ) -> None:
        self.host     = host.strip()
        self.wlc_name = wlc_name or host
        self._user    = username
        self._pass    = password
        self._enable  = enable_pass or password
        self._rc      = WLCClient(host, username, password, verify_ssl, restconf_timeout)

    # ------------------------------------------------------------------
    # RESTCONF — global stats
    # ------------------------------------------------------------------

    def poll_global_stats(self) -> GlobalPredownloadStats:
        """GET ap-img-predownload-stats — aggregate counts across all APs."""
        empty = GlobalPredownloadStats(wlc_name=self.wlc_name)
        try:
            resp = self._rc._get(_AP_IMG_STATS_PATH, timeout=30)
        except WLCDataError:
            return empty
        except (WLCConnectionError, WLCAuthError):
            raise

        if resp.status_code == 204 or not resp.content.strip():
            return empty
        try:
            body = resp.json()
        except ValueError:
            return empty

        container = (
            body.get("Cisco-IOS-XE-wireless-ap-global-oper:ap-img-predownload-stats")
            or body.get("ap-img-predownload-stats")
            or body
        )
        blk = container.get("predownload-stats", container) if isinstance(container, dict) else {}

        return GlobalPredownloadStats(
            wlc_name=self.wlc_name,
            num_initiated=int(blk.get("num-initiated", 0)),
            num_in_progress=int(blk.get("num-in-progress", 0)),
            num_complete=int(blk.get("num-complete", 0)),
            num_failed=int(blk.get("num-failed", 0)),
            is_active=bool(blk.get("is-predownload-in-progress", False)),
        )

    # ------------------------------------------------------------------
    # RESTCONF — per-AP download percentage
    # ------------------------------------------------------------------

    def poll_ap_percentages(self) -> dict[str, int]:
        """Return {ap_name: img_pct} from capwap-data (0 when AP is idle)."""
        try:
            resp = self._rc._get(_CAPWAP_PATH, timeout=60)
        except (WLCConnectionError, WLCAuthError, WLCDataError):
            return {}

        if resp.status_code == 204 or not resp.content.strip():
            return {}
        try:
            body = resp.json()
        except ValueError:
            return {}

        raw = (
            body.get("Cisco-IOS-XE-wireless-access-point-oper:capwap-data")
            or body.get("capwap-data")
            or []
        )
        if not isinstance(raw, list):
            return {}
        return {
            ap["name"]: int(ap.get("image-size-percentage", 0))
            for ap in raw
            if ap.get("name")
        }

    # ------------------------------------------------------------------
    # SSH — per-AP state via show ap image
    # ------------------------------------------------------------------

    def poll_ap_image_statuses(
        self,
        site_tag_map: dict[str, str],
        status_cb: Optional[StatusCallback] = None,
    ) -> List[APImageStatus]:
        """
        SSH 'show ap image' and return APImageStatus list.
        site_tag_map: {ap_name: site_tag} to populate the site_tag field.
        """
        conn = self._open_ssh(status_cb)
        try:
            self._notify(status_cb, "SSH: show ap image…")
            r        = conn.send_command("show ap image", timeout_ops=30)
            statuses = _parse_show_ap_image(r.result, self.wlc_name)
            for s in statuses:
                s.site_tag = site_tag_map.get(s.name, "")
            self._notify(status_cb, f"[green]✓[/green] {len(statuses)} APs from show ap image.")
            return statuses
        except Exception as exc:
            raise WLCConnectionError(f"SSH show ap image failed: {exc}") from exc
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # SSH — trigger pre-download
    # ------------------------------------------------------------------

    def trigger_predownload(
        self,
        site_tags: List[str],
        status_cb: Optional[StatusCallback] = None,
    ) -> None:
        """
        Open one SSH session, probe command syntax, then trigger pre-download
        for every site-tag. Raises WLCConnectionError on failure.
        """
        conn = self._open_ssh(status_cb)
        try:
            for tag in site_tags:
                cmd = _PREDOWNLOAD_CMD.format(tag=tag)
                self._notify(status_cb, f"SSH: {cmd}")
                r   = conn.send_command(cmd, timeout_ops=60)
                out = r.result.strip()
                if out:
                    self._notify(status_cb, f"  WLC: {out}")
                if _is_cisco_error(out):
                    self._notify(status_cb, f"[red]✕[/red] Error on '{tag}': {out}")
                else:
                    self._notify(status_cb, f"[green]✓[/green] Pre-download triggered on '{tag}'")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Combined poll
    # ------------------------------------------------------------------

    def poll(
        self,
        poll_num: int,
        site_tags: List[str],
        site_tag_map: dict[str, str],
        status_cb: Optional[StatusCallback] = None,
    ) -> PredownloadPoll:
        """
        One monitoring cycle combining:
        - Global stats (RESTCONF, fast)
        - Per-AP percentages (RESTCONF, fast)
        - Per-AP state (SSH show ap image, accurate)
        """
        global_stats = self.poll_global_stats()
        ap_pcts      = self.poll_ap_percentages()
        ap_statuses  = self.poll_ap_image_statuses(site_tag_map, status_cb)

        for s in ap_statuses:
            s.img_pct = ap_pcts.get(s.name, 0)

        stp_map: dict[str, SiteTagProgress] = {
            tag: SiteTagProgress(site_tag=tag, wlc_name=self.wlc_name)
            for tag in site_tags
        }
        for s in ap_statuses:
            key = s.site_tag or "unknown"
            if key not in stp_map:
                stp_map[key] = SiteTagProgress(site_tag=key, wlc_name=self.wlc_name)
            stp_map[key].ap_statuses.append(s)

        return PredownloadPoll(
            poll_num=poll_num,
            timestamp=datetime.now(),
            wlc_name=self.wlc_name,
            global_stats=global_stats,
            site_tag_progresses=list(stp_map.values()),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _open_ssh(self, status_cb: Optional[StatusCallback] = None):
        try:
            from scrapli import Scrapli  # type: ignore[import-untyped]
        except ImportError as exc:
            raise WLCConnectionError(
                "scrapli not installed — run: uv add 'scrapli[paramiko]'"
            ) from exc

        self._notify(status_cb, f"SSH: connecting to {self.host}…")
        try:
            conn = Scrapli(
                host=self.host,
                auth_username=self._user,
                auth_password=self._pass,
                auth_secondary=self._enable,
                auth_strict_key=False,
                platform="cisco_iosxe",
                transport="paramiko",
                timeout_socket=10,
                timeout_transport=30,
                timeout_ops=60,
            )
            conn.open()
        except Exception as exc:
            raise WLCConnectionError(f"SSH connection failed: {exc}") from exc

        self._notify(status_cb, f"[green]✓[/green] SSH connected to {self.host}")
        return conn

    @staticmethod
    def _notify(cb: Optional[StatusCallback], msg: str) -> None:
        if cb:
            cb(msg)
