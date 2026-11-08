"""Read the actual on-disk connection graph; nodes.json is optional name metadata."""
import json
from pathlib import Path
import yaml
CONFIG=Path('/etc/xray/config.json')
MIHOMO=Path('/etc/mihomo/config.yaml')
NODES=Path('/etc/xray/nodes.json')
def live_nodes(config_path=None):
    c=json.loads((config_path or CONFIG).read_text());m=yaml.safe_load(MIHOMO.read_text())
    try:meta=json.loads(NODES.read_text())
    except (ValueError,FileNotFoundError):meta=[]
    names={n.get('inbound'):n.get('name') for n in meta if isinstance(n,dict)}
    proxies={p.get('port'):p['name'] for p in m.get('proxies',[]) if p.get('type')=='socks5' and p.get('server')=='127.0.0.1'}
    outbound={o['tag']:o for o in c.get('outbounds',[])};result=[]
    for i in c.get('inbounds',[]):
        if i.get('protocol')!='socks':continue
        if i.get('listen')!='127.0.0.1' or type(i.get('port')) is not int or not 1<=i['port']<=64535:
            raise ValueError('Xray bridge requires loopback SOCKS ports 1–64535')
        if not i.get('settings',{}).get('udp') or i.get('settings',{}).get('auth','noauth')!='noauth':
            raise ValueError('Xray bridge requires SOCKS noauth with UDP enabled')
        rules=[r for r in c.get('routing',{}).get('rules',[]) if i['tag'] in r.get('inboundTag',[])]
        if not rules or any(k in rules[0] for k in ('domain','ip','port','network','source','protocol','balancerTag')):
            raise ValueError('Each SOCKS listener requires an unconditional inbound → outbound rule')
        tag=rules[0].get('outboundTag');o=outbound.get(tag,{})
        servers=o.get('settings',{}).get('vnext',[])
        if o.get('protocol')!='vless' or len(servers)!=1:raise ValueError('Expected one VLESS server per SOCKS port')
        stream=o.get('streamSettings',{});r=stream.get('realitySettings',{})
        result.append({'name':proxies.get(i['port']) or names.get(i['tag']) or i['tag'],
            'port':i['port'],'inbound':i['tag'],'outbound':tag,'server':servers[0]['address'],
            'server_port':servers[0]['port'],'network':stream.get('network','tcp'),
            'fingerprint':r.get('fingerprint',stream.get('tlsSettings',{}).get('fingerprint')),
            'in_mihomo':i['port'] in proxies})
    if not result:raise ValueError('No Xray SOCKS bridge listeners found')
    return result
