"""Домашний сервер «Планировщика поездок».

Раздаёт приложение (index.html) и хранит поездки в data/trips.json,
чтобы все устройства в вашей сети видели и редактировали одни и те же поездки.

Запуск:  python server.py   (или start.bat)
Настройки: server_config.json  — порт и пароль (пустой пароль = без пароля).
Доступ разрешён только из локальной сети (и из Tailscale), запросы из интернета отклоняются.
"""
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
DATA_FILE = os.path.join(DATA_DIR, 'trips.json')
CONFIG_FILE = os.path.join(ROOT, 'server_config.json')

DEFAULT_CONFIG = {'port': 8080, 'password': '', 'allow_internet': False}
LOCK = threading.Lock()
MAX_BODY = 20 * 1024 * 1024  # one trip, with routes and photos links, is far below this
# only the app itself is served as files — never data/, the config with the password, backups or other files
STATIC_FILES = {'index.html'}
STATIC_DIRS = ('lib',)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    with open(CONFIG_FILE, encoding='utf-8') as f:
        return {**DEFAULT_CONFIG, **json.load(f)}


CONFIG = load_config()


def load_db():
    if not os.path.exists(DATA_FILE):
        return {'rev': 0, 'trips': {}, 'deleted': {}}
    with open(DATA_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_db(db):
    os.makedirs(DATA_DIR, exist_ok=True)
    # one backup per day, just in case
    backup = os.path.join(DATA_DIR, f'backup-{date.today().isoformat()}.json')
    if os.path.exists(DATA_FILE) and not os.path.exists(backup):
        shutil.copyfile(DATA_FILE, backup)
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


DB = load_db()

PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    '169.254.0.0/16', '100.64.0.0/10',  # 100.64/10 — Tailscale
    '::1/128', 'fc00::/7', 'fe80::/10')]


