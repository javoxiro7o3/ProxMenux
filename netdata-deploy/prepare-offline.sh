#!/usr/bin/env bash
# Run on a machine WITH internet access. Produces one directory that is copied
# to the air-gapped network and contains everything needed to install Netdata
# (static build, pinned version) and the msmtp mail relay client for the Parent.
#
# Usage: ./prepare-offline.sh <output-dir>
#
# The msmtp .deb files are downloaded with apt-get, so run this on the same
# Debian release as the Parent VM/CT (Debian 12 or 13), or skip them with
# SKIP_DEBS=1 and fetch them separately.
#
# Part of the ProxMenux fork (GPL-3.0).

set -o errexit -o nounset -o pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=versions.env
. "$HERE/versions.env"

OUT="${1:-}"
[ -n "$OUT" ] || { echo "usage: $0 <output-dir>" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "$OUT already exists; choose a new directory" >&2; exit 1; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

echo "==> Netdata v${NETDATA_VERSION} offline source"
# kickstart.sh is taken from the same tag that we pin, not from the moving URL.
curl -fsSL --retry 3 -o "$OUT/kickstart.sh" \
    "https://raw.githubusercontent.com/netdata/netdata/v${NETDATA_VERSION}/packaging/installer/kickstart.sh"
NETDATA_OFFLINE_ARCHES="$NETDATA_OFFLINE_ARCHES" \
    sh "$OUT/kickstart.sh" --non-interactive --release-channel stable \
       --install-version "$NETDATA_VERSION" \
       --prepare-offline-install-source "$OUT/netdata-offline"
rm -f "$OUT/kickstart.sh"

if [ "${SKIP_DEBS:-0}" != "1" ]; then
    echo "==> msmtp packages ($(. /etc/os-release && echo "$PRETTY_NAME"))"
    mkdir -p "$OUT/debs"
    (
        cd "$OUT/debs"
        # Resolve the dependency closure that is not part of a minimal Debian.
        pkgs="msmtp msmtp-mta"
        # shellcheck disable=SC2086
        deps="$(apt-cache depends --recurse --no-recommends --no-suggests \
                  --no-conflicts --no-breaks --no-replaces --no-enhances $pkgs \
                | grep '^\w' | sort -u)"
        # shellcheck disable=SC2086
        apt-get download $deps
    )
    (. /etc/os-release && echo "$VERSION_CODENAME") > "$OUT/debs/DEBIAN_CODENAME"
fi

echo "==> deployment scripts"
mkdir -p "$OUT/netdata-deploy"
cp -a "$HERE/." "$OUT/netdata-deploy/"
rm -rf "$OUT/netdata-deploy/secrets"

(cd "$OUT" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
echo "==> done: $OUT  (verify on the target with: cd $OUT && sha256sum -c SHA256SUMS)"
