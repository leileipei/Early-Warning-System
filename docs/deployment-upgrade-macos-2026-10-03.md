# Mac 本地升级报告

日期：2026-10-03，Asia/Shanghai。范围：将已合并代码更新到当前 Mac 的既有部署，保留账号、配置和业务数据库。本文不包含密码、密钥或真实连接信息。

## 版本与交付

- 实际发布代码：`bc97e766b81df6a97f70a399bc3c0ab5d569b163`，目录 `releases/bc97e76/`。
- 43 个发布文件已逐一与 Git 提交核对，内容一致；生产依赖按 `requirements.lock` 检查，`pip check` 通过。
- 本轮之前的该提交本地回归：583 passed，覆盖率 94.17%；本次部署没有再次运行这套全量回归。
- [该提交 CI](https://github.com/leileipei/Early-Warning-System/actions/runs/37100281882) 成功。
- 升级期间主分支出现合并提交 `e8d139839d0bdcce7286f018e41d3bd89aa96781`，其完整文件树与 `bc97e76` 相同，未引入额外代码差异；[对应 CI](https://github.com/leileipei/Early-Warning-System/actions/runs/37101667227) 成功。
- 后续仅报告类提交不会改变实际部署代码。不能单凭 Git HEAD 或发布目录名称判断运行版本。

## 运行位置与保留数据

根目录为 `$HOME/Library/Application Support/Early Warning System`。

| 位置 | 当前用途 |
| --- | --- |
| releases/bc97e76/ | 本轮验证过的发布代码、模板和静态资源 |
| .venv/ | 既有 Python 3.11.15 生产环境 |
| data/.env | 保留的实际运行配置与密钥 |
| data/early_warning.sqlite3 | 保留的实际运行数据库 |
| logs/web.log、logs/worker.log | 沿用日志，没有清空 |

成对备份位于项目目录 `.venv/ews-runtime/backups/20261003-135832-bc97e76/`。
备份来自上述实际运行 data 目录，数据库通过 SQLite backup API 创建，不以 Documents 中的旧数据库覆盖运行库。

升级后核对：管理员、数据源、SMTP、规则四表与备份逐行一致，分别为 1、2、1、0 条；`.env` 字节一致，管理员密码哈希和会话版本未变，原有 3 项加密凭据可用原密钥解密。数据库完整性检查通过，外键检查无异常行。

data、logs 目录权限为 700，配置、数据库和日志权限为 600。没有重置密码，没有执行业务 SQL 或发送测试邮件。

## 服务切换与验证

Web、Worker 均由当前用户的 LaunchAgent 管理，工作目录保持 data，DATABASE_URL 指向同一个部署库，PYTHONPATH 指向 `releases/bc97e76`。实际 launchd 注册信息已经核对，而非只检查磁盘 plist。

初次 bootstrap 返回重复加载错误；此时磁盘配置已更新，但内存注册定义仍指向旧发布路径，健康接口仍可返回 200。确切重新加载来源未确认。本次临时禁用这两条服务的自动启动，先卸载 Worker、再卸载 Web，确认进程退出后，恢复启用并依次加载 Web、Worker。最终两条服务均为 enabled、running，不存在遗留的禁用状态。

| 检查 | 结果 |
| --- | --- |
| 运行版本 | Web、Worker 的实际 PYTHONPATH 均为 bc97e76 发布目录 |
| 实例和端口 | 单个 Web、单个 Worker；Web 仅监听 127.0.0.1:8000 |
| /health | HTTP 200 |
| /health/ready | HTTP 200，database=ready、worker=ready |
| Worker 心跳 | 新 worker_id 不同于备份，last_sync_ok=true；最终检查心跳距当前时间不足 1 秒 |
| /login | HTTP 200，CSRF 字段已渲染，无原始模板标记 |
| 四项静态资源 | app.js、styles.css、theme.js、favicon.svg 均 HTTP 200，内容与目标发布一致 |
| 安全响应头 | 登录页和静态资源 X-Content-Type-Options=nosniff |
| 浏览器 | 实际登录页标题、用户名与密码输入框、登录按钮显示正常；该页未记录 console error/warn |

访问地址：[本地登录页](http://127.0.0.1:8000/login)。浏览器检查未输入真实凭据，因此不代表已完成登录后的业务流程验收。

## 回退与验收边界

旧发布目录 `releases/3c9bf35/` 和旧启动配置副本已保留。检查发现旧目录中的静态资源与其目录名所代表的历史提交并不完全对应；回退前必须重新核对或从明确 Git 提交导出代码，不能直接假定该目录是未经修改的旧版本。

本次保留并校验了备份，尚未进行业务数据库恢复演练。回退须在维护窗口成对处理数据库和配置，禁止将旧数据库覆盖到正在运行的服务。运行文件、备份和密钥均不应提交 GitHub。

结论：合并后的代码已完成本机升级和上述检查；这不是生产验收完成证明。仍需受控联调真实 SQL Server 的原生语法检测、只读权限与超时重试，以及真实 SMTP 投递。日志轮转、备份恢复演练、Mac 注销或休眠后的运行保障仍未验收。Python 3.13 测试 SQLite ResourceWarning 和 Actions 运行环境迁移提示仍为已知后续项。本机单 SQLite、单 Web、单 Worker 且仅回环访问的边界不变。
