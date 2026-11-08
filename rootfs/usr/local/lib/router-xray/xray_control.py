#!/usr/bin/env python3
"""Mini-PC 2 Xray control. Root CLI; unprivileged panel uses JSON on stdin."""
import contextlib
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0,'/usr/local/lib/router-updater')
from probes import probe_all
from router_transport import Transport
from mapping import live_nodes

ROOT=Path('/var/lib/router-xray')
VERSIONS=ROOT/'versions'
CONFIG=Path('/etc/xray/config.json')
NODES=Path('/etc/xray/nodes.json')
CURRENT=Path('/usr/local/bin/xray')
STATE=ROOT/'state.json'
TRANSACTION=ROOT/'transaction.json'
SCRIPT='/usr/local/sbin/router-xray-control'
UNIT='router-xray-worker.service'
SERVICE='xray.service'
TAG=re.compile(r'^v[0-9]{1,3}\.[0-9]{1,2}\.[0-9]{1,2}$')
ROUTES={'current','direct'}


def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return default


def atomic(path,data,mode=0o600):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    with temp.open('w') as f:
        json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    temp.chmod(mode);os.replace(temp,path)


def run(args,timeout=30,check=True):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if check and p.returncode:
        raise RuntimeError((p.stderr or p.stdout or 'Command failed')[-1800:].strip())
    return p


def active(unit):
    return run(['systemctl','is-active','--quiet',unit],check=False).returncode==0


def running(unit):
    return run(['systemctl','show','-p','ActiveState','--value',unit],check=False).stdout.strip() in ('active','activating','deactivating')


@contextlib.contextmanager
def lock(path,wait=0):
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        deadline=time.monotonic()+wait
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('Another router operation is running')
                time.sleep(.1)
        yield
    finally:os.close(fd)


def tag_ok(tag):
    if not isinstance(tag,str) or not TAG.fullmatch(tag):raise ValueError('Invalid official version tag')
    return tag


def current_tag():
    if CURRENT.is_symlink():
        resolved=CURRENT.resolve()
        if resolved.parent.parent==VERSIONS:return resolved.parent.name
    return None


def status():
    state=read(STATE,{})
    installed=current_tag()
    saved=[]
    if VERSIONS.exists():
        for path in sorted(VERSIONS.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
            if path.is_dir() and TAG.fullmatch(path.name):
                meta=read(path/'verified.json',{})
                saved.append({'tag':path.name,'active':path.name==installed,
                              'prerelease':meta.get('prerelease'),'downloaded_at':meta.get('downloaded_at')})
    nodes=live_nodes()
    is_active=active(SERVICE)
    listening=[]
    if is_active:
        for node in nodes:
            try:
                with socket.create_connection(('127.0.0.1',node['port']),timeout=.15):pass
                listening.append(node['name'])
            except OSError:pass
    busy=running(UNIT)
    if not busy and state.get('status') in ('queued','running'):
        state={**state,'status':'interrupted','message':'Worker interrupted; check recovery log'}
    return {'active':is_active,'healthy':is_active and len(listening)==len(nodes) and bool(nodes),
            'mode':Path('/etc/router/mode').read_text().strip(),
            'enabled':run(['systemctl','is-enabled',SERVICE],check=False).stdout.strip()=='enabled',
            'version':installed,'retained':saved,'retention':3,'listening':len(listening),
            'nodes':nodes,'busy':busy,'job':state,'releases':read(ROOT/'releases.json',[]),
            'rollback':read(ROOT/'previous.json',{}).get('tag'),
            'pending_recovery':TRANSACTION.exists()}


def curl(url,transport,destination=None,timeout=180):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or parsed.username or parsed.password:
        raise ValueError('HTTPS URL required')
    transport.validate_route()
    args=['curl','--proxy',transport.url,'--noproxy','','--fail','--location','--silent',
          '--show-error','--connect-timeout','12','--max-time',str(timeout),
          '--proto','=https','--proto-redir','=https','-H','User-Agent: MiniPC2-Xray-Updater']
    if destination:args+=['--output',str(destination)]
    p=run(args+[url],timeout=timeout+5)
    transport.validate_route()
    return p.stdout


def releases(route):
    with Transport(route) as transport:
        data=json.loads(curl('https://api.github.com/repos/XTLS/Xray-core/releases?per_page=60',transport))
    result=[{'tag':r['tag_name'],'prerelease':r['prerelease'],'published_at':r.get('published_at')}
            for r in data if not r.get('draft') and TAG.fullmatch(r['tag_name'])
            and any(a['name']=='Xray-linux-64.zip' for a in r.get('assets',[]))]
    atomic(ROOT/'releases.json',result)
    return {'message':f'{len(result)} official releases available','route':route}


def validate_binary(path):
    p=run([str(path),'run','-test','-config',str(CONFIG)],timeout=30)
    if 'Configuration OK' not in p.stdout+p.stderr:raise RuntimeError('Xray configuration validation was not confirmed')


def retain():
    protected={current_tag(),read(ROOT/'previous.json',{}).get('tag')}
    paths=sorted((p for p in VERSIONS.iterdir() if p.is_dir() and TAG.fullmatch(p.name)),
                 key=lambda p:p.stat().st_mtime,reverse=True)
    keep={p.name for p in paths if p.name in protected}
    for p in paths:
        if len(keep)<3:keep.add(p.name)
    for p in paths:
        if p.name not in keep:
            if p.resolve().parent!=VERSIONS.resolve() or p.is_symlink():raise RuntimeError('Unsafe version path')
            shutil.rmtree(p)


def download(tag,route):
    tag_ok(tag)
    path=VERSIONS/tag
    if path.exists():
        verified_binary(tag)
        return {'message':'Version already downloaded and verified','tag':tag}
    with tempfile.TemporaryDirectory(prefix='download-',dir=ROOT) as temp:
        temp=Path(temp)
        with Transport(route) as transport:
            data=json.loads(curl('https://api.github.com/repos/XTLS/Xray-core/releases/tags/'+tag,transport))
            if data.get('tag_name')!=tag or data.get('draft'):raise RuntimeError('Release identity mismatch')
            asset=next(a for a in data['assets'] if a['name']=='Xray-linux-64.zip')
            expected=asset.get('digest','')
            if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}',expected):raise RuntimeError('Official SHA256 digest missing')
            url=asset['browser_download_url']
            if not url.startswith('https://github.com/XTLS/Xray-core/releases/download/'+tag+'/'):
                raise RuntimeError('Unexpected release asset URL')
            package=temp/'xray.zip'
            curl(url,transport,package,timeout=300)
        digest=hashlib.sha256(package.read_bytes()).hexdigest()
        if digest!=expected[7:].lower():raise RuntimeError('Archive SHA256 mismatch')
        with zipfile.ZipFile(package) as z:
            info=z.getinfo('xray')
            if not 1000000<info.file_size<200000000:raise RuntimeError('Unexpected Xray executable size')
            binary=temp/'xray';binary.write_bytes(z.read(info));binary.chmod(0o755)
        version=run([str(binary),'version']).stdout.splitlines()[0]
        if not version.startswith('Xray '+tag[1:]+' '):raise RuntimeError('Executable version mismatch')
        validate_binary(binary)
        meta={'tag':tag,'prerelease':data['prerelease'],'asset_sha256':digest,
              'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
              'downloaded_at':int(time.time()),'version':version}
        prepared=temp/'version';prepared.mkdir();prepared.chmod(0o755)
        shutil.move(str(binary),prepared/'xray');atomic(prepared/'verified.json',meta)
        os.replace(prepared,path)
    retain()
    return {'message':'Downloaded, SHA256 verified and current config validated','tag':tag,'route':route}


