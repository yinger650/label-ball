'use strict';

const $ = (s) => document.querySelector(s);
const LS = 'ball_gt_annotator';
const TAB_KEY = 'ball_gt_home_tab';

const S = { tab: 'label', rows: [], busy: false, selected: new Set() };

function who() {
  const el = $('#who');
  if (!el) return '';
  const v = (el.value || '').trim();
  localStorage.setItem(LS, v);
  return v;
}

function setStatus(msg, cls) {
  const el = $('#status');
  if (!el) return;
  el.textContent = msg;
  el.className = cls || '';
}

function setBusy(on, title, msg, cls) {
  S.busy = !!on;
  const overlay = $('#busy');
  document.body.classList.toggle('busy', !!on);
  if (overlay) {
    overlay.classList.toggle('on', !!on);
    if (title) $('#busyTitle').textContent = title;
    if (msg != null) $('#busyText').textContent = msg;
  }
  if (msg) setStatus(msg, cls);
}

function tabFromURL() {
  const t = new URLSearchParams(location.search).get('tab');
  if (t === 'review' || t === 'repair' || t === 'label') return t;
  return localStorage.getItem(TAB_KEY) || 'label';
}

function setTab(tab) {
  S.tab = tab === 'review' || tab === 'repair' ? tab : 'label';
  localStorage.setItem(TAB_KEY, S.tab);
  const url = S.tab === 'label' ? '/' : '/?tab=' + S.tab;
  if (location.pathname + location.search !== url) history.replaceState(null, '', url);
  $('#tabLabel').classList.toggle('on', S.tab === 'label');
  $('#tabReview').classList.toggle('on', S.tab === 'review');
  $('#tabRepair').classList.toggle('on', S.tab === 'repair');
  $('#seedNext').style.display = S.tab === 'label' ? '' : 'none';
  $('#selAll').style.display = S.tab === 'repair' ? '' : 'none';
  $('#toLabel').style.display = S.tab === 'repair' ? '' : 'none';
  $('#next').textContent = S.tab === 'review' ? '审第一条' : (S.tab === 'repair' ? '修下一个' : '领取下一个');
  if (S.tab !== 'repair') S.selected.clear();
  syncSelUI();
}

async function jget(u) {
  const r = await fetch(u);
  const d = await r.json().catch(() => ({ error: '返回不是 JSON' }));
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function claimDone() {
  setBusy(false);
  $('#next').disabled = false;
  $('#seedNext').disabled = false;
}

async function claim(kind, mode, packetId) {
  const w = who();
  if (!w) { setStatus('先填标注员名字', 'bad'); return; }
  const title = mode === 'seed' ? '正在预标领包' : (mode === 'repair' ? '正在领取修复包' : '正在领取标注包');
  setBusy(true, title, packetId ? ('正在领取 ' + packetId + '…') : '正在向服务端领包…');
  $('#next').disabled = true;
  $('#seedNext').disabled = true;
  let r, d = {};
  try {
    r = await fetch('/api/claim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Annotator': w },
      body: JSON.stringify({ kind, mode, worker: w, packet_id: packetId || '' }),
    });
  } catch (e) {
    setBusy(true, '领取失败', '本机人工站没在跑或已断开。请在终端重开 client.manual 后刷新本页。', 'bad');
    return;
  }
  try { d = await r.json(); } catch (e) {
    setBusy(true, '领取失败', '领取失败（HTTP ' + r.status + '，响应不是 JSON）', 'bad');
    return;
  }
  if (!r.ok) {
    setBusy(true, '领取失败', d.error || ('领取失败 HTTP ' + r.status), 'bad');
    return;
  }
  if (d.resume) {
    setBusy(true, title, '已就绪，正在打开工作台…');
    location.href = '/a/' + encodeURIComponent(d.packet_id);
    return;
  }
  const pid = d.packet_id;
  setBusy(true, title, '已领到 ' + pid + '，正在下载并解码…');
  for (let i = 0; i < 3600; i++) {
    await new Promise((res) => setTimeout(res, 1000));
    let s;
    try {
      s = await fetch('/api/job/' + encodeURIComponent(pid)).then((x) => x.json());
    } catch (e) {
      setBusy(true, '准备中断', '准备过程中本机站断开，请重开 client.manual 后刷新', 'bad');
      return;
    }
    const line = (s.msg || s.state || '准备中') + (s.total ? `  ${s.done || 0}/${s.total}` : '');
    setBusy(true, title, line);
    if (s.state === 'raw_ready' || s.state === 'enhancing' || s.state === 'ready') {
      setBusy(true, title, '原图已就绪，正在打开工作台…');
      location.href = '/a/' + encodeURIComponent(pid);
      return;
    }
    if (s.state === 'error') {
      setBusy(true, '准备失败', s.msg || '准备失败', 'bad');
      return;
    }
  }
  setBusy(true, '准备超时', '准备超时，请刷新后重试', 'bad');
}

