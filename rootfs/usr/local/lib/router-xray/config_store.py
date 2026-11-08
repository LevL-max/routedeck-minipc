"""Individual configuration files, atomic replacement, native validation; no live reload."""
import base64,hashlib,json,os,pwd,grp,re,shlex,shutil,stat,subprocess,tempfile,time,uuid,ipaddress
from pathlib import Path
from urllib.parse import urlsplit
import yaml
ROOT=Path('/var/lib/router-configs');BACKUPS=Path('/var/backups/router-configs')
MAX_FILE=1024*1024
def registry():
    r={
      'awg-1':('/etc/amnezia/amneziawg/variants/temp_1.conf','AWG · profile 1','awg'),
      'awg-2':('/etc/amnezia/amneziawg/variants/temp_2.conf','AWG · profile 2','awg'),
      'mihomo':('/etc/mihomo/config.yaml','Mihomo','mihomo'),
      'xray':('/etc/xray/config.json','Xray · connections','xray'),
      'nodes':('/etc/xray/nodes.json','Xray · name metadata','nodes')}
    for p in sorted(Path('/etc/sing-box/configs').glob('tun_*.json')):
        if re.fullmatch(r'tun_[A-Za-z0-9_-]+\.json',p.name):r['tun-'+p.stem]=(str(p),'TUN · '+p.stem,'tun')
    return {k:{'path':v[0],'label':v[1],'kind':v[2],'file':Path(v[0]).name} for k,v in r.items()}
def contents(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_size>MAX_FILE:raise ValueError('Expected a regular config file, at most 1 MiB')
        return f.read(MAX_FILE+1)
def digest(data):return hashlib.sha256(data).hexdigest()
def run(args,timeout=40):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise ValueError('Configuration check failed: '+(p.stderr or p.stdout)[-1200:])
    return p.stdout
def parse(data):
    text=data.decode('utf-8-sig')
    if not text.strip() or '\0' in text:raise ValueError('Empty or binary config')
    return text
def env_values(text):
    result={}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        m=re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)',line.strip())
        if not m or not re.fullmatch(r'''(?:[A-Za-z0-9_./:@%?=,+~-]+|'[^'\r\n]*'|"[^"$`\\\r\n]*")''',m[2]):raise ValueError('Use plain KEY=value settings; shell commands are not allowed')
        parts=shlex.split(m[2],comments=True)
        if len(parts)!=1 or m[1] in result:raise ValueError('Invalid/duplicate setting')
        result[m[1]]=parts[0]
    return result
def validate(kind,data,home):
    text=parse(data);file=home/'config.json';file.write_bytes(data)
    if kind=='awg':
        if re.search(r'(?im)^\s*(PreUp|PostUp|PreDown|PostDown|SaveConfig)\s*=',text):raise ValueError('AWG shell hooks and SaveConfig are not supported')
        if not re.search(r'(?im)^\s*Table\s*=\s*off\s*(?:#.*)?$',text):raise ValueError('Keep Table = off; routing is managed by this router')
        addresses=re.findall(r'(?im)^\s*Address\s*=\s*([^#\r\n]+)',text)
        if len(addresses)!=1 or ',' in addresses[0] or ipaddress.ip_interface(addresses[0].strip()).version!=4:raise ValueError('AWG requires one IPv4 interface address')
        for value in re.findall(r'(?im)^\s*MTU\s*=\s*(\d+)',text):
            if not 1280<=int(value)<=9000:raise ValueError('Invalid AWG MTU')
        file=home/'wg0.conf';file.write_bytes(data);file.chmod(0o600)
        stripped=run(['/usr/bin/awg-quick','strip',str(file)])
        stripped=re.sub(r'(?im)^\s*Endpoint\s*=.*$','Endpoint = 127.0.0.1:9',stripped)
        test=home/'test.conf';test.write_text(stripped);test.chmod(0o600)
        run(['unshare','--net','--','python3',__file__,'--awg',str(test)])
    elif kind=='tun':
        c=json.loads(text);device='tun0'
        inbound=[i for i in c.get('inbounds',[]) if i.get('type')=='tun']
        if len(inbound)!=1 or inbound[0].get('interface_name')!=device or inbound[0].get('auto_route',False):raise ValueError('Keep '+device+' with auto_route=false; router owns routing')
        run(['/usr/bin/sing-box','check','-c',str(file)])
    elif kind=='mihomo':
        c=yaml.safe_load(text)
        if not isinstance(c,dict):raise ValueError('Mihomo YAML object required')
        tun=c.get('tun',{})
        if not tun.get('enable') or tun.get('device')!='mihomo0' or tun.get('auto-route') or tun.get('auto-redirect'):raise ValueError('Keep mihomo0 TUN enabled with auto-route/auto-redirect=false')
        if c.get('dns',{}).get('listen')!='127.0.0.1:1053' or c.get('external-controller')!='192.168.50.1:9090':raise ValueError('Keep existing DNS and controller addresses')
        for section in ('rule-providers','proxy-providers'):
            for provider in c.get(section,{}).values():
                value=provider.get('path')
                if value:
                    p=Path(value)
                    if p.is_absolute() or '..' in p.parts:raise ValueError('Provider paths must be relative to /etc/mihomo')
                    src=Path('/etc/mihomo')/p
                    if src.is_file():
                        dst=home/p;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
        run(['/usr/local/bin/mihomo','-t','-d',str(home),'-f',str(file)])
    elif kind=='xray':
        c=json.loads(text)
        for i in c.get('inbounds',[]):
            if i.get('protocol')!='socks' or i.get('listen')!='127.0.0.1':raise ValueError('Xray bridge listeners must remain SOCKS on 127.0.0.1')
        run(['/usr/local/bin/xray','run','-test','-config',str(file)])
        import mapping
        mapping.live_nodes(file)
    elif kind=='nodes':
        data=json.loads(text)
        if not isinstance(data,list) or any(not isinstance(n,dict) or not isinstance(n.get('name'),str) or not isinstance(n.get('inbound'),str) for n in data):raise ValueError('nodes.json requires an array of name/inbound metadata')
    else:raise ValueError('Unknown config type')
