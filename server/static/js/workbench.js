'use strict';

const $ = (s) => document.querySelector(s);
const PACKET_ID = location.pathname.split('/').pop();
const INTERP_GAP = 2000;

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
  return (localStorage.getItem('ball_gt_annotator') || '').trim();
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
  if (rec && rec.visible === false) return '#5b6270';
  if (f.visible && f.pano_x != null && !f.noLabel) {
    return f.interp ? '#fbbf24' : '#4ade80';
  }
  const interp = willInterp(P.frames);
  if (interp.has(P.frames.indexOf(f))) return '#fbbf24';
  return '#3d4450';
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
  const [lo, hi] = P.rangeBounds();
  const hint = $('#rangeHint');
  if (hint && P.frames[lo] && P.frames[hi]) {
    hint.textContent = `本段 ${P.frames[lo].frame_idx} → ${P.frames[hi].frame_idx}（${hi - lo + 1} 帧）· 清除标注会去掉红叉 · 没有球用本段不可见`;
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

async function markInvisible() {
  if (!W.ready) { toast('还没下载到本地，不能改点', 1800); return; }
  const f = W.player.cur();
  if (!f) return;
  if (await saveLabel({ frame_idx: f.frame_idx, visible: false, occluded: W.occluded, blurred: W.blurred })) {
    toast(`帧 ${f.frame_idx} 标为不可见`, 900);
    W.player.draw();
  }
}

async function clearLabel() {
  if (!W.ready) return;
  const f = W.player.cur();
  if (!f) return;
  if (await saveLabel({ frame_idx: f.frame_idx, clear: true })) {
    W.player.rejected.delete(Number(f.frame_idx));
    toast(`已清除帧 ${f.frame_idx}`, 800);
    W.player.draw();
  }
}

function rangeFrameIdxs() {
  const P = W.player;
  const [lo, hi] = P.rangeBounds();
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
  toast(action === 'clear'
    ? `本段 ${a} → ${b} 已清除标注和红叉（${frames.length} 帧）`
    : `本段 ${a} → ${b} 已标不可见（${frames.length} 帧）`, 1600);
  applyLocalInterp();
}

async function abandonPacket() {
  const w = who();
  if (!w) { toast('请先回列表页填写标注员名字', 2500); return; }
  if (!confirm('抛弃本包：数据质量不行，将从审核和修复队列移除，且不入库。\n\n如果只是没有球，请取消，改用「本段不可见」再提交。')) {
    return;
  }
  const r = await fetch('/api/abandon/' + encodeURIComponent(PACKET_ID), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Annotator': w },
    body: JSON.stringify({ worker: w, reason: 'bad_quality' }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.error || '抛弃失败', 3000); return; }
  toast(d.message || '已抛弃本包', 1600);
  setTimeout(() => { location.href = '/?tab=repair'; }, 1200);
}

async function submit() {
  const w = who();
  if (!w) { toast('请先回列表页填写标注员名字', 2500); return; }
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
  warn(warns);
  P.seek(0);
  return true;
}

async function boot() {
  W.player = new GTPlayer({
    frameColor,
    centerMode: 'prev',
    onDraw: renderSide,
    onMark: (m) => markVisible(m.roiX, m.roiY),
  });
  W.player.bindTransport();
  $('#bInvisible').onclick = markInvisible;
  $('#bClear').onclick = clearLabel;
  $('#bRangeClear').onclick = () => applyRangeAction('clear');
  $('#bRangeInvis').onclick = () => applyRangeAction('invisible');
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
      case 'KeyX': markInvisible(); return;
      case 'KeyU': clearLabel(); return;
      case 'KeyD': applyRangeAction('clear'); return;
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
