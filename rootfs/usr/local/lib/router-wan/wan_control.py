"""Serialized WAN changes with persistent rollback independent of the browser/SSH session."""
import contextlib
import copy
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
import yaml
from wan_common import ACTIVE, ETH, MODE, MODE_ENGINE, PHYSICAL_RE, ROOT, SETTINGS, active, active_interface, atomic, event, locks, read, run, settings
import wan_modem as modem
import wan_network as net
import wan_awg as awg
import wan_bands as bands  # lte-bands-v1: 4G frequency bands

TX = ROOT/'transaction.json'
JOB = ROOT/'job.json'
FAILED = ROOT/'failed-recovery.json'
NAMES = {'ethernet': 'Ethernet', 'lte': '4G'}  # wan-force-v1
CONFIGS = (Path('/etc/xray/config.json'), Path('/etc/mihomo/config.yaml'), Path('/etc/sing-box/config.json'))
WG_MTU = Path('/sys/class/net/wg0/mtu')
ETH_MTU = Path('/sys/class/net')/ETH/'mtu'

def busy():
    state = run(['systemctl', 'show', 'router-wan-worker.service', '-p', 'ActiveState', '--value'],
                timeout=3, check=False).stdout.strip()
    return state in ('active', 'activating', 'deactivating') or TX.exists()

def patch_bindings(interface, only=None):
    result = {}
    # A host without one of these engines installed simply has nothing to rebind there.
    xray = json.loads(CONFIGS[0].read_text()) if only in (None, 'xray') and CONFIGS[0].exists() else {}
    changed = False
    def walk(value):
        nonlocal changed
        if isinstance(value, dict):
            if isinstance(value.get('interface'), str) and re.fullmatch(PHYSICAL_RE, value['interface']):
                changed |= value['interface'] != interface
                value['interface'] = interface
            for item in value.values(): walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
    walk(xray)
    if changed: result[str(CONFIGS[0])] = (json.dumps(xray, indent=2, ensure_ascii=False)+'\n').encode()
    raw = CONFIGS[1].read_text() if only in (None, 'mihomo') and CONFIGS[1].exists() else 'interface-name: '+interface+'\n'
    parsed = yaml.safe_load(raw) or {}
    base = raw
    if MODE_ENGINE == 'apply-mode' and only in (None, 'mihomo') and CONFIGS[1].exists():
        # Mini-PC 3: TUN auto-detect would bind every Mihomo dial (DNS, DIRECT, proxy servers) to
        # main's default route, i.e. Ethernet. Pin Mihomo to the selected WAN instead, as on PC1.
        if 'interface-name' not in parsed:
            raw = 'interface-name: '+interface+'\n'+raw
            parsed = dict(parsed, **{'interface-name': interface})
        if isinstance(parsed.get('tun'), dict) and parsed['tun'].get('auto-detect-interface') is not False:
            raw, n = re.subn(r'(?m)^(  auto-detect-interface:)[ \t]*\S+', r'\1 false', raw)
            if not n: raw, n = re.subn(r'(?m)^tun:[ \t]*\n', 'tun:\n  auto-detect-interface: false\n', raw, count=1)
            if not n: raise RuntimeError('Mihomo tun section not found')
    elif 'interface-name' not in parsed:
        # No global binding and nothing to pin: Mihomo follows the policy routing of the selected WAN.
        raw = base = 'interface-name: '+interface+'\n'
        parsed = {'interface-name': interface}
    if parsed.get('interface-name') not in (ETH, interface) and not re.fullmatch(r'wwan[0-9]+', str(parsed.get('interface-name', ''))):
        raise RuntimeError('Mihomo global interface-name is not a managed physical WAN')
    modified, count = re.subn(r'(?m)^interface-name:[^\r\n]*', 'interface-name: '+interface, raw)
    if count != 1: raise RuntimeError('Expected exactly one top-level Mihomo interface-name')
    # Also update explicit physical bindings on native proxies, leaving loopback SOCKS bindings intact.
    modified = re.sub(r'(?m)^(\s+interface-name:\s*)[\'\"]?'+PHYSICAL_RE+r'[\'\"]?\s*$',
                      lambda m: m[1]+interface, modified)
    if modified != base: result[str(CONFIGS[1])] = modified.encode()
    # sing-box is the TUN mode engine of router-core hosts only (Mini-PC 3 has no TUN mode).
    if only in (None, 'sing-box') and CONFIGS[2].exists() and MODE_ENGINE != 'apply-mode':
        sb = json.loads(CONFIGS[2].read_text())
        prior = copy.deepcopy(sb)
        mark = net.MARKS['ethernet' if interface == ETH else 'lte']
        sb.setdefault('route', {}).update({'auto_detect_interface': False,
                                         'default_interface': interface, 'default_mark': mark})
        for outbound in sb.get('outbounds', []):
            if re.fullmatch(PHYSICAL_RE, str(outbound.get('bind_interface', ''))):
                outbound['bind_interface'] = interface
            if outbound.get('routing_mark') in net.MARKS.values():
                outbound['routing_mark'] = mark
        if sb != prior: result[str(CONFIGS[2])] = (json.dumps(sb, indent=2, ensure_ascii=False)+'\n').encode()
    # Service startup only touches its own config. Validation uses the engine's existing ExecStartPre.
    if only: return result
    checkdir = ROOT/'validate'
    checkdir.mkdir(parents=True, exist_ok=True)
    if str(CONFIGS[0]) in result:
        p = checkdir/'xray.json'; atomic(p, result[str(CONFIGS[0])])
        run(['/usr/local/bin/xray', 'run', '-test', '-config', str(p)], timeout=20)
    if str(CONFIGS[1]) in result:
        p = checkdir/'mihomo.yaml'; atomic(p, result[str(CONFIGS[1])])
        run(['/usr/local/bin/mihomo', '-t', '-d', '/etc/mihomo', '-f', str(p)], timeout=25)
    if str(CONFIGS[2]) in result:
        p = checkdir/'sing-box.json'; atomic(p, result[str(CONFIGS[2])])
        run(['/usr/bin/sing-box', 'check', '-c', str(p)], timeout=20)
    return result

