#!/usr/bin/env python3
"""Hermes SI fpk 自验证 harness（开发自验证，不等用户真机点）.

层级：
  L0 包体静态审计   manifest版本/app.tgz零链接/补丁标记/cmd语法
  L1 真包启动回归   解包->start->端口就绪->index注入->status 0->stop->status 3
  L2 浏览器端到端   sock_proxy 桥 + headless chromium CDP：全响应无4xx、TUI渲染、WS建立
  L3 消息真往返     CDP 向 xterm 注入消息 -> agent 日志出现 inbound+API call+response ready，零401
  L4 崩溃类复现     官方启动器同款命令（-I + 弹三变量）gateway status exit 0

用法: python3 verify.py --tiers 0,1,4 [--keep]   (默认结束自动清理 sim/chromium)
铁律: 绝不触碰生产实例（9119 及 hermes-si.sock）；sim 用独立端口/目录。
"""
import argparse, json, os, queue, re, shutil, socket, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, '/vol2/@apphome/hermes-agent/data/venv/lib/python3.11/site-packages')
import websocket  # websocket-client

HERE = Path(__file__).resolve().parent
SIM = HERE / 'sim'
FPK = HERE / 'pkg' / 'hermes-si.fpk'
PREFIX = '/app/hermes-si'
KEY_ENV = '/vol2/@apphome/hermes-agent/data/.env'
KEY_NAME = 'CUSTOM_UMV4M5OPLH7KI_API_KEY'
ENGINE = 'http://127.0.0.1:8080/v1'
MODEL = 'qwen3.8-flash-next-iq3_s'
PROD_PORT = '9119'
FAILS = []

def ok(tag, cond, detail=''):
    print(f'{"PASS" if cond else "FAIL"} {tag}' + (f' | {detail}' if detail else ''))
    if not cond:
        FAILS.append(tag)

def engine_key():
    for line in open(KEY_ENV):
        if line.upper().startswith(KEY_NAME):
            return line.split('=', 1)[1].strip()
    raise SystemExit('engine key missing in .env')

def free_port(base):
    for p in range(base, base + 30):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            return p
        except OSError:
            pass
        finally:
            s.close()
    raise SystemExit('no free port')

def sh(args, env=None, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)

def port_up(p):
    return f':{p} ' in sh(['ss', '-tln']).stdout

def sim_env():
    e = dict(os.environ)
    e['TRIM_APPDEST'] = str(SIM / 'target')
    e['TRIM_PKGVAR'] = str(SIM / 'pkgvar')
    e['TRIM_PKGHOME'] = str(SIM / 'pkghome')
    return e

def kill_orphan_sims():
    """Kill dashboard processes whose cwd is under SIM (incl. deleted-cwd orphans).
    Pattern-based matching would never hit them after rmtree; cwd scan does, and
    can never hit production (its cwd is under /vol2/@appcenter/hermes-si)."""
    for pid in [x for x in subprocess.run(['pgrep', '-f', 'hermes_cli.main dashboard'],
                                          capture_output=True, text=True).stdout.split()]:
        try:
            cwd = os.readlink(f'/proc/{pid}/cwd')
        except OSError:
            continue
        if cwd.startswith(str(SIM)):
            print(f'KILL orphan pid={pid} cwd={cwd}')
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
    time.sleep(3)

def unpack():
    kill_orphan_sims()
    shutil.rmtree(SIM, ignore_errors=True)
    for d in ('target', 'pkgvar', 'pkghome'):
        (SIM / d).mkdir(parents=True)
    sh(['tar', '-xzf', str(FPK), '-C', str(SIM)])
    sh(['tar', '-xzf', str(SIM / 'app.tgz'), '-C', str(SIM / 'target')])

def write_sim_config():
    hh = SIM / 'pkghome' / 'data' / 'hermes-home'
    hh.mkdir(parents=True, exist_ok=True)
    (hh / 'config.yaml').write_text(
        'model:\n  provider: custom\n'
        f'  default: {MODEL}\nproviders:\n  custom:\n'
        f'    base_url: {ENGINE}\n    api_key: {engine_key()}\n'
        f'    default_model: {MODEL}\n    api_mode: chat_completions\n')

