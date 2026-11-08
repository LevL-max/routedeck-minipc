"""Small shared state API; no network changes on import."""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path('/var/lib/router-wan')
SETTINGS = Path('/etc/router/wan.json')
ACTIVE = Path('/etc/router/wan-active.json')
MODE = Path('/etc/router/mode')
PROFILE = Path('/etc/router/core.env')


def host_profile():
    """Host facts shared with router-core: KEY=value lines, optional quotes."""
    result = {}
    try:
        lines = PROFILE.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.strip().partition('=')
        if sep and not key.startswith('#'):
            result[key.strip()] = value.strip().strip('"\'')
    return result


_HOST = host_profile()
ETH = _HOST.get('WAN_IF') or 'enp3s0'
LAN_IF = _HOST.get('LAN_IF') or '@@LAN_IF@@'
LAN_CIDR = _HOST.get('LAN_CIDR') or '192.168.50.0/24'
# The Ethernet WAN or a ModemManager wwan link: the only bindings router-wan rewrites.
PHYSICAL_RE = '(?:' + re.escape(ETH) + '|wwan[0-9]+)'
# router-core hosts (Mini-PC 1/2) keep iptables guard chains and router-core modes. A host with
# MODE_ENGINE=apply-mode (Mini-PC 3) applies modes with router-apply-mode from nft files, which
# own forwarding and NAT and follow the selected WAN themselves (FIREWALL=nft).
MODE_ENGINE = _HOST.get('MODE_ENGINE') or 'router-core'
FIREWALL = _HOST.get('FIREWALL') or 'iptables'
DEFAULTS = {'preferred': 'ethernet', 'fallback': False, 'failback_seconds': 120,
            'awg_mtu': {'ethernet': None, 'lte': None},
            'mobile': {'auto_apn': True, 'apn': '', 'username': '', 'password': '',
                       'allow_roaming': False, 'sim_key': ''}}

def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default

def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    old = path.stat() if path.exists() else None
    with tmp.open('wb') as f:
        f.write(data if isinstance(data, bytes) else (json.dumps(data, indent=2)+'\n').encode())
        f.flush()
        os.fsync(f.fileno())
        os.fchmod(f.fileno(), old.st_mode & 0o777 if old else mode)
        if old:
            os.fchown(f.fileno(), old.st_uid, old.st_gid)
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

def settings():
    value = read(SETTINGS, {})
    result = dict(DEFAULTS, **value)
    result['mobile'] = dict(DEFAULTS['mobile'], **value.get('mobile', {}))
    result['awg_mtu'] = dict(DEFAULTS['awg_mtu'], **value.get('awg_mtu', {}))
    return result

def active():
    return read(ACTIVE, {'wan': 'ethernet', 'interface': ETH, 'generation': 0})

def active_interface():
    return active().get('interface', ETH)

def run(args, timeout=15, check=True, data=None):
    p = subprocess.run(args, input=data, capture_output=True, text=True, encoding='utf-8', timeout=timeout,
                       env=dict(os.environ, LC_ALL='C.UTF-8'))
    if check and p.returncode:
        # Arguments may contain APN credentials; do not include them or stderr in API errors.
        raise RuntimeError(Path(args[0]).name + ' failed (exit ' + str(p.returncode) + ')')
    return p

def command_json(args, timeout=15):
    return json.loads(run(args, timeout).stdout)

@contextlib.contextmanager
def locks(skip_updater=False):
    fds = []
    try:
        # router-apply-mode takes its own lock; router-wan calls it, so it must not hold that lock.
        paths = (('/run/router-wan.lock',) if MODE_ENGINE == 'apply-mode' else
                 ('/run/router-updater.lock', '/run/lock/router-mode.lock', '/run/router-wan.lock',
                  '/run/watchdog-rotator/lock'))
        for path in paths:
            if skip_updater and path == '/run/router-updater.lock': continue
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            fds.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(fds): os.close(fd)

def event(message, **kw):
    atomic(ROOT/'last-action.json', dict(message=message, at=int(time.time()), **kw))