def bind_awg():
    index = Path('/sys/class/net/wg0/ifindex')
    if not index.exists(): return
    selected = active()
    cfg = net.ethernet() if selected['wan'] == 'ethernet' else read(ROOT/'bearer.json', {})
    if not cfg: return
    signature = {'ifindex': index.read_text().strip(), 'wan': selected['wan'],
                 'interface': cfg['interface'], 'address': cfg['address']}
    desired = net.MARKS[selected['wan']]
    current = run(['awg', 'show', 'wg0', 'fwmark']).stdout.strip()
    if current != hex(desired) or read(ROOT/'awg-path.json') != signature:
        # Updating the device mark also clears cached endpoint source addresses.
        # Retain the interface, peer and session while selecting the new underlay.
        run(['awg', 'set', 'wg0', 'fwmark', str(desired)])
        atomic(ROOT/'awg-path.json', signature)


def mtu_plans(values=None):
    values = settings()['awg_mtu'] if values is None else values
    if not isinstance(values, dict) or set(values) != {'ethernet', 'lte'}:
        raise ValueError('Provide AWG MTU for both Ethernet and 4G')
    physical = {'ethernet': int(ETH_MTU.read_text()) if ETH_MTU.exists() else None,
                'lte': read(ROOT/'bearer.json', {}).get('mtu')}
    return {wan: awg.mtu_plan(physical[wan], value) for wan, value in values.items()}


def mtu_status():
    result = {'values': settings()['awg_mtu'], 'active_wan': active()['wan'],
              'actual': int(WG_MTU.read_text()) if WG_MTU.exists() else None}
    try: result['plans'] = mtu_plans()
    except (OSError, ValueError, RuntimeError) as exc: result['error'] = str(exc)
    return result


