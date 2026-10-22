'use strict';
const $ = id => document.getElementById(id);
let current = null, lastPreview = '', busy = false;
const number = n => Number(n).toLocaleString();
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
$('mode').addEventListener('change', () => {
  const range = $('mode').value === 'range';
  const urls = $('mode').value === 'urls';
  $('range').hidden = !range; $('hint').hidden = !range; $('example').hidden = !(range || urls);
  $('end-url-box').hidden = !urls; $('end-url').required = urls;
  $('url-label').textContent = urls ? 'Start URL' : 'Source URL';
  $('start').required = range; $('end').required = range;
  $('url').placeholder = range ? 'https://example.com/records?page={page}' : urls ? 'https://example.com/people/1' : 'https://example.com/records';
  $('example').textContent = urls ? 'Start /people/1, end /people/100000 visits all 100,000 URLs, including both endpoints. Each URL can contain multiple records.' : 'Replace the changing number with {page}, then enter the inclusive start and end numbers.';
});
$('form').addEventListener('submit', async event => {
  event.preventDefault(); $('message').textContent = ''; $('submit').disabled = true;
  try {
    const job = await api('/api/jobs', {url:$('url').value, end_url:$('end-url').value, mode:$('mode').value, start:Number($('start').value), end:Number($('end').value)});
    current = job.id; lastPreview = ''; await refresh(); await history();
  } catch (e) { $('message').textContent = e.message; }
  finally { $('submit').disabled = false; }
});
$('sample').addEventListener('click', () => {
  $('mode').value = 'urls'; $('mode').dispatchEvent(new Event('change'));
  $('url').value = 'https://jsonplaceholder.typicode.com/users/1';
  $('end-url').value = 'https://jsonplaceholder.typicode.com/users/10';
  $('message').textContent = 'Sample URLs filled. Click Start extraction to collect the 10 fictional users.';
});
for (const action of ['pause','resume']) $(action).addEventListener('click', async () => {
  try { await api('/api/' + action, {id:current}); await refresh(); }
  catch (e) { $('job-error').textContent = e.message; }
});
function textNode(tag, text) { const el = document.createElement(tag); el.textContent = text; return el; }
async function preview() {
  const data = await api('/api/preview?id=' + current), area = $('preview'); area.replaceChildren();
  for (const [i, table] of data.tables.entries()) {
    area.append(textNode('p', table.caption || 'Table ' + (i+1)));
    const el = document.createElement('table'), head = document.createElement('tr');
    table.headers.forEach(h => head.append(textNode('th', h))); el.append(head);
    table.rows.forEach(row => {const tr = document.createElement('tr'); row.forEach(v => tr.append(textNode('td', v))); el.append(tr);});
    area.append(el);
  }
  if (data.fields.length) area.append(textNode('pre', JSON.stringify(data.fields, null, 2)));
  if (!data.tables.length && !data.fields.length) area.append(textNode('p', 'No table or labelled fields in this preview. Full JSON includes any page text and metadata collected.'));
  for (const warning of data.warnings) area.append(textNode('p', warning));
}
async function refresh() {
  if (!current || busy) return;
  busy = true;
  try {
    const job = await api('/api/status?id=' + current);
    $('result').hidden = false; $('status').textContent = job.status.replaceAll('_',' ');
    $('pages').textContent = number(job.completed_pages) + (job.total_pages === null ? '' : ' / ' + number(job.total_pages));
    $('rows').textContent = number(job.table_rows); $('fields').textContent = number(job.fields); $('failures').textContent = number(job.failed_pages);
    $('job-error').textContent = job.error || (job.failed_pages ? 'Some pages failed. Resume retries failed pages; exports include the saved pages and the JSON lists failures.' : '');
    if (job.total_pages === null) $('progress').removeAttribute('value');
    else { $('progress').max = job.total_pages; $('progress').value = job.attempted_pages; }
    const active = ['running','pausing'].includes(job.status);
    $('pause').disabled = !active || job.status === 'pausing'; $('resume').disabled = active;
    $('downloads').hidden = active;
    for (const [id,format] of [['json','json'],['jsonl','jsonl'],['records','records'],['tables','tables'],['fields-download','fields']]) $(id).href = '/api/export?id=' + current + '&format=' + format;
    $('failure-list').textContent = JSON.stringify(job.failures, null, 2);
    const key = current + ':' + (job.completed_pages > 0);
    if (lastPreview !== key) { await preview(); lastPreview = key; }
  } catch (e) { $('message').textContent = e.message; }
  finally { busy = false; }
}
async function history() {
  try {
    const jobs = await api('/api/jobs'); $('history').replaceChildren();
    if (!jobs.length) $('history').append(textNode('p','No runs yet.'));
    for (const job of jobs) {
      const button = textNode('button', job.config.url + ' · ' + job.status + ' · ' + number(job.completed_pages) + ' pages');
      button.addEventListener('click', async () => {current = job.id; lastPreview = ''; await refresh();});
      $('history').append(button);
    }
  } catch (e) { $('message').textContent = e.message; }
}
api('/api/config').then(config => {
  $('environment').textContent = config.hosted ? 'Your online workspace' : 'Runs on your computer';
  $('storage-notice').hidden = !config.ephemeral;
}).catch(error => { $('message').textContent = error.message; });
history(); setInterval(refresh, 1500);
