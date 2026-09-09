/* 足球 GT 标注前端。
 *
 * 三个决定标注速度的设计：
 *
 * 1. 视野自动跟着球走。标完一帧跳到下一帧时，视野中心用**最近两个已标点线性外推**
 *    得到，而不是简单沿用上一帧位置。step=3 时快球两帧间能跑 390 px，只沿用上一帧
 *    位置的话球会直接落在视野外。
 * 2. 邻帧预取。找快球靠来回拨帧（球在静态帧上几乎不可见，一动就跳出来），
 *    所以当前帧附近的原图与增强图都提前加载好，拨帧时零延迟。
 * 3. 服务端只发整条 ROI 图，缩放平移全在本地做，平移不产生任何网络往返。
 */
'use strict';

const PACKET_ID = location.pathname.split('/').pop();
const $ = (s) => document.querySelector(s);

const S = {
  meta: null,
  labels: {},          // frame_idx(str) -> record
  frames: [],          // clip 内所有帧号
  targets: [],         // 需要标注的帧号
  fi: null,            // 当前帧
  view: 'raw',         // raw | enh
  ver: '0',            // 帧图 URL 的版本号，见 imgURL
  flicker: false,      // 按住空格时显示前一帧
  viewSize: 560,       // 视野边长（原生像素）。人在 ERP 上高约 420 px，
                       // 用户实测「比人稍大一点的尺度」最容易看清球
  center: { x: 0, y: 0 },  // 视野中心（ROI 坐标）
  occluded: false,
  blurred: false,
  dragging: false,
};

const CACHE = new Map();
const PREFETCH = 8;   // 当前帧前后各预取多少帧

/* 带版本号：帧图打了 24 小时强缓存（拨帧的流畅度全靠它），而包一旦重新生产，
 * packet_id 与帧号都不变、URL 也就完全相同，浏览器会一直拿旧帧。调试期间已经被
 * 这个坑骗过一次——ROI 修好了但屏幕上还是旧画面。版本取自包的 produced_at。 */
function imgURL(view, fi) {
  return `/img/${PACKET_ID}/${view}/${fi}.jpg?v=${S.ver}`;
}

function getImg(view, fi) {
  const k = view + '/' + fi;
  let im = CACHE.get(k);
  if (!im) {
    im = new Image();
    im.src = imgURL(view, fi);
    CACHE.set(k, im);
    if (CACHE.size > 900) { // 一个包最多 300 帧 × 2 视图，留足余量后按插入顺序淘汰
      const first = CACHE.keys().next().value;
      CACHE.delete(first);
    }
  }
  return im;
}

function prefetch() {
  const i = S.frames.indexOf(S.fi);
  if (i < 0) return;
  for (let d = -PREFETCH; d <= PREFETCH; d++) {
    const fi = S.frames[i + d];
    if (fi === undefined) continue;
    getImg('raw', fi);
    getImg('enh', fi);
  }
}