def is_private(addr):
    try:
        ip = ipaddress.ip_address(addr.split('%')[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in PRIVATE_NETS)
    except ValueError:
        return False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_request(self, code='-', size='-'):
        # keep the console quiet: show only errors
        if str(code).isdigit() and int(code) >= 400:
            sys.stderr.write('%s  %s  %s\n' % (self.client_address[0], self.requestline, code))

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    # ---- guards ----
    def allowed(self):
        if not CONFIG.get('allow_internet') and not is_private(self.client_address[0]):
            self.send_error(403, 'Only local network')
            return False
        # DNS rebinding: a web page must not reach this server under its own domain name
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
        if not CONFIG.get('allow_internet') and not host_ok(host):
            self.send_error(403, 'Unknown host')
            return False
        return True

    def static_ok(self):
        # resolve the real file and allow only index.html and lib/… (case-insensitive file systems, %xx, ../ included)
        real = os.path.realpath(self.translate_path(self.path))
        rel = os.path.relpath(real, ROOT).replace(os.sep, '/')
        if rel == '.':
            return True  # «/» → index.html
        low = rel.lower()
        return low in STATIC_FILES or any(low.startswith(d + '/') for d in STATIC_DIRS)

    def authed(self):
        pw = CONFIG.get('password') or ''
        if pw and self.headers.get('X-Trip-Key', '') != pw:
            self.json(401, {'error': 'password'})
            return False
        return True

    def json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            raise ValueError('too large')
        return json.loads(self.rfile.read(n) or b'{}')

    # ---- routes ----
    def do_GET(self):
        if not self.allowed():
            return
        path = urlparse(self.path).path
        if path == '/api/trips':
            if not self.authed():
                return
            with LOCK:
                return self.json(200, {'rev': DB['rev'], 'protected': bool(CONFIG.get('password')),
                                       'trips': [{'id': k, 'rev': v['rev'], 'data': v['data']} for k, v in DB['trips'].items()],
                                       'deleted': list(DB['deleted'].keys())})
        if path == '/api/ping':
            return self.json(200, {'ok': True, 'protected': bool(CONFIG.get('password')), 'resolve': True})
        if path == '/api/resolve':
            # expand a Google Maps short link (maps.app.goo.gl/…) — the browser cannot follow it itself
            if not self.authed():
                return
            url = (parse_qs(urlparse(self.path).query).get('url') or [''])[0]
            try:
                return self.json(200, {'url': expand_google_link(url)})
            except ValueError as e:
                return self.json(400, {'error': str(e)})
            except Exception as e:  # network problems
                return self.json(502, {'error': f'не удалось открыть ссылку: {e}'})
        if path.startswith('/api/') or not self.static_ok():
            return self.send_error(404)
        return super().do_GET()

    def do_HEAD(self):
        if not self.allowed() or not self.static_ok():
            return self.send_error(404)
        return super().do_HEAD()

    def do_PUT(self):
        if not self.allowed() or not self.authed():
            return
        path = urlparse(self.path).path
        if not path.startswith('/api/trips/'):
            return self.send_error(404)
        tid = path.rsplit('/', 1)[1]
        try:
            req = self.body()
        except ValueError:
            return self.json(400, {'error': 'bad request'})
        if not TRIP_ID.fullmatch(tid) or not isinstance(req.get('data'), dict):
            return self.json(400, {'error': 'bad trip'})
        with LOCK:
            cur = DB['trips'].get(tid)
            if cur and req.get('baseRev', 0) != cur['rev'] and not req.get('force'):
                # somebody else saved this trip in the meantime
                return self.json(409, {'id': tid, 'rev': cur['rev'], 'data': cur['data']})
            DB['rev'] += 1
            DB['trips'][tid] = {'rev': DB['rev'], 'data': req['data'], 'at': time.time()}
            DB['deleted'].pop(tid, None)
            save_db(DB)
            return self.json(200, {'id': tid, 'rev': DB['rev']})

    def do_DELETE(self):
        if not self.allowed() or not self.authed():
            return
        path = urlparse(self.path).path
        if not path.startswith('/api/trips/'):
            return self.send_error(404)
        tid = path.rsplit('/', 1)[1]
        with LOCK:
            if tid in DB['trips']:
                DB['rev'] += 1
                del DB['trips'][tid]
                DB['deleted'][tid] = DB['rev']
                save_db(DB)
        return self.json(200, {'ok': True})


TRIP_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
LOCAL_SUFFIXES = ('.local', '.lan', '.home', '.internal', '.ts.net')


def host_ok(host):
    """The Host header must be an IP address, localhost, this computer's name or a home-network name."""
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    names = {'localhost', socket.gethostname().lower(), socket.getfqdn().lower()}
    return host in names or '.' not in host or host.endswith(LOCAL_SUFFIXES)


def is_google_host(host):
    host = (host or '').lower()
    return host in ('maps.app.goo.gl', 'goo.gl', 'g.co') or host == 'google.com' or host.endswith('.google.com') \
        or bool(re.fullmatch(r'(www\.|maps\.)?google\.[a-z]{2,3}(\.[a-z]{2})?', host))


def expand_google_link(url):
    """Follow the redirects of a Google Maps share link (only Google hosts) and return the full maps URL."""
    for _ in range(6):
        u = urlparse(url)
        if u.scheme not in ('http', 'https') or not is_google_host(u.hostname):
            raise ValueError('это не ссылка Google Maps')
        # the EU cookie wall wraps the real address into ?continue=
        if u.hostname.startswith('consent.'):
            cont = (parse_qs(u.query).get('continue') or [''])[0]
            if not cont:
                raise ValueError('Google вернул страницу согласия без адреса')
            url = cont
            continue
        # a full maps URL with coordinates or a query — done
        if '/maps' in u.path and ('@' in url or '!3d' in url or 'q=' in u.query or 'query=' in u.query or '/place/' in u.path):
            return url
        conn = (http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection)(u.hostname, timeout=10)
        conn.request('GET', (u.path or '/') + ('?' + u.query if u.query else ''), headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
            'Accept-Language': 'ru,en;q=0.8'})
        resp = conn.getresponse()
        loc = resp.getheader('Location')
        body = resp.read(200000).decode('utf-8', 'ignore') if not loc else ''
        conn.close()
        if loc:
            url = urljoin(url, loc)
            continue
        # no redirect: look for a maps URL inside the page
        m = re.search(r'https://www\.google\.[a-z.]+/maps/[^"\'\s<>]+', body)
        if m:
            return m.group(0).replace('\\u0026', '&').replace('&amp;', '&')
        return url
    return url


def lan_addresses():
    """(main address, other addresses). The main one is the adapter of the default route —
    the real Wi-Fi/Ethernet; the others are usually virtual (WSL, Hyper-V, VPN)."""
    main = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))  # UDP: nothing is actually sent
        main = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    others = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            others.add(info[4][0])
    except OSError:
        pass
    others = sorted(ip for ip in others if ip != main and not ip.startswith('127.'))
    return main, others


def main():
    port = int(CONFIG['port'])
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print('=' * 60)
    print('  Планировщик поездок — домашний сервер')
    print('=' * 60)
    print(f'  На этом компьютере:   http://localhost:{port}')
    main_ip, other_ips = lan_addresses()
    if main_ip:
        print(f'  Для других в Wi-Fi:   http://{main_ip}:{port}')
    if other_ips:
        print(f'  (другие сетевые адаптеры, обычно не нужны: {", ".join(other_ips)})')
    print(f'  Пароль: {"задан" if CONFIG.get("password") else "нет (можно задать в server_config.json)"}')
    print(f'  Данные: {DATA_FILE}')
    print('  Остановить: закройте это окно или Ctrl+C')
    print('=' * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
