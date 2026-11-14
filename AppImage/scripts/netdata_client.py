"""Small read-only client for the Netdata Parent API (v3).

ProxMenux fork addition (GPL-3.0). Not part of upstream ProxMenux.

* standard library only (the fork must build and run offline);
* every response is cached for a short TTL and concurrent identical requests
  are coalesced into one HTTP call ("single flight"), so many browser tabs
  polling every 1-5 s cost one request per metric per TTL;
* errors raise NetdataUnavailable with a message meant for the UI. There is no
  silent fallback to the native collectors.
"""

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class NetdataUnavailable(Exception):
    def __init__(self, message, url=None):
        super().__init__(message)
        self.url = url


class _Flight:
    __slots__ = ("event", "value", "error", "ts")

    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.error = None
        self.ts = 0.0


class NetdataClient:
    def __init__(self, base_url, timeout=3.0, ca_file=None, ttl=1.0, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache = {}     # key -> (ts, value)
        self._flights = {}   # key -> _Flight
        if opener is not None:
            self._open = opener
        else:
            ctx = None
            if self.base_url.startswith("https://"):
                ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
            self._ctx = ctx
            self._open = self._urlopen

    # ------------------------------------------------------------------ transport
    def _urlopen(self, url):
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "ProxMenux-Monitor"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
            return resp.read()

    def get(self, path, params=None, ttl=None):
        """GET path?params, JSON-decoded, cached for ttl seconds."""
        query = urllib.parse.urlencode(sorted((params or {}).items()), safe="*,|")
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        ttl = self.ttl if ttl is None else ttl
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(url)
            if hit and now - hit[0] < ttl:
                return hit[1]
            flight = self._flights.get(url)
            leader = flight is None
            if leader:
                flight = self._flights[url] = _Flight()
        if not leader:
            flight.event.wait(self.timeout + 1)
            if flight.error:
                raise flight.error
            if flight.event.is_set():
                return flight.value
            raise NetdataUnavailable(f"Netdata Parent did not answer within {self.timeout:g}s", url)
        try:
            try:
                raw = self._open(url)
            except urllib.error.HTTPError as exc:
                raise NetdataUnavailable(f"Netdata Parent returned HTTP {exc.code} for {path}", url)
            except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
                reason = getattr(exc, "reason", exc)
                raise NetdataUnavailable(f"Cannot reach Netdata Parent at {self.base_url}: {reason}", url)
            try:
                value = json.loads(raw)
            except ValueError:
                raise NetdataUnavailable(f"Netdata Parent returned invalid JSON for {path}", url)
            with self._lock:
                self._cache[url] = (time.monotonic(), value)
                if len(self._cache) > 512:
                    for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:128]:
                        self._cache.pop(k, None)
            flight.value = value
            return value
        except NetdataUnavailable as exc:
            flight.error = exc
            raise
        finally:
            flight.event.set()
            with self._lock:
                self._flights.pop(url, None)

    # ------------------------------------------------------------------ API helpers
    def nodes(self, ttl=None):
        return self.get("/api/v3/nodes", {"scope_nodes": "*"}, ttl=ttl).get("nodes", [])

    def data(self, contexts, group_by="node", after=-5, points=1, scope_nodes="*",
             ttl=None, extra=None):
        params = {"scope_nodes": scope_nodes, "contexts": contexts, "group_by": group_by,
                  "after": int(after), "points": int(points), "format": "json2"}
        if extra:
            params.update(extra)
        return self.get("/api/v3/data", params, ttl=ttl)


def parse_grouped(data):
    """Turn a json2 /api/v3/data response grouped by node[,instance][,dimension]
    into {node_id: {key: value}} using the latest row.

    Column ids are comma-separated group values in group_by order, the node
    machine GUID last (e.g. "used,disk_space./,<mg>"). Keys are the remaining
    parts joined by "|" ("used|disk_space./"), or "" when grouped by node only.
    Null values are skipped.
    """
    out = {}
    try:
        ids = data["view"]["dimensions"]["ids"]
        rows = data["result"]["data"]
    except (KeyError, TypeError):
        return out
    if not rows:
        return out
    row = rows[-1]
    for idx, col in enumerate(ids, start=1):
        if idx >= len(row):
            break
        cell = row[idx]
        value = cell[0] if isinstance(cell, list) else cell
        if value is None:
            continue
        parts = col.rsplit(",", 1)
        node = parts[-1]
        key = "|".join(parts[0].split(",", 1)) if len(parts) == 2 else ""
        out.setdefault(node, {})[key] = value
    return out


def parse_series(data):
    """json2 response -> {"labels": [...names], "times": [...], "series": [[...], ...]}"""
    try:
        names = data["view"]["dimensions"]["names"]
        rows = data["result"]["data"]
        units = data["view"].get("units")
    except (KeyError, TypeError):
        return {"labels": [], "times": [], "series": [], "units": None}
    times, series = [], [[] for _ in names]
    for row in rows:
        times.append(row[0])
        for i in range(len(names)):
            cell = row[i + 1] if i + 1 < len(row) else None
            series[i].append(cell[0] if isinstance(cell, list) else cell)
    return {"labels": names, "times": times, "series": series, "units": units}
