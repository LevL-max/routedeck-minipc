"""Whitelist watch for both WANs (lte-whitelist-v1).

During a mobile whitelist period the operator lets only approved (mostly Russian) services
through: Russian sites open, foreign TLS is reset right after the TCP connect. A data session
opened in that period stays filtered after the period ends, until the bearer reconnects.
Ethernet can meet the same thing upstream: a router or an external modem on a mobile link.

Every few minutes the router-wan monitor checks several Russian and several foreign sites over
each WAN separately (that WAN's own DNS, sockets bound to its interface and address). A sample
looks like a whitelist when at least 3 of 4 Russian sites answer and at most 1 of 5 foreign ones
does; it looks normal when 3 or more foreign sites answer; anything else (outage, partial) counts
for neither. Three whitelist-like samples raise the warning, two normal ones clear it.
While the 4G warning stands the modem re-registers at most once per hour: disconnect and detach
(disable), as a band change does; the monitor's next simple-connect enables, registers and connects
it again, as after any drop. A plain disconnect is not enough: the operator keeps the network side
of the session (same address, same filtering). Ethernet only warns: its mobile session, if any,
lives in another device.
"""
import concurrent.futures
import socket
import ssl
import threading
import time
from time import gmtime, strftime

from wan_common import ROOT, active, atomic, event, locks, read, run
import wan_control as control
import wan_modem as modem
import wan_network as net

MARK = 'lte-whitelist-v1'
STATE = ROOT/'whitelist.json'
SPEEDTEST = ROOT/'speedtest.json'
WANS = ('ethernet', 'lte')
RU_TARGETS = (('ya.ru', '/robots.txt'), ('vk.com', '/robots.txt'), ('mail.ru', '/robots.txt'),
              ('www.gosuslugi.ru', '/robots.txt'))
FOREIGN_TARGETS = (('www.gstatic.com', '/generate_204'), ('github.com', '/robots.txt'),
                   ('www.wikipedia.org', '/robots.txt'), ('www.apple.com', '/robots.txt'),
                   ('www.cloudflare.com', '/cdn-cgi/trace'))
RU_NEEDED = 3            # Russian sites that must answer before blocked foreign ones mean anything
FOREIGN_WHITELIST = 1    # at most this many foreign sites answer in a whitelist-like sample
FOREIGN_NORMAL = 3       # at least this many foreign sites answer in a normal sample
RAISE_AFTER = 3          # whitelist-like samples in a row that raise the warning
CLEAR_AFTER = 2          # normal samples in a row that clear it
INTERVAL = 300           # seconds between samples
CONFIRM = 60             # sooner while a verdict is building up
AFTER_RECONNECT = 90     # first sample after a reconnect
FIRST = 60               # first sample after the monitor starts
RECONNECT_GAP = 3600     # at most one automatic 4G reconnect per hour
TIMEOUT = 5
HISTORY = 10


def site(wan, cfg, host, urlpath):
    """One HTTPS request with a verified certificate over this WAN only. Returns (reachable, stage)."""
    try:
        address = net.resolve(wan, cfg, host, set())[0]
    except (OSError, ValueError, IndexError):
        return False, 'dns'
    try:
        raw = net.bound_socket(wan, cfg, timeout=TIMEOUT)
    except OSError:
        return False, 'socket'
    with raw:
        try:
            raw.connect((address, 443))
        except socket.timeout:
            return False, 'tcp timeout'
        except OSError:
            return False, 'tcp refused'
        try:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as sock:
                sock.sendall(('GET ' + urlpath + ' HTTP/1.1\r\nHost: ' + host +
                              '\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n').encode())
                header = sock.recv(1024).split(b'\r\n', 1)[0].split()
        except socket.timeout:
            return False, 'tls timeout'
        except ssl.SSLCertVerificationError:
            return False, 'tls certificate'
        except ConnectionResetError:
            return False, 'tls reset'
        except (OSError, ssl.SSLError):
            return False, 'tls failed'
    try:
        code = int(header[1])
    except (IndexError, ValueError):
        return False, 'http garbled'
    # Any answer inside a verified TLS session means the site is reachable; a 403 is still an answer.
    return True, 'ok' if 200 <= code < 400 else 'http %d' % code


