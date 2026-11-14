"use client"

// ProxMenux fork (GPL-3.0): "All servers" view. Every Proxmox server that
// streams to the Netdata Parent, live, in one place. Data: /api/fleet/servers.

import React, { useEffect, useMemo, useState } from "react"
import useSWR from "swr"
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import {
  AlertTriangle, ArrowDown, ArrowUp, Boxes, Clock, Cpu, HardDrive, LayoutGrid, List,
  MemoryStick, Network, Search, Server, ServerOff, XCircle,
} from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"
import { Input } from "./ui/input"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "./ui/dialog"
import { fetchApi } from "../lib/api-config"
import { getStorageUsageColor } from "../lib/storage-usage-color"
import { useT } from "../lib/i18n/provider"

// ---------------------------------------------------------------- types
export interface FleetServer {
  id: string
  name: string
  pve_host: string
  status: "online" | "offline"
  last_seen: number | null
  netdata_version: string | null
  cpu: { percent: number | null; cores: number | null }
  memory: { used_bytes: number | null; total_bytes: number | null; cached_bytes: number | null; percent: number | null }
  network: { rx_bps: number | null; tx_bps: number | null }
  disk_io: { read_Bps: number | null; write_Bps: number | null }
  uptime_seconds: number | null
  load: (number | null)[] | null
  disks: { mount: string; used_bytes: number; total_bytes: number; percent: number }[]
  alerts: { critical: number; warning: number }
  guests:
    | { ok: true; qemu: { running: number; total: number }; lxc: { running: number; total: number } }
    | { ok: false; error: string }
    | null
}

interface FleetSnapshot {
  source: string
  parent: string
  generated_at: number
  refresh_seconds: number
  summary: { total: number; online: number; offline: number; alerts: { critical: number; warning: number } }
  servers: FleetServer[]
  other_nodes: string[]
  errors: string[]
}

// ---------------------------------------------------------------- formatting
const UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
export function fmtBytes(n: number | null | undefined, digits = 1): string {
  if (n == null || !isFinite(n)) return "—"
  let i = 0
  let v = Math.abs(n)
  while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${UNITS[i]}`
}
export function fmtBits(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return "—"
  const u = ["bit/s", "kbit/s", "Mbit/s", "Gbit/s", "Tbit/s"]
  let i = 0
  let v = Math.abs(n)
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i++ }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`
}
export function fmtUptime(s: number | null | undefined): string {
  if (s == null) return "—"
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
}
export function fmtAgo(ts: number | null | undefined, now: number): string {
  if (!ts) return "never"
  const s = Math.max(0, Math.round(now - ts))
  if (s < 90) return `${s}s ago`
  if (s < 5400) return `${Math.round(s / 60)}m ago`
  if (s < 172800) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

// good / warning / danger / no data — one palette for the whole view
type Level = "good" | "warning" | "danger" | "nodata"
const LEVEL_BADGE: Record<Level, string> = {
  good: "bg-green-500/10 text-green-500 border-green-500/20",
  warning: "bg-yellow-500/10 text-yellow-500 border-yellow-500/20",
  danger: "bg-red-500/10 text-red-500 border-red-500/20",
  nodata: "bg-gray-500/10 text-gray-400 border-gray-500/20",
}
function serverLevel(s: FleetServer): Level {
  if (s.status !== "online") return "danger"
  if (s.alerts.critical > 0) return "danger"
  if (s.alerts.warning > 0) return "warning"
  return "good"
}

// ---------------------------------------------------------------- persisted view state
function usePersisted<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(initial)
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(key)
      if (raw != null) setV(JSON.parse(raw))
    } catch { /* storage unavailable: keep default */ }
  }, [key])
  const set = (next: T) => {
    setV(next)
    try { window.localStorage.setItem(key, JSON.stringify(next)) } catch { /* ignore */ }
  }
  return [v, set]
}

const fetcher = (url: string) => fetchApi<FleetSnapshot>(url)

// ---------------------------------------------------------------- small pieces
function UsageBar({ percent }: { percent: number | null }) {
  const p = percent == null ? 0 : Math.min(100, Math.max(0, percent))
  const color = percent == null ? "bg-gray-500/40" : getStorageUsageColor(p).bgClass
  return (
    <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
      <div className={`h-full ${color} transition-all duration-500`} style={{ width: `${p}%` }} />
    </div>
  )
}

