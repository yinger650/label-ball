/* 足球 GT 共用播放器。
 *
 * 线上审核页与本地工作台共用这一套：时间轴色条、播放/摆动、跟随裁切、
 * 曲线图、主画面轨迹。source：thumb=缩略视频，local=全分辨率 JPEG。
 */
'use strict';

const GT_LOW_CONF = 0.4;
const GT_ZOOM_SIDE = 256;
const GT_FPS_STEPS = [2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 30];

function gt$(s, root) {
  return (root || document).querySelector(s);
}

function gtFmt(v, n) {
  return (v === null || v === undefined) ? '—' : (typeof v === 'number' ? v.toFixed(n === undefined ? 1 : n) : String(v));
}

function gtUnwrapSeam(vals, panoW) {
  const out = vals.slice();
  if (!panoW) return out;
  let prev = null;
  for (let i = 0; i < out.length; i++) {
    if (out[i] === null) continue;
    let v = out[i];
    if (prev !== null) {
      while (v - prev > panoW / 2) v -= panoW;
      while (prev - v > panoW / 2) v += panoW;
    }
    out[i] = v;
    prev = v;
  }
  return out;
}

function gtOutlierFlags(xs, ys) {
  const n = xs.length;
  const res = new Array(n).fill(0);
  for (let i = 1; i < n - 1; i++) {
    if (xs[i] === null || xs[i - 1] === null || xs[i + 1] === null) continue;
    if (ys[i] === null || ys[i - 1] === null || ys[i + 1] === null) continue;
    res[i] = Math.hypot(xs[i] - (xs[i - 1] + xs[i + 1]) / 2, ys[i] - (ys[i - 1] + ys[i + 1]) / 2);
  }
  const nz = res.filter((v) => v > 0).sort((a, b) => a - b);
  const med = nz.length ? nz[nz.length >> 1] : 0;
  const thr = Math.max(10, med * 4);
  return res.map((v) => v > thr);
}

function gtToast(msg, ms, action) {
  const t = gt$('#toast');
  if (!t) return null;
  t.innerHTML = '';
  const span = document.createElement('span');
  span.textContent = msg;
  t.appendChild(span);
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.onclick = action.fn;
    t.appendChild(b);
  }
  t.style.display = 'flex';
  clearTimeout(gtToast._t);
  if (ms !== 0) gtToast._t = setTimeout(gtHideToast, ms || 1600);
  return t;
}

function gtHideToast() {
  const t = gt$('#toast');
  if (t) t.style.display = 'none';
  clearTimeout(gtToast._t);
}

/* ---------------------------------------------------------------- 帧序列 */

function gtBuildFrames(data, man, warns) {
  const labels = (data.submission && data.submission.labels) || data.labels || [];
  const labelList = Array.isArray(labels) ? labels : Object.values(labels);
  const byIdx = new Map(labelList.map((r) => [Number(r.frame_idx), r]));
  const mf = (man && Array.isArray(man.frames)) ? man.frames : null;
  let hasCells = false;
  let out;

  if (mf && mf.length) {
    hasCells = true;
    let miss = 0;
    out = mf.map((f, k) => {
      const lb = byIdx.get(Number(f.frame_idx));
      if (!lb) miss++;
      return {
        cell: (f.i === undefined || f.i === null) ? k : f.i,
        frame_idx: Number(f.frame_idx),
        pano_x: f.pano_x !== undefined ? f.pano_x : (lb ? lb.pano_x : null),
        pano_y: f.pano_y !== undefined ? f.pano_y : (lb ? lb.pano_y : null),
        dx: f.dx === undefined ? null : f.dx,
        dy: f.dy === undefined ? null : f.dy,
        mx: f.mx === undefined ? null : f.mx,
        my: f.my === undefined ? null : f.my,
        rx: f.rx === undefined ? null : f.rx,
        ry: f.ry === undefined ? null : f.ry,
        ox: f.ox === undefined ? null : f.ox,
        oy: f.oy === undefined ? null : f.oy,
        conf: f.conf === undefined ? (lb ? lb.conf : null) : f.conf,
        interp: !!(f.interp === undefined ? (lb && lb.interp) : f.interp),
        visible: (f.visible === undefined ? (lb ? lb.visible : true) : f.visible) !== false,
        occluded: !!(f.occluded || (lb && lb.occluded)),
        blurred: !!(f.blurred || (lb && lb.blurred)),
        motion_px: f.motion_px === undefined ? null : f.motion_px,
        stages: f.stages || (lb ? lb.stages : null) || [],
        noLabel: !lb,
        from_ai: !!(lb && lb.from_ai),
      };
    });
    if (miss) warns.push(`manifest 有 ${miss} 帧在 labels 里找不到对应帧号，已以 manifest 为准`);
    const extra = labelList.length - (mf.length - miss);
    if (extra > 0) warns.push(`labels 另有 ${extra} 帧不在审核包里，无法播放`);
  } else {
    const order = data.frames && data.frames.length
      ? data.frames.map(Number)
      : labelList.map((r) => Number(r.frame_idx)).sort((a, b) => a - b);
    out = order.map((fi, k) => {
      const lb = byIdx.get(Number(fi)) || {};
      return {
        cell: k, frame_idx: Number(fi),
        pano_x: lb.pano_x !== undefined ? lb.pano_x : null,
        pano_y: lb.pano_y !== undefined ? lb.pano_y : null,
        dx: null, dy: null, mx: null, my: null, rx: null, ry: null, ox: null, oy: null,
        conf: lb.conf !== undefined ? lb.conf : null,
        interp: !!lb.interp,
        visible: lb.visible !== false && lb.pano_x != null,
        occluded: !!lb.occluded, blurred: !!lb.blurred, motion_px: null,
        stages: lb.stages || [], noLabel: !byIdx.has(Number(fi)),
        from_ai: !!lb.from_ai,
      };
    });
  }
  return { frames: out, hasCells };
}

function gtBuildTracks(man, warns, hasCells) {
  const tracks = { follow: {}, region: {}, scene: {} };
  const tr = (man && man.tracks) || {};
  for (const [name, t] of Object.entries(tr)) {
    if (!t || !t.file) continue;
    const mode = t.mode || (name.indexOf('scene') === 0 ? 'scene'
                   : name.indexOf('region') === 0 ? 'region' : 'follow');
    const pane = t.pane || (name.indexOf('_enh') > 0 ? 'enh' : 'raw');
    if (!tracks[mode]) tracks[mode] = {};
    tracks[mode][pane] = t;
  }
  const any = Object.keys(tracks.follow).length + Object.keys(tracks.region).length;
  let cells = hasCells;
  let mode = 'follow';
  if (hasCells && !any) {
    cells = false;
    warns.push('审核包里没有任何 sprite 轨道，已退回数值表形态');
  } else if (hasCells && !tracks.follow.raw && !tracks.follow.enh) {
    warns.push('审核包缺 follow 轨道，跟随模式不可用');
    mode = 'region';
  }
  return { tracks, hasCells: cells, mode };
}

/* 时间轴色条，三个页面的图例都按这四色写 */
const GT_FRAME_COLORS = {
  unlabeled: '#4b5563',
  ball: '#4ade80',
  interp: '#fbbf24',
  invisible: '#f97316',
};

function gtDefaultFrameColor(f) {
  if (f.noLabel) return GT_FRAME_COLORS.unlabeled;
  if (!f.visible) return GT_FRAME_COLORS.invisible;
  if (f.interp) return GT_FRAME_COLORS.interp;
  return GT_FRAME_COLORS.ball;
}

/* ---------------------------------------------------------------- 播放器 */

class GTPlayer {
  constructor(opts) {
    this.opts = opts || {};
    this.frames = [];
    this.i = 0;
    this.tracks = { follow: {}, region: {}, scene: {} };
    this.mode = 'follow';
    this.pane = 'raw';
    this.playing = false;
    this.fps = 12;
    this.dir = 1;
    this.loop = true;
    this.ping = false;
    this.inPt = 0;
    this.outPt = 0;
    this.rangeSet = false;  // 用户是否手动选了段；false 时 [inPt,outPt] 只是默认的全段
    this.rejected = new Set();
    this.decision = null;
    this.series = null;
    this.imgs = new Map();
    this.hasCells = false;
    this.ovEnh = null;
    this.mainSeek = null;
    this.man = null;
    this.base = '';
    this.source = null;   // {kind:'thumb'|'local'|'bundle', ...}
    this._vids = null;
    this.scanUrl = '';
    this.mainView = 'video';  // 'video' | 'scan'
    this.viewSize = 560;
    this.center = { x: 0, y: 0 };
    this.crop = null;
    this.editable = false;
    this.frameColorFn = this.opts.frameColor || gtDefaultFrameColor;
    this.onSeek = this.opts.onSeek || null;
    this.onMark = this.opts.onMark || null;
    this._tickLast = 0;
  }

