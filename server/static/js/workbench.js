'use strict';

const $ = (s) => document.querySelector(s);
// 同一个工作台三种用法：/a/<packet> 标注 / 修复，/review/<submission> 审核
const REVIEW_SID = (() => {
  const m = location.pathname.match(/^\/review\/(\d+)\/?$/);
  return m ? parseInt(m[1], 10) : null;
})();
let PACKET_ID = REVIEW_SID === null ? location.pathname.split('/').pop() : '';
const INTERP_GAP = 2000;
const REVIEWER_KEY = 'ball_gt_reviewer';
const ANNOTATOR_KEY = 'ball_gt_annotator';

const W = {
  player: null,
  meta: null,
  labels: {},
  targets: [],
  mode: 'full',
  kind: 'human',
  ready: false,
  occluded: false,
  blurred: false,
  previewInterp: new Set(),
  previewTimer: 0,
};

function toast(msg, ms, action) { return gtToast(msg, ms, action); }

async function jget(u) {
  const r = await fetch(u);
  const d = await r.json().catch(() => ({ error: '返回不是 JSON' }));
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function warn(lines) {
  const el = $('#warnbar');
  if (!lines.length) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.textContent = '⚠ ' + lines.join('　|　');
}

function setEnhBar(text) {
  const el = $('#enhbar');
  if (!el) return;
  if (!text) {
    el.classList.remove('on');
    el.textContent = '';
    return;
  }
  el.classList.add('on');
  el.textContent = text;
}

function who() {
  const keys = REVIEW_SID === null ? [ANNOTATOR_KEY, REVIEWER_KEY] : [REVIEWER_KEY, ANNOTATOR_KEY];
  return keys.map((k) => (localStorage.getItem(k) || '').trim()).find(Boolean) || '';
}

function ensureWho() {
  let w = who();
  if (w) return w;
  w = (prompt('你的名字（会记进标注 / 审核记录）') || '').trim();
  if (w) {
    localStorage.setItem(ANNOTATOR_KEY, w);
    localStorage.setItem(REVIEWER_KEY, w);
  }
  return w;
}

function fmtTime(v) {
  if (v === null || v === undefined || v === '') return '—';
  const d = typeof v === 'number' ? new Date(v * 1000) : new Date(String(v));
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function renderPkInfo(rows) {
  $('#pkInfo').innerHTML = rows.filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join('');
}

/* 移除：数据本身有问题。标注、修复、审核三处共用。返回请求体，用户取消返回 null。 */
function askRemove() {
  if (!confirm('移除本包：数据质量本身有问题，不再需要标注，也不会再生产出来。\n已入库的帧会一并删除。\n\n只是没有球的话请取消，改成「本段不可见」后提交。')) {
    return null;
  }
  const reason = (prompt('移除原因（例如：画面花屏 / 标定错 / 视角不对）', '数据质量差') || '').trim() || 'bad_quality';
  const banVideo = confirm('这一整路源视频以后也不再使用吗？\n\n确定：停用整路视频，产包端不再从它切包\n取消：只移除这一个包');
  return { reason, ban_video: banVideo };
}

function willInterp(frames) {
  const vis = [];
  frames.forEach((f, i) => {
    if (f.visible && f.pano_x != null && !f.noLabel) vis.push(i);
  });
  const mark = new Set();
  for (let k = 0; k < vis.length - 1; k++) {
    const a = vis[k], b = vis[k + 1];
    if (b - a - 1 > INTERP_GAP) continue;
    for (let i = a + 1; i < b; i++) {
      const f = frames[i];
      if (f.noLabel || (f.visible === false && f.pano_x == null && !f.noLabel && W.labels[String(f.frame_idx)] && W.labels[String(f.frame_idx)].visible === false)) {
        if (!f.visible || f.noLabel) mark.add(i);
      }
      if (f.noLabel) mark.add(i);
    }
  }
  return mark;
}

function frameColor(f, P) {
  if (P.isRejected(f)) return '#f87171';
  const rec = W.labels[String(f.frame_idx)];
  if (rec && rec.visible === false) return GT_FRAME_COLORS.invisible;
  if (f.visible && f.pano_x != null && !f.noLabel) {
    return f.interp ? GT_FRAME_COLORS.interp : GT_FRAME_COLORS.ball;
  }
  const interp = willInterp(P.frames);
  if (interp.has(P.frames.indexOf(f))) return GT_FRAME_COLORS.interp;
  return GT_FRAME_COLORS.unlabeled;
}

function renderSide(P) {
  const f = P.cur();
  const n = P.frames.length;
  if (!f) { $('#kv').innerHTML = '<b>—</b><span>没有帧</span>'; return; }
  const rec = W.labels[String(f.frame_idx)];
  const nLabeled = Object.keys(W.labels).length;
  const nVis = Object.values(W.labels).filter((r) => r && r.visible && r.pano_x != null).length;
  const nInterp = willInterp(P.frames).size;
  const rows = [
    ['序号', `${P.i + 1} / ${n}`],
    ['frame_idx', f.frame_idx],
    ['pano_x/y', `${gtFmt(f.pano_x)}, ${gtFmt(f.pano_y)}`],
    ['状态', [
      rec ? (rec.visible ? '已标' : '不可见') : (f.interp ? '插值预览' : '未标'),
      rec && f.interp ? '插值' : '',
      P.isRejected(f) ? '打回' : '',
    ].filter(Boolean).join(' / ')],
  ];
  $('#kv').innerHTML = rows.map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join('');
  const nPrev = W.previewInterp.size;
  $('#progress').textContent = nPrev
    ? `已标 ${nVis} 可见 / ${nLabeled} 帧 · 全图预览插值 ${nPrev}`
    : `已标 ${nVis} 可见 / ${nLabeled} 帧 · 将插值 ${nInterp}`;
  const [lo, hi] = selBounds(P);
  const hint = $('#rangeHint');
  if (hint && P.frames[lo] && P.frames[hi]) {
    hint.textContent = P.rangeSet
      ? `已选段 ${P.frames[lo].frame_idx} → ${P.frames[hi].frame_idx}（${hi - lo + 1} 帧）· U 清除标注 / X 标不可见 · \\ 取消选段`
      : `单帧 ${P.frames[lo].frame_idx} · U 清除标注 / X 标不可见 · [ ] 选段 · A 全段选取`;
  }
}

async function saveLabel(rec) {
  const r = await fetch('/api/label/' + encodeURIComponent(PACKET_ID), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(rec),
  });
  const d = await r.json();
  if (!r.ok) { toast(d.error || '保存失败'); return false; }
  const key = String(rec.frame_idx);
  if (rec.clear) delete W.labels[key];
  else W.labels[key] = d.record;
  W.player.patchLabel(rec.frame_idx, rec.clear ? null : d.record);
  if (!rec.clear) W.player.rejected.delete(Number(rec.frame_idx));
  applyLocalInterp();
  return true;
}

function clearInterpPreview() {
  const P = W.player;
  if (!P) return;
  for (const fi of W.previewInterp) {
    if (!W.labels[String(fi)]) P.patchLabel(fi, null);
  }
  W.previewInterp.clear();
}

function applyLocalInterp() {
  const P = W.player;
  if (!P || !W.ready || W.mode === 'seed') return;
  clearInterpPreview();
  const panoW = P.panoW() || 0;
  const anchors = [];
  P.frames.forEach((f, i) => {
    const rec = W.labels[String(f.frame_idx)];
    if (rec && rec.visible && rec.pano_x != null && rec.pano_y != null) {
      anchors.push({ i, t: Number(f.frame_idx), x: Number(rec.pano_x), y: Number(rec.pano_y) });
    }
  });
  if (anchors.length >= 2 && panoW) {
    for (let k = 1; k < anchors.length; k++) {
      let x = anchors[k].x;
      const prev = anchors[k - 1].x;
      while (x - prev > panoW / 2) x -= panoW;
      while (prev - x > panoW / 2) x += panoW;
      anchors[k].x = x;
    }
  }
  for (let k = 0; k < anchors.length - 1; k++) {
    const a = anchors[k], b = anchors[k + 1];
    const dt = b.t - a.t;
    if (!dt) continue;
    for (let i = a.i + 1; i < b.i; i++) {
      const f = P.frames[i];
      const rec = W.labels[String(f.frame_idx)];
      if (rec && rec.visible === false) continue;
      if (rec && rec.visible && rec.pano_x != null) continue;
      const t = (Number(f.frame_idx) - a.t) / dt;
      let x = a.x + (b.x - a.x) * t;
      const y = a.y + (b.y - a.y) * t;
      if (panoW) x = ((x % panoW) + panoW) % panoW;
      W.previewInterp.add(Number(f.frame_idx));
      P.patchLabel(f.frame_idx, {
        frame_idx: f.frame_idx, visible: true, pano_x: x, pano_y: y, interp: true,
      });
    }
  }
  if (P.cur()) P.recenterOn(P.cur().frame_idx);
  P.draw();
}

async function markVisible(roiX, roiY) {
  if (!W.ready) { toast('还没下载到本地，不能改点。点「下载到本地以便修正」', 2200); return; }
  const P = W.player;
  const p = P.roiToPano(roiX, roiY);
  const fi = P.cur() && P.cur().frame_idx;
  if (fi == null) return;
  const ok = await saveLabel({
    frame_idx: fi, visible: true, pano_x: p.x, pano_y: p.y,
    occluded: W.occluded, blurred: W.blurred,
  });
  if (!ok) return;
  toast(`已修正帧 ${fi}`, 800);
  P.recenterOn(fi);
  P.draw();
}

function rangeFrameIdxs() {
  const P = W.player;
  const [lo, hi] = selBounds(P);
  return P.frames.slice(lo, hi + 1).map((f) => f.frame_idx);
}

async function applyRangeAction(action) {
  if (!W.ready) { toast('还没下载到本地，不能改标注', 1800); return; }
  const frames = rangeFrameIdxs();
  if (!frames.length) return;
  const r = await fetch('/api/label_batch/' + encodeURIComponent(PACKET_ID), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, frames }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || '本段操作失败', 2500); return; }
  const P = W.player;
  for (const fi of frames) {
    const key = String(fi);
    if (action === 'clear') {
      delete W.labels[key];
      P.patchLabel(fi, null);
    } else {
      const rec = { frame_idx: fi, visible: false, pano_x: null, pano_y: null };
      W.labels[key] = rec;
      P.patchLabel(fi, rec);
    }
    P.rejected.delete(Number(fi));
  }
  const a = frames[0], b = frames[frames.length - 1];
  const what = frames.length === 1 ? `帧 ${a}` : `${a} → ${b}（${frames.length} 帧）`;
  toast(action === 'clear'
    ? `${what} 已清除标注`
    : `${what} 已标不可见`, 1600);
  applyLocalInterp();
}

