#!/usr/bin/env python3
"""Manual router component updates. Importing this module never runs an update."""
import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from transport import Transport
from retention import prune_component, storage_status

VERSION = '1.1.1'
STATE = Path('/var/lib/router-updater')
SETTINGS = Path('/etc/router/updater/settings.json')
LOCK = Path('/run/router-updater.lock')
MODE = Path('/etc/router/mode')
COMPONENTS = {
    'mihomo': {'label': 'Mihomo', 'repo': 'MetaCubeX/mihomo', 'binary': '/usr/local/bin/mihomo',
               'services': ['mihomo.service']},
    'sing-box': {'label': 'sing-box LX', 'repo': 'Leadaxe/sing-box-lx', 'binary': '/usr/bin/sing-box',
                 'services': ['sing-box.service']},
    'system': {'label': 'Ubuntu + AWG/DKMS'},
}
ACTIONS = ('check', 'prepare', 'install', 'rollback', 'cleanup')
JOB = None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with open(tmp, 'x') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def config():
    result = {'wan': '@@WAN_IF@@'}
    result.update(read_json(SETTINGS, {}))
    return result


def clean_env():
    env = {k: v for k, v in os.environ.items() if k.lower() not in
           ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    env['LC_ALL'] = 'C'
    return env


def progress(message):
    if JOB is None:
        return
    JOB['message'] = message
    JOB['updated'] = int(time.time())
    JOB['log'] = (JOB.get('log', '') + message + '\n')[-24000:]
    write_json(STATE / 'job.json', JOB)


def run(command, timeout=60, check=True, env=None, record=True):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors='replace', timeout=timeout, env=env or clean_env())
    if record and result.stdout.strip():
        progress(result.stdout.strip()[-12000:])
    if check and result.returncode:
        raise RuntimeError('Command failed (%s): %s\n%s' %
                           (result.returncode, Path(command[0]).name, result.stdout[-2500:]))
    return result


@contextlib.contextmanager
def exclusive(path=LOCK, wait=False):
    with open(path, 'a+') as f:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError('Another update or router operation is running')
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_busy():
    try:
        with exclusive():
            return False
    except RuntimeError:
        return True


def component_dir(name):
    if name not in COMPONENTS:
        raise ValueError('Unknown component')
    return STATE / name


def installed(name):
    if name == 'system':
        return {'version': platform.release(), 'reboot_required': Path('/var/run/reboot-required').exists(),
                'dkms': run(['dkms', 'status'], check=False, record=False).stdout.strip()}
    binary = Path(COMPONENTS[name]['binary'])
    if not binary.is_file():
        return {'version': 'Not installed', 'sha256': None}
    args = [str(binary), '-v' if name == 'mihomo' else 'version']
    result = run(args, timeout=8, check=False, record=False)
    match = re.search(r'(?:Meta\s+v|version\s+)(\S+)', result.stdout)
    return {'version': match.group(1) if match else result.stdout.split('\n')[0][:100],
            'sha256': sha(binary)}


def snapshot_status():
    items = {}
    for name in COMPONENTS:
        try:
            item = installed(name)
            item.update({'label': COMPONENTS[name]['label'],
                         'check': read_json(component_dir(name) / 'check.json'),
                         'staged': read_json(component_dir(name) / 'staged.json'),
                         'rollback': name != 'system' and (component_dir(name) / 'rollback.json').exists()})
        except Exception as exc:
            item = {'label': COMPONENTS[name]['label'], 'version': 'Unknown', 'error': str(exc)}
        items[name] = item
    return {'ok': True, 'version': VERSION, 'busy': is_busy(),
            'mode': MODE.read_text().strip(), 'components': items,
            'storage': storage_status(STATE),
            'job': read_json(STATE / 'job.json')}


def cleanup_warning(exc):
    message = 'Cleanup needs attention: ' + str(exc)
    if JOB is not None:
        JOB.setdefault('warnings', []).append(message)
    progress(message)


def prune_safely(name):
    try:
        freed = prune_component(STATE, name)
        if freed:
            progress('Removed %.1f MiB of unused %s update files; keeping two backups and prepared downloads.'
                     % (freed / 1024 / 1024, name))
    except Exception as exc:
        cleanup_warning(exc)