  getImg(url) {
    let im = this.imgs.get(url);
    if (!im) {
      im = new Image();
      im.onload = () => { im._failed = false; this.draw(); };
      im.onerror = () => { im._failed = true; this.draw(); };
      im.src = url;
      this.imgs.set(url, im);
    }
    return im;
  }

  retryFailedEnh() {
    for (const [url, im] of this.imgs) {
      if (!im._failed || url.indexOf('/enh/') < 0) continue;
      im._failed = false;
      im.src = url.replace(/&r=\d+/, '') + '&r=' + Date.now();
    }
  }

  localURL(view, fi) {
    const s = this.source || {};
    return `/img/${encodeURIComponent(s.packetId)}/${view}/${fi}.jpg?v=${s.ver || '0'}`;
  }

  isRejected(f) {
    return !!(f && this.rejected.has(f.frame_idx));
  }

  rangeBounds() {
    const lo = Math.min(this.inPt, this.outPt);
    const hi = Math.max(this.inPt, this.outPt);
    return [lo, hi];
  }

  curTrack() {
    const m = this.tracks[this.mode] || {};
    return m[this.pane] || m.raw || null;
  }

  cur() {
    return this.frames[this.i] || null;
  }

  panoW() {
    const meta = (this.source && this.source.meta) || {};
    return (this.man && this.man.pano_size && this.man.pano_size[0])
      || (meta.pano_size && meta.pano_size[0]) || 0;
  }

  roi() {
    const meta = (this.source && this.source.meta) || {};
    return meta.roi || [0, 0, 1, 1];
  }

  panoToRoi(x, y) {
    const r = this.roi();
    const w = this.panoW() || 1;
    return { x: (x - r[0] + w) % w, y: y - r[1] };
  }

  roiToPano(x, y) {
    const r = this.roi();
    const w = this.panoW() || 1;
    return { x: (r[0] + x) % w, y: y + r[1] };
  }

  rebuildSeries() {
    const panoW = this.panoW();
    const xs = gtUnwrapSeam(this.frames.map((f) => (
      f.visible && f.pano_x !== null && f.pano_x !== undefined
    ) ? f.pano_x : null), panoW);
    const ys = this.frames.map((f) => (
      f.visible && f.pano_y !== null && f.pano_y !== undefined
    ) ? f.pano_y : null);
    this.series = { xs, ys, out: gtOutlierFlags(xs, ys) };
  }

  applyFrames(built, man, tracksInfo) {
    this.man = man;
    this.frames = built.frames;
    this.hasCells = tracksInfo.hasCells;
    this.tracks = tracksInfo.tracks;
    this.mode = tracksInfo.mode;
    this.inPt = 0;
    this.outPt = Math.max(0, this.frames.length - 1);
    this.rangeSet = false;
    this.i = 0;
    this.rebuildSeries();
    const r = this.roi();
    this.center = { x: r[2] / 2, y: r[3] / 2 };
  }

  loadBundle(data) {
    const thumb = data.thumb || {};
    const tfiles = thumb.files || [];
    if (thumb.ready && tfiles.indexOf('clip.mp4') >= 0) {
      return this.loadThumb(data);
    }
    const warns = [];
    this.source = { kind: 'bundle' };
    this.man = (data.bundle && data.bundle.manifest) || null;
    this.base = (data.bundle && data.bundle.base) || '';
    this.imgs.clear();
    this.pane = 'raw';
    const built = gtBuildFrames(data, this.man, warns);
    const tr = gtBuildTracks(this.man, warns, built.hasCells);
    this.applyFrames(built, this.man, tr);
    const files = (data.bundle || {}).files || [];
    const ov = (this.man && this.man.overview) || {};
    this.ovEnh = ov.file || (files.indexOf('overview.webp') >= 0 ? 'overview.webp' : null);
    this.applyDegrade();
    return warns;
  }

  ensureVideos() {
    if (this._vids) return this._vids;
    const mk = (id) => {
      let v = document.getElementById(id);
      if (!v) {
        v = document.createElement('video');
        v.id = id;
        v.muted = true;
        v.playsInline = true;
        v.preload = 'auto';
        v.setAttribute('playsinline', '');
        v.setAttribute('muted', '');
        v.style.cssText = 'position:fixed;left:-9999px;top:0;width:4px;height:4px;opacity:0;pointer-events:none';
        document.body.appendChild(v);
      }
      v.addEventListener('seeked', () => this.draw());
      v.addEventListener('loadeddata', () => this.draw());
      return v;
    };
    this._vids = { clip: mk('gtVidClip'), dog: mk('gtVidDog') };
    return this._vids;
  }

  loadThumb(data) {
    const warns = [];
    const meta = (data.packet && data.packet.meta) || data.meta || {};
    const thumb = data.thumb || {};
    const pid = (data.packet && data.packet.packet_id) || data.packet_id || meta.packet_id;
    this.source = {
      kind: 'thumb',
      packetId: pid,
      meta,
      base: thumb.base || (pid ? `/thumb/${pid}` : ''),
      clipFps: Number(meta.clip_fps || 30),
      decim: Math.max(1, Number(meta.decim || 1)),
    };
    this.base = this.source.base;
    this.man = { pano_size: meta.pano_size, fps: meta.fps, step: meta.step, cell: 192 };
    this.imgs.clear();
    this.pane = 'raw';
    const payload = { labels: (data.submission && data.submission.labels) || data.labels, frames: data.frames };
    const built = gtBuildFrames(payload, { frames: data.frames }, warns);
    built.hasCells = true;
    this.applyFrames(built, this.man, { tracks: { follow: {}, region: {}, scene: {} }, hasCells: true, mode: 'follow' });
    this.hasCells = true;
    this.ovEnh = null;
    this.scanUrl = this.source.base + '/scan_time.jpg';
    const vids = this.ensureVideos();
    if (this.source.base) {
      vids.clip.src = this.source.base + '/clip.mp4';
      vids.dog.src = this.source.base + '/dog.mp4';
    }
    this.applyDegrade();
    this.syncVideo();
    const first = this.frames.find((f) => f.visible && f.pano_x != null);
    if (first) this.center = this.panoToRoi(first.pano_x, first.pano_y);
    return warns;
  }

  clipIndexOf(f) {
    if (!f) return 0;
    const meta = (this.source && this.source.meta) || {};
    const decim = (this.source && this.source.decim) || 1;
    const fs = Number(meta.frame_start || 0);
    return Math.max(0, Math.round((Number(f.frame_idx) - fs) / decim));
  }

  activeVideo() {
    if (!this._vids) return null;
    return this.pane === 'enh' ? this._vids.dog : this._vids.clip;
  }

  clipTimeOf(f) {
    const fps = (this.source && this.source.clipFps) || 30;
    return this.clipIndexOf(f) / fps + 0.45 / fps;
  }

