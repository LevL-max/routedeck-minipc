#!/usr/bin/env bash
# routedeck-minipc installer: turns a fresh Ubuntu 22.04 (amd64) box with two
# Ethernet ports into a Mini-PC router (modes DIRECT/AWG/TUN/MIHOMO, RouteDeck
# panel, Ethernet + 4G failover). See README.md.
#
#   sudo ./install.sh --profile DIR               # your box: DIR has host.env, secrets.tar.gpg
#   sudo ./install.sh --host example --no-secrets # a plain router from the template, configs later
#   sudo ./install.sh ... --dry-run               # show what would change, change nothing
#   sudo ./install.sh --check                     # health check after the reboot
#
# Options:
#   --profile DIR    host profile: DIR/host.env, optional DIR/authorized_keys and DIR/secrets.tar.gpg
#   --host NAME      same as --profile hosts/NAME inside this repository (e.g. example)
#   --wan IF         WAN port (default: the port that has the default route)
#   --lan IF         LAN port (default: the other Ethernet port)
#   --secrets FILE   secrets bundle (default DIR/secrets.tar.gpg; a plain .tar works too)
#   --no-secrets     install without keys/configs; mode is forced to direct
#   --mode MODE      mode after install: direct | awg | tun | mihomo (default: profile)
#   --yes            do not ask questions (ports must be clear)
#   --reboot         reboot at the end without asking
set -Eeuo pipefail
umask 022
export LC_ALL=C

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS="$(date -u +%Y%m%d-%H%M%S)"
BACKUP="/var/backups/routedeck/install-$TS"
DRY=0 YES=0 CHECK=0 REBOOT=0 NO_SECRETS=0
HOST="" PROFILE_DIR="" PROFILE_NAME="" WAN="" LAN="" SECRETS="" MODE=""
CLEAN=()

say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() {
  local d
  for d in "${CLEAN[@]:-}"; do
    if [ -n "$d" ]; then rm -rf -- "$d"; fi
  done
  return 0
}
trap cleanup EXIT
trap 'warn "failed at line $LINENO: $BASH_COMMAND"; [ -d "$BACKUP" ] && warn "saved originals and undo scripts: $BACKUP"' ERR

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }
ask() {
  [ "$YES" = 1 ] && return 0
  local a=""
  read -r -p "$1 [y/N] " a </dev/tty || return 1
  case "$a" in y|Y|yes|д|Д|да) return 0 ;; *) return 1 ;; esac
}
sha_of() { sha256sum "$1" | cut -c1-64; }
in_list() { local x=$1 y; shift; for y in "$@"; do [ "$x" = "$y" ] && return 0; done; return 1; }
profile_get() { sed -n "s/^$1=//p" "$HOST_ENV" | sed -n '$p' | sed 's/^"\(.*\)"$/\1/'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST=${2:?}; shift 2 ;;
    --profile) PROFILE_DIR=${2:?}; shift 2 ;;
    --wan) WAN=${2:?}; shift 2 ;;
    --lan) LAN=${2:?}; shift 2 ;;
    --secrets) SECRETS=${2:?}; shift 2 ;;
    --no-secrets) NO_SECRETS=1; shift ;;
    --mode) MODE=${2:?}; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --reboot) REBOOT=1; shift ;;
    --check) CHECK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

preflight() {
  [ "$(id -u)" = 0 ] || die "run as root: sudo $0 $*"
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 22.04 ] || die "needs Ubuntu 22.04, found ${PRETTY_NAME:-unknown}"
  [ "$(dpkg --print-architecture)" = amd64 ] || die "needs amd64"
  command -v python3 >/dev/null || die "python3 is missing"
  [ -f "$REPO/rootfs.manifest" ] && [ -f "$REPO/lib/render.py" ] || die "repository is incomplete"
}