def clean_apt_cache():
    # Keep the cache needed by a downloaded plan. Let APT acquire its own locks.
    if (component_dir('system') / 'staged.json').exists():
        raise RuntimeError('System packages are prepared. Install them before cleaning the APT cache.')
    progress('Clearing downloaded APT package files; installed packages and kernels are preserved')
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=15', 'clean'], timeout=120)


def cleanup_all():
    if (component_dir('system') / 'staged.json').exists():
        raise RuntimeError('System packages are prepared. Install them before cleaning the APT cache.')
    for name in COMPONENTS:
        prune_safely(name)
    clean_apt_cache()


def download(url, destination, transport, max_bytes=350 * 1024 * 1024):
    if not re.match(r'^https://(?:api\.github\.com|github\.com)/', url):
        raise ValueError('Unexpected release URL')
    args = ['curl', '--silent', '--show-error', '--fail', '--location', '--max-redirs', '5',
            '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '15',
            '--max-time', '600', '--retry', '2', '--max-filesize', str(max_bytes),
            '--proxy', transport.url, '--noproxy', '', '--user-agent', 'MiniPC-Router-Updater/' + VERSION,
            '--output', str(destination), url]
    run(args, timeout=1900)
    if Path(destination).stat().st_size > max_bytes:
        raise RuntimeError('Release download is unexpectedly large')


def release(name, transport):
    repo = COMPONENTS[name]['repo']
    url = 'https://api.github.com/repos/' + repo + '/releases'
    url += '/latest'
    with tempfile.TemporaryDirectory(dir=STATE) as temp:
        path = Path(temp) / 'release.json'
        download(url, path, transport, max_bytes=8 * 1024 * 1024)
        data = read_json(path)
    if data.get('draft'):
        raise RuntimeError('Draft releases cannot be installed')
    tag = data['tag_name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,100}', tag):
        raise RuntimeError('Unexpected release version')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'armv7l': 'armv7'}.get(platform.machine())
    if not arch:
        raise RuntimeError('Unsupported CPU architecture')
    if name == 'sing-box':
        asset_name = 'sing-box-%s-linux-%s.tar.gz' % (tag.removeprefix('v'), arch)
    elif name == 'mihomo':
        target = 'amd64-compatible' if arch == 'amd64' else arch
        asset_name = 'mihomo-linux-%s-%s.gz' % (target, tag)
    else:
        raise RuntimeError('Unknown release component')
    assets = [x for x in data.get('assets', []) if x['name'] == asset_name]
    if len(assets) != 1:
        raise RuntimeError('Release has no unique asset for this CPU: ' + asset_name)
    asset = assets[0]
    digest = asset.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest or ''):
        raise RuntimeError('GitHub did not provide the asset SHA256; refusing an unchecked download')
    return {'version': tag, 'prerelease': bool(data.get('prerelease')),
            'url': asset['browser_download_url'], 'asset': asset_name,
            'asset_sha256': digest[7:], 'release_url': data['html_url'],
            'published_at': data['published_at']}


def transport_for(route):
    return Transport(route, wan=config()['wan'])


def check_binary(name, route):
    progress('Checking published %s releases using %s route' % (name, route))
    with transport_for(route) as transport:
        candidate = release(name, transport)
    candidate.update({'checked_at': int(time.time()), 'route': route})
    current = installed(name)
    candidate['installed_version'] = current['version']
    candidate['same_version'] = current['version'].removeprefix('v') == candidate['version'].removeprefix('v')
    write_json(component_dir(name) / 'check.json', candidate)
    progress('Installed: %s; published: %s' % (current['version'], candidate['version']))
    return candidate


def copy_limited(source, target, limit=250 * 1024 * 1024):
    size = 0
    with open(target, 'xb') as out:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise RuntimeError('Extracted binary exceeds size limit')
            out.write(chunk)
    os.chmod(target, 0o755)


def validate_candidate(name, binary):
    progress('Validating candidate against the installed configuration')
    if name == 'mihomo':
        run([str(binary), '-v'], timeout=10)
        run([str(binary), '-t', '-d', '/etc/mihomo', '-f', '/etc/mihomo/config.yaml'], timeout=90)
    elif name == 'sing-box':
        info = run([str(binary), 'version'], timeout=10).stdout
        if '-lx.' not in info:
            raise RuntimeError('Candidate is not a sing-box LX build')
        for directory in ('/etc/sing-box',):
            if Path(directory).is_dir():
                run([str(binary), 'check', '-C', directory], timeout=90)
        for cfg in Path('/etc/sing-box').glob('temp_*/config.json'):
            run([str(binary), 'check', '-c', str(cfg)], timeout=90)
    else:
        run([str(binary), '--help'], timeout=10)


