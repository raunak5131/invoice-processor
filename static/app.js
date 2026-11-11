/* Invoice Desk front-end. Vanilla JS, no build step. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const money = (n, cur = 'USD') => {
  if (n == null) return '—';
  try { return new Intl.NumberFormat('en-US', { style: 'currency', currency: cur }).format(n); }
  catch { return Number(n).toFixed(2); }
};
const fmt = n => Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const STATUS = {
  APPROVED: { label: 'Approved', icon: '✓', cls: 'approved', color: '#0e9f6e' },
  NEEDS_REVIEW: { label: 'Needs review', icon: '!', cls: 'review', color: '#f2a007' },
  ON_HOLD: { label: 'On hold', icon: '❙❙', cls: 'hold', color: '#6d4aff' },
  REJECTED: { label: 'Rejected', icon: '✕', cls: 'rejected', color: '#d6334a' },
};
const STEPS = [['ingest', 'Receive'], ['read', 'Read'], ['extract', 'Extract'], ['complete', 'Complete'], ['vendor', 'Vendor'],
  ['po', 'PO match'], ['dupes', 'Duplicates'], ['amounts', 'Amounts'], ['decide', 'Decision']];
const ICON = { ok: '✓', warn: '!', fail: '✕', skipped: '–', running: '', pending: '' };
const chip = s => { const m = STATUS[s]; return m ? `<span class="chip ${m.cls}">${m.icon} ${m.label}</span>` : esc(s); };

let busy = false, samples = [], lastStatus = {}, filter = 'ALL', allRuns = [];

function toast(msg, ms = 3500) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), ms);
}

/* ------------------------------------------------------------ tabs */
function showTab(name) {
  $$('.tab').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'tab-' + name));
  if (name === 'dashboard') loadDashboard();
  if (name === 'pos') loadPOs();
}
$$('.tab').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));

/* ------------------------------------------------------------ stage detail */
function stageExtra(s) {
  const d = s.data || {}, parts = [];
  if (s.id === 'extract' && d.fields) {
    const F = d.fields, C = d.confidence || {};
    const rows = [['Vendor', 'vendor_name'], ['Invoice number', 'invoice_number'], ['Invoice date', 'invoice_date'], ['PO reference', 'po_number'],
      ['Currency', 'currency'], ['Net', 'subtotal'], ['Tax', 'tax'], ['Total', 'total']];
    parts.push(`<table class="kv">${rows.map(([l, k]) => {
      const v = F[k], c = C[k];
      const val = v == null ? '<span class="low">not found</span>' : (typeof v === 'number' ? fmt(v) : esc(v));
      return `<tr><td>${l}</td><td>${val}</td><td class="conf ${c < .6 && v != null ? 'low' : ''}">${v != null && c != null ? Math.round(c * 100) + '% sure' : ''}</td></tr>`;
    }).join('')}</table>`);
    (d.notes || []).forEach(n => parts.push(`<p>${esc(n)}</p>`));
  } else {
    if (s.id === 'read' && d.text_preview) parts.push(`<details><summary>Show the text that was read</summary><pre class="preview">${esc(d.text_preview)}</pre></details>`);
    if (s.id === 'po' && (d.candidates || []).length) parts.push(`<table class="kv">${d.candidates.map(c =>
      `<tr><td>${esc(c.po_number)}</td><td>${esc(c.description)}</td><td class="conf">match ${Math.round(c.score * 100)}%</td></tr>`).join('')}</table>`);
  }
  return parts.join('');
}
function findingsList(s) {
  return (s.findings || []).length ? `<ul>${s.findings.map(f => `<li><span class="tag ${f.severity}">${f.severity === 'info' ? 'Pass' : f.severity === 'review' ? 'Review' : f.severity === 'hold' ? 'Hold' : 'Reject'}</span>${esc(f.message)}</li>`).join('')}</ul>` : '';
}

/* ------------------------------------------------------------ result */
function poBar(run) {
  const a = (run.stages || []).find(s => s.id === 'amounts'), d = a && a.data;
  if (!d || d.po_amount == null) return '';
  const base = Math.max(d.po_amount, d.cumulative), pct = v => (v / base * 100).toFixed(1), over = d.cumulative > d.po_amount;
  return `<div class="card panel"><h3>${esc(d.po_number)} budget</h3>
    <div class="bar" role="img" aria-label="Billed so far ${money(d.already_invoiced)}, this invoice ${money(d.this_invoice_net)} of ${money(d.po_amount)}">
      <i class="a" style="width:${pct(d.already_invoiced)}%"></i><i class="${over ? 'over' : 'b'}" style="width:${pct(d.this_invoice_net)}%"></i></div>
    <div class="legend"><span>Already billed ${money(d.already_invoiced)}</span><span><b>This invoice ${money(d.this_invoice_net)}</b></span>
    <span>PO total ${money(d.po_amount)}</span></div></div>`;
}