load_profile() {
  if [ -n "$HOST" ] && [ -n "$PROFILE_DIR" ]; then die "use --host or --profile, not both"; fi
  [ -n "$HOST" ] && PROFILE_DIR="$REPO/hosts/$HOST"
  [ -n "$PROFILE_DIR" ] || die "--profile DIR or --host NAME is required (template: --host example)"
  PROFILE_DIR=$(cd "$PROFILE_DIR" 2>/dev/null && pwd) || die "no such profile directory"
  HOST_ENV="$PROFILE_DIR/host.env"
  [ -f "$HOST_ENV" ] || die "no host.env in $PROFILE_DIR"
  PROFILE_NAME=$(profile_get PROFILE)
  [ -n "$PROFILE_NAME" ] || PROFILE_NAME=$(basename "$PROFILE_DIR")
  [ -n "$MODE" ] || MODE=$(profile_get MODE)
  if [ "$NO_SECRETS" = 1 ] && [ "$MODE" != direct ]; then
    warn "--no-secrets: no AWG/proxy configs, so the mode becomes direct"
    MODE=direct
  fi
  case "$MODE" in direct|awg|tun|mihomo) ;; *) die "bad mode $MODE" ;; esac
}

ethernet_ports() {
  local d n drv
  for d in /sys/class/net/*; do
    n=${d##*/}
    [ -e "$d/device" ] || continue
    [ "$(cat "$d/type" 2>/dev/null)" = 1 ] || continue
    if [ -e "$d/wireless" ] || [ -e "$d/phy80211" ]; then continue; fi
    drv=$(basename "$(readlink -f "$d/device/driver")")
    case "$drv" in qmi_wwan|cdc_mbim|option|rndis_host) continue ;; esac
    echo "$n"
  done
}

pick_ports() {
  local all=() rest=() n def
  mapfile -t all < <(ethernet_ports)
  [ "${#all[@]}" -ge 2 ] || die "need two Ethernet ports, found ${#all[@]}: ${all[*]:-none}"
  say "Ethernet ports"
  for n in "${all[@]}"; do
    info "$(printf '%-14s mac %s  driver %-8s link %-4s ipv4 %s' "$n" "$(cat "/sys/class/net/$n/address")" \
      "$(basename "$(readlink -f "/sys/class/net/$n/device/driver")")" \
      "$(sed 's/^1$/up/;s/^0$/down/' "/sys/class/net/$n/carrier" 2>/dev/null || echo '?')" \
      "$(ip -4 -o addr show dev "$n" | awk '{print $4}' | tr '\n' ' ')")"
  done
  def=$(ip -4 route show default 2>/dev/null | awk '!found {for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); found = 1; break}}' || true)
  [ -n "$WAN" ] || WAN=$def
  [ -n "$WAN" ] || die "cannot tell the WAN port: plug the uplink cable in, or pass --wan"
  if [ -z "$LAN" ]; then
    for n in "${all[@]}"; do
      if [ "$n" != "$WAN" ]; then rest+=("$n"); fi
    done
    [ "${#rest[@]}" -eq 1 ] || die "more than two ports: pass --lan (one of: ${rest[*]})"
    LAN=${rest[0]}
  fi
  in_list "$WAN" "${all[@]}" || die "WAN $WAN is not an Ethernet port of this box"
  in_list "$LAN" "${all[@]}" || die "LAN $LAN is not an Ethernet port of this box"
  [ "$WAN" != "$LAN" ] || die "WAN and LAN must be different ports"
  info "WAN (internet, DHCP client)     : $WAN"
  info "LAN (clients, 192.168.50.1/24)  : $LAN"
  info "profile $PROFILE_NAME ($(profile_get DEVICE)) from $PROFILE_DIR, mode after install: $MODE"
  [ "$DRY" = 1 ] || ask "Continue with these ports?" || die "stopped"
}