function toast(msg, ms) {
  const t = $('#toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.style.display = 'none'; }, ms || 1400);
}

/* ---------------------------------------------------------------- 几何
 *
 * 上方 overview：整条 ROI 以 1:1 像素比尽量放大展示，用来点选正方形视野框。
 * 下方 zoom：正方形细节窗，边长 = viewSize（ROI 像素），滚轮缩放。
 */

function roiW() { return S.meta.roi[2]; }
function roiH() { return S.meta.roi[3]; }

/** 正方形细节视野在 ROI 中的裁切框 */
function srcRect() {
  const size = Math.max(80, Math.min(S.viewSize, roiW(), roiH()));
  const sx = Math.max(0, Math.min(roiW() - size, S.center.x - size / 2));
  const sy = Math.max(0, Math.min(roiH() - size, S.center.y - size / 2));
  return { sx, sy, size };
}

/** 上方全场：优先宽度撑满且 1:1；给下方至少留 300px */
function overviewLayout() {
  const BOTTOM_MIN = 300;
  const topH = ($('#top') && $('#top').offsetHeight) || 48;
  const maxH = Math.max(80, window.innerHeight - topH - BOTTOM_MIN - 8);
  const maxW = Math.max(200, window.innerWidth);

  // 先按撑满宽度算高度
  const scaleW = maxW / roiW();
  const hIfFullW = roiH() * scaleW;
  let w, h;
  if (hIfFullW <= maxH) {
    w = maxW;
    h = Math.max(1, Math.floor(hIfFullW));
  } else {
    // 高度不够：按最大可用高度缩放（此时宽度可能不满）
    h = Math.floor(maxH);
    w = Math.max(1, Math.floor(h * roiW() / roiH()));
  }
  return { w, h, scale: w / roiW() };
}

/** 下方正方形：占满 stage 内可用的最大正方形（stage 最少 300px 高） */
function zoomSide() {
  const stage = $('#stage');
  return Math.max(280, Math.floor(Math.min(stage.clientWidth - 8, stage.clientHeight - 8)));
}

function canvasToRoi(px, py) {
  const r = srcRect();
  const side = zoomSide();
  const k = r.size / side;
  return { x: r.sx + px * k, y: r.sy + py * k };
}

function overviewToRoi(px, py) {
  const { scale } = overviewLayout();
  return { x: px / scale, y: py / scale };
}

/* ROI 的横向可能跨过全景接缝（100 路视频里 46 路如此），所以 x 一律按全景宽取模。
 * 约定：pano_x = (roi[0] + local_x) % pano_w。 */
function panoW() { return S.meta.pano_size[0]; }
function roiToPano(x, y) {
  return { x: (S.meta.roi[0] + x) % panoW(), y: y + S.meta.roi[1] };
}
function panoToRoi(x, y) {
  return { x: (x - S.meta.roi[0] + panoW()) % panoW(), y: y - S.meta.roi[1] };
}

/* 用最近的已标注点预测本帧球的位置，作为视野中心。
 * 有两个点就线性外推，只有一个点就沿用，都没有就退回 ROI 中心。 */
function predictCenter(fi) {
  // 全程在 ROI 局部坐标里算：ROI 可能跨接缝，用全景 x 算位移时，
  // 球一穿过接缝差值就会变成约 -7680，外推方向完全反掉
  const pts = S.targets
    .filter((t) => S.labels[t] && S.labels[t].visible)
    .map((t) => ({ t, ...panoToRoi(S.labels[t].pano_x, S.labels[t].pano_y) }));
  if (!pts.length) return null;
  const before = pts.filter((p) => p.t < fi).slice(-2);
  if (before.length === 2) {
    const [a, b] = before;
    const span = b.t - a.t;
    const k = span > 0 ? (fi - b.t) / span : 0;
    let dx = (b.x - a.x) * k, dy = (b.y - a.y) * k;
    // 外推封顶：一次误标不该把视野甩到离谱的地方
    const cap = S.viewSize;
    const n = Math.hypot(dx, dy);
    if (n > cap) { dx *= cap / n; dy *= cap / n; }
    return { x: b.x + dx, y: b.y + dy };
  }
  if (before.length === 1) return { x: before[0].x, y: before[0].y };
  const after = pts.find((p) => p.t > fi);
  return after ? { x: after.x, y: after.y } : null;
}

/* ---------------------------------------------------------------- 绘制 */

function drawZoom() {
  const cv = $('#zoom');
  const side = zoomSide();
  if (cv.width !== side || cv.height !== side) {
    cv.width = side;
    cv.height = side;
  }
  cv.style.width = side + 'px';
  cv.style.height = side + 'px';

  const ctx = cv.getContext('2d');
  const r = srcRect();
  const k = side / r.size;

  let fi = S.fi;
  if (S.flicker) {
    const i = S.frames.indexOf(S.fi);
    if (i > 0) fi = S.frames[i - 1];
  }
  const im = getImg(S.view, fi);

  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, side, side);
  ctx.imageSmoothingEnabled = false;
  if (im.complete && im.naturalWidth) {
    ctx.drawImage(im, r.sx, r.sy, r.size, r.size, 0, 0, side, side);
  } else {
    im.onload = () => draw();
    ctx.fillStyle = '#555';
    ctx.fillText('加载中…', 12, 22);
  }

  const idx = S.targets.indexOf(S.fi);
  for (let d = -4; d <= 4; d++) {
    if (d === 0) continue;
    const t = S.targets[idx + d];
    const rec = t !== undefined ? S.labels[t] : null;
    if (!rec || !rec.visible) continue;
    const p = panoToRoi(rec.pano_x, rec.pano_y);
    const a = 0.55 * (1 - Math.abs(d) / 5);
    ctx.strokeStyle = d < 0 ? `rgba(96,165,250,${a})` : `rgba(74,222,128,${a})`;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc((p.x - r.sx) * k, (p.y - r.sy) * k, 7, 0, 6.283);
    ctx.stroke();
  }

  const cur = S.labels[S.fi];
  if (cur && cur.visible) {
    const p = panoToRoi(cur.pano_x, cur.pano_y);
    const cx = (p.x - r.sx) * k, cy = (p.y - r.sy) * k;
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, 16, 0, 6.283); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx - 26, cy); ctx.lineTo(cx - 6, cy);
    ctx.moveTo(cx + 6, cy); ctx.lineTo(cx + 26, cy);
    ctx.moveTo(cx, cy - 26); ctx.lineTo(cx, cy - 6);
    ctx.moveTo(cx, cy + 6); ctx.lineTo(cx, cy + 26);
    ctx.stroke();
  } else if (!cur) {
    const pc = predictCenter(S.fi);
    if (pc) {
      const cx = (pc.x - r.sx) * k, cy = (pc.y - r.sy) * k;
      ctx.strokeStyle = 'rgba(148,163,184,.5)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx - 10, cy); ctx.lineTo(cx + 10, cy);
      ctx.moveTo(cx, cy - 10); ctx.lineTo(cx, cy + 10);
      ctx.stroke();
    }
  }

  if (S.flicker) {
    ctx.fillStyle = 'rgba(251,191,36,.9)';
    ctx.font = 'bold 14px sans-serif';
    ctx.fillText(`前一帧 ${fi}`, 10, side - 12);
  }
}

