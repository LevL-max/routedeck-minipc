"""Bounded updater storage; never remove installed packages or external backups."""
import json
import os
import re
import shutil
from pathlib import Path

KEEP_BACKUPS = 2
CANDIDATE = r'candidate-[A-Za-z0-9_-]+'
BACKUP = r'backup-\d{8}T\d{6}Z-[0-9a-f]{6}'
SYSTEM_BACKUP = r'before-\d{8}T\d{6}Z(?:-[0-9a-f]{6})?'


def tree_bytes(path):
    path = Path(path)
    if path.is_symlink():
        return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()
                   and not os.path.ismount(Path(root) / d)]
        for name in files:
            item = Path(root) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except FileNotFoundError:
                pass
    return total


def pointer(directory, filename, pattern):
    path = directory / filename
    if path.is_symlink():
        raise RuntimeError('Refusing cleanup with a symlinked state pointer')
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        name = data['directory']
        if not isinstance(name, str) or not re.fullmatch(pattern, name):
            raise ValueError('Invalid directory')
        child = directory / name
        if child.is_symlink() or not child.is_dir():
            raise ValueError('Missing or linked directory')
        return name
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Refusing cleanup: invalid ' + filename) from exc


def remove_owned_tree(directory, child):
    directory, child = Path(directory), Path(child)
    if (directory.is_symlink() or child.is_symlink() or not child.is_dir()
            or child.parent != directory or child.resolve().parent != directory.resolve()):
        raise RuntimeError('Refusing cleanup outside the component directory')
    # Do not descend into mounts, even within a recognized updater directory.
    for root, dirs, _ in os.walk(child, followlinks=False):
        if os.path.ismount(root) or any(os.path.ismount(Path(root) / d) for d in dirs):
            raise RuntimeError('Refusing cleanup of a mounted directory')
    size = tree_bytes(child)
    shutil.rmtree(child)
    return size


def prune_component(state, name):
    state = Path(state)
    if name not in ('mihomo', 'sing-box', 'system') or state.is_symlink():
        raise RuntimeError('Invalid updater storage root')
    directory = state / name
    if directory.is_symlink():
        raise RuntimeError('Refusing cleanup of a symlinked component directory')
    if not directory.exists():
        return 0
    staged = None if name == 'system' else pointer(directory, 'staged.json', CANDIDATE)
    rollback = None if name == 'system' else pointer(directory, 'rollback.json', BACKUP)
    pattern = SYSTEM_BACKUP if name == 'system' else BACKUP
    backups = sorted((p for p in directory.iterdir() if re.fullmatch(pattern, p.name)
                      and p.is_dir() and not p.is_symlink()), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    keep = {rollback} if rollback else set()
    for path in backups:
        if len(keep) >= KEEP_BACKUPS:
            break
        keep.add(path.name)
    freed = 0
    for path in list(directory.iterdir()):
        if re.fullmatch(CANDIDATE, path.name) and name != 'system' and path.name != staged:
            freed += remove_owned_tree(directory, path)
        elif re.fullmatch(pattern, path.name) and path.name not in keep:
            freed += remove_owned_tree(directory, path)
    return freed


def storage_status(state, root='/', boot='/boot', apt='/var/cache/apt/archives'):
    root_usage, boot_usage = shutil.disk_usage(root), shutil.disk_usage(boot)
    return {'free_bytes': root_usage.free, 'total_bytes': root_usage.total,
            'boot_free_bytes': boot_usage.free, 'boot_total_bytes': boot_usage.total,
            'updater_bytes': tree_bytes(state), 'apt_cache_bytes': tree_bytes(apt),
            'kernel_images': sorted(p.name.removeprefix('vmlinuz-') for p in Path(boot).glob('vmlinuz-*')),
            'keep_backups': KEEP_BACKUPS}
