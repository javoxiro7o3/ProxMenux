# netdata-deploy — offline Netdata for the ProxMenux fork

Scripts and configuration that install Netdata on an **air-gapped** network:

* **Parent** — one VM/CT on server 1. Receives streams from all children, keeps
  1 week of per-second history, evaluates alerts, emails them, serves the API that
  ProxMenux reads.
* **host child** — every Proxmox VE server: system metrics, VM/CT usage (cgroups),
  HPE Smart Array (`hpssa`), SMART (`smartctl`), IPMI sensors.
* **guest child** — every VM/CT that runs Docker: system metrics + per-container usage.

Netdata version is pinned in [`versions.env`](versions.env) (currently **2.12.3**,
static build). No auto-update, no Netdata Cloud, no telemetry, ML disabled.

> Status: written against the Netdata source; **not yet run on the real DL380 Gen10
> servers**. Items marked VERIFY in [`../docs/analysis.md`](../docs/analysis.md)
> must be confirmed during the Stage 0 PoC.

## Files

| File | Where it runs | Purpose |
|---|---|---|
| `prepare-offline.sh` | machine with internet | builds the offline bundle (Netdata static + msmtp .debs + these scripts) |
| `gen-stream-keys.sh` | admin machine / Parent | one stream API key (UUID) per child → `secrets/stream-keys.conf` (600) |
| `install.sh` | Parent, every PVE host, Docker guests | install + configure Netdata, idempotent |
| `setup-email.sh` | Parent | msmtp → internal SMTP, Netdata email notifications, test mail |
| `pve-readonly-token.sh` | every PVE host | read-only API token for ProxMenux (inventory + network topology) |
| `conf/health.d/*.conf` | copied to Parent | RAID/SMART alerts (not shipped by Netdata) and TZ thresholds |

Secrets (`secrets/`, SMTP password, stream keys, PVE tokens) never go into git.

## 1. Prepare the bundle (internet machine, Debian matching the Parent)

```bash
SSACLI_DEB=~/Downloads/ssacli_<version>_amd64.deb ./prepare-offline.sh /media/usb/proxmenux-bundle
```

Copy the directory to the internal network, then on each target:

```bash
cd proxmenux-bundle && sha256sum -c SHA256SUMS
```

Not on a stock PVE host and not installable there without internet, so carry them in
the bundle:

* **HPE `ssacli`** (required for RAID controller / array / logical drive / cache battery
  status): download the Debian `.deb` from HPE (Management Component Pack repository) on the
  internet machine, put it in the bundle, install on each PVE host with `dpkg -i ssacli_*.deb`.
  It is a single self-contained package. Without it the `hpssa` collector stays idle.
* `ipmitool` and `lm-sensors` are **not** needed: Netdata's static build has its own IPMI
  plugin (reads `/dev/ipmi0`) and a built-in libsensors.

Before installing, run the read-only inventory on one host and keep the output:

```bash
./collect-hw-info.sh > hw-info-$(hostname).txt 2>&1
```

## 2. Stream keys

```bash
cd proxmenux-bundle/netdata-deploy
./gen-stream-keys.sh srv-01 srv-02 srv-03 srv-04 srv-05 srv-06 srv-01-vm-app-1
```

Naming: hosts `srv-NN`, guests `srv-NN-vm-<name>` (TZ §4). Re-running keeps
existing keys and adds new ones.

## 3. Parent

```bash
./install.sh --role parent --name nd-parent --bind <parent-ip> \
    --allow-streaming '<lan pattern, e.g. 10.0.0.*>' \
    --allow-api '<ProxMenux host ip>' \
    --keys secrets/stream-keys.conf
```

* Port 19999 accepts streams only from `--allow-streaming` and API reads only from
  `--allow-api` (plus localhost). Netdata's API has no login of its own, so also
  restrict 19999 on the Parent's firewall to the same addresses.
* Disk: tier 0 (per-second, 7 days) is capped by `--retention-size` (default 40GiB,
  an estimate for 6 hosts + ~20 Docker guests). Real usage is measured after 24 h:
  `curl -s localhost:19999/api/v1/dbengine_stats`.
