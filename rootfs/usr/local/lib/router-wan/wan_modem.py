"""ModemManager owns QMI. Host applies the bearer IP settings; no AT/QMI port races."""
import hashlib
import json
import ipaddress
import re
import time
from pathlib import Path
from wan_common import ROOT, atomic, command_json, read, run, settings

PATH_RE = re.compile(r'^/org/freedesktop/ModemManager1/(Modem|SIM|Bearer|SMS)/[0-9]+$')
# SIM home operator codes observed on this router. Unknown SIMs require a manual APN.
# T-Mobile Russia APN: https://www.tbank.ru/mobile-operator/help/communication/sim-card/start-using/activate-sim/
APNS = {'25020': 'internet.rtk.ru', '25062': 'tt'}
_signal_setup_attempts = {}
# Radio quality for the panel. ModemManager's own percentage is not comparable between QMI and
# MBIM modems, and an MBIM modem may leave RSRP/RSRQ/SINR out of some polls.
SIGNAL_KEYS = ('rsrp', 'rsrq', 'snr')
SIGNAL_HOLD_SECONDS = 60
QUALITY_SMOOTHING = 0.25  # weight of a fresh sample; the daemon polls about every 10 s
_signal_cache = {}
_quality = {}

def mm(*args, timeout=20):
    return command_json(['mmcli', '-J', *args], timeout)

def path(value):
    if not isinstance(value, str) or not PATH_RE.fullmatch(value):
        raise RuntimeError('ModemManager returned an invalid object path')
    return value

def discover():
    found = mm('-L').get('modem-list', [])
    if not found: raise RuntimeError('4G modem is not connected')
    if len(found) != 1: raise RuntimeError('Multiple modems detected; select the intended modem before connecting')
    return path(found[0])

def text(value, limit=200):
    return '' if value in (None, '--') else str(value)[:limit]

def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def held_signal(obj, lte):
    """Fill RSRP/RSRQ/SINR missing from this poll from the last complete poll, if recent."""
    now = time.monotonic()
    if all(number(lte.get(key)) is not None for key in SIGNAL_KEYS):
        _signal_cache[obj] = (now, {key: lte[key] for key in SIGNAL_KEYS})
        return lte, None
    at, values = _signal_cache.get(obj, (None, {}))
    if at is None or now-at > SIGNAL_HOLD_SECONDS:
        return lte, None
    return dict(lte, **values), round(now-at)

def radio_quality(obj, lte, fresh):
    """0-100: RSRP -120..-70 dBm and SINR -5..+20 dB, half each, smoothed over about a minute."""
    rsrp, snr = number(lte.get('rsrp')), number(lte.get('snr'))
    if rsrp is None or snr is None:
        return None
    sample = 50*min(max((rsrp+120)/50, 0), 1) + 50*min(max((snr+5)/25, 0), 1)
    previous = _quality.get(obj)
    value = sample if previous is None else previous + QUALITY_SMOOTHING*(sample-previous) if fresh else previous
    _quality[obj] = value
    return round(value)

def info():
    obj = discover()
    data = mm('-m', obj)['modem']
    generic, radio = data.get('generic', {}), data.get('3gpp', {})
    sim_path = path(generic.get('sim'))
    sim = mm('-i', sim_path)['sim']['properties']
    identity = text(sim.get('iccid')) or text(sim.get('imsi'))
    if not identity: raise RuntimeError('SIM is not ready or is locked')
    sim_key = hashlib.sha256(identity.encode()).hexdigest()
    result = {'object': obj, 'sim_key': sim_key, 'model': text(generic.get('model')),
              'operator': text(radio.get('operator-name') or sim.get('operator-name')),
              'operator_code': text(sim.get('operator-code')), 'state': text(generic.get('state')),
              'registration': text(radio.get('registration-state')),
              'signal_percent': generic.get('signal-quality', {}).get('value'),
              'technology': generic.get('access-technologies', []),
              'bearers': generic.get('bearers') or []}
    try:
        signal = mm('-m', obj, '--signal-get', timeout=5).get('modem', {}).get('signal', {})
        result['signal'] = signal.get('lte', {})
        result['signal_poll_rate'] = signal.get('refresh', {}).get('rate')
    except Exception:
        result['signal'] = {}
    try:
        lte, age = held_signal(obj, result['signal'])
        quality = radio_quality(obj, lte, age is None)
        result['signal'] = lte
        if age is not None:
            result['signal_age'] = age
        if quality is not None:
            result['signal_percent_modem'] = result['signal_percent']
            result['signal_percent'] = quality
    except Exception:
        pass  # Telemetry only; never block the bearer check.
    return result

