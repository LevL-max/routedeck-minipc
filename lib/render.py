#!/usr/bin/env python3
"""Render rootfs/ for one host, install it, or check it against a snapshot.

  render.py stage    --host-env F [--set K=V]... --out DIR
  render.py install  --host-env F [--set K=V]... --root / --backup DIR [--dry-run]
  render.py verify   --host-env F [--set K=V]... --snapshot-manifest FILE
  render.py lint     --host-env F --set WAN_IF=a --set LAN_IF=b
  render.py secrets  --bundle DIR --root / --backup DIR --wan-from A --wan-to B [--dry-run]
  render.py verify-secrets --bundle DIR --snapshot-manifest FILE

rootfs.manifest lines: type path mode owner group template target.
Files with template=1 contain @@TOKENS@@ (also allowed in paths).
Python 3.10+, standard library only. Owners are set by NAME (install only).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r'@@([A-Z_]+)@@')
IFNAME = re.compile(r'^[A-Za-z0-9_.-]{1,15}$')
MODES = ('direct', 'awg', 'tun', 'mihomo')
# Proxy configs that carry the WAN interface name (router-wan rebinds them later,
# but refuses to touch a name that is not this host's WAN).
WAN_BOUND = ('etc/xray/config.json', 'etc/mihomo/config.yaml', 'etc/mihomo/source-proxies.yaml',
             'etc/sing-box/config.json', 'home/ubuntu/config.yaml')


def die(msg):
    print('render: ' + msg, file=sys.stderr)
    sys.exit(2)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_env(path):
    out = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        key, sep, value = line.partition('=')
        if sep and key and not key.startswith('#'):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            out[key.strip()] = value
    return out


def host_values(args):
    env = read_env(args.host_env)
    for item in args.set or []:
        key, sep, value = item.partition('=')
        if not sep:
            die('--set needs KEY=VALUE: ' + item)
        env[key] = value
    for key in ('WAN_IF', 'LAN_IF'):
        if not IFNAME.match(env.get(key, '')):
            die('%s must be an interface name, got %r' % (key, env.get(key)))
    if env['WAN_IF'] == env['LAN_IF']:
        die('WAN_IF and LAN_IF must differ')
    if env.get('LAN_CIDR', '192.168.50.0/24') != '192.168.50.0/24':
        die('this router type uses LAN 192.168.50.0/24 only')
    if not re.match(r'^#[0-9a-fA-F]{6}$', env.get('PANEL_COLOR', '')):
        die('PANEL_COLOR must look like #2563eb')
    if env.get('MODE') not in MODES:
        die('MODE must be one of ' + ', '.join(MODES))
    if env.get('CPU_BOOST') not in ('on', 'off'):
        die('CPU_BOOST must be on or off')
    nets = [n.strip() for n in env.get('TRUSTED_NET', '').split(',') if n.strip()]
    for net in nets:
        if not re.match(r'^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$', net):
            die('TRUSTED_NET must be empty or comma-separated IPv4 CIDRs, got %r' % net)
    if not re.match(r'^[^"\\\n]{1,40}$', env.get('DEVICE', '')):
        die('DEVICE must be 1-40 characters without quotes')
    env['PANEL_LABEL_JSON'] = json.dumps(env.get('PANEL_LABEL', ''), ensure_ascii=True)[1:-1]
    # The list as the panel itself writes it inside panel.json (json indent=1, nested).
    env['TRUSTED_NETS_JSON'] = ('[\n' + ',\n'.join('  "%s"' % n for n in nets) + '\n ]') if nets else '[]'
    return env


def fill(text, values, where):
    def repl(m):
        if m.group(1) not in values:
            die('%s: no value for @@%s@@' % (where, m.group(1)))
        return values[m.group(1)]
    return TOKEN.sub(repl, text)


def entries(values):
    """Yield (type, path, mode, owner, group, data_or_target) for this host."""
    manifest = (REPO / 'rootfs.manifest').read_text(encoding='utf-8').splitlines()
    for line in manifest:
        if not line or line.startswith('#'):
            continue
        kind, path, mode, owner, group, template, target = line.split('\t')
        out_path = fill(path, values, path)
        if kind == 'F':
            data = (REPO / 'rootfs' / path).read_bytes()
            if template == '1':
                data = fill(data.decode('utf-8'), values, path).encode('utf-8')
            elif b'@@' in data and TOKEN.search(data.decode('utf-8', 'ignore')):
                die('%s has tokens but template=0' % path)
            yield kind, out_path, int(mode, 8), owner, group, data
        elif kind == 'D':
            yield kind, out_path, int(mode, 8), owner, group, None
        elif kind == 'L':
            yield kind, out_path, int(mode, 8), owner, group, target
        else:
            die('bad manifest line: ' + line)


def read_snapshot_manifest(path):
    found = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        parts = line.split(' ')
        if parts[0] == 'F' and len(parts) == 6:
            found[parts[5].lstrip('/')] = ('F', parts[1], parts[2], parts[4])
        elif parts[0] == 'D' and len(parts) == 4:
            found[parts[3].lstrip('/')] = ('D', parts[1], parts[2], None)
        elif parts[0] == 'L' and len(parts) == 6:
            found[parts[3].lstrip('/')] = ('L', parts[1], parts[2], parts[5])
    return found


MISSING_OWNERS = set()


def owner_ids(owner, group, dry_run=False):
    import grp
    import pwd
    try:
        return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid
    except KeyError as exc:
        if dry_run:
            MISSING_OWNERS.add('%s:%s' % (owner, group))
            return -1, -1
        die('missing user or group %s:%s (%s)' % (owner, group, exc))


def current_state(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st


class Installer:
    """Writes files under root, keeping a copy of whatever it replaces."""

    def __init__(self, root, backup, dry_run):
        self.root = Path(root)
        self.backup = Path(backup)
        self.dry = dry_run
        self.actions = []
        if not dry_run:
            (self.backup / 'files').mkdir(parents=True, exist_ok=True)

    def target(self, rel):
        return self.root / rel

    def save(self, rel, st):
        if self.dry or st is None:
            return
        dest = self.backup / 'files' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = self.target(rel)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dest)
        elif st and not os.path.isdir(src):
            shutil.copy2(src, dest)

    def owner_ok(self, st, uid, gid):
        return st.st_uid == uid and st.st_gid == gid

    def put_dir(self, rel, mode, owner, group):
        path = self.target(rel)
        st = current_state(path)
        uid, gid = owner_ids(owner, group, self.dry)
        if st and os.path.isdir(path) and (st.st_mode & 0o7777) == mode and self.owner_ok(st, uid, gid):
            return
        self.actions.append(('dir', rel, 'new' if st is None else 'meta'))
        if self.dry:
            return
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)

    def put_file(self, rel, mode, owner, group, data):
        path = self.target(rel)
        st = current_state(path)
        uid, gid = owner_ids(owner, group, self.dry)
        if st and not os.path.islink(path) and os.path.isfile(path):
            same = path.read_bytes() == data
            if same and (st.st_mode & 0o7777) == mode and self.owner_ok(st, uid, gid):
                return
            self.actions.append(('file', rel, 'meta' if same else 'changed'))
        else:
            self.actions.append(('file', rel, 'new' if st is None else 'replaced'))
        if self.dry:
            return
        self.save(rel, st)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.%s.routedeck-%d' % (path.name, os.getpid()))
        with open(tmp, 'wb') as fh:
            fh.write(data)
        os.chown(tmp, uid, gid)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def put_link(self, rel, owner, group, target):
        path = self.target(rel)
        st = current_state(path)
        if st and os.path.islink(path) and os.readlink(path) == target:
            return
        self.actions.append(('link', rel, 'new' if st is None else 'replaced'))
        if self.dry:
            return
        self.save(rel, st)
        path.parent.mkdir(parents=True, exist_ok=True)
        if st is not None:
            path.unlink()
        os.symlink(target, path)
        uid, gid = owner_ids(owner, group, self.dry)
        os.lchown(path, uid, gid)

    def finish(self, label):
        if not self.dry:
            lines = ['#!/bin/sh', '# Undo "%s" from %s: restore saved files, remove new ones.' % (label, time.strftime('%Y-%m-%d %H:%M:%S')),
                     'set -u', 'B="$(cd "$(dirname "$0")" && pwd)"']
            for kind, rel, how in self.actions:
                dest = '/' + rel
                if kind == 'dir':
                    continue
                if how == 'new':
                    lines.append('rm -f -- "%s"' % dest)
                else:
                    lines.append('cp -a -- "$B/files/%s" "%s"' % (rel, dest))
            lines.append('echo "restored: %d entries"' % sum(1 for a in self.actions if a[0] != 'dir'))
            undo = self.backup / ('undo-%s.sh' % label)
            undo.write_text('\n'.join(lines) + '\n')
            os.chmod(undo, 0o700)
        counts = {}
        for kind, rel, how in self.actions:
            counts[how] = counts.get(how, 0) + 1
        if MISSING_OWNERS:
            print("%s: owners not present yet: %s" % (label, ", ".join(sorted(MISSING_OWNERS))))
        print('%s: %s%s' % (label, ', '.join('%s %d' % kv for kv in sorted(counts.items())) or 'nothing to change',
                            ' (dry run)' if self.dry else ''))
        for kind, rel, how in self.actions[:400]:
            print('  %-8s %-5s /%s' % (how, kind, rel))


def cmd_stage(args):
    values = host_values(args)
    out = Path(args.out)
    for kind, rel, mode, owner, group, data in entries(values):
        path = out / rel
        if kind == 'D':
            path.mkdir(parents=True, exist_ok=True)
        elif kind == 'F':
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if os.name != 'nt':
                os.chmod(path, mode)
    print('staged into %s' % out)


def cmd_install(args):
    values = host_values(args)
    inst = Installer(args.root, args.backup, args.dry_run)
    items = list(entries(values))
    for kind, rel, mode, owner, group, data in items:
        if kind == 'D':
            inst.put_dir(rel, mode, owner, group)
    for kind, rel, mode, owner, group, data in items:
        if kind == 'F':
            inst.put_file(rel, mode, owner, group, data)
        elif kind == 'L':
            inst.put_link(rel, owner, group, data)
    inst.finish('rootfs')


def cmd_verify(args):
    values = host_values(args)
    live = read_snapshot_manifest(args.snapshot_manifest)
    same, differ, missing, meta = [], [], [], []
    for kind, rel, mode, owner, group, data in entries(values):
        entry = live.get(rel)
        if entry is None:
            missing.append(rel)
            continue
        lkind, lmode, lowner, ldigest = entry
        if kind == 'F':
            if lkind != 'F' or ldigest != sha256(data):
                differ.append(rel)
                continue
        elif kind == 'L':
            if lkind != 'L' or ldigest != data:
                differ.append(rel)
                continue
        elif kind != lkind:
            differ.append(rel)
            continue
        if int(lmode, 8) != mode or lowner != '%s:%s' % (owner, group):
            meta.append('%s (live %s %s, repo %o %s:%s)' % (rel, lmode, lowner, mode, owner, group))
        else:
            same.append(rel)
    print('identical: %d, content differs: %d, owner/mode differs: %d, missing on host: %d'
          % (len(same), len(differ), len(meta), len(missing)))
    for title, rows in (('content differs', differ), ('owner/mode differs', meta), ('missing on host', missing)):
        for row in rows:
            print('  %s: %s' % (title, row))
    return 0 if not (differ or meta or missing) else 1


def cmd_lint(args):
    values = host_values(args)
    base = read_env(args.host_env)
    old = {base['WAN_IF'], base['LAN_IF']} - {values['WAN_IF'], values['LAN_IF']}
    pattern = re.compile(r'(?<![A-Za-z0-9_])(' + '|'.join(re.escape(x) for x in sorted(old)) + r')(?![A-Za-z0-9_])') if old else None
    hits = 0
    for kind, rel, mode, owner, group, data in entries(values):
        texts = [rel] + ([data.decode('utf-8', 'ignore')] if kind == 'F' else [])
        for text in texts:
            for m in (pattern.finditer(text) if pattern else []):
                line = text.count('\n', 0, m.start()) + 1
                print('  leftover %s in /%s line %d' % (m.group(1), rel, line))
                hits += 1
    print('lint: %d leftover interface names of the source host' % hits)
    return 1 if hits else 0


def secrets_manifest(bundle):
    rows = []
    for line in (Path(bundle) / 'secrets.manifest').read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            rows.append(line.split('\t'))
    return rows


def rebind(data, rel, wan_from, wan_to):
    if wan_from == wan_to or rel not in WAN_BOUND:
        return data
    pat = re.compile(rb'(?<![A-Za-z0-9_])' + re.escape(wan_from.encode()) + rb'(?![A-Za-z0-9_])')
    return pat.sub(wan_to.encode(), data)


def cmd_secrets(args):
    if not IFNAME.match(args.wan_from) or not IFNAME.match(args.wan_to):
        die('bad interface name')
    inst = Installer(args.root, args.backup, args.dry_run)
    rows = secrets_manifest(args.bundle)
    for kind, rel, mode, owner, group, digest, target in rows:
        if kind == 'D':
            inst.put_dir(rel, int(mode, 8), owner, group)
    for kind, rel, mode, owner, group, digest, target in rows:
        if kind == 'F':
            data = (Path(args.bundle) / 'files' / rel).read_bytes()
            if sha256(data) != digest:
                die('bundle file %s is damaged (sha256 mismatch)' % rel)
            inst.put_file(rel, int(mode, 8), owner, group, rebind(data, rel, args.wan_from, args.wan_to))
        elif kind == 'L':
            inst.put_link(rel, owner, group, target)
    inst.finish('secrets')


def cmd_verify_secrets(args):
    live = read_snapshot_manifest(args.snapshot_manifest)
    bad = 0
    rows = secrets_manifest(args.bundle)
    for kind, rel, mode, owner, group, digest, target in rows:
        entry = live.get(rel)
        if kind == 'F':
            data = (Path(args.bundle) / 'files' / rel).read_bytes()
            ok = entry and entry[0] == 'F' and entry[3] == sha256(data) == digest
        else:
            ok = entry and entry[0] == kind
        ok = ok and int(entry[1], 8) == int(mode, 8) and entry[2] == '%s:%s' % (owner, group)
        if not ok:
            bad += 1
            print('  differs: ' + rel)
    print('secrets: %d entries, %d differ from the snapshot' % (len(rows), bad))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('stage', 'install', 'verify', 'lint'):
        p = sub.add_parser(name)
        p.add_argument('--host-env', required=True)
        p.add_argument('--set', action='append')
        if name == 'stage':
            p.add_argument('--out', required=True)
        if name == 'install':
            p.add_argument('--root', default='/')
            p.add_argument('--backup', required=True)
            p.add_argument('--dry-run', action='store_true')
        if name == 'verify':
            p.add_argument('--snapshot-manifest', required=True)
    p = sub.add_parser('secrets')
    p.add_argument('--bundle', required=True)
    p.add_argument('--root', default='/')
    p.add_argument('--backup', required=True)
    p.add_argument('--wan-from', required=True)
    p.add_argument('--wan-to', required=True)
    p.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('verify-secrets')
    p.add_argument('--bundle', required=True)
    p.add_argument('--snapshot-manifest', required=True)
    args = ap.parse_args()
    handler = {'stage': cmd_stage, 'install': cmd_install, 'verify': cmd_verify, 'lint': cmd_lint,
               'secrets': cmd_secrets, 'verify-secrets': cmd_verify_secrets}[args.cmd]
    sys.exit(handler(args) or 0)


if __name__ == '__main__':
    main()