* After adding children later, add their keys and re-run the same command.

## 4. Proxmox hosts

```bash
./install.sh --role host --name srv-01 --parent <parent-ip> \
    --api-key "$(awk '$1=="srv-01"{print $2}' secrets/stream-keys.conf)" \
    [--smart-cciss '/dev/sda:0,1,2,3']
```

* `hpssa` runs `ssacli ctrl all show config detail` through Netdata's own setuid
  helper `ndsudo`; **no sudoers entry is needed**.
* `--smart-cciss` lists the physical drive numbers behind the Smart Array
  (`smartctl -d cciss,N`). The right device and numbers come from the real
  `ssacli`/`smartctl --scan` output (VERIFY).
* IPMI: the script loads `ipmi_si`/`ipmi_devintf`; whether in-band IPMI works on
  Gen10 is VERIFY.
* The host's local Netdata API listens on 127.0.0.1 only.

## 5. Docker guests

```bash
./install.sh --role guest --name srv-01-vm-app-1 --parent <parent-ip> \
    --api-key "$(awk '$1=="srv-01-vm-app-1"{print $2}' secrets/stream-keys.conf)" \
    --pve-host srv-01 --vmid 105
```

Adds the `netdata` user to the `docker` group (read access to the Docker socket,
needed for container names/state). Host labels `pve_host`/`pve_vmid` let ProxMenux
attach containers to the right VM and server.

## 6. Email alerts (Parent)

```bash
install -m 600 conf/smtp.env.example secrets/smtp.env   # then edit
./setup-email.sh secrets/smtp.env --test
```

Alerts that send mail: child disconnected (`streaming_disconnected`, stock),
RAID controller / cache / battery / array / logical drive / physical drive status,
SMART failed, disk temperature, CPU/RAM > 90 % for 5 min, disk space 85/95 %,
Docker container down (stock; enable per container via `chart labels`). Netdata
sends a recovery ("CLEAR") mail when an alert returns to normal.

## 7. Read-only PVE token (each PVE host)

```bash
./pve-readonly-token.sh
```

Creates `proxmenux@pve!monitor` with role `PVEAuditor` and prints the secret once
plus the certificate fingerprint to pin. These go into the ProxMenux host's secrets
file (Stage 2).

## 8. Connect ProxMenux Monitor (the fork)

On the ProxMenux host, as root:

```bash
install -d -m 700 ~/.config/proxmenux-monitor
cat > ~/.config/proxmenux-monitor/fleet.json <<'EOF'
{
  "data_source": "netdata",
  "refresh_seconds": 2,
  "netdata": {"parent_url": "http://<parent-ip>:19999", "timeout": 3},
  "pve": [
    {"name": "srv-01", "url": "https://<srv-01-ip>:8006",
     "token_id": "proxmenux@pve!monitor", "token_secret": "<secret>",
     "fingerprint": "<AA:BB:...>"}
  ]
}
EOF
chmod 600 ~/.config/proxmenux-monitor/fleet.json
```

* `name` must equal the Netdata host name of that server (`--name` of `install.sh --role host`).
* The file is re-read automatically when it changes; no restart needed.
* `DATA_SOURCE=native` (environment) or `"data_source": "native"` turns the multi-server
  views off. In `netdata` mode an unreachable Parent is shown as an error in the UI;
  ProxMenux never falls back to its own collectors silently.
* The UI section is **Fleet → All servers**.

## Verify

```bash
curl -s localhost:19999/api/v3/info | head        # on any node
curl -s 'localhost:19999/api/v3/nodes' | head     # on the Parent: all children listed
curl -s 'localhost:19999/api/v3/contexts' | grep -o '"hpssa[^"]*"' | sort -u   # on a host
ss -tnp | grep netdata                             # no connection outside the LAN
```

## Upgrade

1. Change `NETDATA_VERSION` in `versions.env`, run `prepare-offline.sh` again.
2. Run `install.sh` with the same arguments on **one** host, check, then the rest,
   Parent last. Rollback = same procedure with the previous bundle.

## Remove

```bash
/opt/netdata/usr/libexec/netdata/netdata-uninstaller.sh --yes --force   # static-install path, VERIFY
```