def ensure_signal_polling(data):
    """Restore runtime telemetry after a modem/MM restart without reconnecting it."""
    try:
        rate = float(data.get('signal_poll_rate'))
    except (TypeError, ValueError):
        return  # No supported signal interface or no successful status read yet.
    if rate != 0:
        return
    obj = data['object']
    now = time.monotonic()
    if now - _signal_setup_attempts.get(obj, float('-inf')) < 60:
        return
    _signal_setup_attempts[obj] = now
    try:
        run(['mmcli', '-m', obj, '--signal-setup=10'], timeout=5)
    except Exception as exc:
        # Missing telemetry must not take a working bearer out of service.
        data['signal_error'] = str(exc)[:200]

def apn_for(data, mobile):
    if mobile['auto_apn']:
        apn = APNS.get(data['operator_code'])
        if not apn: raise RuntimeError('APN not known for this SIM. Enter the operator APN in Mobile settings.')
        return {'apn': apn, 'username': '', 'password': ''}
    if mobile.get('sim_key') != data['sim_key']:
        raise RuntimeError('SIM changed. Save Mobile settings for the new SIM before connecting.')
    return mobile

def validate_mobile(body, data):
    if not isinstance(body, dict): raise ValueError('Mobile settings must be an object')
    old = settings()['mobile']
    if old.get('sim_key') != data['sim_key']: old = dict(old, password='')
    value = {'auto_apn': body.get('auto_apn', True), 'apn': str(body.get('apn', '')).strip(),
             'username': str(body.get('username', '')).strip(),
             'password': str(body.get('password') or ('' if body.get('clear_password') else old['password'])),
             'allow_roaming': body.get('allow_roaming', False), 'sim_key': data['sim_key']}
    if type(value['auto_apn']) is not bool or type(value['allow_roaming']) is not bool:
        raise ValueError('Invalid mobile setting')
    if value['apn'] and not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,98}[A-Za-z0-9])?', value['apn']):
        raise ValueError('APN may contain letters, digits, dots and hyphens')
    if not value['auto_apn'] and not value['apn']: raise ValueError('Enter an APN')
    for key in ('username', 'password'):
        if len(value[key]) > 128 or any(ch in value[key] for ch in ',\r\n\x00\'"'):
            raise ValueError('Unsupported character in mobile ' + key)
    apn_for(data, value)
    return value

def connected_bearer(data):
    for obj in data['bearers']:
        try:
            bearer = mm('-b', path(obj))['bearer']
            if bearer.get('status', {}).get('connected') == 'yes': return bearer
        except Exception: continue
    return None

