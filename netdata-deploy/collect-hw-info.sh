#!/usr/bin/env bash
# Read-only hardware/storage inventory for planning the Netdata collectors.
# Uses only tools that are already on a stock Proxmox VE 9 host
# (smartctl, lspci, lsblk, dmidecode, sysfs). Installs nothing, changes nothing.
#
# Usage:  ./collect-hw-info.sh > hw-info-$(hostname).txt 2>&1
#
# Optional: --try-ipmi loads the kernel IPMI driver (ipmi_si, ipmi_devintf) to see
# whether in-band IPMI works. This is the only change it can make; undo with
# `modprobe -r ipmi_devintf ipmi_si`.
#
# Part of the ProxMenux fork (GPL-3.0).

set -o nounset
TRY_IPMI=0
[ "${1:-}" = "--try-ipmi" ] && TRY_IPMI=1

sec() { printf '\n===== %s =====\n' "$*"; }
run() { printf '$ %s\n' "$*"; timeout 20 "$@" 2>&1; echo "[exit $?]"; }
show() { for f in "$@"; do [ -r "$f" ] && printf '%s = %s\n' "$f" "$(tr -d '\n' < "$f" 2>/dev/null)"; done; }

sec "system"
run hostname
run cat /etc/os-release
run uname -r
run dmidecode -s system-product-name
run dmidecode -s bios-version
run nproc
run free -g

sec "storage controllers (PCI)"
run sh -c "lspci -nnk | grep -iEA3 'raid|sas|sata|storage|nvme'"
run sh -c "lsmod | grep -E '^(hpsa|smartpqi|megaraid_sas|mpt3sas|ahci|nvme) '"

sec "SCSI hosts"
for h in /sys/class/scsi_host/host*; do
    show "$h/proc_name" "$h/model" "$h/vendor" "$h/firmware_revision" "$h/firmware_version" \
         "$h/driver_version" "$h/serial_number" "$h/ctrl_ready_timeout" "$h/rescan" 2>/dev/null
done

sec "block devices"
run lsblk -d -o NAME,SIZE,VENDOR,MODEL,TRAN,ROTA,TYPE,HCTL
for d in /sys/block/sd* /sys/block/nvme*; do
    [ -e "$d" ] || continue
    show "$d/device/vendor" "$d/device/model" "$d/device/raid_level" "$d/device/path_info" \
         "$d/device/lunid" "$d/device/unique_id" "$d/device/ssd_smart_path_enabled"
done

sec "smartctl (direct)"
run smartctl --version
run smartctl --scan-open
for dev in /dev/sd? /dev/nvme?n1; do
    [ -b "$dev" ] || continue
    run smartctl -i -H "$dev"
done

sec "smartctl through Smart Array (cciss,N)"
first=""; for d in /dev/sd?; do [ -b "$d" ] && { first="$d"; break; }; done
if [ -n "$first" ]; then
    for n in 0 1 2 3 4 5 6 7 8 9 10 11; do
        out="$(timeout 20 smartctl -i -H -A -d "cciss,$n" "$first" 2>&1)"
        printf '$ smartctl -i -H -A -d cciss,%s %s\n%s\n' "$n" "$first" "$out"
    done
fi

sec "IPMI"
run sh -c "ls -l /dev/ipmi* 2>&1"
run sh -c "lsmod | grep -i ipmi"
run sh -c "dmidecode -t 38 | head -20"
if [ "$TRY_IPMI" = 1 ]; then
    run modprobe ipmi_si
    run modprobe ipmi_devintf
    run sh -c "ls -l /dev/ipmi* 2>&1"
    run sh -c "dmesg | grep -i ipmi | tail -15"
fi

sec "hwmon sensors (sysfs, no lm-sensors needed)"
for h in /sys/class/hwmon/hwmon*; do
    printf '%s: %s\n' "$h" "$(cat "$h/name" 2>/dev/null)"
    for t in "$h"/temp*_input "$h"/fan*_input "$h"/power*_input; do
        [ -r "$t" ] && printf '  %s = %s\n' "$(basename "$t")" "$(cat "$t")"
    done
done

sec "storage stack"
run pvesm status
run zpool status
run sh -c "pvs; vgs; lvs"
run cat /proc/mdstat

sec "cgroups (VM/CT naming)"
run sh -c "ls /sys/fs/cgroup/qemu.slice/ 2>&1 | head -30"
run sh -c "ls /sys/fs/cgroup/lxc/ 2>&1 | head -30"

sec "network (for the network diagram)"
run ip -br link
run sh -c "bridge link 2>&1 | head -40"
run sh -c "grep -E '^(auto|iface|\s+(bridge-ports|bond-slaves|bond-mode|address|bridge-vlan-aware))' /etc/network/interfaces"

sec "done"
