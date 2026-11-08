#!/usr/bin/env python3
# RouteDeck panel; OpenFlux removed 2026-10-16 (openflux-removed-v1)
import json
import sys
sys.path.insert(0, "/usr/local/lib/router-updater")
import panel_updates as updater_ui
import panel_system
sys.path.insert(0,"/usr/local/lib/router-xray")
import panel_extra
sys.path.insert(0,"/usr/local/lib/router-wan")
import panel_wan
import wan_speedtest
sys.path.insert(0,"/usr/local/lib/router-cpu-boost")
import cpu_boost
from wan_common import active_interface, ROOT, read
import os
import re
import socket
import signal
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from ipaddress import ip_address, ip_network
from pathlib import Path

LAN_IP = "192.168.50.1"
import wan_common
LAN_IF = wan_common.LAN_IF
WAN_IF = wan_common.ETH  # host profile /etc/router/core.env
LAN_NET = ip_network("192.168.50.0/24")
PORT = 8080
# routedeck-access-v1: host label and trusted networks from /etc/router/panel.json
PANEL_CONFIG = Path("/etc/router/panel.json")
def load_panel_config():
    try:
        d=json.loads(PANEL_CONFIG.read_text(encoding="utf-8"))
        d=d if isinstance(d,dict) else {}
    except (OSError,ValueError):d={}
    label="".join(c for c in str(d.get("label") or "") if c.isprintable())[:24]
    color=str(d.get("color") or "")
    if not (len(color)==7 and color[0]=="#" and all(c in "0123456789abcdefABCDEF" for c in color[1:])):color="#2563eb"
    nets=[]
    for n in d.get("trusted_networks") or []:
        try:
            net=ip_network(str(n),strict=False)
            if net.version==4 and net.is_private:nets.append(net)
        except ValueError:pass
    return label,color,nets
PANEL_LABEL,PANEL_COLOR,TRUSTED_NETS=load_panel_config()
def index_page():
    if not PANEL_LABEL:return INDEX
    import html
    label=html.escape(PANEL_LABEL)
    page=INDEX.replace("<title>RouteDeck · Router Control</title>",f"<title>{label} · RouteDeck</title>",1)
    return page.replace("<h1>RouteDeck</h1>",f'<h1>RouteDeck <span style="display:inline-block;margin-left:8px;padding:1px 12px;border-radius:999px;background:{PANEL_COLOR};color:#fff;font-size:.75em;vertical-align:middle">{label}</span></h1>',1)

MODE_FILE = Path("/etc/router/mode")
MIHOMO_CFG = Path("/etc/mihomo/config.yaml")
COUNTER_FILE = Path("/var/lib/router-panel/counters.json")

COMMANDS = {
    "direct": ["/usr/local/sbin/net-direct"],
    "awg": ["/usr/local/sbin/net-awg"],
    "tun": ["/usr/local/sbin/net-tun"],
    "mihomo": ["/usr/local/sbin/net-mihomo"],
}

ROTATORS = {
    ("mihomo", "restart"): ["/usr/local/sbin/router-mode-control", "restart", "mihomo"],
    ("awg", "rotate"): ["/usr/local/sbin/watchdog-rotator", "rotate"],
    ("awg", "restart"): ["/usr/local/sbin/watchdog-rotator", "restart"],
    ("tun", "rotate"): ["/usr/local/sbin/tun-rotator", "rotate"],
    ("tun", "restart"): ["/usr/local/sbin/tun-rotator", "restart"],
}

ACTIVE_IFACE = {
    "direct": WAN_IF,
    "awg": "wg0",
    "tun": "tun0",
    "mihomo": "mihomo0",
}

MODE_MARK = {
    "direct": 0,
    "awg": 0x1,
    "tun": 0x2,
    "mihomo": 0x3,
}

# Diverse targets. Health is successful if ANY HTTPS target works.
# System DNS is used only to obtain the destination IP; the actual TCP/TLS
# socket is marked and therefore follows the active router policy path.
HTTPS_TARGETS = (
    ("www.microsoft.com", "/", 443),
    ("www.apple.com", "/", 443),
    ("www.yandex.ru", "/", 443),
    ("vk.com", "/", 443),
)

PUBLIC_DNS = ("77.88.8.8", "1.1.1.1", "8.8.8.8")

_stats_lock = threading.Lock()
_last_lan_stats = None

_health_lock = threading.Lock()
_health_trigger = threading.Event()
_health_stop = threading.Event()
_health = {
    "mode": "unknown",
    "level": "checking",
    "ok": False,
    "internet_ok": False,
    "dns_ok": None,
    "summary": "Waiting for first external check",
    "last_checked": None,
    "last_ok": None,
    "failed_since": None,
}