def sim_start(port):
    if not (SIM / 'target' / 'src').is_dir():
        unpack()
    if not (SIM / 'pkghome' / 'data' / 'hermes-home' / 'config.yaml').is_file():
        write_sim_config()
    (SIM / 'pkgvar' / 'http_port').write_text(str(port))
    sh(['bash', str(SIM / 'cmd' / 'main'), 'start'], env=sim_env(), timeout=90)
    for _ in range(90):
        if port_up(port):
            return True
        time.sleep(1)
    return False

def sim_stop():
    sh(['bash', str(SIM / 'cmd' / 'main'), 'stop'], env=sim_env(), timeout=60)

def sim_status():
    return sh(['bash', str(SIM / 'cmd' / 'main'), 'status'], env=sim_env()).returncode

def index_probe(port, host_header=True):
    args = ['curl', '-s', '--max-time', '6']
    if host_header:
        args += ['-H', f'Host: 127.0.0.1:{port}']
    args.append(f'http://127.0.0.1:{port}/')
    return sh(args).stdout

def extract_token(html):
    m = re.search(r'__HERMES_SESSION_TOKEN__="([^"]+)"', html)
    return m.group(1) if m else None

def log_lines(port, token, limit=500):
    r = sh(['curl', '-s', '--max-time', '8', '-H', f'Host: 127.0.0.1:{port}',
            '-H', f'Authorization: Bearer {token}',
            f'http://127.0.0.1:{port}/api/logs?file=agent&limit={limit}'])
    try:
        return json.loads(r.stdout).get('lines', [])
    except Exception:
        return []

# ---------- L0 ----------
def tier0():
    ver = re.search(r'^version\s*=\s*(\S+)', sh(['tar', '-xOf', str(FPK), 'manifest']).stdout, re.M).group(1)
    ok('L0.manifest-version', bool(ver), ver)
    sh(['tar', '-xzf', str(FPK), '-C', str(SIM)]) if not (SIM / 'app.tgz').is_file() else None
    links = sh(['tar', '-tzf', str(SIM / 'app.tgz')]).stdout
    n_link = sum(1 for l in links.splitlines() if l.count(':') > 1)
    ok('L0.zero-inner-links', n_link == 0, f'link-entries={n_link}')
    t0 = SIM / 'target'
    if not (t0 / 'src').is_dir():
        unpack()
        t0 = SIM / 'target'
    pth = list((t0 / 'python314' / 'lib').rglob('hermes_si_venv.pth'))
    ok('L0.pth-patch-present', bool(pth))
    stamp_f = t0 / 'src' / 'install-stamp.json'
    try:
        stamp = json.loads(stamp_f.read_text())
    except Exception:
        stamp = {}
    ok('L0.version-stamp', stamp.get('updateMechanism') == 'app-installer'
       and bool(stamp.get('baseVersion')) and bool(stamp.get('commit'))
       and set(str(stamp.get('commit'))) != {'0'},
       f"base={stamp.get('baseVersion')} display={stamp.get('displayVersion')}")
    idx = next((t0 / 'src' / 'hermes_cli' / 'web_dist' / 'assets').glob('index-*.js'), None)
    body = idx.read_text(errors='ignore') if idx else ''
    ok('L0.preload-prefix-fix', body.count('modulepreload') > 0 and 'return`/`+' not in body,
       f'index={idx.name if idx else None}')
    syntax = [f.name for f in (SIM / 'cmd').iterdir() if sh(['bash', '-n', str(f)]).returncode]
    ok('L0.cmd-syntax', not syntax, str(syntax))

# ---------- L1 ----------
def tier1():
    unpack()
    write_sim_config()
    port = free_port(9120)
    ok('L1.start+bind', sim_start(port), f'port={port}')
    html = index_probe(port)
    ok('L1.index-token', '__HERMES_SESSION_TOKEN__=' in html)
    ok('L1.status-0', sim_status() == 0)
    sim_stop()
    time.sleep(3)
    ok('L1.stop+status-3', sim_status() == 3 and not port_up(port))
    ok('L1.prod-untouched', port_up(PROD_PORT))
    (SIM / 'port').write_text(str(port))

