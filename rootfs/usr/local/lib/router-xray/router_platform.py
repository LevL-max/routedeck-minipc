import json
from pathlib import Path
def wan_interface():
    name=json.loads(Path('/etc/router/updater/settings.json').read_text())['wan']
    if not isinstance(name,str) or not name or len(name)>15 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-' for c in name):
        raise ValueError('Invalid configured WAN interface')
    return name
def descriptor():
    value=json.loads(Path('/etc/router/platform.json').read_text())
    return dict(value,active_wan_interface=wan_interface())