def status():
    entries=registry()
    for key,item in entries.items():item['revision']=digest(contents(item['path']))
    active={}
    for mode,path in [('awg','/run/watchdog-rotator/active'),('tun','/var/lib/router/tun-rotator/active')]:
        try:active[mode]=Path(path).read_text().strip()
        except FileNotFoundError:active[mode]='unknown'
    return {'ok':True,'configs':entries,'active_profiles':active,'last_upload':json.loads((ROOT/'last-upload.json').read_text()) if (ROOT/'last-upload.json').exists() else None}
def download(target):
    item=registry()[target]
    return item,contents(item['path'])
def upload(b):
    if b.get('confirm') is not True:raise ValueError('Replacement confirmation required')
    item=registry().get(b.get('target'))
    if not item:raise ValueError('Unknown config')
    data=base64.b64decode(b.get('data',''),validate=True)
    if not 0<len(data)<=MAX_FILE:raise ValueError('File must be at most 1 MiB')
    path=Path(item['path']);original=contents(path)
    if b.get('revision')!=digest(original):raise ValueError('Config changed since loaded; refresh and retry')
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT,prefix='check-') as tmp:validate(item['kind'],data,Path(tmp))
    if contents(path)!=original:raise ValueError('Config changed during validation; retry')
    if data==original:return {'ok':True,'message':'File is already identical; no restart needed.','changed':False}
    backup=BACKUPS/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]);backup.mkdir(mode=0o700,parents=True)
    (backup/path.name).write_bytes(original);(backup/'path.txt').write_text(str(path))
    info=path.stat();fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno());os.fchown(f.fileno(),info.st_uid,info.st_gid);os.fchmod(f.fileno(),stat.S_IMODE(info.st_mode))
        os.replace(tmp,path)
        fd=os.open(path.parent,os.O_DIRECTORY);os.fsync(fd);os.close(fd)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    msg='Saved. Restart the corresponding active mode to apply. Services have not been restarted.'
    if item['kind']=='nodes':msg='Metadata saved. Mapping reads live Mihomo/Xray configs automatically; metadata alone does not change connections.'
    result={'ok':True,'changed':True,'target':b['target'],'message':msg,'backup':str(backup),'saved_at':int(time.time())}
    from xray_control import atomic
    atomic(ROOT/'last-upload.json',result)
    return result
if __name__=='__main__':
    import sys
    if len(sys.argv)==3 and sys.argv[1]=='--awg':
        run(['ip','link','add','awgtest','type','amneziawg'])
        run(['/usr/bin/awg','setconf','awgtest',sys.argv[2]])