def prepare_binary(name, route):
    # The caller holds the updater lock. Only the current prepared download survives.
    prune_safely(name)
    try:
        return _prepare_binary(name, route)
    finally:
        # Failed downloads are discarded; a previous valid candidate stays usable.
        prune_safely(name)


def _prepare_binary(name, route):
    candidate = check_binary(name, route)
    if candidate['same_version']:
        (component_dir(name) / 'staged.json').unlink(missing_ok=True)
        progress('Already current. No download or restart was performed.')
        return
    directory = component_dir(name)
    temp = Path(tempfile.mkdtemp(prefix='candidate-', dir=directory))
    archive = temp / 'release.asset'
    payload = temp / 'binary'
    with transport_for(route) as transport:
        progress('Downloading the complete release before any service interruption')
        download(candidate['url'], archive, transport)
    if sha(archive) != candidate['asset_sha256']:
        raise RuntimeError('Release SHA256 mismatch')
    if name == 'sing-box':
        with tarfile.open(archive, 'r:gz') as tar:
            members = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == 'sing-box']
            if len(members) != 1 or members[0].size > 250 * 1024 * 1024:
                raise RuntimeError('Unexpected sing-box archive contents')
            with tar.extractfile(members[0]) as source:
                copy_limited(source, payload)
    elif name == 'mihomo':
        with gzip.open(archive, 'rb') as source:
            copy_limited(source, payload)
    else:
        os.replace(archive, payload)
        os.chmod(payload, 0o755)
    validate_candidate(name, payload)
    text = run([str(payload), '-v' if name == 'mihomo' else 'version'], record=False).stdout
    if candidate['version'].removeprefix('v') not in text:
        raise RuntimeError('Candidate version differs from release metadata')
    candidate.update({'directory': temp.name, 'binary_sha256': sha(payload),
                      'base_sha256': sha(COMPONENTS[name]['binary']),
                      'prepared_at': int(time.time())})
    write_json(directory / 'staged.json', candidate)
    archive.unlink(missing_ok=True)
    progress('Prepared %s. Installation requires the Install button.' % candidate['version'])


def atomic_binary(source, destination):
    destination = Path(destination)
    handle, temporary = tempfile.mkstemp(prefix='.router-update-', dir=destination.parent)
    os.close(handle)
    try:
        shutil.copyfile(source, temporary)
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o755)
        with open(temporary, 'rb') as f:
            os.fsync(f.fileno())
        os.replace(temporary, destination)
        fd = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def service_active(unit):
    return run(['systemctl', 'is-active', '--quiet', unit], check=False, record=False).returncode == 0


def verify_runtime():
    if config().get('runtime_check') != 'read-only':
        run(['/usr/local/sbin/router-runtime-ensure', 'check-only'], timeout=20)
        return
    # This host's legacy "check-only" ignores the argument and restores the
    # whole network. Inspect the active route and LAN mark without invoking it.
    route = Transport('current', wan=config()['wan'])
    route.validate_route()
    marks = {'direct': 0, 'awg': 1, 'tun': 2, 'mihomo': 3}
    mode = MODE.read_text().strip()
    if marks[mode]:
        run(['iptables', '-t', 'mangle', '-C', 'PREROUTING', '-i', config().get('lan', '@@LAN_IF@@'),
             '-m', 'comment', '--comment', 'LAN_MODE_MARK', '-j', 'MARK',
             '--set-xmark', hex(marks[mode]) + '/0xffffffff'], timeout=10)


def restore_runtime(mode, active, name):
    for unit in active:
        run(['systemctl', 'restart', unit], timeout=120)
    if active:
        env = clean_env()
        env['ROUTER_MODE_OVERRIDE'] = mode
        run(['/usr/local/sbin/router-restore-runtime'], timeout=280, env=env)
    if MODE.read_text().strip() != mode:
        raise RuntimeError('Router mode changed unexpectedly')
    verify_runtime()
    for unit in active:
        if not service_active(unit):
            raise RuntimeError('Service did not recover: ' + unit)
    # An inactive engine stays inactive. No implicit activation in AWG/DIRECT.
    for unit in COMPONENTS[name]['services']:
        if unit not in active and service_active(unit):
            raise RuntimeError('An inactive service unexpectedly started: ' + unit)


