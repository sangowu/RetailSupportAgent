# RetailSupportAgent

零售客服 Agent 面试展示项目：处理订单查询、取消订单、退货申请，以可复现评测证明业务正确性、可靠性和成本表现。

**当前状态：仅完成开发设计文档，尚未实现程序、运行模型实验或创建 AWS 资源。**

## 阅读顺序

1. [项目目标与已确定选择](PROJECT_CONTEXT.md)
2. [MVP 与分阶段开发计划](docs/DEVELOPMENT_PLAN.md)
3. [模型能力核验与选型实验](docs/MODEL_EXPERIMENTS.md)
4. [基线、指标与 CI/CD 门禁](docs/EVALUATION_AND_GATES.md)
5. [架构、数据与权限](docs/ARCHITECTURE_AND_ACCESS.md)
6. [本地和 AWS 一键启停设计](docs/OPERATIONS.md)
7. [每步开发报告模板](docs/CHANGE_REPORT_TEMPLATE.md)

开发人员或编码 Agent 先阅读 [AGENTS.md](AGENTS.md)。

## 当前允许推进的范围

先完成 M0 的评测协议、最小业务流程、模型与思考参数实验，再冻结 MVP 基线。后续按单一改动主题推进，每一步报告相对 MVP 和上一通过版本的变化。完整路线图不是一次性实现全部功能的指令。

本次仅要求落盘文档。未来的代码实现、模型实验、Git 发布及云资源创建分别按当时的任务授权执行。

## 一键启动入口（规划中，尚不存在）

| 环境 | 启动 | 关闭 |
|---|---|---|
| 本地 | `scripts/start-local.ps1` | `scripts/stop-local.ps1` |
| AWS | GitHub Actions：`Start Demo` | GitHub Actions：`Stop Demo` |

这些入口将在对应阶段实现。现在没有可执行的启动命令，也没有在线演示地址。首次配置、依赖、模拟账户和排错说明随实际实现补齐，详见 [启停设计](docs/OPERATIONS.md)。

## 基准归属

计划使用 [Sierra τ²-bench Retail](https://github.com/sierra-research/tau2-bench)。在 M0 固定仓库 commit、许可、数据和任务版本。保留上游归属；不得把上游数据、工具或评分器当作原创实现。标准基准成绩与自建应用权限、审批、恢复测试分开报告。
