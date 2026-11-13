#!/usr/bin/env bash
# Parent only: send Netdata alerts by email through the internal SMTP server
# (built by the partner). Uses msmtp as the system `sendmail`.
#
# Usage:
#   cp conf/smtp.env.example secrets/smtp.env && chmod 600 secrets/smtp.env && edit it
#   ./setup-email.sh secrets/smtp.env [--test]
#
# Idempotent. --test sends Netdata's test notifications (WARNING/CRITICAL/CLEAR).
# Part of the ProxMenux fork (GPL-3.0).

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"
require_root

ENV_FILE="${1:-}"; TEST="${2:-}"
[ -f "$ENV_FILE" ] || die "usage: $0 <smtp.env> [--test]"
[ "$(stat -c '%a' "$ENV_FILE")" = 600 ] || die "$ENV_FILE must be mode 600"

SMTP_HOST="" SMTP_PORT="" SMTP_TLS="" SMTP_STARTTLS="" SMTP_AUTH="" SMTP_USER=""
SMTP_PASSWORD="" SMTP_FROM="" SMTP_TO="" SMTP_CA_FILE=""
# shellcheck disable=SC1090
. "$ENV_FILE"

[[ "$SMTP_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || die "SMTP_HOST invalid"
[[ "$SMTP_PORT" =~ ^[0-9]+$ ]] || die "SMTP_PORT invalid"
for v in SMTP_TLS SMTP_STARTTLS SMTP_AUTH; do
    case "${!v}" in on|off) ;; *) die "$v must be on|off" ;; esac
done
[[ "$SMTP_FROM" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || die "SMTP_FROM invalid"
[ -n "$SMTP_TO" ] || die "SMTP_TO is empty"

ND_ETC=/opt/netdata/etc/netdata
ND_USER="$(stat -c '%U' /opt/netdata/var/lib/netdata 2>/dev/null)" || die "Netdata not installed (run install.sh --role parent first)"
ND_GROUP="$(id -gn "$ND_USER")"

# --- msmtp from the offline bundle
if ! command -v msmtp >/dev/null 2>&1 || [ ! -x /usr/sbin/sendmail ]; then
    DEBS="$HERE/../debs"
    ls "$DEBS"/*.deb >/dev/null 2>&1 || die "msmtp not installed and no .deb files in $DEBS"
    log "installing msmtp from $DEBS"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-download "$DEBS"/*.deb \
        || dpkg -i "$DEBS"/*.deb
fi

# --- password kept out of msmtprc, readable only by the netdata user
if [ "$SMTP_AUTH" = on ]; then
    printf '%s\n' "$SMTP_PASSWORD" | write_if_changed "$ND_ETC/smtp-password" 600 "$ND_USER:$ND_GROUP"
fi

{
    cat <<EOF
# Managed by ProxMenux netdata-deploy/setup-email.sh
defaults
syslog         LOG_MAIL
tls            $SMTP_TLS
tls_starttls   $SMTP_STARTTLS
EOF
    [ -n "$SMTP_CA_FILE" ] && echo "tls_trust_file $SMTP_CA_FILE"
    cat <<EOF

account        internal
host           $SMTP_HOST
port           $SMTP_PORT
from           $SMTP_FROM
auth           $SMTP_AUTH
EOF
    if [ "$SMTP_AUTH" = on ]; then
        echo "user           $SMTP_USER"
        echo "passwordeval   cat $ND_ETC/smtp-password"
    fi
    echo
    echo "account default : internal"
} | write_if_changed /etc/msmtprc 644 root:root

# --- Netdata notification overrides (stock file is read first, this one overrides it)
cat <<EOF | write_if_changed "$ND_ETC/health_alarm_notify.conf" 640 "root:$ND_GROUP"
# Managed by ProxMenux netdata-deploy/setup-email.sh
SEND_EMAIL="YES"
sendmail="/usr/sbin/sendmail"
EMAIL_SENDER="$SMTP_FROM"
DEFAULT_RECIPIENT_EMAIL="$SMTP_TO"
role_recipients_email[sysadmin]="\${DEFAULT_RECIPIENT_EMAIL}"
EMAIL_PLAINTEXT_ONLY="NO"
EOF

if [ "$TEST" = "--test" ]; then
    log "sending test notifications as $ND_USER"
    su -s /bin/bash -c "/opt/netdata/usr/libexec/netdata/plugins.d/alarm-notify.sh test" "$ND_USER"
fi
log "done"
