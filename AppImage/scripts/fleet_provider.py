"""Multi-server ("fleet") data built from the Netdata Parent and read-only PVE APIs.

ProxMenux fork addition (GPL-3.0). Not part of upstream ProxMenux.

Units returned to the UI are plain SI units (bytes, bytes/s, bits/s, seconds,
percent) so the frontend never has to know Netdata's chart units.

"Real usage" definitions (TZ 6.6), server level:
  cpu.percent     system.cpu, sum of all non-idle dimensions (user, system, iowait,
                  irq, softirq, steal, guest, nice) as Netdata reports them
  memory.used     system.ram "used" (page cache and buffers excluded)
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import fleet_config
from netdata_client import NetdataClient, NetdataUnavailable, parse_grouped, parse_series
from pve_client import PveClient, PveUnavailable

MIB = 1024 * 1024
GIB = 1024 * MIB

# metric -> (context, group_by)
_SNAPSHOT_QUERIES = {
    "cpu": ("system.cpu", "node"),
    "ram": ("system.ram", "node,dimension"),
    "net": ("system.net", "node,dimension"),
    "io": ("system.io", "node,dimension"),
    "uptime": ("system.uptime", "node"),
    "load": ("system.load", "node,dimension"),
    "disk": ("disk.space", "node,instance,dimension"),
}

# history metric -> (context, unit conversion factor, display units)
HISTORY_METRICS = {
    "cpu": ("system.cpu", 1.0, "%"),
    "ram": ("system.ram", MIB, "bytes"),
    "net": ("system.net", 1000.0, "bits/s"),
    "io": ("system.io", 1024.0, "bytes/s"),
    "load": ("system.load", 1.0, "load"),
}
HISTORY_RANGES = {"5m": 300, "1h": 3600, "24h": 86400, "7d": 7 * 86400}
HISTORY_POINTS = 300

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fleet")
_state_lock = threading.Lock()
_state = {"key": None, "client": None, "pve": {}}


class FleetError(Exception):
    """Error shown to the user as-is. status = HTTP status for the API route."""

    def __init__(self, message, code, status=503):
        super().__init__(message)
        self.code = code
        self.status = status


def _clients():
    try:
        cfg = fleet_config.load()
    except fleet_config.FleetConfigError as exc:
        raise FleetError(f"Fleet configuration error: {exc}", "config_invalid", 500)
    if cfg["data_source"] != "netdata":
        raise FleetError("Multi-server views need DATA_SOURCE=netdata", "data_source_native", 409)
    nd = cfg["netdata"]
    if not nd["parent_url"]:
        raise FleetError(
            f"Netdata Parent is not configured (set netdata.parent_url in {fleet_config.config_path()})",
            "netdata_not_configured", 503)
    key = (nd["parent_url"], nd["timeout"], nd["ca_file"],
           tuple((p["name"], p["url"], p["token_id"], p["token_secret"], p["fingerprint"]) for p in cfg["pve"]))
    with _state_lock:
        if _state["key"] != key:
            _state["client"] = NetdataClient(nd["parent_url"], timeout=nd["timeout"],
                                             ca_file=nd["ca_file"], ttl=min(1.0, cfg["refresh_seconds"] / 2))
            _state["pve"] = {p["name"]: PveClient(p["name"], p["url"], p["token_id"], p["token_secret"],
                                                  p["fingerprint"]) for p in cfg["pve"]}
            _state["key"] = key
        return cfg, _state["client"], _state["pve"]


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _server_labels(node):
    labels = node.get("labels") or {}
    return {k: v for k, v in labels.items() if not k.startswith("_")}


def _is_server(node):
    return _server_labels(node).get("proxmenux_role") == "host"


def _last_seen(client, node_id):
    """Last stored timestamp of an offline node (one query, only for stale nodes)."""
    try:
        d = client.data("system.cpu", group_by="node", after=-7 * 86400, points=1,
                        scope_nodes=node_id, ttl=30)
        return int(d.get("db", {}).get("last_entry") or 0) or None
    except NetdataUnavailable:
        return None


def _guest_counts(pve):
    try:
        return {"ok": True, **pve.guest_counts()}
    except PveUnavailable as exc:
        return {"ok": False, "error": str(exc)}


def servers_snapshot():
    cfg, client, pve_clients = _clients()

    # Nodes first: without them nothing else is meaningful.
    nodes = client.nodes()
    futures = {m: _pool.submit(client.data, ctx, group_by=gb, after=-5, points=1)
               for m, (ctx, gb) in _SNAPSHOT_QUERIES.items()}
    metrics, errors = {}, []
    for m, fut in futures.items():
        try:
            metrics[m] = parse_grouped(fut.result())
        except NetdataUnavailable as exc:
            metrics[m] = {}
            errors.append(str(exc))

    servers = [n for n in nodes if _is_server(n)]
    pve_names = {_server_labels(n).get("pve_host") or n.get("nm") for n in servers}
    pve_futures = {name: _pool.submit(_guest_counts, c) for name, c in pve_clients.items() if name in pve_names}

    out = []
    for n in servers:
        mg = n.get("mg")
        labels = _server_labels(n)
        online = n.get("state") == "reachable" and mg in metrics["cpu"]
        hw = n.get("hw") or {}
        ram = metrics["ram"].get(mg, {})
        used_b = _num(ram.get("used"), 0) * MIB if ram else None
        total_b = sum(_num(ram.get(k), 0) for k in ("used", "cached", "buffers", "free")) * MIB if ram else None
        if not total_b:
            total_b = _num(hw.get("memory"))
        net = metrics["net"].get(mg, {})
        io = metrics["io"].get(mg, {})
        load = metrics["load"].get(mg, {})
        disks = []
        for key, val in metrics["disk"].get(mg, {}).items():
            dim, _, inst = key.partition("|")
            if dim != "used":
                continue
            avail = _num(metrics["disk"][mg].get(f"avail|{inst}"), 0)
            used = _num(val, 0)
            if used + avail <= 0:
                continue
            mount = inst[len("disk_space."):] if inst.startswith("disk_space.") else inst
            disks.append({"mount": mount, "used_bytes": used * GIB,
                          "total_bytes": (used + avail) * GIB,
                          "percent": round(used * 100.0 / (used + avail), 1)})
        disks.sort(key=lambda d: -d["percent"])
        alerts = ((n.get("health") or {}).get("alerts") or {})
        pve_name = labels.get("pve_host") or n.get("nm")
        guests = None
        if pve_name in pve_futures:
            guests = pve_futures[pve_name].result()

        out.append({
            "id": mg,
            "name": n.get("nm"),
            "pve_host": pve_name,
            "status": "online" if online else "offline",
            "last_seen": int(time.time()) if online else _last_seen(client, mg),
            "netdata_version": n.get("v"),
            "cpu": {"percent": round(_num(metrics["cpu"].get(mg, {}).get(""), 0), 1) if online else None,
                    "cores": int(_num(hw.get("cpus"), 0)) or None},
            "memory": {"used_bytes": used_b if online else None, "total_bytes": total_b,
                       "cached_bytes": (_num(ram.get("cached"), 0) + _num(ram.get("buffers"), 0)) * MIB if ram and online else None,
                       "percent": round(used_b * 100.0 / total_b, 1) if online and ram and total_b else None},
            # Netdata reports outbound traffic and writes as negative values.
            "network": {"rx_bps": _num(net.get("received"), 0) * 1000 if online else None,
                        "tx_bps": abs(_num(net.get("sent"), 0)) * 1000 if online else None},
            "disk_io": {"read_Bps": _num(io.get("reads"), 0) * 1024 if online else None,
                        "write_Bps": abs(_num(io.get("writes"), 0)) * 1024 if online else None},
            "uptime_seconds": int(_num(metrics["uptime"].get(mg, {}).get(""), 0)) if online else None,
            "load": [_num(load.get(k)) for k in ("load1", "load5", "load15")] if online else None,
            "disks": disks if online else [],
            "alerts": {"critical": int(alerts.get("critical") or 0), "warning": int(alerts.get("warning") or 0)},
            "guests": guests,
        })

    out.sort(key=lambda s: s["name"] or "")
    summary = {
        "total": len(out),
        "online": sum(1 for s in out if s["status"] == "online"),
        "offline": sum(1 for s in out if s["status"] == "offline"),
        "alerts": {"critical": sum(s["alerts"]["critical"] for s in out),
                   "warning": sum(s["alerts"]["warning"] for s in out)},
    }
    other_nodes = [n.get("nm") for n in nodes if not _is_server(n)]
    return {
        "source": "netdata",
        "parent": cfg["netdata"]["parent_url"],
        "generated_at": int(time.time()),
        "refresh_seconds": cfg["refresh_seconds"],
        "summary": summary,
        "servers": out,
        "other_nodes": other_nodes,
        "errors": errors,
    }


def server_history(node_id, metric, range_key):
    if metric not in HISTORY_METRICS:
        raise FleetError(f"Unknown metric '{metric}'", "bad_request", 400)
    if range_key not in HISTORY_RANGES:
        raise FleetError(f"Unknown range '{range_key}'", "bad_request", 400)
    _, client, _ = _clients()
    if not any(n.get("mg") == node_id for n in client.nodes()):
        raise FleetError("Unknown server", "not_found", 404)
    ctx, factor, units = HISTORY_METRICS[metric]
    seconds = HISTORY_RANGES[range_key]
    data = client.data(ctx, group_by="dimension", after=-seconds, points=HISTORY_POINTS,
                       scope_nodes=node_id, ttl=1 if seconds <= 300 else 15)
    series = parse_series(data)
    labels = series["labels"]
    if metric == "ram":
        keep = [i for i, name in enumerate(labels) if name in ("used", "cached", "buffers")]
    else:
        keep = list(range(len(labels)))
    result = []
    for i in keep:
        vals = [None if v is None else abs(v) * factor for v in series["series"][i]]
        result.append({"name": labels[i], "values": vals})
    return {"metric": metric, "range": range_key, "units": units,
            "times": series["times"], "series": result}