  nearestIndexFromMedia(t) {
    const fps = (this.source && this.source.clipFps) || 30;
    const ci = Number(t) * fps;
    let best = this.i, bestD = 1e18;
    this.frames.forEach((f, i) => {
      const d = Math.abs(this.clipIndexOf(f) - ci);
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  }

  thumbPlayRate() {
    const clipFps = (this.source && this.source.clipFps) || 30;
    const decim = (this.source && this.source.decim) || 1;
    const step = Number((this.source && this.source.meta && this.source.meta.step)
      || (this.man && this.man.step) || 3);
    const clipStep = Math.max(1, step / decim);
    return Math.max(0.1, Math.min(4, (this.fps * clipStep) / clipFps));
  }

  playVideos() {
    const vids = this.ensureVideos();
    const rate = this.thumbPlayRate();
    ['clip', 'dog'].forEach((k) => {
      const v = vids[k];
      if (!v || !v.src) return;
      v.muted = true;
      v.playbackRate = rate;
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    });
  }

  pauseVideos() {
    if (!this._vids) return;
    ['clip', 'dog'].forEach((k) => {
      const v = this._vids[k];
      if (v && !v.paused) v.pause();
    });
  }

  syncVideo() {
    if (!(this.source && this.source.kind === 'thumb') || !this._vids) return;
    const f = this.cur();
    if (!f) return;
    const t = this.clipTimeOf(f);
    const fps = (this.source && this.source.clipFps) || 30;
    const apply = (v) => {
      if (!v || !v.src) return;
      if (Math.abs((v.currentTime || 0) - t) > 0.4 / fps) v.currentTime = t;
    };
    apply(this._vids.clip);
    apply(this._vids.dog);
  }

  _bindThumbRvfc() {
    const v = this.activeVideo();
    if (!v) return;
    const loop = (_now, meta) => {
      if (!this.playing || !(this.source && this.source.kind === 'thumb')) return;
      const mediaT = (meta && meta.mediaTime != null) ? meta.mediaTime : (v.currentTime || 0);
      const i = this.nearestIndexFromMedia(mediaT);
      if (i !== this.i) {
        this.i = i;
        const f = this.cur();
        if (f) this.recenterOn(f.frame_idx);
        if (this.onSeek) this.onSeek(this);
      }
      this.draw();
      const hi = this.frames[this.outPt];
      const hiT = hi ? this.clipTimeOf(hi) : 1e9;
      if (mediaT >= hiT - 1 / 30) {
        if (this.loop) {
          this.seek(this.inPt, true);
          this.playVideos();
        } else {
          this.stop();
          return;
        }
      }
      if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(loop);
      else requestAnimationFrame(() => loop(0, { mediaTime: v.currentTime || 0 }));
    };
    if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(loop);
    else requestAnimationFrame(() => loop(0, { mediaTime: v.currentTime || 0 }));
  }

  switchToLocal(meta) {
    if (!meta) return;
    this.loadLocal(meta, this.frames.reduce((acc, f) => {
      acc[f.frame_idx] = f;
      return acc;
    }, {}), this.frames.map((f) => f.frame_idx));
  }

  loadLocal(meta, labels, frames) {
    const warns = [];
    this.source = {
      kind: 'local',
      packetId: meta.packet_id,
      ver: encodeURIComponent(meta.produced_at || meta.tag || '0'),
      meta,
    };
    this.base = '';
    this.man = {
      pano_size: meta.pano_size,
      fps: meta.fps,
      step: meta.step,
      cell: 192,
    };
    this.imgs.clear();
    this.pane = 'raw';
    const data = { labels, frames: frames || meta.frames || meta.targets };
    const built = gtBuildFrames(data, null, warns);
    built.hasCells = true;
    this.applyFrames(built, this.man, { tracks: { follow: {}, region: {}, scene: {} }, hasCells: true, mode: 'follow' });
    this.hasCells = true;
    this.ovEnh = null;
    this.applyDegrade();
    const first = this.frames.find((f) => f.visible && f.pano_x != null);
    if (first) this.center = this.panoToRoi(first.pano_x, first.pano_y);
    return warns;
  }

  patchLabel(fi, rec) {
    const f = this.frames.find((x) => x.frame_idx === Number(fi));
    if (!f) return;
    if (!rec) {
      f.pano_x = null; f.pano_y = null; f.visible = false;
      f.interp = false; f.noLabel = true; f.from_ai = false;
    } else {
      f.pano_x = rec.pano_x; f.pano_y = rec.pano_y;
      f.visible = rec.visible !== false && rec.pano_x != null;
      f.interp = !!rec.interp;
      f.occluded = !!rec.occluded;
      f.blurred = !!rec.blurred;
      f.noLabel = false;
      f.from_ai = false;
    }
    this.rebuildSeries();
  }

  applyDegrade() {
    const hasRegion = !!(this.tracks.region && (this.tracks.region.raw || this.tracks.region.enh));
    const bMode = gt$('#bMode');
    if (bMode) {
      bMode.disabled = !hasRegion;
      bMode.title = hasRegion ? '' : '本包没有 region 轨道，整体路径请看主画面总览图';
    }
    const hasScene = !!(this.tracks.scene && (this.tracks.scene.raw || this.tracks.scene.enh));
    const local = this.source && (this.source.kind === 'local' || this.source.kind === 'thumb');
    const hasMain = !!(this.hasCells || this.ovEnh || hasScene || local);
    const ov = gt$('#overview');
    const stage = gt$('#stage');
    const tw = gt$('#tablewrap');
    const zoom = gt$('#zoom');
    const bPane = gt$('#bPane');
    if (ov) ov.style.display = hasMain ? 'block' : 'none';
    if (stage) stage.style.display = hasMain ? 'flex' : 'none';
    if (tw) tw.style.display = (this.hasCells || local) ? 'none' : 'block';
    if (zoom) zoom.style.display = (this.hasCells || local) ? 'block' : 'none';
    if (bPane) bPane.disabled = !this.hasCells && !hasScene && !this.ovEnh && !local;
  }

  markerInCell(f, cell) {
    if (this.mode === 'region') {
      return (f.rx === null || f.ry === null) ? null : { x: f.rx, y: f.ry };
    }
    if (f.mx !== null && f.mx !== undefined && f.my !== null && f.my !== undefined) {
      return { x: f.mx, y: f.my };
    }
    if (f.dx !== null && f.dx !== undefined && f.dy !== null && f.dy !== undefined) {
      return { x: cell / 2 + f.dx, y: cell / 2 + f.dy };
    }
    return null;
  }

  /* ---- 本地裁切 ---- */

  srcRect() {
    const r = this.roi();
    const rw = r[2], rh = r[3];
    const size = Math.max(80, Math.min(this.viewSize, rw, rh));
    const sx = Math.max(0, Math.min(rw - size, this.center.x - size / 2));
    const sy = Math.max(0, Math.min(rh - size, this.center.y - size / 2));
    this.crop = { sx, sy, size };
    return this.crop;
  }

  predictCenter(fi) {
    const pts = this.frames
      .filter((f) => f.visible && f.pano_x != null)
      .map((f) => ({ t: f.frame_idx, ...this.panoToRoi(f.pano_x, f.pano_y) }));
    if (!pts.length) return null;
    const before = pts.filter((p) => p.t < fi).slice(-2);
    if (before.length === 2) {
      const [a, b] = before;
      const span = b.t - a.t;
      const k = span > 0 ? (fi - b.t) / span : 0;
      let dx = (b.x - a.x) * k, dy = (b.y - a.y) * k;
      const cap = this.viewSize;
      const n = Math.hypot(dx, dy);
      if (n > cap) { dx *= cap / n; dy *= cap / n; }
      return { x: b.x + dx, y: b.y + dy };
    }
    if (before.length === 1) return { x: before[0].x, y: before[0].y };
    const after = pts.find((p) => p.t > fi);
    return after ? { x: after.x, y: after.y } : null;
  }

  recenterOn(fi) {
    const f = this.frames.find((x) => x.frame_idx === fi) || this.cur();
    if (f && f.visible && f.pano_x != null) {
      this.center = this.panoToRoi(f.pano_x, f.pano_y);
      return;
    }
    // 工作台：预选框默认跟上帧位置，不外推
    if (this.opts.centerMode === 'prev') {
      const idx = this.frames.findIndex((x) => x.frame_idx === fi);
      const from = idx >= 0 ? idx : this.i;
      for (let i = from - 1; i >= 0; i--) {
        const g = this.frames[i];
        if (g && g.visible && g.pano_x != null) {
          this.center = this.panoToRoi(g.pano_x, g.pano_y);
          return;
        }
      }
      return;
    }
    const p = this.predictCenter(fi);
    if (p) this.center = p;
  }

  /* ---- 绘制：局部 ---- */

  drawPlayer() {
    const kind = this.source && this.source.kind;
    if (kind === 'local') { this.drawPlayerLocal(); return; }
    if (kind === 'thumb') { this.drawPlayerThumb(); return; }
    if (!this.hasCells) return;
    const cv = gt$('#player');
    if (!cv) return;
    const side = GT_ZOOM_SIDE;
    if (cv.width !== side) { cv.width = side; cv.height = side; }
    cv.style.width = side + 'px';
    cv.style.height = side + 'px';
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, side, side);

    const f = this.cur();
    const t = this.curTrack();
    if (!f || !t) {
      ctx.fillStyle = '#8b93a1';
      ctx.font = '14px sans-serif';
      ctx.fillText(t ? '没有帧' : '该模式没有对应轨道', 14, 26);
      return;
    }
    const cell = t.cell || (this.man && this.man.cell) || 192;
    const cols = t.cols || 1;
    const k = side / cell;
    const im = this.getImg(this.base + '/' + t.file);

    if (im.complete && im.naturalWidth) {
      const col = f.cell % cols;
      const row = Math.floor(f.cell / cols);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(im, col * cell, row * cell, cell, cell, 0, 0, side, side);
    } else {
      ctx.fillStyle = '#555';
      ctx.font = '14px sans-serif';
      ctx.fillText('sprite 加载中…', 14, 26);
    }

    for (let d = -4; d <= 4; d++) {
      if (d === 0) continue;
      const g = this.frames[this.i + d];
      if (!g || !g.visible) continue;
      const m = this.markerInCell(g, cell);
      if (!m) continue;
      const a = 0.5 * (1 - Math.abs(d) / 5);
      ctx.strokeStyle = d < 0 ? `rgba(96,165,250,${a})` : `rgba(74,222,128,${a})`;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(m.x * k, m.y * k, 6, 0, 6.283);
      ctx.stroke();
    }

    const m = f.visible ? this.markerInCell(f, cell) : null;
    this._strokeCross(ctx, m, k, f, side);
  }

  drawPlayerLocal() {
    const cv = gt$('#player');
    if (!cv) return;
    const side = GT_ZOOM_SIDE;
    if (cv.width !== side) { cv.width = side; cv.height = side; }
    cv.style.width = side + 'px';
    cv.style.height = side + 'px';
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, side, side);

    const f = this.cur();
    if (!f) return;
    const rec = this.srcRect();
    const k = side / rec.size;
    const im = this.getImg(this.localURL(this.pane === 'enh' ? 'enh' : 'raw', f.frame_idx));
    ctx.imageSmoothingEnabled = false;
    if (im.complete && im.naturalWidth) {
      ctx.drawImage(im, rec.sx, rec.sy, rec.size, rec.size, 0, 0, side, side);
    } else {
      ctx.fillStyle = '#555';
      ctx.font = '14px sans-serif';
      ctx.fillText(im._failed && this.pane === 'enh' ? '增强图生成中…' : '加载中…', 14, 26);
    }

    for (let d = -4; d <= 4; d++) {
      if (d === 0) continue;
      const g = this.frames[this.i + d];
      if (!g || !g.visible || g.pano_x == null) continue;
      const p = this.panoToRoi(g.pano_x, g.pano_y);
      const a = 0.5 * (1 - Math.abs(d) / 5);
      ctx.strokeStyle = d < 0 ? `rgba(96,165,250,${a})` : `rgba(74,222,128,${a})`;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc((p.x - rec.sx) * k, (p.y - rec.sy) * k, 6, 0, 6.283);
      ctx.stroke();
    }

    let m = null;
    if (f.visible && f.pano_x != null) {
      const p = this.panoToRoi(f.pano_x, f.pano_y);
      m = { x: p.x - rec.sx, y: p.y - rec.sy };
    }
    this._strokeCross(ctx, m, k, f, side);
  }