async function abandonPacket() {
  const w = ensureWho();
  if (!w) { toast('请先填写名字', 2500); return; }
  const ask = askRemove();
  if (!ask) return;
  const r = await fetch('/api/abandon/' + encodeURIComponent(PACKET_ID), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Annotator': w },
    body: JSON.stringify({ worker: w, ...ask }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || '移除失败', 3000); return; }
  toast(d.message || '已移除本包', 1600);
  setTimeout(() => { location.href = '/'; }, 1200);
}

async function submit() {
  const w = ensureWho();
  if (!w) { toast('请先填写名字', 2500); return; }
  const seedOnly = W.mode === 'seed';
  const headers = { 'Content-Type': 'application/json', 'X-Annotator': w };
  const body = { annotator: w, seed_only: seedOnly };
  if (!seedOnly) toast('正在生成全图审核包并提交…', 0);
  let r = await fetch('/api/finish/' + encodeURIComponent(PACKET_ID), {
    method: 'POST', headers, body: JSON.stringify(body),
  });
  let d = await r.json();
  if (!r.ok && d.missing && !seedOnly) {
    if (!confirm((d.error || '还有帧没标') + '。仍要提交吗？')) return;
    toast('正在强制提交…', 0);
    r = await fetch('/api/finish/' + encodeURIComponent(PACKET_ID), {
      method: 'POST', headers, body: JSON.stringify({ ...body, force: true }),
    });
    d = await r.json();
  }
  if (!r.ok) { toast(d.error || '提交失败', 3000); return; }
  toast(d.message || '已提交', 2000);
  const next = seedOnly ? '/?tab=label'
    : (d.has_bundle && d.review_url ? d.review_url : '/');
  setTimeout(() => { location.href = next; }, 900);
}