function resultHTML(run) {
  const m = STATUS[run.status] || { label: run.status, cls: 'review' };
  const F = run.fields || {}, cur = F.currency || 'USD';
  const rs = run.reasons || [];
  const extra = rs.length === 1 && rs[0].message === run.headline ? [] : rs;
  const fact = (k, v, cls = '', sub = '') => `<div class="card fact ${cls}"><small>${k}</small><b>${v == null || v === '' ? '—' : esc(v)}</b>${sub}</div>`;
  const em = run.email ? `<div class="card panel email"><div class="email-hd"><span><b>${run.email.audience === 'vendor' ? 'Draft reply to vendor' : 'Draft internal note'}</b> to ${esc(run.email.to)}</span>
    <button class="copy" data-body="${esc(run.email.body)}">Copy</button></div><div class="email-sub" style="font-weight:600;margin-bottom:8px">${esc(run.email.subject)}</div><pre>${esc(run.email.body)}</pre></div>` : '';
  return `<div class="card verdict ${m.cls}"><div class="stamp">${m.label}</div>
      <div><div class="why">${esc(run.headline)}</div><div class="next"><b>Next:</b> ${esc(run.action)}</div></div></div>
    <div class="facts">${fact('Vendor', run.vendor_name || F.vendor_name)}${fact('Invoice', F.invoice_number)}${fact('Date', F.invoice_date)}${fact('PO', run.matched_po || F.po_number)}
      <div class="card fact total"><small>Total</small><b>${money(F.total, cur)}</b>${F.tax ? `<span class="sub">${money(F.net, cur)} net + ${money(F.tax, cur)} tax</span>` : ''}</div></div>
    ${extra.length ? `<div class="card panel"><h3>What we found</h3><ul class="reasons">${extra.map(r => `<li class="${r.severity}">${esc(r.message)}</li>`).join('')}</ul></div>` : ''}
    ${poBar(run)}${em}
    ${run.id ? `<p class="foot">Run ${run.id}, ${run.duration_ms} ms. <a href="/api/runs/${run.id}/file" target="_blank" rel="noopener">Open the original</a></p>` : ''}`;
}

/* ------------------------------------------------------------ run view */
function createRunView(root) {
  const st = { stages: {}, sel: null, cur: null, live: false };
  root.innerHTML = `<div class="run-head"><h2></h2><span></span></div><div class="card tracker"></div><div class="card stage-detail"></div><div class="result"></div>`;
  const head = $('.run-head h2', root), meta = $('.run-head span', root), tr = $('.tracker', root), sd = $('.stage-detail', root), rs = $('.result', root);

  const drawTracker = () => {
    tr.innerHTML = STEPS.map(([id, label]) => {
      const s = st.stages[id], status = s ? s.status : 'pending';
      return `<button class="node ${status} ${st.sel === id ? 'sel' : ''}" data-id="${id}" ${!s || status === 'running' ? 'disabled' : ''}>
        <span class="orb" aria-hidden="true">${ICON[status] ?? ''}</span>${label}</button>`;
    }).join('');
  };
  const drawDetail = () => {
    const s = st.sel ? st.stages[st.sel] : (st.live ? st.cur : null);
    if (!s) { sd.innerHTML = `<p style="margin:0;color:var(--muted)">Select a step above to see what it checked.</p>`; return; }
    sd.innerHTML = `<div class="sd-top">${esc(s.title)}<span>${s.status === 'running' ? 'Working' : s.ms ? s.ms + ' ms' : ''}</span></div>
      <p>${s.status === 'running' ? 'Working on it…' : esc(s.detail || '')}</p>
      ${s.status === 'running' ? '' : (() => { const x = findingsList(s) + stageExtra(s); return x ? `<div class="more">${x}</div>` : ''; })()}`;
  };
  tr.addEventListener('click', e => {
    const b = e.target.closest('.node'); if (!b || b.disabled) return;
    st.sel = st.sel === b.dataset.id ? null : b.dataset.id; drawTracker(); drawDetail();
  });
  rs.addEventListener('click', e => {
    const b = e.target.closest('.copy'); if (b) navigator.clipboard.writeText(b.dataset.body).then(() => toast('Copied'));
  });
  const firstProblem = () => STEPS.map(x => x[0]).find(id => st.stages[id] && ['warn', 'fail'].includes(st.stages[id].status)) || null;

  return {
    start(filename) { st.stages = {}; st.sel = null; st.cur = null; st.live = true; head.textContent = filename; meta.textContent = 'Running'; rs.innerHTML = ''; drawTracker(); drawDetail(); },
    stage(ev) { st.stages[ev.id] = ev; st.cur = ev; drawTracker(); drawDetail(); },
    finish(run) {
      st.live = false; st.sel = null; meta.textContent = `Done in ${run.duration_ms} ms`;
      drawTracker(); drawDetail(); rs.innerHTML = resultHTML(run);
    },
    load(run) {
      st.stages = {}; (run.stages || []).forEach(s => (st.stages[s.id] = s)); st.live = false; st.sel = null;
      head.textContent = run.filename; meta.textContent = `Run ${run.id}`; drawTracker(); drawDetail(); rs.innerHTML = resultHTML(run);
    },
  };
}
const live = createRunView($('#run'));

