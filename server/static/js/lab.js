(() => {
  const $ = (s) => document.querySelector(s);
  const pidSel = $("#packet");
  const modelSel = $("#model");
  const gridsEl = $("#grids");

  let boot = null;
  let state = null;
  let hover = null;
  let hoverStrip = null;
  let bust = 0;
  let panes = [];

  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.style.display = "block";
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.style.display = "none"; }, 2200);
  }

  async function jget(url) {
    const r = await fetch(url);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }
  async function jpost(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }

  function pid() { return (state && state.packet_id) || pidSel.value; }

  async function loadBoot() {
    boot = await jget("/api/bootstrap");
    $("#wfPill").textContent = "工作流 " + (boot.workflow || "");
    pidSel.innerHTML = (boot.packets || []).map((p) =>
      `<option value="${p.packet_id}" data-root="${p.work_root}">${p.packet_id}　${p.n_targets}帧${p.n_labels ? " ·有labels" : ""}</option>`
    ).join("") || `<option value="">（没有已解码的包）</option>`;
    const models = [
      { name: "", label: "角色 primary（自动降级）" },
      ...boot.providers.map((p) => ({
        name: p.name,
        label: `${p.name} · ${p.model}${p.has_key ? "" : "（无 key）"}`,
        disabled: !p.has_key,
      })),
    ];
    modelSel.innerHTML = models.map((m) =>
      `<option value="${m.name}" ${m.disabled ? "disabled" : ""}>${m.label}</option>`
    ).join("");
    const pathPid = location.pathname.startsWith("/p/") ? decodeURIComponent(location.pathname.slice(3)) : "";
    if (pathPid) pidSel.value = pathPid;
    if (pathPid && pidSel.value === pathPid) {
      await openPkt();
    }
  }

  async function openPkt() {
    const packet_id = pidSel.value;
    if (!packet_id) { toast("没有包"); return; }
    const opt = pidSel.selectedOptions[0];
    const work_root = opt && opt.dataset.root;
    const n = Number($("#seed").value);
    toast("正在打开…");
    state = await jpost("/api/open", {
      packet_id, work_root, seed_n: Number.isFinite(n) ? n : 0,
    });
    history.replaceState(null, "", "/p/" + encodeURIComponent(packet_id));
    await render();
    toast("已打开 " + packet_id);
  }

  function stageClass(s) {
    if (s.skipped) return "skip";
    if (s.error) return "fail";
    if (s.ok) return "ok";
    if (!s.ok && !s.skipped) return "null";
    return "";
  }

  function renderStages() {
    const cur = state && state.pending && state.pending.stage;
    const stages = [...(state.stages || [])];
    if (cur && !stages.some((s) => s.stage === cur)) {
      stages.push({
        stage: cur, skipped: false,
        size: state.pending.size, grid: state.pending.grid,
        reason: "等待点选",
      });
    }
    const html = stages.map((s) => {
      const bits = [];
      if (s.stage === cur) bits.push("等待点选");
      else if (s.skipped) bits.push("跳过");
      else if (s.cell) bits.push(s.cell + "  conf " + (s.conf || 0).toFixed(2));
      else if (s.reason) bits.push(s.reason);
      if (s.provider) bits.push(s.provider);
      if (s.size) bits.push(s.size + "px 窗");
      const cls = (s.stage === cur ? "cur " : "") + stageClass(s);
      return `<div class="stage ${cls}"><div class="id">${s.stage}</div><div class="meta">${bits.join(" · ")}</div></div>`;
    }).join("");
    $("#stages").innerHTML = html || `<div class="empty">还没有阶段</div>`;
  }

  function flagsHtml() {
    if (!state) return "";
    const a = state.accepted;
    const p = state.pending;
    let h = "";
    if (p) {
      const extra = p.pixel_click ? "　整幅 ROI 点像素" : "";
      const nc = (p.candidates || []).length;
      const ni = p.n_images > 1 ? `　${p.n_images} 张 1:1` : "";
      h += `<div class="pill">当前 ${p.stage}　${p.grid[0]}×${p.grid[1]}　每格 ${p.cell_px}px　窗 ${p.size}px${ni}${extra}${nc ? "　本地候选 " + nc : ""}</div>`;
    }
    if (a) {
      const out = a.in_field === false;
      h += `<div class="${out ? "warnbox" : "okbox"}">采纳 (${a.x}, ${a.y})　conf ${a.conf}
        ${a.in_field === false ? " · 在球场四边形外" : (a.in_field ? " · 在球场四边形内" : "")}
        ${a.in_roi === false ? " · 超出 ROI" : ""}
        ${a.gt_err != null ? " · 距 GT " + a.gt_err + "px" : ""}</div>`;
    }
    if (state.finished && !p) h += `<div class="okbox">这一帧阶段走完了，按 N 进下一帧。</div>`;
    if (state.all_done) h += `<div class="okbox">包内可交互帧走完了。</div>`;
    return h;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function renderThink() {
    const list = (state && state.suggestions) || [];
    const last = list.length ? list[list.length - 1] : null;
    const box = $("#think");
    if (!last) {
      box.className = "empty";
      box.textContent = "还没有。点「让模型点选」后，思考会整段印在这里。";
      return;
    }
    const think = (last.reasoning || "").trim();
    const ans = (last.answer || "").trim();
    const head = `${last.provider || ""} / ${last.model || ""}　${last.cached ? "缓存" : ((last.latency || 0) + "s")}`;
    if (think) {
      box.className = "";
      box.textContent = head + "\n\n" + think + (ans ? "\n\n—— 最终输出 ——\n" + ans : "");
    } else {
      box.className = "empty";
      box.textContent = head + "\n\n（接口没有返回思考。关了思考的模型本来就不会写；glm-5.3-flash 关不掉，应出现在这里。）"
        + (ans ? "\n\n最终输出：\n" + ans : "");
    }
  }

  function renderSugs() {
    const list = (state && state.suggestions) || [];
    if (!list.length) {
      $("#sugs").innerHTML = `<div class="empty">还没有。选一个模型点「让模型点选」，或「对比已配置模型」。</div>`;
      return;
    }
    $("#sugs").innerHTML = list.map((s) => {
      const extra = [
        s.shift ? `要求挪窗 x${s.shift.x >= 0 ? "+" : ""}${s.shift.x} y${s.shift.y >= 0 ? "+" : ""}${s.shift.y}${s.applied_shift ? "（已重裁，可再问一次）" : ""}` : null,
        s.no_ball ? "两张图都没球 → 按 X 跳过这一帧" : null,
        s.gt_err != null ? "距GT " + s.gt_err + "px" : null,
        s.in_field === false ? "场外" : null,
        s.cached ? "缓存" : (s.latency + "s"),
        s.error ? String(s.error).slice(0, 160) : null,
      ].filter(Boolean).join(" · ");
      const adopt = (s.cell && !s.error)
        ? `<button data-adopt="${s.provider}">采用这一格</button>` : "";
      const think = (s.reasoning || "").trim();
      const thinkHtml = think
        ? `<div class="think">${esc(think)}</div>`
        : `<div class="ans">（没有思考正文）</div>`;
      const ansHtml = s.answer ? `<div class="ans">输出 ${esc(s.answer)}</div>` : "";
      return `<div class="sug">
        <div><b>${esc(s.cell || "null")}</b>　${esc(s.provider)} / ${esc(s.model || "")}　conf ${(s.conf || 0).toFixed(2)}</div>
        <div style="color:var(--dim);font-size:11px;margin:3px 0">${esc(extra)}</div>
        ${adopt}
        ${thinkHtml}
        ${ansHtml}
      </div>`;
    }).join("");
    $("#sugs").querySelectorAll("[data-adopt]").forEach((b) => {
      b.onclick = async () => {
        state = await jpost("/api/adopt/" + encodeURIComponent(pid()), { provider: b.dataset.adopt });
        await render();
      };
    });
  }

  function letterList(layout) {
    if (!layout) return [];
    const raw = layout.letters;
    if (Array.isArray(raw)) return raw;
    return String(raw || "").split("");
  }

  function cellRect(layout, cell) {
    const letters = letterList(layout);
    const m = String(cell).toUpperCase().match(/^([A-Z]+)(\d+)$/);
    if (!m) return null;
    const c = letters.indexOf(m[1]);
    const r = parseInt(m[2], 10) - 1;
    if (c < 0 || r < 0 || r >= layout.rows) return null;
    const x0 = layout.body_x + (c / layout.cols) * layout.body_w;
    const y0 = layout.body_y + (r / layout.rows) * layout.body_h;
    return { x: x0, y: y0, w: layout.body_w / layout.cols, h: layout.body_h / layout.rows };
  }

  function imageMeta(strip) {
    const p = state && state.pending;
    if (!p) return {};
    if (p.images && p.images[strip]) return p.images[strip];
    if (p.strips && p.strips[strip]) return { ...p.strips[strip], clickable: true, role: "strip" };
    return { i: strip, role: "local", clickable: true, layout: p.layout };
  }

  function paneLayout(strip) {
    const p = state && state.pending;
    if (!p) return null;
    const m = imageMeta(strip);
    if (m.layout) return m.layout;
    if (p.strips && p.strips[strip]) return p.strips[strip].layout;
    return p.layout;
  }

  function drawPane(pane) {
    const { canvas, ctx, img, strip } = pane;
    if (!img.complete || !img.naturalWidth) return;
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    const layout = paneLayout(strip);
    if (!layout) return;
    const mark = (cell, color) => {
      const rc = cellRect(layout, cell);
      if (!rc) return;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.strokeRect(rc.x + 1, rc.y + 1, rc.w - 2, rc.h - 2);
      ctx.restore();
    };
    if (hover && hoverStrip === strip) mark(hover, "rgba(96,165,250,.95)");
    if (state.show_gt && state.gt_cell) mark(state.gt_cell, "rgba(74,222,128,.95)");
    (state.suggestions || []).forEach((s, i) => {
      if (s.cell) mark(s.cell, `hsla(${40 + i * 50},80%,60%,0.9)`);
    });
  }

  function drawAll() {
    panes.forEach(drawPane);
  }

  function canvasXY(canvas, ev) {
    const r = canvas.getBoundingClientRect();
    const x = (ev.clientX - r.left) * (canvas.width / r.width);
    const y = (ev.clientY - r.top) * (canvas.height / r.height);
    return [x, y];
  }

  function cellAt(layout, canvas, ev) {
    if (!layout) return null;
    const [x, y] = canvasXY(canvas, ev);
    const n = layout.n_panels;
    const pw = layout.pane_w, ph = layout.pane_h, gap = layout.pane_gap;
    let lx, ly;
    if (layout.axis === 0) {
      const i = Math.floor(y / (ph + gap));
      if (i < 0 || i >= n) return null;
      lx = x; ly = y - i * (ph + gap);
    } else {
      const i = Math.floor(x / (pw + gap));
      if (i < 0 || i >= n) return null;
      lx = x - i * (pw + gap); ly = y;
    }
    if (lx < layout.body_x || ly < layout.body_y ||
        lx >= layout.body_x + layout.body_w || ly >= layout.body_y + layout.body_h) return null;
    const c = Math.floor((lx - layout.body_x) / layout.body_w * layout.cols);
    const r = Math.floor((ly - layout.body_y) / layout.body_h * layout.rows);
    if (c < 0 || r < 0 || c >= layout.cols || r >= layout.rows) return null;
    const letters = letterList(layout);
    return (letters[c] || "") + (r + 1);
  }

  function bindPane(pane) {
    const { canvas, strip } = pane;
    canvas.addEventListener("mousemove", (ev) => {
      hover = cellAt(paneLayout(strip), canvas, ev);
      hoverStrip = strip;
      const pixel = state && state.pending && state.pending.pixel_click;
      $("#hint").textContent = hover
        ? ("格子 " + hover + (pixel ? "（提交用点击位置，不只格心）" : ""))
        : (state && state.pending ? (pixel ? "在对应那张 1:1 大图上点球" : "点在画面格子里") : "");
      drawAll();
    });
    canvas.addEventListener("mouseleave", () => {
      if (hoverStrip === strip) { hover = null; hoverStrip = null; }
      drawAll();
    });
    canvas.addEventListener("click", async (ev) => {
      const meta = imageMeta(strip);
      if (meta.clickable === false) {
        toast("这是参考图，请在局部图上点格子");
        return;
      }
      const layout = paneLayout(strip);
      const cell = cellAt(layout, canvas, ev);
      const [x, y] = canvasXY(canvas, ev);
      if (!cell && !(state && state.pending && state.pending.pixel_click)) {
        toast("点在格子外");
        return;
      }
      const body = { x, y, cell, commit: true, conf: 0.85 };
      if (meta.role === "strip") body.strip = strip;
      try {
        state = await jpost("/api/click/" + encodeURIComponent(pid()), body);
        await render();
      } catch (e) { toast(e.message); }
    });
  }

  function rebuildCanvases() {
    const n = (state && state.pending && state.pending.n_images) || 0;
    const names = ["左", "中", "右"];
    gridsEl.innerHTML = "";
    panes = [];
    if (!n) return;
    for (let i = 0; i < n; i++) {
      const wrap = document.createElement("div");
      wrap.className = "strip";
      const cap = document.createElement("div");
      cap.className = "cap";
      const meta = imageMeta(i);
      const st = state.pending.strips && state.pending.strips[i];
      const lets = meta.letters || (st && st.letters) || "";
      const letStr = Array.isArray(lets) ? lets.join("") : lets;
      cap.textContent = meta.title || (n > 1
        ? `图${i + 1}　${names[i] || ("段" + (i + 1))}　列 ${letStr || "?"}`
        : "");
      if (n === 1 && !meta.title) cap.style.display = "none";
      const canvas = document.createElement("canvas");
      if (meta.clickable === false) canvas.style.cursor = "default";
      wrap.appendChild(cap);
      wrap.appendChild(canvas);
      gridsEl.appendChild(wrap);
      const pane = {
        canvas,
        ctx: canvas.getContext("2d"),
        img: new Image(),
        strip: i,
      };
      bindPane(pane);
      panes.push(pane);
    }
  }

  function pendingHint() {
    const p = state && state.pending;
    if (!p) return "这一帧的模型步骤已走完。";
    if (p.pixel_click && p.n_images > 1) {
      return `当前 ${p.stage}：三张 1:1 大图（左/中/右），格子 ${p.grid[0]}×${p.grid[1]}，每格约 ${p.cell_px} ROI-px。点错了按 Z 回退。`;
    }
    if (p.pixel_click) {
      return `当前 ${p.stage}：在整幅 ROI 上点球。网格 ${p.grid[0]}×${p.grid[1]}。`;
    }
    if (p.images && p.images.some((im) => im.role === "context")) {
      const sh = (p.shifts || []).length ? `　已挪窗 ${p.shifts.length} 次` : "";
      return `当前 ${p.stage}：上面是 RGB 参考图（青框=局部，橙线=轨迹），下面局部图点球心所在的格。方向键可挪窗。网格 ${p.grid[0]}×${p.grid[1]}，每格 ${p.cell_px} ROI-px。${sh}`;
    }
    return `当前 ${p.stage}：点格子提交。网格 ${p.grid[0]}×${p.grid[1]}，每格 ${p.cell_px} ROI-px。`;
  }

  async function render() {
    if (!state) return;
    $("#framePill").textContent = state.frame_idx == null ? "—" :
      `f${state.frame_idx}　种子 ${state.seed_frames.length}　已标 ${state.n_done}`;
    $("#prompt").textContent = (state.pending && state.pending.prompt) || "（这一帧没有等待点选的阶段）";
    $("#prompt").className = state.pending ? "" : "empty";
    renderStages();
    $("#flags").innerHTML = flagsHtml();
    renderSugs();
    renderThink();
    $("#undoBtn").disabled = !state.can_undo;
    hover = null;
    hoverStrip = null;
    bust += 1;
    const q = "?t=" + bust;
    rebuildCanvases();
    if (state.pending) {
      const n = state.pending.n_images || 1;
      panes.forEach((pane) => {
        pane.img.onload = () => drawPane(pane);
        const src = n > 1
          ? `/api/view/${encodeURIComponent(pid())}/${pane.strip}.jpg${q}`
          : `/api/view/${encodeURIComponent(pid())}.jpg${q}`;
        pane.img.src = src;
      });
    }
    $("#ov").src = "/api/overview/" + encodeURIComponent(pid()) + ".jpg" + q;
    $("#hint").textContent = pendingHint();
  }

  $("#open").onclick = () => openPkt().catch((e) => toast(e.message));
  $("#nullBtn").onclick = async () => {
    try {
      state = await jpost("/api/click/" + encodeURIComponent(pid()), { null: true });
      await render();
    } catch (e) { toast(e.message); }
  };
  $("#undoBtn").onclick = async () => {
    try {
      state = await jpost("/api/undo/" + encodeURIComponent(pid()), {});
      await render();
      toast("已回退");
    } catch (e) { toast(e.message); }
  };
  $("#next").onclick = async () => {
    try {
      state = await jpost("/api/next/" + encodeURIComponent(pid()), {});
      await render();
    } catch (e) { toast(e.message); }
  };
  $("#showGt").onchange = async (ev) => {
    if (!state) return;
    state = await jpost("/api/flag/" + encodeURIComponent(pid()), { show_gt: ev.target.checked });
    await render();
  };
  $("#ask").onclick = async () => {
    toast("正在问模型…");
    try {
      const d = await jpost("/api/ask/" + encodeURIComponent(pid()), { provider: modelSel.value || null });
      state = d.state || state;
      if (d.suggestion && d.suggestion.cell) toast(d.suggestion.provider + " → " + d.suggestion.cell);
      else toast((d.suggestion && (d.suggestion.error || "null")) || "问完了");
      await render();
    } catch (e) { toast(e.message); }
  };
  $("#compare").onclick = async () => {
    const names = (boot.available || []).filter((n) => n !== "deepseek_text");
    toast("对比 " + names.join(", "));
    for (const name of names) {
      try {
        const d = await jpost("/api/ask/" + encodeURIComponent(pid()), { provider: name });
        state = d.state || state;
      } catch (e) { toast(name + ": " + e.message); }
    }
    await render();
  };

  async function shiftWindow(dx, dy) {
    if (!(state && state.pending && state.pending.can_shift)) {
      toast("这一级不用挪窗");
      return;
    }
    try {
      state = await jpost("/api/shift/" + encodeURIComponent(pid()), { dx, dy });
      await render();
      toast(`挪窗 x${dx >= 0 ? "+" : ""}${dx} y${dy >= 0 ? "+" : ""}${dy}`);
    } catch (e) { toast(e.message); }
  }

  document.addEventListener("keydown", (ev) => {
    const t = ev.target;
    if (t && typeof t.matches === "function" && t.matches("input,select,textarea")) return;
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[ev.key]) {
      ev.preventDefault();
      shiftWindow(arrows[ev.key][0], arrows[ev.key][1]);
      return;
    }
    const k = ev.key.toLowerCase();
    if (k === "x") $("#nullBtn").click();
    if (k === "z") $("#undoBtn").click();
    if (k === "n") $("#next").click();
    if (k === "m") $("#ask").click();
    if (k === "g") { $("#showGt").checked = !$("#showGt").checked; $("#showGt").dispatchEvent(new Event("change")); }
  });

  loadBoot().catch((e) => toast(e.message));
})();
