import csv
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from app import export_chunks, make_server
from extractor import csv_text, extract, fetch, public_target
from jobs import Jobs, page_url, range_from_urls


class ExtractionTests(unittest.TestCase):
    def test_fields_json_and_pagination(self):
        html = '''<title>People</title><body><dl><dt>Name</dt><dd>Asha Rao</dd><dt>DOB</dt><dd>1997-04-02</dd></dl>
        <p>From: Hyderabad</p><p>To: Berlin</p><span itemprop="birthDate" content="1997-04-02"></span>
        <script type="application/ld+json">{"name":"Asha Rao","birthDate":"1997-04-02"}</script>
        <a href="?page=2" rel="next">Next</a></body>'''
        result = extract(html, 'https://example.com/list?page=1')
        fields = {f.get('label', f.get('path')): f['value'] for f in result['fields']}
        self.assertEqual([fields[k] for k in ['Name','DOB','From','To']], ['Asha Rao','1997-04-02','Hyderabad','Berlin'])
        self.assertEqual(result['next_url'], 'https://example.com/list?page=2')
        self.assertEqual(result['structured_data'][0]['data']['name'], 'Asha Rao')
        self.assertNotIn('birthDate', result['text'])

    def test_table_spans_unicode(self):
        data = extract('''<table><tr><th>Name</th><th>DOB</th><th>From</th></tr>
        <tr><td rowspan="2">భాను</td><td>1997</td><td>India</td></tr>
        <tr><td>1998</td><td>France</td></tr></table>''', 'https://example.com')
        self.assertEqual(data['tables'][0]['rows'], [['భాను','1997','India'],['భాను','1998','France']])

    def test_no_inference_and_cross_origin_next(self):
        result = extract('<p>Someone is 29.</p><a rel="next" href="https://elsewhere.com">Next</a>', 'https://example.com')
        self.assertEqual(result['fields'], [])
        self.assertIsNone(result['next_url'])

    def test_original_json(self):
        value = [{'name':'Bhanu','dob':None},{'name':'Asha','dob':'1997-01-01'}]
        result = extract(json.dumps(value), 'https://example.com/api', 'application/json')
        self.assertEqual(result['structured_data'][0]['data'], value)
        self.assertEqual(len(result['fields']), 4)

    def test_unavailable_source_is_rejected_before_network(self):
        with patch('extractor.socket.getaddrinfo') as dns:
            for url in ['https://www.foggyfly.com/Booking/signature/1','https://FOGGYFLY.COM./anything']:
                with self.assertRaisesRegex(ValueError,'source is unavailable'):
                    public_target(url)
            dns.assert_not_called()

    def test_csv_formula_protection(self):
        values = next(csv.reader(io.StringIO(csv_text([['=CMD','\t+CMD','normal','1997-01-01']]))))
        self.assertTrue(values[0].startswith("'="))
        self.assertTrue(values[1].startswith("'"))
        self.assertEqual(values[2:], ['normal','1997-01-01'])

    @patch('extractor.socket.getaddrinfo')
    def test_private_and_mixed_dns_blocked(self, resolve):
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1']:
            resolve.return_value = [(socket.AF_INET,1,6,'',(ip,80))]
            with self.assertRaises(ValueError):
                public_target('http://example.com')
        resolve.return_value = [(socket.AF_INET,1,6,'',('8.8.8.8',80)),(socket.AF_INET,1,6,'',('10.0.0.1',80))]
        with self.assertRaises(ValueError):
            public_target('http://example.com')
        for url in ['file:///etc/passwd','https://name:secret@example.com']:
            with self.assertRaises(ValueError):
                public_target(url)

    def test_redirect_to_private_blocked(self):
        response = MagicMock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/'
        connection = MagicMock()
        connection.getresponse.return_value = response
        def resolve(host, port, **kwargs):
            return [(socket.AF_INET,1,6,'',('127.0.0.1' if host == '127.0.0.1' else '8.8.8.8',port))]
        with patch('extractor.socket.getaddrinfo',side_effect=resolve), patch('extractor.PinnedHTTP',return_value=connection):
            with self.assertRaisesRegex(ValueError,'public Internet'):
                fetch('http://example.com/')


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'jobs.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, store, job_id):
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        store._run(job_id)

    def test_direct_start_end_urls_visit_inclusive_range(self):
        calls = []
        def fixture(url):
            calls.append(url)
            return url, '<p>Name: Example</p>', 'text/html'
        store = Jobs(self.path, fixture, 0)
        job_id = store.create({'mode':'urls', 'url':'https://example.com/people/1', 'end_url':'https://example.com/people/20'})
        self.run_sync(store, job_id)
        self.assertEqual(calls, [f'https://example.com/people/{i}' for i in range(1,21)])
        self.assertEqual(store.status(job_id)['completed_pages'],20)

    def test_json_records_csv_one_row_per_user(self):
        def fixture(url):
            i = int(url.rsplit('/',1)[1])
            return url,json.dumps({'id':i,'name':f'Fictional User {i}','address':{'city':'Sample City'}}),'application/json'
        store = Jobs(self.path,fixture,0)
        job_id = store.create({'mode':'urls','url':'https://example.com/users/1','end_url':'https://example.com/users/10'})
        self.run_sync(store,job_id)
        rows = list(csv.reader(io.StringIO(''.join(export_chunks(store,job_id,'records')).lstrip('\ufeff'))))
        self.assertEqual(len(rows),11)
        self.assertIn('$["name"]',rows[0])
        self.assertIn('$["address"]["city"]',rows[0])
        self.assertIn('Fictional User 10',rows[-1])

    def test_json_array_records_csv_preserves_all_items(self):
        value = [{'name':'First'},{'name':'Second','extra':'=TEST'}]
        store = Jobs(self.path,lambda url:(url,json.dumps(value),'application/json'),0)
        job_id = store.create({'mode':'single','url':'https://example.com/users'})
        self.run_sync(store,job_id)
        rows = list(csv.reader(io.StringIO(''.join(export_chunks(store,job_id,'records')).lstrip('\ufeff'))))
        self.assertEqual(len(rows),3)
        self.assertIn("'=TEST",rows[-1])

    def test_100000_url_range_planning_without_network(self):
        store = Jobs(self.path, delay=0)
        job_id = store.create({'mode':'urls', 'url':'https://example.com/people/1', 'end_url':'https://example.com/people/100000'})
        config = store.status(job_id)['config']
        self.assertEqual(store.status(job_id)['total_pages'],100000)
        urls = (page_url(config,n) for n in range(config['start'],config['end']+1))
        self.assertEqual(next(urls),'https://example.com/people/1')
        count, last = 1, None
        for last in urls:
            count += 1
        self.assertEqual(count,100000)
        self.assertEqual(last,'https://example.com/people/100000')

    def test_url_range_validation_and_zero_padding(self):
        for first,last in [('https://example.com/x/1','https://other.com/x/10'),
                           ('https://example.com/x/1','https://example.com/y/10'),
                           ('https://example.com/x/20','https://example.com/x/1'),
                           ('https://example.com/x/1?q=a','https://example.com/x/10?q=b')]:
            with self.assertRaises(ValueError):
                range_from_urls(first,last)
        store = Jobs(self.path,delay=0)
        job_id = store.create({'mode':'urls','url':'https://example.com/x/0001/','end_url':'https://example.com/x/0020/'})
        self.assertEqual(page_url(store.status(job_id)['config'],2),'https://example.com/x/0002/')

    def test_50020_rows_twenty_pages_complete_export(self):
        calls = []
        def fixture(url):
            calls.append(url)
            page = int(url.split('=')[-1])
            rows = ''.join(f'<tr><td>Person {page}-{i}</td><td>1997-01-01</td><td>India</td></tr>' for i in range(2501))
            return url, '<table><tr><th>Name</th><th>DOB</th><th>From</th></tr>' + rows + '</table>', 'text/html'
        store = Jobs(self.path, fixture, 0)
        job_id = store.create({'url':'https://example.com/?page={page}','mode':'range','start':1,'end':20})
        self.run_sync(store, job_id)
        status = store.status(job_id)
        self.assertEqual(status['status'],'completed')
        self.assertEqual(status['completed_pages'],20)
        self.assertEqual(status['table_rows'],50020)
        rows = csv.reader(io.StringIO(''.join(export_chunks(store, job_id, 'tables'))))
        self.assertIn('data:Name',next(rows))
        count, last = 0, None
        for last in rows:
            count += 1
        self.assertEqual(count,50020)
        self.assertIn('Person 20-2500',last)
        self.run_sync(store, job_id)
        self.assertEqual(len(calls),20)

    def test_failure_retry_after_restart(self):
        calls, broken = [], [True]
        def fixture(url):
            calls.append(url)
            if url.endswith('/2') and broken[0]:
                raise ValueError('HTTP 429')
            return url, '<p>Name: Test</p>', 'text/html'
        store = Jobs(self.path, fixture, 0)
        job_id = store.create({'url':'https://example.com/{page}','mode':'range','start':1,'end':3})
        self.run_sync(store, job_id)
        self.assertEqual(store.status(job_id)['status'],'completed_with_errors')
        document = json.loads(''.join(export_chunks(store, job_id, 'json')))
        self.assertEqual(document['pages'][1]['error'],'HTTP 429')
        broken[0] = False
        restored = Jobs(self.path, fixture, 0)
        self.run_sync(restored, job_id)
        self.assertEqual(restored.status(job_id)['completed_pages'],3)
        self.assertEqual(calls.count('https://example.com/1'),1)
        self.assertEqual(calls.count('https://example.com/3'),1)
        self.assertEqual(calls.count('https://example.com/2'),4)

    def test_next_and_loop(self):
        def fixture(url):
            link = '<a rel="next" href="/2">Next</a>' if url.endswith('/1') else ''
            return url,'<p>Name: Test</p>'+link,'text/html'
        store = Jobs(self.path,fixture,0)
        job_id = store.create({'url':'https://example.com/1','mode':'next'})
        self.run_sync(store,job_id)
        self.assertEqual(store.status(job_id)['completed_pages'],2)
        store.fetcher = lambda url:(url,'<a rel="next" href="/1">Next</a>','text/html')
        loop_id = store.create({'url':'https://example.com/1','mode':'next'})
        with self.assertRaisesRegex(ValueError,'loop'):
            self.run_sync(store,loop_id)

    def test_http_ui_jobs_export_host_validation(self):
        server = make_server(0,self.path,access_code='')
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            for path in ['/','/app.js','/style.css','/api/jobs']:
                with urlopen(base+path) as response:
                    self.assertEqual(response.status,200)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base+'/api/jobs',headers={'Host':'attacker.example'}))
            self.assertEqual(error.exception.code,403)
            server.jobs.fetcher = lambda url:(url,'<table><tr><th>Name</th></tr><tr><td>Bhanu</td></tr></table>','text/html')
            server.jobs.delay = 0
            payload = json.dumps({'url':'https://example.com','mode':'single'}).encode()
            with urlopen(Request(base+'/api/jobs',data=payload,headers={'Content-Type':'application/json'})) as response:
                job_id = json.load(response)['id']
            for _ in range(200):
                if not server.jobs.running:
                    break
                time.sleep(.01)
            with urlopen(base+'/api/export?id='+job_id+'&format=json') as response:
                self.assertEqual(json.load(response)['job']['table_rows'],1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class HostedTests(unittest.TestCase):
    def test_hosted_login_and_cookie_security(self):
        code = 'fixture-only-access-code-with-enough-entropy'
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(0,Path(tmp)/'jobs.sqlite3',host='0.0.0.0',access_code=code,external_hostname='tool.example.com')
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(base) as response:
                    self.assertIn(b'Access code',response.read())
                with urlopen(base+'/health') as response:
                    self.assertEqual(json.load(response)['status'],'ok')
                for path in ['/api/jobs','/api/export?id=unknown']:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base+path)
                    self.assertEqual(error.exception.code,401)
                payload = json.dumps({'code':code}).encode()
                with urlopen(Request(base+'/api/login',data=payload,headers={'Content-Type':'application/json'})) as response:
                    cookie = response.headers['Set-Cookie']
                    for flag in ['Secure','HttpOnly','SameSite=Strict']:
                        self.assertIn(flag,cookie)
                session = cookie.split(';',1)[0]
                with urlopen(Request(base+'/api/jobs',headers={'Cookie':session})) as response:
                    self.assertEqual(json.load(response),[])
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/jobs',headers={'Cookie':session+'tampered'}))
                self.assertEqual(error.exception.code,401)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/login',data=payload,headers={'Content-Type':'application/json','Origin':'https://attacker.example'}))
                self.assertEqual(error.exception.code,403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_hosted_mode_requires_secret(self):
        with self.assertRaises(ValueError):
            make_server(0,host='0.0.0.0',access_code='',external_hostname='tool.example.com')


if __name__ == '__main__':
    unittest.main()