async function startDownload() {
  const w = who();
  toast('开始下载并解码…', 0);
  const r = await fetch('/api/prepare/' + encodeURIComponent(PACKET_ID), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Annotator': w },
    body: JSON.stringify({ worker: w }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || '无法开始下载', 2500); return; }
  for (let i = 0; i < 3600; i++) {
    await new Promise((res) => setTimeout(res, 1000));
    const s = await fetch('/api/job/' + encodeURIComponent(PACKET_ID)).then((x) => x.json());
    toast((s.msg || s.state || '准备中') + (s.total ? `  ${s.done || 0}/${s.total}` : ''), 0);
    if (s.state === 'raw_ready' || s.state === 'enhancing' || s.state === 'ready') {
      location.reload();
      return;
    }
    if (s.state === 'error') { toast(s.msg || '准备失败', 3000); return; }
  }
  toast('准备超时', 2500);
}

async function watchEnh() {
  let sawEnh = false;
  for (let i = 0; i < 3600; i++) {
    let s;
    try {
      s = await fetch('/api/job/' + encodeURIComponent(PACKET_ID)).then((x) => x.json());
    } catch (e) {
      return;
    }
    if (s.enh_ready || s.state === 'ready') {
      if (W.player) W.player.retryFailedEnh();
      setEnhBar('');
      if (sawEnh) toast('增强图已就绪，按 W 可切换', 1800);
      return;
    }
    if (s.state === 'enhancing' || s.state === 'raw_ready') {
      sawEnh = true;
      setEnhBar(`增强图后台生成  ${s.done || 0}/${s.total || 0}  · 可先用原图标注`);
      if (W.player) W.player.retryFailedEnh();
    } else if (s.state === 'error') {
      setEnhBar('');
      toast(s.msg || '增强图生成失败', 3000);
      return;
    }
    await new Promise((res) => setTimeout(res, 1000));
  }
}