  _strokeCross(ctx, m, k, f, side) {
    if (m) {
      const cx = m.x * k, cy = m.y * k;
      ctx.strokeStyle = (f.conf !== null && f.conf < GT_LOW_CONF) ? '#f87171'
                      : (f.interp ? '#fbbf24' : '#4ade80');
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(cx, cy, 15, 0, 6.283); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(cx - 26, cy); ctx.lineTo(cx - 7, cy);
      ctx.moveTo(cx + 7, cy); ctx.lineTo(cx + 26, cy);
      ctx.moveTo(cx, cy - 26); ctx.lineTo(cx, cy - 7);
      ctx.moveTo(cx, cy + 7); ctx.lineTo(cx, cy + 26);
      ctx.stroke();
    }
    ctx.font = 'bold 13px sans-serif';
    const badge = [];
    if (!f.visible) badge.push(['不可见', '#8b93a1']);
    else if (!m) badge.push(['本轨道无坐标', '#fbbf24']);
    if (f.interp) badge.push(['插值', '#fbbf24']);
    if (f.conf !== null && f.conf < GT_LOW_CONF) badge.push(['低置信 ' + gtFmt(f.conf, 2), '#f87171']);
    if (f.occluded) badge.push(['遮挡', '#8b93a1']);
    if (f.blurred) badge.push(['拖影', '#8b93a1']);
    if (f.noLabel) badge.push(['未标', '#8b93a1']);
    badge.forEach((b, n) => {
      ctx.fillStyle = 'rgba(16,18,22,.8)';
      ctx.fillRect(8, 8 + n * 22, ctx.measureText(b[0]).width + 14, 19);
      ctx.fillStyle = b[1];
      ctx.fillText(b[0], 15, 22 + n * 22);
    });
    if (this.isRejected(f)) {
      ctx.strokeStyle = '#f87171';
      ctx.lineWidth = 4;
      ctx.strokeRect(2, 2, side - 4, side - 4);
      ctx.fillStyle = '#f87171';
      ctx.fillText('错误帧', side - 58, side - 12);
    }
    ctx.fillStyle = 'rgba(230,232,236,.75)';
    ctx.font = '12px ui-monospace,Menlo,monospace';
    const src = this.source && this.source.kind === 'local' ? '本地原图'
              : this.source && this.source.kind === 'thumb' ? '缩略视频'
              : (this.mode === 'follow' ? '跟随' : '固定区域');
    ctx.fillText(`${src} · ${this.pane === 'raw' ? '原图' : '滤波图'} · 帧 ${f.frame_idx}`, 8, side - 12);
  }

  /* ---- 色条 ---- */

