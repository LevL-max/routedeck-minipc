"""Keep two Ubuntu generic kernel releases; never remove the running kernel."""
import contextlib
import fcntl
import json
import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path
import router_update_core as c

ABI = r'\d+\.\d+\.\d+-\d+'
KERNEL = ABI + r'-generic'
STATUS = c.STATE / 'kernel-policy.json'
MANIFEST = c.STATE / 'kernel-purge.json'


def release_for_package(name):
    name = name.split(':')[0]
    match = re.fullmatch(r'linux-(?:image-(?:unsigned-)?|modules-(?:extra-)?|headers-|tools-)' + '(' + KERNEL + ')', name)
    if match:
        return match[1]
    match = re.fullmatch(r'linux-(?:headers-|hwe-\d+\.\d+-(?:headers|tools)-)(' + ABI + ')', name)
    return match[1] + '-generic' if match else None


def image_release(name):
    match = re.fullmatch(r'linux-image-(?:unsigned-)?(' + KERNEL + ')(?::[a-z0-9]+)?', name)
    if not match and re.match(r'linux-image-(?:unsigned-)?\d', name):
        raise RuntimeError('Kernel policy supports the audited Ubuntu generic kernel flavour only')
    return match[1] if match else None


def sort_kernels(values):
    return sorted(set(values), key=lambda x: tuple(int(n) for n in re.findall(r'\d+', x)), reverse=True)


def inventory():
    output = c.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Abbrev}\n'], record=False).stdout
    packages = {}
    for line in output.splitlines():
        fields = line.split('\t')
        if len(fields) == 3:
            name, version, state = fields
            if name.startswith('linux-') and release_for_package(name):
                if state[1:2] not in ('i', 'c', 'n') or state[2:3].strip():
                    raise RuntimeError('Incomplete kernel package state: ' + name)
                if state[1:2] in ('i', 'c'):
                    packages[name] = {'version': version, 'state': state}
            elif name.startswith('linux-image-') and state[1:2] == 'i':
                image_release(name)  # Reject unsupported installed kernel flavours.
    kernels = sort_kernels(image_release(name) for name, info in packages.items()
                           if image_release(name) and info['state'][1:2] == 'i')
    return packages, kernels


def select_keep(installed, additional=(), running=None):
    running = running or platform.release()
    candidates = sort_kernels(list(installed) + list(additional))
    keep = candidates[:2]
    if not re.fullmatch(KERNEL, running) or running not in candidates:
        raise RuntimeError('Running kernel is not in the managed installed kernel set')
    if running not in keep:
        raise RuntimeError('Reboot into the newest installed kernel before another kernel update; the running kernel will never be removed')
    return keep


def preflight(packages):
    _, kernels = inventory()
    added = [image_release(item.split('=')[0]) for item in packages]
    return select_keep(kernels, [x for x in added if x])


def verify_bootable(kernels):
    dkms = c.run(['dkms', 'status'], record=False).stdout
    for kernel in set(kernels):
        for prefix in ('vmlinuz-', 'initrd.img-'):
            path = Path('/boot') / (prefix + kernel)
            if not path.is_file() or path.stat().st_size < 1024:
                raise RuntimeError('Missing boot file: ' + str(path))
        if not any(line.startswith('amneziawg/') and (', ' + kernel + ',') in line
                   and line.rstrip().endswith(': installed') for line in dkms.splitlines()):
            raise RuntimeError('AWG DKMS is not installed for kernel ' + kernel)


@contextlib.contextmanager
def package_locks():
    """Readiness check/short reboot guard using the same POSIX locks as dpkg/APT."""
    with contextlib.ExitStack() as stack:
        for path in ('/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock'):
            stream = stack.enter_context(open(path, 'a+'))
            try:
                fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('APT/dpkg is busy; retry after the package operation finishes') from exc
        yield


def parse_purge(text):
    if re.search(r'^(?:Inst|Conf) ', text, re.M):
        raise RuntimeError('Kernel cleanup would install/configure other packages')
    return {line.split()[1] for line in text.splitlines() if line.startswith(('Remv ', 'Purg '))}


def cleanup_plan():
    packages, kernels = inventory()
    keep = select_keep(kernels)
    remove = sorted(name for name in packages if release_for_package(name) not in keep)
    held = set(c.run(['apt-mark', 'showhold'], record=False).stdout.splitlines())
    if held.intersection(remove):
        raise RuntimeError('An old kernel package is held; review it before cleanup')
    return {'keep': keep, 'remove': remove, 'running': platform.release(),
            'base_fingerprint': c.package_fingerprint()}