function StatTile({ label, value, Icon, tone }: { label: string; value: React.ReactNode; Icon: React.ComponentType<{ className?: string }>; tone?: string }) {
  return (
    <Card className="bg-card border-border">
      <CardContent className="p-4 flex items-center justify-between">
        <div>
          <div className="text-xs text-muted-foreground">{label}</div>
          <div className={`text-2xl font-semibold ${tone || ""}`}>{value}</div>
        </div>
        <Icon className="h-6 w-6 text-muted-foreground" />
      </CardContent>
    </Card>
  )
}

function GuestsText({ g }: { g: FleetServer["guests"] }) {
  const t = useT()
  if (!g) return <span className="text-muted-foreground">—</span>
  if (!g.ok) return <span className="text-yellow-500" title={g.error}>{t("fleet.pveError")}</span>
  return (
    <span>
      {g.qemu.running}/{g.qemu.total} VM · {g.lxc.running}/{g.lxc.total} CT
    </span>
  )
}

function Row({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground flex items-center gap-1 shrink-0">{label}</span>
      <span className="font-mono whitespace-nowrap truncate text-right">{children}</span>
    </div>
  )
}

function ServerCard({ s, now, onOpen }: { s: FleetServer; now: number; onOpen: () => void }) {
  const t = useT()
  const level = serverLevel(s)
  const online = s.status === "online"
  const worstDisk = s.disks[0]
  return (
    <Card
      className={`bg-card border-border cursor-pointer hover:border-blue-500/50 transition-colors ${online ? "" : "opacity-80"}`}
      onClick={onOpen}
    >
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-base font-semibold flex items-center gap-2 min-w-0">
          {online ? <Server className="h-4 w-4 shrink-0" /> : <ServerOff className="h-4 w-4 shrink-0 text-red-500" />}
          <span className="truncate">{s.name}</span>
        </CardTitle>
        <div className="flex items-center gap-1.5">
          {s.alerts.critical > 0 && <Badge variant="outline" className={LEVEL_BADGE.danger}>{s.alerts.critical}</Badge>}
          {s.alerts.warning > 0 && <Badge variant="outline" className={LEVEL_BADGE.warning}>{s.alerts.warning}</Badge>}
          <Badge variant="outline" className={LEVEL_BADGE[online ? level : "danger"]}>
            {online ? t("fleet.online") : t("fleet.offline")}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!online && (
          <div className="text-xs text-red-400 flex items-center gap-1">
            <Clock className="h-3 w-3" /> {t("fleet.lastData")}: {fmtAgo(s.last_seen, now)}
          </div>
        )}
        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-muted-foreground flex items-center gap-1"><Cpu className="h-3 w-3" />CPU</span>
            <span className="font-mono">{s.cpu.percent == null ? "—" : `${s.cpu.percent.toFixed(1)} %`}{s.cpu.cores ? <span className="text-muted-foreground"> · {s.cpu.cores} c</span> : null}</span>
          </div>
          <UsageBar percent={s.cpu.percent} />
        </div>
        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-muted-foreground flex items-center gap-1"><MemoryStick className="h-3 w-3" />RAM</span>
            <span className="font-mono">{fmtBytes(s.memory.used_bytes)} / {fmtBytes(s.memory.total_bytes)}</span>
          </div>
          <UsageBar percent={s.memory.percent} />
        </div>
        {worstDisk && (
          <div>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-muted-foreground flex items-center gap-1 min-w-0"><HardDrive className="h-3 w-3 shrink-0" /><span className="truncate">{worstDisk.mount}</span></span>
              <span className="font-mono">{worstDisk.percent.toFixed(0)} %</span>
            </div>
            <UsageBar percent={worstDisk.percent} />
          </div>
        )}
        <div className="space-y-1 text-xs">
          <Row label={<><Network className="h-3 w-3" />{t("fleet.network")}</>}>
            <ArrowDown className="inline h-3 w-3 text-green-500" />{fmtBits(s.network.rx_bps)}{" "}
            <ArrowUp className="inline h-3 w-3 text-blue-500" />{fmtBits(s.network.tx_bps)}
          </Row>
          <Row label={<><HardDrive className="h-3 w-3" />{t("fleet.diskIo")}</>}>
            R {fmtBytes(s.disk_io.read_Bps)}/s · W {fmtBytes(s.disk_io.write_Bps)}/s
          </Row>
          <Row label={<><Boxes className="h-3 w-3" />{t("fleet.guests")}</>}><GuestsText g={s.guests} /></Row>
          <Row label={<><Clock className="h-3 w-3" />{t("fleet.uptime")}</>}>
            {fmtUptime(s.uptime_seconds)}{s.load?.[0] != null ? ` · load ${s.load[0]!.toFixed(2)}` : ""}
          </Row>
        </div>
      </CardContent>
    </Card>
  )
}

