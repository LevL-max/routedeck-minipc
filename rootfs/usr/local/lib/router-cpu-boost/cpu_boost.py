#!/usr/bin/env python3
"""CPU boost (turbo) switch for the router panels. Changes need root.

Off keeps the CPU at its base clock (Ryzen Embedded R1505G: 2.4 GHz instead of up to
3.3 GHz), which lowers peak power. The choice is saved in /etc/router/cpu-boost and
router-cpu-boost.service applies it at boot; without a saved choice the firmware default
(boost on) stays untouched.

CLI: router-cpu-boost status|on|off|apply
"""
import json
import os
import sys
from pathlib import Path

BOOST = Path('/sys/devices/system/cpu/cpufreq/boost')
STATE = Path('/etc/router/cpu-boost')
ENDPOINT = '/api/cpu-boost'
BOOST_MHZ = {'R1505G': 3300}  # boost clocks are not exposed by acpi-cpufreq


def khz(path):
    try:
        return int(Path(path).read_text())
    except (OSError, ValueError):
        return None


def model():
    try:
        for line in Path('/proc/cpuinfo').read_text().splitlines():
            if line.startswith('model name'):
                return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return ''


def status():
    name = model()
    base = khz('/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq')
    current = [khz(p) for p in Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_cur_freq')]
    current = [c for c in current if c]
    try:
        saved = STATE.read_text().strip() or None
    except OSError:
        saved = None
    return {'ok': True, 'available': BOOST.exists(),
            'boost': BOOST.read_text().strip() == '1' if BOOST.exists() else None,
            'saved': saved, 'model': name,
            'base_mhz': base // 1000 if base else None,
            'boost_mhz': next((mhz for key, mhz in BOOST_MHZ.items() if key in name), None),
            'current_mhz': max(current) // 1000 if current else None}


def set_boost(on):
    if not BOOST.exists():
        raise RuntimeError('This CPU frequency driver has no boost switch')
    BOOST.write_text('1' if on else '0')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name('.cpu-boost.tmp')
    tmp.write_text('on\n' if on else 'off\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE)
    return status()


def apply_saved():
    try:
        saved = STATE.read_text().strip()
    except OSError:
        return 'no saved choice; firmware default kept'
    if saved not in ('on', 'off') or not BOOST.exists():
        return 'nothing to apply'
    BOOST.write_text('1' if saved == 'on' else '0')
    return 'boost ' + saved


# Panel handlers (RouteDeck style: h.path, h.allowed(), h.authorized(), h.j(obj, status)).
def get(h):
    if h.path != ENDPOINT:
        return False
    if not h.allowed():
        h.j({'ok': False, 'error': 'LAN access only'}, 403)
    else:
        h.j(status())
    return True


def post(h):
    if h.path != ENDPOINT:
        return False
    if not h.allowed() or not h.authorized():
        h.j({'ok': False, 'error': 'Request rejected'}, 403)
        return True
    try:
        size = int(h.headers.get('Content-Length', '0'))
        if not 0 < size <= 128:
            raise ValueError('Invalid request size')
        body = json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or type(body.get('boost')) is not bool:
            raise ValueError('Expected {"boost": true|false}')
        h.j(set_boost(body['boost']))
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        h.j({'ok': False, 'error': str(exc)[:200]}, 409)
    return True


def main(argv):
    command = argv[1] if len(argv) > 1 else 'status'
    if command == 'status':
        print(json.dumps(status(), indent=1))
    elif command in ('on', 'off'):
        print(json.dumps(set_boost(command == 'on'), indent=1))
    elif command == 'apply':
        print(apply_saved())
    else:
        raise SystemExit('usage: router-cpu-boost status|on|off|apply')


if __name__ == '__main__':
    main(sys.argv)