def sample(wan, cfg):
    """All sites at once; about 2*TIMEOUT seconds at worst plus DNS."""
    targets = [('ru',) + t for t in RU_TARGETS] + [('foreign',) + t for t in FOREIGN_TARGETS]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [(group, host, pool.submit(site, wan, cfg, host, urlpath)) for group, host, urlpath in targets]
        results = []
        for group, host, future in futures:
            try:
                ok, stage = future.result(timeout=40)
            except Exception as exc:
                ok, stage = False, type(exc).__name__
            results.append({'group': group, 'host': host, 'ok': ok, 'stage': stage})
    ru = sum(r['ok'] for r in results if r['group'] == 'ru')
    foreign = sum(r['ok'] for r in results if r['group'] == 'foreign')
    return {'at': int(time.time()), 'ru_ok': ru, 'ru_total': len(RU_TARGETS), 'foreign_ok': foreign,
            'foreign_total': len(FOREIGN_TARGETS), 'verdict': verdict(ru, foreign), 'sites': results}


def verdict(ru_ok, foreign_ok):
    if ru_ok >= RU_NEEDED and foreign_ok <= FOREIGN_WHITELIST:
        return 'whitelist'
    if foreign_ok >= FOREIGN_NORMAL:
        return 'normal'
    return 'unclear'


class Tracker:
    """Pure state machine for one WAN; the clock and the samples come from outside (unit tests)."""

    def __init__(self, saved, now):
        self.state = 'unknown'
        self.bad = self.good = 0
        self.since = None
        self.last = None
        self.next_check = now + FIRST
        self.pending_result = False
        self.last_reconnect = saved.get('last_reconnect')
        self.history = list(saved.get('history') or [])[-HISTORY:]

    def due(self, now):
        return now >= self.next_check

    def record(self, now, wall, result):
        self.last = result
        kind = result['verdict']
        if kind == 'whitelist':
            self.bad, self.good = self.bad + 1, 0
            if self.state != 'suspected' and self.bad >= RAISE_AFTER:
                self.state, self.since = 'suspected', int(wall)
        elif kind == 'normal':
            self.good, self.bad = self.good + 1, 0
            if self.state != 'suspected' or self.good >= CLEAR_AFTER:
                self.state, self.since = 'normal', None
        if self.pending_result and kind != 'unclear':
            self.pending_result = False
            if self.history:
                self.history[-1]['result'] = 'helped' if kind == 'normal' else 'did not help'
        building = (self.state != 'suspected' and self.bad) or (self.state == 'suspected' and self.good)
        self.next_check = now + (CONFIRM if building else INTERVAL)

    def reconnect_due(self, wall):
        if self.state != 'suspected':
            return False
        if self.last_reconnect is None:
            return True
        elapsed = wall - self.last_reconnect
        return elapsed >= RECONNECT_GAP or elapsed < -RECONNECT_GAP  # a clock set far back allows it

    def reconnected(self, now, wall, active_wan):
        self.last_reconnect = int(wall)
        self.history = (self.history + [{'at': int(wall), 'active_wan': active_wan,
                                         'ru': self.ratio('ru'), 'foreign': self.ratio('foreign')}])[-HISTORY:]
        self.pending_result = True
        self.bad = 0
        self.next_check = now + AFTER_RECONNECT

    def ratio(self, group):
        if not self.last:
            return ''
        return '%d/%d' % (self.last[group + '_ok'], self.last[group + '_total'])

    def status(self, reconnects):
        last = self.history[-1] if self.history else {}
        result = {'state': self.state, 'since': self.since, 'ru': self.ratio('ru'), 'foreign': self.ratio('foreign'),
                  'checked_at': (self.last or {}).get('at'), 'samples': {'whitelist': self.bad, 'normal': self.good},
                  'sites': [r['host'] + ' ' + r['stage'] for r in (self.last or {}).get('sites', [])],
                  'auto_reconnect': reconnects}
        if reconnects:
            result.update(last_reconnect=self.last_reconnect, last_result=last.get('result'))
            if self.state == 'suspected':
                result['next_reconnect'] = (None if self.last_reconnect is None
                                            else int(self.last_reconnect + RECONNECT_GAP))
        return result


_trackers = {}
_workers = {}
_done = {}
_lock = threading.Lock()