def adapt_mtu():
    if not WG_MTU.exists(): return
    wan = active()['wan']
    physical = (int(ETH_MTU.read_text()) if ETH_MTU.exists() else None) if wan == 'ethernet' else read(ROOT/'bearer.json', {}).get('mtu')
    wanted = awg.mtu_plan(physical, settings()['awg_mtu'][wan])['selected']
    if wanted is None: return
    if wanted != int(WG_MTU.read_text()):
        run(['ip', 'link', 'set', 'wg0', 'mtu', str(wanted)])
        run(['ip', 'route', 'flush', 'cache'])
    (ROOT/'mtu.json').unlink(missing_ok=True)


def save_mtu(values):
    mtu_plans(values)  # Validate both WANs before saving anything.
    old = settings(); new = copy.deepcopy(old); new['awg_mtu'] = values
    mode = MODE.read_text().strip()
    if mode != 'awg' or not WG_MTU.exists():
        atomic(SETTINGS, new)
        event('AWG MTU preferences saved; applied when AWG starts', ok=True)
        return
    tx = {'kind': 'awg_mtu', 'before': active(), 'mode': mode, 'settings': old,
          'files': {}, 'mtu_before': int(WG_MTU.read_text()), 'deadline': time.time()+150,
          'allow_awg_rotation': False}
    atomic(TX, tx)
    try:
        atomic(SETTINGS, new)
        adapt_mtu()
        verify_mode('awg')
        TX.unlink()
        event('AWG MTU saved; tunnel internet verified without restart', ok=True)
    except Exception as exc:
        rollback(tx)
        raise RuntimeError('New MTU did not pass its check; previous MTU restored') from exc

DNS_RU = Path('/usr/local/sbin/router-dns-ru')
_dns_follow = {'signature': None, 'tried': 0.0}


def follow_dns(force=False):
    """apply-mode hosts: keep Mihomo's 'ISP' RU DNS profile on the provider DNS of the active
    WAN (DHCP on Ethernet, the bearer's carrier DNS on 4G). Cheap when nothing changed."""
    if MODE_ENGINE != 'apply-mode' or not DNS_RU.exists(): return
    selected = active()
    signature = (selected.get('interface'),
                 tuple(read(ROOT/'bearer.json', {}).get('dns', [])) if selected.get('wan') == 'lte' else ())
    if not force and (signature == _dns_follow['signature'] or time.monotonic()-_dns_follow['tried'] < 60): return
    _dns_follow['tried'] = time.monotonic()
    if run([str(DNS_RU), 'refresh'], timeout=90, check=False).returncode == 0:
        _dns_follow['signature'] = signature


def mihomo_reload(config_changed, drop_connections):
    """Apply a rewritten Mihomo config through its API and close sessions on the old WAN."""
    if run(['systemctl', 'is-active', '--quiet', 'mihomo'], check=False).returncode: return
    import urllib.request
    def call(method, path, body=None):
        request = urllib.request.Request('http://127.0.0.1:9090'+path, method=method, data=body,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=20) as response: return response.status
    if config_changed:
        status = call('PUT', '/configs?force=true', json.dumps({'path': str(CONFIGS[1]), 'payload': ''}).encode())
        if status not in (200, 204): raise RuntimeError('Mihomo did not reload its config')
    if drop_connections:
        try: call('DELETE', '/connections')
        except OSError: pass  # Sessions then time out on their own.