def verified_binary(tag):
    path=VERSIONS/tag_ok(tag)
    meta=read(path/'verified.json',{})
    binary=path/'xray'
    if path.is_symlink() or binary.is_symlink() or path.resolve().parent!=VERSIONS.resolve():
        raise RuntimeError('Invalid version directory')
    if hashlib.sha256(binary.read_bytes()).hexdigest()!=meta.get('binary_sha256'):
        raise RuntimeError('Stored executable checksum mismatch')
    validate_binary(binary)
    return binary


def switch(tag):
    binary=verified_binary(tag)
    temp=CURRENT.with_name('xray.next')
    temp.unlink(missing_ok=True)
    temp.symlink_to(binary)
    os.replace(temp,CURRENT)


def passing(results):
    return {r['name'] for r in results if r.get('tcp') and r.get('udp')}


def wait_service():
    for _ in range(20):
        if active(SERVICE):
            nodes=live_nodes()
            try:
                for node in nodes:
                    with socket.create_connection(('127.0.0.1',node['port']),timeout=.3):pass
                return
            except OSError:pass
        time.sleep(.5)
    raise RuntimeError('Xray service/listeners did not become healthy')


def canary(binary,nodes,required):
    config=read(CONFIG)
    for inbound in config['inbounds']:inbound['port']+=1000
    with tempfile.TemporaryDirectory(prefix='canary-',dir=ROOT) as tmp:
        file=Path(tmp)/'config.json';atomic(file,config)
        for inbound in config['inbounds']:
            with socket.socket() as sock:sock.bind(('127.0.0.1',inbound['port']))
        with (Path(tmp)/'log').open('w') as log:
            process=subprocess.Popen([str(binary),'run','-config',str(file)],stdout=log,stderr=log)
            try:
                time.sleep(1)
                if process.poll() is not None:raise RuntimeError('Canary core failed to start')
                results=probe_all(nodes,1000)
                missing=required-passing(results)
                if missing:raise RuntimeError('Candidate failed TCP/UDP canary: '+', '.join(sorted(missing)))
            finally:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
    return results


