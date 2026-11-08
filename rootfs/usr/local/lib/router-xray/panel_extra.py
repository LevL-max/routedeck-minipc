import json,os,threading,time
from pathlib import Path
import config_store,xray_control
import panel_updates
_cpu_lock=threading.Lock();_cpu_last=None;_cpu_value=None
def cpu_temperature():
    values=[]
    for directory in Path('/sys/class/hwmon').glob('hwmon*'):
        try:
            name=(directory/'name').read_text().strip()
            if name not in ('coretemp','k10temp','zenpower'):continue
            for file in directory.glob('temp*_input'):
                label_file=file.with_name(file.name.replace('_input','_label'))
                label=label_file.read_text().strip() if label_file.exists() else name
                value=int(file.read_text().strip())/1000
                if not -20<value<150:continue
                priority=0 if label=='Tdie' or label.startswith('Package') else 1 if label=='Tctl' else 2
                values.append((priority,value,name+' / '+label))
        except (OSError,ValueError):continue
    if not values:return None,None
    priority=min(v[0] for v in values)
    _,temperature,sensor=max((v for v in values if v[0]==priority),key=lambda v:v[1])
    return round(temperature,1),sensor
def resources():
    global _cpu_last,_cpu_value
    values=[int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    now=(sum(values),values[3]+values[4],time.monotonic())
    with _cpu_lock:
        if _cpu_last and now[2]-_cpu_last[2]>=.5:
            delta=now[0]-_cpu_last[0]
            if delta>0:_cpu_value=round(max(0,min(100,100*(1-(now[1]-_cpu_last[1])/delta))),1)
            _cpu_last=now
        elif _cpu_last is None:_cpu_last=now
        cpu=_cpu_value
    mem={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
    total=mem['MemTotal'];used=total-mem['MemAvailable']
    temperature,sensor=cpu_temperature()
    return {'ok':True,'cpu':cpu,'cores':os.cpu_count(),'load':os.getloadavg(),
            'ram_used':used,'ram_total':total,'ram_percent':round(100*used/total,1),
            'cpu_temp_c':temperature,'cpu_temp_sensor':sensor,
            'uptime':int(float(Path('/proc/uptime').read_text().split()[0]))}
def get(h):
    path=h.path
    if path not in ('/api/xray','/api/configs','/api/resources') and not path.startswith('/api/configs/download/'):return False
    try:
        if path=='/api/xray':h.j(xray_control.status())
        elif path=='/api/resources':h.j(resources())
        elif path=='/api/configs':h.j(config_store.status())
        else:
            item,data=config_store.download(path.rsplit('/',1)[-1])
            h.send_response(200);h.send_header('Content-Type','application/octet-stream')
            h.send_header('Cache-Control','no-store');h.send_header('X-Content-Type-Options','nosniff')
            h.send_header('Content-Disposition','attachment; filename="'+item['file']+'"')
            h.send_header('Content-Length',str(len(data)));h.end_headers();h.wfile.write(data)
    except Exception as exc:h.j({'ok':False,'error':str(exc)[-1200:]},409)
    return True
def post(h):
    if h.path not in ('/api/xray','/api/configs'):return False
    if not h.allowed() or not h.authorized():h.j({'ok':False,'error':'Request rejected'},403);return True
    try:
        size=int(h.headers.get('Content-Length','0'))
        if not 0<size<=2*1024*1024:raise ValueError('Request too large')
        b=json.loads(h.rfile.read(size))
        if not isinstance(b,dict):raise ValueError('JSON object required')
        if h.path=='/api/xray':result=xray_control.enqueue(b)
        else:
            with panel_updates.mode_guard():result=config_store.upload(b)
        h.j(result)
    except Exception as exc:h.j({'ok':False,'error':str(exc)[-1200:]},409)
    return True