def connectivity_gate():
    progress('Checking real HTTPS traffic through the preserved router mode')
    with transport_for('current') as transport:
        for url in ('https://www.microsoft.com/', 'https://www.apple.com/', 'https://www.yandex.ru/'):
            result = run(['curl', '--silent', '--show-error', '--fail', '--head', '--location',
                          '--proxy', transport.url, '--noproxy', '', '--max-time', '15',
                          '--connect-timeout', '5', '--output', '/dev/null', url],
                         timeout=20, check=False, record=False)
            if result.returncode == 0:
                return
    raise RuntimeError('Post-install HTTPS health check failed')


def guard_active_route(name, mode, active):
    """Keep the selected policy table fail-closed while its interface restarts."""
    relevant = {'mihomo': {'mihomo'}, 'sing-box': {'tun'}}
    if not active or mode not in relevant[name]:
        return None
    table = {'tun': 110, 'mihomo': 120}[mode]
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', str(table)], record=False).stdout)
    # Preserve an existing guard owned by the router lifecycle.
    if any(r.get('type') in ('unreachable', 'blackhole', 'prohibit') and r.get('dst') == 'default' for r in routes):
        return None
    run(['ip', '-4', 'route', 'add', 'unreachable', 'default', 'table', str(table), 'metric', '42761'])
    return table


def remove_update_guard(table):
    if table is not None:
        run(['ip', '-4', 'route', 'del', 'unreachable', 'default', 'table', str(table), 'metric', '42761'])