/* ------------------------------------------------------------ running */
async function streamRun(form, onEvent) {
  const res = await fetch('/api/process', { method: 'POST', body: form });
  if (!res.ok) { let m = 'Request failed'; try { m = (await res.json()).error || m; } catch { } throw new Error(m); }
  const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) onEvent(JSON.parse(line)); }
  }
}
async function runForm(form, filename) {
  $('#empty').hidden = true; $('#run').hidden = false; live.start(filename);
  let result = null;
  await streamRun(form, ev => {
    if (ev.type === 'stage') live.stage(ev); else if (ev.type === 'result') result = ev.run;
    else if (ev.type === 'error') throw new Error(ev.message);
  });
  if (result) live.finish(result);
  return result;
}
function setBusy(v) { busy = v; $$('.sample').forEach(b => (b.disabled = v)); $('#runall').disabled = v; $('#drop').style.pointerEvents = v ? 'none' : ''; }
async function guarded(fn) {
  if (busy) return; setBusy(true);
  try { await fn(); } catch (e) { toast('Something went wrong: ' + e.message, 6000); }
  finally { setBusy(false); }
}
async function runSample(name, delay) {
  const form = new FormData(); form.append('sample', name); if (delay != null) form.append('delay', delay);
  const row = $(`[data-file="${name}"]`); row && row.classList.add('running');
  try { const r = await runForm(form, name); if (r) { lastStatus[name] = r.status; renderSamples(); } return r; }
  finally { row && row.classList.remove('running'); }
}

/* ------------------------------------------------------------ samples + upload */
function renderSamples() {
  $('#samples').innerHTML = samples.map(s => `<li><button class="sample" data-file="${esc(s.file)}" title="${esc(s.description)}">
    <span class="nm">${esc(s.label.replace(/^Edge: /, ''))}</span>${lastStatus[s.file] ? chip(lastStatus[s.file]) : ''}</button></li>`).join('');
  $$('.sample').forEach(b => { b.disabled = busy; b.addEventListener('click', () => guarded(() => runSample(b.dataset.file))); });
}
$('#runall').addEventListener('click', () => guarded(async () => {
  let n = 0, off = 0;
  for (const s of samples) { const r = await runSample(s.file, 140); if (r) { n++; if (r.status !== s.expected) off++; } }
  toast(off ? `Ran ${n} invoices. ${off} differ from the expected outcome.` : `Ran ${n} invoices. All match the expected outcome.`, 6000);
}));
const drop = $('#drop'), fileInput = $('#file');
const upload = f => { const form = new FormData(); form.append('file', f); guarded(() => runForm(form, f.name)); };
fileInput.addEventListener('change', () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ''; });
drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) upload(f); });

