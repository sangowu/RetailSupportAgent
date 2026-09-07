# RetailSupportAgent

零售客服 Agent 面试展示项目：处理订单查询、取消订单、退货申请，以可复现评测证明业务正确性、可靠性和成本表现。

**当前状态：M0.1–M0.5 已完成。选定 `deepseek-v4-pro`，在 40 题独立
全集 114 题上取得 **0.8509**（97/114，标准 τ²-bench Retail 口径），
本项目声明的三类业务 42 题子集上 0.9286（39/42），每题 0.0855 元。
成本记账与 Langfuse 观测已接入
（成本口径以 `analyze_eval.py` 为准，原因见 M0.5 文档）。尚未冻结 `baseline-v1`、
已冻结 `baseline-v2`。尚未创建 AWS 资源。**

选型是**成本决策**，不是质量比较：其余候选没有在同一评测栈下测量过
（成本差 4.8 倍，不在一个量级，已决定不补测）。此前 A 轮的跨模型数字因判官
不一致而作废，详见 [§9.9.1](docs/M0.4_EXPERIMENT_PLAN.md)。

## 阅读顺序

1. [项目目标与已确定选择](PROJECT_CONTEXT.md)
2. [M0.1 协议与环境固定报告](docs/M0.1_PROTOCOL_FREEZE.md)
3. [M0.2 模型能力核验报告](docs/M0.2_MODEL_CAPABILITY.md)
4. [M0.3 最小业务闭环报告](docs/M0.3_BUSINESS_LOOP.md)
5. [M0.4 选型实验方案与成本估算](docs/M0.4_EXPERIMENT_PLAN.md)
6. [M0.5（一）成本记账与可观测接入](docs/M0.5_OBSERVABILITY.md)
7. [M0.5（二）本地一键启停与演示流程](docs/M0.5_LOCAL_DEMO.md)
8. [M0.5（三）冻结基线](docs/M0.5_BASELINE.md)（当前进度）
9. [MVP 与分阶段开发计划](docs/DEVELOPMENT_PLAN.md)
7. [模型能力核验与选型实验](docs/MODEL_EXPERIMENTS.md)
8. [基线、指标与 CI/CD 门禁](docs/EVALUATION_AND_GATES.md)
9. [架构、数据与权限](docs/ARCHITECTURE_AND_ACCESS.md)
10. [本地和 AWS 一键启停设计](docs/OPERATIONS.md)
11. [每步开发报告模板](docs/CHANGE_REPORT_TEMPLATE.md)

开发人员或编码 Agent 先阅读 [AGENTS.md](AGENTS.md)。

## 当前允许推进的范围

先完成 M0 的评测协议、最小业务流程、模型与思考参数实验，再冻结 MVP 基线。后续按单一改动主题推进，每一步报告相对 MVP 和上一通过版本的变化。完整路线图不是一次性实现全部功能的指令。

M0.1–M0.4 已实现并验证。下一步是 M0.5 冻结 MVP：建立 `baseline-v1` 归档与校验和。

可引用的成绩只有一个：`deepseek-v4-pro` 在 holdout 40 题上 0.875，
评测栈为「模拟用户 `glm-5.3-flash` + 判官 `qwen3.7-flash` (JSON Schema)」。
**换掉评测栈里任何一个角色，这个数字就不再可比**——2026-09-07 的实测是
同一批题从 0.600 变成 0.875，差别全在评测栈。

## 本地开发（M0.1 可用）

```bash
uv sync --group bench                        # 含上游 τ²-bench 包（跑领域环境需要）
uv run python scripts/fetch_tau2.py          # 按固定 commit 拉取数据并校验 SHA256
uv run ruff check . && uv run mypy && uv run pytest
uv run python scripts/freeze_baseline.py --verify   # 校验 baseline-v1 未被改动
```

需要 `DASHSCOPE_API_KEY` 且**会产生真实费用**的命令：

```bash
uv run python scripts/probe_models.py --dry-run   # 模型能力探针（先看将发多少请求）
uv run python scripts/demo_flows.py               # 三类业务 + 越权拒绝的演示流程
uv run python scripts/run_eval.py --pilot         # τ²-bench 评测冒烟（2 题）
uv run python scripts/analyze_eval.py --project 74  # 用量与成本推算
```

本地服务（仅绑定 127.0.0.1，固定模拟身份，无公开注册）：

```powershell
pwsh scripts/start-local.ps1     # 检查前提，起接口 :8000 与界面 :7860，等健康后打印网址
pwsh scripts/reset-demo.ps1      # 重置演示会话（不删任何文件）
pwsh scripts/stop-local.ps1      # 只停本项目登记过的进程
```

起服务本身不花钱，只有真的发消息才计费。脚本做了什么、为什么这么做，
见 [M0.5（二）](docs/M0.5_LOCAL_DEMO.md)。

密钥从 `.env` 读取（见 `.env.example`），`.env` 已被 gitignore，禁止提交。

## 一键启动入口

| 环境 | 启动 | 关闭 | 状态 |
|---|---|---|---|
| 本地 | `scripts/start-local.ps1` | `scripts/stop-local.ps1` | 已实现并实跑验证 |
| AWS | GitHub Actions：`Start Demo` | GitHub Actions：`Stop Demo` | 规划中，M6 |

本地脚本只控制**本项目登记过**的进程（按 PID 加启动时刻双重核对），不会去猜哪个
python 属于本项目。没有在线演示地址。设计契约见 [启停设计](docs/OPERATIONS.md)。

## 基准归属

使用 [Sierra τ²-bench Retail](https://github.com/sierra-research/tau2-bench)，已固定到 `v1.0.1` / commit `fc0055dc4e0a316c3f83133267fbd6faaa770992`（MIT，Copyright (c) 2025 Sierra Research）；逐文件 SHA256 记录在 `evaluation/tau2_manifest.json`。保留上游归属；不得把上游数据、工具或评分器当作原创实现。标准基准成绩与自建应用权限、审批、恢复测试分开报告。
