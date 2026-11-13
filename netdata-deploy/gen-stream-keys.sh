#!/usr/bin/env bash
# Create/extend the stream API key file used by the Parent.
# One random UUID per child. Existing keys are kept, so re-running is safe.
#
# Usage: ./gen-stream-keys.sh srv-01 srv-02 srv-01-vm-app-1 ...
# Output: secrets/stream-keys.conf (mode 600, never committed — see .gitignore)
#
# Part of the ProxMenux fork (GPL-3.0).

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

[ $# -gt 0 ] || die "usage: $0 <child-name>..."
FILE="${KEYS_FILE:-$HERE/secrets/stream-keys.conf}"
umask 077
mkdir -p "$(dirname "$FILE")"
touch "$FILE"
chmod 600 "$FILE"

for name in "$@"; do
    valid_name "$name" || die "invalid child name: $name"
    if awk -v n="$name" '$1==n {found=1} END {exit !found}' "$FILE"; then
        log "$name: key exists, kept"
        continue
    fi
    uuid="$(cat /proc/sys/kernel/random/uuid)"
    printf '%s %s\n' "$name" "$uuid" >> "$FILE"
    log "$name: new key added"
done

log "keys file: $FILE"
log "child install commands use: --api-key \$(awk '\$1==\"<name>\"{print \$2}' $FILE)"
