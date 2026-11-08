"""TCP and SOCKS5 UDP end-to-end tests, also used for core update canaries."""
import concurrent.futures
import json
import os
import socket
import struct
import subprocess
import time
from pathlib import Path


def exact(s,n):
    data=b''
    while len(data)<n:
        part=s.recv(n-len(data))
        if not part: raise OSError('Short SOCKS reply')
        data+=part
    return data


def udp_dns(port):
    ident=os.urandom(2)
    query=ident+b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'+b'\x07example\x03com\x00\x00\x01\x00\x01'
    with socket.create_connection(('127.0.0.1',port),timeout=10) as control:
        control.sendall(b'\x05\x01\x00')
        if exact(control,2)!=b'\x05\x00':raise OSError('SOCKS auth failed')
        control.sendall(b'\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00')
        head=exact(control,4)
        if head[:3]!=b'\x05\x00\x00':raise OSError('UDP associate failed')
        if head[3]==1: host=socket.inet_ntoa(exact(control,4))
        elif head[3]==3:host=exact(control,exact(control,1)[0]).decode()
        else:raise OSError('Unexpected UDP relay family')
        relayport=struct.unpack('!H',exact(control,2))[0]
        if host=='0.0.0.0':host='127.0.0.1'
        if host!='127.0.0.1':raise OSError('UDP relay is not loopback')
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
            udp.settimeout(12)
            udp.sendto(b'\x00\x00\x00\x01'+socket.inet_aton('1.1.1.1')+b'\x00\x35'+query,(host,relayport))
            reply,_=udp.recvfrom(4096)
            if reply[:4]!=b'\x00\x00\x00\x01':raise OSError('Invalid SOCKS UDP response')
            payload=reply[10:]
            if len(payload)<12 or payload[:2]!=ident or not payload[2]&0x80 or payload[3]&15 or payload[6:8]==b'\0\0':
                raise OSError('Unsuccessful tunneled DNS response')
    return True


def probe_node(node,offset=0,udp=True):
    port=node['port']+offset
    start=time.monotonic()
    result={'name':node['name'],'port':port}
    p=subprocess.run(['curl','--proxy',f'socks5h://127.0.0.1:{port}','--noproxy','',
        '-fsS','--connect-timeout','8','--max-time','18','-o','/dev/null','-w','%{http_code}',
        'https://www.gstatic.com/generate_204'],capture_output=True,text=True,timeout=22)
    result['tcp']=p.returncode==0 and p.stdout=='204'
    result['ms']=round((time.monotonic()-start)*1000)
    if not result['tcp']:result['tcp_error']=p.stderr.strip()[:200]
    if udp:
        try:result['udp']=udp_dns(port)
        except Exception as exc:result['udp']=False;result['udp_error']=str(exc)
    return result


def probe_all(nodes,offset=0,udp=True):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda n:probe_node(n,offset,udp),nodes))


if __name__=='__main__':
    import sys
    nodes=json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(probe_all(nodes),indent=2),flush=True)
