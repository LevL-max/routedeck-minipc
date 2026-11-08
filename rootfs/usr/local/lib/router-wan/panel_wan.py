import json
import threading
import time
from wan_common import ROOT, active, read, settings, locks
import wan_control
import wan_modem
import wan_bands  # lte-bands-v1

_sms_lock = threading.Lock()
_sms_cache = None
_sms_time = 0

def status():
    value = read(ROOT/'status.json', {})
    cfg = settings()
    mobile = dict(cfg['mobile'])
    mobile['password_saved'] = bool(mobile.pop('password', ''))
    mobile.pop('sim_key', None)
    return dict(value, ok=True, active=active(), preferred=cfg['preferred'], fallback=cfg['fallback'],
                force=cfg.get('force') is True,  # wan-force-v1
                panel={'name': 'RouteDeck', 'version': '3.0.10', 'build': '20261020.1'},  # lte-whitelist-v1
                awg_mtu=wan_control.mtu_status(),
                fallback_paused=wan_control.FAILED.exists(),
                failback_seconds=120, mobile_settings=mobile, busy=wan_control.busy(),
                stale=time.time()-value.get('updated', 0)>60,
                job=read(ROOT/'job.json', {}), last_action=read(ROOT/'last-action.json', {}))

def sms():
    global _sms_cache, _sms_time
    with _sms_lock:
        if _sms_cache is None or time.monotonic()-_sms_time>15:
            _sms_cache = wan_modem.sms(); _sms_time = time.monotonic()
        return _sms_cache

def get(h):
    if h.path not in ('/api/wan', '/api/wan/sms', '/api/wan/bands'): return False
    if not h.allowed(): h.j({'ok': False, 'error': 'LAN access only'}, 403); return True
    try: h.j(sms() if h.path.endswith('/sms') else wan_bands.status() if h.path.endswith('/bands') else status())
    except Exception as exc: h.j({'ok': False, 'error': str(exc)[:400]}, 409)
    return True

def post(h):
    global _sms_cache, _sms_time
    if h.path not in ('/api/wan', '/api/wan/sms/delete'): return False
    if not h.allowed() or not h.authorized(): h.j({'ok': False, 'error': 'Request rejected'}, 403); return True
    try:
        size = int(h.headers.get('Content-Length', '0'))
        if not 0<size<=4096: raise ValueError('Invalid request size')
        body = json.loads(h.rfile.read(size))
        if h.path.endswith('/sms/delete'):
            with locks(), _sms_lock:
                result = wan_modem.delete_sms(body)
                _sms_cache = None; _sms_time = 0
            h.j(result)
        else: h.j(wan_control.request(body), 202)
    except Exception as exc: h.j({'ok': False, 'error': str(exc)[:400]}, 409)
    return True