/* 全场总览：1:1 等比，整条 ROI 完整可见 */
function drawOverview() {
  const cv = $('#overview');
  const { w, h, scale } = overviewLayout();
  if (cv.width !== w || cv.height !== h) {
    cv.width = w;
    cv.height = h;
  }
  cv.style.width = w + 'px';
  cv.style.height = h + 'px';

  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, w, h);
  const im = getImg(S.view, S.fi);
  if (im.complete && im.naturalWidth) {
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(im, 0, 0, w, h);
  } else {
    im.onload = () => draw();
  }

  // 已标轨迹
  ctx.strokeStyle = 'rgba(74,222,128,.9)';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let started = false;
  for (const t of S.targets) {
    const rec = S.labels[t];
    if (!rec || !rec.visible) { started = false; continue; }
    const p = panoToRoi(rec.pano_x, rec.pano_y);
    const x = p.x * scale, y = p.y * scale;
    if (started) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    started = true;
  }
  ctx.stroke();

  // 正方形细节框
  const r = srcRect();
  ctx.strokeStyle = '#fbbf24';
  ctx.lineWidth = 2;
  ctx.strokeRect(r.sx * scale, r.sy * scale, r.size * scale, r.size * scale);
}

function draw() { drawZoom(); drawOverview(); updateBar(); }

function updateBar() {
  const n = Object.keys(S.labels).length;
  const t = S.targets.length;
  const isTarget = S.targets.includes(S.fi);
  $('#frameInfo').textContent = `帧 ${S.fi}` + (isTarget ? ` (第 ${S.targets.indexOf(S.fi) + 1}/${t} 个标注点)` : '（非标注帧）');
  $('#progress').textContent = `已标 ${n}/${t}`;
  $('#viewPill').textContent = S.view === 'raw' ? '原图' : '增强视图';
  $('#viewPill').className = 'pill' + (S.view === 'enh' ? ' on' : '');
  const rec = S.labels[S.fi];
  const flags = [];
  if (S.occluded) flags.push('遮挡');
  if (S.blurred) flags.push('拖影');
  $('#statePill').textContent = rec
    ? (rec.visible ? '本帧已标' + (flags.length ? ' · ' + flags.join('/') : '') : '本帧标为看不见')
    : (flags.length ? '待标 · ' + flags.join('/') : '待标');
}

