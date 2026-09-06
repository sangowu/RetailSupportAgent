# RetailSupportAgent

零售客服 Agent 面试展示项目：处理订单查询、取消订单、退货申请，以可复现评测证明业务正确性、可靠性和成本表现。

**当前状态：M0.1–M0.3 已完成；M0.4 评测链路已打通并冒烟通过，全量选型实验待预算授权。
五个候选模型的成绩一个都还没有，尚未选定模型、尚未接入 Langfuse、尚未创建 AWS 资源。**

## 阅读顺序

1. [项目目标与已确定选择](PROJECT_CONTEXT.md)
2. [M0.1 协议与环境固定报告](docs/M0.1_PROTOCOL_FREEZE.md)
3. [M0.2 模型能力核验报告](docs/M0.2_MODEL_CAPABILITY.md)
4. [M0.3 最小业务闭环报告](docs/M0.3_BUSINESS_LOOP.md)
5. [M0.4 选型实验方案与成本估算](docs/M0.4_EXPERIMENT_PLAN.md)（当前进度）
6. [MVP 与分阶段开发计划](docs/DEVELOPMENT_PLAN.md)
7. [模型能力核验与选型实验](docs/MODEL_EXPERIMENTS.md)
8. [基线、指标与 CI/CD 门禁](docs/EVALUATION_AND_GATES.md)
9. [架构、数据与权限](docs/ARCHITECTURE_AND_ACCESS.md)
10. [本地和 AWS 一键启停设计](docs/OPERATIONS.md)
11. [每步开发报告模板](docs/CHANGE_REPORT_TEMPLATE.md)

开发人员或编码 Agent 先阅读 [AGENTS.md](AGENTS.md)。

## 当前允许推进的范围

先完成 M0 的评测协议、最小业务流程、模型与思考参数实验，再冻结 MVP 基线。后续按单一改动主题推进，每一步报告相对 MVP 和上一通过版本的变化。完整路线图不是一次性实现全部功能的指令。

M0.1–M0.3 已实现并验证。M0.4 的评测链路已打通（2 题冒烟通过），
全量选型实验估算 400–700 元，**待预算授权后分阶段执行**。
最终模型由实验决定，现在不预设胜者，也没有任何成绩可引用。

## 本地开发（M0.1 可用）

```bash
uv sync --group bench                        # 含上游 τ²-bench 包（跑领域环境需要）
uv run python scripts/fetch_tau2.py          # 按固定 commit 拉取数据并校验 SHA256
uv run ruff check . && uv run mypy && uv run pytest
```

需要 `DASHSCOPE_API_KEY` 且**会产生真实费用**的命令：

```bash
uv run python scripts/probe_models.py --dry-run   # 模型能力探针（先看将发多少请求）
uv run python scripts/demo_flows.py               # 三类业务 + 越权拒绝的演示流程
uv run python scripts/run_eval.py --pilot         # τ²-bench 评测冒烟（2 题）
uv run python scripts/analyze_eval.py --project 74  # 用量与成本推算
```

本地服务（仅绑定 127.0.0.1，固定模拟身份，无公开注册）：

```bash
uv run uvicorn retail_support_agent.api:create_app --factory --host 127.0.0.1 --port 8000
uv run python -m retail_support_agent.ui
```

密钥从 `.env` 读取（见 `.env.example`），`.env` 已被 gitignore，禁止提交。

## 一键启动入口（规划中，尚不存在）

| 环境 | 启动 | 关闭 |
|---|---|---|
| 本地 | `scripts/start-local.ps1` | `scripts/stop-local.ps1` |
| AWS | GitHub Actions：`Start Demo` | GitHub Actions：`Stop Demo` |

这些入口将在对应阶段实现。现在没有可执行的启动命令，也没有在线演示地址。首次配置、依赖、模拟账户和排错说明随实际实现补齐，详见 [启停设计](docs/OPERATIONS.md)。

## 基准归属

使用 [Sierra τ²-bench Retail](https://github.com/sierra-research/tau2-bench)，已固定到 `v1.0.1` / commit `fc0055dc4e0a316c3f83133267fbd6faaa770992`（MIT，Copyright (c) 2025 Sierra Research）；逐文件 SHA256 记录在 `evaluation/tau2_manifest.json`。保留上游归属；不得把上游数据、工具或评分器当作原创实现。标准基准成绩与自建应用权限、审批、恢复测试分开报告。
