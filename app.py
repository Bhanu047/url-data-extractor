"""Local UI and streaming downloads for durable bulk extraction jobs."""
import argparse
import hashlib
import hmac
import json
import os
import time
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from extractor import csv_text, flatten
from jobs import Jobs

STATIC = Path(__file__).parent / 'static'


def table_columns(table):
    counts, labels = {}, []
    for value in table['headers']:
        name = value or 'Unnamed'
        counts[name] = counts.get(name, 0) + 1
        labels.append(f'data:{name}' + (f' [{counts[name]}]' if counts[name] > 1 else ''))
    return labels


def json_records(page):
    if not page['data']:
        return
    for block_number, block in enumerate(page['data']['structured_data'], 1):
        data = block['data']
        records = data if isinstance(data, list) else [data]
        for record_number, record in enumerate(records, 1):
            fields = {f['path']: f['value'] for f in flatten(record)}
            yield block_number, record_number, fields


def export_chunks(store, job_id, kind):
    status = store.status(job_id)
    if kind == 'json':
        yield '{"job":' + json.dumps(status, ensure_ascii=False) + ',"pages":['
        first = True
        for page in store.pages(job_id):
            yield ('' if first else ',') + json.dumps(page, ensure_ascii=False)
            first = False
        yield ']}'
    elif kind == 'jsonl':
        yield json.dumps({'job': status}, ensure_ascii=False) + '\n'
        for page in store.pages(job_id):
            yield json.dumps(page, ensure_ascii=False) + '\n'
    elif kind == 'records':
        columns, known = [], set()
        for page in store.pages(job_id):
            for _, _, fields in json_records(page):
                for column in fields:
                    if column not in known:
                        known.add(column)
                        columns.append(column)
        yield '\ufeff' + csv_text([['page', 'source_url', 'json_block', 'record_number'] + columns])
        for page in store.pages(job_id):
            for block, number, fields in json_records(page):
                yield csv_text([[page['page'], page['data']['source_url'], block, number] + [fields.get(c, '') for c in columns]])
    elif kind == 'fields':
        yield '\ufeff' + csv_text([['page', 'source_url', 'label_or_path', 'value', 'source']])
        for page in store.pages(job_id):
            data = page['data']
            if data:
                for field in data['fields']:
                    yield csv_text([[page['page'], data['source_url'], field.get('label', field.get('path', '')),
                                     field.get('value', ''), field.get('source', 'JSON')]])
    else:
        columns, known = [], set()
        for page in store.pages(job_id):
            if page['data']:
                for table in page['data']['tables']:
                    for column in table_columns(table):
                        if column not in known:
                            known.add(column)
                            columns.append(column)
        yield '\ufeff' + csv_text([['page', 'source_url', 'table', 'caption', 'row_number'] + columns])
        for page in store.pages(job_id):
            data = page['data']
            if data:
                for i, table in enumerate(data['tables'], 1):
                    labels = table_columns(table)
                    for n, row in enumerate(table['rows'], 1):
                        mapped = dict(zip(labels, row))
                        yield csv_text([[page['page'], data['source_url'], i, table['caption'], n] + [mapped.get(c, '') for c in columns]])


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def headers_for(self, code, kind, length=None):
        self.send_response(code)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")

    def send(self, code, value, kind='application/json'):
        body = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode('utf-8')
        self.headers_for(code, kind, len(body))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in self.server.allowed_hosts

    def authenticated(self):
        if not self.server.access_code:
            return True
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookie['extractor_session'].value
            expiry, signature = value.split('.', 1)
            expected = hmac.new(self.server.access_code.encode(), expiry.encode(), hashlib.sha256).hexdigest()
            return int(expiry) > time.time() and hmac.compare_digest(signature, expected)
        except (CookieError, KeyError, ValueError):
            return False

    def login(self, request):
        code = request.get('code')
        if not isinstance(code, str) or not hmac.compare_digest(code.encode(), self.server.access_code.encode()):
            time.sleep(0.5)
            return self.send(401, {'error': 'Incorrect access code.'})
        expiry = str(int(time.time()) + 30 * 86400)
        signature = hmac.new(self.server.access_code.encode(), expiry.encode(), hashlib.sha256).hexdigest()
        body = b'{"ok":true}'
        self.headers_for(200, 'application/json', len(body))
        secure = '; Secure' if self.server.hosted else ''
        self.send_header('Set-Cookie', f'extractor_session={expiry}.{signature}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Invalid host'})
        parsed = urlsplit(self.path)
        if parsed.path == '/health':
            return self.send(200, {'status': 'ok'})
        public_assets = {'/style.css': ('style.css', 'text/css'), '/login.js': ('login.js', 'text/javascript')}
        if parsed.path in public_assets:
            filename, kind = public_assets[parsed.path]
            return self.send(200, (STATIC / filename).read_text(), kind)
        if not self.authenticated():
            if parsed.path == '/':
                return self.send(200, (STATIC / 'login.html').read_text(), 'text/html')
            return self.send(401, {'error': 'Sign in with your access code.'})
        files = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'), '/app.js': ('app.js', 'text/javascript')}
        if parsed.path in files:
            filename, kind = files[parsed.path]
            return self.send(200, (STATIC / filename).read_text(), kind)
        store = self.server.jobs
        try:
            query = parse_qs(parsed.query)
            job_id = query.get('id', [''])[0]
            if parsed.path == '/api/config':
                return self.send(200, {'hosted': self.server.hosted, 'ephemeral': self.server.ephemeral})
            if parsed.path == '/api/jobs':
                return self.send(200, store.listing())
            if parsed.path == '/api/status':
                return self.send(200, store.status(job_id))
            if parsed.path == '/api/preview':
                # Preview a few rows only; exports always include all saved rows.
                for page in store.pages(job_id):
                    if page['data']:
                        data = page['data']
                        return self.send(200, {'page': page['page'], 'title': data['title'],
                            'tables': [{**t, 'rows': t['rows'][:10]} for t in data['tables'][:3]],
                            'fields': data['fields'][:15], 'warnings': data['warnings']})
                return self.send(200, {'tables': [], 'fields': [], 'warnings': []})
            if parsed.path == '/api/export':
                status = store.status(job_id)
                if status['status'] in {'running', 'pausing'}:
                    return self.send(409, {'error': 'Pause the job or wait for completion before downloading a consistent snapshot.'})
                kind = query.get('format', ['json'])[0]
                if kind not in {'json', 'jsonl', 'tables', 'fields', 'records'}:
                    raise ValueError('Unknown export format.')
                extension = 'csv' if kind in {'tables', 'fields', 'records'} else kind
                mime = 'text/csv' if extension == 'csv' else 'application/json' if extension == 'json' else 'application/x-ndjson'
                # Hold the job lock during the stream so a concurrent resume cannot change its snapshot.
                with store.lock:
                    if job_id in store.running:
                        return self.send(409, {'error': 'Job is finishing; try again shortly.'})
                    self.headers_for(200, mime)
                    self.send_header('Content-Disposition', f'attachment; filename="extract-{job_id[:8]}-{kind}.{extension}"')
                    self.end_headers()
                    for chunk in export_chunks(store, job_id, kind):
                        self.wfile.write(chunk.encode('utf-8'))
                return
            return self.send(404, {'error': 'Not found'})
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError as exc:
            return self.send(400, {'error': str(exc)})

    def do_POST(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Invalid host'})
        origin = self.headers.get('Origin')
        if origin and origin not in self.server.allowed_origins:
            return self.send(403, {'error': 'Invalid origin'})
        if self.path != '/api/login' and not self.authenticated():
            return self.send(401, {'error': 'Sign in with your access code.'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'Send application/json'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('Request must be between 1 and 8192 bytes.')
            self.connection.settimeout(15)
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError('Send a JSON object.')
            if self.path == '/api/login':
                if not self.server.access_code:
                    return self.send(400, {'error': 'Access-code login is disabled locally.'})
                return self.login(request)
            if self.path == '/api/jobs':
                if not isinstance(request.get('url'), str):
                    raise ValueError('Enter a URL as text.')
                job_id = self.server.jobs.create(request)
                self.server.jobs.start(job_id)
                return self.send(201, {'id': job_id})
            job_id = request.get('id', '')
            if not isinstance(job_id, str):
                raise ValueError('Invalid job ID.')
            if self.path == '/api/resume':
                self.server.jobs.start(job_id)
            elif self.path == '/api/pause':
                self.server.jobs.pause(job_id)
            else:
                return self.send(404, {'error': 'Not found'})
            self.send(200, self.server.jobs.status(job_id))
        except (ValueError, TypeError, OSError) as exc:
            self.send(400, {'error': str(exc)})

    def log_message(self, format, *args):
        pass


def make_server(port=8765, database=None, host='127.0.0.1', access_code=None, external_hostname=None):
    access_code = os.environ.get('APP_ACCESS_CODE', '') if access_code is None else access_code
    external_hostname = external_hostname or os.environ.get('RENDER_EXTERNAL_HOSTNAME', '')
    hosted = host not in {'127.0.0.1', 'localhost', '::1'}
    if hosted and (len(access_code) < 24 or not external_hostname):
        raise ValueError('Hosted mode requires APP_ACCESS_CODE (24+ characters) and RENDER_EXTERNAL_HOSTNAME.')
    server = ThreadingHTTPServer((host, port), Handler)
    server.access_code, server.hosted = access_code, hosted
    server.ephemeral = os.environ.get('EPHEMERAL_STORAGE') == 'true'
    server.allowed_hosts = {f'127.0.0.1:{server.server_port}', f'localhost:{server.server_port}'}
    server.allowed_origins = {f'http://127.0.0.1:{server.server_port}', f'http://localhost:{server.server_port}'}
    if external_hostname:
        server.allowed_hosts.update({external_hostname, external_hostname + ':443'})
        server.allowed_origins.add('https://' + external_hostname)
    server.jobs = Jobs(database or Path(__file__).parent / 'data' / 'jobs.sqlite3')
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bulk public URL data extraction')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    server = make_server(args.port, host=args.host)
    print(f'Open http://127.0.0.1:{server.server_port} in your browser. Ctrl+C stops the app.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
