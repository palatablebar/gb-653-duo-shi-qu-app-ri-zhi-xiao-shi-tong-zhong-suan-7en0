# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号；签收版为 `handoff-v{n}` |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `version` | integer | 否 | 交接签收版本号，只增不复用 |
| `wm_ts` | string | 否 | 水位时间（UTC，ISO8601），只前进不倒退 |
| `hour` | string | 否 | UTC 小时桶键，左闭右开，形如 `2026-11-01T06:00:00Z` |
| `fold` | integer | 否 | DST 折叠小时的次序：0=第一次（夏令时），1=第二次（标准时） |
| `correction_id` | integer | 否 | 修正流水号，追加只增 |
| `kind` | string | 否 | 修正类型：`INSERT`/`LATE_INSERT`/`MOVE`/`RESTATE` |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。

## 账本表（SQLite）

| 表 | 语义 | 可变性 |
|---|---|---|
| `runs` | 输入摘要 → run_id 与结果摘要，兼作批次幂等登记 | 仅 `INSERT OR IGNORE` |
| `events` | 权威事件，每 `request_id` 一行最新采信状态 | 修订时更新，旧值进流水 |
| `corrections` | 修正流水：旧桶/新桶、旧错误位/新错误位、原值/新值、原因 | 只追加 |
| `bucket_head` | 当前小时桶物化结果（值班视角的修正后数字） | 按 delta 增减，归零删行 |
| `watermark` | 单行水位，推进取 `max(old, 观测最大时间-窗口)` | 只前进 |
| `handoffs` | 交接签收快照：版本号、水位、桶行 JSON、合计 | 冻结不可变 |

## 双时态口径

- **当前桶（head）**：权威事件实时物化，迟到修订后值班立即看到修正后的数。
- **签收版（handoff）**：签收瞬间冻结桶行与合计，之后任何修订都不改写旧版，按 `version` 或 `run_id` 原样导出。
- **挪桶**：修订先对旧桶 `(-1, -旧错误位)` 再对新桶 `(+1, +新错误位)`，总量与错误量守恒；同批摘要重跑复用 `run_id`，不重复增减。
