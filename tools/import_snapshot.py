#!/usr/bin/env python3
"""Import read-only snapshots (tools/snapshot.sh) into this repository.

  python3 tools/import_snapshot.py --repo . --base pc2 \
      --snapshot pc2=/path/pc2-....tgz --snapshot pc1=/path/pc1-....tgz \
      --profiles-out /path/to/private-profiles --secrets-out /path/OUTSIDE/any/git

Writes:
  rootfs/              shared code and system files of the BASE host; host facts
                       are replaced by @@TOKENS@@ that lib/render.py fills in
  rootfs.manifest      one line per path: type path mode owner group template target
  <profiles-out>/<name>/   host.env (facts) and authorized_keys of each box. They
                       name your own network and keys: keep them in a PRIVATE repo
  <secrets-out>/<name>.tar PLAINTEXT secrets bundle + manifest. Never commit it;
                       tools/encrypt-secrets.sh turns it into <profiles-out>/<name>/secrets.tar.gpg

Only the paths listed below are taken; everything else on the box (old
installers, backups, test files, OpenFlux leftovers) stays out on purpose.
Python 3.10+, standard library only.
"""
import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path

# --- what the repository manages (relative to /) ---------------------------
ROOTFS_FILES = [
    # router code
    'usr/local/lib/router-core/mode_control.py',
    'usr/local/lib/router-cpu-boost/cpu_boost.py',
    'usr/local/lib/router-mihomo/legacy/router-apply-mode',
    'usr/local/lib/router-mihomo/legacy/router-restore-runtime',
    'usr/local/lib/router-panel/router-panel.py',
    'usr/local/lib/router-updater/kernel_policy.py',
    'usr/local/lib/router-updater/panel_system.py',
    'usr/local/lib/router-updater/panel_updates.py',
    'usr/local/lib/router-updater/retention.py',
    'usr/local/lib/router-updater/router_update_core.py',
    'usr/local/lib/router-updater/transport.py',
    'usr/local/lib/router-wan/panel_wan.py',
    'usr/local/lib/router-wan/wan_awg.py',
    'usr/local/lib/router-wan/wan_bands.py',
    'usr/local/lib/router-wan/wan_common.py',
    'usr/local/lib/router-wan/wan_control.py',
    'usr/local/lib/router-wan/wan_daemon.py',
    'usr/local/lib/router-wan/wan_modem.py',
    'usr/local/lib/router-wan/wan_network.py',
    'usr/local/lib/router-wan/wan_speedtest.py',
    'usr/local/lib/router-wan/wan_whitelist.py',
    'usr/local/lib/router-xray/config_store.py',
    'usr/local/lib/router-xray/mapping.py',
    'usr/local/lib/router-xray/panel_extra.py',
    'usr/local/lib/router-xray/prepare_config.py',
    'usr/local/lib/router-xray/probes.py',
    'usr/local/lib/router-xray/router_platform.py',
    'usr/local/lib/router-xray/router_transport.py',
    'usr/local/lib/router-xray/xray_control.py',
    'usr/local/sbin/mihomo-runtime-hook',
    'usr/local/sbin/mihomo-wait-lan',
    'usr/local/sbin/net-awg',
    'usr/local/sbin/net-direct',
    'usr/local/sbin/net-mihomo',
    'usr/local/sbin/net-tun',
    'usr/local/sbin/router-apply-mode',
    'usr/local/sbin/router-apply-mss-clamp',
    'usr/local/sbin/router-cpu-boost',
    'usr/local/sbin/router-mode-control',
    'usr/local/sbin/router-restore-runtime',
    'usr/local/sbin/router-runtime-ensure',
    'usr/local/sbin/router-update',
    'usr/local/sbin/router-wan',
    'usr/local/sbin/router-xray-control',
    'usr/local/sbin/tun-rotator',
    'usr/local/sbin/watchdog-rotator',
    # systemd units and drop-ins
    'etc/systemd/system/awg-auto.service',
    'etc/systemd/system/awg-quick@.service.d/override.conf',
    'etc/systemd/system/isc-dhcp-server.service.d/50-restart.conf',
    'etc/systemd/system/mihomo.service',
    'etc/systemd/system/mihomo.service.d/10-router-wan.conf',
    'etc/systemd/system/mihomo.service.d/30-xray.conf',
    'etc/systemd/system/router-cpu-boost.service',
    'etc/systemd/system/router-kernel-retention.service',
    'etc/systemd/system/router-kernel-retention.timer',
    'etc/systemd/system/router-mode.service',
    'etc/systemd/system/router-mode.service.d/override.conf',
    'etc/systemd/system/router-panel.service',
    'etc/systemd/system/router-policy.service',
    'etc/systemd/system/router-restore.service',
    'etc/systemd/system/router-restore.service.d/40-start-timeout.conf',
    'etc/systemd/system/router-runtime-late.service',
    'etc/systemd/system/router-runtime-late.service.d/40-start-timeout.conf',
    'etc/systemd/system/router-wan-boot.service',
    'etc/systemd/system/router-wan-guard-early.service',
    'etc/systemd/system/router-wan-recover.service',
    'etc/systemd/system/router-wan-recover.timer',
    'etc/systemd/system/router-wan-worker.service',
    'etc/systemd/system/router-wan.service',
    'etc/systemd/system/sing-box.service.d/10-router-wan.conf',
    'etc/systemd/system/xray-recover.service',
    'etc/systemd/system/xray-recover.timer',
    'etc/systemd/system/xray.service',
    'etc/systemd/system/xray.service.d/10-router-wan.conf',
    # network, firewall, DHCP, kernel settings
    'etc/systemd/network/10-netplan-eno1.network.d/50-keep-lan-address.conf',
    'etc/systemd/networkd.conf.d/90-router-preserve-policy-rules-v2.conf',
    'etc/systemd/networkd.conf.d/91-router-preserve-routes.conf',
    'etc/systemd/resolved.conf',
    'etc/netplan/01-router.yaml',
    'etc/dhcp/dhcpd.conf',
    'etc/default/isc-dhcp-server',
    'etc/iptables/rules.v4',
    'etc/iptables/rules.v6',
    'etc/iproute2/rt_tables',
    'etc/sysctl.d/99-conntrack-timeouts.conf',
    'etc/sysctl.d/99-conntrack.conf',
    'etc/sysctl.d/99-disable-ipv6.conf',
    'etc/sysctl.d/99-ipforward.conf',
    'etc/sysctl.d/99-router.conf',
    # apt, sudo, cron
    'etc/apt/apt.conf.d/20auto-upgrades',
    'etc/apt/apt.conf.d/99-router-kernel-retention',
    'etc/apt/sources.list.d/amnezia-ubuntu-ppa-jammy.list',
    'etc/apt/trusted.gpg.d/amnezia-ubuntu-ppa.gpg',
    'etc/sudoers.d/99-ubuntu-nopasswd',
    'var/spool/cron/crontabs/root',
    # host facts (templated)
    'etc/router/core.env',
    'etc/router/platform.json',
    'etc/router/panel.json',
    'etc/router/mode',
    'etc/router/cpu-boost',
    'etc/router/updater/config.yaml',
    'etc/router/updater/settings.json',
]
# State directories the code expects (contents are runtime state, not copied).
ROOTFS_DIRS = [
    'etc/mihomo',
    'var/lib/router', 'var/lib/router/tun-rotator', 'var/lib/router-configs',
    'var/lib/router-panel', 'var/lib/router-updater', 'var/lib/router-wan',
    'var/lib/router-xray', 'var/lib/router-xray/versions',
]
# Symlinks the router relies on (resolved runs with DNSStubListener=no).
ROOTFS_LINKS = ['etc/resolv.conf']
# Per-host secrets (keys, proxy configs, subscription HWID). Never in git as plaintext.
SECRET_PATTERNS = [
    'etc/amnezia/amneziawg/**', 'etc/router/watchdog/**', 'etc/router/wan.json',
    'etc/sing-box/config.json', 'etc/sing-box/configs/**',
    'etc/mihomo/config.yaml', 'etc/mihomo/source-proxies.yaml', 'etc/mihomo/providers/**',
    'etc/xray/**', 'home/ubuntu/config.yaml', 'home/ubuntu/AWG_MiniPC_wg0/**',
]
TEXT_LIMIT = 4 * 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        key, sep, value = line.partition('=')
        if sep and key and not key.startswith('#'):
            out[key.strip()] = value.strip().strip('"\'')
    return out


