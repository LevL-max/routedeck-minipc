"""Independent physical WAN routing. Existing VPN policy rules keep precedence."""
import ipaddress
import json
import os
import socket
import ssl
import struct
import sys
import time
from pathlib import Path
from wan_common import ACTIVE, ETH, FIREWALL, LAN_CIDR, LAN_IF, MODE, ROOT, active, active_interface, atomic, command_json, read, run
sys.path.insert(0, '/usr/local/lib/router-updater')
try:
    from transport import dns_addresses
except ImportError:  # host without router-updater (Mini-PC 3)
    from wan_dns import dns_addresses

TABLES = {'ethernet': 141, 'lte': 140}
MARKS = {'ethernet': 0x641, 'lte': 0x640}

def ethernet():
    routes = command_json(['ip', '-j', '-4', 'route', 'show', 'table', 'main', 'default'])
    route = next((r for r in routes if r.get('dev') == ETH and r.get('gateway')), None)
    if not route: return None
    addresses = command_json(['ip', '-j', '-4', 'address', 'show', 'dev', ETH])[0].get('addr_info', [])
    address = next((a for a in addresses if a.get('scope') == 'global'), None)
    if not address: return None
    return {'interface': ETH, 'address': address['local'], 'prefix': address['prefixlen'],
            'gateway': route['gateway'], 'mtu': int((Path('/sys/class/net')/ETH/'mtu').read_text()), 'dns': [route['gateway']]}

def routes(table):
    p = run(['ip', '-j', '-4', 'route', 'show', 'table', str(table)], check=False)
    return json.loads(p.stdout or '[]')

def rule(priority, *parts):
    existing = command_json(['ip', '-j', '-4', 'rule'])
    # These priorities belong exclusively to router-wan; installation verifies they are free.
    if not any(r['priority'] == priority for r in existing):
        run(['ip', '-4', 'rule', 'add', 'priority', str(priority), *parts])

def interface_exists(name):
    return bool(name and (Path('/sys/class/net')/name).exists())

def usable_config(cfg):
    # QMI metadata persists across reboot even when its old IP is absent.
    if not cfg or not interface_exists(cfg.get('interface')): return None
    result = command_json(['ip', '-j', '-4', 'address', 'show', 'dev', cfg['interface']])
    if not result or 'UP' not in result[0].get('flags', []): return None
    if not any(a.get('local') == cfg.get('address') and a.get('prefixlen') == cfg.get('prefix')
               for a in result[0].get('addr_info', [])): return None
    return cfg


def carrier(name):
    """wan-force-v1: physical link of a NIC; False when it is down or has no cable."""
    try: return (Path('/sys/class/net')/name/'carrier').read_text().strip() == '1'
    except OSError: return False


def link(wan, cfg):
    """wan-force-v1: the WAN can carry packets at all: its IPv4 settings on an UP interface and, for
    Ethernet, a cable with carrier. Nothing about internet beyond the first hop."""
    try: cfg = usable_config(cfg)
    except (OSError, RuntimeError, ValueError): return False
    return bool(cfg) and (wan != 'ethernet' or carrier(cfg['interface']))