function applyRejected(need) {
  const P = W.player;
  P.rejected = new Set((need || []).map(Number));
}

async function loadLocal(meta) {
  W.meta = meta;
  W.labels = meta.labels || {};
  W.targets = meta.targets || [];
  W.mode = meta.mode || 'full';
  W.kind = meta.kind || 'human';
  W.ready = true;
  const P = W.player;
  P.editable = true;
  const warns = P.loadLocal(meta, W.labels, meta.frames || meta.targets);
  if (meta.has_scan) P.scanUrl = `/img/${encodeURIComponent(meta.packet_id || PACKET_ID)}/scan_time.jpg`;
  if (W.mode === 'repair' || W.kind === 'repair') {
    applyRejected(meta.target_frames || meta.targets);
  }
  $('#pid').textContent = meta.packet_id || PACKET_ID;
  $('#modePill').textContent = W.mode === 'seed' ? '预标几帧（帮 AI 起步）'
    : (W.mode === 'repair' ? '修复打回帧' : '整包标注');
  $('#srcPill').textContent = '本地原图 · 可修正';
  $('#srcPill').className = 'pill ok';
  $('#bSubmit').textContent = W.mode === 'seed' ? '保存预标' : '提交';
  $('#bDownload').style.display = 'none';
  renderPkInfo([
    ['生产', fmtTime(meta.produced_at)],
    ['来源', esc([meta.group, meta.stem, meta.tag].filter(Boolean).join(' / '))],
    ['采样', esc(meta.sampling || '')],
    ['提交后', W.mode === 'seed' ? '仍在待标注，AI 优先领' : '直接入库'],
  ]);
  warn(warns);
  const first = P.frames.findIndex((f) => P.isRejected(f) || f.noLabel);
  P.seek(first >= 0 ? first : 0, true);
  applyLocalInterp();
  watchEnh();
}

async function loadBundleFallback() {
  let subs;
  try {
    subs = await jget('/api/v2/submissions?packet_id=' + encodeURIComponent(PACKET_ID) + '&state=all&source=all&limit=20');
  } catch (e) {
    return false;
  }
  const withBundle = (subs.submissions || []).find((s) => s.has_thumb || s.has_bundle);
  if (!withBundle) return false;
  const d = await jget('/api/v2/submission/' + withBundle.submission_id);
  const P = W.player;
  P.editable = false;
  const warns = P.loadThumb(d);
  warns.push('尚未下载到本地，现在用缩略视频只读。下载高清后才能点选修正。');
  const need = (d.packet && d.packet.need_frames) || [];
  applyRejected(need);
  $('#pid').textContent = PACKET_ID;
  $('#srcPill').textContent = '线上缩略视频 · 只读';
  $('#srcPill').className = 'pill warn';
  $('#bDownload').style.display = '';
  renderPkInfo([['生产', fmtTime(d.packet && d.packet.produced_at)]]);
  warn(warns);
  P.seek(0);
  return true;
}