class Snapshot:
    def __init__(self, path):
        self.path = Path(path)
        self.tar = tarfile.open(self.path, 'r:gz')
        self.members = {m.name: m for m in self.tar.getmembers()}
        self.entries = {}
        for line in self.read('meta/manifest.txt').decode().splitlines():
            parts = line.split(' ')
            kind = parts[0]
            if kind == 'F' and len(parts) == 6:
                _, mode, owner, size, digest, path = parts
                self.entries[path.lstrip('/')] = dict(type='F', mode=mode, owner=owner, size=int(size), sha=digest)
            elif kind == 'D' and len(parts) == 4:
                _, mode, owner, path = parts
                self.entries[path.lstrip('/')] = dict(type='D', mode=mode, owner=owner)
            elif kind == 'L' and len(parts) == 6 and parts[4] == '->':
                _, mode, owner, path, _, target = parts
                self.entries[path.lstrip('/')] = dict(type='L', mode=mode, owner=owner, target=target)
            else:
                raise SystemExit('unexpected manifest line in %s: %r' % (self.path.name, line))
        self.date = self.read('meta/date.txt').decode().strip()

    def read(self, name):
        member = self.members.get(name)
        if member is None or not member.isfile():
            raise KeyError(name)
        return self.tar.extractfile(member).read()

    def file(self, rel):
        entry = self.entries.get(rel)
        if not entry or entry['type'] != 'F':
            raise SystemExit('%s: %s is not a file in the snapshot manifest' % (self.path.name, rel))
        data = self.read(rel)
        if sha256(data) != entry['sha']:
            raise SystemExit('%s: %s does not match its manifest sha256' % (self.path.name, rel))
        return data, entry

    def facts(self):
        core = parse_env(self.read('etc/router/core.env').decode())
        panel = json.loads(self.read('etc/router/panel.json').decode())
        return {
            'DEVICE': core['DEVICE'], 'WAN_IF': core['WAN_IF'], 'LAN_IF': core['LAN_IF'],
            'LAN_CIDR': core['LAN_CIDR'], 'HOSTNAME': self.read('etc/hostname').decode().strip(),
            'PANEL_LABEL': panel['label'], 'PANEL_COLOR': panel['color'],
            'TRUSTED_NET': ','.join(panel.get('trusted_networks', [])),
            'MODE': self.read('etc/router/mode').decode().strip(),
            'CPU_BOOST': self.read('etc/router/cpu-boost').decode().strip(),
        }


