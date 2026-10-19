# sub2api API Key 数据查询

独立、朴素的中文只读页面。用户输入完整 API Key，查询自己的状态、钱包余额、Key 额度、过期时间、最后使用时间、已用金额、Token 和当前并发。部署在两个 `key.*` 子域名；主项目网页使用主域名。本仓库不修改 sub2api 代码，也不调用会改写最后使用时间的 `/v1/usage`。

已按官方 **sub2api v0.2.17** 核对数据表和 Redis 槽位。运行时 PostgreSQL 仅 SELECT，Redis 仅 TIME / ZCOUNT。数据库结构升级后应重新核对本仓库的查询与权限。

## 运行

需要 Python 3.10+，Ubuntu 可使用系统包：

```sh
apt-get install python3 python3-psycopg2 python3-redis
```

兼容 Ubuntu 的 psycopg2 2.9.2 / redis-py 3.5.3。也可在虚拟环境安装 `requirements.txt`。服务依赖通过环境变量提供；Python 不会自动加载 `.env`，示例 systemd 单元使用 `EnvironmentFile` 加载。

将服务文件部署到 `/opt/check-sub2api-key-status`，复制 `.env.example` 为 `/etc/key-status/key-status.env` 并填写凭据，文件权限设为 `0600`。按 `grants.sql` 创建专用数据库只读角色。本机部署使用原生 PostgreSQL / Redis，只在宿主机回环地址提供数据库连接。

```sh
cp key-status.service /etc/systemd/system/key-status.service
systemctl daemon-reload
systemctl enable --now key-status
curl -fsS http://127.0.0.1:8090/healthz
```

`/healthz` 是进程存活检查，不读取数据库。Nginx / Caddy 应仅把 `key.tmallmodels.com`、`key.manyai.icu` 代理到 `127.0.0.1:8090`，保留原始 Host，开启 HTTPS。关闭代理对该站点的请求体记录；服务本身不记录路径、请求头、请求体、Key 或查询结果。

## 环境变量

| 变量 | 默认值 / 含义 |
| --- | --- |
| `KEY_STATUS_PG_DSN` | 必填；libpq DSN，只读角色凭据 |
| `KEY_STATUS_REDIS_HOST` | `127.0.0.1` |
| `KEY_STATUS_REDIS_PORT` | `6379` |
| `KEY_STATUS_REDIS_USERNAME` | `key_status_ro` |
| `KEY_STATUS_REDIS_PASSWORD` | 必填；独立 Redis ACL 用户密码 |
| `KEY_STATUS_REDIS_DB` | `0`；与 sub2api 一致 |
| `KEY_STATUS_SLOT_TTL_SECONDS` | `1800`；必须与主项目并发 TTL 一致 |
| `KEY_STATUS_BIND` | `127.0.0.1`；只允许回环地址 |
| `KEY_STATUS_PORT` | `8090` |
| `KEY_STATUS_ALLOWED_HOSTS` | 两个 key 子域名、`localhost,127.0.0.1` |
| `SUB2API_TIMEZONE` | `Asia/Shanghai`；必须与主项目一致，用于订阅日窗口 |

主项目 v0.2.17 正常配置默认 `gateway.concurrency_slot_ttl_minutes=30`。环境变量名称为 `GATEWAY_CONCURRENCY_SLOT_TTL_MINUTES`。若主项目调整它，本服务同时调整为对应秒数。

## 数据库只读权限

PostgreSQL 管理员（superuser）在 **sub2api 数据库**中执行：

```sql
CREATE ROLE key_status_ro LOGIN PASSWORD '<随机生成、单独保存的密码>';
```

然后执行 [`grants.sql`](grants.sql)。该文件精确列出所需列；不授予用户邮箱、密码哈希、上游账号信息或任何写入权限。数据库名不是 `sub2api` 时调整 CONNECT 授权。角色必须是新建的独立角色，不要复用已有管理角色。服务还为每次查询设置只读事务、4 秒 SQL 超时和 3 秒连接超时。

`psycopg2` 参数绑定能防止 SQL 注入，但 PostgreSQL 语句日志仍可能包含绑定后的 Key。因此 `grants.sql` 用此登录角色的设置关闭语句日志、慢查询采样及错误附带 SQL；其中日志设置需要 superuser 执行。后续启用数据库审计或第三方查询追踪时，也应对本角色避免记录原始 SQL 和参数。

## Redis 只读权限

由 Redis 管理员创建独立 ACL 用户；以下是 ACL 规则示例，真实密码应通过安全渠道传入，不写入仓库：

```text
ACL SETUSER key_status_ro reset on >GENERATED_PASSWORD ~concurrency:api_key:* ~concurrency:live:api_key:* +auth +select +time +zcount
```