def restart_path(mode, changed):
    if str(CONFIGS[0]) in changed and not run(['systemctl', 'is-active', '--quiet', 'xray'], check=False).returncode:
        run(['systemctl', 'restart', 'xray'], timeout=30)
    if MODE_ENGINE == 'apply-mode':
        # router-apply-mode renders forwarding, NAT and DIRECT DNS for the selected WAN.
        # Mihomo is hot-reloaded, not restarted: a restart recreates its TUN link, and the
        # networkd hooks then reapply the whole mode a second time.
        mihomo_reload(str(CONFIGS[1]) in changed, drop_connections=mode == 'mihomo')
        run(['/usr/local/sbin/router-apply-mode', mode, '--runtime-only'], timeout=150)
        follow_dns(force=True)
        if mode == 'awg': adapt_mtu()
        run(['ip', 'route', 'flush', 'cache'])
        bind_awg()
        net.refresh_connected()
        if MODE.read_text().strip() != mode: raise RuntimeError('Traffic mode changed unexpectedly')
        return
    if mode == 'mihomo':
        run(['systemctl', 'restart', 'mihomo'], timeout=40)
    elif mode == 'tun':
        run(['ip', 'route', 'replace', 'unreachable', 'default', 'table', '110', 'metric', '32767'])
        run(['systemctl', 'restart', 'sing-box'], timeout=35)
        for _ in range(40):
            if Path('/sys/class/net/tun0').exists(): break
            time.sleep(.25)
        run(['ip', 'route', 'replace', 'default', 'dev', 'tun0', 'table', '110'])
    elif mode == 'awg':
        # Retain the live peer, keys and tunnel interface; underlay routing selects the new WAN.
        adapt_mtu()
        run(['ip', 'route', 'flush', 'cache'])
    bind_awg()
    net.refresh_connected()
    if MODE.read_text().strip() != mode: raise RuntimeError('Traffic mode changed unexpectedly')

def verify_mode(mode):
    if MODE.read_text().strip() != mode: raise RuntimeError('Traffic mode changed')
    if MODE_ENGINE == 'apply-mode':
        run(['/usr/local/sbin/router-assert-mode', mode], timeout=20)
        # The router's own request through the same path as the LAN in this mode.
        via = (['--proxy', 'http://127.0.0.1:7895', '--noproxy', ''] if mode == 'mihomo' else
               ['--interface', 'wg0' if mode == 'awg' else active_interface()])
        for url in ('https://ya.ru/robots.txt', 'https://www.apple.com/', 'https://www.microsoft.com/'):
            p = run(['curl', '-4', '--silent', '--fail', '--show-error', '--proto', '=https', *via,
                     '--max-time', '10', '--connect-timeout', '4', '--max-filesize', '1048576',
                     '--output', '/dev/null', url], timeout=13, check=False)
            if p.returncode == 0:
                if MODE.read_text().strip() != mode: raise RuntimeError('Traffic mode changed')
                return
        raise RuntimeError('The selected traffic mode did not pass its DNS/HTTPS internet check')
    sys.path.insert(0, '/usr/local/lib/router-updater')
    import router_update_core as core
    core.verify_runtime()
    # Keep this gate read-only: the legacy runtime "check-only" actually restarts routing.
    from transport import Transport
    with Transport('current', wan=active_interface()) as transport:
        for url in ('https://ya.ru/robots.txt', 'https://www.apple.com/', 'https://www.microsoft.com/'):
            p = run(['curl', '-4', '--silent', '--fail', '--show-error', '--proto', '=https',
                     '--proxy', transport.url, '--noproxy', '', '--max-time', '10',
                     '--connect-timeout', '4', '--max-filesize', '1048576', '--output', '/dev/null', url],
                    timeout=13, check=False)
            if p.returncode == 0:
                if MODE.read_text().strip() != mode: raise RuntimeError('Traffic mode changed')
                return
    raise RuntimeError('The selected traffic mode did not pass its DNS/HTTPS internet check')