def token_re(value):
    return re.compile(rb'(?<![A-Za-z0-9_])' + re.escape(value.encode()) + rb'(?![A-Za-z0-9_])')


def is_text(data):
    if b'\0' in data or len(data) > TEXT_LIMIT:
        return False
    try:
        data.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False


def nets_json(nets):
    """The list as the panel writes it inside panel.json (json indent=1, nested)."""
    if not nets:
        return '[]'
    return '[\n' + ',\n'.join('  "%s"' % n for n in nets) + '\n ]'


def exact(data, old, new, rel):
    count = data.count(old.encode())
    if count != 1:
        raise SystemExit('%s: expected exactly one %r, found %d' % (rel, old, count))
    return data.replace(old.encode(), new.encode())


def host_tokens(rel, data, facts):
    """Exact host facts inside the host files -> tokens (base host values only)."""
    if rel == 'etc/router/core.env':
        data = exact(data, 'DEVICE="%s"' % facts['DEVICE'], 'DEVICE="@@DEVICE@@"', rel)
    elif rel == 'etc/router/platform.json':
        data = exact(data, '"device": "%s"' % facts['DEVICE'], '"device": "@@DEVICE@@"', rel)
    elif rel == 'etc/router/panel.json':
        label = json.dumps(facts['PANEL_LABEL'], ensure_ascii=True)[1:-1]
        data = exact(data, '"label": "%s"' % label, '"label": "@@PANEL_LABEL_JSON@@"', rel)
        data = exact(data, '"color": "%s"' % facts['PANEL_COLOR'], '"color": "@@PANEL_COLOR@@"', rel)
        nets = [n for n in facts['TRUSTED_NET'].split(',') if n]
        data = exact(data, '"trusted_networks": ' + nets_json(nets),
                     '"trusted_networks": @@TRUSTED_NETS_JSON@@', rel)
    elif rel == 'etc/router/mode':
        data = exact(data, facts['MODE'], '@@MODE@@', rel)
    elif rel == 'etc/router/cpu-boost':
        data = exact(data, facts['CPU_BOOST'], '@@CPU_BOOST@@', rel)
    return data