def verify_purge():
    plan = c.read_json(MANIFEST)
    if (not plan or c.package_fingerprint() != plan['base_fingerprint']
            or platform.release() != plan['running'] or plan['running'] not in plan['keep']):
        raise RuntimeError('Kernel cleanup package state changed; refusing package removal')


def remove_old_boot_copies(keep, removed_packages):
    # Only these known generated backup files, only after a successful purge.
    for path in Path('/boot').glob('initrd.img-*.old-dkms'):
        kernel = path.name[len('initrd.img-'):-len('.old-dkms')]
        if re.fullmatch(KERNEL, kernel) and kernel not in keep and not path.is_symlink():
            path.unlink()
    # dpkg may leave unowned DKMS files after purging an image. Restrict cleanup
    # to versions in this successfully completed package transaction.
    from retention import remove_owned_tree
    for kernel in {release_for_package(name) for name in removed_packages}:
        if kernel and kernel not in keep and kernel != platform.release():
            path = Path('/lib/modules') / kernel
            if path.exists():
                remove_owned_tree(Path('/lib/modules'), path)


def _enforce():
    with package_locks():
        plan = cleanup_plan()
    verify_bootable([plan['running'], plan['keep'][0]])
    if plan['remove']:
        options = ['-o', 'APT::Get::AutomaticRemove=false', '-o', 'DPkg::Lock::Timeout=30']
        trial = c.run(['apt-get', '--simulate'] + options + ['purge'] + plan['remove'], timeout=120, record=False)
        if parse_purge(trial.stdout) != set(plan['remove']):
            raise RuntimeError('APT would remove packages outside the selected old kernel versions')
        if c.package_fingerprint() != plan['base_fingerprint']:
            raise RuntimeError('Package state changed during kernel cleanup planning')
        c.write_json(MANIFEST, plan)
        try:
            c.progress('Removing old kernel packages; keeping ' + ', '.join(plan['keep']))
            c.run(['apt-get', '--assume-yes', '--no-download'] + options + [
                '-o', 'DPkg::Pre-Invoke::=/usr/bin/python3 /usr/local/lib/router-updater/kernel_policy.py verify-purge',
                'purge'] + plan['remove'], timeout=1800,
                env=dict(c.clean_env(), DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l'))
        finally:
            MANIFEST.unlink(missing_ok=True)
    _, actual = inventory()
    if actual != plan['keep']:
        raise RuntimeError('Kernel retention verification failed')
    verify_bootable([plan['running'], actual[0]])
    remove_old_boot_copies(actual, plan['remove'])
    result = {'ok': True, 'limit': 2, 'kernels': actual, 'running': plan['running'],
              'removed_packages': plan['remove'], 'checked_at': int(time.time())}
    c.write_json(STATUS, result)
    return result


def enforce():
    try:
        return _enforce()
    except Exception as exc:
        c.write_json(STATUS, {'ok': False, 'limit': 2, 'error': str(exc), 'checked_at': int(time.time())})
        raise


def guard_install(paths):
    """APT version-1 pre-install hook: files arrive on stdin, never shell-expanded."""
    additions = []
    for text in paths:
        path = text.rstrip('\n')
        if not path or not path.endswith('.deb'):
            continue
        name = c.run(['dpkg-deb', '-f', path, 'Package'], timeout=10, record=False).stdout.strip()
        if image_release(name):
            additions.append(name)
    if additions:
        preflight(additions)


def main():
    action = sys.argv[1] if len(sys.argv) == 2 else ''
    if action == 'guard-install':
        guard_install(sys.stdin)
        return
    if action == 'verify-purge':
        verify_purge()
        return
    if action not in ('plan', 'enforce'):
        raise SystemExit('Expected plan, enforce, guard-install or verify-purge')
    if action == 'plan':
        print(json.dumps(cleanup_plan()))
        return
    with c.exclusive(wait=True):
        try:
            deadline = time.monotonic() + 600
            while True:
                try:
                    with package_locks():
                        pass
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
            result = enforce()
            print(json.dumps(result))
        except Exception as exc:
            c.write_json(STATUS, {'ok': False, 'limit': 2, 'error': str(exc), 'checked_at': int(time.time())})
            raise


if __name__ == '__main__':
    main()