def recover(token,stop_worker=True):
    tx=read(TRANSACTION)
    if not tx or tx.get('token')!=token:return {'message':'Nothing pending'}
    if stop_worker:run(['systemctl','stop',UNIT],timeout=20,check=False)
    switch(tx['previous'])
    run(['systemctl','restart',SERVICE],timeout=30)
    wait_service()
    # A failed recovery leaves the transaction for the persistent timer to retry.
    if tx.get('required'):
        nodes=[n for n in live_nodes() if n['name'] in tx['required']]
        if set(tx['required'])-passing(probe_all(nodes)):
            raise RuntimeError('Restored core; some previously healthy nodes remain unavailable')
    TRANSACTION.unlink(missing_ok=True)
    atomic(STATE,{'status':'rolled_back','message':'Previous Xray core restored by local recovery',
                  'finished_at':int(time.time()),'tag':tx['previous']})
    return {'message':'Previous core restored','tag':tx['previous']}


def install(tag):
    binary=verified_binary(tag)
    old=current_tag()
    if old==tag:return {'message':'Selected core is already active','tag':tag}
    if not old or not active(SERVICE):raise RuntimeError('Start the current Xray core before changing versions')
    nodes=live_nodes()
    required=passing(probe_all(nodes))
    if not required:raise RuntimeError('No healthy baseline node; live replacement refused')
    canary(binary,nodes,required)
    token=uuid.uuid4().hex
    atomic(TRANSACTION,{'token':token,'previous':old,'candidate':tag,'required':sorted(required),
                        'deadline':int(time.time())+150})
    # The persistent 15s timer reads this deadline, including after worker crash
    # or reboot. It does not depend on SSH, browser, or the updater process.
    try:
        switch(tag)
        run(['systemctl','restart',SERVICE],timeout=30);wait_service()
        results=probe_all(nodes)
        if required-passing(results):raise RuntimeError('Post-switch TCP/UDP health regression')
        atomic(ROOT/'previous.json',{'tag':old})
        TRANSACTION.unlink()
        retain()
        return {'message':'Core changed; TCP/UDP checks passed','tag':tag,'previous':old,'checks':results}
    except BaseException:
        recover(token,stop_worker=False)
        raise


def worker(job_id):
    job=read(ROOT/'job.json')
    if not job or job['id']!=job_id:raise RuntimeError('Stale job')
    try:
        with lock('/run/router-updater.lock',10),lock('/run/lock/router-mode.lock',10):
            atomic(STATE,{**job,'status':'running','started_at':int(time.time())})
            op=job['op']
            if op=='check':result=releases(job['route'])
            elif op=='download':result=download(job['tag'],job['route'])
            elif op=='install':result=install(job['tag'])
            elif op=='rollback':result=install(read(ROOT/'previous.json',{})['tag'])
            elif op in ('start','stop','restart'):
                run(['systemctl',op,SERVICE],timeout=30)
                if op!='stop':wait_service()
                result={'message':'Xray '+op+' completed'}
            else:raise ValueError('Unsupported operation')
            atomic(STATE,{**job,**result,'status':'complete','finished_at':int(time.time())})
    except BaseException as exc:
        atomic(STATE,{**job,'status':'failed','message':str(exc)[-1800:],'finished_at':int(time.time())})
        raise


def enqueue(request):
    op=request.get('op')
    if op not in {'check','download','install','rollback','start','stop','restart'}:raise ValueError('Unknown operation')
    route=request.get('route','current')
    if route not in ROUTES:raise ValueError('Unknown route')
    tag=request.get('tag')
    if op in ('download','install'):tag_ok(tag)
    else:tag=None
    if op in ('install','rollback','stop','restart') and request.get('confirm') is not True:
        raise ValueError('Confirmation required')
    if TRANSACTION.exists():raise RuntimeError('Core recovery is pending')
    with lock('/run/lock/router-xray-launch.lock'),lock('/run/router-updater.lock'):
        from panel_updates import pending_service
        from panel_system import reboot_status
        if running(UNIT) or pending_service() or reboot_status()['pending']:raise RuntimeError('Another router operation is running')
        job={'id':uuid.uuid4().hex,'op':op,'tag':tag,'route':route,'created_at':int(time.time())}
        atomic(ROOT/'job.json',job);atomic(STATE,{**job,'status':'queued'})
        run(['systemctl','reset-failed',UNIT],check=False)
        run(['systemd-run','--unit=router-xray-worker','--collect','--property=UMask=0077',
             '--property=RuntimeMaxSec=1200',SCRIPT,'--worker',job['id']])
    return {'ok':True,'job':job}


def main():
    if os.geteuid()!=0:raise RuntimeError('Root required')
    os.umask(0o077);ROOT.mkdir(exist_ok=True);VERSIONS.mkdir(exist_ok=True)
    if len(sys.argv)==3 and sys.argv[1]=='--worker':return worker(sys.argv[2])
    if sys.argv[1:]==['--recover']:
        tx=read(TRANSACTION)
        if tx and time.time()>=tx['deadline']:recover(tx['token'])
        return
    if len(sys.argv)!=1:raise ValueError('Invalid arguments')
    request=json.loads(sys.stdin.read(8193))
    if not isinstance(request,dict):raise ValueError('Object required')
    op=request.get('op')
    if op=='status':result=status()
    else:result=enqueue(request)
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)}));raise SystemExit(1)
