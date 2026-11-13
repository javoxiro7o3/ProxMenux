# shellcheck shell=bash
# Shared helpers for the netdata-deploy scripts.
# Part of the ProxMenux fork (GPL-3.0). Not part of upstream ProxMenux.

set -o errexit -o nounset -o pipefail

log()  { printf '[netdata-deploy] %s\n' "$*"; }
warn() { printf '[netdata-deploy] WARNING: %s\n' "$*" >&2; }
die()  { printf '[netdata-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root"
}

# write_if_changed is usually the last stage of a pipeline (a subshell), so a
# shell variable would be lost; changes are recorded in a marker file instead.
CHANGE_MARKER="$(mktemp)"
trap 'rm -f "$CHANGE_MARKER"' EXIT
mark_changed() { echo 1 > "$CHANGE_MARKER"; }
has_changes()  { [ -s "$CHANGE_MARKER" ]; }

# write_if_changed <dest> <mode> <owner:group>  (content on stdin)
# Idempotent: only replaces the file when content, mode or owner differ.
write_if_changed() {
    local dest="$1" mode="$2" owner="$3" tmp
    tmp="$(mktemp)"
    cat > "$tmp"
    mkdir -p "$(dirname "$dest")"
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest" \
        && [ "$(stat -c '%a' "$dest")" = "$mode" ] \
        && [ "$(stat -c '%U:%G' "$dest")" = "$owner" ]; then
        rm -f "$tmp"
        return 0
    fi
    install -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    rm -f "$tmp"
    mark_changed
    log "updated $dest"
}

# Validate simple identifiers used in config files (hostnames, keys, labels).
valid_name() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]]; }
valid_uuid() { [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; }
