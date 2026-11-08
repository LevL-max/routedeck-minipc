"""4G modem frequency bands for the Mobile network card (lte-bands-v1).

ModemManager reports the bands the installed modem supports and the ones it currently uses,
so every mini-PC lists its own modem's bands. The modem stores the selection itself, so it
survives reboots. A change runs in router-wan-worker on the standby modem only (Ethernet
active): disconnect, set, wait for registration, reconnect, verify 4G internet; any failure
restores the previous bands. router-wan-recover finishes a change interrupted by a hung
worker or a reboot.
"""
import hashlib
import re
import time

from wan_common import MODE, ROOT, active, atomic, event, read, run, settings
import wan_modem as modem
import wan_network as net

TX = ROOT/'transaction.json'
JOB = ROOT/'job.json'
ORIGINAL = ROOT/'bands-original.json'
BAND_RE = re.compile(r'(utran|eutran)-([0-9]{1,3})')
GROUPS = (('3g', '3G UMTS'), ('lte-fdd', '4G LTE-FDD'), ('lte-tdd', '4G LTE-TDD'), ('other', 'Other'))
GROUP_ORDER = [group for group, _ in GROUPS]
# Downlink frequency in MHz, shown under each band (3GPP TS 36.101 / 25.101).
LTE_MHZ = {1: '2100', 2: '1900', 3: '1800', 4: '1700/2100', 5: '850', 7: '2600', 8: '900', 11: '1500',
           12: '700', 13: '700', 14: '700', 17: '700', 18: '800', 19: '800', 20: '800', 21: '1500',
           25: '1900', 26: '850', 28: '700', 29: '700', 30: '2300', 31: '450', 32: '1500', 34: '2000',
           38: '2600', 39: '1900', 40: '2300', 41: '2500', 42: '3500', 43: '3700', 46: '5200', 48: '3600',
           66: '1700/2100', 71: '600', 72: '450'}
UMTS_MHZ = {1: '2100', 2: '1900', 3: '1800', 4: '1700/2100', 5: '850', 6: '800', 8: '900', 9: '1700', 19: '800'}
LTE_ACTIVE = 'Select Ethernet before changing 4G bands; the active 4G connection is kept'
SETTLE_SECONDS = 8       # the modem leaves its old cell before registration is polled
REGISTER_SECONDS = 90
DEADLINE_SECONDS = 420   # then router-wan-recover takes over (hung or killed worker)
_cache = {'at': 0.0, 'value': None}


def describe(name):
    match = BAND_RE.fullmatch(name)
    if not match:
        return {'id': name, 'label': name.upper(), 'group': 'other', 'mhz': '', 'n': 0}
    n = int(match[2])
    if match[1] == 'utran':
        return {'id': name, 'label': str(n), 'group': '3g', 'mhz': UMTS_MHZ.get(n, ''), 'n': n}
    return {'id': name, 'label': 'B%d' % n, 'group': 'lte-tdd' if 33 <= n <= 53 else 'lte-fdd',
            'mhz': LTE_MHZ.get(n, ''), 'n': n}


def order(name):
    item = describe(name)
    return GROUP_ORDER.index(item['group']), item['n'], name


def short(name):
    item = describe(name)
    return '3G ' + item['label'] if item['group'] == '3g' else item['label']


def changes(before, after):
    """'B40 off; B7 on' style summary; empty when the sets are equal."""
    parts = []
    for names, verb in ((set(before)-set(after), 'off'), (set(after)-set(before), 'on')):
        if names:
            labels = [short(b) for b in sorted(names, key=order)]
            parts.append((', '.join(labels) if len(labels) <= 6 else '%d bands' % len(labels)) + ' ' + verb)
    return '; '.join(parts)


def modem_bands(obj=None):
    """(object path, mmcli modem JSON, supported bands, current bands) from ModemManager."""
    obj = obj or modem.discover()
    data = modem.mm('-m', obj)['modem']
    generic = data.get('generic', {})
    supported = sorted({b for b in generic.get('supported-bands') or []
                        if isinstance(b, str) and b not in ('any', 'unknown')}, key=order)
    current = [b for b in generic.get('current-bands') or [] if isinstance(b, str)]
    current = list(supported) if 'any' in current else sorted(set(current), key=order)
    return obj, data, supported, current