def ensure_mode(tx, force_awg_restart=False):
    # On apply-mode hosts the mode was just re-applied by router-apply-mode; no AWG rotation there.
    if tx['mode'] != 'awg' or MODE_ENGINE == 'apply-mode':
        verify_mode(tx['mode'])
        return
    def progress(stage):
        tx['awg_touched'] = True
        tx['awg_stage'] = stage
        atomic(TX, tx)
        atomic(JOB, {'status': 'running', 'at': int(time.time()),
                     'message': 'Verifying AWG after '+('profile rotation' if stage == 'rotate' else 'restart')})
    awg.ensure(verify_mode, progress, force_restart=force_awg_restart,
               allow_rotation=tx.get('allow_awg_rotation', True))


def failed_recovery(tx, exc):
    # Retain the private snapshot for diagnosis without blocking manual Restart /
    # Rotate behind an eternal "busy" transaction or repeatedly undoing the user.
    message = ('WAN recovery failed; '+MODE.read_text().strip().upper()+
               ' remains selected. Manual recovery required; automatic WAN switching paused.')
    atomic(FAILED, dict(tx, failed_at=int(time.time()), error=str(exc)[:300], message=message))
    TX.unlink(missing_ok=True)
    atomic(JOB, {'status': 'recovery_failed', 'at': int(time.time()), 'message': message})
    event(message, ok=False, recovered=False, active=active()['wan'])

def rollback(tx):
    if tx.get('kind') == 'bands':
        bands.rollback(tx)
        return
    if tx.get('kind') == 'awg_mtu':
        try:
            if MODE.read_text().strip() != tx['mode'] or active() != tx['before']:
                raise RuntimeError('WAN or mode changed; stale MTU recovery stopped')
            atomic(SETTINGS, tx['settings'])
            if WG_MTU.exists():
                run(['ip', 'link', 'set', 'wg0', 'mtu', str(tx['mtu_before'])])
                run(['ip', 'route', 'flush', 'cache'])
            verify_mode('awg')
        except Exception as exc:
            failed_recovery(tx, exc)
            raise RuntimeError('Previous MTU restored but internet could not be verified') from exc
        TX.unlink(missing_ok=True)
        event('Previous AWG MTU restored and internet verified', ok=False, recovered=True)
        return
    # Always restore the underlay first. A failed traffic engine never selects DIRECT.
    tx.update(phase='rollback', deadline=time.time()+300)
    atomic(TX, tx)
    try:
        if MODE.read_text().strip() != tx['mode']:
            raise RuntimeError('Traffic mode changed; stale recovery must not overwrite it')
        cfg = net.ethernet() if tx['before']['wan'] == 'ethernet' else read(ROOT/'bearer.json')
        net.select(tx['before']['wan'], cfg)
        for path, data in tx['files'].items(): atomic(path, bytes.fromhex(data))
        atomic(SETTINGS, tx['settings'])
        changed = awg.restore(tx['awg']) if tx.get('awg') else False
        restart_path(tx['mode'], tx['files'])
        adapt_mtu()
        if not tx.get('forced'):  # wan-force-v1: a forced switch is undone without a mode check
            ensure_mode(tx, force_awg_restart=changed or tx.get('awg_touched', False))
    except Exception as exc:
        failed_recovery(tx, exc)
        raise RuntimeError('WAN/tunnel recovery failed; manual recovery required') from exc
    TX.unlink(missing_ok=True)
    FAILED.unlink(missing_ok=True)
    event('Previous WAN restored'+('' if tx.get('forced') else '; '+tx['mode'].upper()+' internet verified'),
          ok=False, recovered=True, active=tx['before']['wan'])

def wan_down(wan):
    """wan-force-v1: this WAN has no link or no IPv4 at all (cable out, no lease, bearer gone)."""
    return not net.link(wan, net.ethernet() if wan == 'ethernet' else read(ROOT/'bearer.json'))


def force_target(target):
    """wan-force-v1: the target's IPv4 settings as they are now; None while it has none."""
    if target == 'ethernet': return net.ethernet(), ''
    try: return modem.ensure()[1], ''
    except Exception as exc: return None, str(exc)[:160]


