# 深度登记 多时区应用日志小时桶重算

        - 规模：中型
        - 核心规则：事件先归一到 UTC，DST 重叠时保留 fold；小时桶右端开，水位只能前进且迟到事件仍进入修订。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 日志采信 | 运行样例并复核证据 | `/api/logs/import` / `/logs` | `app/modules/m1_logs/core.py` `app/modules/m1_logs/service.py` | `tests/test_m1_logs.py` |
| M2 时间解析 | 运行样例并复核证据 | `/api/time/normalize` / `/time` | `app/modules/m2_time/core.py` `app/modules/m2_time/service.py` | `tests/test_m2_time.py` |
| M3 事件去重 | 运行样例并复核证据 | `/api/events/dedupe` / `/events` | `app/modules/m3_events/core.py` `app/modules/m3_events/service.py` | `tests/test_m3_events.py` |
| M4 小时分桶 | 运行样例并复核证据 | `/api/buckets/rebuild` / `/buckets` | `app/modules/m4_buckets/core.py` `app/modules/m4_buckets/service.py` | `tests/test_m4_buckets.py` |
| M5 迟到修订 | 运行样例并复核证据 | `/api/watermark/advance` / `/watermark` | `app/modules/m5_watermark/core.py` `app/modules/m5_watermark/service.py` | `tests/test_m5_watermark.py` |
| M6 批次对账 | 运行样例并复核证据 | `/api/export/buckets.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |

## 持久化账本（交接核对）

| 能力 | 入口 | 核心文件 | 测试 |
|---|---|---|---|
| 批次幂等摄入与当前桶 | `/api/buckets/ingest` `/api/buckets/head` | `app/ledger/pipeline.py` `app/store.py` | `tests/test_ledger.py` `tests/test_handoff_api.py` |
| 迟到修订挪桶、水位单调 | `/api/watermark/revise` `/api/watermark/state` | `app/ledger/pipeline.py` `app/ledger/timeutil.py` | 同上 |
| 交接签收冻结与版本导出 | `/api/handoffs/sign-off` `/api/handoffs` `/api/handoffs/export` | `app/ledger/pipeline.py` `app/store.py` | 同上 |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-03 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_logs/core.py` | `tests/test_m1_logs.py::test_trap_1` |
| TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_time/core.py` | `tests/test_m2_time.py::test_trap_1` |
| TRAP-07 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_events/core.py` | `tests/test_m3_events.py::test_trap_1` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_buckets/core.py` | `tests/test_m4_buckets.py::test_trap_1` |
| TRAP-11 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_watermark/core.py` | `tests/test_m5_watermark.py::test_trap_1` |
| TRAP-12 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-13 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_logs/core.py` | `tests/test_m1_logs.py::test_trap_2` |
| TRAP-14 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_time/core.py` | `tests/test_m2_time.py::test_trap_2` |
| TRAP-16 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_events/core.py` | `tests/test_m3_events.py::test_trap_2` |
| TRAP-23 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_buckets/core.py` | `tests/test_m4_buckets.py::test_trap_2` |
| TRAP-24 | DST 回拨折叠小时同形墙上时间 | 按字符串分桶会把两次 01:30 合并 | 保留 fold 归一 UTC，落入两个 UTC 小时桶 | `app/ledger/timeutil.py` | `tests/test_ledger.py::test_fold_distinguishes_fall_back_hours` |
| TRAP-25 | 迟到修订直接改旧桶 | 已签收交接数字被改写、对不上签收记录 | 签收快照冻结不可变，修订只改当前桶并追加修正流水 | `app/ledger/pipeline.py` | `tests/test_ledger.py::test_signed_numbers_frozen_while_head_is_corrected` |
| TRAP-26 | 迟到事件把水位拉回 | 下游按旧水位重复窗口计算 | 水位取 max 只前进，迟到事件仍进修订 | `app/ledger/timeutil.py` `app/ledger/pipeline.py` | `tests/test_ledger.py::test_watermark_not_regressed_by_late_revision` |
| TRAP-27 | 同批重跑重复加减桶 | 桶计数翻倍、修正流水污染 | 输入摘要派生 run_id 做幂等登记，重跑原样返回 | `app/ledger/pipeline.py` | `tests/test_ledger.py::test_rerun_same_batch_is_idempotent` |
| TRAP-28 | 挪桶只加新桶不减旧桶 | 总量虚增、守恒破裂 | 旧桶 -1/-错误位、新桶 +1/+新错误位，事务内守恒校验 | `app/ledger/pipeline.py` `app/store.py` | `tests/test_ledger.py::test_total_conservation_across_revisions` |

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。
        - 桶合计始终等于权威事件表行数；挪桶/改状态在单事务内完成并校验守恒，破裂即回滚。
        - 签收版本不可变：版本导出的桶行与合计必须等于签收当时的当前桶。
        - 水位单调：任意修订后 wm_new >= wm_old。

        ## 已知不覆盖

        - 生产系统连接、身份认证和外部密钥不在演示范围内。
