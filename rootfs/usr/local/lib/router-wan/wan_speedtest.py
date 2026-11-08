"""Bounded, explicit LTE download test. Never selects a WAN or reconnects a modem."""
import copy
import ipaddress
import json
import os
import re
import select
import subprocess
import termios
import threading
import time
import tty
from pathlib import Path

from wan_common import ROOT, MODE, active, atomic, read
import wan_control
import wan_network as net

ENDPOINT = '/api/wan/speedtest'
STATE_FILE = ROOT / 'speedtest.json'
HOST = 'mirror.yandex.ru'
URL = 'https://' + HOST + '/ubuntu-releases/24.04/ubuntu-24.04.4-desktop-amd64.iso'
STREAMS, STREAM_BYTES, SECONDS = 3, 32 * 1024 * 1024, 20
_lock = threading.RLock()
_cancel = threading.Event()
_state = read(STATE_FILE, {'status': 'idle'})
if _state.get('status') == 'running':
    _state.update(status='failed', message='Previous test was interrupted by a panel restart.')


def status():
    with _lock:
        value = copy.deepcopy(_state)
    result = value.get('last_result')
    if result and result.get('radio_schema', 0) < 2:
        # Correct an old band label without inventing lost secondary-cell data.
        for sample in [result.get('radio', {}), *result.get('radio_samples', [])]:
            for cell in [sample.get('primary', {}), sample.get('secondary', {}), *sample.get('scells', [])]:
                if 'earfcn' in cell:
                    cell['band'] = band_name(cell.get('band', ''), str(cell['earfcn']))
            if sample.get('primary') and not (sample.get('scells') or sample.get('secondary')):
                sample['primary'].pop('state', None)
                result['radio_note'] = 'Older test: secondary-carrier details were not reliably recorded. Run a new test to refresh.'
    value.update(ok=True, duration_seconds=SECONDS, max_mib=STREAMS*STREAM_BYTES//1048576)
    value['elapsed_seconds'] = round(max(0, time.time()-value.get('started_at', time.time())), 1) if value['status'] == 'running' else 0
    return value


def publish(**values):
    with _lock:
        _state.update(values)
        atomic(STATE_FILE, _state)


def lte_config():
    cfg = net.usable_config(read(ROOT / 'bearer.json'))
    if not cfg or not re.fullmatch(r'wwan[0-9]+', cfg.get('interface', '')):
        raise RuntimeError('4G has no ready IPv4 connection. Connect the modem first.')
    ipaddress.IPv4Address(cfg['address'])
    return cfg


def run(args, timeout=5):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def parse_radio(raw):
    result, scells = {}, []
    headers = list(re.finditer(r'^\s*(Primary Cell|Secondary Cell(?:\s+\d+)?) Info\s*:?[ \t]*$', raw, re.MULTILINE))
    for index, header in enumerate(headers):
        end = headers[index+1].start() if index+1 < len(headers) else len(raw)
        block = raw[header.end():end]
        item = {}
        for field, name in [('LTE Band', 'band'), ('Physical Cell ID', 'pci'), ('RX Channel', 'earfcn'), ('DL Bandwidth', 'mhz'), ('State', 'state'), ('Cell index', 'index')]:
            match = re.search(r'^\s*' + re.escape(field) + r":\s*'([^']+)'", block, re.MULTILINE)
            if match:
                item[name] = match[1].replace('eutran-', 'B') if name == 'band' else match[1]
        if not item:
            continue
        # Some LM960 QMI responses label B40 as eutran-41. EARFCN is authoritative.
        item['band'] = band_name(item.get('band', ''), item.get('earfcn', ''))
        if header.group(1) == 'Primary Cell':
            result['primary'] = item
        else:
            scells.append(item)
    if scells:
        result['scells'] = scells
        result['secondary'] = {key: ' + '.join(str(c.get(key, '?')) for c in scells)
                               for key in ('band', 'mhz', 'pci', 'earfcn', 'state')}
    return result


# The MBIM Telit composition uses its otherwise unused AT interface 7 for CA info.
# The QMI composition has no interface 7: use QMI via the proxy, leaving its managed
# AT interfaces alone. Both paths return the same primary/scells structure.
TELIT_VENDOR, TELIT_AT_INTERFACE = '1bc7', '07'
BANDWIDTH_MHZ = {'0': '1.4', '1': '3', '2': '5', '3': '10', '4': '15', '5': '20'}
SCELL_STATE = {'0': 'deconfigured', '1': 'deactivated', '2': 'activated'}
# Downlink EARFCN ranges from 3GPP TS 36.101.
EARFCN_BANDS = ((0, 599, 1), (600, 1199, 2), (1200, 1949, 3), (1950, 2399, 4), (2400, 2649, 5),
                (2750, 3449, 7), (3450, 3799, 8), (5010, 5179, 12), (5180, 5279, 13), (5280, 5379, 14),
                (5730, 5849, 17), (5850, 5999, 18), (6000, 6149, 19), (6150, 6449, 20), (8040, 8689, 25),
                (8690, 9039, 26), (9210, 9659, 28), (9660, 9769, 29), (9770, 9869, 30), (9920, 10359, 32),
                (37750, 38249, 38), (38250, 38649, 39), (38650, 39649, 40), (39650, 41589, 41),
                (41590, 43589, 42), (43590, 45589, 43), (46790, 54539, 46), (55240, 56739, 48),
                (66436, 67335, 66), (68586, 68935, 71))


def band_name(code, earfcn):
    channel = int(earfcn) if earfcn.isdigit() else -1
    for low, high, band in EARFCN_BANDS:
        if low <= channel <= high:
            return 'B%d' % band
    # Qualcomm band class: LTE band N is 119+N for bands 1-43.
    return 'B%d' % (int(code)-119) if code.isdigit() and 120 <= int(code) <= 162 else code


def parse_cainfo(raw):
    line = next((x.strip() for x in raw.splitlines() if x.strip().startswith('#CAINFO:')), '')
    f = [x.strip() for x in line.split(':', 1)[1].split(',')] if line else []
    if len(f) < 12 or f[0] in ('', '0'):
        return {}

    def cell(b):
        # Qualcomm SINR index, dB = index/5-20; 0 means no measurement (inactive SCells report 0).
        sinr = round(int(b[7])/5-20, 1) if b[7].isdigit() and b[7] != '0' else None
        return {'band': band_name(b[0], b[1]), 'earfcn': b[1], 'mhz': BANDWIDTH_MHZ.get(b[2], b[2]),
                'pci': b[3], 'rsrp': b[4], 'rsrq': b[6], 'sinr': sinr}
    # The PCell takes 12 fields, then each SCell slot 11; an unused slot is all zeros.
    result = {'primary': cell(f[:12])}
    scells = [dict(cell(f[i:i+11]), state=SCELL_STATE.get(f[i+8], f[i+8]))
              for i in range(12, len(f)-10, 11) if f[i] not in ('', '0') and f[i+1] not in ('', '0')]
    if scells:
        result['scells'] = scells
        # The panel prints only 'primary' and 'secondary', so 'secondary' lists every SCell.
        result['secondary'] = {key: ' + '.join(str(c.get(key) or '?') for c in scells)
                               for key in ('band', 'mhz', 'pci', 'state')}
    return result


def telit_at_port(device):
    """AT tty on USB interface 7 of a Telit modem; device = the data interface's sysfs node."""
    usb = device.resolve().parent
    if (usb / 'idVendor').read_text().strip() != TELIT_VENDOR:
        return None
    for interface in usb.glob(usb.name + ':*'):
        if (interface / 'bInterfaceNumber').read_text().strip() == TELIT_AT_INTERFACE:
            ttys = sorted(interface.glob('ttyUSB*'))
            return '/dev/'+ttys[0].name if ttys else None
    return None


def at_query(port, command, timeout=2.0):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        os.write(fd, command.encode() + b'\r')
        data, deadline = b'', time.monotonic()+timeout
        while not re.search(rb'\r\n(OK|ERROR|\+CME ERROR[^\r]*)\r\n', data):
            left = deadline-time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            data += chunk
        return data.decode('ascii', 'replace')
    finally:
        os.close(fd)


def radio():
    try:
        device = Path('/sys/class/net') / lte_config()['interface'] / 'device'
        port = telit_at_port(device)
        if port:
            return parse_cainfo(at_query(port, 'AT#CAINFO?'))
        ports = sorted((device / 'usbmisc').glob('cdc-wdm*'))
        if not ports:
            return {}
        mbim = ['--device-open-mbim'] if (device / 'driver').resolve().name == 'cdc_mbim' else []
        p = run(['qmicli', '-d', '/dev/'+ports[0].name, '--device-open-proxy', *mbim, '--nas-get-lte-cphy-ca-info'], 3)
        return parse_radio(p.stdout) if p.returncode == 0 else {}
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, termios.error):
        return {}


