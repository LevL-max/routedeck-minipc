"""Public egress identity and explicitly requested reboot for the LAN panel."""
import ipaddress
import json
import platform
import threading
import time
import uuid
from pathlib import Path
import router_update_core as c

REBOOT_REQUIRED = Path('/var/run/reboot-required')
REBOOT_PACKAGES = Path('/var/run/reboot-required.pkgs')
REBOOT_STATE = Path('/run/router-panel-reboot.json')
_lock = threading.Lock()
_geo = {}
_fetching = False
_next_check = 0
_request_generation = 0
_observed_path = None
GEO_REFRESH_SECONDS = 30


def text(value, length=160):
    return ''.join(ch for ch in str(value or '') if ch.isprintable())[:length]


def parse_geo(data):
    if data.get('success') is not True:
        raise RuntimeError('IP lookup unavailable: ' + text(data.get('message')))
    address = ipaddress.ip_address(data['ip'])
    if address.version != 4 or not address.is_global:
        raise RuntimeError('IP lookup returned an invalid public IPv4 address')
    connection = data.get('connection') or {}
    asn = connection.get('asn')
    if asn is not None and (isinstance(asn, bool) or not str(asn).isdigit() or not 0 < int(asn) < 2**32):
        raise RuntimeError('IP lookup returned an invalid ASN')
    return {'ip': str(address), 'asn': 'AS' + str(asn) if asn else None,
            'org': text(connection.get('org') or connection.get('isp')),
            'city': text(data.get('city')), 'country': text(data.get('country')),
            'source': 'ipwho.is'}


YANDEX_IP_URL = 'https://ipv4-internet.yandex.net/api/v0/ip'


def _lookup(transport, url, connect, total):
    result = c.run(['curl', '--silent', '--show-error', '--fail', '--proto', '=https',
                    '--connect-timeout', str(connect), '--max-time', str(total), '--max-filesize', '32768',
                    '--proxy', transport.url, '--noproxy', '', url], timeout=total + 5, record=False)
    if len(result.stdout) > 32768:
        raise RuntimeError('IP lookup response is too large')
    transport.validate_route()
    return json.loads(result.stdout)


def parse_plain_ip(data):
    address = ipaddress.ip_address(str(data).strip())
    if address.version != 4 or not address.is_global:
        raise RuntimeError('IP lookup returned an invalid public IPv4 address')
    return {'ip': str(address), 'asn': None, 'org': 'Yandex IP check: no ASN or location',
            'city': '', 'country': '', 'source': 'yandex'}


def fetch_geo():
    # routedeck-access-v1: the ISP drops some CDN addresses of ipwho.is, so each
    # address gets 3 s instead of 12; in DIRECT and AWG, where Yandex sees the
    # real exit, Yandex names the address when ipwho.is cannot be reached.
    with c.transport_for('current') as transport:
        transport.connect_timeout = 3
        try:
            return parse_geo(_lookup(transport, 'https://ipwho.is/?fields=success,ip,city,country,connection', 8, 10))
        except Exception:
            if transport.mode not in ('direct', 'awg'):
                raise
        return parse_plain_ip(_lookup(transport, YANDEX_IP_URL, 4, 6))


def invalidate_geo():
    global _next_check, _geo, _request_generation
    with _lock:
        _request_generation += 1
        _next_check = 0
        _geo = {}


def _refresh_geo(mode, generation):
    global _geo, _fetching, _next_check
    try:
        result = dict(fetch_geo(), ok=True, checked_at=int(time.time()), mode=mode)
    except Exception:
        result = {'ok': False, 'error': 'Exit IP lookup unavailable through the current mode',
                  'mode': mode, 'checked_at': int(time.time())}
    with _lock:
        if generation == _request_generation and mode == c.MODE.read_text().strip():
            _geo = result
            _next_check = time.monotonic() + (GEO_REFRESH_SECONDS if result['ok'] else 60)
        _fetching = False


def network_status(proxy=None):
    global _fetching, _observed_path, _request_generation, _geo, _next_check
    mode = c.MODE.read_text().strip()
    with _lock:
        # A new connection must be measured after a Mihomo selector change,
        # even when the router mode and TUN interface have stayed the same.
        path = (mode, proxy if mode == 'mihomo' else None, c.read_json(Path('/etc/router/wan-active.json'), {}))
        if path != _observed_path:
            _observed_path = path
            _request_generation += 1
            _geo = {}
            _next_check = 0
        if not _fetching and (mode != _geo.get('mode') or time.monotonic() >= _next_check):
            _fetching = True
            threading.Thread(target=_refresh_geo, args=(mode, _request_generation), daemon=True).start()
        result = dict(_geo) if _geo.get('mode') == mode else {'mode': mode, 'ok': False}
        result['checking'] = _fetching
        result['refresh_seconds'] = GEO_REFRESH_SECONDS
    # WAN next-hop is distinct from the public exit address.
    try:
        result['wan_gateway'] = c.read_json(Path('/etc/router/wan-active.json'), {}).get('gateway')
    except Exception:
        result['wan_gateway'] = None
    return result


def reboot_status():
    state = c.read_json(REBOOT_STATE, {})
    pending = state.get('status') in ('scheduled', 'rebooting') and time.time() - state.get('at', 0) < 180
    if not pending and state.get('status') in ('scheduled', 'rebooting'):
        state = dict(state, status='failed', error='Reboot did not complete; check system logs')
    return {'required': REBOOT_REQUIRED.exists(), 'packages': REBOOT_PACKAGES.read_text().splitlines()[:30]
            if REBOOT_PACKAGES.exists() else [], 'pending': pending, 'state': state}


def reboot_ready():
    import kernel_policy as k
    _, kernels = k.inventory()
    if not kernels:
        raise RuntimeError('No installed kernel found')
    k.verify_bootable([platform.release(), kernels[0]])


def request_reboot(body):
    if not isinstance(body, dict) or body.get('confirm') is not True:
        raise ValueError('Confirm the router reboot')
    import panel_updates as ui
    import kernel_policy as k
    with ui.mode_guard(), k.package_locks():
        reboot_ready()
        ident = uuid.uuid4().hex
        c.write_json(REBOOT_STATE, {'status': 'scheduled', 'at': int(time.time()), 'id': ident})
        try:
            c.run(['systemd-run', '--quiet', '--collect', '--unit=router-panel-reboot-' + ident,
                   '--on-active=5s', '--timer-property=AccuracySec=1s',
                   '/usr/bin/python3', '/usr/local/lib/router-updater/panel_system.py', 'reboot', ident], timeout=10, record=False)
        except Exception as exc:
            c.write_json(REBOOT_STATE, {'status': 'failed', 'at': int(time.time()), 'error': str(exc)})
            raise
    return {'ok': True, 'message': 'Reboot scheduled. Internet and this panel will temporarily disconnect.'}


def perform_reboot(ident):
    import kernel_policy as k
    try:
        with c.exclusive(), k.package_locks():
            state = c.read_json(REBOOT_STATE, {})
            if state.get('id') != ident or state.get('status') != 'scheduled':
                raise RuntimeError('Reboot request is no longer active')
            reboot_ready()
            c.write_json(REBOOT_STATE, {'status': 'rebooting', 'at': int(time.time())})
            c.run(['systemctl', 'reboot'], timeout=30, record=False)
    except Exception as exc:
        c.write_json(REBOOT_STATE, {'status': 'failed', 'at': int(time.time()), 'error': str(exc)})
        raise


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != 'reboot':
        raise SystemExit('Expected reboot')
    perform_reboot(sys.argv[2])
