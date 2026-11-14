#!/usr/bin/env bash
# Install and configure Netdata (offline, static build) for the ProxMenux fork.
# Idempotent: run it again with the same arguments and nothing changes;
# change an argument and only the affected files are rewritten.
#
# Roles:
#   parent  central collector on server 1 (receives all streams, 1 week history,
#           runs alerts and email, serves the API to ProxMenux)
#   host    Proxmox VE host child (system, cgroups for VMs/CTs, hpssa, smartctl, ipmi)
#   guest   VM/CT child with Docker (system + docker containers)
#
# Examples:
#   ./install.sh --role parent --name nd-parent --bind 10.0.0.20 \
#        --allow-streaming '10.0.0.*' --allow-api '10.0.0.11' --keys secrets/stream-keys.conf
#   ./install.sh --role host  --name srv-01 --parent 10.0.0.20 --api-key <uuid>
#   ./install.sh --role guest --name srv-01-vm-app-1 --parent 10.0.0.20 --api-key <uuid> \
#        --pve-host srv-01 --vmid 105
#
# Part of the ProxMenux fork (GPL-3.0).

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"
# shellcheck source=versions.env
. "$HERE/versions.env"

ND_PREFIX="${ND_PREFIX:-/opt/netdata}"  # override only for tests
ND_ETC="$ND_PREFIX/etc/netdata"
ND_BIN="$ND_PREFIX/bin/netdata"
STREAM_PORT=19999

ROLE="" NAME="" PARENT="" API_KEY="" PVE_HOST="" VMID="" SOURCE=""
BIND="" ALLOW_STREAMING="" ALLOW_API="" KEYS_FILE="" TLS=0
RETENTION_SIZE="40GiB" SMART_CCISS=""

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; cat <<'EOF'

Options:
  --role parent|host|guest     required
  --name NAME                  Netdata hostname (srv-01, srv-01-vm-app-1, ...)
  --source DIR                 offline bundle "netdata-offline" dir
                               (default: ../netdata-offline next to this script)
  child options:
  --parent ADDR                Parent IP/hostname (port 19999)
  --api-key UUID               this child's stream API key
  --tls                        stream with TLS (Parent must have a certificate)
  --pve-host NAME              (guest) Proxmox server this guest runs on
  --vmid N                     (guest) PVE VMID of this guest
  --smart-cciss 'DEV:N,N,..'   (host) disks behind Smart Array, e.g. '/dev/sda:0,1,2,3'
  parent options:
  --bind ADDR                  IP to listen on (default: all)
  --allow-streaming PATTERN    Netdata simple pattern of child IPs, e.g. '10.0.0.*'
  --allow-api PATTERN          IPs allowed to read the API (the ProxMenux host)
  --keys FILE                  stream keys file made by gen-stream-keys.sh
  --retention-size SIZE        tier 0 disk budget (default 40GiB, see README)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --role) ROLE="$2"; shift ;;
        --name) NAME="$2"; shift ;;
        --parent) PARENT="$2"; shift ;;
        --api-key) API_KEY="$2"; shift ;;
        --pve-host) PVE_HOST="$2"; shift ;;
        --vmid) VMID="$2"; shift ;;
        --source) SOURCE="$2"; shift ;;
        --bind) BIND="$2"; shift ;;
        --allow-streaming) ALLOW_STREAMING="$2"; shift ;;
        --allow-api) ALLOW_API="$2"; shift ;;
        --keys) KEYS_FILE="$2"; shift ;;
        --retention-size) RETENTION_SIZE="$2"; shift ;;
        --smart-cciss) SMART_CCISS="$2"; shift ;;
        --tls) TLS=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done

# ---------------------------------------------------------------- validation
require_root
case "$ROLE" in parent|host|guest) ;; *) usage; die "--role is required" ;; esac
valid_name "$NAME" || die "--name must be a simple hostname"
if [ "$ROLE" != parent ]; then
    [ -n "$PARENT" ] || die "--parent is required for role $ROLE"
    [[ "$PARENT" =~ ^[A-Za-z0-9.:_-]+$ ]] || die "invalid --parent"
    valid_uuid "$API_KEY" || die "--api-key must be a UUID (see gen-stream-keys.sh)"
fi
if [ "$ROLE" = guest ]; then
    valid_name "$PVE_HOST" || die "--pve-host is required for role guest"
    [[ "$VMID" =~ ^[0-9]+$ ]] || die "--vmid is required for role guest"
fi
if [ "$ROLE" = parent ]; then
    [ -n "$ALLOW_STREAMING" ] || die "--allow-streaming is required for role parent"
    [ -n "$ALLOW_API" ] || die "--allow-api is required for role parent"
    [ -f "$KEYS_FILE" ] || die "--keys file not found (run gen-stream-keys.sh)"
    [[ "$RETENTION_SIZE" =~ ^[0-9]+(MiB|GiB)$ ]] || die "invalid --retention-size"
    for v in "$ALLOW_STREAMING" "$ALLOW_API" "$BIND"; do
        re='^[A-Za-z0-9.:*! _/-]*$'; [[ "$v" =~ $re ]] || die "invalid characters in --allow-*/--bind: $v"
    done