def table_config(table, cfg, local=False):
    table = str(table)
    cfg = usable_config(cfg)
    run(['ip', '-4', 'route', 'replace', 'unreachable', 'default', 'table', table, 'metric', '32767'])
    wanted = set()
    if cfg and interface_exists(cfg['interface']):
        network = str(ipaddress.IPv4Network(cfg['address']+'/'+str(cfg['prefix']), strict=False))
        run(['ip', '-4', 'route', 'replace', network, 'dev', cfg['interface'], 'src', cfg['address'],
             'scope', 'link', 'table', table, 'metric', '20'])
        wanted.add((network, cfg['interface']))
        run(['ip', '-4', 'route', 'replace', 'default', 'via', cfg['gateway'], 'dev', cfg['interface'],
             'onlink', 'src', cfg['address'], 'table', table, 'metric', '10'])
    else:
        for route in routes(table):
            if route.get('dst') == 'default' and route.get('type', 'unicast') == 'unicast':
                run(['ip', '-4', 'route', 'del', 'default', 'table', table,
                     'metric', str(route.get('metric', 0))], check=False)
    if local:
        # Preserve every connected network, including the internal TCP endpoint of
        # system/mixed TUN stacks. A WAN default before main otherwise steals it.
        for route in command_json(['ip', '-j', '-4', 'route', 'show', 'table', 'main']):
            if route.get('scope') != 'link' or not route.get('dev'): continue
            if route.get('type', 'unicast') != 'unicast': continue
            if not route.get('dst') or route['dst'] == 'default': continue
            args = ['ip', '-4', 'route', 'replace', route['dst'], 'dev', route['dev'], 'scope', 'link',
                    'table', table, 'metric', '20']
            if route.get('prefsrc'): args += ['src', route['prefsrc']]
            run(args)
            wanted.add((route['dst'], route['dev']))
    for route in routes(table):
        if route.get('dst') == 'default':
            if route.get('type', 'unicast') == 'unicast' and route.get('metric', 0) != 10:
                run(['ip', '-4', 'route', 'del', 'default', 'table', table,
                     'metric', str(route.get('metric', 0))], check=False)
            continue
        if (route.get('dst'), route.get('dev')) not in wanted or route.get('metric', 0) != 20:
            run(['ip', '-4', 'route', 'del', route['dst'], 'table', table,
                 'metric', str(route.get('metric', 0))], check=False)

def refresh_connected():
    selected = active()
    cfg = ethernet() if selected['wan'] == 'ethernet' else read(ROOT/'bearer.json')
    table_config(142, cfg, local=True)


def prepare(eth, lte):
    for wan, cfg in (('ethernet', eth), ('lte', lte)):
        table_config(TABLES[wan], cfg)
        rule(80 if wan == 'lte' else 81, 'fwmark', str(MARKS[wan]), 'lookup', str(TABLES[wan]))
    # The installation's old one-off source rule is superseded by an exact current bearer rule.
    previous = read(ROOT/'source-rule.json', {})
    if lte:
        source = lte['address']
        if previous.get('address') != source or previous.get('priority') != 140:
            for item in command_json(['ip', '-j', '-4', 'rule']):
                if item.get('priority') in (90, 140) and str(item.get('table')) == '140':
                    run(['ip', '-4', 'rule', 'del', 'priority', str(item['priority']), 'lookup', '140'], check=False)
            # Explicit VPN marks must precede this unmarked LTE source route.
            rule(140, 'from', source, 'lookup', '140')
            atomic(ROOT/'source-rule.json', {'address': source, 'priority': 140})
    elif previous:
        for priority in (90, 140):
            run(['ip', '-4', 'rule', 'del', 'priority', str(priority), 'lookup', '140'], check=False)
        atomic(ROOT/'source-rule.json', {})

def select(wan, cfg, dns=True):
    table_config(142, cfg, local=True)
    # Routes and LAN reachability exist before the new rule is published.
    rule(150, 'lookup', '142')
    previous = active()
    interface = cfg['interface'] if cfg else (ETH if wan == 'ethernet' else (previous.get('interface', 'wwan0') if previous.get('wan') == 'lte' else 'wwan0'))
    atomic(ACTIVE, {'wan': wan, 'interface': interface,
                   'generation': previous.get('generation', 0) + (previous.get('wan') != wan or previous.get('interface') != interface),
                   'gateway': cfg.get('gateway') if cfg else None})
    if dns and cfg:
        # The policy for LAN DNS stays in the selected traffic engine. This only selects host bootstrap DNS.
        if wan == 'lte':
            run(['resolvectl', 'dns', interface, *(cfg.get('dns') or ['77.88.8.8', '1.1.1.1'])])
            run(['resolvectl', 'domain', interface, '~.'])
            run(['resolvectl', 'default-route', interface, 'yes'])
            run(['resolvectl', 'default-route', ETH, 'no'], check=False)
        else:
            for path in Path('/sys/class/net').glob('wwan*'):
                run(['resolvectl', 'revert', path.name], check=False)
            run(['resolvectl', 'default-route', ETH, 'yes'])
    direct_nat()

