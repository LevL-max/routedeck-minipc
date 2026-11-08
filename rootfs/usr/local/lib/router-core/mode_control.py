"""One traffic-mode controller for every mini-PC (router-core-v2).

Host facts come from /etc/router/core.env. On a router-wan host (Ethernet/LTE
failover) its firewall and NAT helpers run with every guard, as before.

DIRECT/AWG/TUN/MIHOMO selection is sticky: a failed start keeps the selected
mode fail-closed for Restart/Rotate. Every outcome is written to
mode-action.json for the panel.
# openflux-removed-v1: the OPENFLUX mode was removed on 2026-10-16.
"""
import contextlib, fcntl, json, os, re, signal, subprocess, sys, time, uuid
from pathlib import Path

MODE=Path('/etc/router/mode')
STATE=Path('/etc/router/mode-action.json')
PROFILE=Path('/etc/router/core.env')
MODES={'direct':None,'awg':(1,100),'tun':(2,110),'mihomo':(3,120)}
RESTORE='/usr/local/sbin/router-restore-runtime'
APPLY='/usr/local/sbin/router-apply-mode'
# The same lock set on every host; a lock file nobody else uses costs nothing.
LOCKS=('/run/router-updater.lock','/run/lock/router-mode.lock','/run/router-wan.lock','/run/watchdog-rotator/lock')

def profile():
    values={}
    try:lines=PROFILE.read_text().splitlines()
    except OSError:return values
    for line in lines:
        line=line.split('#',1)[0].strip()
        if '=' in line:
            key,value=line.split('=',1)
            values[key.strip()]=value.strip().strip('"\'')
    return values

WAN_STACK=profile().get('WAN_STACK','static')
net=None
STATES=[STATE]
if WAN_STACK=='router-wan':
    # router-wan owns the WAN guard chains and physical-WAN NAT on this host,
    # and its panel reads the mode state from the router-wan directory.
    sys.path.insert(0,'/usr/local/lib/router-wan')
    import wan_network as net
    STATES.append(Path('/var/lib/router-wan/mode-action.json'))

def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
    with tmp.open('w') as f:
        f.write(value if isinstance(value,str) else json.dumps(value));f.flush();os.fsync(f.fileno())
    tmp.chmod(0o644);os.replace(tmp,path)

def run(args,timeout=20,check=True):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if check and p.returncode:
        detail=((p.stdout or '')+'\n'+(p.stderr or '')).strip()
        raise RuntimeError(detail[-2000:] or Path(args[0]).name+' failed (exit '+str(p.returncode)+')')
    return p

def guard(mode):
    """Fail-closed policy for mode: unreachable default under its table, its
    rule and the LAN mark. Takes no lock; callers already hold it."""
    if mode not in MODES:raise ValueError('Unknown mode')
    if net:net.firewall()
    if MODES[mode]:
        mark,table=MODES[mode]
        run(['ip','route','replace','unreachable','default','table',str(table),'metric','32767'])
        # The routing layer reserves these priorities and marks.
        rules=json.loads(run(['ip','-j','rule']).stdout)
        matches=[r for r in rules if r.get('priority')==table]
        if not matches:run(['ip','rule','add','fwmark',hex(mark),'lookup',str(table),'priority',str(table)])
        elif any(str(r.get('fwmark')) not in (str(mark),hex(mark)) or str(r.get('table')) not in (str(table),{100:'awg',110:'tun',120:'mihomo'}[table]) for r in matches):
            raise RuntimeError('Managed policy priority is occupied: '+str(table))
    run([APPLY],timeout=20)
    if net:net.direct_nat()

@contextlib.contextmanager
def operation_lock():
    # The panel and the updater hold the updater lock in their parent process.
    skip=os.environ.get('ROUTER_UPDATER_LOCK_HELD')=='1'
    with contextlib.ExitStack() as stack:
        for path in LOCKS:
            if skip and path=='/run/router-updater.lock':continue
            Path(path).parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);stack.callback(os.close,fd)
            deadline=time.monotonic()+15
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise RuntimeError('Another router operation is running; retry shortly')
                    time.sleep(.2)
        yield

def restore(mode):
    env={**os.environ,'ROUTER_MODE_LOCK_HELD':'1','ROUTER_MODE_OVERRIDE':mode}
    budget=100
    p=subprocess.Popen([RESTORE],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env,start_new_session=True)
    try:
        output,_=p.communicate(timeout=budget)
    except BaseException:
        os.killpg(p.pid,signal.SIGTERM)
        try:p.communicate(timeout=5)
        except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.communicate()
        raise
    if output:print(output[-6000:],flush=True)
    if p.returncode:raise RuntimeError((output or '').strip()[-2000:]+'\nStartup failed (exit '+str(p.returncode)+')')

def explain(exc):
    """The most useful single line of a failure for the panel."""
    text=str(exc)
    lines=[l.strip() for l in text.splitlines() if l.strip()]
    for pattern in (r'did not pass end-to-end HTTPS',r'client failed to start',r'^FAIL:',r'MARKED_\w+_FAIL',r'REFUSED'):
        for line in lines:
            if re.search(pattern,line):return line[:300]
    for line in reversed(lines):
        if not line.startswith(('Traceback','File ')) and 'remains selected' not in line:return line[:300]
    return type(exc).__name__

def state(mode,status,message):
    value={'mode':mode,'status':status,'at':int(time.time()),'message':message[:1500]}
    for path in [STATE]+STATES[1:]:atomic(path,value)

def select_plain(mode,restart=False):
    """Sticky selection: on failure the selected mode stays guarded."""
    atomic(MODE,mode+'\n')
    state(mode,'connecting','Starting '+mode.upper())
    try:
        guard(mode)
        if restart and mode=='mihomo':run(['systemctl','stop','mihomo'],timeout=30)
        elif restart:raise ValueError('Use the existing AWG/TUN Restart or Rotate controls')
        restore(mode)
        state(mode,'started',mode.upper()+' started')
        print('LAN mode: '+mode+'; no automatic mode rollback',flush=True)
    except BaseException as exc:
        try:guard(mode)
        except Exception:pass
        message=mode.upper()+' remains selected. Use Restart/Rotate or select another mode. '+explain(exc)
        state(mode,'failed',message)
        raise RuntimeError(message) from exc

def select(mode,restart=False):
    if mode not in MODES:raise ValueError('Unknown traffic mode')
    current=MODE.read_text().strip() if MODE.exists() else 'direct'
    if restart and current!=mode:raise RuntimeError('Restart requires the selected mode')
    return select_plain(mode,restart)

def main():
    args=sys.argv[1:]
    if len(args)==2 and args[0]=='guard':return guard(args[1])
    restart=len(args)==2 and args[0]=='restart'
    mode=args[-1] if args else ''
    if mode not in MODES or len(args)!=(2 if restart else 1):raise ValueError('Usage: router-mode-control [restart] direct|awg|tun|mihomo | guard MODE')
    def interrupted(signum,frame):raise RuntimeError('Startup interrupted')
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,interrupted)
    with operation_lock():select(mode,restart)

if __name__=='__main__':
    try:main()
    except Exception as exc:raise SystemExit(str(exc))
