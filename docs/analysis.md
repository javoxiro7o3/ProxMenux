# Stage 0 — Analysis: ProxMenux Monitor + Netdata

Status: **draft, desk analysis only** (source code read; nothing has been run on real hardware yet).
Date: 2026-11-13

Sources analysed:

| Project | Repository | Revision read |
|---|---|---|
| ProxMenux | `MacRimi/ProxMenux` | `main` @ `55409fff` (release `v1.2.8`), GPL-3.0 |
| Netdata | `netdata/netdata` | `master` @ `313fd1e` (rolling), GPL-3.0 (agent) |

Anything marked **[VERIFY]** has to be confirmed on a real DL380 Gen10 host before it is relied on.

---

## 1. ProxMenux Monitor — structure

ProxMenux Monitor lives entirely in `AppImage/` of the upstream repo. The rest of the
repo (`scripts/`, `menu`, `web/`, `oci/`, ...) is the ProxMenux TUI menu and the public
website — not needed for the fork's monitor work, but kept for GPL/upstream-merge reasons.

### 1.1 Runtime model

* One **AppImage** (`ProxMenux-1.2.8.AppImage`, ~54 MB) runs on **one Proxmox host**.
* Inside: a **Flask** backend (`scripts/flask_server.py`, ~24 000 lines) served by
  **gevent `pywsgi`** on `[::]:8008` (HTTP or HTTPS), which also serves the
  statically exported **Next.js** frontend (`/`, `/_next/...`).
* `scripts/AppRun` is the entry point; it picks the bundled Python deps for
  CPython 3.11 (PVE 8) or 3.13 (PVE 9) and starts `flask_server.py`.
* Systemd unit is installed by `install_proxmenux.sh` (outside `AppImage/`).

### 1.2 Backend (Flask) — files that matter for this project

| File | Role | Fate in fork |
|---|---|---|
| `flask_server.py` | Monolith: ~180 routes, all metric collection helpers (`psutil`, `/proc`, `/sys`, `pvesh`, `lsblk`, `smartctl`, `ipmitool`, `sensors`), static file serving, server start | Metric routes get a provider switch; new routes for the 3 new sections |
| `auth_manager.py`, `flask_auth_routes.py`, `jwt_middleware.py` | Login, password hash, JWT, **TOTP 2FA** (`pyotp`), API tokens; config in `auth.json` | **Unchanged** |
| `flask_terminal_routes.py`, `terminal-panel.tsx` | Web terminal (WebSocket, host shell / `pct enter`) | **Unchanged** |
| `health_monitor.py`, `health_persistence.py`, `health_thresholds.py`, `flask_health_routes.py` | Own sampler (CPU every few s via `psutil`), health checks, SQLite history, thresholds, acknowledgements | Sampler is the main "native collection" to replace; health checks need a decision (see §5) |
| `notification_*.py`, `flask_notification_routes.py` | Own notification engine (Telegram, Discord, Gotify, email, Apprise, AI enrichment) | Kept; in offline env only local email/none are meaningful. Netdata alerts are the primary channel per TZ |
| `hardware_monitor.py`, `temperature_sensor_resolver.py`, `smartctl_resolver.py`, `disk_temperature_history.py` | Temps (`sensors`), IPMI fans/PSU (`ipmitool sensor`), SMART (`smartctl`), disk temp history | Replaced by Netdata (`sensors`/`ipmi`/`smartctl`/`hpssa` collectors) |
| `proxmox_storage_monitor.py`, `mount_monitor.py`, `lvm_device_scope.py` | Storage pools (`pvesh get /storage`), mounts, LVM | Pool inventory stays native (it is PVE config, not a metric) |
| `monitor_links.py`, `flask_monitor_links_routes.py` | **Links** to other ProxMenux Monitors (cluster nodes/PBS/manual). Only URLs, no data aggregation | Kept; not a multi-host data path |
| `lxc_apps.py`, `docker_app_discovery.py`, `managed_installs.py`, `oci_*.py`, host backup, audit, security | Management features | **Unchanged** |

Collection primitives used today (all **local host only**):

