"""Read-only Proxmox VE API client used for the multi-server views.

ProxMenux fork addition (GPL-3.0). Not part of upstream ProxMenux.

Uses an API token with role PVEAuditor (see netdata-deploy/pve-readonly-token.sh).
Only GET requests are issued. TLS: if a SHA-256 fingerprint is configured, the
server certificate must match it exactly (PVE uses a self-signed certificate by
default); without a fingerprint the system trust store is used.
"""

import hashlib
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.parse


class PveUnavailable(Exception):
    pass


class PveClient:
    def __init__(self, name, url, token_id, token_secret, fingerprint=None, timeout=4.0, ttl=30.0):
        self.name = name
        parsed = urllib.parse.urlsplit(url)
        self.host = parsed.hostname
        self.port = parsed.port or 8006
        self.token_header = f"PVEAPIToken={token_id}={token_secret}"
        self.fingerprint = fingerprint.replace(":", "").lower() if fingerprint else None
        self.timeout = timeout
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache = {}

    def _context(self):
        if self.fingerprint:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # replaced by the exact fingerprint check below
            return ctx
        return ssl.create_default_context()

    def get(self, path):
        """GET /api2/json<path> and return the 'data' member. Cached for ttl seconds."""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(path)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                           context=self._context())
        try:
            try:
                conn.connect()
                if self.fingerprint:
                    der = conn.sock.getpeercert(binary_form=True)
                    if hashlib.sha256(der).hexdigest() != self.fingerprint:
                        raise PveUnavailable(
                            f"{self.name}: TLS certificate fingerprint does not match the pinned value")
                conn.request("GET", "/api2/json" + path,
                             headers={"Authorization": self.token_header,
                                      "Accept": "application/json"})
                resp = conn.getresponse()
                body = resp.read()
            except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as exc:
                raise PveUnavailable(f"{self.name}: cannot reach PVE API ({exc})")
            if resp.status == 401:
                raise PveUnavailable(f"{self.name}: PVE API token rejected (401)")
            if resp.status == 403:
                raise PveUnavailable(f"{self.name}: PVE API token lacks permission (403); role PVEAuditor on / is required")
            if resp.status != 200:
                raise PveUnavailable(f"{self.name}: PVE API returned HTTP {resp.status} for {path}")
            try:
                data = json.loads(body).get("data")
            except (ValueError, AttributeError):
                raise PveUnavailable(f"{self.name}: invalid JSON from PVE API")
        finally:
            conn.close()
        with self._lock:
            self._cache[path] = (time.monotonic(), data)
        return data

    def guests(self):
        """Running and stopped guests of this server (standalone node)."""
        return self.get("/cluster/resources?type=vm") or []

    def guest_counts(self):
        counts = {"qemu": {"running": 0, "total": 0}, "lxc": {"running": 0, "total": 0}}
        for g in self.guests():
            kind = "lxc" if g.get("type") == "lxc" else "qemu"
            counts[kind]["total"] += 1
            if g.get("status") == "running":
                counts[kind]["running"] += 1
        return counts