`AUTH` 用于认证；`SELECT` 仅切换本连接的逻辑数据库。数据命令仅 `TIME` 和 `ZCOUNT`，不授予写命令、KEYS、SCAN、EVAL、MULTI/EXEC 或其他 Key 的读取权限。`redis-py` 使用 `pipeline(transaction=False)`，不会发事务命令。3.5.3 不发送 CLIENT SETINFO；新版客户端显式关闭它。

**持久化 ACL：** `ACL SETUSER` 仅在当前 Redis 进程有效。应在 Redis 配置中启用宿主机 `aclfile`，将专用用户规则持久化到该文件，使 Redis 重启后仍可认证。保留主程序使用的原有 default 用户及其认证方式。变更后验证服务，再重启 Redis 验证 ACL 能保留；勿把含密码的文件提交到 Git。

## 字段含义与精度

| 页面字段 | 来源 / 含义 |
| --- | --- |
| Key 数据库状态 | `api_keys.status`；即使停用、过期仍允许持有者查询自身数据 |
| Key 有效状态 | 数据库状态，叠加当前时间和 Key 总额度检查；不承诺请求一定能被上游处理 |
| 用户 / 分组状态 | `users.status` / `groups.status`；不存在的分组单独显示 |
| 用户共享钱包余额 | `users.balance`；全部 Key 共享，不代表每个 Key 独享此余额 |
| 用户共享冻结金额 | `users.frozen_balance`；单独展示主程序当前值，不自行重复扣减余额 |
| Key 总额度及剩余额度 | `api_keys.quota` / `max(quota-quota_used, 0)`；总额度为 0 表示不限制 |
| Key 额度计数已用 | `api_keys.quota_used`；管理员可重置；无限额 Key 通常不累计该计数 |
| 累计已使用额度 | 此 Key 保留的 `usage_logs.actual_cost` 之和，含主程序计费倍率 |
| 最后使用时间 | `api_keys.last_used_at`；主程序约 30 秒防抖，尽力记录鉴权使用时间，并非每次请求的精确完成时间；其他官方鉴权查询可能更新它；本页不会更新它 |
| Token 总数 | `input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens`，与官方页面一致 |
| 当前并发 | 此 Key 的普通请求槽位 + Live 租约；没有使用用户总并发冒充 Key 并发 |
| 用户并发上限 | 用户全部 Key 共享的上限，额外标示便于理解 |
| Key 额度窗口 | 5h / 1d / 7d；到期窗口以 0 用量显示，不修改主项目数据 |
| 订阅额度 | 展示日 / 周 / 30 天周期和各周期剩余额度的最小值；订阅已失效时可用剩余为 0；未配置周期上限时显示不限制 |

订阅日窗口按主程序时区自然日刷新；一天内到期的日额度不自动重发。周 / 30 天窗口遵循 v0.2.17 的期限锚点、初始旧锚点修正及到期边界，在内存中计算当前显示值，不维护主程序数据库窗口。Key 过期时间和订阅到期时间分别显示。

所有累计统计均基于当前保留的用量记录。管理员清理历史日志后会减少；进行中的请求结算后才出现。`quota_used` 是独立计数，可能因重置或日志清理与日志累计金额不同。

Redis 使用服务器 TIME，然后只读计数：

```text
ZCOUNT concurrency:api_key:{id} (now-slot_ttl_seconds +inf
ZCOUNT concurrency:live:api_key:{id} (now-60 +inf
```

排除截止时间及之前的过期槽位，等价于主程序先清理再计数。不含等待用户槽位的排队和空闲 OpenAI WebSocket ingress 连接；已持有用户槽位、等待上游账号的请求可能计入。异常中断可能要等待槽位过期。Redis 不可用时显示“暂不可用”，不会误报为 0。

## 接口与隐私

仅支持同站 `POST /api/check`，`Content-Type: application/json`，请求体 `{"key":"完整 API Key"}`。Key 最长 128 字节，请求体最大 1024 字节；后端使用参数化 SQL，不把 Key 放入 URL、响应或日志。查询不存在 / 已删除的 Key 返回通用 404。数据库错误返回通用 503，不暴露异常文本。

页面没有外部资源、分析脚本、Cookie、localStorage 或 sessionStorage。Key 仅留在当前页面的密码输入框中，清空或离开页面后清理；手动查询不自动轮询。所有响应 `Cache-Control: no-store`，严格 CSP；不启用 CORS，检查 Host 与 Origin，拒绝 iframe。HTTPS 由反向代理提供。

## 测试

测试不需要数据库、Redis 或真实 Key：

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖 HTTP 查询、无效输入、同源校验、通用错误、禁止缓存、Key 不回显、只读 SQL 参数、只读 Redis 命令和 TTL 边界，以及额度 / Token / 订阅窗口计算。部署时还应使用临时测试 Key 进行一轮实际查询并立即删除。
