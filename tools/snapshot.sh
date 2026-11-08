
# routedeck-minipc: read-only snapshot of a mini-PC router (PC1/PC2 type).
#
#   ssh HOST "tr -d '\r' | sudo bash -s" < tools/snapshot.sh > HOST.tgz 2> HOST.log
#
# Changes nothing on the host. The only write is a temp dir under /run,
# removed on exit. stdout = tar.gz with meta/ (inventory) plus the host's
# own files (not owned by any package, or package conffiles that were
# changed), with modes and owners. stderr = log lines.
# The archive CONTAINS SECRETS (keys, proxy configs): keep it private.
set -u
umask 077
export LC_ALL=C

SNAP_TMP=
trap 'if [ -n "${SNAP_TMP:-}" ]; then rm -rf -- "$SNAP_TMP"; fi' EXIT

snap_main() {
  local T M p s lim
  T=$(mktemp -d /run/routedeck-snap.XXXXXX) || { echo "snap: mktemp failed" >&2; return 1; }
  SNAP_TMP=$T
  M="$T/meta"
  mkdir -p "$M"
  echo "routedeck-snapshot v1" > "$M/VERSION"

  run() { local out=$1; shift; "$@" >"$M/$out" 2>&1 || true; }

  dmi() {
    local k
    for k in system-manufacturer system-product-name baseboard-manufacturer baseboard-product-name bios-vendor bios-version bios-release-date; do
      printf '%s=%s\n' "$k" "$(dmidecode -s "$k" 2>/dev/null)"
    done
  }
  nics() {
    local i
    for i in /sys/class/net/*; do
      i=${i##*/}
      [ -e "/sys/class/net/$i/device" ] || continue
      echo "== $i mac=$(cat "/sys/class/net/$i/address") driver=$(basename "$(readlink -f "/sys/class/net/$i/device/driver")") dev=$(readlink -f "/sys/class/net/$i/device")"
      ethtool -i "$i" 2>&1
      ethtool "$i" 2>&1 | grep -E 'Speed|Duplex|Link detected|Wake-on'
      udevadm info -q property -p "/sys/class/net/$i" 2>/dev/null | grep -E '^(ID_NET_NAME|ID_PATH=|ID_VENDOR_ID|ID_MODEL_ID|ID_NET_DRIVER)'
    done
  }
  modem() {
    local m
    mmcli -L 2>&1
    for m in $(mmcli -L 2>/dev/null | grep -o 'Modem/[0-9]*' | cut -d/ -f2); do
      mmcli -m "$m" -J 2>/dev/null | python3 -c '
import json, sys
g = json.load(sys.stdin)["modem"]["generic"]
keys = ("manufacturer", "model", "revision", "plugin", "drivers", "primary-port",
        "ports", "current-capabilities", "supported-capabilities")
print(json.dumps({k: g.get(k) for k in keys}, ensure_ascii=False, indent=1))
'
    done
  }
  hostkeys() { local f; for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done; }
  bins() {
    local f
    for f in /usr/local/bin/* /usr/local/sbin/* /usr/bin/sing-box /usr/bin/awg /usr/bin/awg-quick /usr/bin/xray /usr/bin/mihomo; do
      [ -f "$f" ] || continue
      [ "$(head -c 4 "$f" 2>/dev/null | tail -c 3)" = "ELF" ] || continue
      printf '%s %s %s pkg=%s\n' "$(sha256sum "$f" | cut -c1-64)" "$(stat -c %s "$f")" "$f" "$(dpkg -S "$f" 2>/dev/null | cut -d: -f1)"
    done
    echo "--- versions"
    for f in /usr/local/bin/mihomo /usr/bin/mihomo; do [ -x "$f" ] && "$f" -v 2>&1 | head -2; done
    for f in /usr/local/bin/xray /usr/bin/xray; do [ -x "$f" ] && "$f" version 2>&1 | head -2; done
    command -v sing-box >/dev/null && sing-box version 2>&1 | head -4
    command -v awg >/dev/null && awg --version 2>&1 | head -1
    modinfo amneziawg 2>/dev/null | grep -E '^(filename|version|srcversion|vermagic):'
    return 0
  }
  aptpol() {
    local p
    for p in amneziawg amneziawg-tools amneziawg-dkms sing-box xray mihomo isc-dhcp-server modemmanager libqmi-utils iptables iptables-persistent netfilter-persistent python3 python3-yaml curl ethtool linux-generic-hwe-22.04; do
      echo "== $p"
      apt-cache policy "$p" 2>/dev/null | head -8
    done
  }
  aptsrc() {
    local f
    cat /etc/apt/sources.list 2>/dev/null
    for f in /etc/apt/sources.list.d/*; do echo "== $f"; cat "$f"; done
  }
  conffiles() {
    dpkg-query -W -f '${Conffiles}\n' 2>/dev/null \
      | awk 'NF==2 && substr($1,1,1)=="/" {print $2 "  " $1}' \
      | md5sum -c 2>/dev/null | grep -E ': FAILED'
  }

  echo "snap: meta" >&2
  run date.txt date -u +%Y-%m-%dT%H:%M:%SZ
  run os-release.txt cat /etc/os-release
  run uname.txt uname -a
  run hostnamectl.txt hostnamectl
  run uptime.txt uptime
  run cmdline.txt cat /proc/cmdline
  run lscpu.txt lscpu
  run free.txt free -m
  run dmi.txt dmi
  run lspci.txt lspci -nnk
  run lsusb.txt lsusb
  run lsusb-tree.txt lsusb -t
  run lsblk.txt lsblk -o NAME,TYPE,FSTYPE,SIZE,MOUNTPOINT,MODEL
  run df.txt df -hT -x tmpfs -x devtmpfs -x squashfs -x overlay
  run lsmod.txt lsmod
  run dkms.txt dkms status
  run packages.tsv dpkg-query -W -f '${Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n'
  run apt-manual.txt apt-mark showmanual
  run apt-hold.txt apt-mark showhold
  run apt-policy.txt aptpol
  run apt-sources.txt aptsrc
  run snap.txt snap list
  run python.txt python3 -c 'import sys; print(sys.version)'
  run pip.txt python3 -m pip list --format=freeze
  run unit-files.txt systemctl list-unit-files --no-pager --no-legend
  run units.txt systemctl list-units --all --no-pager --no-legend --plain
  run failed.txt systemctl --failed --no-pager --no-legend --plain
  run timers.txt systemctl list-timers --all --no-pager --no-legend
  run crontab-root.txt crontab -l -u root
  run crontab-ubuntu.txt crontab -l -u ubuntu
  run passwd.txt getent passwd
  run group.txt getent group
  run nics.txt nics
  run modem.txt modem
  run ip-link.txt ip -d link show
  run ip-addr.txt ip addr show
  run ip-rule4.txt ip -4 rule show
  run ip-rule6.txt ip -6 rule show
  run ip-route.txt ip route show table all
  run networkctl.txt networkctl list --no-pager
  run netplan-get.txt netplan get
  run resolvectl.txt resolvectl status --no-pager
  run resolv-conf.txt ls -la /etc/resolv.conf
  run iptables-save.txt iptables-save
  run ip6tables-save.txt ip6tables-save
  run nft-ruleset.txt nft list ruleset
  run ss.txt ss -lntupH
  run ps.txt ps -eo user,pid,ppid,rss,etime,args --sort=args
  run journal-usage.txt journalctl --disk-usage
  run timedatectl.txt timedatectl
  run sysctl.txt sysctl -a
  run lsattr.txt lsattr -R /usr/local /etc/router
  run getfacl.txt getfacl -R -s -p /etc /usr/local /opt
  run root-ls.txt ls -la /root
  run home-ls.txt ls -la /home/ubuntu
  run ssh-hostkeys.txt hostkeys
  run binaries.txt bins
  echo "snap: package checks" >&2
  run conffiles-changed.txt conffiles
  run dpkg-verify.txt dpkg --verify

  echo "snap: file list" >&2
  cat /var/lib/dpkg/info/*.list 2>/dev/null | sort -u > "$T/owned"
  {
    find /etc /usr/local /opt /srv \( -path /etc/ssl/certs -o -path /etc/alternatives -o -path /etc/lvm/archive -o -path /etc/lvm/backup -o -path /etc/apparmor.d/cache -o -path /usr/local/share/man \) -prune -o \( -type f -o -type l -o -type d \) -print
    find /var/lib -mindepth 1 -maxdepth 1 \( -name 'router*' -o -name 'watchdog-rotator' -o -name 'tun-rotator' -o -name 'mihomo' -o -name 'sing-box' -o -name 'xray' -o -name 'amnezia*' \) -exec find {} \( -type f -o -type l -o -type d \) -print \;
    find /home/ubuntu \( -path /home/ubuntu/.cache -o -path /home/ubuntu/.local -o -path /home/ubuntu/snap -o -path /home/ubuntu/.vscode-server -o -path /home/ubuntu/go -o -path /home/ubuntu/.npm \) -prune -o \( -type f -o -type l -o -type d \) -print
    find /var/spool/cron/crontabs \( -type f -o -type d \) -print
  } 2>/dev/null | sort -u > "$T/cand"
  comm -23 "$T/cand" "$T/owned" > "$T/unowned"

  EXCL='^/etc/(shadow|gshadow|passwd|group|subuid|subgid)-?$|^/etc/\.pwd\.lock$|^/etc/machine-id$|^/etc/ld\.so\.cache$|^/etc/udev/hwdb\.bin$|^/etc/ssh/ssh_host_[a-z0-9]+_key$|^/etc/ssl/private/|^/etc/console-setup/cached|^/etc/mtab$|^/etc/adjtime$'
  CRUFT='(~|\.bak[^/]*|\.orig|\.old|\.dpkg-(old|dist|bak|new))$|/\.net-[a-z]+-[0-9a-f]{32}$'
  grep -E "$EXCL" "$T/unowned" > "$M/excluded.txt" || true
  grep -vE "$EXCL" "$T/unowned" > "$T/u2" || true
  grep -E "$CRUFT" "$T/u2" > "$M/cruft.txt" || true
  grep -vE "$CRUFT" "$T/u2" > "$T/keep" || true
  sed -n 's/: FAILED$//p' "$M/conffiles-changed.txt" > "$T/conf-mod"
  awk '$1 !~ /^missing/ && NF>=2 {print $NF}' "$M/dpkg-verify.txt" | grep '^/' > "$T/verify-mod" || true
  cat "$T/keep" "$T/conf-mod" "$T/verify-mod" | sort -u > "$T/all"

  : > "$T/final"
  : > "$M/big.txt"
  while IFS= read -r p; do
    if [ -L "$p" ] || [ -d "$p" ]; then echo "$p" >> "$T/final"; continue; fi
    [ -f "$p" ] || continue
    s=$(stat -c %s "$p")
    lim=26214400
    case "$p" in /var/lib/*|/home/*) lim=5242880 ;; esac
    case "$p" in *.dat|*.mmdb|*.metadb) lim=1048576 ;; esac
    case "$p" in
      *.tar|*.tar.*|*.tgz|*.zip|*.deb|*.gz|*.xz|*.zst|*.7z)
        case "$p" in /var/lib/*|/home/*|/opt/*) lim=0 ;; esac ;;
    esac
    if [ "$s" -gt "$lim" ]; then
      echo "$(sha256sum "$p" | cut -c1-64) $s $p" >> "$M/big.txt"
    else
      echo "$p" >> "$T/final"
    fi
  done < "$T/all"

  while IFS= read -r p; do
    if [ -L "$p" ]; then printf 'L %s %s -> %s\n' "$(stat -c '%a %U:%G' "$p")" "$p" "$(readlink "$p")"
    elif [ -d "$p" ]; then printf 'D %s %s\n' "$(stat -c '%a %U:%G' "$p")" "$p"
    else printf 'F %s %s %s\n' "$(stat -c '%a %U:%G %s' "$p")" "$(sha256sum "$p" | cut -c1-64)" "$p"
    fi
  done < "$T/final" > "$M/manifest.txt"

  echo "snap: $(wc -l < "$T/final") paths, $(wc -l < "$M/big.txt") big files listed only, $(wc -l < "$M/cruft.txt") cruft names listed only" >&2
  sed 's#^/##' "$T/final" > "$T/final.rel"
  tar --numeric-owner --no-recursion -cpf - -C / -T "$T/final.rel" --recursion -C "$T" meta 2>"$T/tar.err" | gzip -6
  sed 's/^/snap: tar: /' "$T/tar.err" >&2
  echo "snap: done" >&2
}

{
  snap_main
} </dev/null
exit 0