type SortKey = "name" | "status" | "cpu" | "ram" | "rx" | "tx" | "io" | "uptime" | "alerts"
function sortValue(s: FleetServer, k: SortKey): number | string {
  switch (k) {
    case "name": return s.name
    case "status": return s.status === "online" ? 1 : 0
    case "cpu": return s.cpu.percent ?? -1
    case "ram": return s.memory.percent ?? -1
    case "rx": return s.network.rx_bps ?? -1
    case "tx": return s.network.tx_bps ?? -1
    case "io": return (s.disk_io.read_Bps ?? 0) + (s.disk_io.write_Bps ?? 0)
    case "uptime": return s.uptime_seconds ?? -1
    case "alerts": return s.alerts.critical * 1000 + s.alerts.warning
  }
}

function ServerTable({ servers, now, onOpen }: { servers: FleetServer[]; now: number; onOpen: (s: FleetServer) => void }) {
  const t = useT()
  const [sort, setSort] = usePersisted<{ key: SortKey; dir: 1 | -1 }>("proxmenux-fleet-sort", { key: "name", dir: 1 })
  const rows = useMemo(() => [...servers].sort((a, b) => {
    const va = sortValue(a, sort.key), vb = sortValue(b, sort.key)
    return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir
  }), [servers, sort])
  const th = (key: SortKey, label: string, right = false) => (
    <th
      className={`px-3 py-2 font-medium cursor-pointer select-none whitespace-nowrap ${right ? "text-right" : "text-left"}`}
      onClick={() => setSort({ key, dir: sort.key === key ? (sort.dir === 1 ? -1 : 1) : (key === "name" ? 1 : -1) })}
    >
      {label}{sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
    </th>
  )
  return (
    <Card className="bg-card border-border overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-muted-foreground text-xs">
            <tr>
              {th("name", t("fleet.server"))}{th("status", t("fleet.status"))}{th("cpu", "CPU", true)}{th("ram", "RAM", true)}
              {th("rx", "RX", true)}{th("tx", "TX", true)}{th("io", t("fleet.diskIo"), true)}{th("uptime", t("fleet.uptime"), true)}
              <th className="px-3 py-2 text-right font-medium">{t("fleet.guests")}</th>{th("alerts", t("fleet.alerts"), true)}
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const online = s.status === "online"
              return (
                <tr key={s.id} className="border-t border-border hover:bg-muted/30 cursor-pointer" onClick={() => onOpen(s)}>
                  <td className="px-3 py-2 font-medium">{s.name}</td>
                  <td className="px-3 py-2">
                    <Badge variant="outline" className={LEVEL_BADGE[online ? serverLevel(s) : "danger"]}>
                      {online ? t("fleet.online") : `${t("fleet.offline")} · ${fmtAgo(s.last_seen, now)}`}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-right font-mono">{s.cpu.percent == null ? "—" : `${s.cpu.percent.toFixed(1)} %`}</td>
                  <td className="px-3 py-2 text-right font-mono">{s.memory.percent == null ? "—" : `${s.memory.percent.toFixed(0)} % · ${fmtBytes(s.memory.used_bytes)}`}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtBits(s.network.rx_bps)}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtBits(s.network.tx_bps)}</td>
                  <td className="px-3 py-2 text-right font-mono">{online ? `${fmtBytes((s.disk_io.read_Bps ?? 0) + (s.disk_io.write_Bps ?? 0))}/s` : "—"}</td>
                  <td className="px-3 py-2 text-right font-mono">{fmtUptime(s.uptime_seconds)}</td>
                  <td className="px-3 py-2 text-right font-mono"><GuestsText g={s.guests} /></td>
                  <td className="px-3 py-2 text-right">
                    {s.alerts.critical > 0 && <Badge variant="outline" className={`${LEVEL_BADGE.danger} mr-1`}>{s.alerts.critical}</Badge>}
                    {s.alerts.warning > 0 && <Badge variant="outline" className={LEVEL_BADGE.warning}>{s.alerts.warning}</Badge>}
                    {s.alerts.critical + s.alerts.warning === 0 && <span className="text-muted-foreground">0</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

// ---------------------------------------------------------------- detail dialog
interface History { metric: string; range: string; units: string; times: number[]; series: { name: string; values: (number | null)[] }[] }
const METRICS = [
  { key: "cpu", label: "CPU" },
  { key: "ram", label: "RAM" },
  { key: "net", label: "Network" },
  { key: "io", label: "Disk I/O" },
  { key: "load", label: "Load" },
] as const
const RANGES = ["5m", "1h", "24h", "7d"] as const
const SERIES_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16", "#64748b"]

function fmtValue(v: number, units: string): string {
  if (units === "%") return `${v.toFixed(1)} %`
  if (units === "bytes") return fmtBytes(v)
  if (units === "bytes/s") return `${fmtBytes(v)}/s`
  if (units === "bits/s") return fmtBits(v)
  return v.toFixed(2)
}

function ServerDetail({ server, refreshSeconds, onClose }: { server: FleetServer | null; refreshSeconds: number; onClose: () => void }) {
  const t = useT()
  const [metric, setMetric] = usePersisted<string>("proxmenux-fleet-detail-metric", "cpu")
  const [range, setRange] = usePersisted<string>("proxmenux-fleet-detail-range", "5m")
  const url = server ? `/api/fleet/servers/${server.id}/history?metric=${metric}&range=${range}` : null
  const { data, error } = useSWR<History>(url, (u: string) => fetchApi<History>(u), {
    refreshInterval: range === "5m" ? refreshSeconds * 1000 : 30000,
    keepPreviousData: true,
    revalidateOnFocus: false,
  })
  const rows = useMemo(() => {
    if (!data) return []
    return data.times.map((ts, i) => {
      const r: Record<string, number | null> = { ts }
      data.series.forEach((s) => { r[s.name] = s.values[i] })
      return r
    })
  }, [data])
  const fmtTick = (ts: number) => {
    const d = new Date(ts * 1000)
    return range === "7d" ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" }) :
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", ...(range === "5m" ? { second: "2-digit" } : {}) })
  }
  return (
    <Dialog open={!!server} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-4xl bg-card border-border">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Server className="h-5 w-5" /> {server?.name}
            {server && (
              <Badge variant="outline" className={LEVEL_BADGE[server.status === "online" ? serverLevel(server) : "danger"]}>
                {server.status === "online" ? t("fleet.online") : t("fleet.offline")}
              </Badge>
            )}
          </DialogTitle>
        </DialogHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-1">
            {METRICS.map((m) => (
              <Button key={m.key} size="sm" variant={metric === m.key ? "default" : "outline"} onClick={() => setMetric(m.key)}>{m.label}</Button>
            ))}
          </div>
          <div className="flex gap-1">
            {RANGES.map((r) => (
              <Button key={r} size="sm" variant={range === r ? "default" : "outline"} onClick={() => setRange(r)}>{r === "5m" ? t("fleet.live") : r}</Button>
            ))}
          </div>
        </div>
        {error && !data && <div className="text-sm text-red-500">{(error as Error).message}</div>}
        <div className="h-72 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={rows} margin={{ left: 10, right: 10, top: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="currentColor" className="text-border" />
              <XAxis dataKey="ts" tickFormatter={fmtTick} stroke="currentColor" className="text-xs text-muted-foreground" minTickGap={40} />
              <YAxis width={80} stroke="currentColor" className="text-xs text-muted-foreground"
                tickFormatter={(v: number) => (data ? fmtValue(v, data.units) : String(v))} />
              <Tooltip
                contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 12 }}
                labelFormatter={(ts: number) => new Date(ts * 1000).toLocaleString()}
                formatter={(v: number, name: string) => [data ? fmtValue(v, data.units) : v, name]}
              />
              {data?.series.map((s, i) => (
                <Area key={s.name} type="monotone" dataKey={s.name} isAnimationActive={false}
                  stackId={data.metric === "cpu" || data.metric === "ram" ? "a" : undefined}
                  stroke={SERIES_COLORS[i % SERIES_COLORS.length]} fill={SERIES_COLORS[i % SERIES_COLORS.length]} fillOpacity={0.15} />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        </div>
        {server && server.disks.length > 0 && (
          <div className="space-y-2">
            <div className="text-sm font-medium">{t("fleet.filesystems")}</div>
            {server.disks.map((d) => (
              <div key={d.mount} className="text-xs">
                <div className="flex justify-between mb-1">
                  <span className="font-mono truncate">{d.mount}</span>
                  <span className="font-mono">{fmtBytes(d.used_bytes)} / {fmtBytes(d.total_bytes)} · {d.percent.toFixed(0)} %</span>
                </div>
                <UsageBar percent={d.percent} />
              </div>
            ))}
          </div>
        )}
        <div className="text-xs text-muted-foreground">{t("fleet.sourceNote")}</div>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------- main view
export function FleetServers() {
  const t = useT()
  const [view, setView] = usePersisted<"cards" | "table">("proxmenux-fleet-view", "cards")
  const [query, setQuery] = usePersisted<string>("proxmenux-fleet-query", "")
  const [statusFilter, setStatusFilter] = usePersisted<"all" | "online" | "offline">("proxmenux-fleet-status", "all")
  const [openId, setOpenId] = useState<string | null>(null)
  const [refreshMs, setRefreshMs] = useState(2000)
  const { data, error, isLoading } = useSWR<FleetSnapshot>("/api/fleet/servers", fetcher, {
    refreshInterval: refreshMs,
    keepPreviousData: true,
    revalidateOnFocus: false,
    dedupingInterval: 500,
  })
  useEffect(() => {
    if (data?.refresh_seconds) setRefreshMs(Math.round(data.refresh_seconds * 1000))
  }, [data?.refresh_seconds])

  const now = data?.generated_at ?? Date.now() / 1000
  const servers = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (data?.servers ?? []).filter((s) =>
      (statusFilter === "all" || s.status === statusFilter) &&
      (!q || s.name.toLowerCase().includes(q) || s.pve_host.toLowerCase().includes(q)))
  }, [data, query, statusFilter])
  const open = data?.servers.find((s) => s.id === openId) ?? null

  if (isLoading && !data) {
    return <div className="text-sm text-muted-foreground p-6">{t("fleet.loading")}</div>
  }

  return (
    <div className="space-y-4">
      {error && (
        <Card className="bg-red-500/10 border-red-500/20">
          <CardContent className="p-4 flex items-start gap-3 text-red-500">
            <XCircle className="h-5 w-5 mt-0.5 shrink-0" />
            <div className="text-sm">
              <div className="font-semibold">{t("fleet.errorTitle")}</div>
              <div>{(error as Error).message}</div>
              {data && <div className="text-xs mt-1 opacity-80">{t("fleet.showingLast")} {fmtAgo(data.generated_at, Date.now() / 1000)}</div>}
            </div>
          </CardContent>
        </Card>
      )}
      {data && data.errors.length > 0 && (
        <Card className="bg-yellow-500/10 border-yellow-500/20">
          <CardContent className="p-3 text-xs text-yellow-500 flex gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <div>{data.errors.join(" · ")}</div>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatTile label={t("fleet.servers")} value={data.summary.total} Icon={Server} />
            <StatTile label={t("fleet.online")} value={data.summary.online} Icon={Server} tone="text-green-500" />
            <StatTile label={t("fleet.offline")} value={data.summary.offline} Icon={ServerOff}
              tone={data.summary.offline > 0 ? "text-red-500" : ""} />
            <StatTile label={t("fleet.alerts")} Icon={AlertTriangle}
              value={<span><span className={data.summary.alerts.critical ? "text-red-500" : ""}>{data.summary.alerts.critical}</span>
                <span className="text-muted-foreground text-base"> / </span>
                <span className={data.summary.alerts.warning ? "text-yellow-500" : ""}>{data.summary.alerts.warning}</span></span>} />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("fleet.search")} className="pl-8" />
            </div>
            <div className="flex gap-1">
              {(["all", "online", "offline"] as const).map((f) => (
                <Button key={f} size="sm" variant={statusFilter === f ? "default" : "outline"} onClick={() => setStatusFilter(f)}>
                  {t(`fleet.filter.${f}`)}
                </Button>
              ))}
            </div>
            <div className="flex gap-1">
              <Button size="sm" variant={view === "cards" ? "default" : "outline"} onClick={() => setView("cards")} aria-label="Cards"><LayoutGrid className="h-4 w-4" /></Button>
              <Button size="sm" variant={view === "table" ? "default" : "outline"} onClick={() => setView("table")} aria-label="Table"><List className="h-4 w-4" /></Button>
            </div>
          </div>

          {data.servers.length === 0 ? (
            <Card className="bg-card border-border">
              <CardContent className="p-6 text-sm text-muted-foreground">
                {t("fleet.noServers")}
                {data.other_nodes.length > 0 && <div className="mt-2 text-xs">{t("fleet.otherNodes")}: {data.other_nodes.join(", ")}</div>}
              </CardContent>
            </Card>
          ) : view === "cards" ? (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 xl:gap-4">
              {servers.map((s) => <ServerCard key={s.id} s={s} now={now} onOpen={() => setOpenId(s.id)} />)}
            </div>
          ) : (
            <ServerTable servers={servers} now={now} onOpen={(s) => setOpenId(s.id)} />
          )}
          <div className="text-xs text-muted-foreground">
            {t("fleet.footer", { parent: data.parent, seconds: data.refresh_seconds })}
          </div>
        </>
      )}

      <ServerDetail server={open} refreshSeconds={data?.refresh_seconds ?? 2} onClose={() => setOpenId(null)} />
    </div>
  )
}