/* ------------------------------------------------------------ dashboard */
async function loadDashboard() {
  const [stats, runs] = await Promise.all([fetch('/api/stats').then(r => r.json()), fetch('/api/runs').then(r => r.json())]);
  allRuns = runs;
  const by = stats.by_status, n = k => (by[k] ? by[k].count : 0), tot = stats.total;
  const order = ['APPROVED', 'NEEDS_REVIEW', 'ON_HOLD', 'REJECTED'];
  let acc = 0;
  const grad = tot ? order.map(k => { const a = acc / tot * 100; acc += n(k); return `${STATUS[k].color} ${a}% ${acc / tot * 100}%`; }).join(',') : '#dde3f0 0 100%';
  const rate = tot ? Math.round(n('APPROVED') / tot * 100) : 0;
  $('#kpis').innerHTML = `
    <div class="card kpi"><small>Invoices processed</small><b>${tot}</b><span class="s">${Math.round(stats.avg_ms)} ms on average</span></div>
    <div class="card kpi"><small>Approved value</small><b>${money(by.APPROVED ? by.APPROVED.value : 0)}</b><span class="s">${n('APPROVED')} invoices</span></div>
    <div class="card kpi"><small>Need a person</small><b>${n('NEEDS_REVIEW') + n('ON_HOLD')}</b><span class="s">${n('NEEDS_REVIEW')} to review, ${n('ON_HOLD')} on hold</span></div>
    <div class="card kpi mix"><div class="donut" style="background:conic-gradient(${grad})"><span>${rate}%</span></div>
      <div class="lg"><small style="color:var(--muted)">Approved with no human touch</small>${order.map(k => `<span><i style="background:${STATUS[k].color}"></i>${STATUS[k].label} <b>${n(k)}</b></span>`).join('')}</div></div>`;
  $('#filters').innerHTML = [['ALL', 'All']].concat(order.map(k => [k, STATUS[k].label])).map(([k, l]) =>
    `<button data-f="${k}" class="${filter === k ? 'on' : ''}">${l}</button>`).join('');
  $$('#filters button').forEach(b => b.addEventListener('click', () => { filter = b.dataset.f; $$('#filters button').forEach(x => x.classList.toggle('on', x === b)); drawRuns(); }));
  drawRuns();
}
function drawRuns() {
  const q = $('#search').value.toLowerCase();
  const rows = allRuns.filter(r => (filter === 'ALL' || r.status === filter) &&
    (!q || [r.vendor_name, r.invoice_number, r.filename, r.po_number].join(' ').toLowerCase().includes(q)));
  $('#runs tbody').innerHTML = rows.map(r => `<tr data-id="${r.id}"><td><b>${esc(r.vendor_name || 'Unknown')}</b></td><td data-label="Invoice">${esc(r.invoice_number || '—')}</td>
    <td data-label="PO">${esc(r.po_number || '—')}</td><td class="num" data-label="Total">${r.total == null ? '—' : money(r.total, r.currency || 'USD')}</td><td>${chip(r.status)}</td><td class="why">${esc(r.headline)}</td></tr>`).join('');
  $('#runs-empty').hidden = rows.length > 0;
  $$('#runs tbody tr').forEach(tr => tr.addEventListener('click', () => openDrawer(tr.dataset.id)));
}
$('#search').addEventListener('input', drawRuns);

async function openDrawer(id) {
  const run = await fetch('/api/runs/' + id).then(r => r.json());
  $('#drawer-body').innerHTML = '<div id="drawer-run"></div>';
  createRunView($('#drawer-run')).load(run);
  $('#scrim').hidden = $('#drawer').hidden = false;
}
const closeDrawer = () => { $('#scrim').hidden = $('#drawer').hidden = true; };
$('#scrim').addEventListener('click', closeDrawer); $('#drawer-close').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

/* ------------------------------------------------------------ purchase orders */
async function loadPOs() {
  const pos = await fetch('/api/pos').then(r => r.json());
  $('#pos').innerHTML = pos.map(p => {
    const pct = p.invoiced / p.amount * 100;
    return `<div class="card po"><div class="t"><b>${esc(p.po_number)}</b><span>${p.invoices} invoice${p.invoices === 1 ? '' : 's'}</span></div>
      <div class="v">${esc(p.vendor_name)}. ${esc(p.description)}</div>
      <div class="bar" role="img" aria-label="${Math.round(pct)} percent billed"><i class="${pct > 100 ? 'over' : 'b'}" style="width:${Math.min(100, pct)}%"></i></div>
      <div class="nums"><span>Billed <b>${money(p.invoiced, p.currency)}</b></span><span>Left <b>${money(p.remaining, p.currency)}</b></span></div></div>`;
  }).join('');
}

/* ------------------------------------------------------------ reset + init */
$('#reset').addEventListener('click', async () => {
  if (busy || !confirm('Delete all runs and restore PO balances?')) return;
  await fetch('/api/reset', { method: 'POST' });
  lastStatus = {}; renderSamples(); $('#run').hidden = true; $('#empty').hidden = false;
  toast('Demo reset. Samples are ready to run again.');
  if ($('#tab-dashboard').classList.contains('on')) loadDashboard();
  if ($('#tab-pos').classList.contains('on')) loadPOs();
});

(async function init() {
  samples = await fetch('/api/samples').then(r => r.json());
  const runs = await fetch('/api/runs').then(r => r.json());
  for (const r of runs.slice().reverse()) lastStatus[r.filename] = r.status;
  renderSamples();
})();