def replace_component(name, source, metadata):
    binary = Path(COMPONENTS[name]['binary'])
    mode = MODE.read_text().strip()
    active = [u for u in COMPONENTS[name]['services'] if service_active(u)]
    validate_candidate(name, source)
    if name == 'sing-box':
        diversion = run(['dpkg-divert', '--list', str(binary)], record=False).stdout
        if 'sing-box.distrib' not in diversion:
            raise RuntimeError('sing-box package diversion is missing; refusing replacement')
    if active:
        connectivity_gate()
    checkpoint = component_dir(name) / ('backup-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6])
    checkpoint.mkdir(mode=0o700)
    previous = checkpoint / 'binary'
    shutil.copy2(binary, previous)
    before = installed(name)
    record = {'directory': checkpoint.name, 'version': before['version'],
              'binary_sha256': before['sha256'], 'mode': mode, 'active': active,
              'replaces_sha256': sha(source), 'created_at': int(time.time())}
    write_json(checkpoint / 'metadata.json', record)
    progress('Checkpoint created; replacing only the selected component binary')
    with contextlib.ExitStack() as locks:
        locks.enter_context(exclusive(Path('/run/watchdog-rotator.lock')))
        locks.enter_context(exclusive(Path('/run/tun-rotator.lock')))
        guard = guard_active_route(name, mode, active)
        recovered = False
        try:
            atomic_binary(source, binary)
            restore_runtime(mode, active, name)
            if active:
                connectivity_gate()
            recovered = True
        except BaseException as failure:
            progress('Installation failed; restoring the previous binary locally')
            atomic_binary(previous, binary)
            try:
                restore_runtime(mode, active, name)
                if active:
                    connectivity_gate()
                recovered = True
            except Exception as recovery:
                raise RuntimeError('Rollback needs attention: %s; original error: %s' % (recovery, failure)) from recovery
            raise RuntimeError('Previous binary restored successfully. Install error: ' + str(failure)) from failure
        finally:
            # If both installation and recovery failed, leave the fail-closed
            # guard in place instead of allowing traffic to fall back to WAN.
            if recovered:
                remove_update_guard(guard)
    write_json(component_dir(name) / 'rollback.json', record)
    write_json(component_dir(name) / 'installed.json', {'version': metadata['version'], 'sha256': sha(binary)})
    progress('Installed %s; mode remains %s. %s' %
             (metadata['version'], mode.upper(), 'Active service checked.' if active else 'Engine remains stopped; activation was not tested.'))


def staged_binary(name, pointer='staged.json'):
    data = read_json(component_dir(name) / pointer)
    if not data:
        raise RuntimeError('No prepared candidate' if pointer == 'staged.json' else 'No rollback checkpoint')
    dirname = data.get('directory', '')
    if not re.fullmatch(r'(?:candidate|backup)-[A-Za-z0-9_-]+', dirname):
        raise RuntimeError('Invalid local checkpoint')
    source = component_dir(name) / dirname / 'binary'
    if sha(source) != data['binary_sha256']:
        raise RuntimeError('Local candidate/checkpoint SHA256 mismatch')
    expected = data['base_sha256'] if pointer == 'staged.json' else data['replaces_sha256']
    if sha(COMPONENTS[name]['binary']) != expected:
        raise RuntimeError('Installed binary changed since preparation; prepare a new candidate')
    return source, data


def apt_options(transport):
    settings = run(['apt-config', 'dump'], record=False).stdout
    if re.search(r'Acquire::(?:http|https)::(?:Proxy::[^\s"]|Proxy-Auto-Detect|ProxyAutoDetect)', settings, re.I):
        raise RuntimeError('APT has host-specific/automatic proxy settings; review these before using the update transport')
    return ['-o', 'Acquire::http::Proxy=' + transport.url,
            '-o', 'Acquire::https::Proxy=' + transport.url,
            '-o', 'Acquire::ForceIPv4=true', '-o', 'Acquire::Retries=2',
            '-o', 'Acquire::http::Timeout=30', '-o', 'Acquire::https::Timeout=30',
            '-o', 'APT::Update::Error-Mode=any', '-o', 'DPkg::Lock::Timeout=15']


def package_fingerprint():
    data = run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n'], record=False).stdout
    return hashlib.sha256(data.encode()).hexdigest()


def parse_plan(text):
    if re.search(r'^Remv ', text, re.M):
        raise RuntimeError('Package removals are not permitted by this updater')
    packages = []
    for line in text.splitlines():
        if line.startswith('Inst '):
            match = re.match(r'^Inst ([a-zA-Z0-9.+:_-]+)(?: \[[^\]]+\])? \(([^\s]+)', line)
            if not match:
                raise RuntimeError('Could not parse APT package plan')
            packages.append(match.group(1) + '=' + match.group(2))
    return packages


def system_plan(transport, refresh):
    options = apt_options(transport)
    if refresh:
        progress('Refreshing APT package indexes; installed packages are unchanged')
        run(['apt-get'] + options + ['update'], timeout=1800)
    result = run(['apt-get', '--simulate', '--with-new-pkgs', '--no-remove', 'upgrade'], timeout=120)
    return {'packages': parse_plan(result.stdout), 'plan': result.stdout[-18000:],
            'base_fingerprint': package_fingerprint(), 'checked_at': int(time.time()),
            'version': '%d packages' % len(parse_plan(result.stdout))}


def system_action(action, route):
    directory = component_dir('system')
    if action in ('check', 'prepare'):
        with transport_for(route) as transport:
            plan = system_plan(transport, refresh=True)
            plan['route'] = route
            write_json(directory / 'check.json', plan)
            if action == 'prepare' and plan['packages']:
                # A new Download explicitly replaces the old package preparation.
                (directory / 'staged.json').unlink(missing_ok=True)
                clean_apt_cache()
                progress('Downloading package files; nothing will be installed')
                try:
                    run(['apt-get'] + apt_options(transport) + ['--download-only', '--assume-yes', '--no-remove',
                        'install'] + plan['packages'], timeout=3600)
                except BaseException:
                    try:
                        clean_apt_cache()
                    except Exception as exc:
                        cleanup_warning(exc)
                    raise
                # Exact versions are retained; installation will reject a changed package base.
                plan['prepared_at'] = int(time.time())
                write_json(directory / 'staged.json', plan)
            elif action == 'prepare':
                (directory / 'staged.json').unlink(missing_ok=True)
                clean_apt_cache()
            if action == 'prepare':
                prune_safely('system')
            progress('Package check/download complete: ' + plan['version'])
        return
    if action == 'rollback':
        raise RuntimeError('APT has no reliable one-click rollback; use a full system backup/recovery plan')
    plan = read_json(directory / 'staged.json')
    if not plan or not plan['packages']:
        raise RuntimeError('Prepare system packages first')
    if package_fingerprint() != plan['base_fingerprint']:
        raise RuntimeError('Installed package state changed; prepare a fresh package plan')
    trial = run(['apt-get', '--simulate', '--no-remove', 'install'] + plan['packages'], timeout=120)
    if sorted(parse_plan(trial.stdout)) != sorted(plan['packages']):
        raise RuntimeError('APT package plan changed; prepare again')
    import kernel_policy
    kernel_policy.preflight(plan['packages'])
    mode = MODE.read_text().strip()
    before = {x: sha(COMPONENTS[x]['binary']) for x in ('mihomo', 'sing-box')}
    backup = directory / ('before-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6])
    backup.mkdir(mode=0o700)
    run(['tar', '--acls', '--xattrs', '-czf', str(backup / 'etc.PRIVATE.tar.gz'), '-C', '/', 'etc'], timeout=120)
    write_json(backup / 'plan.json', plan)
    write_json(backup / 'state.json', {'mode': mode, 'binaries': before, 'kernel': platform.release()})
    progress('Installing previously downloaded packages. APT rollback is not automatic. No automatic reboot.')
    env = clean_env()
    env.update({'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l', 'LC_ALL': 'C'})
    with contextlib.ExitStack() as locks:
        locks.enter_context(exclusive(Path('/run/watchdog-rotator.lock')))
        locks.enter_context(exclusive(Path('/run/tun-rotator.lock')))
        run(['apt-get', '--no-download', '--assume-yes', '--no-remove',
             '-o', 'DPkg::Lock::Timeout=15', '-o', 'Dpkg::Options::=--force-confdef',
             '-o', 'Dpkg::Options::=--force-confold', 'install'] + plan['packages'], timeout=7200, env=env)
    for name, digest in before.items():
        if sha(COMPONENTS[name]['binary']) != digest:
            raise RuntimeError('APT changed the managed %s binary; manual review required' % name)
    if MODE.read_text().strip() != mode:
        raise RuntimeError('Mode changed during package installation; manual review required')
    verify_runtime()
    dkms = run(['dkms', 'status']).stdout
    kernels = {platform.release()}
    for package in plan['packages']:
        match = re.match(r'linux-image-(\d[^:=]+)', package)
        if match:
            kernels.add(match.group(1))
    for kernel in kernels:
        if not any('amneziawg/' in line and kernel in line and ': installed' in line for line in dkms.splitlines()):
            raise RuntimeError('AWG DKMS is not installed for kernel %s. Do not reboot until repaired.' % kernel)
    progress('System packages installed. Reboot required: ' + str(Path('/var/run/reboot-required').exists()))
    (directory / 'staged.json').unlink(missing_ok=True)
    try:
        kernel_policy.enforce()
    except Exception as exc:
        cleanup_warning(exc)
    prune_safely('system')
    try:
        clean_apt_cache()
    except Exception as exc:
        cleanup_warning(exc)


def execute(name, action, route):
    if name not in COMPONENTS or action not in ACTIONS or route not in ('current', 'direct'):
        raise ValueError('Invalid update operation')
    if action == 'cleanup':
        if name != 'system':
            raise ValueError('Cleanup is a system maintenance action')
        return cleanup_all()
    if name == 'system':
        return system_action(action, route)
    if action == 'check':
        return check_binary(name, route)
    if action == 'prepare':
        return prepare_binary(name, route)
    source, metadata = staged_binary(name, 'rollback.json' if action == 'rollback' else 'staged.json')
    replace_component(name, source, metadata)
    (component_dir(name) / 'staged.json').unlink(missing_ok=True)
    prune_safely(name)


def main():
    global JOB
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('status',) + ACTIONS)
    parser.add_argument('component', nargs='?', choices=tuple(COMPONENTS))
    parser.add_argument('--route', choices=('current', 'direct'), default='current')
    parser.add_argument('--job-id', default=None)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run as root')
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == 'status':
        print(json.dumps(snapshot_status()))
        return
    if not args.component:
        parser.error('component is required')
    component_dir(args.component).mkdir(mode=0o700, exist_ok=True)
    with exclusive(wait=True):
        JOB = {'id': args.job_id or uuid.uuid4().hex, 'component': args.component, 'action': args.action,
               'route': args.route, 'status': 'running', 'started': int(time.time()), 'log': ''}
        progress('Starting ' + args.action + ' for ' + args.component)
        try:
            execute(args.component, args.action, args.route)
            JOB['status'] = 'success'
            progress('Operation completed' + ('; cleanup requires attention (see log)' if JOB.get('warnings') else ''))
        except Exception as exc:
            JOB['status'] = 'failed'
            progress(str(exc))
            raise SystemExit(1)
        finally:
            JOB['finished'] = int(time.time())
            write_json(STATE / 'job.json', JOB)


if __name__ == '__main__':
    main()
