"""Configuration for the Netdata data source and the multi-server ("fleet") views.

ProxMenux fork addition (GPL-3.0). Not part of upstream ProxMenux.

The file holds secrets (PVE API token secrets), so it must be mode 0600 and is
never committed. Location: $PROXMENUX_FLEET_CONFIG, default
~/.config/proxmenux-monitor/fleet.json (same directory as auth.json).

Example:
{
  "data_source": "netdata",
  "refresh_seconds": 2,
  "netdata": {"parent_url": "http://10.0.0.20:19999", "timeout": 3},
  "pve": [
    {"name": "srv-01", "url": "https://10.0.0.11:8006",
     "token_id": "proxmenux@pve!monitor", "token_secret": "xxxxxxxx-...",
     "fingerprint": "AA:BB:...:FF"}
  ]
}

DATA_SOURCE (env) overrides "data_source". Valid values: netdata, native.
"""

import json
import os
import re
import stat
import threading
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "proxmenux-monitor" / "fleet.json"
VALID_SOURCES = ("netdata", "native")
DEFAULT_SOURCE = "netdata"

_lock = threading.Lock()
_cache = {"mtime": None, "path": None, "data": None, "error": None}


class FleetConfigError(Exception):
    """Configuration is missing or invalid. The message is shown to the user."""


def config_path():
    return Path(os.environ.get("PROXMENUX_FLEET_CONFIG", str(DEFAULT_PATH)))


def _validate(raw):
    if not isinstance(raw, dict):
        raise FleetConfigError("fleet.json must contain a JSON object")
    cfg = {
        "data_source": str(raw.get("data_source", DEFAULT_SOURCE)).strip().lower(),
        "refresh_seconds": raw.get("refresh_seconds", 2),
        "netdata": {},
        "pve": [],
    }
    try:
        refresh = float(cfg["refresh_seconds"])
    except (TypeError, ValueError):
        raise FleetConfigError("refresh_seconds must be a number")
    cfg["refresh_seconds"] = min(max(refresh, 1.0), 5.0)

    nd = raw.get("netdata") or {}
    if not isinstance(nd, dict):
        raise FleetConfigError("'netdata' must be an object")
    url = str(nd.get("parent_url", "")).strip().rstrip("/")
    if url and not re.match(r"^https?://[A-Za-z0-9.\-\[\]:]+$", url):
        raise FleetConfigError("netdata.parent_url must look like http://HOST:19999 (no path)")
    try:
        timeout = float(nd.get("timeout", 3))
    except (TypeError, ValueError):
        raise FleetConfigError("netdata.timeout must be a number")
    cfg["netdata"] = {
        "parent_url": url,
        "timeout": min(max(timeout, 0.5), 30.0),
        "ca_file": nd.get("ca_file") or None,
    }

    pve = raw.get("pve") or []
    if not isinstance(pve, list):
        raise FleetConfigError("'pve' must be a list")
    seen = set()
    for i, ep in enumerate(pve):
        if not isinstance(ep, dict):
            raise FleetConfigError(f"pve[{i}] must be an object")
        name = str(ep.get("name", "")).strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$", name):
            raise FleetConfigError(f"pve[{i}].name is invalid")
        if name in seen:
            raise FleetConfigError(f"pve name '{name}' is duplicated")
        seen.add(name)
        ep_url = str(ep.get("url", "")).strip().rstrip("/")
        if not re.match(r"^https://[A-Za-z0-9.\-\[\]:]+$", ep_url):
            raise FleetConfigError(f"pve[{i}].url must look like https://HOST:8006")
        token_id = str(ep.get("token_id", "")).strip()
        if not re.match(r"^[^@\s]+@[^!\s]+![A-Za-z0-9._-]+$", token_id):
            raise FleetConfigError(f"pve[{i}].token_id must look like user@realm!tokenname")
        secret = str(ep.get("token_secret", "")).strip()
        if not secret:
            raise FleetConfigError(f"pve[{i}].token_secret is empty")
        fp = str(ep.get("fingerprint", "")).strip().upper().replace(" ", "")
        if fp and not re.match(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$", fp):
            raise FleetConfigError(f"pve[{i}].fingerprint must be a SHA-256 fingerprint AA:BB:..")
        cfg["pve"].append({"name": name, "url": ep_url, "token_id": token_id,
                           "token_secret": secret, "fingerprint": fp or None})

    env_source = os.environ.get("DATA_SOURCE")
    if env_source:
        cfg["data_source"] = env_source.strip().lower()
    if cfg["data_source"] not in VALID_SOURCES:
        raise FleetConfigError(
            f"data source '{cfg['data_source']}' is invalid (use one of: {', '.join(VALID_SOURCES)})")
    return cfg


def load():
    """Return the validated config. Raises FleetConfigError with a clear message.

    A missing file is not an error by itself: the data source still defaults to
    netdata, and callers report "Netdata Parent is not configured".
    """
    path = config_path()
    with _lock:
        try:
            st = path.stat()
        except FileNotFoundError:
            return _validate({})
        if _cache["path"] == str(path) and _cache["mtime"] == st.st_mtime_ns:
            if _cache["error"]:
                raise FleetConfigError(_cache["error"])
            return _cache["data"]
        try:
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise FleetConfigError(f"{path} must not be readable by group/others (chmod 600)")
            with open(path, "r", encoding="utf-8") as fh:
                data = _validate(json.load(fh))
            _cache.update(mtime=st.st_mtime_ns, path=str(path), data=data, error=None)
            return data
        except json.JSONDecodeError as exc:
            msg = f"{path} is not valid JSON: {exc}"
        except FleetConfigError as exc:
            msg = str(exc)
        _cache.update(mtime=st.st_mtime_ns, path=str(path), data=None, error=msg)
        raise FleetConfigError(msg)


def data_source():
    """'netdata' or 'native'. Invalid config falls back to the default only for this
    question; the fleet endpoints still report the configuration error."""
    try:
        return load()["data_source"]
    except FleetConfigError:
        env = (os.environ.get("DATA_SOURCE") or DEFAULT_SOURCE).strip().lower()
        return env if env in VALID_SOURCES else DEFAULT_SOURCE


def public_view(cfg):
    """Config without secrets, safe to return to the browser."""
    return {
        "data_source": cfg["data_source"],
        "refresh_seconds": cfg["refresh_seconds"],
        "netdata": {"parent_url": cfg["netdata"]["parent_url"],
                    "configured": bool(cfg["netdata"]["parent_url"])},
        "pve": [{"name": ep["name"], "url": ep["url"], "token_id": ep["token_id"],
                 "pinned": bool(ep["fingerprint"])} for ep in cfg["pve"]],
    }