def firewall():
    # nft hosts: the mode files (forward policy drop, per-WAN accept) are the kill switch.
    if FIREWALL == 'nft': return
    for chain in ('ROUTER_WAN_GUARD', 'ROUTER_LTE_INPUT'):
        run(['iptables', '-w', '5', '-N', chain], check=False)
    rules = ['*filter', '-F ROUTER_WAN_GUARD', '-F ROUTER_LTE_INPUT',
             '-A ROUTER_WAN_GUARD -i '+LAN_IF+' -o '+ETH+' -m mark ! --mark 0x0/0xffffffff -j REJECT',
             '-A ROUTER_WAN_GUARD -i '+LAN_IF+' -o wwan+ -m mark ! --mark 0x0/0xffffffff -j REJECT',
             '-A ROUTER_WAN_GUARD -i wwan+ -m conntrack ! --ctstate ESTABLISHED,RELATED -j DROP',
             '-A ROUTER_LTE_INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
             '-A ROUTER_LTE_INPUT -p icmp -j ACCEPT', '-A ROUTER_LTE_INPUT -j DROP', 'COMMIT', '']
    if MODE.read_text().strip() != 'direct':
        # Also protects the boot interval before LAN marks/tunnel interfaces are restored.
        rules[3:3] = ['-A ROUTER_WAN_GUARD -i '+LAN_IF+' -o '+ETH+' -j REJECT',
                      '-A ROUTER_WAN_GUARD -i '+LAN_IF+' -o wwan+ -j REJECT']
    run(['iptables-restore', '-w', '5', '--noflush'], data='\n'.join(rules))
    for chain, args in (('FORWARD', ['-j', 'ROUTER_WAN_GUARD']),
                        ('INPUT', ['-i', 'wwan+', '-j', 'ROUTER_LTE_INPUT'])):
        if run(['iptables', '-w', '5', '-C', chain, *args], check=False).returncode:
            run(['iptables', '-w', '5', '-I', chain, '1', *args])

def direct_nat():
    # nft hosts: router-apply-mode renders masquerading for the selected WAN.
    if FIREWALL == 'nft': return
    mode, iface = MODE.read_text().strip(), active_interface()
    # Preserve existing tunnel NAT. Remove only physical-WAN LAN masquerading when inappropriate.
    interfaces = {ETH, iface, 'wwan0', 'wwan1'} | {p.name for p in Path('/sys/class/net').glob('wwan*')}
    for name in sorted(interfaces):
        args = ['POSTROUTING', '-s', LAN_CIDR, '-o', name, '-j', 'MASQUERADE']
        if mode == 'direct' and name == iface:
            if run(['iptables', '-w', '5', '-t', 'nat', '-C', *args], check=False).returncode:
                run(['iptables', '-w', '5', '-t', 'nat', '-A', *args])
        else:
            while not run(['iptables', '-w', '5', '-t', 'nat', '-C', *args], check=False).returncode:
                run(['iptables', '-w', '5', '-t', 'nat', '-D', *args])

def bound_socket(wan, cfg, kind=socket.SOCK_STREAM, timeout=3):
    sock = socket.socket(socket.AF_INET, kind)
    sock.settimeout(timeout)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, cfg['interface'].encode()+b'\0')
    sock.setsockopt(socket.SOL_SOCKET, 36, MARKS[wan])
    sock.bind((cfg['address'], 0))
    return sock

# Mobile and Russian networks drop ICMP and cut UDP to outside resolvers first. Names resolve through the
# WAN's own DNS (DHCP lease, gateway, LTE bearer) before TCP to public resolvers; the verdict is verified
# HTTPS to any one of several unrelated services. An address that already passed HTTPS stands in for DNS.
PROBE_TARGETS = (('ya.ru', '/robots.txt'), ('vk.com', '/robots.txt'),
                 ('www.gstatic.com', '/generate_204'), ('www.cloudflare.com', '/cdn-cgi/trace'))
PUBLIC_RESOLVERS = (('77.88.8.8', 'tcp'), ('77.88.8.1', 'tcp'), ('1.1.1.1', 'tcp'), ('77.88.8.8', 'udp'))
_answered = {}  # wan -> resolver that answered last; asked first next time
_verified = {}  # host -> recent addresses that passed verified HTTPS

