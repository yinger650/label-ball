#!/usr/bin/env python3
"""单包 HTML 追踪报告：每帧每阶段的**实际入模图片** + 模型的**原始回答** + 判定。

这是调提示词唯一有效的工具。看聚合指标只能知道「变差了」，看不出为什么；只有把
那张真正送进模型的图和它逐字说了什么摆在一起，才能发现「哦，这一级的裁切把球
切到边上去了」「模型每次都选那条边线」这种问题。

报告是**单文件 HTML**，图片全部内联成 data URI：可以直接 scp 给别人、可以塞进
issue、可以离线打开，不依赖任何服务。代价是文件大（一包几十 MB），所以默认只
详细展开有问题的帧（页内有「全部展开」按钮）。

用法：
    venv/bin/python3 -m client.inspect --packet 260116__004__1533_1543 --frames 12 --seed-gt 2
    venv/bin/python3 -m client.inspect --packet ... --out /tmp/x.html
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import track as T, views as V  # noqa: E402
from client.prep import load_prepared, pano_to_roi  # noqa: E402
from client.vlm import VlmPool  # noqa: E402
from client.workflow import FrameRun, StageRun, Workflow, summarize, to_obs  # noqa: E402

WORK = Path("/mnt/afs/wangmin/data/_ai_label_work/work")
GT = Path("/mnt/afs/wangmin/data/_ai_label_work/gt")


def _b64(data: bytes, mime: str = "image/webp") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


CSS = """
:root{--bg:#14161a;--fg:#e6e6e6;--dim:#8b8f96;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--line:#2a2e35}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;margin:0;padding:20px}
h1{font-size:19px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 8px;color:#cfd3d8}
.sub{color:var(--dim);margin-bottom:16px}
table{border-collapse:collapse;font-size:12px;margin:8px 0}
th,td{border:1px solid var(--line);padding:3px 8px;text-align:right;white-space:nowrap}
th{background:#1c1f25;color:#aab;font-weight:600} td.l,th.l{text-align:left}
.frame{border:1px solid var(--line);border-radius:6px;margin:10px 0;background:#181b20}
.fhead{padding:7px 12px;cursor:pointer;display:flex;gap:14px;align-items:center;user-select:none}
.fhead:hover{background:#1f232a}
.fhead .id{font-weight:600;min-width:86px}
.badge{padding:1px 7px;border-radius:9px;font-size:11px;border:1px solid}
.b-ok{color:var(--ok);border-color:#265} .b-bad{color:var(--bad);border-color:#622}
.b-warn{color:var(--warn);border-color:#653} .b-dim{color:var(--dim);border-color:#333}
.body{display:none;padding:4px 12px 14px;border-top:1px solid var(--line)}
.frame.open .body{display:block}
.stage{display:flex;gap:14px;margin:12px 0;padding:10px;background:#101317;border-radius:5px;border-left:3px solid #333}
.stage.hit{border-left-color:var(--ok)} .stage.miss{border-left-color:var(--bad)}
.stage.skip{border-left-color:#333;opacity:.55} .stage.nullans{border-left-color:var(--warn)}
.stage img{max-width:560px;border-radius:3px;display:block;background:#000}
.meta{min-width:290px;font-size:12px}
.meta div{margin:2px 0} .k{color:var(--dim);display:inline-block;min-width:76px}
pre{background:#0b0d10;border:1px solid var(--line);border-radius:4px;padding:7px;margin:6px 0 0;
    white-space:pre-wrap;word-break:break-all;font-size:11px;color:#b9c0c8;max-height:190px;overflow:auto}
details summary{cursor:pointer;color:var(--dim);font-size:11px;margin-top:5px}
.ctl{margin:10px 0}
button{background:#22262e;color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:5px 11px;
       cursor:pointer;margin-right:6px;font-size:12px}
button:hover{background:#2c313a}
.chart{background:#101317;border:1px solid var(--line);border-radius:6px;padding:8px;margin:8px 0}
"""

JS = """
function tog(e){e.classList.toggle('open')}
function all(o){document.querySelectorAll('.frame').forEach(f=>f.classList.toggle('open',o))}
function onlyBad(){document.querySelectorAll('.frame').forEach(f=>
  f.classList.toggle('open', f.dataset.bad==='1'))}
"""


def _traj_svg(runs: Sequence[FrameRun], pts, gt: Dict[int, Tuple[float, float]],
              size: Tuple[int, int]) -> str:
    """ROI 俯视图上的三条轨迹：模型原始点、平滑轨迹、人工 GT。

    一眼能看出「是整条跑偏了」还是「个别帧跳了」，这两种病的治法完全不同。
    """
    w, h = size
    W, H = 1080, max(120, int(1080 * h / max(w, 1)))
    sc = W / max(w, 1)

    def path(seq) -> str:
        d, pen = [], True
        for p in seq:
            if p is None:
                pen = True
                continue
            d.append(("M" if pen else "L") + f"{p[0]*sc:.1f},{p[1]*sc:.1f}")
            pen = False
        return " ".join(d)

    raw = [(r.x, r.y) if r.x is not None else None for r in runs]
    sm = [(p.smooth_x, p.smooth_y) if p.smooth_x is not None else None for p in pts]
    g = [gt.get(r.frame_idx) for r in runs]
    dots = []
    for r in runs:
        if r.x is None:
            continue
        e = gt.get(r.frame_idx)
        c = "#3fb950" if (e and np.hypot(r.x - e[0], r.y - e[1]) < 20) else ("#f85149" if e else "#58a6ff")
        dots.append(f'<circle cx="{r.x*sc:.1f}" cy="{r.y*sc:.1f}" r="2.6" fill="{c}"/>')
    return (
        f'<div class="chart"><svg viewBox="0 0 {W} {H}" width="100%" style="background:#0b0d10">'
        f'<path d="{path(g)}" stroke="#8b949e" stroke-width="3" fill="none" opacity=".55"/>'
        f'<path d="{path(sm)}" stroke="#d29922" stroke-width="1.6" fill="none"/>'
        f'<path d="{path(raw)}" stroke="#58a6ff" stroke-width="1" fill="none" opacity=".8"/>'
        f'{"".join(dots)}</svg>'
        f'<div style="color:#8b8f96;font-size:11px;margin-top:4px">'
        f'灰粗 = 人工 GT　橙 = 平滑轨迹　蓝 = 模型原始点（绿点 &lt;20px，红点 ≥20px）</div></div>'
    )


def _stage_html(s: StageRun, gt: Optional[Tuple[float, float]]) -> str:
    if s.skipped:
        return (f'<div class="stage skip"><div class="meta"><b>{_esc(s.stage)}</b> '
                f'<span class="badge b-dim">跳过</span><div><span class="k">原因</span>'
                f'{_esc(s.reason)}</div></div></div>')
    err = None
    if gt and s.x is not None:
        err = float(np.hypot(s.x - gt[0], s.y - gt[1]))
    cls = "miss"
    if s.error:
        cls = "miss"
    elif s.x is None:
        cls = "nullans"
    elif err is None or err < max(s.cell_px, 8) * 1.5:
        cls = "hit"
    img = f'<img src="{_b64(s.view_png)}" loading="lazy">' if s.view_png else \
          '<div style="color:#8b8f96">（没有留图，跑的时候没开 trace）</div>'
    rows = [
        f'<div><span class="k">类型</span>{_esc(s.kind)}</div>',
        f'<div><span class="k">网格</span>{s.grid[0]}×{s.grid[1]}　每格 {s.cell_px:.1f} ROI-px</div>',
        f'<div><span class="k">裁切中心</span>{_esc(None if not s.center else (round(s.center[0],1), round(s.center[1],1)))}　边长 {s.size:g}</div>',
        f'<div><span class="k">模型答</span><b>{_esc(s.cell)}</b>　conf={s.conf:.2f}'
        + ("　blurred" if s.blurred else "")
        + (f'　review { _esc(s.first_cell)}→{_esc(s.review_cell)}' if getattr(s, "review_changed", False) else "")
        + "</div>",
        f'<div><span class="k">解出坐标</span>{_esc(None if s.x is None else (round(s.x,1), round(s.y,1)))}</div>',
    ]
    if err is not None:
        rows.append(f'<div><span class="k">对 GT 误差</span>'
                    f'<b style="color:{"#3fb950" if err<20 else "#f85149"}">{err:.1f} px</b></div>')
    rows.append(f'<div><span class="k">供应商</span>{_esc(s.provider)} / {_esc(s.model)}　'
                f'{s.latency:.1f}s{"　(缓存)" if s.cached else ""}</div>')
    if s.reason:
        rows.append(f'<div><span class="k">判定</span>{_esc(s.reason)}</div>')
    if s.error:
        rows.append(f'<div><span class="k">错误</span><span style="color:#f85149">{_esc(s.error)}</span></div>')
    if s.candidates:
        rows.append(f'<details><summary>本地候选 {len(s.candidates)} 个</summary>'
                    f'<pre>{_esc(json.dumps(s.candidates, ensure_ascii=False, indent=1))}</pre></details>')
    body = "".join(rows)
    body += f'<div style="color:#8b8f96;font-size:11px;margin-top:7px">模型原始回答</div>' \
            f'<pre>{_esc(s.answer)}</pre>'
    if s.prompt:
        body += f'<details><summary>展开提示词（{len(s.prompt)} 字）</summary><pre>{_esc(s.prompt)}</pre></details>'
    return (f'<div class="stage {cls}"><div>{img}</div>'
            f'<div class="meta"><b>{_esc(s.stage)}</b>{body}</div></div>')


def _verify_html(r: FrameRun, gt: Optional[Tuple[float, float]]) -> str:
    """逐帧列出本地核验的五项证据。

    门限（`verify.gate`）是这套流程唯一的收敛旋钮，而它只能靠看**证据**来调：
    光看「弃了 12 帧」没法判断该松还是该紧，得看清那 12 帧是哪一项拖下去的。
    """
    ev = getattr(r, "evidence", None)
    if not ev:
        return ""
    items = []
    for key, name in (("zone_score", "在场内"), ("dog", "DoG 亮斑"), ("rgb", "RGB 球感"),
                      ("kine", "轨迹一致"), ("chain", "级间一致")):
        v = ev.get(key)
        if v is None:
            items.append(f'<span class="badge b-dim">{name} —</span>')
            continue
        cls = "b-ok" if v >= 0.6 else ("b-warn" if v >= 0.25 else "b-bad")
        items.append(f'<span class="badge {cls}">{name} {float(v):.2f}</span>')
    head = (f'核验分 <b>{ev.get("score")}</b>　zone={_esc(ev.get("zone"))}　'
            f'final_conf <b>{r.final_conf:.2f}</b>')
    body = f'<div style="margin:4px 0">{"".join(items)}</div>'
    if r.drop_reason:
        body += f'<div style="color:#f85149">{_esc(r.drop_reason)}</div>'
    if r.rejected:
        rx, ry, rc = r.rejected
        de = f"，离 GT {np.hypot(rx - gt[0], ry - gt[1]):.1f}px" if gt else ""
        body += (f'<div style="color:#8b8f96">被弃掉的点 ({rx:.1f}, {ry:.1f}) '
                 f'model_conf={rc:.2f}{de}</div>')
    detail = ev.get("detail") or {}
    body += (f'<details><summary>证据明细</summary>'
             f'<pre>{_esc(json.dumps(detail, ensure_ascii=False, indent=1))}</pre></details>')
    return (f'<div class="stage" style="grid-template-columns:1fr">'
            f'<div class="meta"><b>本地核验</b><div>{head}</div>{body}</div></div>')


def build_html(packet_id: str, runs: Sequence[FrameRun], pts, stats,
               gt: Dict[int, Tuple[float, float]], size: Tuple[int, int],
               wf_name: str, extra: Optional[dict] = None) -> str:
    summ = summarize(runs)
    errs = [float(np.hypot(r.x - gt[r.frame_idx][0], r.y - gt[r.frame_idx][1]))
            for r in runs if r.x is not None and r.frame_idx in gt]
    head = [
        f"帧 {summ['n_frames']}", f"答出 {summ['n_found']}",
        f"调用 {summ['n_calls']}（{summ['calls_per_frame']:.2f}/帧）",
        f"{summ['sec_per_frame']:.1f} s/帧", f"平均置信 {summ['mean_conf']:.2f}",
    ]
    if errs:
        head.append(f"对 GT 中位误差 <b>{np.median(errs):.1f} px</b>")
        head.append(f"&lt;20px {sum(e<20 for e in errs)/len(runs):.0%}")

    parts = [f'<!doctype html><meta charset="utf-8"><title>{_esc(packet_id)} 追踪报告</title>'
             f"<style>{CSS}</style><script>{JS}</script>",
             f"<h1>{_esc(packet_id)}　<span style='font-size:13px;color:#8b8f96'>工作流 {_esc(wf_name)}</span></h1>",
             f'<div class="sub">{"　·　".join(head)}　·　生成于 {time.strftime("%Y-%m-%d %H:%M")}</div>']

    parts.append("<h2>轨迹</h2>")
    parts.append(_traj_svg(runs, pts, gt, size))
    parts.append(f'<div class="sub">离群剔除 {stats.n_outlier}　插值 {stats.n_interp}　'
                 f'仍缺 {stats.n_missing}　最长空洞 {stats.max_gap}</div>')

    parts.append("<h2>各阶段</h2><table><tr><th class='l'>阶段</th><th>跑过</th><th>跳过</th>"
                 "<th>答出</th><th>说找不到</th><th>失败</th><th>每格px</th><th>中位误差</th><th>中位延迟</th></tr>")
    order, agg = [], {}
    for r in runs:
        for s in r.stages:
            if s.stage not in agg:
                agg[s.stage] = {"ran": 0, "skip": 0, "ans": 0, "null": 0, "fail": 0, "e": [], "l": [], "cell": s.cell_px}
                order.append(s.stage)
            a = agg[s.stage]
            if s.skipped:
                a["skip"] += 1
                continue
            a["ran"] += 1
            a["l"].append(s.latency)
            if s.error:
                a["fail"] += 1
            elif s.x is None:
                a["null"] += 1
            else:
                a["ans"] += 1
                if s.cell_px:
                    a["cell"] = s.cell_px
                if r.frame_idx in gt:
                    a["e"].append(float(np.hypot(s.x - gt[r.frame_idx][0], s.y - gt[r.frame_idx][1])))
    for k in order:
        a = agg[k]
        parts.append(f"<tr><td class='l'>{_esc(k)}</td><td>{a['ran']}</td><td>{a['skip']}</td>"
                     f"<td>{a['ans']}</td><td>{a['null']}</td><td>{a['fail']}</td>"
                     f"<td>{a['cell']:.1f}</td>"
                     f"<td>{f'{np.median(a[chr(101)]):.1f}' if a['e'] else '-'}</td>"
                     f"<td>{f'{np.median(a[chr(108)]):.2f}s' if a['l'] else '-'}</td></tr>")
    parts.append("</table>")

    parts.append('<h2>逐帧</h2><div class="ctl">'
                 '<button onclick="all(true)">全部展开</button>'
                 '<button onclick="all(false)">全部收起</button>'
                 '<button onclick="onlyBad()">只看有问题的</button></div>')
    pt_by = {p.frame_idx: p for p in pts}
    for r in runs:
        g = gt.get(r.frame_idx)
        e = float(np.hypot(r.x - g[0], r.y - g[1])) if (g and r.x is not None) else None
        p = pt_by.get(r.frame_idx)
        bad = (r.x is None) or (e is not None and e >= 20) or (p is not None and (p.outlier or p.interp))
        tags = []
        if r.x is None:
            tags.append('<span class="badge b-bad">没答出</span>')
        if e is not None:
            tags.append(f'<span class="badge {"b-ok" if e<20 else "b-bad"}">{e:.1f} px</span>')
        if p is not None and p.outlier:
            tags.append('<span class="badge b-bad">离群剔除</span>')
        if p is not None and p.interp:
            tags.append('<span class="badge b-warn">插值</span>')
        if r.conf:
            tags.append(f'<span class="badge b-dim">conf {r.conf:.2f}</span>')
        if getattr(r, "verify_score", None) is not None:
            vs = float(r.verify_score)
            tags.append(f'<span class="badge {"b-ok" if vs >= 0.6 else "b-warn"}">'
                        f'核验 {vs:.2f} → {r.final_conf:.2f}</span>')
        if getattr(r, "drop_reason", ""):
            tags.append('<span class="badge b-bad">核验弃帧</span>')
        tags.append(f'<span class="badge b-dim">{r.n_calls} 次调用 {r.elapsed:.1f}s</span>')
        tags.append(f'<span class="badge b-dim">{" → ".join(r.stage_names) or "无"}</span>')
        parts.append(
            f'<div class="frame{" open" if bad else ""}" data-bad="{int(bad)}">'
            f'<div class="fhead" onclick="tog(this.parentNode)">'
            f'<span class="id">f{r.frame_idx}</span>{"".join(tags)}</div>'
            f'<div class="body">{_verify_html(r, g)}'
            f'{"".join(_stage_html(s, g) for s in r.stages)}</div></div>'
        )
    if extra:
        parts.append(f"<h2>其它</h2><pre>{_esc(json.dumps(extra, ensure_ascii=False, indent=1))}</pre>")
    return "\n".join(parts)


def inspect_packet(
    packet_id: str,
    work: Path = WORK,
    gt_dir: Path = GT,
    workflow: str = "erp_ball_v2",
    frames: int = 12,
    start: int = 0,
    seed_gt: int = 0,
    out: Optional[Path] = None,
    pool: Optional[VlmPool] = None,
    provider: Optional[str] = None,
    on_frame=None,
) -> Path:
    prep = load_prepared(work, packet_id)
    gt: Dict[int, Tuple[float, float]] = {}
    for f in sorted(gt_dir.glob(f"{packet_id}__*.json")):
        d = json.loads(f.read_text())
        if d.get("annotator") == "smoketest":
            continue
        for r in d.get("labels", []):
            if r.get("visible") and r.get("pano_x") is not None:
                gt[int(r["frame_idx"])] = pano_to_roi(r["pano_x"], r["pano_y"], prep.roi, prep.pano_w)

    pool = pool or VlmPool()
    if provider:
        # 整包只用这一家，不走角色降级——对比「这一家到底标成什么样」才有意义。
        pool.roles = {k: [provider] for k in (pool.roles or {"primary": [provider]})}
    wf = Workflow.load(workflow, pool)
    targets = prep.targets[start : start + frames]
    seed = None
    if seed_gt:
        seed = [(f, gt[f][0], gt[f][1]) for f in targets[:seed_gt] if f in gt]
        targets = targets[seed_gt:]
    runs = wf.run_packet(prep, frames=targets, trace=True, seed=seed, on_frame=on_frame)
    pts, stats = T.process(to_obs(runs), fps=float(prep.meta.fps) / max(int(prep.meta.step or 1), 1))

    outp = Path(out or (prep.root / f"inspect_{packet_id}.html"))
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(build_html(packet_id, runs, pts, stats, gt, prep.size, wf.name,
                               extra={"seed": seed, "providers": pool.available()}),
                    encoding="utf-8")
    return outp


def main() -> None:
    ap = argparse.ArgumentParser(description="单包 HTML 追踪报告")
    ap.add_argument("--packet", required=True)
    ap.add_argument("--work", type=Path, default=WORK)
    ap.add_argument("--gt", type=Path, default=GT)
    ap.add_argument("--workflow", default="erp_ball_v2")
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--seed-gt", type=int, default=0)
    ap.add_argument("--provider", default=None, help="只走这一家供应商，例如 glm53_flash")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    t0 = time.time()
    n = [0]

    def tick(fr):
        n[0] += 1
        print(f"\r  f{fr.frame_idx}  {n[0]}  found={fr.found}  {fr.elapsed:.1f}s", end="", flush=True)

    p = inspect_packet(a.packet, a.work, a.gt, a.workflow, a.frames, a.start, a.seed_gt, a.out,
                       provider=a.provider, on_frame=tick)
    print(f"\n{p}  ({p.stat().st_size/1e6:.1f} MB, {time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
