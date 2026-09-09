# 设计要点

## 为什么要单独攒数据

单帧检测器（COCO sports ball、WASB、FootAndBall 等）在这批 8K ERP 全景上不够用：比赛球通常只有 8–30 px，飞行时还有动态模糊，静态单帧人眼都经常找不到。多间隔残差 + DoG 增强后，飞行中的球是一块孤立白斑。信息在时间维度上，模型也必须吃多帧。

## 只标一个球

GT 定义为「比赛中正在使用的那一个球」。场边备用球、球门后的球一律不标。纯几何跟踪曾经被一个完全静止的备用球锁死——时空平滑代价下它反而是最优解。

## 视野跟着球走

标完一帧跳下一帧时，视野中心用最近两个已标点**线性外推**（不是沿用上一帧位置）。step=3 时快球两帧间能跑约 390 px。

## 任务池没有中心库

状态完全由对象存储里的对象是否存在表达：

```
packets/<id>.zip
packets/<id>.meta.json
claims/<id>/<who>.json
results/<id>__<who>.json
produced.jsonl
```

领取是先到先得 + 乐观复核。claim 超过 36 小时未出结果视为放弃。

## 坐标

- 标注端只存全景像素 `(pano_x, pano_y)`
- ROI 可以跨 ERP 接缝：`pano_x = (roi[0] + local_x) % pano_w`
- 球场 `field_u/v` 只在 `collect_results.py` 里用 FootballDet360 的权威几何补齐

## 已验证的事实

| 结论 | 说明 |
|---|---|
| CRF 16 不损球的可辨识度 | 编码器降噪还略抑制伪 DoG 峰值 |
| 约 46/100 路视频球场跨接缝 | ROI 必须用最小覆盖圆弧，不能用 min/max 包围盒 |
| 增强 σ 不能调大 | 球员会被糊成大白块；快球主要靠拨帧 |
| 帧号必须可反推 | `ffmpeg -copyts -ss T` + `stream.start_time` |