def validate(requested, supported):
    if not isinstance(requested, list) or not 0 < len(requested) <= 128:
        raise ValueError('Select the bands to keep on')
    chosen = set()
    for band in requested:
        if not isinstance(band, str) or band not in supported:
            raise ValueError('This modem does not support band ' + str(band)[:24])
        chosen.add(band)
    if not any(b.startswith('eutran-') for b in chosen):
        raise ValueError('Keep at least one 4G (LTE) band on')
    return sorted(chosen, key=order)


def modem_key(data):
    identity = str(data.get('generic', {}).get('equipment-identifier') or '')
    return hashlib.sha256(identity.encode()).hexdigest()[:16] if identity else ''


def remember_original(data, current):
    """The bands a modem had before its first change from the panel: the 'Original set' button."""
    key, saved = modem_key(data), read(ORIGINAL, {})
    if key and key not in saved:
        saved[key] = {'model': modem.text(data.get('generic', {}).get('model')), 'bands': current,
                      'saved_at': int(time.time())}
        atomic(ORIGINAL, saved)


def serving():
    """Cells in use now (primary and secondary carriers) while 4G is connected."""
    try:
        import wan_speedtest  # lazily: it imports wan_control, which imports this module
        return wan_speedtest.radio() or {}
    except Exception:
        return {}


def status():
    if _cache['value'] is not None and time.monotonic()-_cache['at'] < 3:
        return _cache['value']
    import wan_control  # lazily, see serving()
    _, data, supported, current = modem_bands()
    generic, cells = data.get('generic', {}), serving()
    in_use = []
    for cell in [cells.get('primary') or {}, *(cells.get('scells') or [])]:
        match = re.fullmatch(r'B([0-9]+)', str(cell.get('band', '')))
        if match and 'eutran-'+match[1] not in in_use:
            in_use.append('eutran-'+match[1])
    original = [b for b in read(ORIGINAL, {}).get(modem_key(data), {}).get('bands') or [] if b in supported]
    busy, wan = wan_control.busy(), active().get('wan')
    reason = LTE_ACTIVE if wan == 'lte' else 'Another WAN operation is running' if busy else ''
    value = {'ok': True, 'model': modem.text(generic.get('model')), 'revision': modem.text(generic.get('revision')),
             'groups': [{'id': group, 'title': title} for group, title in GROUPS],
             'bands': [dict(describe(b), enabled=b in current) for b in supported],
             'in_use': in_use, 'serving': cells,
             'original': original if original and set(original) != set(current) else None,
             'registration': data.get('3gpp', {}).get('registration-state'),
             'active_wan': wan, 'busy': busy, 'editable': not reason, 'reason': reason,
             'job': read(JOB, {}), 'checked_at': int(time.time())}
    _cache.update(at=time.monotonic(), value=value)
    return value


def check(body):
    """Quick checks in the panel request, before the worker starts."""
    if active().get('wan') == 'lte':
        raise RuntimeError(LTE_ACTIVE)
    validate(body.get('bands'), modem_bands()[2])


def progress(message):
    atomic(JOB, {'status': 'running', 'at': int(time.time()), 'message': message})


def set_current(obj, bands):
    p = run(['mmcli', '-m', obj, '--set-current-bands=' + '|'.join(bands)], timeout=90, check=False)
    if p.returncode:
        detail = (p.stderr or p.stdout or '').strip().splitlines()
        raise RuntimeError('the modem rejected the band list' + (': ' + detail[-1][-200:] if detail else ''))


def wait_modem(seconds=REGISTER_SECONDS):
    deadline = time.monotonic()+seconds
    while True:
        try:
            return modem.discover()
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def wait_registered(seconds=REGISTER_SECONDS):
    deadline = time.monotonic()+seconds
    while True:
        try:
            data = modem.mm('-m', modem.discover())['modem']
            if (data.get('3gpp', {}).get('registration-state') in ('home', 'roaming') and
                    data.get('generic', {}).get('state') in ('registered', 'connecting', 'connected')):
                return
        except Exception:
            pass  # ModemManager may drop the modem object for a moment while it re-registers
        if time.monotonic() >= deadline:
            raise RuntimeError('no network found on the selected bands within %d s' % seconds)
        time.sleep(3)