def match(rel, patterns):
    for pattern in patterns:
        if pattern.endswith('/**'):
            base = pattern[:-3]
            if rel == base or rel.startswith(base + '/'):
                return True
        elif rel == pattern:
            return True
    return False


def owner_split(owner):
    user, _, group = owner.partition(':')
    return user, group


def build_rootfs(repo, snap, facts):
    root = repo / 'rootfs'
    if root.exists():
        shutil.rmtree(root)
    lines = ['# type\tpath\tmode\towner\tgroup\ttemplate\ttarget']
    wan, lan = facts['WAN_IF'], facts['LAN_IF']
    rewrites = [(wan, b'@@WAN_IF@@'), (lan, b'@@LAN_IF@@')]
    dirs = set(ROOTFS_DIRS)
    templated = 0
    for rel in ROOTFS_FILES:
        data, entry = snap.file(rel)
        template = 0
        if is_text(data):
            if b'@@' in data:
                raise SystemExit('%s already contains @@; pick another token syntax' % rel)
            new = host_tokens(rel, data, facts)
            for value, token in rewrites:
                new = token_re(value).sub(token, new)
            if new != data:
                template, data = 1, new
                templated += 1
        out_rel = token_re(lan).sub(b'@@LAN_IF@@', rel.encode()).decode()
        target = root / out_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        user, group = owner_split(entry['owner'])
        lines.append('\t'.join(['F', out_rel, entry['mode'], user, group, str(template), '-']))
        parent = rel.rsplit('/', 1)[0]
        while parent and parent in snap.entries and snap.entries[parent]['type'] == 'D':
            dirs.add(parent)
            parent = parent.rsplit('/', 1)[0] if '/' in parent else ''
    for rel in sorted(dirs):
        entry = snap.entries.get(rel)
        if not entry or entry['type'] != 'D':
            raise SystemExit('state directory %s missing in the snapshot' % rel)
        out_rel = token_re(lan).sub(b'@@LAN_IF@@', rel.encode()).decode()
        user, group = owner_split(entry['owner'])
        lines.append('\t'.join(['D', out_rel, entry['mode'], user, group, '0', '-']))
    for rel in ROOTFS_LINKS:
        entry = snap.entries.get(rel)
        if not entry or entry['type'] != 'L':
            raise SystemExit('symlink %s missing in the snapshot' % rel)
        user, group = owner_split(entry['owner'])
        lines.append('\t'.join(['L', rel, entry['mode'], user, group, '0', entry['target']]))
    (repo / 'rootfs.manifest').write_text('\n'.join(lines) + '\n', newline='\n')
    return len(ROOTFS_FILES), len(dirs), templated


def write_host(profiles_out, name, snap, facts):
    hdir = profiles_out / name
    hdir.mkdir(parents=True, exist_ok=True)
    keys = snap.read('home/ubuntu/.ssh/authorized_keys')
    (hdir / 'authorized_keys').write_bytes(keys)
    lines = [
        '# routedeck host profile "%s" - facts only, no secrets.' % name,
        '# Imported from the read-only snapshot taken %s.' % snap.date,
        'PROFILE=%s' % name,
        'DEVICE="%s"' % facts['DEVICE'],
        'HOSTNAME=%s' % facts['HOSTNAME'],
        '# Ethernet ports of the ORIGINAL box. install.sh asks for the ports of the',
        '# new box (or takes --wan/--lan) and renders everything with those names.',
        'WAN_IF=%s' % facts['WAN_IF'],
        'LAN_IF=%s' % facts['LAN_IF'],
        '# Fixed for this router type: LAN 192.168.50.1/24, DHCP .100-.200.',
        'LAN_CIDR=%s' % facts['LAN_CIDR'],
        'PANEL_LABEL="%s"' % facts['PANEL_LABEL'],
        'PANEL_COLOR="%s"' % facts['PANEL_COLOR'],
        '# Extra networks the panel answers besides the LAN: comma-separated CIDRs or empty.',
        'TRUSTED_NET=%s' % facts['TRUSTED_NET'],
        '# Traffic mode after installation: direct | awg | tun | mihomo',
        'MODE=%s' % facts['MODE'],
        'CPU_BOOST=%s' % facts['CPU_BOOST'],
        '# WAN name written inside this profile\'s proxy configs (secrets bundle).',
        'SECRETS_WAN_IF=%s' % facts['WAN_IF'],
    ]
    (hdir / 'host.env').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')