packages() {
  local list missing=() p
  list=$(grep -vE '^[[:space:]]*(#|$)' "$REPO/lock/packages.txt" | tr '\n' ' ')
  say "Packages"
  for p in $list; do
    case "$(dpkg-query -W -f '${db:Status-Abbrev}' "$p" 2>/dev/null || true)" in ii*) ;; *) missing+=("$p") ;; esac
  done
  if [ "${#missing[@]}" -eq 0 ]; then info "all $(echo "$list" | wc -w) packages present"; fi
  if [ "$DRY" = 1 ]; then
    [ "${#missing[@]}" -eq 0 ] || info "would install: ${missing[*]}"
    if apt-mark showauto 2>/dev/null | grep -x modemmanager >/dev/null; then
      info "would mark modemmanager as manual (now auto: apt autoremove would remove 4G support)"
    fi
    return 0
  fi
  install -m 644 "$REPO/rootfs/etc/apt/sources.list.d/amnezia-ubuntu-ppa-jammy.list" /etc/apt/sources.list.d/
  install -m 644 "$REPO/rootfs/etc/apt/trusted.gpg.d/amnezia-ubuntu-ppa.gpg" /etc/apt/trusted.gpg.d/
  printf '%s\n' 'iptables-persistent iptables-persistent/autosave_v4 boolean false' \
                'iptables-persistent iptables-persistent/autosave_v6 boolean false' | debconf-set-selections
  if [ "${#missing[@]}" -gt 0 ]; then
    info "installing: ${missing[*]}"
    # Keep new daemons (dhcpd, ModemManager) stopped until the files are in place and the box reboots.
    if [ ! -e /usr/sbin/policy-rc.d ]; then
      printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
      chmod 755 /usr/sbin/policy-rc.d
      CLEAN+=(/usr/sbin/policy-rc.d)
    fi
    DEBIAN_FRONTEND=noninteractive apt-get update -q
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o Dpkg::Options::=--force-confold $list
    rm -f /usr/sbin/policy-rc.d
  fi
  apt-mark manual modemmanager libqmi-utils usb-modeswitch >/dev/null
  dkms status 2>/dev/null | grep '^amneziawg.*installed' >/dev/null || warn "amneziawg DKMS module is not built yet (check after the reboot)"
}

read_lock() {
  local row
  row=$(awk -F'\t' -v n="$1" '$1 == n' "$REPO/lock/binaries.lock")
  [ -n "$row" ] || die "lock entry $1 missing"
  IFS=$'\t' read -r _ L_VER L_ASSET L_URL L_ASHA L_BSHA L_DEST <<<"$row"
}

download() {   # name -> $WORK/<asset>; returns 1 on any failure
  read_lock "$1"
  local out="$WORK/$L_ASSET"
  if [ -f "$out" ] && echo "$L_ASHA  $out" | sha256sum -c --quiet >/dev/null 2>&1; then return 0; fi
  info "download $L_ASSET ($L_VER)"
  curl -fsSL --retry 3 --connect-timeout 20 -o "$out.part" "$L_URL" || return 1
  echo "$L_ASHA  $out.part" | sha256sum -c --quiet >/dev/null 2>&1 || { warn "$L_ASSET: sha256 mismatch"; return 1; }
  mv "$out.part" "$out"
}

installed_json() {   # component version sha
  install -d -o root -g root -m 700 /var/lib/router-updater "/var/lib/router-updater/$1"
  printf '{\n  "version": "%s",\n  "sha256": "%s"\n}' "$2" "$3" > "/var/lib/router-updater/$1/installed.json"
  chmod 600 "/var/lib/router-updater/$1/installed.json"
}