def curl_args(cfg, address, stream):
    return ['curl', '-q', '-4', '-sS', '--fail', '--noproxy', '*',
            '--interface', 'if!'+cfg['interface'], '--resolve', HOST+':443:'+address,
            '--proto', '=https', '--connect-timeout', '5', '--max-time', str(SECONDS),
            '--range', str(stream*STREAM_BYTES)+'-'+str((stream+1)*STREAM_BYTES-1),
            '--max-filesize', str(STREAM_BYTES), '--output', '/dev/null',
            '--write-out', '%{json}', URL]


def validate_stream(code, data, cfg):
    # Exit 28 is expected at the intentional time limit, but only after a valid
    # partial-content response with payload from the verified LTE source address.
    if code not in (0, 28) or data.get('http_code') != 206:
        raise RuntimeError('The download server did not return usable partial content through 4G.')
    if data.get('local_ip') != cfg['address']:
        raise RuntimeError('The download did not use the expected 4G address; result discarded.')
    if not 0 < data.get('size_download', 0) <= STREAM_BYTES:
        raise RuntimeError('No valid download data was received through 4G.')
    return {k: data.get(k) for k in ('size_download', 'time_total', 'local_ip', 'remote_ip')}


def terminate(processes):
    for p in processes:
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)


def worker():
    processes = []
    try:
        cfg = lte_config()
        snapshot = (active(), MODE.read_text())
        publish(message='Resolving the download server through 4G…', phase='preparing')
        addresses = net.resolve('lte', cfg, HOST)
        address = next((str(ipaddress.IPv4Address(a)) for a in addresses if ipaddress.IPv4Address(a).is_global), None)
        if not address:
            raise RuntimeError('The 4G DNS lookup returned no usable public IPv4 address.')
        route = json.loads(run(['ip', '-j', '-4', 'route', 'get', address, 'from', cfg['address'], 'oif', cfg['interface']]).stdout)
        if not route or route[0].get('dev') != cfg['interface'] or route[0].get('type') in ('unreachable', 'blackhole', 'prohibit'):
            raise RuntimeError('No direct route through the modem. Ethernet fallback is disabled for this test.')
        if _cancel.is_set():
            raise InterruptedError('Test cancelled.')
        started = time.monotonic()
        publish(message='Measuring 4G download…', phase='download', download_started_at=time.time())
        for i in range(STREAMS):
            processes.append(subprocess.Popen(curl_args(cfg, address, i), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True))
        samples, next_radio = [], started + 3
        while any(p.poll() is None for p in processes):
            if _cancel.is_set():
                raise InterruptedError('Test cancelled.')
            if (active(), MODE.read_text()) != snapshot:
                raise InterruptedError('WAN or traffic mode changed; test stopped.')
            current = lte_config()
            if (current['interface'], current['address']) != (cfg['interface'], cfg['address']):
                raise InterruptedError('The mobile connection changed; test stopped.')
            now = time.monotonic()
            if now-started > SECONDS+7:
                raise RuntimeError('The download test exceeded its time limit.')
            if now >= next_radio:
                sample = radio()
                if sample:
                    samples.append(sample)
                next_radio = time.monotonic()+6
            time.sleep(0.2)
        wall_seconds = time.monotonic()-started
        if _cancel.is_set() or (active(), MODE.read_text()) != snapshot:
            raise InterruptedError('Test stopped; no new result saved.')
        results, failures = [], []
        for p in processes:
            out, _ = p.communicate(timeout=2)
            try:
                results.append(validate_stream(p.returncode, json.loads(out), cfg))
            except (ValueError, TypeError, RuntimeError) as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError(failures[0])
        # Use the longest transfer interval; the three processes start together.
        seconds = max(float(x['time_total']) for x in results)
        if seconds <= 0:
            raise RuntimeError('The measured transfer interval was invalid.')
        count = sum(x['size_download'] for x in results)
        result = {'download_mbps': round(count*8/seconds/1000000, 2), 'mib': round(count/1048576, 1),
                  'seconds': round(seconds, 1), 'wall_seconds': round(wall_seconds, 1), 'at': int(time.time()),
                  'interface': cfg['interface'], 'local_ip': cfg['address'], 'server': HOST,
                  'radio': samples[-1] if samples else {}, 'radio_samples': samples, 'radio_schema': 2,
                  'streams': STREAMS, 'production_unchanged': True}
        publish(status='complete', phase='complete', message='4G download test complete.', last_result=result, finished_at=time.time())
    except InterruptedError as exc:
        publish(status='cancelled', phase='complete', message=str(exc), finished_at=time.time())
    except Exception as exc:
        publish(status='failed', phase='complete', message=str(exc)[:300], finished_at=time.time())
    finally:
        terminate(processes)


def request(body):
    if not isinstance(body, dict) or set(body) != {'action'} or body['action'] not in ('start', 'cancel'):
        raise ValueError('Expected action start or cancel.')
    with _lock:
        if body['action'] == 'cancel':
            if _state.get('status') == 'running':
                _cancel.set()
                _state['message'] = 'Stopping the test…'
            return status()
        if _state.get('status') == 'running':
            raise RuntimeError('A 4G speed test is already running.')
        if wan_control.busy():
            raise RuntimeError('Wait for the current WAN operation to finish.')
        lte_config()
        _cancel.clear()
        publish(status='running', phase='preparing', message='Starting 4G download test…', started_at=time.time())
        try:
            threading.Thread(target=worker, name='lte-speedtest', daemon=True).start()
        except Exception:
            publish(status='failed', message='Could not start the test worker.')
            raise
        return status()


def get(h):
    if h.path != ENDPOINT:
        return False
    h.j(status() if h.allowed() else {'ok': False, 'error': 'LAN access only'}, 200 if h.allowed() else 403)
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
        h.j(request(json.loads(h.rfile.read(size))), 202)
    except (ValueError, TypeError, RuntimeError) as exc:
        h.j({'ok': False, 'error': str(exc)[:300]}, 409)
    return True
