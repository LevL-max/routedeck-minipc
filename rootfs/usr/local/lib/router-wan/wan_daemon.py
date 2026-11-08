"""Internet-based failover; three failed samples, 120 seconds stable preferred WAN."""
import concurrent.futures
import time
from pathlib import Path
from wan_common import ACTIVE, MODE_ENGINE, ROOT, active, atomic, locks, read, run, settings
import wan_control as control
import wan_modem as modem
import wan_network as net
import wan_whitelist as whitelist  # lte-whitelist-v1

class Decision:
    def __init__(self):
        self.failed = {'ethernet': 0, 'lte': 0}
        self.since = {'ethernet': None, 'lte': None}
        self.last_attempt = -10000
        self.signature = None
        self.last_sample = None

    def step(self, now, current, cfg, checks):
        signature = (current, cfg['preferred'], cfg['fallback'])
        if signature != self.signature or (self.last_sample is not None and now-self.last_sample>30):
            self.failed = dict.fromkeys(self.failed, 0)
            self.since = dict.fromkeys(self.since)
            self.signature = signature
        self.last_sample = now
        for key in self.failed:
            if checks.get(key, {}).get('ok') is True:
                self.failed[key] = 0
                if self.since[key] is None: self.since[key] = now
            else:
                self.failed[key] += 1
                self.since[key] = None
        if not cfg['fallback'] or now - self.last_attempt < 30: return None
        other = 'lte' if current == 'ethernet' else 'ethernet'
        # wan-force-v1: a WAN with no link at all gives way to one that has a link, verified or not.
        down = checks.get(current, {}).get('link') is False and checks.get(other, {}).get('link') is True
        if self.failed[current] >= 3 and (checks.get(other, {}).get('ok') or down):
            self.last_attempt = now
            return other, ('fallback: active WAN failed three internet checks' if checks.get(other, {}).get('ok')
                           else 'fallback: active WAN has no link')
        preferred = cfg['preferred']
        if preferred != current and self.since[preferred] is not None and now-self.since[preferred] >= 120:
            self.last_attempt = now
            return preferred, 'failback: preferred WAN stable for 120 seconds'
        return None

PUBLIC = Path('/run/router/wan-status.json')


def publish(state, cfg, paused):
    """World-readable summary for unprivileged health checks (router-agent, router-web).
    No addresses, SIM or APN data: which WAN carries traffic and whether each one works."""
    checks, mobile = state.get('checks', {}), state.get('mobile') or {}
    atomic(PUBLIC, {
        'updated': state['updated'], 'active': state['active'].get('wan'),
        'interface': state['active'].get('interface'), 'preferred': cfg['preferred'],
        'fallback': cfg['fallback'], 'fallback_paused': paused,
        'ethernet_ok': checks.get('ethernet', {}).get('ok'), 'lte_ok': checks.get('lte', {}).get('ok'),
        'lte_error': (mobile.get('error') or checks.get('lte', {}).get('error') or '')[:200],
        'lte_interface': mobile.get('interface'),
        'mobile': {k: mobile.get(k) for k in ('operator', 'technology', 'signal_percent', 'registration', 'state')},
    }, mode=0o644)


def main():
    decision = Decision()
    while True:
        start = time.monotonic()
        try:
            if control.busy():
                time.sleep(2); continue
            with locks():
                eth = net.ethernet()
                try:
                    mobile, lte = modem.ensure()
                except Exception as exc:
                    mobile, lte = {'error': str(exc)[:300]}, None
                net.prepare(eth, lte)
                net.firewall()
                selected = active()
                chosen = eth if selected['wan'] == 'ethernet' else lte
                # Reconcile DHCP/bearer renewal without restarting the mode or its tunnels.
                net.select(selected['wan'], chosen)
                control.bind_awg()
                control.adapt_mtu()
                control.follow_dns()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = {key: pool.submit(net.probe, key, data) for key, data in (('ethernet', eth), ('lte', lte))}
                checks = {key: future.result() for key, future in futures.items()}
            if selected != active(): continue
            cfg = settings()
            recovery = read(control.FAILED, {})
            # wan-force-v1: Force pins the selected WAN; the samples still count for the panel.
            rules = dict(cfg, fallback=cfg['fallback'] and cfg.get('force') is not True)
            choice = decision.step(time.monotonic(), selected['wan'], rules, checks) if not recovery else None
            state = {'updated': int(time.time()), 'active': selected, 'checks': checks, 'mobile': mobile,
                     'failed_samples': decision.failed,
                     'stable_seconds': {key: round(time.monotonic()-value) if value is not None else 0
                                        for key, value in decision.since.items()}}
            if recovery: state['daemon_error'] = recovery['message']
            state['whitelist'] = whitelist.tick({'ethernet': eth, 'lte': lte})  # lte-whitelist-v1
            atomic(ROOT/'status.json', state)
            if MODE_ENGINE == 'apply-mode': publish(state, cfg, bool(recovery))
            if choice and not control.busy():
                with locks():
                    target, reason = choice
                    atomic(ROOT/'request.json', {'action': 'auto', 'wan': target, 'reason': reason,
                                                'generation': active().get('generation')})
                    run(['systemctl', 'start', '--no-block', 'router-wan-worker.service'])
        except BlockingIOError:
            # An updater or user operation owns the router. Do not compete with it.
            pass
        except Exception as exc:
            prior = read(ROOT/'status.json', {})
            atomic(ROOT/'status.json', dict(prior, daemon_error=str(exc)[:250]))
        time.sleep(max(1, 10-(time.monotonic()-start)))

if __name__ == '__main__': main()