binaries() {
  say "Binaries (lock/binaries.lock)"
  local name have
  if [ "$DRY" = 1 ]; then
    for name in mihomo sing-box-lx xray; do
      read_lock "$name"
      have=$([ -f "$L_DEST" ] && sha_of "$L_DEST" || echo none)
      if [ "$have" = "$L_BSHA" ]; then info "$name $L_VER present"; else info "would install $name $L_VER"; fi
    done
    if dpkg-divert --list /usr/bin/sing-box 2>/dev/null | grep sing-box.distrib >/dev/null; then
      info "sing-box package diversion present"
    else
      info "would install the sing-box package and divert its binary"
    fi
    return 0
  fi
  WORK=$(mktemp -d /var/tmp/routedeck-work.XXXXXX); CLEAN+=("$WORK")

  # sing-box package: user, units, /etc/sing-box. Its binary is diverted; the LX build replaces it.
  read_lock sing-box-deb
  if [ -n "$(dpkg-query -W -f '${Version}' sing-box 2>/dev/null || true)" ]; then
    info "sing-box package present"
  else
    download sing-box-deb || die "sing-box package download failed"
    dpkg -i "$WORK/$L_ASSET" >/dev/null
  fi
  systemctl disable --now sing-box.service >/dev/null 2>&1 || true
  dpkg-divert --list /usr/bin/sing-box | grep sing-box.distrib >/dev/null ||
    dpkg-divert --local --rename --divert /usr/bin/sing-box.distrib --add /usr/bin/sing-box >/dev/null

  read_lock sing-box-lx
  if [ -f "$L_DEST" ] && [ "$(sha_of "$L_DEST")" = "$L_BSHA" ]; then
    info "sing-box LX $L_VER present"
  else
    download sing-box-lx || die "sing-box LX download failed"
    tar -xzf "$WORK/$L_ASSET" -C "$WORK"
    local bin; bin=$(find "$WORK" -type f -name sing-box -path '*linux-amd64*' -print -quit)
    [ -n "$bin" ] && [ "$(sha_of "$bin")" = "$L_BSHA" ] || die "sing-box LX binary sha256 mismatch"
    install -o root -g root -m 755 "$bin" "$L_DEST"
  fi
  installed_json sing-box "$L_VER" "$L_BSHA"

  read_lock mihomo
  if [ -f "$L_DEST" ] && [ "$(sha_of "$L_DEST")" = "$L_BSHA" ]; then
    info "mihomo $L_VER present"
  else
    download mihomo || die "mihomo download failed"
    gunzip -c "$WORK/$L_ASSET" > "$WORK/mihomo"
    [ "$(sha_of "$WORK/mihomo")" = "$L_BSHA" ] || die "mihomo binary sha256 mismatch"
    install -o root -g root -m 755 "$WORK/mihomo" "$L_DEST"
  fi
  installed_json mihomo "$L_VER" "$L_BSHA"

  # xray as router-xray keeps it: versions/<tag>/xray + verified.json, /usr/local/bin/xray -> it
  read_lock xray
  local vdir; vdir=$(dirname "$L_DEST")
  install -d -o root -g root -m 755 /var/lib/router-xray /var/lib/router-xray/versions "$vdir"
  if [ -f "$L_DEST" ] && [ "$(sha_of "$L_DEST")" = "$L_BSHA" ]; then
    info "xray $L_VER present"
  else
    download xray || die "xray download failed"
    rm -rf "$WORK/xray"; mkdir -p "$WORK/xray"
    unzip -q -o "$WORK/$L_ASSET" xray -d "$WORK/xray"
    [ "$(sha_of "$WORK/xray/xray")" = "$L_BSHA" ] || die "xray binary sha256 mismatch"
    install -o root -g root -m 755 "$WORK/xray/xray" "$L_DEST"
  fi
  if [ ! -f "$vdir/verified.json" ]; then
    python3 - "$vdir/verified.json" "$L_VER" "$L_ASHA" "$L_BSHA" "$("$L_DEST" version 2>&1 | sed -n 1p)" <<'PYEOF'
import json, os, sys, time
path, tag, asha, bsha, version = sys.argv[1:6]
data = {'tag': tag, 'prerelease': False, 'asset_sha256': asha, 'binary_sha256': bsha,
        'downloaded_at': int(time.time()), 'version': version}
with open(path, 'w') as fh:
    fh.write(json.dumps(data, indent=2) + '\n')
os.chmod(path, 0o600)
PYEOF
  fi
  ln -sfn "$L_DEST" /usr/local/bin/xray

  # Mihomo web UI (optional): Zashboard static files
  read_lock zashboard
  if [ -f /etc/mihomo/zashboard/index.html ]; then
    info "Mihomo web UI present"
  elif download zashboard; then
    rm -rf "$WORK/zash"; mkdir -p "$WORK/zash"
    unzip -q "$WORK/$L_ASSET" -d "$WORK/zash"
    local idx; idx=$(find "$WORK/zash" -name index.html -print -quit)
    if [ -n "$idx" ]; then
      install -d -m 2750 -o ubuntu -g ubuntu /etc/mihomo
      rm -rf /etc/mihomo/zashboard
      cp -r "$(dirname "$idx")" /etc/mihomo/zashboard
      chown -R root:ubuntu /etc/mihomo/zashboard
      chmod -R u=rwX,g=rX,o=rX /etc/mihomo/zashboard
      info "Mihomo web UI $L_VER installed"
    fi
  else
    warn "Mihomo web UI not downloaded (optional; the router works without it)"
  fi
}

