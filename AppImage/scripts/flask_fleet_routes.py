"""Routes for the multi-server views (data from the Netdata Parent).

ProxMenux fork addition (GPL-3.0). Not part of upstream ProxMenux.

Errors are always explicit JSON ({"error", "code"}) with a non-200 status; the
UI shows them and keeps the last good data. No fallback to native collection.
"""

import re

from flask import Blueprint, jsonify, request

import fleet_config
import fleet_provider
from jwt_middleware import require_auth
from netdata_client import NetdataUnavailable

fleet_bp = Blueprint("fleet", __name__)

_NODE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def _error(message, code, status):
    return jsonify({"error": message, "code": code}), status


def _guard(fn):
    try:
        return jsonify(fn())
    except fleet_provider.FleetError as exc:
        return _error(str(exc), exc.code, exc.status)
    except NetdataUnavailable as exc:
        return _error(str(exc), "netdata_unreachable", 503)


@fleet_bp.route("/api/fleet/config", methods=["GET"])
@require_auth
def fleet_config_view():
    try:
        return jsonify(fleet_config.public_view(fleet_config.load()))
    except fleet_config.FleetConfigError as exc:
        return _error(f"Fleet configuration error: {exc}", "config_invalid", 500)


@fleet_bp.route("/api/fleet/servers", methods=["GET"])
@require_auth
def fleet_servers():
    return _guard(fleet_provider.servers_snapshot)


@fleet_bp.route("/api/fleet/servers/<node_id>/history", methods=["GET"])
@require_auth
def fleet_server_history(node_id):
    if not _NODE_ID.match(node_id):
        return _error("Invalid server id", "bad_request", 400)
    metric = request.args.get("metric", "cpu")
    range_key = request.args.get("range", "5m")
    return _guard(lambda: fleet_provider.server_history(node_id, metric, range_key))