def _run(wan, cfg):
    try:
        result = sample(wan, cfg)
    except Exception as exc:
        result = {'error': str(exc)[:200]}
    with _lock:
        _done[wan] = result


def busy_elsewhere():
    """A 4G speed test or a WAN transaction must not lose its bearer under it."""
    test = read(SPEEDTEST, {})
    if test.get('status') == 'running' and time.time() - test.get('started_at', 0) < 900:
        return True
    return control.busy()


def save():
    tracker = _trackers['lte']
    atomic(STATE, {'lte': {'last_reconnect': tracker.last_reconnect, 'history': tracker.history}})


def reconnect(tracker, now, wall):
    wan = active().get('wan')
    detach = 'detached'
    with locks():
        if busy_elsewhere():
            return
        obj = modem.discover()
        modem.disconnect()
        try:
            run(['mmcli', '-m', obj, '--disable'], timeout=60)
        except Exception as exc:
            detach = 'detach failed: ' + str(exc)[:100]  # disconnected anyway; the monitor reconnects
    tracker.reconnected(now, wall, wan)
    tracker.history[-1]['detach'] = detach
    save()
    message = ('4G modem re-registered: possible mobile whitelist (Russian sites %s, foreign %s); '
               'new data session' % (tracker.ratio('ru'), tracker.ratio('foreign')))
    event(message, ok=True)
    print('whitelist: ' + message + ' · ' + detach + (' · 4G was the active WAN' if wan == 'lte' else ''), flush=True)


def step(wan, cfg, now, wall):
    if wan not in _trackers:
        _trackers[wan] = Tracker(read(STATE, {}).get(wan, {}) if wan == 'lte' else {}, now)
    tracker = _trackers[wan]
    with _lock:
        result = _done.pop(wan, None)
    if result and 'verdict' in result:
        before = tracker.state
        tracker.record(now, wall, result)
        if tracker.state != before and 'unknown' not in (before, tracker.state):
            print('whitelist: %s %s -> %s (Russian %s, foreign %s) %s' % (
                wan, before, tracker.state, tracker.ratio('ru'), tracker.ratio('foreign'),
                ', '.join(r['host'] + ' ' + r['stage'] for r in result['sites'] if not r['ok'])), flush=True)
            if before == 'suspected' and tracker.state == 'normal':
                # v1.1: the panel's WAN card shows this until the next action; PCs run UTC, the user reads MSK
                event('%s: whitelist lifted %s MSK (Russian sites %s, foreign %s)' % (
                    '4G' if wan == 'lte' else 'Ethernet', strftime('%d.%m %H:%M', gmtime(int(wall) + 3 * 3600)),
                    tracker.ratio('ru'), tracker.ratio('foreign')), ok=True)
    elif result:
        tracker.next_check = now + INTERVAL
    running = wan in _workers and _workers[wan].is_alive()
    usable = bool(cfg and cfg.get('interface') and cfg.get('address'))
    if wan == 'lte' and tracker.reconnect_due(wall) and not running and usable and not busy_elsewhere():
        try:
            reconnect(tracker, now, wall)
        except BlockingIOError:
            pass  # an operation owns the router; next round
        except Exception as exc:
            tracker.next_check = now + INTERVAL
            tracker.last_reconnect = int(wall)  # do not retry a failing reconnect every round
            save()
            print('whitelist: 4G reconnect failed: ' + str(exc)[:200], flush=True)
    elif usable and not running and tracker.due(now) and not control.busy():
        tracker.next_check = now + INTERVAL  # replaced once the sample arrives
        _workers[wan] = threading.Thread(target=_run, args=(wan, dict(cfg)), name='whitelist-' + wan, daemon=True)
        _workers[wan].start()
    return tracker.status(wan == 'lte')


def tick(configs):
    """Called by the monitor once per round with {'ethernet': cfg, 'lte': cfg} (None when down).
    Never raises; a failure shows as that WAN's state 'error'."""
    now, wall = time.monotonic(), time.time()
    result = {}
    for wan in WANS:
        try:
            result[wan] = step(wan, configs.get(wan), now, wall)
        except Exception as exc:
            result[wan] = {'state': 'error', 'error': str(exc)[:200]}
    return result