/* ---------------------------------------------------------------- 审核模式 */

const R = { data: null, isManual: false };

/* 提交结果由错误帧推定：没有 = 通过，全部 = 重标，部分 = 部分人工修复 */
function effectiveDecision() {
  const P = W.player;
  if (!P || !P.rejected.size) return 'accept';
  return P.frames.every((f) => P.rejected.has(f.frame_idx)) ? 'relabel' : 'repair';
}

const DECISION_TEXT = {
  accept: ['通过：整包入库', 'var(--ok)'],
  repair: ['部分人工修复', 'var(--warn)'],
  relabel: ['重标：回待领取池', 'var(--bad)'],
};

function renderReviewSide(P) {
  const f = P.cur();
  if (!f) { $('#kv').innerHTML = '<b>—</b><span>没有帧</span>'; return; }
  const cf = f.conf === null || f.conf === undefined ? '—' : f.conf.toFixed(2);
  const rows = [
    ['序号', `${P.i + 1} / ${P.frames.length}`],
    ['frame_idx', f.frame_idx],
    ['pano_x/y', `${gtFmt(f.pano_x)}, ${gtFmt(f.pano_y)}`],
    ['conf', cf],
    ['状态', [f.visible ? '可见' : '不可见', f.interp ? '插值' : '', f.occluded ? '遮挡' : '',
              f.blurred ? '拖影' : '', f.noLabel ? 'labels 无此帧' : ''].filter(Boolean).join(' / ')],
    ['本帧', P.isRejected(f) ? '<span style="color:var(--bad)">错误帧</span>' : '正确'],
  ];
  $('#kv').innerHTML = rows.map(([k, v]) => `<b>${k}</b><span>${v}</span>`).join('');
  const dec = effectiveDecision();
  const [txt, color] = DECISION_TEXT[dec];
  const extra = dec === 'repair' ? `（错误帧 ${P.rejected.size}，其余入库）` : '';
  $('#decInfo').innerHTML = `提交后：<b style="color:${color}">${txt}</b>${extra}`
    + '<br>没有错误帧 = 通过；全部错误 = 重标；部分错误 = 部分人工修复';
  const [lo, hi] = selBounds(P);
  const nBad = P.frames.slice(lo, hi + 1).filter((fr) => P.isRejected(fr)).length;
  $('#rangeHint').textContent = !P.frames[lo] ? ''
    : P.rangeSet
      ? `已选段 ${P.frames[lo].frame_idx} → ${P.frames[hi].frame_idx}（${hi - lo + 1} 帧，错误 ${nBad}）· O 标记正确 / X 标记错误 · \\ 取消选段`
      : `单帧 ${P.frames[lo].frame_idx} · O 标记正确 / X 标记错误 · [ ] 选段 · A 全段选取`;
  $('#progress').textContent = `错误帧 ${P.rejected.size} / ${P.frames.length}`;
}

/* 没选段时只作用于当前帧 */
function selBounds(P) {
  return P.rangeSet ? P.rangeBounds() : [P.i, P.i];
}

function markSel(ok) {
  const P = W.player;
  const [lo, hi] = selBounds(P);
  if (!P.frames[lo]) return;
  for (let i = lo; i <= hi; i++) {
    const f = P.frames[i];
    if (!f) continue;
    if (ok) P.rejected.delete(f.frame_idx);
    else P.rejected.add(f.frame_idx);
  }
  const what = lo === hi ? `帧 ${P.frames[lo].frame_idx}`
    : `${P.frames[lo].frame_idx} → ${P.frames[hi].frame_idx}（${hi - lo + 1} 帧）`;
  toast(`${what} 标记为${ok ? '正确' : '错误'}`, 1200);
  P.draw();
}

