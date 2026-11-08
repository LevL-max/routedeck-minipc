"""Bounded AWG recovery. Caller owns the WAN/mode and legacy rotator locks."""
import re
import time
from pathlib import Path
from wan_common import MODE, atomic, run
import wan_network as net

CONFIG = Path('/etc/amnezia/amneziawg/wg0.conf')
VARIANTS = CONFIG.parent/'variants'
SELECTION = (CONFIG.parent/'active', Path('/run/watchdog-rotator/active'))


def snapshot():
    return {str(p): p.read_bytes().hex() if p.exists() else None
            for p in (CONFIG, *SELECTION)}


def restore(saved):
    changed = False
    for name, value in saved.items():
        path = Path(name)
        # Transaction files are private, but restrict this helper's write scope too.
        if path not in (CONFIG, *SELECTION):
            raise ValueError('Unexpected AWG recovery path')
        data = bytes.fromhex(value) if value is not None else None
        old = path.read_bytes() if path.exists() else None
        if data == old:
            continue
        if path == CONFIG:
            if data is None: raise ValueError('AWG config is missing from recovery snapshot')
            changed = True
        if data is None: path.unlink(missing_ok=True)
        else: atomic(path, data)
    return changed


def peer_identity(data):
    # Ignore comments, hooks and formatting when detecting the same server/peer.
    return tuple(value.strip() for value in
                 re.findall(rb'(?im)^\s*(?:PublicKey|Endpoint)\s*=\s*([^#\r\n]+)', data))


def alternative():
    current = peer_identity(CONFIG.read_bytes())
    if not current: return None
    for name in ('temp_1', 'temp_2'):
        path = VARIANTS/(name+'.conf')
        if not path.is_file(): continue
        data = path.read_bytes()
        if peer_identity(data) and peer_identity(data) != current:
            return name, data
    return None


def assert_mode():
    if MODE.read_text().strip() != 'awg':
        raise RuntimeError('AWG recovery stopped because the traffic mode changed')


def profile_values():
    values = {}
    for line in CONFIG.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep: values[key.strip().lower()] = value.split('#', 1)[0].strip()
    return values


def mtu_limit(original, bearer):
    """Reserve AWG's S4 and bounded content padding as well as IP/UDP/WG headers.

    80 bytes retains the existing IPv6-outer allowance (20 bytes spare on IPv4).
    Do not change padding/shared server parameters to solve a path MTU problem.
    """
    values = profile_values()
    s4 = int(values.get('s4', '0'))
    content = values.get('contentpaddingaddition', '0')
    if not re.fullmatch(r'\d+(?:-\d+)?', content):
        raise ValueError('Invalid AWG ContentPaddingAddition range')
    extra = s4 + max(int(v) for v in content.split('-'))
    ceiling = int(bearer) - 80 - extra
    if extra: ceiling = ceiling // 16 * 16
    if ceiling < 1280:
        raise RuntimeError('AWG padding does not leave a 1280-byte tunnel MTU on this WAN')
    return min(int(original), ceiling)


def mtu_plan(physical, manual=None):
    """Local interface budget, not a claim of end-to-end path MTU discovery."""
    profile = profile_values().get('mtu')
    maximum = mtu_limit(9000, physical) if physical else None
    automatic = min(int(profile), maximum) if profile and maximum else int(profile) if profile else maximum
    if manual is not None:
        if type(manual) is not int or not 1280 <= manual <= 9000:
            raise ValueError('AWG MTU must be Auto or a whole number from 1280 to 9000')
        if maximum is not None and manual > maximum:
            raise ValueError('AWG MTU exceeds this WAN budget (maximum '+str(maximum)+')')
    return {'manual': manual, 'auto': automatic, 'selected': manual if manual is not None else automatic,
            'maximum': maximum, 'physical': physical, 'profile': int(profile) if profile else None}


def restart(replacement=None):
    assert_mode()
    desired = CONFIG.read_bytes() if replacement is None else replacement[1]
    # Marked and forwarded packets must fail closed while wg0 is absent.
    net.firewall()
    run(['ip', 'route', 'replace', 'unreachable', 'default', 'table', '100', 'metric', '32767'])
    run(['/usr/bin/awg-quick', 'down', 'wg0'], timeout=15, check=False)
    if run(['ip', 'link', 'show', 'dev', 'wg0'], timeout=3, check=False).returncode == 0:
        raise RuntimeError('AWG interface did not stop; config was not replaced')
    # SaveConfig in a user's current profile can rewrite wg0.conf on down.
    # Preserve exact edited bytes; install a rotated profile only after down.
    if CONFIG.read_bytes() != desired: atomic(CONFIG, desired)
    if replacement is not None:
        for path in SELECTION: atomic(path, (replacement[0]+'\n').encode())
    run(['/usr/bin/awg-quick', 'up', 'wg0'], timeout=20)
    # The worker already owns router-mode.lock. Do not wait for our own lock.
    run(['env', 'ROUTER_MODE_LOCK_HELD=1', 'ROUTER_MODE_OVERRIDE=awg',
         '/usr/local/sbin/router-restore-runtime'], timeout=40)
    assert_mode()


def ensure(verify, progress, force_restart=False, allow_rotation=True):
    """One restart, then at most one alternative; success always needs DNS/HTTPS.

    On failure leave config/selection on the last attempted profile. The WAN
    transaction restores its snapshot before rollback, or archives it on a
    failed rollback so manual Rotate remains available. Never select DIRECT.
    """
    assert_mode()
    if not force_restart:
        try:
            verify('awg')
            return 'unchanged'
        except Exception:
            assert_mode()
    next_profile = alternative() if allow_rotation else None
    for stage in ('restart', 'rotate'):
        if stage == 'rotate' and next_profile is None: break
        assert_mode()
        progress(stage)  # Persist intent before changing the tunnel or profile.
        try:
            restart(next_profile if stage == 'rotate' else None)
            verify('awg')
        except Exception:
            assert_mode()
            continue
        state = SELECTION[1].parent
        atomic(state/'fail_count', b'0\n')
        atomic(state/('last_rotate' if stage == 'rotate' else 'last_restart'),
               (str(int(time.time()))+'\n').encode())
        (state/'last_rxtx').unlink(missing_ok=True)
        return stage
    raise RuntimeError('AWG internet check failed after restart and available alternative profile')