INDEX = r'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>RouteDeck · Router Control</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--card:#151b23;--line:#29313d;--text:#e6edf3;--muted:#8b949e;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--btn:#21262d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:980px;margin:auto;padding:18px}h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.span2{grid-column:1/-1}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.modegrid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:12px}
button,a.btn{border:1px solid var(--line);background:var(--btn);color:var(--text);border-radius:10px;padding:12px 14px;font-weight:650;text-decoration:none;text-align:center;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}button.active{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok) inset}
button.reboot-needed{border-color:var(--warn);color:#ffdc89;background:#3a2d12;box-shadow:0 0 0 1px var(--warn) inset}
.exitgrid{display:grid;grid-template-columns:1.1fr 1.5fr 1.2fr;gap:12px;margin-top:14px}.exitgrid .value{font-size:17px;overflow-wrap:anywhere}
dialog{color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:14px;max-width:440px;padding:22px}dialog::backdrop{background:#0009}
.kpi{font-size:30px;font-weight:750}.small{font-size:13px;color:var(--muted)}.status{display:inline-flex;align-items:center;gap:7px;font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad)}.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px}.value{font-size:20px;font-weight:700}
pre{white-space:pre-wrap;word-break:break-word;background:#0a0e13;border:1px solid var(--line);border-radius:10px;padding:10px;max-height:180px;overflow:auto}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#202936;border:1px solid var(--line);border-radius:10px;padding:10px 14px;display:none;max-width:90vw}
@media(max-width:700px){.grid{grid-template-columns:1fr}.span2{grid-column:auto}.modegrid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.exitgrid{grid-template-columns:1fr}.exitgrid>div{padding-top:4px}}
/* Subtle accents identify each section; text and status labels remain explicit. */
body{background:radial-gradient(ellipse at 50% 0,#152236 0,transparent 55%),var(--bg)}
h1::before{content:'';display:inline-block;width:10px;height:10px;border-radius:50%;background:#58b9ff;margin-right:11px;box-shadow:0 0 16px #58b9ff55;vertical-align:middle}
.card{box-shadow:0 7px 22px #0002}.grid{margin-top:12px}
button,a.btn{transition:background .15s,border-color .15s,box-shadow .15s}
button:hover:not(:disabled),a.btn:hover{background:#29384a;border-color:#60758e}
button:focus-visible,a.btn:focus-visible,select:focus-visible{outline:2px solid #7acaff;outline-offset:3px}
.modegrid button{--tone:157,173,194;color:rgb(var(--tone));background:rgba(var(--tone),.07);border-color:rgba(var(--tone),.3)}
.modegrid [data-mode="awg"]{--tone:86,211,151}.modegrid [data-mode="tun"]{--tone:91,183,255}
.modegrid [data-mode="mihomo"]{--tone:186,151,255}
.modegrid button.active{background:rgba(var(--tone),.19);border-color:rgb(var(--tone));box-shadow:0 0 0 1px rgba(var(--tone),.45) inset,0 0 18px rgba(var(--tone),.07)}
.modegrid button:hover:not(:disabled){background:rgba(var(--tone),.16);border-color:rgb(var(--tone))}
.exitgrid>div{padding:12px 14px!important;border-radius:11px;border:1px solid #284158;background:#112434}
.exitgrid>div:nth-child(2){background:#201c32;border-color:#39304f}.exitgrid>div:nth-child(3){background:#122a29;border-color:#264843}
.exitgrid .small{color:#a7b7c8}.exitgrid .value{margin-top:4px}
#exitIP{color:#7acaff;font-size:22px;letter-spacing:.2px;font-variant-numeric:tabular-nums}
#exitASN{color:#c3a7ff}#exitLocation{color:#77d9c6}#exitOrg{margin-top:3px}
#refreshExit,[data-update-action="check"]{color:#92d1ff;border-color:#355975;background:#162c3e}
[data-update-action="prepare"],a.btn{color:#d0bbff;border-color:#54416e;background:#2a213b}
[data-update-action="install"]{color:#96e8b6;border-color:#356344;background:#193524}
[data-update-action="rollback"],#cleanupUpdates{color:#edce94;border-color:#645035;background:#332a1c}
#rebootButton{color:#ffbc94;border-color:#684935;background:#30231c}
#rebootButton.reboot-needed,#confirmReboot{color:#ffe2a3;background:#543e13;border-color:#d29922;box-shadow:0 0 0 1px #d2992244 inset}
#updateCards [data-component]{--tone:116,186,255;border-top:3px solid rgb(var(--tone));background:#121a24}
#updateCards [data-component="mihomo"]{--tone:186,151,255}
#updateCards [data-component="system"]{--tone:86,211,151}#updateCards [data-field="label"]{color:rgb(var(--tone))}
#updateCards [data-field="prepared"]{color:#96e8b6}#updateStorage{padding:11px 13px;background:#10261e;border:1px solid #274536;border-radius:9px;color:#a2cbb2}
@media(prefers-reduced-motion:reduce){button,a.btn{transition:none}}

#xrayCard select{margin-top:6px;padding:10px;border-radius:9px;border:1px solid var(--line);background:var(--btn);color:var(--text);max-width:100%}
#xrayCard .xray-controls{display:grid;grid-template-columns:minmax(160px,1fr) auto minmax(160px,1fr) auto;gap:10px;align-items:end}
#xrayCard .xray-activation{grid-template-columns:minmax(160px,1fr) auto auto}
#xrayCard .xray-controls label{display:flex;flex-direction:column;gap:6px;min-width:0}#xrayCard .xray-controls br{display:none}
#xrayCard .xray-controls select{margin:0;width:100%;height:44px}#xrayCard .xray-controls button{height:44px;white-space:nowrap}
.resource-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin-top:14px}#cpuTemperature{color:#77d9c6}
.configtable{border-collapse:collapse;width:100%;font-size:13px;margin-top:10px}.configtable th,.configtable td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.configrow{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px 0;border-top:1px solid var(--line)}.filebtn{display:inline-block;cursor:pointer;padding:12px 14px;border:1px solid #54416e;border-radius:10px;color:#d0bbff;background:#2a213b;font-weight:650}
progress{width:100%;height:10px;accent-color:#77d9c6}#xrayCard summary{cursor:pointer}#xrayJob,#configMessage{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:700px){.configrow{align-items:flex-start;flex-direction:column}.configrow .row{width:100%}.configrow a,.configrow label{flex:1;padding:10px}#xrayCard .row{gap:8px}#xrayCard label{max-width:100%}}
@media(max-width:700px){#xrayCard .xray-controls{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}#xrayCard .xray-activation label{grid-column:1/-1}#xrayCard .xray-controls button{padding:10px 8px}.resource-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.resource-grid>div:last-child{grid-column:1/-1}}

.router-header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:14px}.router-header h1{margin:0;font-size:24px;white-space:nowrap}.wan-controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.wan-toggle{display:inline-flex;padding:4px;gap:4px;background:#080f1c;border:1px solid #43566f;border-radius:13px}.wan-toggle button{min-width:104px;height:44px;padding:8px 16px;border:1px solid transparent;background:transparent;border-radius:9px;font-weight:700;font-size:15px;color:#c2cfe2}.wan-toggle button[aria-pressed="true"]{background:#19486b;border-color:#73c6ff;color:#fff;box-shadow:0 0 0 1px #73c6ff}.wan-check{display:inline-flex;gap:8px;align-items:center;font-size:14px;color:var(--text);cursor:pointer}.wan-check input{width:19px;height:19px;accent-color:#77d9c6;margin:0;flex:0 0 auto}.wan-live{font-size:12px;color:#9ce6d8;margin-top:6px}.wan-overview{margin:0 0 14px;display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);font-size:12px}.wan-overview b{font-weight:600;color:var(--text)}.modem-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:14px}.modem-grid>div{overflow-wrap:anywhere}.modem-grid label{display:flex;flex-direction:column;gap:6px}.modem-grid input{box-sizing:border-box;width:100%;height:44px;min-width:0;padding:10px;border:1px solid var(--line);border-radius:9px;background:var(--btn);color:var(--text)}#modemCard summary{cursor:pointer}#modemError{color:#ffd087}.sms-message{padding:12px;border:1px solid var(--line);border-radius:10px;margin-top:8px}.sms-message p{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}.wan-reboot{text-align:right}#wanJob{overflow-wrap:anywhere}@media(max-width:700px){.router-header{display:grid;grid-template-columns:1fr auto;gap:12px}.router-header h1{font-size:22px}.wan-controls{grid-column:1/-1;grid-row:2;justify-content:space-between;gap:10px}.wan-reboot{grid-column:2;grid-row:1}.wan-toggle button{min-width:98px}.modem-grid{grid-template-columns:1fr}.wan-overview{line-height:1.6}#modemCard .row{gap:10px}}

.mtu-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}.mtu-grid fieldset{min-width:0;margin:0;padding:14px;border:1px solid var(--line);border-radius:10px}.mtu-grid legend{padding:0 6px;font-weight:600}.mtu-grid label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted);margin-bottom:10px}.mtu-grid input,.mtu-grid select{box-sizing:border-box;width:100%;min-width:0;height:44px;padding:10px;border:1px solid var(--line);border-radius:9px;background:var(--btn);color:var(--text)}.mtu-grid input:disabled{opacity:.65}#mtuSaveNote{overflow-wrap:anywhere}@media(max-width:700px){.mtu-grid{grid-template-columns:1fr}}


.lte-speed{margin-top:16px;padding:15px;border:1px solid #365a60;border-radius:12px;background:linear-gradient(120deg,#152a30,#18202b)}.lte-speed-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.lte-speed-actions{display:flex;gap:8px}.lte-speed-value{font-size:30px;font-weight:750;color:#91ead5;line-height:1.3;margin-top:8px}.lte-speed-value span{font-size:14px;color:var(--muted);font-weight:500}.lte-speed progress{display:block;width:100%;height:6px;margin-top:10px;accent-color:#77d9c6}.lte-speed [hidden]{display:none!important}#lteSpeedNote,#lteSpeedRadio{margin-top:6px;overflow-wrap:anywhere}@media(max-width:520px){.lte-speed-actions{width:100%}.lte-speed-actions button{flex:1}.lte-speed-value{font-size:28px}}
/* Configuration files: collapsible groups per engine (display only; API unchanged). */
#configCard .cfggroup{border:1px solid var(--line);border-radius:12px;margin-top:10px;background:#10161e}
#configCard .cfggroup>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 14px;font-weight:650;border-radius:12px}
#configCard .cfggroup>summary::-webkit-details-marker{display:none}
#configCard .cfggroup>summary::before{content:'\25B8';color:var(--muted);display:inline-block;transition:transform .15s}
#configCard .cfggroup[open]>summary::before{transform:rotate(90deg)}
#configCard .cfggroup>summary:hover{background:#151d27}
#configCard .cfgcount{color:var(--muted);font-size:13px;font-weight:500}
#configCard .cfgchip{display:inline-block;font-size:11px;font-weight:650;padding:2px 8px;border-radius:999px;border:1px solid #54416e;color:#d0bbff;background:#2a213b;vertical-align:middle}
#configCard .cfgchip.mode{border-color:#396a4c;color:#8de9b6;background:#193425}
#configCard .cfgchip.secret{border-color:#756039;color:#f3d197;background:#342c1f}
#configCard .cfgchip[hidden]{display:none}
#configCard .cfgrows{padding:0 14px 4px}
#configCard .cfgrow{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
#configCard .cfgmain{min-width:0;flex:1}
#configCard .cfgfile{font-size:12px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
#configCard .cfgpath{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
#configCard .cfgbtns{display:flex;gap:8px;flex:0 0 auto}
#configCard .cfgbtns a.btn,#configCard .cfgbtns .filebtn{padding:8px 12px;font-size:13px}
@media(max-width:700px){#configCard .cfgrow{flex-direction:column;align-items:stretch}#configCard .cfgbtns a.btn,#configCard .cfgbtns .filebtn{flex:1;text-align:center}}
/* lte-bands-v1: Frequency bands pills in the Mobile network card. */
#bandsDetails .bands-now{margin-top:12px;overflow-wrap:anywhere}
#bandsDetails .bands-groups{display:grid;gap:14px;margin-top:14px}
#bandsDetails .bands-group{display:grid;grid-template-columns:120px minmax(0,1fr);gap:10px;align-items:start}
#bandsDetails .bands-title{font-size:13px;color:var(--muted);padding-top:13px}
#bandsDetails .bands-pills{display:flex;flex-wrap:wrap;gap:8px}
#bandsDetails .band{position:relative;display:inline-flex;flex-direction:column;align-items:center;justify-content:center;min-width:62px;height:46px;padding:3px 12px;border-radius:999px;border:1px solid #3d7a71;background:#15393a;color:#eafffb;font-weight:700;font-size:14px;line-height:1.15;box-shadow:none}
#bandsDetails .band small{font-size:10.5px;font-weight:500;color:#9fd9cf}
#bandsDetails .band[aria-pressed="false"]{background:transparent;border-color:#2c3540;color:#6c7886}
#bandsDetails .band[aria-pressed="false"] small{color:#56616e}
#bandsDetails .band:hover:not(:disabled){border-color:#77d9c6;background:#1b4746}
#bandsDetails .band[aria-pressed="false"]:hover:not(:disabled){background:#17202a}
#bandsDetails .band:disabled{opacity:.55}
#bandsDetails .band.serving::after{content:'';position:absolute;top:5px;right:9px;width:7px;height:7px;border-radius:50%;background:#77d9c6;box-shadow:0 0 6px #77d9c6}
#bandsDetails .band.changed{border-style:dashed;border-color:#d29922}
#bandsDetails .bands-actions{justify-content:flex-end;margin-top:14px}
#bandsDetails #bandsApply:not(:disabled){color:#96e8b6;border-color:#356344;background:#193524}
#bandsDetails #bandsNote{margin-top:8px;overflow-wrap:anywhere}
@media(max-width:700px){#bandsDetails .bands-group{grid-template-columns:1fr;gap:6px}#bandsDetails .bands-title{padding-top:0}#bandsDetails .band{min-width:56px;padding:3px 10px}#bandsDetails .bands-actions button{flex:1}}
.wan-force input{accent-color:#f5b25c}/* wan-force-v1 */
</style></head><body>
<div class="wrap"><div class="router-header"><div><h1>RouteDeck</h1><div class="small" title="Panel build 20261020.1">Panel v3.0.10</div></div>
 <div class="wan-controls"><div><div class="wan-toggle" role="group" aria-label="Preferred internet WAN"><button id="wanEthernet" aria-pressed="true">Ethernet</button><button id="wanLte" aria-pressed="false">4G</button></div><div id="wanActive" class="wan-live" role="status">Checking active WAN…</div></div><label class="wan-check"><input id="wanFallback" type="checkbox">Fallback</label><label class="wan-check wan-force" title="Use the selected WAN as it is: no internet checks, no rollback and no automatic switching until Force is off"><input id="wanForce" type="checkbox">Force</label><div><label class="wan-check" title="Off keeps the CPU at its base clock: lower peak power"><input id="cpuBoost" type="checkbox">CPU boost</label><div id="cpuBoostNote" class="wan-live" role="status">Checking CPU boost…</div></div></div>
 <div class="wan-reboot"><button id="rebootButton">Reboot</button><div id="rebootNote" class="small" role="status" style="margin-top:5px"></div></div></div>
 <div class="wan-overview"><span id="ethernetHealth">Ethernet: checking…</span><span id="lteHealth">4G: checking…</span><span id="wanRule">Fallback OFF</span></div><div id="wanWhitelist" class="small" role="status" aria-live="polite" style="display:none;margin:4px 0 12px;font-size:12px;line-height:1.5"></div><!-- lte-whitelist-v1 -->
<dialog id="rebootDialog" aria-labelledby="rebootDialogTitle"><h2 id="rebootDialogTitle" style="font-size:20px;margin-top:0">Reboot Mini-PC?</h2><p>Internet access and this panel will temporarily disconnect. Continue when you are ready.</p><div class="row" style="justify-content:flex-end"><button id="cancelReboot">Cancel</button><button id="confirmReboot">Reboot now</button></div></dialog>

<div class="card span2">
 <div class="row" style="justify-content:space-between">
  <div><div class="small">Current mode</div><div class="kpi" id="mode">...</div></div>
  <div class="status"><span class="dot warn" id="healthDot"></span><span id="health">Checking...</span></div>
 </div>
 <div class="modegrid">
  <button data-mode="direct">DIRECT</button><button data-mode="awg">AWG</button>
  <button data-mode="tun">TUN</button><button data-mode="mihomo">MIHOMO</button>
 </div>
 <div class="small" id="healthDetail" style="margin-top:10px">Waiting for external check</div>
 <div class="small" id="healthTimes" style="margin-top:4px"></div>
 <div class="exitgrid">
  <div><div class="small">Public exit IP</div><div class="value" id="exitIP">Checking…</div></div>
  <div><div class="small">Network / ASN</div><div class="value" id="exitASN">—</div><div class="small" id="exitOrg"></div></div>
  <div><div class="small">City / country</div><div class="value" id="exitLocation">—</div></div>
 </div>
 <div class="row" style="justify-content:space-between;margin-top:10px"><div class="small" id="exitInfo"></div><button id="refreshExit" style="padding:7px 10px;font-size:12px">Refresh IP</button></div>
 <div class="small" id="kernelPolicyNote" style="margin-top:8px"></div>
</div>

<section class="card span2" id="resourcesCard" style="margin-top:12px;border-top:3px solid #77d9c6">
 <div class="value">System Resources</div><div class="resource-grid"><div><div class="small">CPU</div><div class="kpi" id="cpuPercent">…</div><progress id="cpuBar" max="100" value="0"></progress><div class="small" id="cpuDetail"></div></div><div><div class="small">RAM</div><div class="kpi" id="ramPercent">…</div><progress id="ramBar" max="100" value="0"></progress><div class="small" id="ramDetail"></div></div><div><div class="small">CPU temperature</div><div class="kpi" id="cpuTemperature">…</div><div class="small" id="cpuSensor"></div></div></div><div class="small" id="resourceUptime" style="margin-top:10px"></div>
</section>

<div class="grid">
 <div class="card">
  <div class="row" style="justify-content:space-between">
   <div><div class="small">LAN traffic - current session</div><div class="value">@@LAN_IF@@</div></div>
   <button id="resetCounters">Reset</button>
  </div>
  <div class="two">
   <div><div class="small">Download</div><div class="value" id="down">0 Mbps</div></div>
   <div><div class="small">Upload</div><div class="value" id="up">0 Mbps</div></div>
  </div>
  <div class="two">
   <div><div class="small">Downloaded</div><div id="downloadTotal">0</div></div>
   <div><div class="small">Uploaded</div><div id="uploadTotal">0</div></div>
  </div>
  <div class="small" style="margin-top:8px">Reset manually or automatically after a successful mode switch.</div>
 </div>

 <div class="card">
  <div class="small">Mihomo</div>
  <div class="row" style="justify-content:space-between">
   <div class="status"><span class="dot" id="mihomoDot"></span><span id="mihomoState">...</span></div>
   <a class="btn" href="http://192.168.50.1:9090/ui/" target="_blank">Open Zashboard</a><button data-target="mihomo" data-action="restart">Restart</button>
  </div>
  <div style="margin-top:12px"><div class="small">Selected proxy</div><div class="value" id="proxy">-</div></div>
 </div>

 <div class="card">
  <div class="small">AWG</div>
  <div class="status"><span class="dot" id="awgDot"></span><span id="awgState">...</span></div>
  <div class="row" style="margin-top:12px"><button data-target="awg" data-action="rotate">Rotate</button><button data-target="awg" data-action="restart">Restart</button></div>
  <div class="small" id="awgHealth" style="margin-top:8px"></div>
 </div>

 <div class="card">
  <div class="small">TUN / sing-box</div>
  <div class="status"><span class="dot" id="tunDot"></span><span id="tunState">...</span></div>
  <div class="row" style="margin-top:12px"><button data-target="tun" data-action="rotate">Rotate</button><button data-target="tun" data-action="restart">Restart</button></div>
  <div class="small" id="tunHealth" style="margin-top:8px"></div>
 </div>

 <div class="card span2">
  <div class="row" style="justify-content:space-between">
   <div><div class="small">Last action</div><div id="lastAction">None</div></div><button id="refresh">Check now</button>
  </div>
  <pre id="output" style="display:none"></pre>
 </div>
<section class="card span2" id="modemCard" style="margin-top:12px;border-top:3px solid #77d9c6">
 <div class="row" style="justify-content:space-between"><div><div class="small">Mobile network</div><div class="value">4G modem · Network settings &amp; SMS</div></div><div class="status"><span id="modemDot" class="dot warn"></span><span id="modemConnection">Checking…</span></div></div>
 <div class="modem-grid"><div><div class="small">Operator / modem</div><div id="modemOperator">—</div><div class="small" id="modemModel"></div></div><div><div class="small">Radio quality</div><div id="modemRadio">—</div><div class="small" id="modemSignal"></div></div><div><div class="small">Mobile connection</div><div id="modemAddress">—</div><div class="small" id="modemApn"></div></div></div>
 <div id="modemError" class="small" role="status" style="margin-top:8px"></div>

 <section class="lte-speed" aria-labelledby="lteSpeedTitle">
  <div class="lte-speed-head"><div><div id="lteSpeedTitle" style="font-weight:700">4G download speed</div><div class="small">Measure the modem before switching to 4G</div></div><div class="lte-speed-actions"><button id="lteSpeedStart" type="button">Test 4G download</button><button id="lteSpeedStop" type="button" hidden>Stop test</button></div></div>
  <div class="lte-speed-value" id="lteSpeedValue">— <span>Mbps</span></div>
  <div class="small" id="lteSpeedWhen">No measurement yet</div>
  <div class="small" id="lteSpeedRadio"></div>
  <progress id="lteSpeedProgress" max="20" value="0" hidden aria-label="4G speed test progress"></progress>
  <div class="small" id="lteSpeedNote" role="status" aria-live="polite">Ready when the modem has an internet connection.</div>
  <div class="small" style="margin-top:10px">Direct 4G · about 20 seconds · up to 96 MiB of mobile data. Keeps your selected WAN and traffic mode. Measures download only; other traffic can affect the result.</div>
 </section>
 <details style="margin-top:14px"><summary>Mobile settings · APN</summary>
  <form id="mobileForm" autocomplete="off">
   <div class="row" style="margin-top:12px"><label class="wan-check"><input type="checkbox" id="autoApn" checked>Automatic operator APN</label><label class="wan-check"><input type="checkbox" id="allowRoaming">Allow roaming</label></div>
   <p class="small">Supported operators, including Rostelecom and T-Mobile, are recognized automatically. You can also enter an APN below. After replacing a SIM, saved manual settings must be confirmed for that SIM.</p>
   <div class="modem-grid"><label class="small">Access point (APN)<input id="mobileApn" type="text" maxlength="100" placeholder="internet.rtk.ru" autocapitalize="none" spellcheck="false"></label><label class="small">Username (optional)<input id="mobileUser" type="text" maxlength="128" autocapitalize="none" spellcheck="false"></label><label class="small">Password (optional)<input id="mobilePassword" type="password" maxlength="128" autocomplete="new-password" placeholder="Leave blank to keep saved password"></label></div>
   <div class="row" style="justify-content:space-between;margin-top:12px"><label class="wan-check"><input id="clearMobilePassword" type="checkbox">Clear saved password</label><button id="saveMobile" type="submit">Save &amp; reconnect 4G</button></div>
   <div class="small" id="mobileSaveNote" style="margin-top:8px">APN changes reconnect only the standby modem. Select Ethernet first if 4G is active.</div>
  </form>
 </details>
 <!-- lte-bands-v1: Frequency bands sub-card; the band list comes from the modem -->
 <details id="bandsDetails" style="margin-top:16px"><summary>Frequency bands · <span id="bandsSummary">reading the modem…</span></summary>
  <div class="small bands-now" id="bandsServing" role="status"></div>
  <div class="bands-groups" id="bandsGroups"></div>
  <div class="row bands-actions"><button type="button" id="bandsAll">All on</button><button type="button" id="bandsOriginal" hidden>Original set</button><button type="button" id="bandsUndo">Undo</button><button type="button" id="bandsApply">Apply bands</button></div>
  <div class="small" id="bandsNote" role="status" aria-live="polite"></div>
  <p class="small">The list comes from the modem itself. Tap a band to turn it on or off: bright is on, dim is off, a dot marks a band in use now, a dashed edge marks a change not applied yet. Apply works while Ethernet is active: it reconnects the standby 4G (about a minute), checks its internet and restores the previous bands if 4G does not come back. The modem keeps its bands after a reboot.</p>
 </details>
 <details id="mtuDetails" style="margin-top:16px"><summary>AWG MTU · Ethernet / 4G</summary>
  <p class="small">Auto uses the WAN interface MTU and AWG packet overhead, capped by the MTU in wg0.conf. This is a local budget, not full path MTU discovery. Choose Manual for a smaller limit on a particular network.</p>
  <form id="mtuForm"><div class="mtu-grid">
   <fieldset><legend>Ethernet</legend><label>MTU selection<select id="mtuEthernetMode"><option value="auto">Auto</option><option value="manual">Manual</option></select></label><label>MTU, bytes<input id="mtuEthernetValue" type="number" min="1280" max="9000" step="1" inputmode="numeric" disabled></label><div class="small" id="mtuEthernetHint"></div></fieldset>
   <fieldset><legend>4G</legend><label>MTU selection<select id="mtuLteMode"><option value="auto">Auto</option><option value="manual">Manual</option></select></label><label>MTU, bytes<input id="mtuLteValue" type="number" min="1280" max="9000" step="1" inputmode="numeric" disabled></label><div class="small" id="mtuLteHint"></div></fieldset>
  </div><div class="row" style="justify-content:space-between;margin-top:12px"><div id="mtuActual" class="small" role="status"></div><button id="saveMtu" type="submit">Save AWG MTU</button></div>
  <p class="small">Saved for each WAN and kept after reboot. Applies without restarting AWG or editing its profile; failed internet checks restore the previous setting. Reconnect a download or speed test to measure the new value.</p><div class="small" id="mtuSaveNote" role="status"></div></form>
 </details>
 <details id="smsDetails" style="margin-top:16px"><summary>Incoming SMS</summary><div class="row" style="justify-content:space-between;margin-top:10px"><span class="small">Messages stored on the modem / SIM</span><button id="refreshSms" type="button">Refresh SMS</button></div><div id="smsMessages" role="status" style="margin-top:10px">Open to load incoming messages.</div></details>
 <div class="small" id="wanJob" role="status" style="margin-top:12px"></div>
</section>


<section class="card span2" id="xrayCard" style="margin-top:12px;border-top:3px solid #7acaff">
 <div class="row" style="justify-content:space-between"><div><div class="small">Native Xray · Mihomo SOCKS bridge</div><div class="status"><span id="xrayDot" class="dot warn"></span><span id="xrayState">Checking…</span></div></div><div class="row"><button data-xray="start">Start</button><button data-xray="stop">Stop</button><button data-xray="restart">Restart</button></div></div>
 <div class="small" id="xrayDetails" style="margin-top:10px"></div>
 <div class="xray-controls" style="margin-top:14px"><label class="small">Download via<br><select id="xrayRoute"><option value="current">Current router mode</option><option value="direct">Direct / WAN</option></select></label><button data-xray="check" data-update-action="check">Check releases</button><label class="small">Official version<br><select id="xrayRelease"><option value="v26.9.9">v26.9.9</option></select></label><button data-xray="download" data-update-action="prepare">Download</button></div>
 <div class="xray-controls xray-activation" style="margin-top:10px"><label class="small">Downloaded cores<br><select id="xraySaved"></select></label><button data-xray="install" data-update-action="install">Install selected</button><button data-xray="rollback" data-update-action="rollback">Rollback</button></div>
 <p class="small">Keeps 3 cores, including the active and rollback versions. Downloads follow the selected path without automatic WAN fallback. Core changes validate the config and compare TCP/UDP checks before activation.</p>
 <div id="xrayJob" class="small" role="status"></div>
 <details style="margin-top:14px"><summary>SOCKS → Xray proxy mapping</summary><div style="overflow-x:auto"><table class="configtable"><thead><tr><th>Mihomo name</th><th>Local SOCKS</th><th>Xray server</th><th>Transport / fingerprint</th></tr></thead><tbody id="xrayMapping"></tbody></table></div><p class="small">Read from current Mihomo and Xray files. nodes.json provides fallback names only. Restart the affected engine after changing its connection config.</p></details>
</section>

<section class="card span2" id="updatesSection" style="margin-top:12px">
 <div class="row" style="justify-content:space-between;align-items:flex-start">
  <div><div class="value">Updates</div><div class="small" style="margin-top:5px">Check, download, then install when you are ready.</div></div>
  <label class="small">Download via<br><select id="updateRoute" style="margin-top:6px;padding:10px;border-radius:9px;border:1px solid var(--line);background:var(--btn);color:var(--text)"><option value="current">Current router mode</option><option value="direct">Direct / WAN</option></select></label>
 </div>
 <div class="small" id="updateRouteInfo" style="margin:12px 0">Reading update status…</div>
 <div class="small" id="updateStorage" style="margin:12px 0"></div>
 <div class="row" style="gap:12px;margin-bottom:14px"><button id="cleanupUpdates">Clean cache</button><span class="small">Automatic cleanup after successful installs. Two backups kept. Prepared downloads are protected.</span></div>
 <div class="grid" id="updateCards"></div>
 <div style="margin-top:14px" role="status" aria-live="polite" id="updateJob">No update operation has been started.</div>
 <details style="margin-top:10px"><summary class="small" style="cursor:pointer">Operation log</summary><pre id="updateLog">No operations yet.</pre></details>
</section>

<section class="card span2" id="configCard" style="margin-top:12px;border-top:3px solid #c3a7ff">
 <div class="row" style="justify-content:space-between"><div class="value">Configuration Files</div><button id="refreshConfigs">Refresh</button></div>
 <p class="small">Download or replace individual files. Uploads are checked and backed up; services are not restarted automatically. After editing, use the restart button for the corresponding active mode. DIRECT has no tunnel config.</p>
 <div id="configRows"></div><div class="small" id="configMessage" role="status" style="margin-top:10px"></div>
</section>

</div></div><div id="toast"></div>

<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];let busy=false;
function fmtBytes(n){if(n==null)return'-';const u=['B','KB','MB','GB','TB'];let i=0,x=Number(n);while(x>=1024&&i<u.length-1){x/=1024;i++}return `${x.toFixed(i?1:0)} ${u[i]}`}
function fmtMbps(bps){return `${((bps||0)*8/1e6).toFixed(1)} Mbps`}
function fmtTime(ts){if(!ts)return'';try{return new Date(ts*1000).toLocaleTimeString()}catch{return''}}
function dot(id,state){const e=$(id);e.className='dot'+(state==='ok'?' ok':state==='warn'||state==='checking'?' warn':'')}
function toast(msg){const e=$('#toast');e.textContent=msg;e.style.display='block';setTimeout(()=>e.style.display='none',2500)}
function lock(v){busy=v;$$('button').forEach(b=>b.disabled=v)}
async function api(path,opts={}){const r=await fetch(path,{cache:'no-store',...opts});const d=await r.json().catch(()=>({ok:false,error:`HTTP ${r.status}`}));if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);return d}

async function refresh(){
 try{
  const s=await api('/api/status');
  $('#mode').textContent=(s.mode||'unknown').toUpperCase();
  const ma=s.mode_action||{};if(ma.mode===s.mode&&ma.status==='failed'&&!busy&&!s.health?.internet_ok){$('#lastAction').textContent=ma.message;}
  $$('.modegrid button').forEach(b=>b.classList.toggle('active',b.dataset.mode===s.mode));

  $('#down').textContent=fmtMbps(s.traffic?.download_rate);$('#up').textContent=fmtMbps(s.traffic?.upload_rate);
  $('#downloadTotal').textContent=fmtBytes(s.traffic?.download_bytes);$('#uploadTotal').textContent=fmtBytes(s.traffic?.upload_bytes);

  $('#mihomoState').textContent=s.mihomo?.active?'Running':'Stopped';dot('#mihomoDot',s.mihomo?.active?'ok':'bad');$('#proxy').textContent=s.mihomo?.proxy||'-';
  $('#awgState').textContent=s.interfaces?.wg0?'wg0 up':'wg0 down';dot('#awgDot',s.mode==='awg'?(s.health?.level||'checking'):(s.interfaces?.wg0?'warn':'bad'));$('#awgHealth').textContent=s.mode==='awg'?(s.health?.summary||''):'';
  $('#tunState').textContent=s.interfaces?.tun0?'tun0 up':'tun0 down';dot('#tunDot',s.mode==='tun'?(s.health?.level||'checking'):(s.interfaces?.tun0?'warn':'bad'));$('#tunHealth').textContent=s.mode==='tun'?(s.health?.summary||''):'';

  const h=s.health||{};
  dot('#healthDot',h.level||'checking');
  if(h.level==='ok')$('#health').textContent='Internet OK';
  else if(h.level==='warn')$('#health').textContent='Internet OK - DNS warning';
  else if(h.level==='fail')$('#health').textContent='No Internet';
  else $('#health').textContent='Checking...';
  $('#healthDetail').textContent=h.summary||'Waiting for external check';

  let parts=[];
  if(h.last_ok)parts.push(`Last OK: ${fmtTime(h.last_ok)}`);
  if(h.failed_since)parts.push(`Failed since: ${fmtTime(h.failed_since)}`);
  $('#healthTimes').textContent=parts.join(' | ');

  $$('[data-target="mihomo"]').forEach(b=>b.disabled=busy||window.routerUpdateBusy||s.mode!=='mihomo');
  $$('[data-target="awg"]').forEach(b=>b.disabled=busy||window.routerUpdateBusy||!(s.actions?.awg&&s.mode==='awg'));
  $$('[data-target="tun"]').forEach(b=>b.disabled=busy||window.routerUpdateBusy||!(s.actions?.tun&&s.mode==='tun'));
  $$('[data-mode]').forEach(b=>b.disabled=busy||window.routerUpdateBusy);
  const mh=$('[data-mode="mihomo"]');if(mh)mh.disabled=busy||window.routerUpdateBusy||!s.mihomo?.available;
  if(window.renderRouterUpdates)window.renderRouterUpdates();
 }catch(e){$('#health').textContent='Panel/backend error';$('#healthDetail').textContent=e.message;dot('#healthDot','bad')}
}

async function post(path,body){
 lock(true);$('#output').style.display='none';
 try{
  const d=await api(path,{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:JSON.stringify(body)});
  $('#lastAction').textContent=d.message||'OK';
  if(d.output){$('#output').textContent=d.output;$('#output').style.display='block'}
  toast('Done');
 }catch(e){
  $('#lastAction').textContent='Failed';$('#output').textContent=e.message;$('#output').style.display='block';toast('Action failed');
 }finally{lock(false);await refresh()}
}

$$('[data-mode]').forEach(b=>b.onclick=()=>post('/api/mode',{mode:b.dataset.mode}));
$$('[data-action]').forEach(b=>b.onclick=()=>post('/api/action',{target:b.dataset.target,action:b.dataset.action}));
$('#resetCounters').onclick=()=>post('/api/counters/reset',{});
$('#refresh').onclick=()=>post('/api/health/check',{});
let rebootPending=false;
$('#rebootButton').onclick=()=>{if(!busy&&!window.routerUpdateBusy&&!rebootPending)$('#rebootDialog').showModal()};
$('#cancelReboot').onclick=()=>$('#rebootDialog').close();
$('#confirmReboot').onclick=async()=>{
 $('#rebootDialog').close();lock(true);
 try{const d=await api('/api/reboot',{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:JSON.stringify({confirm:true})});rebootPending=true;$('#rebootNote').textContent=d.message;toast('Reboot scheduled')}
 catch(e){$('#rebootNote').textContent=e.message}
 finally{lock(false);if(window.renderRouterUpdates)window.renderRouterUpdates()}
};
async function refreshNetwork(){
 try{
  const d=await api('/api/network'), n=d.network;
  $('#exitIP').textContent=n.ok?n.ip:n.checking?'Checking…':'Unavailable';
  $('#exitASN').textContent=n.ok?(n.asn||'Unknown'):'—';$('#exitOrg').textContent=n.ok?n.org:'';
  $('#exitLocation').textContent=n.ok?([n.city,n.country].filter(Boolean).join(', ')||'Unknown'):'—';
  $('#exitInfo').textContent='Via '+(n.mode||'unknown').toUpperCase()+' · WAN gateway: '+(n.wan_gateway||'—')+
   (n.checked_at?' · Checked '+fmtTime(n.checked_at):'')+' · Auto-refresh '+(n.refresh_seconds||30)+'s'+(n.checking?' · Refreshing…':'')+(n.ok?' · IP location is approximate':n.error?' · '+n.error:'');
 }catch(e){$('#exitIP').textContent='Unavailable';$('#exitASN').textContent='—';$('#exitOrg').textContent='';$('#exitLocation').textContent='—'}
}
$('#refreshExit').onclick=async()=>{try{await api('/api/network/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:'{}'});await refreshNetwork()}catch(e){toast(e.message)}};

refreshNetwork();setInterval(refreshNetwork,5000);
refresh();setInterval(()=>{if(!busy)refresh()},2000);
</script><script>
(() => {
  const names = ['mihomo', 'sing-box', 'system'];
  let data = null, requesting = false;
  window.routerUpdateBusy = false;
  const cards = document.getElementById('updateCards');
  names.forEach(name => {
    const card = document.createElement('div'); card.className = 'card';
    card.innerHTML = '<div class="value" style="font-size:17px" data-field="label"></div>' +
      '<div class="small" style="margin-top:10px">Installed</div><div data-field="installed" style="overflow-wrap:anywhere">…</div>' +
      '<div class="small" style="margin-top:10px">Available</div><div data-field="available">Not checked</div>' +
      '<div class="small" data-field="prepared" style="margin-top:8px;min-height:20px"></div>' +
      '<div class="row" style="gap:7px;margin-top:12px"></div>' +
      '<div class="small" data-field="note" style="margin-top:10px"></div>';
    card.dataset.component = name;
    const row = card.querySelector('.row');
    [['check','Check'],['prepare','Download'],['install','Install'],['rollback','Rollback']].forEach(([action,label]) => {
      if (name === 'system' && action === 'rollback') return;
      const button = document.createElement('button'); button.textContent = label;
      button.style.cssText = 'font-size:12px;padding:8px 10px';
      button.dataset.updateAction = action; button.onclick = () => act(name, action);
      row.appendChild(button);
    });
    cards.appendChild(card);
  });

  function render() {
    if (!data) return;
    window.routerUpdateBusy = !!data.busy || requesting;
    const blocked = window.routerUpdateBusy || busy;
    const reboot = data.reboot || {};
    rebootPending = !!reboot.pending;
    const rebootButton = document.getElementById('rebootButton');
    rebootButton.classList.toggle('reboot-needed', !!reboot.required);
    rebootButton.textContent = reboot.pending ? 'Rebooting…' : reboot.required ? 'Reboot required' : 'Reboot';
    rebootButton.disabled = blocked || rebootPending;
    document.getElementById('rebootNote').textContent = reboot.pending ? 'Router will restart shortly' :
      reboot.state?.status === 'failed' ? (reboot.state.error || 'Reboot failed') : reboot.required ? 'System updates need a restart' : '';
    const kernels = data.kernels;
    document.getElementById('kernelPolicyNote').textContent = kernels?.ok === false
      ? 'Kernel cleanup needs attention: ' + kernels.error
      : 'Kernel retention: latest 2 versions' + (kernels?.kernels ? ' · ' + kernels.kernels.join(', ') : '');
    document.getElementById('updateRoute').disabled = blocked;
    document.getElementById('cleanupUpdates').disabled = blocked || !!data.components.system?.staged;
    const storage = data.storage;
    if (storage) {
      const gib = n => (n / 1073741824).toFixed(1) + ' GiB';
      const mib = n => (n / 1048576).toFixed(1) + ' MiB';
      document.getElementById('updateStorage').textContent = 'Free: ' + gib(storage.free_bytes) +
        ' · /boot free: ' + mib(storage.boot_free_bytes) + ' · APT cache: ' + mib(storage.apt_cache_bytes) +
        ' · Update files and backups: ' + mib(storage.updater_bytes) +
        (storage.kernel_images.length > 2 ? ' · Kernel cleanup pending; see status above.' : '');
    }
    const route = document.getElementById('updateRoute').value;
    document.getElementById('updateRouteInfo').textContent = route === 'current'
      ? 'Downloads and DNS use ' + (data.mode || 'unknown').toUpperCase() + '. No automatic switch to Direct.'
      : 'Downloads use the direct WAN connection.';
    cards.querySelectorAll('[data-component]').forEach(card => {
      const name = card.dataset.component, item = data.components[name] || {};
      const field = (key, value) => card.querySelector('[data-field="' + key + '"]').textContent = value;
      field('label', item.label || name);
      field('installed', item.version || 'Unknown');
      const c = item.check;
      field('available', c ? (c.version + (c.same_version ? ' · Up to date' : '') + (c.prerelease ? ' · Prerelease' : '')) : 'Not checked');
      field('prepared', item.staged ? 'Downloaded: ' + item.staged.version : '');
      field('note', name === 'system'
        ? 'Ubuntu packages + AWG/DKMS. ' + (item.reboot_required ? 'Reboot required. ' : '') + 'No automatic rollback or reboot.'
        : 'Verified release files. Existing configuration is preserved.');
      card.querySelectorAll('[data-update-action]').forEach(button => {
        const action = button.dataset.updateAction;
        button.disabled = blocked || (action === 'install' && !item.staged) ||
          (action === 'rollback' && !item.rollback) || (action === 'prepare' && c?.same_version === true);
      });
    });
    if (window.routerUpdateBusy) document.querySelectorAll('[data-mode],[data-target]').forEach(b => b.disabled = true);
    const job = data.job;
    document.getElementById('updateJob').textContent = job
      ? (data.components[job.component]?.label || job.component) + ' · ' + job.action + ' · ' + job.status + (job.message ? ': ' + job.message.slice(0, 240) : '')
      : 'No update operation has been started.';
    document.getElementById('updateLog').textContent = job?.log || 'No operations yet.';
  }

  async function poll() {
    try { data = await api('/api/updates'); render(); }
    catch (error) { document.getElementById('updateJob').textContent = 'Update status unavailable: ' + error.message; }
  }

  async function act(component, action) {
    if (requesting || data?.busy || busy) return;
    const version = data?.components[component]?.staged?.version || '';
    let message = action === 'cleanup' ? 'Remove cached APT packages and unused update downloads, and keep two backups per component? Prepared component downloads, installed packages, kernels and the original system backup are preserved.'
      : action === 'rollback' ? 'Restore the previous binary? An active service may briefly restart.'
      : component === 'system' ? 'Install ' + version + '? Services may restart and connectivity may be interrupted. There is no automatic package rollback. The router will not reboot automatically.'
      : 'Install ' + version + '? An active service may briefly restart. The previous binary will be kept for rollback.';
    if (['install', 'rollback', 'cleanup'].includes(action) && !window.confirm(message)) return;
    requesting = true; render();
    try {
      await api('/api/updates/action', {method:'POST', headers:{'Content-Type':'application/json','X-Router-Panel':'1'},
        body:JSON.stringify({component,action,route:document.getElementById('updateRoute').value,confirm:true})});
      await poll();
    } catch (error) { document.getElementById('updateJob').textContent = error.message; }
    finally { requesting = false; render(); }
  }
  document.getElementById('updateRoute').onchange = render;
  document.getElementById('cleanupUpdates').onclick = () => act('system', 'cleanup');
  window.renderRouterUpdates = render;
  poll(); setInterval(poll, 4000);
})();

(()=>{
 const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let xr=null,cf=null,acting=false,uploading=false;
 const send=(path,data)=>api(path,{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:JSON.stringify(data)});
 function options(el,items){const value=el.value;const html=items.map(([v,label])=>`<option value="${esc(v)}">${esc(label)}</option>`).join('');if(el.innerHTML!==html){el.innerHTML=html;if(items.some(i=>i[0]===value))el.value=value}}
 async function xrefresh(){try{xr=await api('/api/xray');dot('#xrayDot',xr.healthy?'ok':xr.active?'warn':'bad');$('#xrayState').textContent=(xr.active?'Running':'Stopped')+' · '+(xr.version||'no core');$('#xrayDetails').textContent=`${xr.listening}/${xr.nodes.length} SOCKS listeners · Autostart ${xr.enabled?'enabled':'disabled'} · Router mode ${xr.mode.toUpperCase()}`;
 options($('#xrayRelease'),(xr.releases.length?xr.releases:[{tag:'v26.9.9',prerelease:true}]).map(r=>[r.tag,r.tag+(r.prerelease?' · prerelease':'')]));options($('#xraySaved'),xr.retained.map(r=>[r.tag,r.tag+(r.active?' · active':'')]));
 $('#xrayJob').textContent=xr.job?.message?`${xr.job.status} · ${xr.job.message}`:'Ready';
 $('#xrayMapping').innerHTML=xr.nodes.map(n=>`<tr><td>${esc(n.name)}${n.in_mihomo?'':' (not in Mihomo)'}</td><td>127.0.0.1:${n.port}</td><td>${esc(n.server)}:${n.server_port}</td><td>${esc(n.network)} / ${esc(n.fingerprint)}</td></tr>`).join('');
 $$('[data-xray]').forEach(b=>b.disabled=acting||xr.busy||busy||(b.dataset.xray==='rollback'&&!xr.rollback)||(b.dataset.xray==='start'&&xr.active)||(b.dataset.xray==='stop'&&!xr.active));
 }catch(e){$('#xrayJob').textContent=e.message;dot('#xrayDot','warn')}}
 $$('[data-xray]').forEach(b=>b.onclick=async()=>{const op=b.dataset.xray;if(['stop','restart','install','rollback'].includes(op)&&!confirm(`${op[0].toUpperCase()+op.slice(1)} Xray? ${xr?.mode==='mihomo'?'Active VLESS connections may disconnect briefly.':''}`))return;
 acting=true;try{await send('/api/xray',{op,route:$('#xrayRoute').value,tag:op==='download'?$('#xrayRelease').value:$('#xraySaved').value,confirm:true});toast('Xray operation started')}catch(e){toast(e.message)}finally{acting=false;await xrefresh()}});
 // Configuration files grouped by engine. Display only: the /api/configs contract is unchanged.
 const CFG_GROUPS=[
  {id:'awg',title:'AWG',kinds:['awg'],modes:['awg'],profile:'awg'},
  {id:'tun',title:'TUN · sing-box',kinds:['tun'],modes:['tun'],profile:'tun'},
  {id:'mihomo',title:'Mihomo',kinds:['mihomo'],modes:['mihomo']},
  {id:'xray',title:'Xray',kinds:['xray','nodes'],modes:['mihomo']}];
 const CFG_SECRET=new Set();
 let cfgOpen=null;
 const cfgStem=f=>String(f||'').replace(/\.[^.]+$/,'');
 function cfgName(c){const i=c.label.indexOf(' · '),s=i<0?c.label:c.label.slice(i+3);return s===cfgStem(c.file)?s:s.charAt(0).toUpperCase()+s.slice(1)}
 function cfgMarkMode(){const m=($('#mode')?.textContent||'').trim().toLowerCase();$$('#configRows details[data-group]').forEach(d=>{const chip=d.querySelector('.cfgchip.mode'),ms=d.dataset.modes?d.dataset.modes.split(' '):[];if(chip)chip.hidden=!(m&&ms.includes(m))})}
 function cfgRow(id,c,active){const secret=CFG_SECRET.has(c.kind);
  return `<div class="cfgrow"><div class="cfgmain"><div><strong>${esc(cfgName(c))}</strong> <span class="cfgfile">${esc(c.file)}</span>${active&&cfgStem(c.file)===active?' <span class="cfgchip">active</span>':''}${secret?' <span class="cfgchip secret">secret</span>':''}</div><div class="small cfgpath" title="${esc(c.path)}">${esc(c.path)}</div>${c.description?`<div class="small">${esc(c.description||'')}</div>`:''}</div><div class="cfgbtns"><a class="btn" href="/api/configs/download/${encodeURIComponent(id)}" download${secret?' data-secret="1"':''}>Download</a><label class="btn filebtn">Replace<input type="file" data-config="${esc(id)}" accept=".json,.yaml,.yml,.conf,.env,.txt" hidden></label></div></div>`}
 async function configrefresh(){if(uploading)return;try{cf=await api('/api/configs');
  if(cfgOpen===null){let m='';try{m=String((await api('/api/status')).mode||'').toLowerCase()}catch(e){}cfgOpen=new Set(CFG_GROUPS.filter(g=>g.modes.includes(m)).map(g=>g.id))}
  const entries=Object.entries(cf.configs),used=new Set();
  const groups=CFG_GROUPS.map(g=>({...g,items:entries.filter(([id,c])=>g.kinds.includes(c.kind))}));
  groups.forEach(g=>g.items.forEach(([id])=>used.add(id)));
  const other=entries.filter(([id])=>!used.has(id));
  if(other.length)groups.push({id:'other',title:'Other',kinds:[],modes:[],items:other});
  $('#configRows').innerHTML=groups.filter(g=>g.items.length).map(g=>{const active=g.profile?String(cf.active_profiles?.[g.profile]||'unknown'):'';const hit=g.items.find(([id,c])=>cfgStem(c.file)===active);
   const info=g.profile?' · active: '+(hit?cfgName(hit[1]):active):'';
   return `<details class="cfggroup" data-group="${g.id}" data-modes="${g.modes.join(' ')}"${cfgOpen.has(g.id)?' open':''}><summary><span class="cfgtitle">${esc(g.title)}</span><span class="cfgcount">${g.items.length} ${g.items.length===1?'file':'files'}${esc(info)}</span><span class="cfgchip mode" hidden>current mode</span></summary><div class="cfgrows">${g.items.map(([id,c])=>cfgRow(id,c,active)).join('')}</div></details>`}).join('');
  $$('#configRows details[data-group]').forEach(d=>d.addEventListener('toggle',()=>{if(d.open)cfgOpen.add(d.dataset.group);else cfgOpen.delete(d.dataset.group)}));
  $$('#configRows a[data-secret]').forEach(a=>a.onclick=e=>{if(!confirm('This file contains secret credentials. Download it?'))e.preventDefault()});
  $$('[data-config]').forEach(input=>input.onchange=()=>upload(input));
  cfgMarkMode();
 }catch(e){$('#configMessage').textContent=e.message}}
 async function upload(input){const file=input.files[0];if(!file)return;const id=input.dataset.config;if(file.size>1048576){toast('File must be at most 1 MiB');input.value='';return}if(!confirm(`Replace ${cf.configs[id].path} with ${file.name}? A backup will be saved. Restart is a separate action.`)){input.value='';return}
 uploading=true;$('#configMessage').textContent='Validating and saving…';try{const bytes=new Uint8Array(await file.arrayBuffer());let raw='';for(let i=0;i<bytes.length;i+=8192)raw+=String.fromCharCode(...bytes.subarray(i,i+8192));const d=await send('/api/configs',{target:id,data:btoa(raw),revision:cf.configs[id].revision,confirm:true});$('#configMessage').textContent=d.message;toast(d.changed?'Configuration saved':'No changes')}catch(e){$('#configMessage').textContent=e.message}finally{uploading=false;await configrefresh()}}
 $('#refreshConfigs').onclick=configrefresh;
 async function rrefresh(){try{const r=await api('/api/resources');$('#cpuPercent').textContent=r.cpu==null?'Measuring…':r.cpu+'%';$('#cpuBar').value=r.cpu||0;$('#cpuDetail').textContent=`${r.cores} logical CPUs · Load ${r.load.map(n=>n.toFixed(2)).join(' / ')}`;$('#ramPercent').textContent=r.ram_percent+'%';$('#ramBar').value=r.ram_percent;$('#ramDetail').textContent=`${fmtBytes(r.ram_used)} / ${fmtBytes(r.ram_total)}`;$('#cpuTemperature').textContent=r.cpu_temp_c==null?'Unavailable':r.cpu_temp_c.toFixed(1)+' °C';$('#cpuSensor').textContent=r.cpu_temp_sensor||'CPU sensor not available';$('#resourceUptime').textContent=`Uptime ${Math.floor(r.uptime/86400)}d ${Math.floor(r.uptime%86400/3600)}h ${Math.floor(r.uptime%3600/60)}m`}catch(e){$('#resourceUptime').textContent=e.message}}
 xrefresh();configrefresh();rrefresh();setInterval(xrefresh,6000);setInterval(rrefresh,3000);setInterval(cfgMarkMode,3000);
})();

</script><script>
(()=>{
 const el=id=>document.getElementById(id);let wanState=null,changing=false,formDirty=false,formLoaded=false;
 const name=w=>w==='lte'?'4G':'Ethernet';
 let mtuDirty=false,mtuSaving=false;
 const mtuRows=[['ethernet','Ethernet'],['lte','Lte']];
 function mtuFields(){for(const [,id] of mtuRows){const manual=el('mtu'+id+'Mode').value==='manual';el('mtu'+id+'Value').disabled=!manual;el('mtu'+id+'Value').required=manual;}}
 async function call(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:JSON.stringify(body)}:{cache:'no-store'});const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||'Request failed');return d}
 function autoFields(){for(const id of ['mobileApn','mobileUser','mobilePassword','clearMobilePassword'])el(id).disabled=el('autoApn').checked;}
 async function refreshWan(){try{
  const d=await call('/api/wan');wanState=d;const a=d.active?.wan,preferred=d.preferred;const locked=changing||d.busy;
  el('wanEthernet').setAttribute('aria-pressed',String(preferred==='ethernet'));el('wanLte').setAttribute('aria-pressed',String(preferred==='lte'));
  el('wanEthernet').disabled=el('wanLte').disabled=el('wanForce').disabled=locked;el('wanFallback').disabled=locked||!!d.force;el('wanFallback').parentElement.title=d.force?'Paused while Force is on; kept for when Force is off':'';
  if(!changing){el('wanFallback').checked=!!d.fallback;el('wanForce').checked=!!d.force;}
  const nolink=d.checks?.[a]?.link===false;el('wanActive').textContent='Active: '+name(a)+(d.force?' · forced':a!==preferred?' · reserve; preferred '+name(preferred):'')+(nolink?' · no link':'');el('wanActive').style.color=nolink?'#ffd087':'';
  el('wanRule').textContent=d.force?'Force ON · no checks, no rollback, no automatic switching':d.fallback_paused?'Fallback paused · restore internet, then re-enable':d.fallback?'Fallback ON · return after 2 min stable internet':'Fallback OFF';
  for(const [w,id] of [['ethernet','ethernetHealth'],['lte','lteHealth']]){const c=d.checks?.[w];el(id).textContent=name(w)+': '+(d.stale?'check pending':c?.ok?'Internet OK':c?.error||'checking…');el(id).style.color=!d.stale&&c?.ok?'#9ce6d8':'#ffd087';}
  const m=d.mobile||{},c=d.checks?.lte;el('modemDot').className='dot '+(!d.stale&&c?.ok?'ok':'warn');
  el('modemConnection').textContent=!d.stale&&c?.ok?(a==='lte'?'Internet active':'Internet ready · standby'):(m.error?'Needs attention':'Checking internet');
  el('modemOperator').textContent=(m.operator||'—')+(m.operator_code?' · '+m.operator_code:'');el('modemModel').textContent=(m.model||'')+(m.state?' · '+m.state:'');
  el('modemRadio').textContent=(m.technology||[]).join(', ').toUpperCase()+(m.signal_percent?' · '+m.signal_percent+'%':'');
  const signal=m.signal||{};el('modemSignal').textContent=['RSRP '+(signal.rsrp||'—')+' dBm','RSRQ '+(signal.rsrq||'—')+' dB','SINR '+(signal.snr||'—')+' dB'].join(' · ');
  el('modemAddress').textContent=(m.interface||'—')+(m.address?' · '+m.address:'');el('modemApn').textContent=(m.apn?'APN '+m.apn:'')+(m.mtu?' · MTU '+m.mtu:'');
  el('modemError').textContent=m.error||d.daemon_error||'';
  {const wl=d.whitelist||{},lines=[],tips=[],hm=t=>t?new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'?';/* lte-whitelist-v1 */
  for(const [w,id] of [['ethernet','ethernetHealth'],['lte','lteHealth']]){const s=wl[w]||{};if(s.sites?.length)tips.push(name(w)+' '+hm(s.checked_at)+': '+s.sites.join(', '));
   if(!d.stale&&s.state==='suspected'){el(id).textContent=name(w)+': Russian sites only · whitelist?';el(id).style.color='#ffd087';
    lines.push(['⚠ '+name(w)+': possible whitelist since '+hm(s.since)+' · Russian sites open '+s.ru+', foreign '+s.foreign,s.auto_reconnect?(s.last_reconnect?'modem reconnected '+hm(s.last_reconnect)+(s.last_result?' ('+s.last_result+')':'')+', next automatic reconnect after '+hm(s.next_reconnect):'reconnecting the modem'):'the session is upstream: reconnect the mobile link of the router or modem behind Ethernet'].join(' · '));}
   else if(s.auto_reconnect&&s.last_result&&Date.now()/1000-s.last_reconnect<21600)lines.push(name(w)+': modem reconnected '+hm(s.last_reconnect)+' for a possible whitelist ('+s.last_result+')');}
  if(!d.stale&&wl.lte?.state==='suspected'){el('modemDot').className='dot warn';el('modemConnection').textContent='Possible whitelist';}
  const box=el('wanWhitelist');box.replaceChildren(...lines.map(t=>{const p=document.createElement('div');p.textContent=t;if(t.startsWith('⚠'))p.style.color='#ffd087';return p;}));box.style.display=lines.length?'':'none';box.title=tips.join(' | ');}
  el('wanJob').textContent=d.job?.message?d.job.status.toUpperCase()+' · '+d.job.message:(d.last_action?.message||'');
  if(!formLoaded||!formDirty){const s=d.mobile_settings||{};el('autoApn').checked=!!s.auto_apn;el('allowRoaming').checked=!!s.allow_roaming;el('mobileApn').value=s.apn||'';el('mobileUser').value=s.username||'';el('mobilePassword').placeholder=s.password_saved?'Saved password · leave blank to keep':'Optional password';formLoaded=true;autoFields();}
  el('saveMobile').disabled=locked||a==='lte';el('mobileSaveNote').textContent=a==='lte'?'Select Ethernet before changing APN; the active mobile connection is kept.':'Save reconnects the standby modem and checks its internet. Current traffic mode stays unchanged.';
  const mt=d.awg_mtu||{};
  if(mtuSaving&&!d.busy&&['complete','failed','recovery_failed','rolled_back'].includes(d.job?.status)){mtuSaving=false;mtuDirty=false;el('mtuSaveNote').textContent=d.job.message;}
  for(const [wan,id] of mtuRows){const p=mt.plans?.[wan]||{},value=mt.values?.[wan];if(!mtuDirty&&!mtuSaving){el('mtu'+id+'Mode').value=value==null?'auto':'manual';el('mtu'+id+'Value').value=value??p.auto??'';el('mtu'+id+'Value').max=p.maximum??9000;}el('mtu'+id+'Hint').textContent='Auto: '+(p.auto??'pending')+' · WAN: '+(p.physical??'unavailable')+(p.maximum?' · Max: '+p.maximum:'');}
  mtuFields();el('saveMtu').disabled=locked||mtuSaving;
  for(const [,id] of mtuRows){el('mtu'+id+'Mode').disabled=locked||mtuSaving;if(locked||mtuSaving)el('mtu'+id+'Value').disabled=true;}
  el('mtuActual').textContent=mt.error||('Current AWG MTU: '+(mt.actual??'tunnel stopped')+' · '+name(mt.active_wan));
 }catch(e){el('wanActive').textContent='WAN status unavailable';el('wanJob').textContent=e.message}}
 async function action(body){if(changing)return;changing=true;el('wanJob').textContent='Starting…';try{await call('/api/wan',body)}catch(e){el('wanJob').textContent=e.message;alert(e.message)}finally{changing=false;await refreshWan()}}
 el('wanEthernet').onclick=()=>action({action:'select',wan:'ethernet'});el('wanLte').onclick=()=>action({action:'select',wan:'lte'});
 el('wanFallback').onchange=()=>action({action:'fallback',enabled:el('wanFallback').checked});
 el('wanForce').onchange=()=>action({action:'force',enabled:el('wanForce').checked});/* wan-force-v1 */
 el('mtuForm').oninput=()=>{mtuDirty=true;mtuFields();el('mtuSaveNote').textContent='Unsaved changes';};
 el('mtuForm').onsubmit=async e=>{e.preventDefault();if(changing||mtuSaving)return;const values={};for(const [wan,id] of mtuRows)values[wan]=el('mtu'+id+'Mode').value==='auto'?null:Number(el('mtu'+id+'Value').value);mtuSaving=true;changing=true;el('saveMtu').disabled=true;el('mtuSaveNote').textContent='Saving and checking AWG internet…';try{await call('/api/wan',{action:'awg_mtu',values})}catch(err){mtuSaving=false;el('mtuSaveNote').textContent=err.message;}finally{changing=false;await refreshWan();}};
 el('mobileForm').oninput=()=>{formDirty=true;autoFields()};
 el('mobileForm').onsubmit=async e=>{e.preventDefault();await action({action:'mobile',mobile:{auto_apn:el('autoApn').checked,apn:el('mobileApn').value,username:el('mobileUser').value,password:el('mobilePassword').value,clear_password:el('clearMobilePassword').checked,allow_roaming:el('allowRoaming').checked}});el('mobilePassword').value='';el('clearMobilePassword').checked=false;formDirty=false;};
 async function refreshSms(){el('refreshSms').disabled=true;try{const d=await call('/api/wan/sms');el('smsMessages').replaceChildren();if(!d.messages.length)el('smsMessages').textContent='No incoming SMS stored on the modem / SIM.';for(const m of d.messages){const box=document.createElement('article');box.className='sms-message';const heading=document.createElement('div');heading.className='small';heading.textContent=m.number+' · '+(m.timestamp||m.state);const p=document.createElement('p');p.textContent=m.text||'(No text content)';const del=document.createElement('button');del.type='button';del.textContent='Delete SMS';del.onclick=async()=>{if(!confirm('Delete this SMS from '+m.number+' ('+(m.timestamp||m.state)+')? This cannot be undone.'))return;del.disabled=true;try{await call('/api/wan/sms/delete',{id:m.id,revision:m.revision,confirm:true});await refreshSms()}catch(err){del.disabled=false;alert(err.message)}};const row=document.createElement('div');row.className='row';row.style.justifyContent='space-between';row.append(heading,del);box.append(row,p);el('smsMessages').append(box)}}catch(e){el('smsMessages').textContent=e.message}finally{el('refreshSms').disabled=false}}
 el('refreshSms').onclick=refreshSms;el('smsDetails').ontoggle=()=>{if(el('smsDetails').open)refreshSms()};
 refreshWan();setInterval(refreshWan,5000);
})();

</script>
<script>
(()=>{
 const el=id=>document.getElementById(id);let pending=false,inFlight=false,timer=null;
 async function call(body){const r=await fetch('/api/wan/speedtest',body?{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:JSON.stringify(body)}:{cache:'no-store'});const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||'Speed test unavailable');return d;}
 function render(d){const running=d.status==='running',r=d.last_result;
  el('lteSpeedStart').disabled=running||pending;el('lteSpeedStop').hidden=!running;el('lteSpeedStop').disabled=pending;
  el('lteSpeedProgress').hidden=!running;el('lteSpeedProgress').value=d.phase==='download'?Math.min(20,Math.max(0,Date.now()/1000-d.download_started_at)):0;
  el('lteSpeedNote').textContent=d.message||'Ready when the modem has an internet connection.';el('lteSpeedNote').style.color=d.status==='failed'?'#ffd087':'';
  if(r){el('lteSpeedValue').replaceChildren(document.createTextNode(Number(r.download_mbps).toFixed(1)+' '));const unit=document.createElement('span');unit.textContent='Mbps';el('lteSpeedValue').append(unit);
   el('lteSpeedWhen').textContent='Last result: '+new Date(r.at*1000).toLocaleString()+' · '+r.mib+' MiB · '+r.seconds+' s · '+r.server;
   const cells=[['Primary',r.radio?.primary]];
   if(r.radio?.scells?.length)r.radio.scells.forEach((c,i)=>cells.push(['Secondary '+(i+1),c]));
   else if(r.radio?.secondary)cells.push(['Secondary',r.radio.secondary]);
   const labels=[];for(const [title,c] of cells){if(c?.band)labels.push(title+': '+c.band+(c.mhz?' / '+c.mhz+' MHz':'')+(c.earfcn?' / EARFCN '+c.earfcn:'')+(c.pci!==undefined?' / PCI '+c.pci:'')+(title!=='Primary'&&c.state?' / '+c.state:''));}
   el('lteSpeedRadio').textContent=(labels.length?labels.join(' · '):'Radio details unavailable for this measurement')+(r.radio_note?' · '+r.radio_note:'');}
  return running;
 }
 async function refresh(){if(inFlight)return;inFlight=true;let running=false;try{running=render(await call())}catch(e){el('lteSpeedNote').textContent=e.message}finally{inFlight=false;clearTimeout(timer);timer=setTimeout(refresh,running?1000:5000)}}
 async function action(action){if(pending)return;pending=true;el('lteSpeedStart').disabled=el('lteSpeedStop').disabled=true;try{render(await call({action}))}catch(e){el('lteSpeedNote').textContent=e.message}finally{pending=false;el('lteSpeedStop').disabled=false;await refresh()}}
 el('lteSpeedStart').onclick=()=>action('start');el('lteSpeedStop').onclick=()=>action('cancel');refresh();
})();
</script>
<script>
(()=>{
 const el=id=>document.getElementById(id);let busy=false;
 function render(d){const box=el('cpuBoost'),note=el('cpuBoostNote');
  if(!d.available){box.checked=false;box.disabled=true;note.textContent='Boost switch not available';return}
  box.checked=!!d.boost;box.disabled=busy;
  note.textContent=d.boost?'Boost on'+(d.boost_mhz?' · up to '+(d.boost_mhz/1000).toFixed(1)+' GHz':''):'Boost off · max '+((d.base_mhz||0)/1000).toFixed(1)+' GHz';}
 async function call(body){const r=await fetch('/api/cpu-boost',body?{method:'POST',headers:{'Content-Type':'application/json','X-Router-Panel':'1'},body:JSON.stringify(body)}:{cache:'no-store'});const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||'CPU boost unavailable');return d}
 async function refresh(){if(busy)return;try{render(await call())}catch(e){el('cpuBoostNote').textContent=e.message}}
 el('cpuBoost').onchange=async()=>{busy=true;const want=el('cpuBoost').checked;el('cpuBoost').disabled=true;
  try{render(await call({boost:want}))}catch(e){alert(e.message)}finally{busy=false;el('cpuBoost').disabled=false;await refresh()}};
 refresh();setInterval(refresh,30000);
})();
</script>
<script>
/* lte-bands-v1: Frequency bands sub-card. GET /api/wan/bands lists this modem's bands (ModemManager);
   Apply starts the router-wan worker with POST /api/wan {action:'bands'}. */
(()=>{
 const el=id=>document.getElementById(id),box=el('bandsDetails');if(!box)return;
 const postHeaders=()=>({'X-Router-Panel':'1'});
 let data=null,chosen=new Set(),base=null,timer=null,inFlight=false,starting=false,watching=false,since=0,message=null;
 async function call(path,body){const r=await fetch(path,body?{method:'POST',headers:Object.assign({'Content-Type':'application/json'},postHeaders()),body:JSON.stringify(body)}:{cache:'no-store'});if(r.redirected){location='/login';throw Error('Login required')}const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.error||'Band list unavailable');return d}
 const short=b=>b.group==='3g'?'3G '+b.label:b.label;
 const label=id=>{const b=data.bands.find(x=>x.id===id);return b?short(b):id};
 const enabled=()=>data.bands.filter(b=>b.enabled).map(b=>b.id);
 function pending(){const off=[],on=[];for(const b of data.bands){const want=chosen.has(b.id);if(want!==b.enabled)(want?on:off).push(short(b))}return{off,on,count:off.length+on.length}}
 function cell(c,primary){if(!c||!c.band)return'';return c.band+(c.earfcn?' · '+c.earfcn:'')+(c.mhz?' · '+c.mhz+' MHz':'')+(c.pci!=null&&c.pci!==''?' · PCI '+c.pci:'')+(!primary&&c.state?' ('+c.state+')':'')}
 function note(text,warn,good){const n=el('bandsNote');n.textContent=text||'';n.style.color=warn?'#ffd087':good?'#9ce6d8':''}
 function render(){
  if(!data)return;
  const job=data.job||{},running=starting||data.busy||['queued','running'].includes(job.status),can=data.editable&&!running;
  const groups=el('bandsGroups');groups.replaceChildren();
  for(const g of data.groups){
   const items=data.bands.filter(b=>b.group===g.id);if(!items.length)continue;
   const row=document.createElement('div');row.className='bands-group';row.setAttribute('role','group');row.setAttribute('aria-label',g.title);
   const title=document.createElement('div');title.className='bands-title';title.textContent=g.title;
   const pills=document.createElement('div');pills.className='bands-pills';
   for(const b of items){
    const on=chosen.has(b.id),use=data.in_use.includes(b.id),btn=document.createElement('button');
    btn.type='button';btn.className='band'+(use?' serving':'')+(on!==b.enabled?' changed':'');
    btn.setAttribute('aria-pressed',String(on));btn.disabled=!can;
    btn.title=short(b)+(b.mhz?' · '+b.mhz+' MHz':'')+(on?' · on':' · off')+(use?' · in use now':'')+(on!==b.enabled?' · not applied yet':'');
    const name=document.createElement('span');name.textContent=b.label;btn.append(name);
    if(b.mhz){const mhz=document.createElement('small');mhz.textContent=b.mhz;btn.append(mhz)}
    btn.onclick=()=>{if(chosen.has(b.id))chosen.delete(b.id);else chosen.add(b.id);message=null;render()};
    pills.append(btn)}
   row.append(title,pills);groups.append(row)}
  const p=pending(),s=data.serving||{};
  el('bandsSummary').textContent=enabled().length+' of '+data.bands.length+' on'+(data.in_use.length?' · in use '+data.in_use.map(label).join(' + '):'');
  const cells=[cell(s.primary,true),...(s.scells||[]).map(c=>cell(c,false))].filter(Boolean);
  el('bandsServing').textContent=(data.model?data.model+' · ':'')+(cells.length?'In use now: '+cells.join(' + '):'No 4G carrier details right now');
  el('bandsApply').disabled=!can||!p.count;el('bandsUndo').disabled=running||!p.count;el('bandsAll').disabled=!can;
  el('bandsOriginal').hidden=!data.original;el('bandsOriginal').disabled=!can;
  if(running)note(job.status==='running'&&job.message?job.message:'Starting…');
  else if(message)note(message.text,message.warn,message.good);
  else if(p.count)note('Not applied yet: '+[p.off.length?p.off.join(', ')+' off':'',p.on.length?p.on.join(', ')+' on':''].filter(Boolean).join('; '));
  else if(!data.editable)note(data.reason,true);
  else note('');
 }
 async function refresh(){
  if(inFlight)return;inFlight=true;let fast=false;
  try{
   data=await call('/api/wan/bands');const job=data.job||{},key=enabled().join(',');
   fast=data.busy||['queued','running'].includes(job.status);
   if(watching&&!fast&&job.at>=since&&['complete','failed'].includes(job.status)){watching=false;message={text:job.message,warn:job.status!=='complete',good:job.status==='complete'};base=null}
   if(key!==base){base=key;chosen=new Set(enabled())}
   render()}
  catch(e){note(e.message,true);if(!data)el('bandsSummary').textContent='unavailable'}
  finally{inFlight=false;clearTimeout(timer);if(box.open||fast||watching)timer=setTimeout(refresh,fast||watching?2000:10000)}}
 el('bandsApply').onclick=async()=>{
  if(!data)return;const bands=data.bands.filter(b=>chosen.has(b.id)).map(b=>b.id);
  if(!bands.some(id=>id.startsWith('eutran-'))){message={text:'Keep at least one 4G (LTE) band on',warn:true};render();return}
  starting=true;message=null;since=data.checked_at||0;render();
  try{await call('/api/wan',{action:'bands',bands});watching=true}
  catch(e){message={text:e.message,warn:true}}
  finally{starting=false;await refresh()}};
 el('bandsUndo').onclick=()=>{if(!data)return;chosen=new Set(enabled());message=null;render()};
 el('bandsAll').onclick=()=>{if(!data)return;chosen=new Set(data.bands.map(b=>b.id));message=null;render()};
 el('bandsOriginal').onclick=()=>{if(!data)return;chosen=new Set(data.original||[]);message=null;render()};
 box.addEventListener('toggle',()=>{if(box.open)refresh()});
 refresh();
})();
</script>
</body></html>'''

def run(cmd, timeout=45):
    # Own process group: a timeout stops the whole command tree, not only its top process.
    try:
        p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        try:
            out,_=p.communicate(timeout=timeout)
            return p.returncode==0,(out or "").strip(),p.returncode
        except subprocess.TimeoutExpired:
            os.killpg(p.pid,signal.SIGTERM)
            try:out,_=p.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGKILL);out,_=p.communicate()
            return False,((out or "")+"\nTIMEOUT; selected mode retained").strip(),124
    except Exception as e:return False,str(e),125

def current_mode():
    try:return MODE_FILE.read_text().strip() or "unknown"
    except Exception:return "unknown"

def exists_exe(path):return os.path.isfile(path) and os.access(path,os.X_OK)
def iface_exists(name):return os.path.exists(f"/sys/class/net/{name}")
def service_active(name):return run(["/bin/systemctl","is-active","--quiet",name],timeout=3)[0]

def read_counter(iface,kind):
    try:return int(Path(f"/sys/class/net/{iface}/statistics/{kind}_bytes").read_text().strip())
    except Exception:return 0

def raw_lan_counters():return {"rx":read_counter(LAN_IF,"rx"),"tx":read_counter(LAN_IF,"tx")}

def save_counter_baseline(c=None):
    if c is None:c=raw_lan_counters()
    COUNTER_FILE.parent.mkdir(parents=True,exist_ok=True)
    tmp=COUNTER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"rx":int(c["rx"]),"tx":int(c["tx"]),"ts":time.time()}))
    os.replace(tmp,COUNTER_FILE)

def load_counter_baseline():
    cur=raw_lan_counters()
    try:
        d=json.loads(COUNTER_FILE.read_text());brx=int(d.get("rx",cur["rx"]));btx=int(d.get("tx",cur["tx"]))
        if cur["rx"]<brx or cur["tx"]<btx:raise ValueError
        return {"rx":brx,"tx":btx}
    except Exception:
        save_counter_baseline(cur);return cur

def reset_counters():
    global _last_lan_stats
    with _stats_lock:
        _last_lan_stats=None
        save_counter_baseline()

def lan_traffic():
    global _last_lan_stats
    now=time.monotonic();cur=raw_lan_counters();base=load_counter_baseline();ur=dr=0.0
    with _stats_lock:
        if _last_lan_stats:
            dt=max(now-_last_lan_stats[0],.001)
            ur=max(0.0,(cur["rx"]-_last_lan_stats[1])/dt)
            dr=max(0.0,(cur["tx"]-_last_lan_stats[2])/dt)
        _last_lan_stats=(now,cur["rx"],cur["tx"])
    return {"upload_rate":ur,"download_rate":dr,"upload_bytes":max(0,cur["rx"]-base["rx"]),"download_bytes":max(0,cur["tx"]-base["tx"])}

def parse_mihomo_secret():
    try:
        txt=MIHOMO_CFG.read_text(encoding="utf-8",errors="replace")
        m=re.search(r'(?m)^[ \t]*secret:[ \t]*(.*?)[ \t]*$',txt)
        if m:
            v=m.group(1).strip()
            if v in ("","''",'""',"null","~"):return ""
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):return v[1:-1]
            return v
    except Exception:pass
    return ""

def mihomo_proxy():
    if not service_active("mihomo"):return ""
    req=urllib.request.Request(f"http://{LAN_IP}:9090/proxies/PROXY")
    secret=parse_mihomo_secret()
    if secret:req.add_header("Authorization",f"Bearer {secret}")
    try:
        with urllib.request.urlopen(req,timeout=2.0) as r:return json.loads(r.read().decode()).get("now") or ""
    except Exception:return ""

def set_mark(sock, mark):
    if mark:
        # Linux SO_MARK
        sock.setsockopt(socket.SOL_SOCKET,36,int(mark))

def wan_dns_servers():
    servers=[]
    try:
        p=subprocess.run(["/usr/bin/resolvectl","dns",active_interface()],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=2)
        for token in (p.stdout or "").replace(":"," ").split():
            try:
                ip_address(token)
                if "." in token and token not in servers:
                    servers.append(token)
            except Exception:
                pass
    except Exception:
        pass
    return servers

def dns_query(server, mark, name="example.com", timeout=2.0):
    txid=int(time.time()*1000000)&0xffff
    header=struct.pack("!HHHHHH",txid,0x0100,1,0,0,0)
    qname=b"".join(bytes([len(x)])+x.encode() for x in name.split("."))+b"\x00"
    packet=header+qname+struct.pack("!HH",1,1)
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        set_mark(s,mark);s.settimeout(timeout);s.sendto(packet,(server,53));data,_=s.recvfrom(4096)
        if len(data)<12:return False
        rid,flags,qd,an,ns,ar=struct.unpack("!HHHHHH",data[:12])
        return rid==txid and bool(flags&0x8000) and (flags&0xf)==0 and an>0
    except Exception:return False
    finally:s.close()

def dns_check(mode, mark):
    servers=[]
    if mode=="direct":
        servers.extend(wan_dns_servers())
    for s in PUBLIC_DNS:
        if s not in servers:servers.append(s)
    tried=[]
    for server in servers[:4]:
        tried.append(server)
        if dns_query(server,mark):
            return True,server
    return False,", ".join(tried)

def https_connect(domain,path,port,mark):
    # Resolve destination using router control-plane DNS. Only the actual
    # TCP/TLS socket is marked so the dataplane follows the selected mode.
    try:
        infos=socket.getaddrinfo(domain,port,socket.AF_INET,socket.SOCK_STREAM)
    except Exception as e:
        return False,f"resolve {domain}: {e}"
    last=""
    for info in infos[:3]:
        ip=info[4][0]
        raw=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            set_mark(raw,mark);raw.settimeout(4.0);raw.connect((ip,port))
            ctx=ssl.create_default_context()
            tls=ctx.wrap_socket(raw,server_hostname=domain)
            tls.settimeout(4.0)
            req=f"HEAD {path} HTTP/1.1\r\nHost: {domain}\r\nUser-Agent: RouterPanelHealth/2.4\r\nConnection: close\r\n\r\n".encode()
            tls.sendall(req)
            data=tls.recv(32)
            tls.close()
            if data.startswith(b"HTTP/"):
                return True,f"{domain} via {ip}"
            last=f"{domain} via {ip}: no HTTP response"
        except Exception as e:
            last=f"{domain} via {ip}: {e}"
            try:raw.close()
            except Exception:pass
    return False,last

def https_check(mark):
    details=[]
    for domain,path,port in HTTPS_TARGETS:
        ok,detail=https_connect(domain,path,port,mark)
        if ok:return True,detail
        details.append(detail)
    return False,"; ".join(details[-3:])

def health_probe(mode):
    iface=active_interface() if mode=="direct" else ACTIVE_IFACE.get(mode)
    now=time.time()
    if not iface or not iface_exists(iface):
        return {"mode":mode,"level":"fail","ok":False,"internet_ok":False,"dns_ok":None,
                "summary":f"{iface or '?'} is down","last_checked":now}

    mark=MODE_MARK.get(mode,0)
    internet_ok,https_detail=https_check(mark)
    dns_ok,dns_detail=dns_check(mode,mark)

    if internet_ok and dns_ok:
        level="ok";summary=f"HTTPS OK + DNS OK through {mode.upper()}"
    elif internet_ok:
        level="warn";summary=f"Internet OK through {mode.upper()} - DNS test failed"
    else:
        level="fail";summary=f"External HTTPS failed through {mode.upper()}"

    return {"mode":mode,"level":level,"ok":internet_ok,"internet_ok":internet_ok,"dns_ok":dns_ok,
            "summary":summary,"https_detail":https_detail,"dns_detail":dns_detail,"last_checked":now}

def set_checking(mode):
    with _health_lock:
        prev=dict(_health)
        _health.update({"mode":mode,"level":"checking","summary":f"Checking {mode.upper()}...",
                        "internet_ok":False,"ok":False,"dns_ok":None,"last_checked":prev.get("last_checked")})

def apply_probe_result(result):
    now=time.time()
    with _health_lock:
        prev=dict(_health)
        last_ok=prev.get("last_ok")
        failed_since=prev.get("failed_since")
        if result.get("internet_ok"):
            last_ok=now;failed_since=None
        else:
            if prev.get("mode")!=result.get("mode") or not failed_since:
                failed_since=now
        _health.clear()
        _health.update(result)
        _health["last_ok"]=last_ok
        _health["failed_since"]=failed_since

def trigger_health():
    set_checking(current_mode())
    _health_trigger.set()

def health_worker():
    # Immediate first check, then every 8 seconds, or immediately on trigger.
    while not _health_stop.is_set():
        mode=current_mode()
        set_checking(mode)
        result=health_probe(mode)
        apply_probe_result(result)
        _health_trigger.wait(8.0)
        _health_trigger.clear()

def health_snapshot():
    with _health_lock:return dict(_health)

def status_payload():
    m=current_mode()
    return {"ok":True,"mode":m,
            "interfaces":{x:iface_exists(x) for x in ("wg0","tun0","mihomo0")},
            "traffic":lan_traffic(),"health":health_snapshot(),"mode_action":read(ROOT/"mode-action.json",{}),
            "mihomo":{"active":service_active("mihomo"),"proxy":mihomo_proxy(),"available":exists_exe("/usr/local/sbin/net-mihomo")},
            "actions":{"awg":exists_exe("/usr/local/sbin/watchdog-rotator"),
                       "tun":exists_exe("/usr/local/sbin/tun-rotator")},
            "ts":time.time()}

def client_allowed(addr):
    try:
        a=ip_address(addr)
        return a in LAN_NET or any(a in n for n in TRUSTED_NETS)
    except Exception:return False

class Handler(BaseHTTPRequestHandler):
    server_version="RouteDeck/3.0.10"
    def log_message(self,fmt,*args):print(f"{self.client_address[0]} - {fmt%args}",flush=True)
    def allowed(self):
        if not (self.client_address[0] in ("127.0.0.1","::1") or client_allowed(self.client_address[0])):return False
        # Answer only to this panel's own addresses: a page under a foreign name (DNS rebinding) is refused.
        host=(self.headers.get("Host") or "").rsplit(":",1)[0]
        return not host or host in ("127.0.0.1","localhost",LAN_IP,self.connection.getsockname()[0])
    def j(self,obj,status=200):
        b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("X-Frame-Options","DENY");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def h(self,text,status=200):
        b=text.encode();self.send_response(status);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("X-Frame-Options","DENY");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def body(self):
        n=int(self.headers.get("Content-Length","0"))
        if n<1 or n>4096:raise ValueError
        return json.loads(self.rfile.read(n).decode())
    def authorized(self):
        if self.headers.get("X-Router-Panel")!="1":return False
        o=self.headers.get("Origin")
        return not o or o in (f"http://{LAN_IP}:{PORT}",f"http://{LAN_IP}",f"http://{self.connection.getsockname()[0]}:{PORT}")

    def do_GET(self):
        if not self.allowed():return self.j({"ok":False,"error":"LAN access only"},403)
        if wan_speedtest.get(self):return
        if cpu_boost.get(self):return
        if panel_wan.get(self):return
        if panel_extra.get(self):return
        if self.path in ("/","/index.html"):return self.h(index_page())
        if self.path=="/api/status":return self.j(status_payload())
        if self.path=="/api/network":return self.j({"ok":True,"network":panel_system.network_status(mihomo_proxy() if current_mode() == 'mihomo' else None)})
        if self.path=="/api/updates":
            try:return self.j(updater_ui.status())
            except Exception as e:return self.j({"ok":False,"error":str(e)},500)
        if self.path=="/healthz":return self.j({"ok":True})
        return self.j({"ok":False,"error":"not found"},404)

    def do_POST(self):
        if wan_speedtest.post(self):return
        if cpu_boost.post(self):return
        if panel_wan.post(self):return
        if panel_extra.post(self):return
        if self.path in ("/api/reboot","/api/network/refresh"):
            if not self.allowed():return self.j({"ok":False,"error":"LAN access only"},403)
            if not self.authorized():return self.j({"ok":False,"error":"request rejected"},403)
            try:
                body=self.body()
                if self.path=="/api/reboot":return self.j(panel_system.request_reboot(body),202)
                panel_system.invalidate_geo()
                return self.j({"ok":True})
            except (ValueError,TypeError):return self.j({"ok":False,"error":"Explicit confirmation required"},400)
            except RuntimeError as e:return self.j({"ok":False,"error":str(e)},409)
            except Exception as e:return self.j({"ok":False,"error":str(e)},500)
        if self.path=="/api/updates/action":
            if not self.allowed():return self.j({"ok":False,"error":"LAN access only"},403)
            if not self.authorized():return self.j({"ok":False,"error":"request rejected"},403)
            try:return self.j(updater_ui.launch(self.body()),202)
            except (ValueError,TypeError,AttributeError):return self.j({"ok":False,"error":"Invalid update request"},400)
            except RuntimeError as e:return self.j({"ok":False,"error":str(e)},409)
            except Exception as e:return self.j({"ok":False,"error":str(e)},500)
        try:
            if self.path in ("/api/mode","/api/action"):
                with updater_ui.mode_guard():return self._router_do_POST()
            return self._router_do_POST()
        except RuntimeError as e:return self.j({"ok":False,"error":str(e)},409)

    def _router_do_POST(self):
        if not self.allowed():return self.j({"ok":False,"error":"LAN access only"},403)
        if not self.authorized():return self.j({"ok":False,"error":"request rejected"},403)
        try:b=self.body()
        except Exception:return self.j({"ok":False,"error":"invalid JSON"},400)

        if self.path=="/api/counters/reset":
            reset_counters()
            return self.j({"ok":True,"message":"Traffic counters reset"})

        if self.path=="/api/health/check":
            trigger_health()
            return self.j({"ok":True,"message":"External health check started"})

        if self.path=="/api/mode":
            target=b.get("mode")
            if target not in COMMANDS:return self.j({"ok":False,"error":"invalid mode"},400)
            cmd=COMMANDS[target]
            if not exists_exe(cmd[0]):return self.j({"ok":False,"error":f"{cmd[0]} is not installed"},409)
            ok,out,rc=run(["/usr/bin/env","ROUTER_UPDATER_LOCK_HELD=1",*cmd],180);actual=current_mode()
            trigger_health();panel_system.invalidate_geo()
            if not ok or actual!=target:
                return self.j({"ok":False,"error":(f"{target.upper()} was not started; {actual.upper()} is active." if actual!=target else f"{actual.upper()} remains selected; startup failed. Use Restart/Rotate or select another mode.")+"\n"+(out.strip().splitlines() or [""])[-1][-600:],"output":out[-5000:]},500)
            reset_counters();trigger_health();panel_system.invalidate_geo()
            return self.j({"ok":True,"message":f"Mode switched to {target.upper()} - checking Internet","output":out[-5000:]})

        if self.path=="/api/action":
            target=b.get("target");action=b.get("action");cmd=ROTATORS.get((target,action))
            if not cmd:return self.j({"ok":False,"error":"invalid action"},400)
            if current_mode()!=target:return self.j({"ok":False,"error":f"{target.upper()} is not the active mode"},409)
            if not exists_exe(cmd[0]):return self.j({"ok":False,"error":f"{cmd[0]} is not installed"},409)
            ok,out,rc=run(["/usr/bin/env","ROUTER_UPDATER_LOCK_HELD=1",*cmd],180)
            if not ok:return self.j({"ok":False,"error":f"{target} {action} command failed","output":out[-5000:]},500)
            trigger_health();panel_system.invalidate_geo()
            return self.j({"ok":True,"message":f"{target.upper()} {action} complete - checking Internet","output":out[-5000:]})

        return self.j({"ok":False,"error":"not found"},404)

def main():
    if os.geteuid()!=0:raise SystemExit("router-panel must run as root")
    load_counter_baseline()
    t=threading.Thread(target=health_worker,name="router-health",daemon=True)
    t.start()
    h=ThreadingHTTPServer(("0.0.0.0",PORT),Handler);h.daemon_threads=True
    print(f"Router panel v2.4 listening on 0.0.0.0:{PORT}, LAN URL http://{LAN_IP}:{PORT}/",flush=True)
    try:h.serve_forever()
    finally:_health_stop.set();_health_trigger.set()

if __name__=="__main__":main()