* `psutil` (CPU, RAM, net counters, processes), `os.getloadavg()`, `/proc`, `/sys/class/net/*`
* `pvesh get /cluster/resources` → VM/CT list incl. `cpu`, `mem`, `netin/out`, `diskread/write` (filtered to the local node)
* `pvesh get /nodes/<node>/rrddata` and `/nodes/<node>/<qemu|lxc>/<vmid>/rrddata` → history charts (hour/day/week/month/year)
* `lsblk`, `smartctl`, `sensors -j`, `ipmitool sensor`, `upsc`, `nvidia-smi`/GPU tools
* **No HPE Smart Array (`ssacli`) support exists in ProxMenux today.** Disks behind a Smart Array in RAID mode are invisible to its SMART code (it calls `smartctl` on `/dev/sdX` without `-d cciss,N`).

### 1.3 API endpoints relevant to monitoring

| Endpoint | Today's source | Netdata replacement |
|---|---|---|
| `GET /api/system` | health_monitor sampler (psutil), `psutil.virtual_memory`, `sensors`, loadavg, uptime | `system.cpu`, `mem.available`/`system.ram`, `system.load`, `system.uptime`, `sensors.*` |
| `GET /api/node/metrics?timeframe=` | `pvesh .../rrddata` (PVE RRD, 1 min–1 day resolution) | `/api/v3/data` on `system.*` contexts (per-second, 1 week on Parent) |
| `GET /api/vms` | `pvesh /cluster/resources` (config + PVE's own usage numbers) | Inventory/status stays `pvesh`; usage from `cgroup.*` (§3.3) |
| `GET /api/vms/<id>/metrics` | `pvesh .../rrddata` | `/api/v3/data` with `instances=` the guest's cgroup |
| `GET /api/network`, `/api/network/summary`, `/api/network/<if>/metrics` | `/sys/class/net`, `psutil.net_io_counters` | `net.net`, `net.packets`, `net.errors` (counters only) |
| `GET /api/storage`, `/api/storage/summary`, `/api/storage/smart/*` | `lsblk`, `smartctl` | `disk.*`, `smartctl.*`, `hpssa.*` |
| `GET /api/hardware`, `/api/hardware/live` | `lscpu`, `dmidecode`, `sensors`, `ipmitool` | Live part from `sensors.*`/`ipmi.*`; static inventory (`dmidecode`, `lscpu`) has **no Netdata equivalent** → keep native |
| `GET /api/health`, `/api/health/full` | own health checks | Netdata `/api/v3/alerts` for monitored metrics; PVE-specific checks stay native |
| `GET /api/prometheus` | own exporter | Keep or proxy Netdata `allmetrics?format=prometheus` (decision later) |

### 1.4 Frontend (Next.js 15, React 19, static export)

* `app/page.tsx` → `components/proxmox-dashboard.tsx` — top-level tabs:
  `overview`, `apps`, `vms`, `node` (storage / network / hardware / logs), `backup`, `terminal`, `admin` (security / settings / profile / about).
  Tab order is user-orderable, persisted in `localStorage` (`lib/tab-order.ts`).
* Data fetching: `lib/api-config.ts` `fetchApi()` (JWT from `localStorage`) + **SWR** polling
  (`refreshInterval` 2.5 s – 60 s depending on component); no SSE today.
* UI kit: shadcn/radix components in `components/ui`, Tailwind, `next-themes`, `recharts` for charts.
* i18n: `messages/<lang>` — English stays default (TZ: UI language unchanged).
* Biggest components: `virtual-machines.tsx` (460 KB), `host-backup.tsx`, `storage-overview.tsx` (259 KB), `hardware.tsx`, `network-flow.tsx`.

New sections will be **new tabs** (`servers`, `guests`, `docker`) added to `TabId` in
`lib/tab-order.ts` and to `proxmox-dashboard.tsx`, as separate component files, so upstream
merges touch as few existing lines as possible.

### 1.5 Network Flow diagram ("tarmoq chizmasi")

`components/network-flow.tsx` uses `GET /api/network` → `get_network_info()`:

* topology from **local** `/sys/class/net/*` (`device`, `master`, `bonding/`, `brif/`), `ip` data, PVE guest configs (which `tap`/`veth` belongs to which VMID);
* live rates from `psutil.net_io_counters(pernic=True)`.

**Netdata does not provide topology** (bridge membership, bond slaves, tap→VM mapping). It only
has per-interface counters (`net.net`), with no "member of bridge X" label.

➡ **Decided:** whole-network diagram built from read-only PVE API (topology) + Netdata (rates). See §5.1.

---|---|---|
| `appimagetool` | `wget` from GitHub (cached in `~/.cache`) | ship the binary in `vendor/` |
| `npm install --legacy-peer-deps` | npm registry | `vendor/npm-cache` (`npm ci --offline --cache`) |
| `pip3 install --target ...` (Flask, gevent, psutil, pyotp, ...; cp311 + cp313 wheels) | PyPI | `vendor/wheels/` + `pip --no-index --find-links` |
| `borg` binary | GitHub release | `vendor/` or drop |
| `apt-get download ipmitool lm-sensors ...` | Debian mirror | `vendor/debs/` (likely unnecessary once Netdata does sensors) |
| logo | `raw.githubusercontent.com` | already in repo — use local file |

Runtime external calls found (must be removed/disabled in the fork):

* `api.github.com` / `raw.githubusercontent.com` — self-update & version checks (`proxmenux_version_status.py`, `flask_proxmenux_routes.py`), app catalog (`lxc_apps.py`, `managed_installs.py`).
* `cdn.jsdelivr.net/gh/selfhst/icons` — app icons loaded **by the browser** (`lxc-app-panel.tsx`, `virtual-machines.tsx`).
* Notification channels / AI providers (Telegram, Discord, OpenAI, ...) — only if the user configures them; in an air-gapped net they just fail. Will hide or leave inert (decision later, low priority).
* Fonts: `geist` is an npm package (bundled locally) — OK.

### 1.7 Single-host assumption — what this means for "all servers"

Every collection path is hard-wired to the local node (`get_proxmox_node_name()`; remote
nodes in `/cluster/resources` are explicitly skipped). Nothing in the backend models "a list of
hosts". Therefore:

* existing tabs (Overview, Storage, Hardware, VMs, Network) stay **per local host** — they combine
  metrics with management actions that only work locally (`qm`, `pct`, `pvesh`);
* the **3 new tabs are the multi-host part** and read **only** from the Netdata Parent;
* the effort is moderate (new module + 3 new components + switching ~10 metric routes), not a rewrite.

---

## 2. Netdata — what we get

### 2.1 API (from `src/web/api/netdata-swagger.json`)

Available on the Parent, covering all streamed children:

| Need | Endpoint |
|---|---|
| list of nodes, online/offline, last seen | `GET /api/v3/nodes`, `GET /api/v3/stream_info`, `GET /api/v3/info` |
| contexts available per node | `GET /api/v3/contexts?scope_nodes=...` |
| **batched** current values for many nodes/instances | `GET /api/v3/data?scope_nodes=*&contexts=cgroup.cpu&group_by=node,instance&after=-5&points=1` (one call for all ~90 guests) |
| history (1 week) | same `/api/v3/data` with `after=-604800&points=N` |
| active alerts | `GET /api/v3/alerts`, `/api/v3/alert_transitions` |
| per-node legacy | `/host/<hostname>/api/v1/data`, `/api/v1/allmetrics` |

`/api/v3/data` with `group_by` + `scope_nodes` is the key to the TZ requirement
"don't fire one request per VM": the provider can fetch every metric of one kind for every
node/guest in **one** HTTP call, cache it for 1 s, and serve all browser polls from cache.

### 2.2 Collectors and contexts

| Data | Collector | Contexts (from source) | Notes |
|---|---|---|---|
| Host CPU/RAM/load/net/disk I/O/PSI | proc.plugin | `system.cpu`, `system.ram`, `mem.available`, `system.load`, `system.uptime`, `net.net`, `disk.io`, `disk.util`, `system.*_pressure` | standard, 1 s |
| VM / CT usage | cgroups.plugin | `cgroup.cpu`, `cgroup.cpu_limit`, `cgroup.mem`, `cgroup.mem_usage`, `cgroup.io`, `cgroup.net_*` | Proxmox support built in: `qemu.slice/<vmid>.scope` and `lxc/<vmid>` → name resolved from `/etc/pve` (`cgroup-name/FLOW.md` rules 9–10). **[VERIFY]** name/label format on PVE 8/9 |
| Docker containers (inside VM/CT) | cgroups.plugin + go.d `docker` | `cgroup.*` with label `container_name`, `image`; `docker.container_state`, `docker.container_health_status` | `docker` collector needs access to `/var/run/docker.sock` (netdata user in `docker` group) |
| HPE Smart Array | go.d `hpssa` | `hpssa.controller_status`, `hpssa.controller_temperature`, `hpssa.controller_cache_module_{presence_status,status,battery_status,temperature}`, `hpssa.array_status`, `hpssa.logical_drive_status`, `hpssa.physical_drive_status`, `hpssa.physical_drive_temperature` | Runs `ssacli` through Netdata's own **`ndsudo`** helper (not sudoers). Parser tested upstream only on P212/P410i/P400ar/P400i/P408i-a output. **[VERIFY]** on Gen10 (P408i-a / P816i-a) with real output |
| SMART | go.d `smartctl` | `smartctl.device_smart_status`, `device_temperature`, `device_power_on_time`, `device_*_errors_rate`, `device_smart_attr_*` | Disks behind Smart Array need `extra_devices: [{name: /dev/sda, type: cciss,0}, ...]`. **[VERIFY]** that `smartctl -d cciss,N` works on Gen10 in RAID mode (HBA mode would expose disks directly) |
| Temps/fans/PSU | go.d `sensors`, `ipmi` plugin | `sensors.*`, `ipmi.*` | in-band IPMI needs `ipmi_si`/`ipmi_devintf` kernel modules. **[VERIFY]** |

RAID composition **is available** through chart labels (`hpssa/charts.go`):

* controller charts: `slot`, `model`
* array charts: `slot`, `array_id`, `interface_type`, `array_type`
* logical drive charts: `slot`, `array_id`, `logical_drive_id`, `disk_name`, `drive_type`
* physical drive charts: `slot`, `array_id`, `logical_drive_id`, `location`, `interface_type`, `drive_type`, `model`

So "logical drive X consists of physical drives at locations A, B, C" is a join on
`logical_drive_id`. Drive **capacity** is not exposed by hpssa → take it from smartctl
(`device_name`/`model_name`/`serial_number` labels) if the cciss mapping works. **[VERIFY]** label values
with real `ssacli` output. Status values: each status chart has dimensions `ok`/`nok` (1 = current
state); `nok` covers degraded/rebuilding/failed without distinguishing them — the exact text
("Interim Recovery Mode", "Rebuilding") is not exported. **[VERIFY]** whether that is enough or a
small parser of `ssacli` text is needed for the "rebuilding" label in the UI.

`smartctl` via `ndsudo` accepts `--device cciss,N` (comma is in ndsudo's allowed character set);
extra devices are keyed by `name|type`, so several `cciss,N` entries for the same `/dev/sdX` work.

### 2.3 Alerts

Already shipped with Netdata:

* `streaming_disconnected`, `streaming_never_connected` (`health.d/streaming.conf`) — on the Parent, child stopped streaming. ✅ "child aloqasi uzildi".
* `docker_container_down`, `docker_container_unhealthy` (`health.d/docker.conf`).
* CPU/RAM/disk space (`cpu.conf`, `ram.conf`, `disks.conf`), cgroup CPU/RAM (`cgroups.conf`).

**Not shipped**: there is **no `hpssa.conf` and no `smartctl` alert file** in `health.d/`. RAID degraded,
physical drive failed, controller/battery failed, SMART failed alerts must be **written by us**
(`netdata-deploy/conf/health.d/proxmenux-hpe.conf`).

Email: Netdata's `alarm-notify.sh` sends via `sendmail`; `msmtp-mta` provides a `sendmail` binary →
configure `/etc/msmtprc` on the Parent.

### 2.4 Offline install

`packaging/installer/methods/offline.md`: `kickstart.sh --prepare-offline-install-source DIR` on an
internet machine, copy `DIR`, run `install.sh` from it. **Static builds only, no auto-update** —
matches the TZ. Pinned to stable **v2.12.3** (`netdata-deploy/versions.env`); `--install-version`
works together with `--prepare-offline-install-source`.

**Netdata Cloud:** in current Netdata the installer flag `--disable-cloud` is accepted but ignored
(`kickstart.sh`: "Cloud cannot be disabled"). The agent only connects to Cloud after it is
**claimed**; we never claim, keep the dashboard/registry off and the network has no internet, so no
data leaves. To be proven in the PoC with `ss -tnp` / firewall logs (no outbound attempts).
Telemetry: `--disable-telemetry` creates `.opt-out-from-anonymous-statistics`.

The Netdata API has **no authentication** of its own (bearer protection is a Cloud feature). The
Parent API is protected by `[web] allow dashboard from = <ProxMenux IP>` plus a host firewall; the
TZ's "read token" for ProxMenux is therefore an IP allow-list, not a token.

### 2.5 Licences

* ProxMenux: GPL-3.0 → fork stays GPL-3.0, keep `LICENSE`, add `NOTICE` listing changes.
* Netdata agent: GPL-3.0 (`LICENSE`).
* Netdata **dashboard UI** (bundled in the agent): **Netdata Cloud UI License v1.0 (NCUL1)**, not open source
  (`REDISTRIBUTED.md`). We only call the **API**; we do not redistribute or modify the UI, so it does not affect the fork. (Not legal advice.)

---

## 3. Design of the change (proposal — not started)

### 3.1 Provider layer

```
AppImage/scripts/
  data_providers/
    __init__.py          get_provider()  ← DATA_SOURCE=netdata|native (default netdata)
    base.py              interface: system_snapshot(), node_history(), guests_usage(), disks(), ...
    native_provider.py   thin wrappers around the existing functions (no logic moved)
    netdata_client.py    HTTP client to Parent: timeout, bearer/API key, 1 s TTL cache, request coalescing
    netdata_provider.py  maps Netdata responses to ProxMenux's existing JSON shapes
  flask_fleet_routes.py  new blueprint: /api/fleet/servers, /api/fleet/guests, /api/fleet/docker, /api/fleet/stream (SSE)
```

* Existing routes change by ~2 lines each: `return jsonify(get_provider().system_snapshot())`.
* If `DATA_SOURCE=netdata` and the Parent is unreachable → HTTP 503 with
  `{"error": "netdata_unreachable", "parent": "...", "detail": "..."}`; the UI shows a banner.
  **No silent fallback to native.**
* Config: `/usr/local/share/proxmenux/netdata.json` (mode 600): `parent_url`, `api_key/bearer`,
  `refresh_seconds`, `local_node_name`, `tls_ca`. `.env.example` in repo, real file gitignored.

### 3.2 "Real usage" definitions mapped to Netdata

| Object | CPU | RAM |
|---|---|---|
| Server | `system.cpu` (100 − idle − iowait?) **[decide: iowait counted as busy or not]** | `system.ram` used (excludes cache/buffers) |
| VM/CT (host view) | `cgroup.cpu` (sum of user+system, % of one core → normalised by vCPU count) | `cgroup.mem_usage` (QEMU RSS incl. overhead for VMs; cgroup memory for CT) |
| VM (inside view, if child installed) | that child's `system.cpu` | that child's `system.ram` used |
| Docker container | `cgroup.cpu` of the container cgroup on the guest's child | `cgroup.mem_usage` (+ `cgroup.mem_usage_limit` if set) |

Difference vs Proxmox UI: PVE shows `cpu` from `/cluster/resources` (cgroup-based too) but
VM `mem` = guest-reported via balloon/QEMU agent → can differ from host RSS. Tooltip in UI will state
"Host view (cgroup)" vs "Guest view (Netdata in guest)".

### 3.3 Joining guests to servers and Docker to guests

* Host child hostname: `srv-01` … ; guest child hostname: `srv-01-vm-<name>` (TZ §4).
* Better than parsing hostnames: Netdata **host labels** (`netdata.conf` `[host labels]`):
  `pve_host=srv-01`, `pve_vmid=105`, `role=pve-host|guest`. The provider joins on labels.

---

## 4. Gaps / things Netdata cannot provide

| Item | Status |
|---|---|
| Network Flow topology (bridges/bonds/taps) | No Netdata source → read-only PVE API per server (§5.1) |
| Static hardware inventory (dmidecode: board, BIOS, DIMMs, PCI) | No source → keep native for local host; not shown for other servers |
| VM/CT config, status, names, tags, notes, backups, control | PVE config, not metrics → keep `pvesh`/`qm`/`pct` for local host; other servers: inventory via read-only PVE API; stopped guests not shown (§5) |
| RAID composition (logical drive ↔ physical drives), drive model/capacity | Depends on hpssa chart labels — **[VERIFY]** with real `ssacli` output |
| ProxMenux health checks for PVE-specific things (updates, storage pools, backups, certificates) | Keep native |
| SMART detail modal (full attribute table, self-test run/schedule) | Netdata has attributes as metrics, but **running a self-test** is an action → keep native |

---

## 5. Decisions (2026-11-13)

| # | Question | Decision |
|---|---|---|
| 1 | Repo import | Real GitHub fork of `MacRimi/ProxMenux` with full history, work based on release tag **`v1.2.8`** (`8fe9d207`). Upstream merges via `git merge upstream/vX.Y.Z`. |
| 2 | Where ProxMenux runs | All servers have equal priority. ProxMenux runs on **one Proxmox host** (default: srv-01, configurable). Being on a PVE host is only needed for the local terminal / local VM control; all multi-server data comes from Netdata + PVE API, so the choice of host does not change the monitoring view. |
| 3 | Network Flow | Must show the **whole network**: every server, its NICs/bonds/bridges and its **running** VMs/CTs. See §5.1. |
| 4 | Remote guest inventory | **Read-only PVE API token per server** (role `PVEAuditor`). Stopped guests are not shown. |
| 5 | Hardware samples | User will collect them on site (`ssacli`, `smartctl`, `ipmitool`, `pveversion`). Parsers/alerts for hpssa stay **[VERIFY]** until then. |
| 6 | ProxMenux own notifications | Keep running **in parallel** with Netdata email alerts. |

### 5.1 Whole-network diagram (consequence of decisions 3 + 4)

Netdata alone cannot draw topology, but together with the read-only PVE API it can:

| Diagram element | Source |
|---|---|
| Servers | list of configured PVE endpoints ↔ Netdata nodes (joined by host label `pve_host`) |
| Physical NICs, bonds (slaves), bridges (ports), VLANs | PVE API `GET /nodes/{node}/network` (fields `type`, `bond_slaves`, `bridge_ports`, `vlan-raw-device`) |
| Running VMs/CTs and which bridge each NIC is on | PVE API `GET /nodes/{node}/qemu` + `/lxc` (status) and `.../{vmid}/config` (`netN: ...,bridge=vmbr0,tag=..`) |
| Live rx/tx per link | Netdata `net.net` per interface on that host's child: physical NIC / bond / bridge / `tapNNNiM` (VM) / `vethNNNiM` (CT) — tap/veth name encodes vmid + NIC index, so mapping is exact |
| Link state / speed | Netdata `net.operstate`, `net.speed` [VERIFY context names] |

The existing `network-flow.tsx` layout (NICs → host → bridges → guests) is reused; the new data
shape adds an outer "server" level. The current single-host view keeps working as the
"local server" case. PVE API calls are cached (inventory changes rarely: 30–60 s TTL);
only the rates come from Netdata at 1–5 s.

PVE API connection details: HTTPS to each host's `:8006`, token `user@pve!proxmenux` with
`PVEAuditor` on `/`, secret in a mode-600 file, server certificate pinned / internal CA.
This is **not** OPNsense or iLO, so it is within the TZ rules.

Effect on the VM/CT section: names, vmid, type, status, vCPU/maxmem come from the PVE API;
real CPU/RAM/IO/net usage still comes from Netdata `cgroup.*` (host view) as required.

## 5.2 First server sample (host `main`, 2026-11-14)

| Item | Result | Consequence |
|---|---|---|
| PVE | `pve-manager 9.2.2`, kernel `7.0.2-6-pve`, Debian 13 (trixie), `smartmontools 7.5`, `zfsutils-linux 2.4.2` | ProxMenux AppImage runs on cp313 (supported). Parent VM: Debian 13 recommended, msmtp debs from trixie. |
| `ssacli` | **not installed**, no internet | `hpssa` collector has no data until the HPE `ssacli` .deb is carried in the same way as the Netdata bundle. Without it: no controller / cache battery / array / logical-drive status and no RAID-degraded alert. |
| `smartctl --scan` | `/dev/sda`..`/dev/sdd`, type `scsi` | Either 4 HPE logical volumes (Smart Array in RAID mode) or 4 disks in HBA mode — not decidable from this output. `smartctl -i /dev/sda` shows vendor/product (`HPE LOGICAL VOLUME` = RAID mode). |
| `smartctl -d cciss,0` | not tested: typed `cciss,o` (letter o) instead of `cciss,0` | Re-run. If it works, physical disk SMART/temperature is available without `ssacli`. |
| `ipmitool` | not installed | **Not needed**: Netdata ships its own Go `ipmi.plugin` (setuid, reads `/dev/ipmi0` directly). Only the kernel driver `ipmi_si`/`ipmi_devintf` is required (in the PVE kernel). |
| lm-sensors | not checked | **Not needed**: Netdata `debugfs.plugin` has a vendored libsensors reading `/sys/class/hwmon`. |

Also confirmed in the Netdata source: `hpssa`, `smartctl` and `docker` go.d collectors are **enabled by
default** (no `Disabled` flag; `go.d.conf` `default_run: yes`), so a job file is enough.

Monitoring without `ssacli` (fallback if it can never be brought in):

| Need | Without ssacli |
|---|---|
| physical disk health, temperature, SMART attributes | `smartctl -d cciss,N` via the `smartctl` collector — **if** the controller driver (`smartpqi` on Gen10 P408i/P816i, `hpsa` on older) passes it through [VERIFY] |
| logical drive degraded / rebuilding, cache battery | **no source** in Netdata. Would need a small custom collector reading the controller — out of scope / needs a decision |
| failed physical disk | indirect: disappears from cciss scan or SMART failed |

`netdata-deploy/collect-hw-info.sh` gathers everything above in one read-only run (no installs).

## 5.3 Sandbox verification with real Netdata 2.12.3 (2026-11-14)

Done in a Linux container (not a PVE host, no RAID/IPMI hardware):

| Check | Result |
|---|---|
| `prepare-offline.sh` downloads the pinned v2.12.3 static build | ✅ (~330 MB). Two script bugs found and fixed: kickstart strips `/` from the target path; a failed download was reported as success |
| Offline install from the bundle (`install.sh --non-interactive --disable-telemetry --no-updates`) | ✅ `netdata v2.12.3`, type `kickstart-static` |
| Our `netdata.conf` accepted | ✅ ML off, registry off, dbengine tiers/retention as set, host labels present |
| Netdata Cloud | ✅ not connected: `cloud.status=available, reason "Agent is not claimed yet"` |
| Parent `stream.conf` (per-child UUID key) + child stream | ✅ child `srv-child` streamed to parent, link established |
| Host labels propagate to Parent | ✅ `/api/v3/nodes` shows `proxmenux_role`, `pve_host`, `pve_vmid` per node |
| Our health files load on the Parent | ✅ `proxmenux_cpu_usage`, `proxmenux_ram_usage`, `proxmenux_disk_space_usage` active; stock `10min_cpu_usage`, `ram_in_use`, `disk_space_usage` disabled; no health config errors logged. HPE/SMART templates load but have no instances without the hardware |
| One request for all nodes | ✅ `/api/v3/data?scope_nodes=*&contexts=system.cpu&group_by=node&points=1&format=json2` → one column per node UUID; `group_by=node,dimension` gives `used/cached/buffers/free` per node |

Not verified yet (needs real servers): hpssa, smartctl cciss, IPMI, cgroup names of VMs/CTs,
Docker, replication after a network cut, `streaming_disconnected` (fires only after the parent
has been up 30 min), email.

## 6. Open items

* `ssacli` .deb: user can bring files in → added to the bundle (`SSACLI_DEB=...`), installed by `install.sh --role host`.
* Output of `netdata-deploy/collect-hw-info.sh` from one host (ideally two).
* SMTP parameters from the partner.
* Parent VM/CT location and OS (Debian 12/13) on server 1.
* List of guests with Docker (where to install guest children).

## 7. Next step

On one DL380 Gen10 (no internet): install Netdata static offline, enable `hpssa`, `smartctl`
(with `cciss` devices), `ipmi`, `sensors`; dump `/api/v3/contexts` and `/api/v3/data` for those
contexts; fill the context table above with the real names/labels; measure child CPU/RAM.