/* ---------------------------------------------------------------- 导航 */

function goto(fi, recenter) {
  if (fi === undefined || fi === null) return;
  S.fi = fi;
  if (recenter) {
    const rec = S.labels[fi];
    const c = rec && rec.visible ? panoToRoi(rec.pano_x, rec.pano_y) : predictCenter(fi);
    if (c) S.center = c;
  }
  prefetch();
  draw();
}

function stepTarget(dir) {
  const i = S.targets.indexOf(S.fi);
  if (i < 0) {
    // 当前在非标注帧上，跳到最近的标注帧
    const near = S.targets.reduce((a, b) => (Math.abs(b - S.fi) < Math.abs(a - S.fi) ? b : a));
    goto(near, true);
    return;
  }
  goto(S.targets[Math.max(0, Math.min(S.targets.length - 1, i + dir))], true);
}

function stepFrame(dir) {
  const i = S.frames.indexOf(S.fi);
  goto(S.frames[Math.max(0, Math.min(S.frames.length - 1, i + dir))], false);
}

function nextUnlabeled(dir) {
  const i = S.targets.indexOf(S.fi);
  const from = i < 0 ? 0 : i;
  const seq = dir > 0
    ? S.targets.slice(from + 1).concat(S.targets.slice(0, from + 1))
    : S.targets.slice(0, from).reverse().concat(S.targets.slice(from).reverse());
  const t = seq.find((x) => !S.labels[x]);
  if (t === undefined) { toast('全部标完了，可以提交'); return; }
  goto(t, true);
}

/* ---------------------------------------------------------------- 保存 */