def build_secrets(out_dir, name, snap, facts):
    out_dir.mkdir(parents=True, exist_ok=True)
    picked = sorted(rel for rel in snap.entries if match(rel, SECRET_PATTERNS))
    manifest = ['# type\tpath\tmode\towner\tgroup\tsha256\ttarget']
    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for rel in picked:
            entry = snap.entries[rel]
            user, group = owner_split(entry['owner'])
            if entry['type'] == 'F':
                data, _ = snap.file(rel)
                info = tarfile.TarInfo('files/' + rel)
                info.size, info.mode, info.mtime = len(data), int(entry['mode'], 8), now
                info.uname, info.gname = user, group
                tar.addfile(info, io.BytesIO(data))
                manifest.append('\t'.join(['F', rel, entry['mode'], user, group, sha256(data), '-']))
            elif entry['type'] == 'D':
                manifest.append('\t'.join(['D', rel, entry['mode'], user, group, '-', '-']))
            else:
                manifest.append('\t'.join(['L', rel, entry['mode'], user, group, '-', entry['target']]))
        meta = ('PROFILE=%s\nSNAPSHOT_DATE=%s\nSECRETS_WAN_IF=%s\n' % (name, snap.date, facts['WAN_IF'])).encode()
        for member, payload in (('secrets.manifest', ('\n'.join(manifest) + '\n').encode()), ('secrets.env', meta)):
            info = tarfile.TarInfo(member)
            info.size, info.mode, info.mtime = len(payload), 0o600, now
            tar.addfile(info, io.BytesIO(payload))
    path = out_dir / (name + '.tar')
    path.write_bytes(buf.getvalue())
    return path, sum(1 for line in manifest[1:] if line.startswith('F'))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--repo', required=True)
    ap.add_argument('--base', required=True, help='host whose code and system files become rootfs/')
    ap.add_argument('--snapshot', action='append', required=True, help='name=path.tgz')
    ap.add_argument('--profiles-out', required=True, help='PRIVATE directory for host profiles (host.env, authorized_keys)')
    ap.add_argument('--secrets-out', required=True, help='directory outside any git repo for plaintext bundles')
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    secrets_out = Path(args.secrets_out).resolve()
    profiles_out = Path(args.profiles_out).resolve()
    for out in (secrets_out, profiles_out):
        if out == repo or repo in out.parents:
            raise SystemExit('%s must be outside this (public) repository' % out)
    if profiles_out == secrets_out or secrets_out in profiles_out.parents or profiles_out in secrets_out.parents:
        raise SystemExit('keep --secrets-out (plaintext) apart from --profiles-out (a git repo)')
    snaps = dict(item.split('=', 1) for item in args.snapshot)
    loaded = {name: Snapshot(path) for name, path in snaps.items()}
    if args.base not in loaded:
        raise SystemExit('--base must be one of the snapshots')
    facts = {name: snap.facts() for name, snap in loaded.items()}
    base = loaded[args.base]
    files, dirs, templated = build_rootfs(repo, base, facts[args.base])
    print('rootfs: %d files (%d templated), %d directories, from %s %s' % (files, templated, dirs, args.base, base.date))
    for name, snap in loaded.items():
        write_host(profiles_out, name, snap, facts[name])
        path, count = build_secrets(secrets_out, name, snap, facts[name])
        print('host %s: WAN %s LAN %s, secrets %d files -> %s' % (name, facts[name]['WAN_IF'], facts[name]['LAN_IF'], count, path))


if __name__ == '__main__':
    main()