async function postReview(body) {
  const r = await fetch('/api/v2/review/' + REVIEW_SID, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({ error: '返回不是 JSON' }));
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

async function submitDecision() {
  const rv = ensureWho();
  if (!rv) { toast('请先填写审核员名字', 2200); return; }
  const P = W.player;
  const dec = effectiveDecision();
  const frames = {};
  if (dec === 'repair') P.rejected.forEach((fi) => { frames[String(fi)] = false; });
  const desc = {
    accept: '通过，整包入库',
    repair: `部分人工修复：${P.rejected.size} 帧错误帧进修复，其余入库`,
    relabel: '重标：清空入库、回待领取池，同一方法不能再领',
  }[dec];
  if (!confirm(`提交：${desc}\n审核员：${rv}`)) return;
  P.stop();
  $('#bDecide').disabled = true;
  try {
    afterReview(await postReview({ reviewer: rv, decision: dec, frames, notes: $('#notes').value || '' }));
  } catch (e) {
    toast('提交失败：' + (e.message || e), 4000);
  }
  $('#bDecide').disabled = false;
}

async function removeFromReview() {
  const rv = ensureWho();
  if (!rv) { toast('请先填写审核员名字', 2200); return; }
  const ask = askRemove();
  if (!ask) return;
  W.player.stop();
  try {
    afterReview(await postReview({ reviewer: rv, decision: 'remove', notes: $('#notes').value || '', ...ask }));
  } catch (e) {
    toast('移除失败：' + (e.message || e), 4000);
  }
}

function listUrl() {
  return R.isManual ? '/?tab=review' : '/review';
}

function afterReview(d) {
  const label = { accept: '通过', repair: '部分人工修复', relabel: '重标', remove: '移除' }[d.decision] || d.decision;
  const parts = [`已提交：${label}`];
  if (d.n_gt_written) parts.push(`入库 ${d.n_gt_written} 帧`);
  if (d.decision === 'repair') parts.push(`待修 ${d.n_rejected} 帧`);
  if (d.n_gt_removed) parts.push(`从 GT 删 ${d.n_gt_removed} 帧`);
  if (d.banned_video) parts.push(`已停用视频 ${d.banned_video}`);
  const head = parts.join(' · ');
  if (R.isManual && d.decision === 'repair') {
    toast(head + '。可以去待修复列表修这几帧', 0, {
      label: '去修复', fn: () => { location.href = '/?tab=repair'; },
    });
    return;
  }
  const next = R.data && R.data.next_submission_id;
  if (!next) {
    toast(head + '，返回队列…', 0);
    setTimeout(() => { location.href = listUrl(); }, 1500);
    return;
  }
  let left = 2;
  let timer = null;
  const cancel = () => { clearInterval(timer); gtHideToast(); toast('已留在本页', 1500); };
  const show = () => toast(`${head}。${left}s 后审下一条 #${next}`, 0, { label: '留在本页', fn: cancel });
  show();
  timer = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(timer); location.href = '/review/' + next; } else show();
  }, 1000);
}

async function loadReview() {
  let d;
  try { d = await jget('/api/v2/submission/' + REVIEW_SID); }
  catch (e) { warn(['加载失败：' + e.message]); return; }
  R.data = d;
  const P = W.player;
  P.rejected = new Set();
  P.decision = null;
  const s = d.submission || {};
  const p = d.packet || {};
  PACKET_ID = s.packet_id || p.packet_id || '';
  const warns = P.loadBundle(d);
  if (!P.hasCells && !(P.source && P.source.kind === 'thumb')) {
    warns.push('这条提交没有缩略视频，只能看曲线与数值表，仍可裁决');
  }
  if (s.state && s.state !== 'pending') {
    warns.push(`这条提交已审过（${s.state}），可以再审；重标 / 移除会删掉已入库的帧`);
  }
  if ((p.blocked_methods || []).length) {
    warns.push(`本包已被重标过 ${p.blocked_methods.length} 次`);
  }
  $('#pid').textContent = PACKET_ID;
  $('#modePill').textContent = `审核 #${s.submission_id}`;
  $('#srcPill').textContent = s.source === 'human' ? '人工提交' : 'AI 提交';
  $('#srcPill').className = 'pill ' + (s.source === 'human' ? 'ok' : 'warn');
  const meta = p.meta || {};
  const qc = (s.qc && s.qc.flags) || [];
  renderPkInfo([
    ['标注方法', esc(s.method_tag || [s.model_used, s.workflow].filter(Boolean).join(' · ') || '—')],
    ['标注员', esc(s.worker || '—')],
    ['生产', fmtTime(p.produced_at)],
    ['标注', fmtTime(s.created_at)],
    ['审核', fmtTime(p.reviewed_at)],
    ['覆盖', `${s.n_labeled || 0}/${s.n_targets || 0} · 可见 ${s.n_visible || 0} · 插值 ${s.n_interp || 0}`],
    ['QC', qc.length ? `<span style="color:var(--bad)">${esc(qc.join(' / '))}</span>` : '无标记'],
    ['来源', esc([meta.group, meta.stem, meta.sampling].filter(Boolean).join(' / '))],
    ['历史审核', (d.reviews || []).length ? `${d.reviews.length} 条` : ''],
  ]);
  warn(warns);
  P.seek(0);
}

