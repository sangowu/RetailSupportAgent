# 本地运行与 AWS 按需演示

状态：运行入口和基础设施均未实现。以下是必须实现并验证的操作契约，不能当作当前可用命令。

## 本地一键启停

| 规划脚本 | 行为 |
|---|---|
| `scripts/start-local.ps1` | 检查依赖与配置，启动当前阶段需要的组件，等待健康，打印本地网址 |
| `scripts/stop-local.ps1` | 停止本项目服务，默认保留数据与实验结果 |
| `scripts/reset-demo.ps1` | 显式重置指定模拟范围，不删除基线或其他账户数据 |

M0 不要求 PostgreSQL/Cognito/AWS 才能运行；后续通过明确的本地配置启用持久化。模型 Key 与 Langfuse 配置从忽略版本控制的环境读取。首次说明包括 Python/uv/Docker 要求、配置模板、凭证设置、模拟身份、三条演示流程和排错方法。

Windows 脚本只控制已确认属于本项目的进程/容器，不按模糊名称终止全局 Python。后台进程隐藏窗口；启动失败提供日志位置和清理结果。

## AWS 技术目标

区域初定 `eu-west-1`。容器运行在 ECS Fargate，镜像在 ECR；应用数据和 checkpoint 在 RDS PostgreSQL；Secrets Manager 管密钥；CloudWatch 管运行日志；S3 管基线/归档；GitHub Actions 用 OIDC 临时权限执行 Terraform。

CPU、内存、并发与 DB 规格在部署前基于本地测试确定。AWS 迁移单独建立性能环境参照，不能把跨地域模型延迟误算成代码退步。

## 独立基础层与演示层

| 层 | 资源与规则 |
|---|---|
| 基础层 | ECR、S3 及状态文件、密钥、Cognito、部署 IAM/OIDC、必要的稳定入口；长期保留且限制权限 |
| 演示层 | ECS 服务、ALB、RDS、临时网络；按需创建与删除 |

两套 Terraform 状态；显式输出依赖关系。状态存储开启版本保护、加密、访问控制和受支持的锁机制，不能被演示层 destroy 管理。不同启停操作使用同一部署并发锁，不能同时 apply/destroy。

保留基础层不代表零费用。镜像、对象、密钥等存在存储/服务收费，关闭后列出残留资源；不承诺账单立刻归零。预算金额暂不设，但记录运行时长、残留项和模型实验用量。

## 免费地址与 HTTPS

用户选择免费地址，优先验证稳定的 CloudFront 默认 `*.cloudfront.net` 地址，无需购买域名或单独 Route 53 区域。它只是候选架构，不代表当前已配置。

部署前必须验证：

- CloudFront 计划/计费方式适合动态 API、Cookie/Authorization 转发、所有必要方法、禁用个人数据缓存、SSE/流式与 Gradio 路径。
- Cognito callback/logout 地址和 FastAPI 外部 URL 正确；代理 headers 仅信任可信代理。
- CloudFront 证书覆盖浏览器到边缘，不自动解决边缘到 ALB 的加密。ALB 默认域名不自带匹配的公有证书。
- 如果采用 VPC origin，应验证可用性、协议/流式限制和费用；不能默认为免费套餐支持。
- 若免费入口与当前 ALB 架构无法提供所需保护，应在 M6 提交入口方案调整及测试，不静默退回公网 HTTP 传输凭证。
- 原始后端不能绕过登录和资源授权直接访问；免费地址不意味着允许公开注册。

因此 ACM/Route 53 不作为必然要创建的资源；是否需要取决于经验证的源站方案。不要为展示免费网址而引入未经验证的安全和费用假设。

## AWS Start Demo

GitHub Actions 手动入口，规划参数为已通过门禁的 release/image digest、演示时长标记、环境标识。操作顺序：

1. 验证基础层、OIDC、密钥、允许访问账户和发布 manifest。
2. 只允许已通过相应门禁的镜像；不在现场重新调参或跑全量模型评测。
3. Terraform plan 后 apply 演示层，记录实际资源清单。
4. 执行数据库迁移和固定模拟数据初始化；已存在环境再次启动不得重置数据。
5. 部署应用、更新入口，验证 HTTPS、登录、授权、流式和无副作用 smoke 测试。
6. 打印网址、版本、状态与关闭入口；失败保留诊断并清理本次临时资源，不能误删旧环境。

一键不是即时可用。创建 RDS/网络等需要等待，M6 实测记录冷启动与重启时间；面试前预留准备时间，本地版作为备用。

## AWS Stop Demo

1. 阻止新任务，等待正在执行的操作完成或进入可归档状态。
2. 导出演示审计、必要状态与运行 manifest；上传 S3 并验证校验和/可读取性。
3. 归档失败时停止删除流程并报告，不能丢掉唯一副本。
4. 对专属演示层执行 Terraform destroy，包括 RDS、ALB、任务与临时收费网络资源。
5. 查询资源清单核实清理结果，打印基础层保留项及仍可能计费资源。

关闭演示是结束该演示环境，之后默认以固定模拟数据重新开始；它不同于同一环境中服务重启恢复任务。不得只暂停 RDS 作为长期关机策略：RDS 会在停止 7 天后自动启动，且停止期间仍有存储费；ALB 删除后才停止其资源收费。

源码、镜像、基线与归档不得被演示清理一起删除。镜像清理保留初版基线、当前与必要回滚版本。

## 首次配置和发布验收

首次需要配置 AWS 账户/区域、OIDC 与最小部署权限、S3 状态后端、模拟账户、Secrets Manager 中的密钥、Langfuse 项目。Cognito 用户由受控流程创建，关闭公开注册。

真实部署之前完成本地检查和具体 Terraform plan；Git 发布和付费资源创建按当时明确授权执行。独立测试回滚、迁移失败、apply 部分失败、销毁中断与归档失败。Terraform 管资源，Actions 串联流程，Python 管迁移/数据操作。

## 官方参考（核验于本次讨论，实施时复查）

- [GitHub 手动 workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-workflow)
- [GitHub OIDC 到 AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [RDS 临时停止](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StopInstance.html)
- [删除 ALB](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-delete.html)
- [CloudFront 默认域名和证书](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesGeneral.html)
- [CloudFront 价格与计划](https://aws.amazon.com/cloudfront/pricing/)
