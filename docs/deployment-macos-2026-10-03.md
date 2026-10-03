# Mac 本地部署记录

> 本文为首次部署的历史快照。当前发布副本已切换至 `bc97e76`，
> 最新状态和升级验证见 [Mac 本地升级报告](deployment-upgrade-macos-2026-10-03.md)。

日期：2026-10-03。用户授权合并、推送并部署到当前 Mac。
本记录不包含密码、加密密钥或真实数据源连接信息。

## 1. 版本与交付

- [PR #11](https://github.com/leileipei/Early-Warning-System/pull/11) 已合并到 main。
- 合并时间：2026-10-03 11:18:14，Asia/Shanghai。
- 本地部署代码：`3c9bf3535fc0f202378446f1364a546c6734bbb9`。
- [合并后 main CI](https://github.com/leileipei/Early-Warning-System/actions/runs/37092748086)
  对应同一提交，Python 3.11 / 3.12 / 3.13 全部 success。
- 合并后本地 Python 3.12.14 回归：579 passed，25.15 秒，覆盖率 94.17%，门槛 93%。

## 2. 实际运行目录

根目录为 `$HOME/Library/Application Support/Early Warning System`。

| 相对根目录的位置 | 用途 |
| --- | --- |
| releases/3c9bf35/ | 从 Git 合并提交导出的 app、模板、静态资源及生产锁文件 |
| .venv/ | 专用 Python 3.11.15 环境，按 requirements.lock 安装 |
| data/.env | 原配置和原密钥的受限权限副本 |
| data/early_warning.sqlite3 | 当前服务实际使用的数据库 |
| logs/web.log、logs/worker.log | Web 与 Worker 输出日志 |

Web 和 Worker 通过相同的 PYTHONPATH 加载该发布副本，工作目录均为 data。
两个 LaunchAgent 的 DATABASE_URL 均显式指向上表的部署数据库，优先于 .env。
部署根目录、data、logs 权限为 700，.env、数据库、日志及 LaunchAgent 文件权限为 600。

**原 Documents 项目目录中的 .env 和数据库已保留，但原库不是当前运行库。**
后续页面配置和执行记录写入 Library 下的部署库；不要在原目录另外启动一个 Worker，
也不要直接用原库覆盖部署库。升级代码必须更新发布副本和启动服务的 PYTHONPATH；
仅 git pull 不会更新当前运行服务。

最初尝试从 Documents 启动用户服务，launchd 因隐私权限限制无法打开日志，
只读 pwd 探针亦返回 Operation not permitted。失败服务和探针已卸载。
改用标准应用数据目录后启动成功，未关闭 macOS 隐私保护或修改 TCC 数据库。

## 3. 数据保留与验证

- 部署前成对备份位于原项目 `.venv/ews-runtime/backups/20261003-3c9bf35/`。
- SQLite 通过 backup API 创建备份和部署副本，不直接复制运行中的数据库文件。
- 备份及部署库 integrity_check=ok，foreign_key_check 无异常行。
- 部署 .env 与原文件字节一致，管理员密码哈希及 session_version 保持不变。
- 原有 2 个数据源和 1 个 SMTP 的加密凭据保持不变，3 项均可用原密钥解密。
- 部署时有 1 个管理员、0 条规则；没有重置密码，没有发测试邮件或执行业务 SQL。
- 部署环境 pip check 通过，检测到 ODBC Driver 18 for SQL Server。

后续备份应针对 **部署 data 目录**，保持数据库与该目录的 .env 成对保存、权限受限。
在维护窗口先停止 Worker，再停止 Web；用 SQLite backup API 或 sqlite3 .backup，
并校验完整性。不要把 .env、数据库、日志和备份上传 GitHub。
本次已完成备份和保留校验，尚未演练从备份恢复业务数据。

## 4. 服务和访问

- 登录地址：<http://127.0.0.1:8000/login>。
- 单个 Web，绑定 127.0.0.1:8000，未向局域网开放。
- 单个 Worker，与 Web 共用部署数据库和配置。
- 由当前用户的 LaunchAgent 管理，已配置 RunAtLoad、KeepAlive 和 30 秒重启节流。
- 关闭 Codex 不会主动停止这些服务。用户注销和 Mac 休眠期间不保证调度执行。
- 本次验证了卸载再加载服务；没有实际注销、重启 Mac 或休眠唤醒验证。

服务文件位于 `$HOME/Library/LaunchAgents/`，标签为
`com.leileipei.early-warning.web` 和 `com.leileipei.early-warning.worker`。

停止服务，先 Worker 后 Web：

```sh
launchctl bootout "gui/$(id -u)/com.leileipei.early-warning.worker"
launchctl bootout "gui/$(id -u)/com.leileipei.early-warning.web"
```

加载服务，先 Web；确认 /health 返回 200 后，再加载 Worker：

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.leileipei.early-warning.web.plist"
curl --fail http://127.0.0.1:8000/health
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.leileipei.early-warning.worker.plist"
curl --fail http://127.0.0.1:8000/health/ready
```

启动后可能需要等数秒再检查；已经加载的服务不要重复 bootstrap。
检查服务可用 launchctl print，日志位于上述 logs 目录。
日志文件尚未配置自动轮转，长期运行应单独补齐容量管理。

## 5. 实际验收结果及边界

| 检查 | 结果 |
| --- | --- |
| /health | HTTP 200，status=ok |
| /health/ready | HTTP 200，database=ready、worker=ready |
| /login | HTTP 200，CSRF 隐藏字段已渲染，无原始模板标记 |
| styles.css / app.js | HTTP 200，内容非空 |
| 安全响应头 | 登录页和静态资源 X-Content-Type-Options=nosniff |
| 实例与端口 | 仅一个 Web、一个 Worker；仅回环监听 |
| 服务重启 | 按顺序卸载再加载后，新 PID 健康和就绪检查通过 |

结论：本轮合并、GitHub 交付和 Mac 本地部署已完成，不能据此认定生产验收完成。
未使用真实账号进行浏览器登录，未做本轮浏览器视觉验收；真实 SQL Server 原生语法检测、
只读权限、超时重试和真实 SMTP 投递仍需受控联调。
Python 3.13 SQLite ResourceWarning 及 Actions 运行时提示仍按发布验证记录列为后续项。
单机 SQLite、单 Web、单 Worker 边界不变，不构成多机高可用或全天候服务器部署。