def resolvers(wan, cfg):
    own = list(cfg.get('dns') or [])
    try:
        index = (Path('/sys/class/net')/cfg['interface']/'ifindex').read_text().strip()
        lease = (Path('/run/systemd/netif/leases')/index).read_text().splitlines()
        own = [s for line in lease if line.startswith('DNS=') for s in line[4:].split()] + own
    except OSError: pass
    methods = list(dict.fromkeys([(s, 'udp') for s in own if ':' not in s] + list(PUBLIC_RESOLVERS)))
    return sorted(methods, key=lambda method: method != _answered.get(wan))

def query(wan, cfg, host, server, protocol):
    transaction = int.from_bytes(os.urandom(2), 'big')
    labels = host.encode('idna').split(b'.')
    packet = struct.pack('!6H', transaction, 0x100, 1, 0, 0, 0)
    packet += b''.join(bytes([len(p)])+p for p in labels) + b'\0' + struct.pack('!HH', 1, 1)
    if protocol == 'udp':
        with bound_socket(wan, cfg, socket.SOCK_DGRAM, 1.5) as sock:
            sock.connect((server, 53)); sock.send(packet)
            return dns_addresses(sock.recv(4096), transaction)
    with bound_socket(wan, cfg, socket.SOCK_STREAM, 1.5) as sock:
        sock.connect((server, 53)); sock.sendall(len(packet).to_bytes(2, 'big')+packet)
        data = b''
        while len(data) < 2 or len(data) < 2+int.from_bytes(data[:2], 'big'):
            chunk = sock.recv(4096)
            if not chunk: raise OSError('DNS over TCP closed early')
            data += chunk
        return dns_addresses(data[2:2+int.from_bytes(data[:2], 'big')], transaction)

def resolve(wan, cfg, host, failed=None):
    failed = set() if failed is None else failed
    for method in resolvers(wan, cfg):
        if method in failed: continue
        try:
            addresses = query(wan, cfg, host, *method)
        except (OSError, ValueError):
            failed.add(method); continue
        _answered[wan] = method
        return addresses
    raise OSError('DNS failed through this WAN')

def probe(wan, cfg):
    started = time.monotonic()
    result = {'ok': False, 'checked_at': int(time.time()), 'interface': cfg.get('interface') if cfg else None,
              'dns_ok': False, 'https_ok': False}
    if not link(wan, cfg):
        # wan-force-v1: tell an unplugged cable from a missing lease or bearer; neither can pass.
        unplugged = wan == 'ethernet' and not carrier((cfg or {}).get('interface') or ETH)
        return dict(result, link=False, error='No link: cable unplugged?' if unplugged else 'No IPv4 connection')
    result['link'] = True
    # No system DNS or other WAN fallback: both DNS and TLS sockets are marked and device/source-bound.
    errors, failed = [], set()
    for host, urlpath in PROBE_TARGETS:
        if time.monotonic() - started > 13: break
        try:
            addresses = resolve(wan, cfg, host, failed)[:1]
            result.update(dns_ok=True, dns_via='/'.join(_answered[wan]))
        except OSError:
            addresses = _verified.get(host, [])[:1]
            errors.append(host+' DNS')
        for address in addresses:
            try:
                with bound_socket(wan, cfg) as raw:
                    raw.connect((address, 443))
                    with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as sock:
                        sock.sendall(('GET '+urlpath+' HTTP/1.1\r\nHost: '+host+'\r\nConnection: close\r\n\r\n').encode())
                        header = sock.recv(1024).split(b'\r\n', 1)[0].split()
                        if len(header) < 2 or not 200 <= int(header[1]) < 400: raise OSError('HTTPS response rejected')
            except (OSError, ValueError) as exc:
                errors.append(host+' '+type(exc).__name__); continue
            _verified[host] = [address] + [a for a in _verified.get(host, []) if a != address][:3]
            return dict(result, ok=True, https_ok=True, target=host, elapsed_ms=round((time.monotonic()-started)*1000))
    return dict(result, error='Internet check failed (DNS + verified HTTPS): '+', '.join(errors),
                elapsed_ms=round((time.monotonic()-started)*1000))