def ensure():
    data = info()
    ensure_signal_polling(data)
    mobile = settings()['mobile']
    profile = apn_for(data, mobile)
    if data['registration'] == 'roaming' and not mobile['allow_roaming']:
        raise RuntimeError('SIM is roaming; allow roaming in Mobile settings to connect')
    bearer = connected_bearer(data)
    if bearer and bearer.get('properties', {}).get('apn') != profile['apn']:
        disconnect(data)
        bearer = None
    if not bearer:
        pairs = ['apn=' + profile['apn'], 'ip-type=ipv4',
                 'allow-roaming=' + ('yes' if mobile['allow_roaming'] else 'no')]
        if profile.get('username'):
            pairs += ['user=' + profile['username'], 'password=' + profile.get('password', '')]
        # Action commands return a plain success message even with -J on Ubuntu's mmcli.
        # Only inspection commands have a JSON result; success here is the process exit code.
        run(['mmcli', '-m', data['object'], '--simple-connect=' + ','.join(pairs)], timeout=45)
        data = info()
        bearer = connected_bearer(data)
        if not bearer: raise RuntimeError('No connected IPv4 bearer returned by the modem')
    cfg = bearer.get('ipv4-config', {})
    if cfg.get('method') != 'static': raise RuntimeError('This integration requires a static IPv4 QMI bearer')
    iface = bearer['status'].get('interface', '')
    if not re.fullmatch(r'wwan[0-9]+', iface): raise RuntimeError('Unexpected mobile data interface')
    address = str(ipaddress.IPv4Address(cfg['address']))
    prefix = int(cfg['prefix'])
    if not 1 <= prefix <= 32: raise RuntimeError('Invalid mobile prefix')
    gateway = str(ipaddress.IPv4Address(cfg['gateway']))
    mtu = int(cfg.get('mtu') or 1430)
    if not 576 <= mtu <= 1500: raise RuntimeError('Invalid mobile MTU')
    result = {'interface': iface, 'address': address, 'prefix': prefix, 'gateway': gateway,
              'mtu': mtu, 'dns': [str(ipaddress.IPv4Address(v)) for v in cfg.get('dns', [])],
              'apn': profile['apn'], 'sim_key': data['sim_key'], 'updated': int(time.time())}
    previous = read(ROOT/'bearer.json', {})
    if previous.get('address') and previous.get('address') != address:
        run(['ip', 'address', 'del', previous['address']+'/'+str(previous['prefix']),
             'dev', previous['interface']], check=False)
    # Idempotent, does not reset the bearer or interface.
    run(['ip', 'link', 'set', iface, 'up', 'mtu', str(mtu)])
    run(['ip', 'address', 'replace', address+'/'+str(prefix), 'dev', iface])
    run(['sysctl', '-qw', 'net.ipv4.conf.'+iface+'.rp_filter=2'])
    atomic(ROOT/'bearer.json', result)
    data.update(result)
    data.pop('sim_key', None); data.pop('bearers', None); data.pop('object', None)
    return data, result

def disconnect(data=None):
    data = data or info()
    run(['mmcli', '-m', data['object'], '--simple-disconnect'], timeout=30)

def sms_revision(message):
    # Object IDs can be reused after a modem/reboot. Bind deletion to shown content.
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    return hashlib.sha256((boot+json.dumps(message, sort_keys=True, ensure_ascii=False)).encode('utf-8')).hexdigest()


def sms():
    modem = discover()
    listing = mm('-m', modem, '--messaging-list-sms').get('modem.messaging.sms', [])
    result = []
    for item in listing[-30:]:
        obj = item.get('path') if isinstance(item, dict) else item
        # mmcli versions return either a path or a path with a state suffix.
        obj = re.match(r'/org/freedesktop/ModemManager1/SMS/[0-9]+', str(obj))
        if not obj: continue
        raw = mm('-s', path(obj.group()))['sms']
        content, props = raw.get('content', {}), raw.get('properties', {})
        state = text(props.get('state') or raw.get('state'))
        if state not in ('received', 'receiving'): continue
        message = {'id': obj.group().rsplit('/', 1)[-1], 'number': text(content.get('number')),
                       'text': text(content.get('text'), 16000), 'state': state,
                       'timestamp': text(props.get('timestamp'))}
        message['revision'] = sms_revision(message)
        result.append(message)
    return {'ok': True, 'messages': sorted(result, key=lambda v: v['timestamp'], reverse=True),
            'checked_at': int(time.time())}


def delete_sms(body):
    if not isinstance(body, dict) or body.get('confirm') is not True:
        raise ValueError('Confirm SMS deletion')
    sms_id = body.get('id')
    if not isinstance(sms_id, str) or not re.fullmatch(r'[0-9]{1,10}', sms_id):
        raise ValueError('Invalid SMS ID')
    modem = discover()
    message = next((m for m in sms()['messages'] if m['id'] == sms_id), None)
    if not message or message['revision'] != body.get('revision'):
        raise RuntimeError('SMS changed or is no longer available; refresh the list')
    run(['mmcli', '-m', modem, '--messaging-delete-sms=/org/freedesktop/ModemManager1/SMS/'+sms_id], timeout=20)
    return {'ok': True, 'message': 'SMS deleted', 'id': sms_id}
