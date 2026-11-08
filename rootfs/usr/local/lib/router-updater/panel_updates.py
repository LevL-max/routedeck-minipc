"""Adapter for the existing LAN router panel. No automatic update scheduling."""
import contextlib
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
import router_update_core as core
import panel_system

_guard = threading.Lock()
_cache = None
_cache_time = 0


def pending_wan():
    p=core.run(['systemctl','show','router-wan-worker.service','-p','ActiveState','--value'],timeout=3,check=False,record=False)
    return p.stdout.strip() in ('active','activating','reloading','deactivating') or Path('/var/lib/router-wan/transaction.json').exists()

def pending_xray():
    p=core.run(['systemctl','show','router-xray-worker.service','-p','ActiveState','--value'],timeout=3,check=False,record=False)
    return p.stdout.strip() in ('active','activating','reloading','deactivating')


def pending_service():
    job = core.read_json(core.STATE / 'job.json', {})
    ident = job.get('id', '')
    if job.get('status') not in ('queued', 'running') or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
        return False
    result = core.run(['systemctl', 'show', 'router-update-' + ident + '.service',
                       '-p', 'ActiveState', '--value'], timeout=3, check=False, record=False)
    return result.stdout.strip() in ('active', 'activating', 'reloading', 'deactivating')


def busy():
    return core.is_busy() or pending_service() or pending_xray() or pending_wan() or panel_system.reboot_status()['pending']


def status():
    global _cache, _cache_time
    with _guard:
        if _cache is None or time.monotonic() - _cache_time > 15:
            _cache = core.snapshot_status()
            _cache_time = time.monotonic()
        result = dict(_cache)
    result['busy'] = busy()
    result['mode'] = core.MODE.read_text().strip()
    result['job'] = core.read_json(core.STATE / 'job.json')
    result['reboot'] = panel_system.reboot_status()
    result['kernels'] = core.read_json(core.STATE / 'kernel-policy.json', {'limit': 2})
    for name, value in result['components'].items():
        value['check'] = core.read_json(core.component_dir(name) / 'check.json')
        value['staged'] = core.read_json(core.component_dir(name) / 'staged.json')
        value['rollback'] = name != 'system' and (core.component_dir(name) / 'rollback.json').exists()
    if result['job'] and result['job'].get('status') in ('running', 'queued') and not result['busy']:
        result['job'] = dict(result['job'], status='interrupted',
                             message='Worker stopped unexpectedly. Review the log before retrying.')
    return result


@contextlib.contextmanager
def mode_guard():
    if panel_system.reboot_status()['pending']:
        raise RuntimeError('Router reboot is pending')
    if pending_service() or pending_xray() or pending_wan():
        raise RuntimeError('An update operation is running; wait before changing the router mode')
    with core.exclusive():
        yield


def launch(body):
    name, action = body.get('component'), body.get('action')
    route = body.get('route', 'current')
    if name not in core.COMPONENTS or action not in core.ACTIONS or route not in ('current', 'direct'):
        raise ValueError('Invalid update request')
    if action in ('install', 'rollback', 'cleanup') and body.get('confirm') is not True:
        raise ValueError('Confirm the selected installation/rollback in the panel')
    if name == 'system' and action == 'rollback':
        raise ValueError('System packages do not support automatic rollback')
    if action == 'cleanup' and name != 'system':
        raise ValueError('Cleanup is a system maintenance action')
    with mode_guard():
        ident = uuid.uuid4().hex
        job = {'id': ident, 'component': name, 'action': action, 'route': route,
               'status': 'queued', 'started': int(time.time()), 'message': 'Starting worker', 'log': ''}
        core.write_json(core.STATE / 'job.json', job)
        args = ['systemd-run', '--quiet', '--collect', '--no-block', '--unit=router-update-' + ident,
                '--property=Type=exec', '--property=RuntimeMaxSec=3h', '--property=Nice=10',
                '/usr/bin/python3', '/usr/local/lib/router-updater/router_update_core.py',
                action, name, '--route', route, '--job-id', ident]
        result = core.run(args, timeout=10, check=False, record=False)
        if result.returncode:
            core.write_json(core.STATE / 'job.json', dict(job, status='failed', message=result.stdout[-2000:]))
            raise RuntimeError('Could not start update worker: ' + result.stdout[-1000:])
    return {'ok': True, 'id': ident, 'message': action.capitalize() + ' started'}