def keep_target(tx, target):
    """wan-force-v1: the traffic mode failed its check on the new WAN. Going back only helps while the
    previous WAN still has internet. Otherwise stay on the new WAN, which passed its own check, and put
    back the user's AWG profile if the check rotated it."""
    before = tx['before']['wan']
    cfg = net.ethernet() if before == 'ethernet' else read(ROOT/'bearer.json')
    if net.link(before, cfg) and net.probe(before, cfg)['ok']: return None
    note = ''
    try:
        if tx.get('awg') and awg.restore(tx['awg']): awg.restart()
    except Exception as exc:
        note = '; AWG restart failed: '+str(exc)[:120]
    TX.unlink(missing_ok=True)
    FAILED.unlink(missing_ok=True)
    message = (NAMES[target]+' kept: '+tx['mode'].upper()+' did not pass its internet check there, and '
               +NAMES[before]+' has no internet to go back to'+note)
    event(message, ok=False, active=target, reason=tx.get('reason'), kept=True)
    return {'status': 'warning', 'message': message}

def switch(target, reason='manual', allow_awg_rotation=True, forced=False):
    current = active()
    # wan-force-v1: a forced switch takes the WAN as it is, also with no link or bearer yet.
    cfg, note = force_target(target) if forced else (net.ethernet() if target == 'ethernet' else modem.ensure()[1], '')
    net.prepare(cfg if target == 'ethernet' else net.ethernet(),
                cfg if target == 'lte' else read(ROOT/'bearer.json'))
    unverified = False
    if not forced and not net.probe(target, cfg)['ok']:
        # wan-force-v1: a WAN with no link at all is worse than one with a link, verified or not.
        if not (net.link(target, cfg) and wan_down(current['wan'])):
            raise RuntimeError('Requested WAN has no verified internet connection; tick Force to switch without checks')
        unverified = True
    interface = cfg['interface'] if cfg else (ETH if target == 'ethernet' else
                                              read(ROOT/'bearer.json', {}).get('interface', 'wwan0'))
    if current['wan'] == target and current['interface'] == interface and not FAILED.exists():
        return {'status': 'complete', 'message': NAMES[target]+' is already in use'} if forced else None
    mode = MODE.read_text().strip()
    changes = patch_bindings(interface)
    tx = {'before': current, 'mode': mode, 'settings': settings(),
          'files': {p: Path(p).read_bytes().hex() for p in changes}, 'deadline': time.time()+300,
          'target': target, 'reason': reason, 'allow_awg_rotation': allow_awg_rotation, 'forced': forced}
    if mode == 'awg': tx['awg'] = awg.snapshot()
    atomic(TX, tx)
    try:
        net.firewall()
        for path, data in changes.items(): atomic(path, data)
        net.select(target, cfg)
        restart_path(mode, changes)
        if forced or unverified:
            # wan-force-v1: no mode check and no rollback; the daemon keeps routes and the bearer current.
            TX.unlink()
            FAILED.unlink(missing_ok=True)
            if forced:
                message = (NAMES[target]+' forced: no internet checks, rollback or automatic switching while Force is on'
                           +('' if cfg else '. No IPv4 on '+NAMES[target]+' yet'+(' ('+note+')' if note else '')
                             +'; router-wan keeps trying'))
            else:
                message = (NAMES[target]+' selected without a verified internet check: '
                           +NAMES[current['wan']]+' has no link')
            event(message, ok=forced, active=target, reason='force' if forced else reason)
            return {'status': 'complete' if forced else 'warning', 'message': message}
        try:
            ensure_mode(tx)
        except Exception:
            kept = keep_target(tx, target)
            if kept is None: raise
            return kept
        TX.unlink()
        FAILED.unlink(missing_ok=True)
        event('WAN switched; '+mode.upper()+' preserved', ok=True, active=target, reason=reason)
    except Exception as exc:
        rollback(tx)
        if forced: raise RuntimeError('Forced WAN change failed; previous WAN restored') from exc
        raise RuntimeError('Requested WAN change failed; previous WAN and '+mode.upper()+' internet restored. '
                           'Tick Force to switch without checks') from exc