fi
[ -z "$SMART_CCISS" ] || [[ "$SMART_CCISS" =~ ^/dev/[a-z0-9]+:[0-9]+(,[0-9]+)*$ ]] \
    || die "invalid --smart-cciss, expected e.g. /dev/sda:0,1,2,3"

# ---------------------------------------------------------------- install
installed_version() { [ -x "$ND_BIN" ] && "$ND_BIN" -v 2>/dev/null | awk '{print $2}' | sed 's/^v//'; }

if [ "$(installed_version || true)" != "$NETDATA_VERSION" ]; then
    SOURCE="${SOURCE:-$HERE/../netdata-offline}"
    [ -x "$SOURCE/install.sh" ] || die "offline source not found at $SOURCE (use --source)"
    log "installing Netdata $NETDATA_VERSION from $SOURCE"
    # --disable-cloud is accepted but ignored by current Netdata; the agent is
    # simply never claimed, so it never connects to Netdata Cloud.
    (cd "$SOURCE" && ./install.sh --non-interactive --disable-telemetry --no-updates)
    [ "$(installed_version || true)" = "$NETDATA_VERSION" ] \
        || die "installed version is '$(installed_version || true)', expected $NETDATA_VERSION"
    mark_changed
else
    log "Netdata $NETDATA_VERSION already installed"
fi

ND_USER="$(stat -c '%U' "$ND_PREFIX/var/lib/netdata" 2>/dev/null || echo netdata)"
ND_GROUP="$(id -gn "$ND_USER")"

# Telemetry opt-out (also done by --disable-telemetry; kept for re-runs).
write_if_changed "$ND_ETC/.opt-out-from-anonymous-statistics" 644 root:root < /dev/null

# ---------------------------------------------------------------- netdata.conf
BIND_TO="*"; [ -n "$BIND" ] && BIND_TO="localhost $BIND"
render_netdata_conf() {
    cat <<EOF
# Managed by ProxMenux netdata-deploy (role: $ROLE). Local edits are overwritten.
[global]
    hostname = $NAME

[db]
    mode = dbengine
    update every = 1
EOF
    if [ "$ROLE" = parent ]; then
        cat <<EOF
    storage tiers = 3
    # Tier 0 = per-second data for 1 week (TZ requirement), size is a cap.
    dbengine tier 0 retention time = 7d
    dbengine tier 0 retention size = $RETENTION_SIZE
    dbengine tier 1 retention time = 3mo
    dbengine tier 1 retention size = 4GiB
    dbengine tier 2 retention time = 1y
    dbengine tier 2 retention size = 2GiB
EOF
    else
        cat <<'EOF'
    # Children keep a short local buffer so the Parent can back-fill
    # (replication) after a network or Parent outage.
    storage tiers = 1
    dbengine tier 0 retention time = 1d
    dbengine tier 0 retention size = 512MiB
EOF
    fi
    cat <<'EOF'

[ml]
    enabled = no

[registry]
    enabled = no

EOF
    if [ "$ROLE" = parent ]; then
        cat <<EOF
[health]
    enabled = yes
    # Stock alerts replaced by the proxmenux-*.conf versions (TZ thresholds).
    enabled alarms = !10min_cpu_usage !ram_in_use !disk_space_usage *

[web]
    default port = $STREAM_PORT
    bind to = $BIND_TO
    allow connections from = localhost $ALLOW_STREAMING $ALLOW_API
    allow streaming from = $ALLOW_STREAMING
    allow dashboard from = localhost $ALLOW_API
    allow badges from = localhost
    allow netdata.conf from = localhost
EOF
        if [ "$TLS" = 1 ]; then
            cat <<'EOF'
    ssl key = /opt/netdata/etc/netdata/ssl/key.pem
    ssl certificate = /opt/netdata/etc/netdata/ssl/cert.pem
EOF
        fi
    else
        cat <<'EOF'
[health]
    # Alerts are evaluated on the Parent.
    enabled = no

[web]
    # Local dashboard/API only; nothing listens on the network.
    bind to = 127.0.0.1 ::1
    allow connections from = localhost
    allow dashboard from = localhost
EOF
    fi
    cat <<EOF

[host labels]
    proxmenux_role = $ROLE
EOF
    case "$ROLE" in
        host)  echo "    pve_host = $NAME" ;;
        guest) printf '    pve_host = %s\n    pve_vmid = %s\n' "$PVE_HOST" "$VMID" ;;
    esac
}
render_netdata_conf | write_if_changed "$ND_ETC/netdata.conf" 644 "root:$ND_GROUP"

# ---------------------------------------------------------------- stream.conf
if [ "$ROLE" = parent ]; then
    {
        echo "# Managed by ProxMenux netdata-deploy. Contains secrets: mode 640."
        echo "[stream]"
        echo "    enabled = no"
        echo
        # keys file lines: <name> <uuid>
        while read -r kname kuuid; do
            case "$kname" in ''|'#'*) continue ;; esac
            if ! valid_name "$kname" || ! valid_uuid "$kuuid"; then die "bad line in $KEYS_FILE: $kname"; fi
            cat <<EOF
