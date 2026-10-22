"""Durable, disk-backed bulk jobs. No record-count cutoff."""
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

from extractor import extract, fetch


def range_from_urls(start_url, end_url):
    if not isinstance(start_url, str) or not isinstance(end_url, str):
        raise ValueError('Enter both Start URL and End URL.')
    first, last = urlsplit(start_url.strip()), urlsplit(end_url.strip())
    a = re.fullmatch(r'(.*?/)([0-9]+)(/?)', first.path)
    b = re.fullmatch(r'(.*?/)([0-9]+)(/?)', last.path)
    if (first.scheme not in {'http', 'https'} or not first.hostname or
        not a or not b or first[:2] != last[:2] or first.query != last.query or
        first.fragment != last.fragment or a.group(1, 3) != b.group(1, 3)):
        raise ValueError('Use matching URLs that differ only in the final numeric path ID, such as /people/1 and /people/100000.')
    start, end = int(a[2]), int(b[2])
    if end < start or end > 2147483647:
        raise ValueError('End ID must be at or above Start ID and no more than 2147483647.')
    width = len(a[2]) if len(a[2]) > 1 and a[2].startswith('0') else 0
    if str(end).zfill(width) != b[2]:
        raise ValueError('Use consistent numeric formatting in both URLs.')
    template = first._replace(path=a[1] + '{page}' + a[3]).geturl()
    return template, start, end, width


def page_url(config, page):
    return config['url'].replace('{page}', str(page).zfill(config.get('width', 0))) if config['mode'] == 'range' else config['url']