def request(body):
    if not isinstance(body, dict): raise ValueError('JSON object required')
    action = body.get('action')
    if action not in ('select', 'fallback', 'force', 'mobile', 'awg_mtu', 'bands'): raise ValueError('Unknown WAN action')
    if action == 'bands': bands.check(body)
    if action == 'awg_mtu': mtu_plans(body.get('values', {}))
    if action == 'select' and body.get('wan') not in ('ethernet', 'lte'): raise ValueError('Invalid WAN')
    if action == 'fallback' and type(body.get('enabled')) is not bool: raise ValueError('Invalid fallback setting')
    if action == 'force' and type(body.get('enabled')) is not bool: raise ValueError('Invalid force setting')
    if action == 'mobile' and active()['wan'] == 'lte':
        raise RuntimeError('Select Ethernet before replacing APN settings; the active 4G connection will be kept')
    if MODE_ENGINE == 'apply-mode':
        guard = contextlib.nullcontext()  # the worker serializes with router-wan.lock
    else:
        sys.path.insert(0, '/usr/local/lib/router-updater')
        import panel_updates
        guard = panel_updates.mode_guard()
    with guard:
        if busy(): raise RuntimeError('A WAN operation is already running')
        atomic(ROOT/'request.json', body)
        atomic(JOB, {'status': 'queued', 'at': int(time.time()), 'message': 'Checking requested change'})
        run(['systemctl', 'start', '--no-block', 'router-wan-worker.service'])
    return {'ok': True, 'message': 'WAN operation started'}

def worker():
    body = read(ROOT/'request.json', {})
    done = None
    prior = read(JOB, {})  # wan-force-v1: shown again when an automatic request is dropped
    try:
        with locks():
            atomic(JOB, {'status': 'running', 'at': int(time.time()), 'message': 'Checking internet and preserving traffic mode'})
            cfg = settings()
            if body['action'] == 'awg_mtu':
                save_mtu(body.get('values', {}))
            elif body['action'] == 'select':
                done = switch(body['wan'], forced=cfg.get('force') is True)
                cfg['preferred'] = body['wan']; atomic(SETTINGS, cfg)
            elif body['action'] == 'auto':
                # Re-evaluate user intent after the daemon's asynchronous request.
                status = read(ROOT/'status.json', {})
                if (FAILED.exists() or not cfg['fallback'] or cfg.get('force') is True
                        or body.get('generation') != active().get('generation')):
                    if prior: atomic(JOB, prior)
                    return
                done = switch(body['wan'], body.get('reason', 'fallback'))
            elif body['action'] == 'force':
                cfg['force'] = body['enabled']
                if body['enabled']:
                    cfg['preferred'] = active()['wan']  # pin the WAN in use
                    FAILED.unlink(missing_ok=True)      # the user's choice ends a paused recovery
                atomic(SETTINGS, cfg)
                done = ('Force ON: '+NAMES[cfg['preferred']]+' stays selected; no internet checks, rollback '
                        'or automatic switching' if body['enabled'] else
                        'Force OFF: internet checks and automatic WAN rules apply again')
                event(done, ok=True)
            elif body['action'] == 'fallback':
                if body['enabled'] and FAILED.exists():
                    verify_mode(MODE.read_text().strip())
                    FAILED.unlink()
                cfg['fallback'] = body['enabled']; atomic(SETTINGS, cfg)
                event('Automatic WAN fallback '+('enabled' if cfg['fallback'] else 'disabled'), ok=True)
            elif body['action'] == 'bands':
                done = bands.apply(body.get('bands'))
            elif body['action'] == 'mobile':
                if active()['wan'] == 'lte': raise RuntimeError('Select Ethernet before changing APN')
                data = modem.info()
                new = modem.validate_mobile(body.get('mobile'), data)
                old = copy.deepcopy(cfg)
                modem.disconnect(data)
                cfg['mobile'] = new; atomic(SETTINGS, cfg)
                try:
                    mobile, bearer = modem.ensure()
                    net.prepare(net.ethernet(), bearer)
                    if not net.probe('lte', bearer)['ok']: raise RuntimeError('New APN did not provide working internet')
                except Exception:
                    atomic(SETTINGS, old)
                    try: modem.disconnect(); modem.ensure()
                    except Exception: pass
                    raise
                event('Mobile APN saved and internet verified', ok=True)
            else: raise ValueError('Unknown operation')
            if not isinstance(done, dict): done = {'status': 'complete', 'message': done or 'Complete; traffic mode preserved'}
            atomic(JOB, dict(done, at=int(time.time())))
    except Exception as exc:
        if read(JOB, {}).get('status') != 'recovery_failed':
            atomic(JOB, {'status': 'failed', 'at': int(time.time()), 'message': str(exc)[:500]})
            event(str(exc)[:500], ok=False)
        raise
    finally:
        (ROOT/'request.json').unlink(missing_ok=True)

