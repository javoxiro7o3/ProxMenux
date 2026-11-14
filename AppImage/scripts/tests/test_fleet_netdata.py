"""Netdata data source and multi-server views (ProxMenux fork).

Fixtures in fixtures/netdata/ are real responses of Netdata v2.12.3 (Parent +
one child labelled proxmenux_role=host), captured in a test container.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import fleet_config  # noqa: E402
import fleet_provider  # noqa: E402
import netdata_client  # noqa: E402
from netdata_client import NetdataClient, NetdataUnavailable, parse_grouped  # noqa: E402

FX = SCRIPTS / "tests" / "fixtures" / "netdata"
CHILD = "a45477ec-1b38-45f0-a2e6-e76c0b135193"


def fixture(name):
    return json.loads((FX / name).read_text())


def fake_opener(nodes_file="nodes_online.json", calls=None, fail=False):
    def opener(url):
        if calls is not None:
            calls.append(url)
        if fail:
            raise urllib.error.URLError("Connection refused")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "/api/v3/nodes" in url:
            return (FX / nodes_file).read_bytes()
        ctx = q["contexts"][0]
        if q.get("scope_nodes") == [CHILD]:
            # last-seen lookup for a stale node
            return json.dumps({"db": {"last_entry": 1794678686}, "view": {}, "result": {}}).encode()
        if nodes_file == "nodes_child_stale.json":
            # an offline node has no columns in live queries
            d = fixture(f"data_{ctx}.json")
            ids = d["view"]["dimensions"]["ids"]
            keep = [i for i, c in enumerate(ids) if not c.endswith(CHILD)]
            d["view"]["dimensions"]["ids"] = [ids[i] for i in keep]
            d["result"]["data"] = [[r[0]] + [r[i + 1] for i in keep] for r in d["result"]["data"]]
            return json.dumps(d).encode()
        return (FX / f"data_{ctx}.json").read_bytes()
    return opener


class ParseTests(unittest.TestCase):
    def test_grouped_by_node(self):
        cpu = parse_grouped(fixture("data_system.cpu.json"))
        self.assertIn(CHILD, cpu)
        self.assertIn("", cpu[CHILD])
        self.assertGreaterEqual(cpu[CHILD][""], 0)

    def test_grouped_by_node_dimension(self):
        ram = parse_grouped(fixture("data_system.ram.json"))
        self.assertEqual(set(ram[CHILD]), {"free", "used", "cached", "buffers"})

    def test_grouped_by_node_instance_dimension(self):
        disk = parse_grouped(fixture("data_disk.space.json"))
        self.assertIn("used|disk_space./", disk[CHILD])
        self.assertIn("avail|disk_space./", disk[CHILD])

    def test_series_oldest_first(self):
        # Netdata returns newest row first; charts need ascending time.
        d = {"view": {"dimensions": {"names": ["user"]}, "units": "percentage"},
             "result": {"data": [[30, [3, 0, 0]], [20, [2, 0, 0]], [10, [1, 0, 0]]]}}
        s = netdata_client.parse_series(d)
        self.assertEqual(s["times"], [10, 20, 30])
        self.assertEqual(s["series"][0], [1, 2, 3])

    def test_empty_or_broken(self):
        self.assertEqual(parse_grouped({}), {})
        self.assertEqual(parse_grouped({"view": {"dimensions": {"ids": ["x"]}}, "result": {"data": []}}), {})


class ClientTests(unittest.TestCase):
    def test_cache_and_single_flight(self):
        calls = []
        gate = threading.Event()

        def slow(url):
            calls.append(url)
            gate.wait(2)
            return b'{"nodes": []}'

        c = NetdataClient("http://parent:19999", ttl=5, opener=slow)
        results = []
        threads = [threading.Thread(target=lambda: results.append(c.nodes())) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        gate.set()
        for t in threads:
            t.join(3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [[]] * 8)
        c.nodes()
        self.assertEqual(len(calls), 1, "second call within TTL must hit the cache")

    def test_unreachable_is_explicit(self):
        c = NetdataClient("http://parent:19999", opener=fake_opener(fail=True))
        with self.assertRaises(NetdataUnavailable) as ctx:
            c.nodes()
        self.assertIn("Cannot reach Netdata Parent at http://parent:19999", str(ctx.exception))

    def test_invalid_json(self):
        c = NetdataClient("http://parent:19999", opener=lambda url: b"<html>")
        with self.assertRaises(NetdataUnavailable):
            c.nodes()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "fleet.json"
        self.env = patch.dict(os.environ, {"PROXMENUX_FLEET_CONFIG": str(self.path)}, clear=False)
        self.env.start()
        os.environ.pop("DATA_SOURCE", None)

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def write(self, data, mode=0o600):
        self.path.write_text(json.dumps(data))
        os.chmod(self.path, mode)
        # mtime granularity: make sure each write invalidates the cache
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + int(time.time_ns() % 1000) + 1))

    def test_missing_file_defaults_to_netdata(self):
        self.assertEqual(fleet_config.load()["data_source"], "netdata")
        self.assertEqual(fleet_config.load()["netdata"]["parent_url"], "")

    def test_world_readable_rejected(self):
        self.write({"netdata": {"parent_url": "http://10.0.0.20:19999"}}, mode=0o644)
        with self.assertRaises(fleet_config.FleetConfigError):
            fleet_config.load()

    def test_bad_url_rejected(self):
        self.write({"netdata": {"parent_url": "http://x/evil?y"}})
        with self.assertRaises(fleet_config.FleetConfigError):
            fleet_config.load()

    def test_env_overrides_source(self):
        self.write({"data_source": "netdata"})
        with patch.dict(os.environ, {"DATA_SOURCE": "native"}):
            self.write({"data_source": "netdata"})
            self.assertEqual(fleet_config.load()["data_source"], "native")

    def test_public_view_has_no_secrets(self):
        self.write({"netdata": {"parent_url": "http://10.0.0.20:19999"},
                    "pve": [{"name": "srv-01", "url": "https://10.0.0.11:8006",
                             "token_id": "proxmenux@pve!monitor", "token_secret": "s3cr3t"}]})
        view = json.dumps(fleet_config.public_view(fleet_config.load()))
        self.assertNotIn("s3cr3t", view)

    def test_native_source_blocks_fleet(self):
        self.write({"data_source": "native"})
        with self.assertRaises(fleet_provider.FleetError) as ctx:
            fleet_provider._clients()
        self.assertEqual(ctx.exception.status, 409)

    def test_parent_not_configured(self):
        self.write({"data_source": "netdata"})
        with self.assertRaises(fleet_provider.FleetError) as ctx:
            fleet_provider._clients()
        self.assertEqual(ctx.exception.code, "netdata_not_configured")


class SnapshotTests(unittest.TestCase):
    def run_snapshot(self, nodes_file):
        client = NetdataClient("http://parent:19999", opener=fake_opener(nodes_file))
        cfg = {"netdata": {"parent_url": "http://parent:19999"}, "refresh_seconds": 2}
        with patch.object(fleet_provider, "_clients", return_value=(cfg, client, {})):
            return fleet_provider.servers_snapshot()

    def test_online_server(self):
        snap = self.run_snapshot("nodes_online.json")
        self.assertEqual(snap["summary"]["total"], 1)        # only proxmenux_role=host
        self.assertEqual(snap["other_nodes"], ["nd-parent"])
        s = snap["servers"][0]
        self.assertEqual((s["name"], s["status"]), ("srv-child", "online"))
        self.assertGreater(s["memory"]["total_bytes"], 1e9)
        self.assertLess(s["memory"]["used_bytes"], s["memory"]["total_bytes"])
        self.assertGreaterEqual(s["network"]["tx_bps"], 0)   # sent is negative in Netdata
        self.assertGreaterEqual(s["disk_io"]["write_Bps"], 0)
        self.assertEqual(len(s["load"]), 3)
        self.assertEqual(s["disks"][0]["mount"], "/")
        self.assertEqual(snap["errors"], [])

    def test_offline_server(self):
        snap = self.run_snapshot("nodes_child_stale.json")
        s = snap["servers"][0]
        self.assertEqual(s["status"], "offline")
        self.assertEqual(s["last_seen"], 1794678686)
        self.assertIsNone(s["cpu"]["percent"])
        self.assertEqual(snap["summary"]["offline"], 1)

    def test_parent_down_raises(self):
        client = NetdataClient("http://parent:19999", opener=fake_opener(fail=True))
        cfg = {"netdata": {"parent_url": "http://parent:19999"}, "refresh_seconds": 2}
        with patch.object(fleet_provider, "_clients", return_value=(cfg, client, {})):
            with self.assertRaises(NetdataUnavailable):
                fleet_provider.servers_snapshot()

    def test_pve_counts_attached_and_errors_isolated(self):
        class FakePve:
            def guest_counts(self):
                return {"qemu": {"running": 3, "total": 4}, "lxc": {"running": 2, "total": 2}}

        client = NetdataClient("http://parent:19999", opener=fake_opener())
        cfg = {"netdata": {"parent_url": "http://parent:19999"}, "refresh_seconds": 2}
        with patch.object(fleet_provider, "_clients", return_value=(cfg, client, {"srv-child": FakePve()})):
            s = fleet_provider.servers_snapshot()["servers"][0]
        self.assertEqual(s["guests"]["qemu"], {"running": 3, "total": 4})


try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


@unittest.skipUnless(HAVE_FLASK, "flask not installed")
class RouteTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        import jwt_middleware
        import flask_fleet_routes
        self.auth = patch.object(jwt_middleware, "load_auth_config", return_value={"enabled": False})
        self.auth.start()
        app = Flask(__name__)
        app.register_blueprint(flask_fleet_routes.fleet_bp)
        self.client = app.test_client()

    def tearDown(self):
        self.auth.stop()

    def test_parent_down_is_503_with_message(self):
        err = NetdataUnavailable("Cannot reach Netdata Parent at http://p:19999: refused")
        with patch.object(fleet_provider, "servers_snapshot", side_effect=err):
            r = self.client.get("/api/fleet/servers")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["code"], "netdata_unreachable")
        self.assertIn("Cannot reach Netdata Parent", r.get_json()["error"])

    def test_native_mode_is_409(self):
        err = fleet_provider.FleetError("Multi-server views need DATA_SOURCE=netdata", "data_source_native", 409)
        with patch.object(fleet_provider, "servers_snapshot", side_effect=err):
            r = self.client.get("/api/fleet/servers")
        self.assertEqual(r.status_code, 409)

    def test_history_rejects_bad_id(self):
        r = self.client.get("/api/fleet/servers/..%2Fetc/history")
        self.assertIn(r.status_code, (400, 404))

    def test_auth_enforced_when_enabled(self):
        import jwt_middleware
        with patch.object(jwt_middleware, "load_auth_config", return_value={"enabled": True}), \
             patch.object(jwt_middleware, "verify_token_full", return_value=None):
            r = self.client.get("/api/fleet/servers")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