async function saveLabel(rec) {
  const r = await fetch(`/api/label/${PACKET_ID}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(rec),
  });
  const d = await r.json();
  if (!r.ok) { toast(d.error || '保存失败'); return false; }
  if (rec.clear) delete S.labels[rec.frame_idx];
  else S.labels[rec.frame_idx] = d.record;
  return true;
}

async function markVisible(roiX, roiY) {
  const p = roiToPano(roiX, roiY);
  const ok = await saveLabel({
    frame_idx: S.fi, visible: true, pano_x: p.x, pano_y: p.y,
    occluded: S.occluded, blurred: S.blurred,
  });
  if (!ok) return;
  if (S.targets.includes(S.fi)) stepTarget(1); else draw();
}

async function markInvisible() {
  if (await saveLabel({ frame_idx: S.fi, visible: false, occluded: S.occluded, blurred: S.blurred })) {
    if (S.targets.includes(S.fi)) stepTarget(1); else draw();
  }
}

async function clearLabel() {
  if (await saveLabel({ frame_idx: S.fi, clear: true })) draw();
}

async function submit() {
  const who = (window.BallGtAoss && BallGtAoss.getWho()) || '';
  if (!who) { toast('请先回首页设置标注员名字', 3000); return; }
  const headers = { 'Content-Type': 'application/json', 'X-Annotator': who };
  let r = await fetch(`/api/build_result/${PACKET_ID}`, {
    method: 'POST', headers, body: JSON.stringify({ annotator: who }),
  });
  let d = await r.json();
  if (!r.ok && d.missing) {
    if (!confirm(`${d.error}。仍要提交吗？`)) return;
    r = await fetch(`/api/build_result/${PACKET_ID}`, {
      method: 'POST', headers, body: JSON.stringify({ annotator: who, force: true }),
    });
    d = await r.json();
  }
  if (!r.ok) { toast(d.error || '组装失败', 3000); return; }
  try {
    toast('正在上传到对象存储…', 5000);
    const key = await BallGtAoss.submitResult(PACKET_ID, who, d.result);
    await fetch(`/api/cleanup/${PACKET_ID}`, {
      method: 'POST', headers, body: JSON.stringify({ key, annotator: who }),
    });
    toast(`已提交（${d.n_labeled} 帧），即将返回`, 2000);
    setTimeout(() => { location.href = '/'; }, 1500);
  } catch (e) {
    toast('上传失败：' + (e.message || e), 4000);
  }
}

/* ---------------------------------------------------------------- 交互 */

function bind() {
  const cv = $('#zoom');

  cv.addEventListener('contextmenu', (e) => e.preventDefault());
  cv.addEventListener('mousedown', (e) => {
    const b = cv.getBoundingClientRect();
    const px = e.clientX - b.left, py = e.clientY - b.top;
    if (e.button === 2) {  // 右键拖动平移
      S.dragging = { px, py, cx: S.center.x, cy: S.center.y };
      return;
    }
    if (e.button !== 0) return;
    const p = canvasToRoi(px, py);
    markVisible(p.x, p.y);
  });
  window.addEventListener('mousemove', (e) => {
    if (!S.dragging) return;
    const b = cv.getBoundingClientRect();
    const r = srcRect();
    const side = zoomSide();
    const k = r.size / side;
    S.center.x = S.dragging.cx - (e.clientX - b.left - S.dragging.px) * k;
    S.center.y = S.dragging.cy - (e.clientY - b.top - S.dragging.py) * k;
    draw();
  });
  window.addEventListener('mouseup', () => { S.dragging = false; });

  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    const f = e.deltaY > 0 ? 1.18 : 1 / 1.18;
    S.viewSize = Math.max(160, Math.min(roiW(), roiH(), Math.round(S.viewSize * f)));
    draw();
  }, { passive: false });

  $('#overview').addEventListener('mousedown', (e) => {
    const b = e.currentTarget.getBoundingClientRect();
    const p = overviewToRoi(e.clientX - b.left, e.clientY - b.top);
    S.center.x = p.x;
    S.center.y = p.y;
    draw();
  });

  window.addEventListener('keydown', (e) => {
    if (e.repeat && e.code !== 'Space') return;
    switch (e.code) {
      case 'Space': e.preventDefault(); if (!S.flicker) { S.flicker = true; drawZoom(); } return;
      case 'KeyW': S.view = S.view === 'raw' ? 'enh' : 'raw'; draw(); return;
      case 'KeyA': case 'ArrowLeft': stepTarget(-1); return;
      case 'KeyD': case 'ArrowRight': stepTarget(1); return;
      case 'Comma': stepFrame(-1); return;
      case 'Period': stepFrame(1); return;
      case 'KeyX': markInvisible(); return;
      case 'KeyU': clearLabel(); return;
      case 'KeyJ': nextUnlabeled(1); return;
      case 'KeyK': nextUnlabeled(-1); return;
      case 'KeyO': S.occluded = !S.occluded; toast('遮挡标记：' + (S.occluded ? '开' : '关')); updateBar(); return;
      case 'KeyB': S.blurred = !S.blurred; toast('拖影标记：' + (S.blurred ? '开' : '关')); updateBar(); return;
      default: return;
    }
  });
  window.addEventListener('keyup', (e) => {
    if (e.code === 'Space') { S.flicker = false; drawZoom(); }
  });
  window.addEventListener('resize', draw);

  $('#nextUnlabeled').onclick = () => nextUnlabeled(1);
  $('#prevUnlabeled').onclick = () => nextUnlabeled(-1);
  $('#submit').onclick = submit;
}

/* ---------------------------------------------------------------- 启动 */

(async function main() {
  const r = await fetch(`/api/meta/${PACKET_ID}`);
  const d = await r.json();
  if (!r.ok) { document.body.innerHTML = `<p style="padding:24px">${d.error || '加载失败'}</p>`; return; }
  S.meta = d;
  S.ver = encodeURIComponent(d.produced_at || d.tag || '0');
  S.frames = d.frames;
  S.targets = d.targets.filter((t) => d.frames.includes(t));
  S.labels = d.labels || {};
  $('#pid').textContent = `${d.group} / ${d.stem} / ${d.tag}  (${d.sampling === 'active' ? '活跃段' : '随机段'})`;
  S.center = { x: roiW() / 2, y: roiH() / 2 };
  bind();
  // 从第一个未标注的点开始，接着上次的进度
  const first = S.targets.find((t) => !S.labels[t]);
  goto(first !== undefined ? first : S.targets[0], true);
})();