function renderLabel(rows) {
  if (!rows.length) {
    $('#tableWrap').innerHTML = '<div class="dim">没有可领的包</div>';
    return;
  }
  $('#tableWrap').innerHTML = `<table><thead><tr>
    <th>packet</th><th>状态</th><th>预标</th><th>目标帧</th><th></th>
  </tr></thead><tbody>${rows.map((p) => {
    const seed = p.has_seed
      ? `<span class="tag ok">已预标 ${p.n_seed || ''}</span>`
      : '<span class="dim">—</span>';
    return `<tr class="q" data-pid="${p.packet_id}">
      <td class="mono">${p.packet_id}<div class="dim">${p.sampling || ''} · ${p.stem || ''}</div></td>
      <td><span class="tag">${p.state}</span></td>
      <td>${seed}${p.seed_worker ? `<div class="dim">${p.seed_worker}</div>` : ''}</td>
      <td>${p.n_targets || '—'}</td>
      <td style="text-align:right">
        <button class="sm" data-act="seed">预标</button>
        <button class="sm primary" data-act="full">标注</button>
      </td></tr>`;
  }).join('')}</tbody></table>`;
  $('#tableWrap').querySelectorAll('tr.q').forEach((tr) => {
    const pid = tr.dataset.pid;
    tr.querySelector('[data-act="full"]').onclick = (e) => { e.stopPropagation(); claim('human', 'full', pid); };
    tr.querySelector('[data-act="seed"]').onclick = (e) => { e.stopPropagation(); claim('human', 'seed', pid); };
    tr.onclick = () => claim('human', 'full', pid);
  });
}

function renderReview(rows) {
  if (!rows.length) {
    $('#tableWrap').innerHTML = '<div class="dim">没有待审的提交</div>';
    return;
  }
  $('#tableWrap').innerHTML = `<table><thead><tr>
    <th>submission</th><th>packet</th><th>来源</th><th>覆盖</th><th>置信</th><th>QC</th><th></th>
  </tr></thead><tbody>${rows.map((r) => {
    const cov = r.coverage == null ? '—' : Math.round(r.coverage * 100) + '%';
    const tags = (r.qc_flags || []).map((f) => `<span class="tag warn">${f}</span>`).join('')
      + (r.has_bundle ? '' : '<span class="tag warn">无审核包</span>');
    return `<tr class="q" data-id="${r.submission_id}">
      <td class="mono">#${r.submission_id}</td>
      <td class="mono">${r.packet_id}</td>
      <td>${r.source === 'human' ? '人工' : (r.source || '—')}</td>
      <td>${r.n_labeled}/${r.n_targets}（${cov}）</td>
      <td>${r.mean_conf == null ? '—' : r.mean_conf.toFixed(2)}</td>
      <td>${tags || '—'}</td>
      <td style="text-align:right"><button class="sm primary">审核</button></td></tr>`;
  }).join('')}</tbody></table>`;
  $('#tableWrap').querySelectorAll('tr.q').forEach((tr) => {
    tr.onclick = () => { location.href = '/review/' + tr.dataset.id; };
  });
}

function syncSelUI() {
  const n = S.selected.size;
  const btn = $('#toLabel');
  if (btn) {
    btn.disabled = !n;
    btn.textContent = n ? `打回待标注（${n}）` : '打回待标注';
  }
  const all = $('#selAll');
  if (all && S.tab === 'repair') {
    const nRows = S.rows.length;
    all.textContent = nRows && n === nRows ? '取消全选' : '全选';
    all.disabled = !nRows;
  }
  $('#tableWrap').querySelectorAll('tr.q[data-pid]').forEach((tr) => {
    const on = S.selected.has(tr.dataset.pid);
    tr.classList.toggle('sel', on);
    const cb = tr.querySelector('input[type=checkbox]');
    if (cb) cb.checked = on;
  });
}

function toggleSel(pid, on) {
  if (!pid) return;
  if (on == null) on = !S.selected.has(pid);
  if (on) S.selected.add(pid);
  else S.selected.delete(pid);
  syncSelUI();
}

function toggleSelAll() {
  const ids = S.rows.map((p) => p.packet_id).filter(Boolean);
  const allOn = ids.length && ids.every((id) => S.selected.has(id));
  S.selected.clear();
  if (!allOn) ids.forEach((id) => S.selected.add(id));
  syncSelUI();
}