def reconnect():
    """Registration, the 4G bearer and its own internet check (DNS and HTTPS over 4G only)."""
    wait_registered()
    try:
        bearer = modem.ensure()[1]
    except Exception as exc:
        raise RuntimeError('4G did not connect on the selected bands (' + str(exc)[:120] + ')') from exc
    net.prepare(net.ethernet(), bearer)
    result = net.probe('lte', bearer)
    if not result.get('ok'):
        raise RuntimeError('4G connected, but its internet check failed: ' + str(result.get('error') or 'no answer')[:150])


def bands_in_use():
    cells = serving()
    names = [str(c.get('band', '')) for c in [cells.get('primary') or {}, *(cells.get('scells') or [])]]
    return ' + '.join(dict.fromkeys(n for n in names if n))


def apply(requested):
    """Worker action. Returns the job message; raises after restoring the previous bands."""
    if active().get('wan') == 'lte':
        raise RuntimeError(LTE_ACTIVE)
    obj, data, supported, before = modem_bands()
    wanted = validate(requested, supported)
    change = changes(before, wanted)
    if not change:
        event('4G bands unchanged', ok=True)
        return '4G bands unchanged'
    remember_original(data, before)
    tx = {'kind': 'bands', 'before': active(), 'mode': MODE.read_text().strip(), 'settings': settings(),
          'files': {}, 'bands_before': before, 'bands_after': wanted, 'deadline': time.time()+DEADLINE_SECONDS}
    atomic(TX, tx)
    try:
        progress('Bands: disconnecting the standby 4G modem')
        try:
            modem.disconnect()
        except Exception:
            pass  # not connected; the band change detaches the modem anyway
        progress('Bands: ' + change + ' · writing to the modem')
        set_current(obj, wanted)
        progress('Bands: ' + change + ' · waiting for network registration and 4G internet')
        time.sleep(SETTLE_SECONDS)
        reconnect()
    except Exception as exc:
        rollback(tx)
        raise RuntimeError('Bands not applied: ' + str(exc)[:200] + '. Previous bands restored') from exc
    TX.unlink(missing_ok=True)
    now_on = bands_in_use()
    message = '4G bands applied: ' + change + '; 4G internet verified' + (', now on ' + now_on if now_on else '')
    event(message, ok=True)
    return message


def rollback(tx):
    """Put the previous bands back. Never pauses WAN fallback: Ethernet carried the traffic."""
    tx.update(phase='rollback', deadline=time.time()+DEADLINE_SECONDS)
    atomic(TX, tx)
    progress('Bands: restoring the previous bands')
    try:
        set_current(wait_modem(), tx['bands_before'])
    except Exception as exc:
        TX.unlink(missing_ok=True)
        message = 'Previous 4G bands could not be restored (' + str(exc)[:200] + '); set them again in Frequency bands'
        event(message, ok=False)
        raise RuntimeError(message) from exc
    try:
        time.sleep(SETTLE_SECONDS)
        reconnect()
        verified = True
    except Exception:
        verified = False  # the router-wan daemon keeps reconnecting 4G by itself
    TX.unlink(missing_ok=True)
    event('Previous 4G bands restored' + ('; 4G internet verified' if verified else '; 4G reconnect pending'),
          ok=False, recovered=True)


def interrupted(tx):
    """router-wan-recover found its own earlier attempt killed: stop retrying, tell the user."""
    TX.unlink(missing_ok=True)
    message = 'A 4G band change was interrupted; check Frequency bands in Mobile network'
    atomic(JOB, {'status': 'failed', 'at': int(time.time()), 'message': message})
    event(message, ok=False)


def after_boot():
    """A reboot interrupted a band change. router-wan-recover restores the previous bands as soon
    as ModemManager has the modem; the daemon waits while the transaction exists."""
    tx = read(TX, {})
    tx['deadline'] = 0
    atomic(TX, tx)
