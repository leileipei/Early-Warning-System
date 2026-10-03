# 审计修复发布验证记录

日期：2026-10-03

基线：`c16e493`。修复分支：`codex/audit-reliability-2026-10-03`，目标分支：`main`。

## 1. 验证范围

本批汇总 Cron 时区、SMTP 部分拒收、SQL 安全筛查、SQLSTATE 瞬时故障重试和依赖漏洞修复。
具体内容及历史证据见 [可靠性修复](audit-reliability-fixes-2026-10-03.md)、
[SQL 修复](audit-sql-fixes-2026-10-03.md)、[依赖修复](audit-dependency-fix-2026-10-03.md)。

本记录用于代码交付和发布前验证，不是生产环境验收结论。
未访问真实 SQL Server，未发送真实邮件，未重启现有服务或修改真实配置。

## 2. 本地完整测试

两种 Python 环境均按 requirements-dev.lock 安装依赖。
Python 3.11 使用临时虚拟环境，不替换工作区原有 .venv。

| 环境 | 测试 | 覆盖率 | 耗时 |
| --- | --- | --- | --- |
| macOS / Python 3.11.15 | 579 passed | 94.17% | 28.10 秒 |
| macOS / Python 3.12.14 | 579 passed | 94.17% | 28.31 秒 |

- Ruff 和 git diff --check 通过。
- Python 3.11 的 pip check 通过；依赖安装无破损。
- 提交前重新执行生产和开发锁文件的默认 strict 漏洞扫描，均退出 0，未发现已知漏洞。
- 本机未安装 Python 3.13；该版本及 Linux 平台交由 GitHub CI 验证。

## 3. 真实进程冒烟验证

用临时目录中的空 SQLite、合成密钥及专用测试管理员启动真实 Uvicorn 和 Worker。
临时工作目录没有 .env，Web 仅绑定随机回环端口；不复用运行中的服务。

| 步骤 | 实际结果 |
| --- | --- |
| Web 启动 | /health 返回 HTTP 200，status=ok |
| Worker 未启动 | /health/ready 返回 HTTP 503，worker=missing |
| 登录 | 登录页 HTTP 200，有已渲染的 CSRF 隐藏字段；合成账号登录后 HTTP 303 跳转到仪表盘 |
| 登录后页面 | 仪表盘、配置、规则、日志均 HTTP 200；未出现未渲染的模板插值 |
| 静态资源 | styles.css、app.js 均 HTTP 200，内容非空 |
| 安全响应头 | 登录页 X-Content-Type-Options=nosniff |
| Worker 启动 | /health/ready 返回 HTTP 200，database=ready、worker=ready |
| Worker 停止且心跳过期 | /health/ready 返回 HTTP 503，worker=stale；/health 仍为 HTTP 200 |
| SQLite 检查 | integrity_check=ok；foreign_key_check 无异常行 |
| 空库边界 | 数据源、SMTP、规则、执行日志和邮件日志数量全部为 0 |
| 清理 | 全部测试子进程已停止，临时数据库已删除 |

冒烟脚本首次使用 csrf_token 作为表单字段而失败。
核对模板宏、认证测试及安全中间件后，确认实际字段是 _csrf_token；修正脚本后完整重跑通过。
没有因此修改应用安全逻辑，也没有绕过 CSRF 校验。

本轮是 HTTP 及进程验证，不包含浏览器视觉验收、实际规则调度执行或外部投递验证。

## 4. GitHub 交付

用户已授权提交、推送和 CI 检查。采用独立修复分支和 Pull Request 交付，
不直接修改远端 main，不自动合并，不启用自动合并。

提交仅包含修复代码、相关测试、锁文件、CI 和报告。
两份原有未跟踪的 2026-07-14 计划文档留在工作区，不加入提交。
数据库、.env、临时环境、密钥和真实密码不上传。

代码修复已提交并推送：`c3756350212635c1f752d30d4f2545865efa6374`。
审查入口：[PR #11](https://github.com/leileipei/Early-Warning-System/pull/11)。

| 远端验证 | 记录 | 结果 |
| --- | --- | --- |
| 分支 push CI | [37090980352](https://github.com/leileipei/Early-Warning-System/actions/runs/37090980352) | Python 3.11/3.12/3.13 全部 success |
| PR 合并视图 CI | [37091004468](https://github.com/leileipei/Early-Warning-System/actions/runs/37091004468) | Python 3.11/3.12/3.13 全部 success |

已核对两条流水线 headSha 均对应上述修复提交，各版本任务均完成依赖安装、
pip check、Ruff、完整 pytest、93% 覆盖率门槛，以及生产和开发锁文件扫描。
分支 CI 的三个版本覆盖率均为 94.17%；3.11 和 3.12 日志分别为 579 passed。
PR 在核对时为 OPEN、MERGEABLE、CLEAN，六项检查全部 SUCCESS；未执行合并。

CI 还有非阻断提示：现有 checkout@v4、setup-python@v5 标注的 Node.js 20
被 Runner 强制改为 Node.js 24，以及 ubuntu-latest 后续系统镜像迁移提示。
本批不升级 Actions 主版本或改变 Runner 选择，后续应单独验证并更新 CI 基础配置。

本节记录代码提交的已完成检查。补充本报告的文档提交将触发新一轮 CI，
须按 PR 最新 headRefOid 核对最终检查，不把前一个提交的绿灯当作最新状态。

## 5. 生产验收待办

1. 在目标部署平台检查 ODBC 驱动及系统依赖，使用最小只读 SQL Server 账号验证已有规则。
2. 验证 SQL Server 原生语法检测、查询超时、连接故障及重试耗尽行为。
3. 在受控 SMTP 环境验证全部接受、部分拒收、完全拒收及实际投递；SMTP 接受不等于邮件送达。
4. 备份并保持数据库与原 SECRET_KEY 一致，演练升级、备份恢复、Worker 故障及就绪检查。
5. 审查并合并 PR 后按单 Web、单 Worker、单机 SQLite 边界发布；配置 GitHub 主分支保护。

逐收件人明细、仅对失败收件人重发、多主机高可用不属于本批交付范围。
