# 多时区应用日志小时桶重算

面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

## 业务模块

- M1 日志采信：`/api/logs/import`
- M2 时间解析：`/api/time/normalize`
- M3 事件去重：`/api/events/dedupe`
- M4 小时分桶：`/api/buckets/rebuild`（无状态样例）；持久化账本 `/api/buckets/ingest`、当前桶 `/api/buckets/head`
- M5 迟到修订：`/api/watermark/advance`（无状态样例）；持久化挪桶重算 `/api/watermark/revise`、水位与修正流水 `/api/watermark/state`
- M6 批次对账：`/api/export/buckets.csv`；交接签收 `/api/handoffs/sign-off`、版本清单 `/api/handoffs`、版本导出 `/api/handoffs/export?version=n`（亦支持 `run_id=...`）

## 核心规则

事件先归一到 UTC，DST 重叠时保留 fold；小时桶右端开，水位只能前进且迟到事件仍进入修订。

### 双时态账本（交接核对口径）

- **当前桶**：权威事件实时物化。迟到补正证实归错小时后，先移走旧桶贡献（`-1 / -错误位`）再写入新桶，值班立即看到修正后的数；总量与错误量守恒，桶计数归零则删行。
- **签收快照**：交接签收把当时的水位、桶行、合计冻结为不可变版本（`handoff-v{n}`）。之后的修订不改写旧版，已签收数字按版本号或 `run_id` 原样查回、原样导出。
- **水位单调**：`new = max(old, 观测最大时间 − 窗口)`，迟到事件不回退水位。
- **批次幂等**：`run_id` 由输入摘要派生，同一批重跑复用原 `run_id`，不重复增减；同键冲突与非法时间进隔离并附原因码。
- **DST 回拨**：折叠小时靠 `fold` 区分两个瞬间，归一到 UTC 后落入不同小时桶；春季间隙（不存在的墙上时间）拒绝，原因码 `DST_GAP`。

## 启动

```bash
docker compose up --build
```

浏览器访问 `http://localhost:18212`。运行测试：

```bash
docker compose exec -T app pytest -q
```

## 样例流程

依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

账本持久化流程：`/api/buckets/ingest` 摄入批次 → `/api/watermark/revise` 推进水位 → `/api/handoffs/sign-off` 签收 → 迟到修订挪桶 → 再次签收 → 用 `/api/handoffs/export?version=1` 取回第一版数字。

## 已知不覆盖

不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
