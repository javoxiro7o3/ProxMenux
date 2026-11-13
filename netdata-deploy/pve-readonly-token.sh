#!/usr/bin/env bash
# Run on EACH Proxmox VE server. Creates a read-only API token that the
# ProxMenux fork uses to read guest inventory and network topology
# (VM/CT names, status, NIC -> bridge mapping, host bridges/bonds).
#
#   user  : proxmenux@pve   (no password, cannot log in to the web UI)
#   role  : PVEAuditor on /  (read-only: Sys.Audit, VM.Audit, Datastore.Audit, ...)
#   token : proxmenux@pve!monitor, privilege separation off (inherits PVEAuditor only)
#
# The secret is printed ONCE. Store it on the ProxMenux host in its secrets
# file (mode 600). Re-running does not change an existing token; use
# --rotate to delete and recreate it.
#
# Part of the ProxMenux fork (GPL-3.0).

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"
require_root
command -v pveum >/dev/null || die "pveum not found: run this on a Proxmox VE host"

USER_ID="proxmenux@pve"
TOKEN_ID="monitor"
ROTATE=0
[ "${1:-}" = "--rotate" ] && ROTATE=1

if ! pveum user list --output-format json | grep -q "\"userid\":\"$USER_ID\""; then
    pveum user add "$USER_ID" --comment "ProxMenux read-only monitoring"
    log "created user $USER_ID"
fi

# Idempotent: acl modify with the same role is a no-op.
pveum acl modify / --users "$USER_ID" --roles PVEAuditor

has_token() { pveum user token list "$USER_ID" --output-format json | grep -q "\"tokenid\":\"$TOKEN_ID\""; }

if has_token && [ "$ROTATE" = 1 ]; then
    pveum user token remove "$USER_ID" "$TOKEN_ID"
    log "removed old token"
fi

if has_token; then
    log "token $USER_ID!$TOKEN_ID already exists (secret not shown again; use --rotate to replace it)"
else
    log "creating token $USER_ID!$TOKEN_ID — copy the secret now, it is shown only once:"
    pveum user token add "$USER_ID" "$TOKEN_ID" --privsep 0 --comment "ProxMenux read-only" --output-format json-pretty
fi

FP="$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
log "node: $(hostname)   API: https://$(hostname -I | awk '{print $1}'):8006   TLS fingerprint (pin this): ${FP:-unknown}"