def recover(force=False):
    tx = read(TX)
    if not tx or (not force and time.time() < tx['deadline']): return
    run(['systemctl', 'stop', 'router-wan-worker.service'], timeout=15, check=False)
    with locks():
        tx = read(TX)
        if tx:
            # If a rollback itself was killed, allow one final bounded attempt.
            if tx.get('independent_attempted') and tx.get('kind') == 'bands':
                bands.interrupted(tx)
                return
            if tx.get('independent_attempted'):
                failed_recovery(tx, RuntimeError('Independent recovery was interrupted'))
                return
            tx['independent_attempted'] = True
            atomic(TX, tx)
            rollback(tx)
            atomic(JOB, {'status': 'rolled_back', 'at': int(time.time()), 'message': 'Independent recovery restored the previous 4G bands' if tx.get('kind') == 'bands' else 'Independent recovery restored and verified the previous WAN'})

def boot():
    # Recover an interrupted transaction before restoring traffic engines.
    if TX.exists() and read(TX, {}).get('kind') == 'bands':
        bands.after_boot()  # router-wan-recover puts the previous bands back once the modem is up
    elif TX.exists():
        tx = read(TX)
        for path, data in tx['files'].items(): atomic(path, bytes.fromhex(data))
        if tx.get('awg'): awg.restore(tx['awg'])
        atomic(ACTIVE, tx['before']); atomic(SETTINGS, tx['settings']); TX.unlink()
    cfg = settings()
    target = active()['wan']
    mobile = None
    if target == 'lte':
        try: mobile = modem.ensure()[1]
        except Exception:
            if cfg['fallback'] and cfg.get('force') is not True: target = 'ethernet'  # wan-force-v1: a forced 4G waits for its bearer
    eth = net.ethernet()
    net.prepare(eth, mobile)
    net.firewall()
    chosen = eth if target == 'ethernet' else mobile
    interface = ETH if target == 'ethernet' else (mobile or {}).get('interface', active_interface())
    for file, data in patch_bindings(interface).items(): atomic(file, data)
    net.select(target, chosen)

if __name__ == '__main__':
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else ''
    if action == 'worker': worker()
    elif action == 'recover': recover()
    elif action == 'boot': boot()
    elif action == 'interface': print(active_interface())
    elif action == 'adapt': net.refresh_connected(); bind_awg(); adapt_mtu(); net.firewall(); net.direct_nat()
    elif action == 'refresh-routes': net.refresh_connected()
    elif action in ('bind-xray', 'bind-mihomo', 'bind-sing-box'):
        for file, data in patch_bindings(active_interface(), action[5:]).items(): atomic(file, data)
    else: raise SystemExit('Unknown WAN command')
