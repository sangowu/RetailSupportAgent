# RetailSupportAgent

零售客服 Agent 面试展示项目：处理订单查询、取消订单、退货申请，以可复现评测证明业务正确性、可靠性和成本表现。

**当前状态：M0.1 已完成（协议与环境固定、静态检查、CI 骨架）。
尚未调用任何模型、尚未实现业务闭环、尚未创建 AWS 资源。**

## 阅读顺序

1. [项目目标与已确定选择](PROJECT_CONTEXT.md)
2. [M0.1 协议与环境固定报告](docs/M0.1_PROTOCOL_FREEZE.md)（当前进度）
3. [MVP 与分阶段开发计划](docs/DEVELOPMENT_PLAN.md)
4. [模型能力核验与选型实验](docs/MODEL_EXPERIMENTS.md)
5. [基线、指标与 CI/CD 门禁](docs/EVALUATION_AND_GATES.md)
6. [架构、数据与权限](docs/ARCHITECTURE_AND_ACCESS.md)
7. [本地和 AWS 一键启停设计](docs/OPERATIONS.md)
8. [每步开发报告模板](docs/CHANGE_REPORT_TEMPLATE.md)

开发人员或编码 Agent 先阅读 [AGENTS.md](AGENTS.md)。

## 当前允许推进的范围

先完成 M0 的评测协议、最小业务流程、模型与思考参数实验，再冻结 MVP 基线。后续按单一改动主题推进，每一步报告相对 MVP 和上一通过版本的变化。完整路线图不是一次性实现全部功能的指令。

M0.1 已实现并本地验证，详见 [M0.1 报告](docs/M0.1_PROTOCOL_FREEZE.md)。
下一步是 M0.2（核验 5 个候选模型的可用性与思考参数），需要真实 API 调用与计费授权。

## 本地开发（M0.1 可用）

```bash
uv sync --only-group dev                     # 安装静态检查与测试依赖
uv run python scripts/fetch_tau2.py          # 按固定 commit 拉取 τ²-bench 并校验 SHA256
uv run ruff check . && uv run mypy && uv run pytest
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