users() {
  say "Users"
  local missing=()
  id ubuntu >/dev/null 2>&1 || missing+=(ubuntu)
  id router-xray >/dev/null 2>&1 || missing+=(router-xray)
  getent group crontab >/dev/null || missing+=(crontab-group)
  if [ "$DRY" = 1 ]; then
    [ "${#missing[@]}" -eq 0 ] && info "ubuntu, router-xray, crontab present" || info "would create: ${missing[*]}"
    return 0
  fi
  if ! id ubuntu >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo ubuntu
    passwd -l ubuntu >/dev/null
    info "created user ubuntu (password locked: key login; set one with: sudo passwd ubuntu)"
  fi
  getent group router-xray >/dev/null || groupadd --system router-xray
  id router-xray >/dev/null 2>&1 ||
    useradd --system --gid router-xray --home-dir /home/router-xray --no-create-home --shell /usr/sbin/nologin router-xray
  getent group crontab >/dev/null || die "group crontab missing (is cron installed?)"
  info "ubuntu, router-xray, crontab ok"
}

ssh_keys() {
  local src="$PROFILE_DIR/authorized_keys" d=/home/ubuntu/.ssh k
  [ -s "$src" ] || return 0
  say "SSH keys for ubuntu"
  if [ "$DRY" = 1 ]; then
    while IFS= read -r k; do
      [ -n "$k" ] || continue
      grep -qxF -- "$k" "$d/authorized_keys" 2>/dev/null && info "present: ${k##* }" || info "would add: ${k##* }"
    done < "$src"
    return 0
  fi
  install -d -o ubuntu -g ubuntu -m 700 "$d"
  touch "$d/authorized_keys"
  while IFS= read -r k; do
    [ -n "$k" ] || continue
    grep -qxF -- "$k" "$d/authorized_keys" || { echo "$k" >> "$d/authorized_keys"; info "added: ${k##* }"; }
  done < "$src"
  chown ubuntu:ubuntu "$d/authorized_keys"
  chmod 600 "$d/authorized_keys"
}