async function bootReview() {
  try {
    const w = await jget('/api/whoami');
    R.isManual = w.mode === 'manual';
  } catch (e) { /* 公网站也有 whoami */ }
  $('#bList').href = listUrl();
  $('#bList').textContent = '返回队列';
  $('#labelBtns').style.display = 'none';
  $('#labelMarks').style.display = 'none';
  $('#revBtns').style.display = '';
  $('#revSide').style.display = '';
  $('#revKeys').style.display = '';
  $('#labelKeys').style.display = 'none';
  $('#bDownload').style.display = 'none';
  $('#revMarks').style.display = '';
  $('#bSelAll').style.display = '';
  $('#bClrRange').textContent = '取消选段 \\';
  $('#zoomTitle').textContent = '局部放大';
  W.player = new GTPlayer({
    onDraw: renderReviewSide, singleDefault: true, clearRangeText: '已取消选段，回到单帧',
  });
  W.player.bindTransport();
  $('#bMarkOk').onclick = () => markSel(true);
  $('#bMarkBad').onclick = () => markSel(false);
  $('#bSelAll').onclick = () => W.player.selectAll();
  $('#bDecide').onclick = submitDecision;
  $('#bRemove').onclick = removeFromReview;
  window.addEventListener('keydown', (e) => {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) {
      if (e.code === 'Escape') t.blur();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (W.player.handlePlayKey(e)) return;
    switch (e.code) {
      case 'KeyO': markSel(true); return;
      case 'KeyX': case 'KeyB': markSel(false); return;
      case 'KeyA': W.player.selectAll(); return;
      case 'KeyS': e.preventDefault(); submitDecision(); return;
      case 'KeyR': loadReview(); return;
      case 'Escape': gtHideToast(); return;
      default: return;
    }
  });
  await loadReview();
}

async function boot() {
  if (REVIEW_SID !== null) { await bootReview(); return; }
  $('#bList').href = '/';
  $('#bSelAll').style.display = '';
  $('#bClrRange').textContent = '取消选段 \\';
  W.player = new GTPlayer({
    frameColor,
    centerMode: 'prev',
    onDraw: renderSide,
    onMark: (m) => markVisible(m.roiX, m.roiY),
    singleDefault: true,
    clearRangeText: '已取消选段，回到单帧',
  });
  W.player.bindTransport();
  $('#bInvisible').onclick = () => applyRangeAction('invisible');
  $('#bClear').onclick = () => applyRangeAction('clear');
  $('#bSelAll').onclick = () => W.player.selectAll();
  $('#bAbandon').onclick = abandonPacket;
  $('#bSubmit').onclick = submit;
  $('#bDownload').onclick = startDownload;
  $('#bDownload').style.display = 'none';

  window.addEventListener('keydown', (e) => {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) {
      if (e.code === 'Escape') t.blur();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (W.player && W.player.handlePlayKey(e)) return;
    switch (e.code) {
      case 'KeyX': applyRangeAction('invisible'); return;
      case 'KeyU': case 'KeyD': applyRangeAction('clear'); return;
      case 'KeyA': W.player.selectAll(); return;
      case 'KeyS': e.preventDefault(); submit(); return;
      case 'Escape': gtHideToast(); return;
      default: return;
    }
  });

  let meta = null;
  try {
    meta = await jget('/api/meta/' + encodeURIComponent(PACKET_ID));
  } catch (e) {
    const ok = await loadBundleFallback();
    if (!ok) {
      warn(['包还没准备好：' + e.message, '可从列表重新领取，或点「下载到本地以便修正」']);
      $('#bDownload').style.display = '';
      $('#pid').textContent = PACKET_ID;
    }
    return;
  }
  await loadLocal(meta);
}

boot();