class Jobs:
    def __init__(self, database, fetcher=fetch, delay=1.0):
        self.database = str(database)
        self.fetcher, self.delay = fetcher, delay
        self.lock = threading.Lock()
        self.running = set()
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, config TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS pages (
                    job_id TEXT NOT NULL, page INTEGER NOT NULL,
                    url TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, error TEXT NOT NULL DEFAULT '',
                    table_rows INTEGER NOT NULL DEFAULT 0,
                    fields INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id, page));
            ''')
            # A terminated process leaves durable results ready to resume.
            db.execute("UPDATE jobs SET status='paused' WHERE status IN ('running','pausing')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, config):
        mode = config.get('mode', 'single')
        width = 0
        if mode == 'urls':
            url, start, end, width = range_from_urls(config.get('url'), config.get('end_url'))
            config = {**config, 'url': url, 'start': start, 'end': end}
            mode = 'range'
        url = config.get('url', '')
        if mode not in {'single', 'range', 'next'}:
            raise ValueError('Choose single page, numbered range, or next-link mode.')
        if not isinstance(url, str) or not url:
            raise ValueError('Enter a URL.')
        url = url.strip()
        if mode == 'range':
            start, end = int(config.get('start', 1)), int(config.get('end', 20))
            if start < 0 or end < start or end > 2147483647:
                raise ValueError('Use a nonnegative start and an end at or above start.')
            if url.count('{page}') != 1:
                raise ValueError('Put {page} exactly once where the page number belongs in the URL.')
            candidate = url.replace('{page}', str(start))
        else:
            start = end = 1
            candidate = url
        parsed = urlsplit(candidate)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError('Enter a full http:// or https:// URL.')
        # Fetch rechecks DNS, ports, and every redirect at request time.
        config = {'mode': mode, 'url': url, 'start': start, 'end': end, 'width': width}
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO jobs(id,config,status) VALUES(?,?,?)',
                       (job_id, json.dumps(config), 'paused'))
        return job_id

    def status(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise ValueError('Job not found.')
            result = dict(row)
            result['config'] = json.loads(result['config'])
            result.update(dict(db.execute('''SELECT COUNT(*) AS attempted_pages,
                COALESCE(SUM(status='done'),0) AS completed_pages,
                COALESCE(SUM(status='failed'),0) AS failed_pages,
                COALESCE(SUM(table_rows),0) AS table_rows,
                COALESCE(SUM(fields),0) AS fields FROM pages WHERE job_id=?''', (job_id,)).fetchone()))
            result['failures'] = [dict(r) for r in db.execute(
                "SELECT page,url,error FROM pages WHERE job_id=? AND status='failed' ORDER BY page LIMIT 100", (job_id,))]
            result['total_pages'] = (result['config']['end'] - result['config']['start'] + 1) if result['config']['mode'] != 'next' else None
            return result

    def listing(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM jobs ORDER BY created DESC,rowid DESC LIMIT 50')]
        return [self.status(i) for i in ids]

    def start(self, job_id):
        with self.lock:
            if job_id in self.running:
                raise ValueError('This job is still running; wait until it is paused or finished.')
            if self.running:
                raise ValueError('Pause the current job before starting another one.')
            self.status(job_id)
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='running',error='' WHERE id=?", (job_id,))
            self.running.add(job_id)
            threading.Thread(target=self._worker, args=(job_id,), daemon=True).start()

    def pause(self, job_id):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='pausing' WHERE id=? AND status='running'", (job_id,))

    def _worker(self, job_id):
        try:
            self._run(job_id)
        except Exception as exc:
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='paused',error=? WHERE id=?", (str(exc), job_id))
        finally:
            with self.lock:
                self.running.discard(job_id)

    def _run(self, job_id):
        config = self.status(job_id)['config']
        page = config['start']
        url = page_url(config, page)
        seen = set()
        while True:
            with self.connect() as db:
                state = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            if state != 'running':
                with self.connect() as db:
                    db.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,))
                return
            if config['mode'] != 'next' and page > config['end']:
                break
            with self.connect() as db:
                columns = 'status,result' if config['mode'] == 'next' else 'status'
                stored = db.execute(f'SELECT {columns} FROM pages WHERE job_id=? AND page=?', (job_id, page)).fetchone()
            if config['mode'] == 'next':
                canonical = urldefrag(url)[0]
                if canonical in seen:
                    raise ValueError('Pagination loop detected. Saved results are available; inspect the final next link.')
                seen.add(canonical)
            if stored and stored['status'] == 'done':
                result = json.loads(stored['result']) if config['mode'] == 'next' else None
            else:
                result, error = None, ''
                for attempt in range(3):
                    try:
                        final_url, body, kind = self.fetcher(url)
                        result = extract(body, final_url, kind)
                        break
                    except Exception as exc:
                        error = str(exc)
                        if attempt < 2:
                            time.sleep(self.delay * (attempt + 1))
                with self.connect() as db:
                    db.execute('''INSERT OR REPLACE INTO pages
                        (job_id,page,url,status,result,error,table_rows,fields)
                        VALUES(?,?,?,?,?,?,?,?)''',
                        (job_id, page, url, 'done' if result is not None else 'failed',
                         json.dumps(result, ensure_ascii=False) if result is not None else None,
                         '' if result is not None else error,
                         sum(len(t['rows']) for t in result['tables']) if result else 0,
                         len(result['fields']) if result else 0))
            if config['mode'] == 'next':
                if result is None:
                    break  # Cannot discover the next page after a failure. Resume retries it.
                next_url = result.get('next_url')
                if not next_url:
                    break
                # The extractor checks origin; keep the complete chain on the original origin too.
                if urlsplit(next_url)[:2] != urlsplit(config['url'])[:2]:
                    raise ValueError('Pagination crossed to another origin; enter its URLs separately.')
                url = next_url
            else:
                url = page_url(config, page + 1)
            page += 1
            time.sleep(self.delay)
        with self.connect() as db:
            failed = db.execute("SELECT COUNT(*) FROM pages WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
            db.execute('UPDATE jobs SET status=? WHERE id=?', ('completed_with_errors' if failed else 'completed', job_id))

    def pages(self, job_id):
        self.status(job_id)
        with self.connect() as db:
            for row in db.execute('SELECT page,url,status,error,result FROM pages WHERE job_id=? ORDER BY page', (job_id,)):
                yield {'page': row['page'], 'requested_url': row['url'], 'status': row['status'],
                       'error': row['error'], 'data': json.loads(row['result']) if row['result'] else None}