  drawStrip() {
    const cv = gt$('#strip');
    if (!cv) return;
    const w = Math.max(200, Math.floor(cv.clientWidth));
    const h = 52;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const ctx = cv.getContext('2d');
    ctx.clearRect(0, 0, w, h);
    const n = this.frames.length;
    if (!n) return;
    const bh = 26;
    const sw = w / n;
    const [lo, hi] = this.rangeBounds();
    const single = !!this.opts.singleDefault && !this.rangeSet;
    const allRejected = this.frames.every((f) => this.rejected.has(f.frame_idx));

    for (let i = 0; i < n; i++) {
      const f = this.frames[i];
      const x = i * sw;
      ctx.fillStyle = this.frameColorFn(f, this);
      ctx.fillRect(x, 0, Math.max(1, sw - (sw > 4 ? 1 : 0)), bh);
      if (!single && (i < lo || i > hi)) {
        ctx.fillStyle = 'rgba(20,22,26,.68)';
        ctx.fillRect(x, 0, Math.max(1, sw), bh);
      }
    }
    if (allRejected && n) {
      ctx.fillStyle = 'rgba(248,113,113,.45)';
      ctx.fillRect(0, bh, w, 5);
    }
    ctx.strokeStyle = '#60a5fa';
    ctx.lineWidth = 2;
    if (single) {
      ctx.fillStyle = '#60a5fa';
      ctx.fillRect(this.i * sw, bh + 1, Math.max(3, sw), 4);
    } else if (this.opts.singleDefault) {
      ctx.lineWidth = 3;
      ctx.strokeRect(lo * sw + 1.5, 1.5, (hi - lo + 1) * sw - 3, bh - 3);
      ctx.fillStyle = 'rgba(96,165,250,.55)';
      ctx.fillRect(lo * sw, bh + 1, (hi - lo + 1) * sw, 4);
      ctx.lineWidth = 2;
    }
    if (!single) {
      ctx.beginPath(); ctx.moveTo(lo * sw + 1, 0); ctx.lineTo(lo * sw + 1, bh + 8); ctx.stroke();
      ctx.beginPath(); ctx.moveTo((hi + 1) * sw - 1, 0); ctx.lineTo((hi + 1) * sw - 1, bh + 8); ctx.stroke();
    }
    // 错误帧最后画，不被选段框盖住；格子太窄画不下叉就整格涂红
    for (let i = 0; i < n; i++) {
      const f = this.frames[i];
      if (!this.isRejected(f)) continue;
      const x = i * sw;
      if (sw <= 6) {
        ctx.fillStyle = '#ef4444';
        ctx.fillRect(x, 0, Math.max(2, sw), bh);
        continue;
      }
      const onRed = this.frameColorFn(f, this) === '#f87171';
      ctx.strokeStyle = '#ef4444';
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 1, 1, sw - 2, bh - 2);
      ctx.strokeStyle = onRed ? '#450a0a' : '#ef4444';
      ctx.lineWidth = sw > 10 ? 2.5 : 2;
      ctx.beginPath();
      ctx.moveTo(x + 2, 3); ctx.lineTo(x + sw - 2, bh - 3);
      ctx.moveTo(x + sw - 2, 3); ctx.lineTo(x + 2, bh - 3);
      ctx.stroke();
    }
    ctx.fillStyle = '#60a5fa';
    ctx.font = '10px sans-serif';
    if (!single && (sw > 8 || lo !== hi)) {
      ctx.fillText('头', lo * sw + 3, bh + 8);
      const tw = ctx.measureText('尾').width;
      ctx.fillText('尾', Math.max(lo * sw + 16, (hi + 1) * sw - tw - 3), bh + 8);
    }
    ctx.fillStyle = '#8b93a1';
    ctx.font = '10px ui-monospace,Menlo,monospace';
    const stepLbl = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(w / 64))));
    for (let i = 0; i < n; i += stepLbl) {
      ctx.fillRect(i * sw, bh + (allRejected ? 6 : 0), 1, 3);
      ctx.fillText(String(this.frames[i].frame_idx), i * sw + 2, h - 2);
    }
    const px = (this.i + 0.5) * sw;
    ctx.strokeStyle = '#e6e8ec';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(px, -1); ctx.lineTo(px, bh + 2); ctx.stroke();
    ctx.fillStyle = '#e6e8ec';
    ctx.beginPath();
    ctx.moveTo(px - 5, bh + 2); ctx.lineTo(px + 5, bh + 2); ctx.lineTo(px, bh + 9);
    ctx.closePath(); ctx.fill();
  }

  /* ---- 曲线 ---- */

  chartGeom(cv) {
    const w = cv.width, h = cv.height;
    return { w, h, padL: 40, padR: 6, padT: 14, gap: 18, panH: (h - 14 * 2 - 18) / 2 };
  }

  drawChart() {
    const cv = gt$('#chart');
    if (!cv) return;
    const w = Math.max(200, Math.floor(cv.clientWidth));
    if (cv.width !== w) cv.width = w;
    const ctx = cv.getContext('2d');
    const G = this.chartGeom(cv);
    ctx.fillStyle = '#0f1115';
    ctx.fillRect(0, 0, G.w, G.h);
    const n = this.frames.length;
    if (!n || !this.series) return;
    const plotW = G.w - G.padL - G.padR;
    const xAt = (i) => G.padL + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const panels = [
      { vals: this.series.xs, top: G.padT, label: 'pano_x', color: '#60a5fa' },
      { vals: this.series.ys, top: G.padT + G.panH + G.gap, label: 'pano_y', color: '#4ade80' },
    ];
    for (const p of panels) {
      const valid = p.vals.filter((v) => v !== null);
      let lo = valid.length ? Math.min.apply(null, valid) : 0;
      let hi = valid.length ? Math.max.apply(null, valid) : 1;
      if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.08;
      lo -= pad; hi += pad;
      const yAt = (v) => p.top + G.panH - (v - lo) / (hi - lo) * G.panH;
      ctx.strokeStyle = '#2c313b';
      ctx.lineWidth = 1;
      ctx.strokeRect(G.padL, p.top, plotW, G.panH);
      ctx.fillStyle = '#8b93a1';
      ctx.font = '10px ui-monospace,Menlo,monospace';
      ctx.fillText(Math.round(hi), 2, p.top + 9);
      ctx.fillText(Math.round(lo), 2, p.top + G.panH - 1);
      ctx.fillStyle = p.color;
      ctx.font = '11px sans-serif';
      ctx.fillText(p.label, G.padL + 4, p.top - 3);
      ctx.strokeStyle = p.color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < n; i++) {
        const v = p.vals[i];
        if (v === null) { started = false; continue; }
        const x = xAt(i), y = yAt(v);
        if (started) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        started = true;
      }
      ctx.stroke();
      for (let i = 0; i < n; i++) {
        const v = p.vals[i];
        if (v === null) continue;
        const f = this.frames[i];
        if (this.series.out[i]) {
          ctx.strokeStyle = '#f87171';
          ctx.lineWidth = 1.6;
          ctx.beginPath(); ctx.arc(xAt(i), yAt(v), 3.6, 0, 6.283); ctx.stroke();
        } else if (f.interp) {
          ctx.fillStyle = '#fbbf24';
          ctx.fillRect(xAt(i) - 1.5, yAt(v) - 1.5, 3, 3);
        }
      }
    }
    const px = xAt(this.i);
    ctx.strokeStyle = 'rgba(230,232,236,.85)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px, 2); ctx.lineTo(px, G.h - 2); ctx.stroke();
  }

  /* ---- 主画面 ---- */

  stageFit(iw, ih) {
    const st = gt$('#stage');
    const cv = gt$('#overview');
    const availW = Math.max(40, (st ? st.clientWidth : 800) - 8);
    const availH = Math.max(40, (st ? st.clientHeight : 400) - 8);
    const scale = Math.min(availW / Math.max(iw, 1), availH / Math.max(ih, 1));
    const w = Math.max(1, Math.floor(iw * scale));
    const h = Math.max(1, Math.floor(ih * scale));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    cv.style.width = w + 'px';
    cv.style.height = h + 'px';
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#0a0b0e';
    ctx.fillRect(0, 0, w, h);
    return { cv, w, h, ctx };
  }

  overlayTrack(ctx, w, h, srcW, srcH, toXY, label, imgScale) {
    const kx = w / Math.max(srcW, 1), ky = h / Math.max(srcH, 1);
    const [lo, hi] = this.rangeBounds();
    const strokePath = (from, to, color, width) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      let started = false;
      for (let i = from; i <= to; i++) {
        const pt = toXY(this.frames[i]);
        if (!pt) { started = false; continue; }
        const x = pt.x * kx, y = pt.y * ky;
        if (started) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        started = true;
      }
      ctx.stroke();
    };
    strokePath(0, this.frames.length - 1, 'rgba(96,165,250,.45)', 1.5);
    if (lo !== 0 || hi !== this.frames.length - 1) strokePath(lo, hi, 'rgba(96,165,250,.95)', 2.2);

    for (const fr of this.frames) {
      if (!this.isRejected(fr)) continue;
      const pt = toXY(fr);
      if (!pt) continue;
      const x = pt.x * kx, y = pt.y * ky;
      ctx.strokeStyle = 'rgba(248,113,113,.9)';
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(x - 4, y - 4); ctx.lineTo(x + 4, y + 4);
      ctx.moveTo(x + 4, y - 4); ctx.lineTo(x - 4, y + 4);
      ctx.stroke();
    }

    const f = this.cur();
    const cur = toXY(f);
    if (cur) {
      const x = cur.x * kx, y = cur.y * ky;
      ctx.strokeStyle = '#fbbf24';
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 8, 0, 6.283); ctx.stroke();
      const cell = (this.man && this.man.cell) || 192;
      const ov = (this.man && this.man.overview) || {};
      const sc = (imgScale !== undefined && imgScale !== null) ? Number(imgScale) : (Number(ov.scale) || 1);
      const half = cell * sc * kx / 2;
      ctx.strokeStyle = 'rgba(251,191,36,.7)';
      ctx.lineWidth = 1;
      ctx.strokeRect(x - half, y - half, half * 2, half * 2);
    }
    ctx.fillStyle = 'rgba(230,232,236,.8)';
    ctx.font = '12px ui-monospace,Menlo,monospace';
    ctx.fillText(label, 8, h - 10);
  }

  ovPoint(fr) {
    if (!fr || !fr.visible || fr.ox === null || fr.ox === undefined || fr.oy === null || fr.oy === undefined) return null;
    return { x: fr.ox, y: fr.oy };
  }

  scenePoint(fr, track) {
    const p = this.ovPoint(fr);
    if (!p) return null;
    const ov = (this.man && this.man.overview) || {};
    const ovS = Number(ov.scale) || 0;
    const scS = Number(track.scale) || 0;
    if (!ovS || !scS) return p;
    return { x: p.x / ovS * scS, y: p.y / ovS * scS };
  }

  mainSource() {
    const scene = this.tracks.scene || {};
    if (this.pane === 'raw' && scene.raw) return { kind: 'scene', track: scene.raw, label: '全局 · 当前帧原图' };
    if (this.pane === 'enh' && scene.enh) return { kind: 'scene', track: scene.enh, label: '全局 · 当前帧 DoG' };
    if (this.pane === 'enh' && this.ovEnh) return { kind: 'overview', file: this.ovEnh, label: '全局 · DoG 总览' };
    return null;
  }

  drawOverview() {
    if (this.mainView === 'scan' && this.scanUrl) {
      this.drawOverviewScan();
      return;
    }
    const kind = this.source && this.source.kind;
    if (kind === 'local') { this.drawOverviewLocal(); return; }
    if (kind === 'thumb') { this.drawOverviewThumb(); return; }
    const src = this.mainSource();
    const cv = gt$('#overview');
    if (!cv) return;
    if (!src) {
      const { ctx } = this.stageFit(400, 220);
      ctx.fillStyle = '#8b93a1';
      ctx.font = '14px sans-serif';
      ctx.fillText(this.pane === 'raw' ? '本包没有全局原图（只有局部裁切）' : '本包没有滤波主画面', 16, 28);
      this.mainSeek = null;
      return;
    }
    if (src.kind === 'overview') {
      const im = this.getImg(this.base + '/' + src.file);
      const ov = (this.man && this.man.overview) || {};
      const iw = (im.complete && im.naturalWidth) ? im.naturalWidth : (ov.w || ov.width || 1200);
      const ih = (im.complete && im.naturalHeight) ? im.naturalHeight : (ov.h || ov.height || 567);
      const { w, h, ctx } = this.stageFit(iw, ih);
      if (im.complete && im.naturalWidth) {
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(im, 0, 0, w, h);
      }
      this.overlayTrack(ctx, w, h, iw, ih, (fr) => this.ovPoint(fr), src.label, Number((this.man && this.man.overview || {}).scale) || 1);
      this.mainSeek = { kind: 'overview', iw, ih };
      return;
    }
    const t = src.track;
    const f = this.cur();
    const cols = t.cols || 1;
    const cw = t.cell_w || t.cell || (this.man && this.man.cell) || 192;
    const ch = t.cell_h || t.cell || cw;
    const { w, h, ctx } = this.stageFit(cw, ch);
    const im = this.getImg(this.base + '/' + t.file);
    if (f && im.complete && im.naturalWidth) {
      const col = f.cell % cols;
      const row = Math.floor(f.cell / cols);
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(im, col * cw, row * ch, cw, ch, 0, 0, w, h);
    } else if (!im.complete || !im.naturalWidth) {
      ctx.fillStyle = '#8b93a1';
      ctx.font = '14px sans-serif';
      ctx.fillText('画面加载中…', 14, 26);
    }
    this.overlayTrack(ctx, w, h, cw, ch, (fr) => this.scenePoint(fr, t), src.label, Number(t.scale) || 1);
    this.mainSeek = { kind: 'scene', track: t, cw, ch };
  }

  drawOverviewLocal() {
    const f = this.cur();
    const r = this.roi();
    const { w, h, ctx } = this.stageFit(r[2], r[3]);
    if (f) {
      const im = this.getImg(this.localURL(this.pane === 'enh' ? 'enh' : 'raw', f.frame_idx));
      if (im.complete && im.naturalWidth) {
        ctx.imageSmoothingEnabled = true;
        ctx.drawImage(im, 0, 0, w, h);
      } else if (im._failed && this.pane === 'enh') {
        ctx.fillStyle = '#8b93a1';
        ctx.font = '14px sans-serif';
        ctx.fillText('增强图还在后台生成，可先用原图标注', 16, 28);
      }
    }
    this.overlayTrack(ctx, w, h, r[2], r[3], (fr) => {
      if (!fr || !fr.visible || fr.pano_x == null) return null;
      return this.panoToRoi(fr.pano_x, fr.pano_y);
    }, this.pane === 'raw' ? '全局 · 本地原图' : '全局 · 本地滤波', 1);
    const crop = this.srcRect();
    const kx = w / r[2], ky = h / r[3];
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth = 2;
    ctx.strokeRect(crop.sx * kx, crop.sy * ky, crop.size * kx, crop.size * ky);
    this.mainSeek = { kind: 'local', iw: r[2], ih: r[3] };
  }

  drawOverviewThumb() {
    const r = this.roi();
    const v = this.activeVideo();
    const vw = (v && v.videoWidth) || r[2];
    const vh = (v && v.videoHeight) || r[3];
    const { w, h, ctx } = this.stageFit(vw, vh);
    if (v && v.readyState >= 2) {
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(v, 0, 0, w, h);
    } else {
      ctx.fillStyle = '#8b93a1';
      ctx.font = '14px sans-serif';
      ctx.fillText(this.pane === 'enh' ? '滤波视频加载中…' : '原视频加载中…', 16, 28);
    }
    this.overlayTrack(ctx, w, h, r[2], r[3], (fr) => {
      if (!fr || !fr.visible || fr.pano_x == null) return null;
      return this.panoToRoi(fr.pano_x, fr.pano_y);
    }, this.pane === 'raw' ? '全局 · 缩略原图' : '全局 · 缩略 DoG', 1);
    this.mainSeek = { kind: 'thumb', iw: r[2], ih: r[3] };
  }

  drawOverviewScan() {
    const im = this.getImg(this.scanUrl);
    const r = this.roi();
    const iw = (im.complete && im.naturalWidth) ? im.naturalWidth : r[2];
    const ih = (im.complete && im.naturalHeight) ? im.naturalHeight
      : Math.max(1, Math.round(r[3] * iw / Math.max(r[2], 1)));
    const { w, h, ctx } = this.stageFit(iw, ih);
    if (im.complete && im.naturalWidth) {
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(im, 0, 0, w, h);
    } else {
      ctx.fillStyle = '#8b93a1';
      ctx.font = '14px sans-serif';
      ctx.fillText('Scan 图加载中…', 16, 28);
    }
    this.overlayTrack(ctx, w, h, r[2], r[3], (fr) => {
      if (!fr || !fr.visible || fr.pano_x == null) return null;
      return this.panoToRoi(fr.pano_x, fr.pano_y);
    }, '主画面 · Scan 时间彩色（E 回视频）', 1);
    this.mainSeek = { kind: 'scan', iw: r[2], ih: r[3] };
  }

  drawPlayerThumb() {
    const cv = gt$('#player');
    if (!cv) return;
    const side = GT_ZOOM_SIDE;
    if (cv.width !== side) { cv.width = side; cv.height = side; }
    cv.style.width = side + 'px';
    cv.style.height = side + 'px';
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, side, side);
    const f = this.cur();
    if (!f) return;
    const rec = this.srcRect();
    const v = this.activeVideo();
    const r = this.roi();
    const vw = (v && v.videoWidth) || r[2];
    const vh = (v && v.videoHeight) || r[3];
    const sx = rec.sx * vw / r[2], sy = rec.sy * vh / r[3];
    const ss = rec.size * vw / r[2];
    if (v && v.readyState >= 2) {
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(v, sx, sy, ss, ss, 0, 0, side, side);
    } else {
      ctx.fillStyle = '#555';
      ctx.font = '14px sans-serif';
      ctx.fillText('视频加载中…', 14, 26);
    }
    const k = side / rec.size;
    let m = null;
    if (f.visible && f.pano_x != null) {
      const p = this.panoToRoi(f.pano_x, f.pano_y);
      m = { x: p.x - rec.sx, y: p.y - rec.sy };
    }
    this._strokeCross(ctx, m, k, f, side);
  }

  drawScan() {
    const cv = gt$('#scan');
    if (!cv || !this.scanUrl) return;
    const im = this.getImg(this.scanUrl);
    const r = this.roi();
    const iw = (im.complete && im.naturalWidth) ? im.naturalWidth : r[2];
    const ih = (im.complete && im.naturalHeight) ? im.naturalHeight : Math.max(1, Math.round(r[3] * iw / r[2]));
    const maxW = Math.max(200, Math.floor(cv.clientWidth || 400));
    const sc = Math.min(1, maxW / iw);
    const w = Math.round(iw * sc), h = Math.round(ih * sc);
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#0b0d11';
    ctx.fillRect(0, 0, w, h);
    if (im.complete && im.naturalWidth) ctx.drawImage(im, 0, 0, w, h);
    this.overlayTrack(ctx, w, h, r[2], r[3], (fr) => {
      if (!fr || !fr.visible || fr.pano_x == null) return null;
      return this.panoToRoi(fr.pano_x, fr.pano_y);
    }, 'Scan · 时间彩色 + 标注折线', 1);
    cv.onclick = (e) => {
      const b = cv.getBoundingClientRect();
      const px = (e.clientX - b.left) * (iw / Math.max(w, 1));
      const py = (e.clientY - b.top) * (ih / Math.max(h, 1));
      const x = px * (r[2] / Math.max(iw, 1));
      const y = py * (r[3] / Math.max(ih, 1));
      let best = -1, bestD = 1e18;
      this.frames.forEach((f, i) => {
        if (!f.visible || f.pano_x == null) return;
        const p = this.panoToRoi(f.pano_x, f.pano_y);
        const d = (p.x - x) * (p.x - x) + (p.y - y) * (p.y - y);
        if (d < bestD) { bestD = d; best = i; }
      });
      if (best >= 0) { this.seek(best, true); return; }
      if (im.complete && im.naturalWidth) {
        const tmp = document.createElement('canvas');
        tmp.width = 1; tmp.height = 1;
        const tctx = tmp.getContext('2d');
        const sx = Math.max(0, Math.min(im.naturalWidth - 1, Math.floor(px)));
        const sy = Math.max(0, Math.min(im.naturalHeight - 1, Math.floor(py)));
        tctx.drawImage(im, sx, sy, 1, 1, 0, 0, 1, 1);
        const rgb = tctx.getImageData(0, 0, 1, 1).data;
        if (rgb[0] + rgb[1] + rgb[2] > 24) {
          const t = (rgb[0] + 1) / (rgb[0] + rgb[2] + 2);
          this.seek(Math.round(t * Math.max(0, this.frames.length - 1)), true);
        }
      }
    };
  }

  drawTable() {
    if (this.hasCells || (this.source && this.source.kind === 'local')) return;
    const tb = gt$('#tbody');
    if (!tb) return;
    if (tb.dataset.n !== String(this.frames.length)) {
      tb.innerHTML = '<tr><th>#</th><th>frame</th><th>pano_x</th><th>pano_y</th>'
        + '<th>conf</th><th>状态</th><th>裁决</th></tr>'
        + this.frames.map((f, i) => `<tr data-i="${i}">
        <td>${i + 1}</td><td>${f.frame_idx}</td>
        <td>${gtFmt(f.pano_x)}</td><td>${gtFmt(f.pano_y)}</td>
        <td style="color:${this.frameColorFn(f, this)}">${gtFmt(f.conf, 2)}</td>
        <td>${f.visible ? (f.interp ? '插值' : '') : '不可见'}</td>
        <td class="rj"></td></tr>`).join('');
      tb.dataset.n = String(this.frames.length);
      tb.querySelectorAll('tr[data-i]').forEach((tr) => {
        tr.onclick = () => this.seek(parseInt(tr.dataset.i, 10));
      });
    }
    tb.querySelectorAll('tr[data-i]').forEach((tr) => {
      const i = parseInt(tr.dataset.i, 10);
      tr.className = i === this.i ? 'cur' : '';
      const cell = tr.querySelector('.rj');
      if (cell) cell.innerHTML = this.isRejected(this.frames[i])
        ? '<span style="color:var(--bad)">错误</span>' : '';
      if (i === this.i && !this.playing) tr.scrollIntoView({ block: 'nearest' });
    });
  }

  drawTransport() {
    const pFps = gt$('#pFps');
    const bPlay = gt$('#bPlay');
    const bPane = gt$('#bPane');
    const bMode = gt$('#bMode');
    const bLoop = gt$('#bLoop');
    const bPing = gt$('#bPing');
    if (pFps) pFps.textContent = this.fps + ' fps';
    if (bPlay) bPlay.textContent = this.playing ? '暂停 ␣' : '播放 ␣';
    if (bPane) bPane.textContent = this.pane === 'raw' ? '滤波图 W' : '原图 W';
    const bScan = gt$('#bScan');
    if (bScan) {
      bScan.style.display = this.scanUrl ? '' : 'none';
      bScan.className = this.mainView === 'scan' ? 'on' : '';
      bScan.textContent = this.mainView === 'scan' ? '视频 E' : 'Scan E';
    }
    if (bMode) bMode.textContent = this.mode === 'follow' ? '固定区域 F' : '跟随 F';
    if (bLoop) bLoop.className = this.loop ? 'on' : '';
    if (bPing) bPing.className = this.ping ? 'on' : '';
    if (this.opts.singleDefault) {
      const [lo, hi] = this.rangeBounds();
      const full = this.rangeSet && lo === 0 && hi === this.frames.length - 1;
      const bSelAll = gt$('#bSelAll');
      const bClr = gt$('#bClrRange');
      if (bSelAll) bSelAll.className = full ? 'on' : '';
      if (bClr) bClr.disabled = !this.rangeSet;
    }
  }

  draw() {
    this.drawPlayer();
    this.drawStrip();
    this.drawChart();
    this.drawOverview();
    this.drawScan();
    this.drawTable();
    this.drawTransport();
    if (this.opts.onDraw) this.opts.onDraw(this);
  }

  /* ---- 播放 ---- */

  seek(i, recenter) {
    const n = this.frames.length;
    if (!n) return;
    this.i = Math.max(0, Math.min(n - 1, i));
    if (recenter && this.source && (this.source.kind === 'local' || this.source.kind === 'thumb')) {
      const f = this.cur();
      if (f) this.recenterOn(f.frame_idx);
    }
    this.syncVideo();
    this.draw();
    if (this.onSeek) this.onSeek(this);
  }

  step(d) {
    this.stop();
    this.seek(this.i + d, true);
  }

  advance() {
    const lo = this.inPt, hi = this.outPt;
    let i = this.i + this.dir;
    if (i > hi) {
      if (this.ping) { this.dir = -1; i = Math.max(lo, hi - 1); }
      else if (this.loop) i = lo;
      else { i = hi; this.stop(); }
    } else if (i < lo) {
      if (this.ping) { this.dir = 1; i = Math.min(hi, lo + 1); }
      else if (this.loop) i = hi;
      else { i = lo; this.stop(); }
    }
    this.i = i;
    if (this.source && (this.source.kind === 'local' || this.source.kind === 'thumb')) {
      const f = this.cur();
      if (f) this.recenterOn(f.frame_idx);
    }
    this.syncVideo();
    this.draw();
    if (this.onSeek) this.onSeek(this);
  }

  _tick(t) {
    if (!this.playing) return;
    const need = 1000 / this.fps;
    if (!this._tickLast) this._tickLast = t;
    let dt = t - this._tickLast;
    if (dt >= need) {
      const steps = Math.min(3, Math.floor(dt / need));
      for (let k = 0; k < steps && this.playing; k++) this.advance();
      this._tickLast += steps * need;
    }
    requestAnimationFrame((tt) => this._tick(tt));
  }

  play() {
    if (this.playing || this.frames.length < 2) return;
    if (this.i >= this.outPt) this.i = this.inPt;
    this.playing = true;
    this._tickLast = 0;
    if (this.source && this.source.kind === 'thumb') {
      this.syncVideo();
      this.playVideos();
      this._bindThumbRvfc();
      this.drawTransport();
      return;
    }
    requestAnimationFrame((t) => this._tick(t));
    this.drawTransport();
  }

  stop() {
    this.playing = false;
    this.pauseVideos();
    this.drawTransport();
  }

  togglePlay() {
    this.playing ? this.stop() : this.play();
  }

  setFps(d) {
    let k = GT_FPS_STEPS.indexOf(this.fps);
    if (k < 0) k = GT_FPS_STEPS.indexOf(12);
    this.fps = GT_FPS_STEPS[Math.max(0, Math.min(GT_FPS_STEPS.length - 1, k + d))];
    if (this.playing && this.source && this.source.kind === 'thumb' && this._vids) {
      const rate = this.thumbPlayRate();
      ['clip', 'dog'].forEach((name) => {
        if (this._vids[name]) this._vids[name].playbackRate = rate;
      });
    }
    this.drawTransport();
  }

  isSuspicious(i) {
    const f = this.frames[i];
    if (!f) return false;
    return !!(f.interp || !f.visible || f.noLabel || this.isRejected(f)
           || (this.series && this.series.out[i])
           || (f.conf !== null && f.conf !== undefined && f.conf < GT_LOW_CONF));
  }

  jumpSuspicious(dir) {
    const n = this.frames.length;
    for (let d = 1; d <= n; d++) {
      const i = ((this.i + dir * d) % n + n) % n;
      if (this.isSuspicious(i)) {
        this.stop();
        this.seek(i, true);
        gtToast(`可疑帧 ${this.frames[i].frame_idx}`, 900);
        return;
      }
    }
    gtToast('没有可疑帧', 1100);
  }

  togglePane() {
    const want = this.pane === 'raw' ? 'enh' : 'raw';
    const local = this.source && this.source.kind === 'local';
    const thumb = this.source && this.source.kind === 'thumb';
    const localHas = !!(this.tracks[this.mode] || {})[want];
    const sceneHas = !!(this.tracks.scene || {})[want];
    const ovHas = want === 'enh' && !!this.ovEnh;
    if (!local && !thumb && !localHas && !sceneHas && !ovHas) {
      gtToast(`没有${want === 'enh' ? '滤波图' : '原图'}可切`, 1600);
      return;
    }
    this.pane = want;
    if (thumb && this.playing) this._bindThumbRvfc();
    this.draw();
  }

  toggleScan() {
    if (!this.scanUrl) {
      gtToast('这一包没有 Scan 图', 1600);
      return;
    }
    this.mainView = this.mainView === 'scan' ? 'video' : 'scan';
    this.draw();
  }

  toggleMode() {
    if (this.mode === 'follow') {
      if (!(this.tracks.region || {}).raw && !(this.tracks.region || {}).enh) {
        gtToast('本审核包没有 region 轨道，整体路径请看主画面总览图', 2200);
        return;
      }
      this.mode = 'region';
    } else {
      this.mode = 'follow';
    }
    if (!(this.tracks[this.mode] || {})[this.pane]) {
      this.pane = (this.tracks[this.mode] || {}).raw ? 'raw' : 'enh';
    }
    this.draw();
  }

  setIn() {
    this.inPt = Math.min(this.i, this.outPt);
    this.rangeSet = true;
    const f = this.frames[this.inPt];
    gtToast(`标记头 ${f ? f.frame_idx : ''}`, 900);
    this.draw();
  }

  setOut() {
    this.outPt = Math.max(this.i, this.inPt);
    this.rangeSet = true;
    const f = this.frames[this.outPt];
    gtToast(`标记尾 ${f ? f.frame_idx : ''}`, 900);
    this.draw();
  }

  clearRange() {
    this.inPt = 0;
    this.outPt = Math.max(0, this.frames.length - 1);
    this.rangeSet = false;
    gtToast(this.opts.clearRangeText || '本段恢复全段', 900);
    this.draw();
  }

  selectAll() {
    this.inPt = 0;
    this.outPt = Math.max(0, this.frames.length - 1);
    this.rangeSet = true;
    gtToast(`已选取全段 ${this.frames.length} 帧`, 900);
    this.draw();
  }

  stripSeek(ev) {
    const cv = gt$('#strip');
    const b = cv.getBoundingClientRect();
    const n = this.frames.length;
    if (!n) return;
    const i = Math.floor((ev.clientX - b.left) / b.width * n);
    this.stop();
    this.seek(i, true);
  }

  overviewSeek(ev) {
    const meta = this.mainSeek;
    if (!meta) { this.togglePlay(); return; }
    const cv = gt$('#overview');
    const b = cv.getBoundingClientRect();
    if (!b.width || !this.frames.length) return;
    const x = (ev.clientX - b.left) * (cv.width / b.width);
    const y = (ev.clientY - b.top) * (cv.height / b.height);

    if (meta.kind === 'local' && this.editable) {
      const r = this.roi();
      this.center.x = x * (r[2] / cv.width);
      this.center.y = y * (r[3] / cv.height);
      this.draw();
      return;
    }
    if (meta.kind === 'cell') { this.togglePlay(); return; }

    let best = this.i, bestD = Infinity;
    const srcW = meta.kind === 'scene' ? meta.cw : meta.iw;
    const srcH = meta.kind === 'scene' ? meta.ch : meta.ih;
    const kx = cv.width / srcW, ky = cv.height / srcH;
    const useRoi = meta.kind === 'local' || meta.kind === 'thumb' || meta.kind === 'scan';
    for (let i = 0; i < this.frames.length; i++) {
      let pt;
      if (useRoi) {
        const fr = this.frames[i];
        pt = (fr && fr.visible && fr.pano_x != null) ? this.panoToRoi(fr.pano_x, fr.pano_y) : null;
      } else {
        pt = meta.kind === 'scene' ? this.scenePoint(this.frames[i], meta.track) : this.ovPoint(this.frames[i]);
      }
      if (!pt) continue;
      const d = Math.hypot(pt.x * kx - x, pt.y * ky - y);
      if (d < bestD) { bestD = d; best = i; }
    }
    this.stop();
    this.seek(best, true);
  }

  chartSeek(ev) {
    const cv = gt$('#chart');
    const b = cv.getBoundingClientRect();
    const n = this.frames.length;
    if (n < 2) return;
    const G = this.chartGeom(cv);
    const x = (ev.clientX - b.left) * (cv.width / b.width);
    const i = Math.round((x - G.padL) / (G.w - G.padL - G.padR) * (n - 1));
    this.stop();
    this.seek(i, true);
  }

  playerClick(ev) {
    if (this.editable && this.source && this.source.kind === 'local' && this.onMark) {
      const cv = gt$('#player');
      const b = cv.getBoundingClientRect();
      const rec = this.srcRect();
      const side = GT_ZOOM_SIDE;
      const px = (ev.clientX - b.left) * (cv.width / b.width);
      const py = (ev.clientY - b.top) * (cv.height / b.height);
      this.onMark({
        roiX: rec.sx + px * (rec.size / side),
        roiY: rec.sy + py * (rec.size / side),
        frame_idx: this.cur() && this.cur().frame_idx,
      });
      return;
    }
    this.togglePlay();
  }

  bindTransport() {
    const on = (id, fn) => { const el = gt$('#' + id); if (el) el.onclick = fn; };
    on('bPlay', () => this.togglePlay());
    on('bPrev', () => this.step(-1));
    on('bNext', () => this.step(1));
    on('bSlower', () => this.setFps(-1));
    on('bFaster', () => this.setFps(1));
    on('bPane', () => this.togglePane());
    on('bScan', () => this.toggleScan());
    on('bMode', () => this.toggleMode());
    on('bLoop', () => { this.loop = !this.loop; this.drawTransport(); });
    on('bPing', () => { this.ping = !this.ping; this.drawTransport(); });
    on('bIn', () => this.setIn());
    on('bOut', () => this.setOut());
    on('bClrRange', () => this.clearRange());
    on('bSus', () => this.jumpSuspicious(1));

    const cv = gt$('#strip');
    if (cv && !cv._gtBound) {
      cv._gtBound = true;
      let drag = false;
      cv.addEventListener('mousedown', (e) => { drag = true; this.stripSeek(e); });
      window.addEventListener('mousemove', (e) => { if (drag) this.stripSeek(e); });
      window.addEventListener('mouseup', () => { drag = false; });
    }
    const ch = gt$('#chart');
    if (ch && !ch._gtBound) {
      ch._gtBound = true;
      ch.addEventListener('mousedown', (e) => this.chartSeek(e));
    }
    const ov = gt$('#overview');
    if (ov && !ov._gtBound) {
      ov._gtBound = true;
      ov.addEventListener('mousedown', (e) => this.overviewSeek(e));
    }
    const pl = gt$('#player');
    if (pl && !pl._gtBound) {
      pl._gtBound = true;
      pl.addEventListener('click', (e) => this.playerClick(e));
      pl.addEventListener('contextmenu', (e) => e.preventDefault());
      pl.addEventListener('wheel', (e) => {
        if (!(this.source && this.source.kind === 'local')) return;
        e.preventDefault();
        const f = e.deltaY > 0 ? 1.18 : 1 / 1.18;
        const r = this.roi();
        this.viewSize = Math.max(160, Math.min(r[2], r[3], Math.round(this.viewSize * f)));
        this.draw();
      }, { passive: false });
      let dragging = null;
      pl.addEventListener('mousedown', (e) => {
        if (e.button !== 2 || !(this.source && this.source.kind === 'local')) return;
        const b = pl.getBoundingClientRect();
        dragging = { px: e.clientX - b.left, py: e.clientY - b.top, cx: this.center.x, cy: this.center.y };
      });
      window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const b = pl.getBoundingClientRect();
        const rec = this.srcRect();
        const k = rec.size / GT_ZOOM_SIDE;
        this.center.x = dragging.cx - (e.clientX - b.left - dragging.px) * k;
        this.center.y = dragging.cy - (e.clientY - b.top - dragging.py) * k;
        this.draw();
      });
      window.addEventListener('mouseup', () => { dragging = null; });
    }
    window.addEventListener('resize', () => {
      clearTimeout(this._layoutT);
      this._layoutT = setTimeout(() => this.draw(), 80);
    });
  }

  handlePlayKey(e) {
    switch (e.code) {
      case 'Space': e.preventDefault(); this.togglePlay(); return true;
      case 'Comma': this.step(-1); return true;
      case 'Period': this.step(1); return true;
      case 'Minus': case 'NumpadSubtract': this.setFps(-1); return true;
      case 'Equal': case 'NumpadAdd': this.setFps(1); return true;
      case 'KeyW': this.togglePane(); return true;
      case 'KeyE': this.toggleScan(); return true;
      case 'KeyF': this.toggleMode(); return true;
      case 'KeyJ': this.jumpSuspicious(1); return true;
      case 'KeyK': this.jumpSuspicious(-1); return true;
      case 'KeyH': case 'BracketLeft': this.setIn(); return true;
      case 'KeyT': case 'BracketRight': this.setOut(); return true;
      case 'Backslash': this.clearRange(); return true;
      default: return false;
    }
  }
}

window.GTPlayer = GTPlayer;
window.gt$ = gt$;
window.gtFmt = gtFmt;
window.gtToast = gtToast;
window.gtHideToast = gtHideToast;
window.gtBuildFrames = gtBuildFrames;
window.GT_LOW_CONF = GT_LOW_CONF;
window.GT_ZOOM_SIDE = GT_ZOOM_SIDE;
window.GT_FPS_STEPS = GT_FPS_STEPS;