network_prep() {
  say "Network config"
  local f
  for f in /etc/netplan/*.yaml; do
    [ -e "$f" ] || continue
    [ "$(basename "$f")" = 01-router.yaml ] && continue
    if [ "$DRY" = 1 ]; then info "would move aside $f (01-router.yaml describes both ports)"; continue; fi
    install -d -m 700 "$BACKUP/netplan-aside"
    mv "$f" "$BACKUP/netplan-aside/"
    info "moved aside $f -> $BACKUP/netplan-aside/"
  done
  if [ -d /etc/cloud/cloud.cfg.d ] && ! grep -rqs 'config: *disabled' /etc/cloud/cloud.cfg.d; then
    if [ "$DRY" = 1 ]; then info "would disable cloud-init network config"
    else echo 'network: {config: disabled}' > /etc/cloud/cloud.cfg.d/99-routedeck-disable-network.cfg; info "cloud-init network config disabled"; fi
  fi
}

rootfs() {
  say "Router files (rootfs/, rendered for WAN $WAN, LAN $LAN)"
  local args=(install --host-env "$HOST_ENV" --set "WAN_IF=$WAN" --set "LAN_IF=$LAN" --set "MODE=$MODE" --root / --backup "$BACKUP")
  [ "$DRY" = 1 ] && args+=(--dry-run)
  python3 "$REPO/lib/render.py" "${args[@]}"
  if [ "$DRY" = 0 ]; then
    visudo -cf /etc/sudoers.d/99-ubuntu-nopasswd >/dev/null || die "sudoers drop-in failed validation"
  fi
}

secrets() {
  say "Secrets (keys and proxy configs)"
  if [ "$NO_SECRETS" = 1 ]; then warn "skipped (--no-secrets): upload configs later in the panel"; return 0; fi
  local src=${SECRETS:-$PROFILE_DIR/secrets.tar.gpg} tmp from prof
  if [ ! -f "$src" ] && [ "$DRY" = 1 ]; then info "bundle $src not found (a real run needs it, or --no-secrets)"; return 0; fi
  [ -f "$src" ] || die "secrets bundle not found: $src (pass --secrets FILE or --no-secrets)"
  tmp=$(mktemp -d /run/routedeck-secrets.XXXXXX); CLEAN+=("$tmp"); chmod 700 "$tmp"
  case "$src" in
    *.gpg)
      if [ "$DRY" = 1 ]; then info "would decrypt $(basename "$src") (passphrase) and install it"; return 0; fi
      install -d -m 700 "$tmp/gnupg"
      info "decrypting $(basename "$src"): type the secrets passphrase"
      GNUPGHOME="$tmp/gnupg" gpg --quiet --pinentry-mode loopback --decrypt --output "$tmp/bundle.tar" "$src" </dev/tty ||
        die "could not decrypt $src"
      GNUPGHOME="$tmp/gnupg" gpgconf --kill gpg-agent >/dev/null 2>&1 || true ;;
    *.tar) cp -- "$src" "$tmp/bundle.tar" ;;
    *) die "secrets bundle must be .tar.gpg or .tar" ;;
  esac
  tar -xf "$tmp/bundle.tar" -C "$tmp"
  rm -f "$tmp/bundle.tar"
  prof=$(sed -n 's/^PROFILE=//p' "$tmp/secrets.env")
  from=$(sed -n 's/^SECRETS_WAN_IF=//p' "$tmp/secrets.env")
  info "bundle: profile $prof, snapshot $(sed -n 's/^SNAPSHOT_DATE=//p' "$tmp/secrets.env"), WAN inside: $from -> $WAN"
  [ "$prof" = "$PROFILE_NAME" ] || ask "Bundle belongs to profile $prof, not $PROFILE_NAME. Use it anyway?" || die "stopped"
  local args=(secrets --bundle "$tmp" --root / --backup "$BACKUP" --wan-from "$from" --wan-to "$WAN")
  [ "$DRY" = 1 ] && args+=(--dry-run)
  python3 "$REPO/lib/render.py" "${args[@]}"
}

services() {
  say "Services"
  local state unit want now changes=0
  [ "$DRY" = 1 ] || systemctl daemon-reload
  while read -r state unit; do
    [ -n "$unit" ] || continue
    # Without secrets there is no Xray config yet: keep Xray off until you add one.
    if [ "$NO_SECRETS" = 1 ] && { [ "$unit" = xray.service ] || [ "$unit" = xray-recover.timer ]; }; then
      state=disable
    fi
    systemctl cat "$unit" >/dev/null 2>&1 || { info "absent: $unit"; continue; }
    now=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    want=$([ "$state" = enable ] && echo enabled || echo disabled)
    [ "$now" = "$want" ] && continue
    case "$now" in static|indirect|generated|alias) [ "$state" = disable ] && continue ;; esac
    changes=$((changes + 1))
    if [ "$DRY" = 1 ]; then info "would $state $unit (now $now)"; continue; fi
    systemctl "$state" "$unit" >/dev/null 2>&1 && info "$state $unit" || warn "could not $state $unit"
  done < <(grep -vE '^[[:space:]]*(#|$)' "$REPO/lock/units.txt")
  [ "$changes" -gt 0 ] || info "all unit states already match"
}

host_name() {
  local h; h=$(profile_get HOSTNAME)
  [ -n "$h" ] || return 0
  if [ "$(hostname)" = "$h" ]; then return 0; fi
  if [ "$DRY" = 1 ]; then info "would set hostname $h"; return 0; fi
  hostnamectl set-hostname "$h"
  sed -i '/^127\.0\.1\.1[[:space:]]/d' /etc/hosts
  echo "127.0.1.1 $h" >> /etc/hosts
  info "hostname $h"
}

twin_warning() {
  [ "$NO_SECRETS" = 1 ] && return 0
  [ "$DRY" = 1 ] && return 0
  say "Same keys as the original"
  info "The secrets of profile $PROFILE_NAME are the LIVE keys of $(profile_get DEVICE): AWG, proxies,"
  info "subscription device ids. Two boxes with the same AWG key push each other off the server."
  info "Run this box INSTEAD of the original (switch the original off first)."
  ask "Is the original switched off or will this box replace it?" || die "stopped: nothing was changed"
}

check() {
  local fail=0 lan mode wan
  ok()  { printf '  OK    %s\n' "$*"; }
  bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
  note() { printf '  INFO  %s\n' "$*"; }
  lan=$(sed -n 's/^LAN_IF=//p' /etc/router/core.env 2>/dev/null | tr -d '"')
  wan=$(sed -n 's/^WAN_IF=//p' /etc/router/core.env 2>/dev/null | tr -d '"')
  mode=$(cat /etc/router/mode 2>/dev/null || echo '?')
  say "Health check: mode $mode, WAN $wan, LAN $lan"
  case "$(uname -r)" in 6.8.*) ok "kernel $(uname -r)" ;; *) bad "kernel $(uname -r) (expected HWE 6.8; reboot after install?)" ;; esac
  modinfo amneziawg >/dev/null 2>&1 && ok "amneziawg module available" || bad "amneziawg module missing (dkms status)"
  local u
  for u in router-panel router-wan isc-dhcp-server ModemManager router-restore router-wan-boot awg-auto; do
    systemctl is-active --quiet "$u" && ok "$u active" || bad "$u not active ($(systemctl is-active "$u" 2>/dev/null || true))"
  done
  if [ -f /etc/xray/config.json ]; then
    systemctl is-active --quiet xray && ok "xray active" || bad "xray not active"
  else
    note "xray: no config yet (installed without secrets)"
  fi
  ip -4 -o addr show dev "$lan" 2>/dev/null | grep ' 192\.168\.50\.1/24' >/dev/null && ok "LAN $lan has 192.168.50.1/24" || bad "LAN $lan has no 192.168.50.1/24"
  ss -lnup 2>/dev/null | grep ':67 ' >/dev/null && ok "DHCP server listening" || bad "DHCP server not listening on :67"
  curl -fsS -o /dev/null -m 5 http://127.0.0.1:8080/ && ok "panel answers on :8080" || bad "panel does not answer on 127.0.0.1:8080"
  curl -fsS -o /dev/null -m 10 --interface "$wan" https://ya.ru/ && ok "internet via WAN $wan" || note "no internet check via $wan (is 4G active?)"
  case "$mode" in
    awg)
      local hs; hs=$(awg show wg0 latest-handshakes 2>/dev/null | awk '{print $2}' | sort -n | sed -n '$p')
      if [ -n "$hs" ] && [ "$hs" -gt 0 ] && [ $(( $(date +%s) - hs )) -lt 300 ]; then ok "AWG handshake $(( $(date +%s) - hs )) s ago"; else bad "no fresh AWG handshake on wg0"; fi
      local ip; ip=$(curl -fsS -m 10 --interface wg0 https://ipv4-internet.yandex.net/api/v0/ip 2>/dev/null | tr -d '"' || true)
      [ -n "$ip" ] && ok "exit IP via AWG: $ip" || bad "no answer through wg0" ;;
    mihomo) systemctl is-active --quiet mihomo && ok "mihomo active" || bad "mihomo not active" ;;
    tun) systemctl is-active --quiet sing-box && ok "sing-box active" || bad "sing-box not active" ;;
    direct) note "mode direct: LAN goes straight to WAN" ;;
  esac
  [ "$fail" = 0 ] && say "All checks passed" || say "Some checks failed"
  return "$fail"
}

main() {
  preflight "$@"
  if [ "$CHECK" = 1 ]; then if check; then exit 0; else exit 1; fi; fi
  load_profile
  pick_ports
  [ "$DRY" = 1 ] && say "DRY RUN: nothing will be changed"
  twin_warning
  if [ "$DRY" = 0 ]; then install -d -m 700 "$BACKUP"; fi
  packages
  users
  binaries
  network_prep
  rootfs
  secrets
  ssh_keys
  host_name
  services
  if [ "$DRY" = 1 ]; then say "Dry run finished: no changes were made"; exit 0; fi
  say "Installed"
  info "saved originals and undo scripts: $BACKUP"
  info "next: reboot, then check: sudo $REPO/install.sh --check"
  if [ "$REBOOT" = 1 ] || ask "Reboot now?"; then systemctl reboot; fi
}

main "$@"