function renderRepair(rows) {
  if (!rows.length) {
    S.selected.clear();
    $('#tableWrap').innerHTML = '<div class="dim">没有待修复的包</div>';
    syncSelUI();
    return;
  }
  const keep = new Set(rows.map((p) => p.packet_id));
  [...S.selected].forEach((id) => { if (!keep.has(id)) S.selected.delete(id); });
  $('#tableWrap').innerHTML = `<table><thead><tr>
    <th class="chk"><input type="checkbox" id="chkAll" title="全选"></th>
    <th>packet</th><th>待修帧</th><th>来源</th><th></th>
  </tr></thead><tbody>${rows.map((p) => {
    const n = Array.isArray(p.need_frames) ? p.need_frames.length : '—';
    const on = S.selected.has(p.packet_id);
    return `<tr class="q${on ? ' sel' : ''}" data-pid="${p.packet_id}">
      <td class="chk"><input type="checkbox"${on ? ' checked' : ''}></td>
      <td class="mono">${p.packet_id}</td>
      <td class="err">待修 ${n} 帧</td>
      <td>${p.worker || p.model_used || '—'}</td>
      <td style="text-align:right"><button class="sm primary" data-act="fix">修复</button></td></tr>`;
  }).join('')}</tbody></table>`;
  const wrap = $('#tableWrap');
  const chkAll = wrap.querySelector('#chkAll');
  if (chkAll) {
    chkAll.checked = rows.length && rows.every((p) => S.selected.has(p.packet_id));
    chkAll.onclick = (e) => { e.stopPropagation(); toggleSelAll(); };
  }
  wrap.querySelectorAll('tr.q').forEach((tr) => {
    const pid = tr.dataset.pid;
    const fix = tr.querySelector('[data-act="fix"]');
    if (fix) fix.onclick = (e) => { e.stopPropagation(); claim('repair', 'repair', pid); };
    tr.onclick = (e) => {
      if (e.target.closest('button')) return;
      const cb = tr.querySelector('input[type=checkbox]');
      if (e.target === cb) { toggleSel(pid, cb.checked); return; }
      toggleSel(pid);
    };
  });
  syncSelUI();
}

async function requeueSelected() {
  const ids = [...S.selected];
  if (!ids.length) { setStatus('先勾选要打回的包', 'bad'); return; }
  if (!confirm(`把 ${ids.length} 个包从待修复打回待标注？\n之后按整包重标，不再只修打回帧。`)) return;
  setBusy(true, '打回待标注', `正在处理 ${ids.length} 个包…`);
  try {
    const r = await fetch('/api/v2/requeue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ packet_ids: ids, reason: 'requeue_label' }),
    });
    const d = await r.json().catch(() => ({ error: '返回不是 JSON' }));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    const n = (d.moved || []).length;
    const skip = (d.skipped || []).length;
    const msg = skip ? `已打回 ${n} 个，跳过 ${skip} 个` : `已打回待标注 ${n} 个`;
    const cls = n ? 'ok' : 'bad';
    S.selected.clear();
    setBusy(false);
    await load();
    setStatus(msg, cls);
  } catch (e) {
    setBusy(false);
    setStatus(e.message || String(e), 'bad');
  }
}

async function load() {
  setTab(S.tab);
  try {
    if (S.tab === 'review') {
      const d = await jget('/api/v2/submissions?state=pending&source=all&limit=500');
      S.rows = d.submissions || [];
      $('#count').textContent = S.rows.length ? `${S.rows.length} 条待审` : '没有待审';
      $('#next').disabled = !S.rows.length;
      renderReview(S.rows);
    } else if (S.tab === 'repair') {
      const d = await jget('/api/v2/packets?state=need_human&limit=500');
      S.rows = d.packets || [];
      $('#count').textContent = S.rows.length ? `${S.rows.length} 个待修` : '没有待修';
      $('#next').disabled = !S.rows.length;
      renderRepair(S.rows);
    } else {
      const d = await jget('/api/v2/packets?state=available&limit=500');
      S.rows = d.packets || [];
      $('#count').textContent = S.rows.length ? `${S.rows.length} 个可标` : '没有可标';
      $('#next').disabled = !S.rows.length;
      renderLabel(S.rows);
    }
    if (!S.busy) {
      setStatus(S.tab === 'repair'
        ? '勾选后可打回待标注；点「修复」只修打回帧。'
        : '点某一行打开；或用上面的按钮领池子里的下一个。');
    }
  } catch (e) {
    $('#count').innerHTML = `<span class="err">加载失败：${e.message}</span>`;
    setStatus(e.message || String(e), 'bad');
  }
}

function nextAction() {
  if (S.tab === 'review') {
    if (S.rows.length) location.href = '/review/' + S.rows[0].submission_id;
    return;
  }
  if (S.tab === 'repair') {
    claim('repair', 'repair', S.rows[0] && S.rows[0].packet_id);
    return;
  }
  claim('human', 'full');
}

$('#who').value = localStorage.getItem(LS) || '';
$('#tabLabel').onclick = () => { S.tab = 'label'; load(); };
$('#tabReview').onclick = () => { S.tab = 'review'; load(); };
$('#tabRepair').onclick = () => { S.tab = 'repair'; load(); };
$('#refresh').onclick = load;
$('#next').onclick = nextAction;
$('#seedNext').onclick = () => claim('human', 'seed');
$('#selAll').onclick = toggleSelAll;
$('#toLabel').onclick = requeueSelected;
S.tab = tabFromURL();
jget('/api/whoami').then(load).catch((e) => {
  $('#count').innerHTML = '<span class="err">本机站未运行</span>';
  setStatus('本机人工站没在跑。请在终端执行：venv/bin/python -m client.manual --server https://ballgt.yinger650.com --worker 你的名字', 'bad');
});
