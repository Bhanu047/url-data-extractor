"""Dependency-free extraction of public HTML and JSON; no inferred personal data."""
import csv
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urljoin, urlsplit

MAX_BYTES = int(os.environ.get('MAX_RESPONSE_MB', '100')) * 1024 * 1024
VOID = set('area base br col embed hr img input link meta param source track wbr'.split())
IGNORED = {'script', 'style', 'noscript', 'template'}


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.walk(tag)

    def text(self):
        def parts(node):
            for child in node.children:
                if isinstance(child, str):
                    yield child
                elif child.tag not in IGNORED:
                    yield from parts(child)
        return re.sub(r'\s+', ' ', ' '.join(parts(self))).strip()

    def ancestor(self, tag):
        node = self.parent
        while node:
            if node.tag == tag:
                return node
            node = node.parent


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        # Common omitted closing tags in tables/lists.
        if tag in {'td', 'th'} and self.current.tag in {'td', 'th'}:
            self.current = self.current.parent
        if tag == 'tr':
            while self.current.tag in {'td', 'th', 'tr'}:
                self.current = self.current.parent
        if tag in {'li', 'dt', 'dd'} and self.current.tag in {'li', 'dt', 'dd'}:
            self.current = self.current.parent
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def check_source(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    if host == 'instagram.com' or host.endswith('.instagram.com'):
        raise ValueError('This source is unavailable in this tool. Use public test data or a source you are authorized to export.')


def public_target(url):
    check_source(url)
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        raise ValueError('Invalid URL or port.') from exc
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('Enter a full http:// or https:// URL.')
    if parsed.username or parsed.password or port not in {80, 443}:
        raise ValueError('Credentials and nonstandard ports are unsupported.')
    host = parsed.hostname.encode('idna').decode('ascii')
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('The hostname could not be resolved.')
    for info in addresses:
        address = ipaddress.ip_address(info[4][0])
        mapped = getattr(address, 'ipv4_mapped', None)
        if not address.is_global or (mapped and not mapped.is_global):
            raise ValueError('Only public Internet URLs are supported.')
    return parsed, host, port, addresses[0][4][0]


class PinnedHTTP(HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection((self.target_ip, self.port), self.timeout)


class PinnedHTTPS(HTTPSConnection):
    def connect(self):
        raw = socket.create_connection((self.target_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch(url):
    """Revalidate redirects and pin resolved IPs to prevent DNS rebinding."""
    deadline = time.monotonic() + 30
    for _ in range(6):
        parsed, host, port, address = public_target(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('The page took too long to respond.')
        cls = PinnedHTTPS if parsed.scheme == 'https' else PinnedHTTP
        connection = cls(host, port, timeout=min(10, remaining))
        connection.target_ip = address
        try:
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={
                'User-Agent': 'URLDataExtractor/1.0',
                'Accept': 'text/html, application/json, application/ld+json',
                'Accept-Encoding': 'identity',
            })
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise ValueError('The site returned an empty redirect.')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f'The site returned HTTP {response.status}. Login or bot protection may block access.')
            content_type = response.getheader('Content-Type', '').lower()
            if not any(t in content_type for t in ('text/html', 'application/xhtml+xml', 'application/json', 'application/ld+json')):
                raise ValueError('This version supports HTML and JSON pages, not PDFs, images, or other downloads.')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('The site returned unsupported compressed content.')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('The page took too long to download.')
                if connection.sock:
                    connection.sock.settimeout(min(10, remaining))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f'This page exceeds the {MAX_BYTES // (1024 * 1024)} MB download limit; use smaller paginated responses.')
                chunks.append(chunk)
            encoding = re.search(r'charset=["\']?([^;\s"\']+)', content_type)
            try:
                body = b''.join(chunks).decode(encoding.group(1) if encoding else 'utf-8', errors='replace')
            except LookupError:
                body = b''.join(chunks).decode('utf-8', errors='replace')
            return url, body, content_type
        finally:
            connection.close()
    raise ValueError('The page redirected too many times.')


def table_data(table):
    grid, header_flags, occupied = [], [], {}
    for r, row in enumerate(n for n in table.walk('tr') if n.ancestor('table') is table):
        values, flags, col = {}, [], 0
        for (rr, cc), value in list(occupied.items()):
            if rr == r:
                values[cc] = value
        cells = [n for n in row.walk() if n.tag in {'td', 'th'} and n.ancestor('tr') is row and n.ancestor('table') is table]
        for cell in cells:
            while col in values:
                col += 1
            def span(name):
                try:
                    return max(1, min(100, int(cell.attrs.get(name, 1))))
                except (ValueError, TypeError):
                    return 1
            for dr in range(span('rowspan')):
                for dc in range(span('colspan')):
                    if dr == 0:
                        values[col + dc] = cell.text()
                    else:
                        occupied[r + dr, col + dc] = cell.text()
            flags.append(cell.tag == 'th')
            col += span('colspan')
        if values:
            grid.append([values.get(c, '') for c in range(max(values) + 1)])
            header_flags.append(bool(flags) and all(flags))
    width = max(map(len, grid), default=0)
    grid = [row + [''] * (width - len(row)) for row in grid]
    has_header = bool(grid and header_flags[0])
    captions = list(table.walk('caption'))
    return {'caption': captions[0].text() if captions else '',
            'headers': grid[0] if has_header else [f'Column {i+1}' for i in range(width)],
            'rows': grid[1:] if has_header else grid,
            'header_detected': has_header}


def flatten(value, path='$'):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from flatten(child, f'{path}[{json.dumps(key)}]')
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from flatten(child, f'{path}[{i}]')
    else:
        yield {'path': path, 'value': value}


def extract(body, url, content_type='text/html'):
    result = {'source_url': url, 'title': '', 'next_url': None, 'tables': [], 'fields': [],
              'structured_data': [], 'metadata': [], 'links': [], 'images': [],
              'headings': [], 'text': '', 'warnings': []}
    if 'json' in content_type:
        value = json.loads(body)
        result['structured_data'].append({'type': 'json', 'data': value})
        result['fields'] = list(flatten(value))
        return result
    root = Document(body).root
    titles = list(root.walk('title'))
    result['title'] = titles[0].text() if titles else ''
    result['tables'] = [table_data(t) for t in root.walk('table')]
    for node in root.walk():
        attrs = node.attrs
        if node.tag == 'meta':
            result['metadata'].append({'name': attrs.get('name') or attrs.get('property') or attrs.get('http-equiv', ''), 'content': attrs.get('content', '')})
        if node.tag == 'a' and attrs.get('href'):
            result['links'].append({'text': node.text(), 'url': urljoin(url, attrs['href'])})
        if node.tag in {'a', 'link'} and attrs.get('href') and ('next' in attrs.get('rel', '').split() or (node.tag == 'a' and node.text().lower() in {'next', 'next page', 'next ›', 'next »'})):
            candidate = urljoin(url, attrs['href'])
            # Automatic pagination stays on the exact same origin.
            if urlsplit(candidate)[:2] == urlsplit(url)[:2]:
                result['next_url'] = candidate
        if node.tag == 'img':
            result['images'].append({'alt': attrs.get('alt', ''), 'url': urljoin(url, attrs.get('src', ''))})
        if node.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            result['headings'].append({'level': node.tag, 'text': node.text()})
        if attrs.get('itemprop'):
            result['fields'].append({'label': attrs['itemprop'], 'value': attrs.get('content') or attrs.get('datetime') or attrs.get('href') or attrs.get('src') or node.text(), 'source': 'microdata'})
        if node.tag == 'script' and attrs.get('type', '').lower().split(';')[0].strip() in {'application/ld+json', 'application/json'}:
            raw = ''.join(x for x in node.children if isinstance(x, str))
            try:
                data = json.loads(raw)
                result['structured_data'].append({'type': attrs['type'], 'id': attrs.get('id', ''), 'data': data})
                for field in flatten(data):
                    result['fields'].append({**field, 'source': 'embedded JSON'})
            except (ValueError, RecursionError):
                result['warnings'].append('An embedded JSON block could not be parsed.')
        if node.tag == 'dt' and node.parent:
            siblings = node.parent.children
            following = siblings[siblings.index(node) + 1:]
            for sibling in following:
                if isinstance(sibling, Node) and sibling.tag == 'dt':
                    break
                if isinstance(sibling, Node) and sibling.tag == 'dd':
                    result['fields'].append({'label': node.text(), 'value': sibling.text(), 'source': 'definition list'})
        # Only explicit short label/value pairs; never infer a person's identity.
        if node.tag in {'p', 'li', 'div', 'span'} and not any(isinstance(c, Node) and c.tag in {'p', 'li', 'div'} for c in node.children):
            match = re.fullmatch(r'([^:]{1,60}):\s*(.{1,500})', node.text())
            if match and not re.match(r'https?$', match.group(1), re.I):
                result['fields'].append({'label': match.group(1).strip(), 'value': match.group(2).strip(), 'source': 'labelled text'})
    for i, table in enumerate(result['tables']):
        if len(table['headers']) == 2:
            for row in table['rows']:
                result['fields'].append({'label': row[0], 'value': row[1], 'source': f'table {i+1}'})
    bodies = list(root.walk('body'))
    result['text'] = (bodies[0] if bodies else root).text()
    result['warnings'].append('JavaScript-rendered content, login-only data, and arbitrary linked detail pages are not included. Missing fields are not inferred. Pagination requires range or next-link mode.')
    return result


def csv_text(rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    for row in rows:
        # Prevent spreadsheet formula execution when a CSV is opened.
        writer.writerow([("'" + str(v)) if str(v).lstrip().startswith(('=', '+', '-', '@')) else str(v) for v in row])
    return output.getvalue()