# child: $kname
[$kuuid]
    type = api
    enabled = yes
    allow from = $ALLOW_STREAMING
    db = dbengine
    health enabled = yes
    postpone alerts on connect = 1m
    enable replication = yes
    replication period = 1d

EOF
        done < "$KEYS_FILE"
    } | write_if_changed "$ND_ETC/stream.conf" 640 "root:$ND_GROUP"
else
    dest="tcp:$PARENT:$STREAM_PORT"
    [ "$TLS" = 1 ] && dest="$dest:SSL"
    cat <<EOF | write_if_changed "$ND_ETC/stream.conf" 640 "root:$ND_GROUP"
# Managed by ProxMenux netdata-deploy. Contains secrets: mode 640.
[stream]
    enabled = yes
    destination = $dest
    api key = $API_KEY
    enable replication = yes
$( [ "$TLS" = 1 ] && echo "    CAfile = $ND_ETC/ssl/parent-ca.pem" )
EOF
fi

# ---------------------------------------------------------------- collectors
GOD="$ND_ETC/go.d"
if [ "$ROLE" = host ]; then
    # HPE Smart Array. Runs `ssacli ctrl all show config detail` through
    # Netdata's setuid helper ndsudo, so no sudoers entry is needed.
    if ! command -v ssacli >/dev/null 2>&1; then
        deb="$(ls "$HERE"/../hpe/ssacli_*.deb 2>/dev/null | sort -V | tail -1 || true)"
        if [ -n "$deb" ]; then
            log "installing $(basename "$deb") from the offline bundle"
            dpkg -i "$deb" || die "dpkg -i $deb failed"
            mark_changed
        else
            warn "ssacli not found and no hpe/ssacli_*.deb in the bundle: RAID controller/array/logical drive status will be missing"
        fi
    fi
    cat <<'EOF' | write_if_changed "$GOD/hpssa.conf" 644 "root:$ND_GROUP"
# Managed by ProxMenux netdata-deploy.
jobs:
  - name: hpssa
    update_every: 10
EOF
    {
        cat <<'EOF'
# Managed by ProxMenux netdata-deploy.
jobs:
  - name: smartctl
    update_every: 60
    scan_every: 15m
    poll_devices_every: 5m
    no_check_power_mode: standby
EOF
        if [ -n "$SMART_CCISS" ]; then
            echo "    extra_devices:"
            dev="${SMART_CCISS%%:*}"
            IFS=, read -r -a idx <<< "${SMART_CCISS#*:}"
            for i in "${idx[@]}"; do
                printf '      - name: %s\n        type: cciss,%s\n' "$dev" "$i"
            done
        fi
    } | write_if_changed "$GOD/smartctl.conf" 644 "root:$ND_GROUP"

    # In-band IPMI (temps, fans, PSU) needs the kernel IPMI driver.
    if modprobe -n ipmi_si >/dev/null 2>&1; then
        printf 'ipmi_si\nipmi_devintf\n' | write_if_changed /etc/modules-load.d/netdata-ipmi.conf 644 root:root
        modprobe ipmi_si 2>/dev/null || warn "ipmi_si did not load (no in-band BMC access?)"
        modprobe ipmi_devintf 2>/dev/null || true
    fi
    [ -e /dev/ipmi0 ] || warn "/dev/ipmi0 missing: ipmi collector will have no data (VERIFY on Gen10)"
fi

if [ "$ROLE" = guest ]; then
    if command -v docker >/dev/null 2>&1 && getent group docker >/dev/null; then
        if ! id -nG "$ND_USER" | tr ' ' '\n' | grep -qx docker; then
            usermod -aG docker "$ND_USER"; mark_changed; log "added $ND_USER to group docker"
        fi
        cat <<'EOF' | write_if_changed "$GOD/docker.conf" 644 "root:$ND_GROUP"
# Managed by ProxMenux netdata-deploy.
jobs:
  - name: local
    address: 'unix:///var/run/docker.sock'
    timeout: 2
    collect_container_size: no
EOF
    else
        warn "docker not found on this guest: only system metrics will be collected"
    fi
fi

# ---------------------------------------------------------------- parent: alerts
if [ "$ROLE" = parent ]; then
    for f in "$HERE"/conf/health.d/*.conf; do
        write_if_changed "$ND_ETC/health.d/$(basename "$f")" 644 "root:$ND_GROUP" < "$f"
    done
fi

# ---------------------------------------------------------------- service
systemctl enable netdata >/dev/null 2>&1 || true
if has_changes || ! systemctl is-active --quiet netdata; then
    log "restarting netdata"
    systemctl restart netdata
else
    log "no changes"
fi

# ---------------------------------------------------------------- check
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$STREAM_PORT/api/v3/info" >/dev/null 2>&1; then
        log "netdata API answers on 127.0.0.1:$STREAM_PORT"
        exit 0
    fi
    sleep 1
done
die "netdata did not answer on 127.0.0.1:$STREAM_PORT within 30s (journalctl -u netdata)"
