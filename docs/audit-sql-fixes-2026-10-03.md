# SQL 安全校验与瞬时故障重试修复记录

日期：2026-10-03

> 后续进展：第三批已升级生产和开发漏洞依赖，579 个测试通过，两份锁文件扫描通过。详见 [依赖修复报告](audit-dependency-fix-2026-10-03.md)。下文保留第二批交付时的检查结果与待办快照。

基线：`c16e493` 及第一批本地可靠性修复。当前修改未提交、未推送，未重启运行中的服务。

## 1. 本轮结论

已在本地修复审计复现的无分号多语句放行、合法标识符误拒，以及 ODBC 瞬时故障不重试的问题。
完整自动化测试通过，但依赖漏洞未关闭，真实 SQL Server、SMTP 和生产部署验证尚未完成。

## 2. SQL 安全校验

- 使用锁定版本 SQLGlot 30.21.0 的 T-SQL 分词器和语法树，替换手写字符扫描。
- 保持单条 SELECT/WITH 查询及只允许一个尾部分号的策略；支持只读集合查询、TOP、引用标识符和嵌套注释。
- 拒绝未加分隔符的第二条语句，例如 SELECT 后的 GRANT、DENY、REVOKE、WAITFOR、DBCC 和另一个 SELECT。
- 拒绝修改数据、DDL、SELECT INTO、NEXT VALUE FOR 以及 OPENQUERY、OPENROWSET、OPENDATASOURCE 等结构。
- WITH 子查询必须是 SELECT 查询结构；不支持的 Command 回退直接拒绝，不打印原始 SQL。
- 语句关键字用作标识符时需要引用；合法的 [update]、[delete]、[into] 和 [semi;colon] 不再误报。
- SQL 适配器的 query 和 validate_syntax 在连接前再次执行安全校验，页面检测和预览入口也已验证。
- 不转写待执行 SQL，避免改变原查询语义；服务器解析仍使用 SET PARSEONLY。

本地语法树属于安全筛查，不替代 SQL Server 原生语法检测、对象绑定或权限检查。
SQLGlot 本身是宽容的解析器，不是数据库验证器，未支持的合法 T-SQL 也可能被本策略拒绝。
升级前应逐条复核现有规则，不能因为本地解析成功就认为数据库一定可执行。
参见 [SQLGlot 官方说明](https://sqlglot.com/sqlglot.html)。

数据库账号仍必须使用最小只读权限。查询调用的自定义函数、CLR、跨库访问等能力取决于服务器权限与配置，应用层筛查不能替代数据库权限边界。

## 3. SQLSTATE 重试

- 只从真实 pyodbc 异常的第一个参数提取规范 SQLSTATE，不从错误消息中搜索代码。
- 08 类连接故障、HYT00/HYT01 超时和 40001 事务回滚进入既有重试流程，默认总尝试三次。
- 42000 语法或权限错误、28000 认证错误、IM002 驱动配置错误及未知 ODBC 状态不重试。
- 原始错误类型保留；对外错误继续采用固定摘要，敏感内容不回显。
- 重试耗尽仍只保存一条执行日志，显示已重试次数，不产生邮件日志或调用邮件发送。
- SQLSTATE 仅在本次执行结果中用于分类，不增加数据库字段；原有非 ODBC 错误重试策略不变。
- 缺少 pyodbc 时，SQLSTATE 提取不覆盖原本的依赖错误。

代码含义参考 [Microsoft ODBC SQLSTATE 表](https://learn.microsoft.com/en-us/sql/odbc/reference/appendixes/appendix-a-odbc-error-codes?view=sql-server-ver17)。

## 4. 验证

测试环境：本地 Python 3.12.14，隔离 SQLite，SQL/SMTP 测试替身。

| 检查 | 结果 |
| --- | --- |
| 修复前主要回归用例 | 19 failed、7 passed，复现原缺陷 |
| 修复前适配器防护及不支持 CTE 用例 | 8 failed |
| SQL 校验、SQL 适配器、执行器测试 | 134 passed（随后另补 3 个端到端用例） |
| 页面入口拦截及重试耗尽定点测试 | 3 passed |
| 最终完整 pytest | 573 passed，28.35 秒；本轮新增 38 个用例 |
| app 覆盖率 | 94.17%，达到 93% 门槛 |
| Ruff / pip check / git diff --check | 通过；pip 有本机缓存不可写提示 |
| 生产及开发锁文件 | 新增 sqlglot==30.21.0，其余依赖版本未改变 |
| 漏洞扫描 | 失败；cryptography 49.0.0，PYSEC-2026-3552，修复版本 50.0.0，同一编号被重复列出 |

漏洞扫描命令：`pip-audit -r requirements.lock --strict --no-deps --disable-pip`。
使用完整固定版本锁文件，禁用解析环境的临时 pip 升级；该检查不证明没有未公布漏洞或可利用业务缺陷。

## 5. 部署与剩余事项

- 新版本必须安装更新的 requirements.lock，否则无法导入 SQL 解析器；开发和 CI 使用更新的 requirements-dev.lock。
- SQLGlot 私有 Command 回退钩子已按固定版本实现 fail-closed；今后升级解析器须重跑安全语料回归，不能直接放宽解析错误。
- 生产仍按单 Web、单 Worker、共享单机 SQLite 边界部署，不混用新旧代码。
- 此前的 Cron 和 SMTP 部分拒收修复仍保留，完整回归包含它们的测试。
- 下一批处理 cryptography 依赖升级、漏洞扫描和完整 CI；主分支保护仍需配置。
- 真实 SQL Server 语法、权限、连接中断恢复，以及 SMTP 实际接受结果需要部署环境验证。

本轮不修改真实配置，不访问实际业务库，不发送真实邮件，不进行生产服务重启。