# ---------- bridge + CDP ----------
def start_bridge(listen, unix):
    srv = socket.socket(socket.AF_INET)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', listen))
    srv.listen(16)
    def pipe(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except OSError:
            pass
        finally:
            try:
                b.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    def accept():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                u = socket.socket(socket.AF_UNIX)
                u.connect(str(unix))
            except OSError:
                c.close()
                continue
            threading.Thread(target=pipe, args=(c, u), daemon=True).start()
            threading.Thread(target=pipe, args=(u, c), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    return srv

class CDP:
    def __init__(self, port):
        self.ws = None
        self.mid = 0
        self.ev = queue.Queue()
    def start(self, port):
        url = sh(['curl', '-s', '-X', 'PUT', f'http://127.0.0.1:{port}/json/new?url=about:blank'])
        raw = url.stdout
        if raw.strip().startswith('{'):
            target = json.loads(raw)
        elif raw.strip().startswith('['):
            target = json.loads(raw)[0]
        else:
            target = json.loads(sh(['curl', '-s', f'http://127.0.0.1:{port}/json/list']).stdout)[0]
        self.ws = websocket.create_connection(target['webSocketDebuggerUrl'],
                                              suppress_origin=True, max_size=None, timeout=60)
        def rd():
            while True:
                try:
                    m = json.loads(self.ws.recv())
                except Exception:
                    return
                self.ev.put(m)
        threading.Thread(target=rd, daemon=True).start()
    def cmd(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({'id': self.mid, 'method': method, 'params': params}))
        while True:
            m = self.ev.get(timeout=60)
            if m.get('id') == self.mid:
                return m.get('result', {})
            self.ev.put(m)  # not ours: re-queue for wait_ev/drain loops
    def wait_ev(self, method, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                m = self.ev.get(timeout=1)
            except queue.Empty:
                continue
            if m.get('method') == method:
                return m
        return None

def click_text(c, text):
    """Find element by visible text, click center with CDP mouse events. Returns True on click."""
    g = c.cmd('Runtime.evaluate',
              expression="(function(t){const els=[...document.querySelectorAll('a,button,[role=menuitem]')];"
                         "const el=els.find(e=>e.textContent.trim().toUpperCase()===t.toUpperCase()"
                         "&&e.getBoundingClientRect().width>0);if(!el)return null;"
                         "el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();"
                         "return Math.round(r.x+r.width/2)+','+Math.round(r.y+r.height/2);})('" + text + "')")
    coords = g.get('result', {}).get('value')
    if not coords:
        return False
    x, y = map(int, coords.split(','))
    for t in ('mousePressed', 'mouseReleased'):
        c.cmd('Input.dispatchMouseEvent', type=t, x=x, y=y, button='left', clickCount=1,
              buttons=1 if t == 'mousePressed' else 0)
    return True

def tier2():
    port = int((SIM / 'port').read_text()) if (SIM / 'port').is_file() else free_port(9120)
    if not port_up(port):
        ok('L2.sim-start', sim_start(port), f'port={port}')
    else:
        ok('L2.sim-already-up', True, f'port={port}')
    sock = SIM / 'target' / 'hermes-si.sock'
    br = free_port(18700)
    srv = start_bridge(br, sock)
    hp = index_probe(br) if False else None
    # proxied page injects base path; use plain curl first
    prox = sh(['curl', '-s', '--max-time', '6', f'http://127.0.0.1:{br}{PREFIX}/']).stdout
    ok('L2.proxy-base-inject', f'__HERMES_BASE_PATH__="{PREFIX}"' in prox)
    cdp = free_port(18740)
    chr_dir = SIM / 'chr'
    shutil.rmtree(chr_dir, ignore_errors=True)
    chrome = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--no-sandbox', '--disable-gpu',
                               '--remote-allow-origins=*', '--window-size=1920,1080',
                               f'--user-data-dir={chr_dir}',
                               f'--remote-debugging-port={cdp}', 'about:blank'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if sh(['curl', '-s', '--max-time', '2', f'http://127.0.0.1:{cdp}/json/version']).stdout.strip():
            break
        time.sleep(1)
    c = CDP(cdp)
    c.start(cdp)
    c.cmd('Page.enable'); c.cmd('Runtime.enable'); c.cmd('Network.enable')
    c.cmd('Emulation.setDeviceMetricsOverride', width=1920, height=1080, deviceScaleFactor=1, mobile=False)
    c.cmd('Page.navigate', url=f'http://127.0.0.1:{br}{PREFIX}/')
    ok('L2.page-load', c.wait_ev('Page.loadEventFired', 45) is not None)
    time.sleep(8)
    # dismiss telemetry consent popup if present
    click_text(c, 'No thanks')
    bad, ws_seen = [], 0
    while True:
        try:
            m = c.ev.get(timeout=1)
        except queue.Empty:
            break
        p = m.get('params', {})
        if m.get('method') == 'Network.responseReceived':
            r = p['response']
            if r['status'] >= 400:
                bad.append(f"{r['status']} {r['url']}")
        if m.get('method') == 'Network.webSocketCreated':
            ws_seen += 1
    ok('L2.no-http-errors', not bad, str(bad[:3]))
    # chat page: click CHAT, THEN ws + xterm appear (landing page hosts no TUI)
    ok('L2.nav-chat', click_text(c, 'CHAT'), 'nav item not found')
    time.sleep(8)
    ws_ok = xterm_ok = False
    for _ in range(20):
        g = c.cmd('Runtime.evaluate', expression='!!document.querySelector(".xterm")')
        xterm_ok = g.get('result', {}).get('value') is True
        while True:
            try:
                m = c.ev.get(timeout=1)
            except queue.Empty:
                break
            if m.get('method') == 'Network.webSocketCreated':
                ws_seen += 1
        ws_ok = ws_seen > 0
        if xterm_ok and ws_ok:
            break
        time.sleep(2)
    ok('L2.websocket-up', ws_ok, f'ws-created={ws_seen}')
    ok('L2.tui-rendered', xterm_ok)
    # nav away & back: catch SPA route breakage / 404 on lazy chunks
    bad.clear()
    for label in ('SESSIONS', 'CHAT'):
        click_text(c, label)
        time.sleep(4)
    while True:
        try:
            m = c.ev.get(timeout=1)
        except queue.Empty:
            break
        p = m.get('params', {})
        if m.get('method') == 'Network.responseReceived' and p['response']['status'] >= 400:
            bad.append(f"{p['response']['status']} {p['response']['url']}")
    ok('L2.nav-no-404', not bad, str(bad[:3]))
    term = c.cmd('Runtime.evaluate', expression='!!document.querySelector(".xterm")')
    ok('L2.chat-back-tui', term.get('result', {}).get('value') is True)
    chrome.terminate()
    srv.close()

# ---------- L3 ----------
def tier3():
    port = int((SIM / 'port').read_text()) if (SIM / 'port').is_file() else free_port(9120)
    if not port_up(port):
        ok('L3.sim-start', sim_start(port), f'port={port}')
    sock = SIM / 'target' / 'hermes-si.sock'
    br = free_port(18700)
    srv = start_bridge(br, sock)
    cdp = free_port(18740)
    chr_dir = SIM / 'chr3'
    shutil.rmtree(chr_dir, ignore_errors=True)
    chrome = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--no-sandbox', '--disable-gpu',
                               '--remote-allow-origins=*', '--window-size=1920,1080',
                               f'--user-data-dir={chr_dir}',
                               f'--remote-debugging-port={cdp}', 'about:blank'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if sh(['curl', '-s', '--max-time', '2', f'http://127.0.0.1:{cdp}/json/version']).stdout.strip():
            break
        time.sleep(1)
    c = CDP(cdp)
    c.start(cdp)
    c.cmd('Page.enable'); c.cmd('Runtime.enable')
    c.cmd('Emulation.setDeviceMetricsOverride', width=1920, height=1080, deviceScaleFactor=1, mobile=False)
    c.cmd('Page.navigate', url=f'http://127.0.0.1:{br}{PREFIX}/')
    ok('L3.page-load', c.wait_ev('Page.loadEventFired', 45) is not None)
    time.sleep(8)
    click_text(c, 'No thanks')
    ok('L3.nav-chat', click_text(c, 'CHAT'), 'nav item not found')
    time.sleep(10)
    g = c.cmd('Runtime.evaluate',
              expression="(function(){const t=document.querySelector('.xterm-helper-textarea');"
                         'if(!t)return null;t.focus();const r=t.getBoundingClientRect();'
                         'return Math.round(r.x+r.width/2)+","+Math.round(r.y+r.height/2);})()')
    coords = g.get('result', {}).get('value')
    ok('L3.xterm-focusable', coords is not None)
    marker = f'selfcheck{int(time.time())}'
    if coords:
        x, y = map(int, coords.split(','))
        for t in ('mousePressed', 'mouseReleased'):
            c.cmd('Input.dispatchMouseEvent', type=t, x=x, y=y, button='left', clickCount=1, buttons=1 if t == 'mousePressed' else 0)
    msg = f'ping {marker}'
    for ch in msg:
        c.cmd('Input.dispatchKeyEvent', type='keyDown', text=ch, key=ch, windowsVirtualKeyCode=ord(ch.upper()))
        c.cmd('Input.dispatchKeyEvent', type='keyUp', key=ch, windowsVirtualKeyCode=ord(ch.upper()))
    c.cmd('Input.dispatchKeyEvent', type='rawKeyDown', key='Enter', code='Enter', windowsVirtualKeyCode=13)
    c.cmd('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter', windowsVirtualKeyCode=13)
    html = index_probe(port)
    tok = extract_token(html)
    t_start = time.time()
    inbound = reply = auth_err = False
    while time.time() - t_start < 180:
        time.sleep(5)
        lines = '\n'.join(log_lines(port, tok, limit=400))
        if marker in lines:
            inbound = True
        if re.search(r'API call #\d+:', lines) and re.search(r'(response ready|tui turn finished)', lines):
            reply = True
        if 'AuthenticationError' in lines or 'missing or wrong API key' in lines:
            auth_err = True
        if inbound and reply:
            break
    ok('L3.message-inbound', inbound, f'marker={marker}')
    ok('L3.model-reply', reply, f'{int(time.time()-t_start)}s')
    if not reply:
        lines = log_lines(port, tok, limit=800)
        pat = re.compile(r'inbound|API call|Turn ended|ERROR|Traceback|gateway|Turn|agent')
        for l in lines[-40:]:
            if pat.search(l):
                print('DIAG |', l[:160])
    ok('L3.zero-auth-error', not auth_err)
    chrome.terminate()
    srv.close()
    sim_stop()
    time.sleep(3)
    ok('L3.prod-untouched', port_up(PROD_PORT))

# ---------- L4 ----------
def tier4():
    if not (SIM / 'target' / 'src').is_dir():
        unpack()
    code = ("import os, sys, runpy; os.environ.pop('PYTHONHOME', None); "
            "os.environ.pop('PYTHONPATH', None); os.environ.pop('VIRTUAL_ENV', None); "
            f"sys.path.insert(0, '{SIM}/target/src'); sys._hermes_pin_default_home = True; "
            "import hermes_bootstrap; os.environ['HERMES_HOME'] = "
            f"'{SIM}/pkghome/data/hermes-home'; runpy.run_module('hermes_cli.main', "
            "run_name='__main__', alter_sys=True)")
    r = sh([str(SIM / 'target' / 'python314' / 'bin' / 'python3.14'), '-I', '-c', code, 'gateway', 'status'],
           timeout=120)
    out = r.stdout + r.stderr
    ok('L4.launcher-repro-no-import-error', 'No module named' not in out and r.returncode == 0,
       out.strip().splitlines()[-1] if out.strip() else 'empty')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tiers', default='0,1,2,3,4')
    ap.add_argument('--keep', action='store_true')
    a = ap.parse_args()
    tiers = [int(x) for x in a.tiers.split(',')]
    kill_orphan_sims()
    t0 = time.time()
    for t in tiers:
        {0: tier0, 1: tier1, 2: tier2, 3: tier3, 4: tier4}[t]()
    if not a.keep:
        kill_orphan_sims()
        shutil.rmtree(SIM, ignore_errors=True)
    print(f'\n{"ALL-PASS" if not FAILS else "FAILED: " + ",".join(FAILS)} | {int(time.time()-t0)}s')
    sys.exit(1 if FAILS else 0)

if __name__ == '__main__':
    main()
