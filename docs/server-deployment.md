# 当前服务器部署记录

部署日期：2026-10-20（Asia/Shanghai）。服务器：`8.209.241.20`，Ubuntu 22.04。

## 访问地址

| 用途 | 地址 |
| --- | --- |
| Sub2API 主站 | https://tmallmodels.com / https://manyai.icu |
| 独立 API Key 查询页 | https://key.tmallmodels.com / https://key.manyai.icu |

管理员登录名为 `admin@tmallmodels.com`，密码按部署请求设置，不存放在此仓库。
管理员首次进入后台需要本人确认 Sub2API 自带的使用声明。

## Sub2API

使用官方 `Wei-Shaw/sub2api` 的 `deploy/install.sh` 安装稳定版 `v0.2.17`。
官方二进制安装支持管理后台检测更新、应用更新及回滚。为满足网页更新和 root 运行要求，采用宿主机二进制与 systemd。

- 二进制：`/opt/sub2api/sub2api`
- 配置与安装标记：`/opt/sub2api/data/`
- 环境配置：`/etc/sub2api/sub2api.env`（root:root，0600）
- systemd：`sub2api.service`；`/etc/systemd/system/sub2api.service.d/override.conf` 设置 `User=root`、`Group=root`。
- 监听：`127.0.0.1:8080`，由 Nginx 提供 HTTPS、WebSocket 和流式代理。
- PostgreSQL 16：宿主机 `/var/lib/postgresql/16/main`，仅本机监听。
- Redis：宿主机 `/var/lib/redis`，开启 AOF，仅本机监听且要求密码。
- 固定保存 JWT 与 TOTP 加密密钥，更新和重启后保留。
- 并发槽位有效期设置为 `GATEWAY_CONCURRENCY_SLOT_TTL_MINUTES=30`。

官方 Docker Compose 也是推荐部署方式，其中本地目录方案把应用、PostgreSQL、Redis 分别挂载到宿主机的 `data/`、`postgres_data/`、`redis_data/`。常规容器重建会保留数据；官方 Docker 升级命令为拉取镜像再重建容器。此次未使用 Docker。

参考：[官方部署说明](https://github.com/Wei-Shaw/sub2api/blob/main/README_CN.md)、[官方本地数据目录 Compose](https://github.com/Wei-Shaw/sub2api/blob/main/deploy/docker-compose.local.yml)。

## 独立查询服务

本仓库代码部署到 `/opt/check-sub2api-key-status/`，由 `key-status.service` 以 root 运行，监听 `127.0.0.1:8090`。
配置在 `/etc/key-status/key-status.env`（0600），不提交 Git。

服务不修改 Sub2API 的二进制、前后端代码和业务数据。数据库使用仅有指定列 SELECT 权限的 `key_status_ro` 角色；Redis 使用只允许读取对应并发槽位的独立 ACL 用户。
Sub2API 的普通槽位配置与查询服务的 `KEY_STATUS_SLOT_TTL_SECONDS=1800` 必须同步。

查询页的 🔑 标签页图标使用 HTML 内嵌 SVG。Nginx 仅在查询域名的 `location = /` 中覆盖页面 CSP，在原有策略上添加 `img-src data:`；其余静态资源、接口和后端程序保持原有配置。迁移代理配置时需保留此规则，否则浏览器会拦截图标。

## 常用操作

```bash
systemctl status sub2api key-status nginx postgresql redis-server
journalctl -u sub2api -n 100 --no-pager
journalctl -u key-status -n 100 --no-pager
systemctl restart sub2api
systemctl restart key-status
nginx -t && systemctl reload nginx
```

Key 查询页更新：将仓库中的应用文件同步到 `/opt/check-sub2api-key-status/` 后运行 `systemctl restart key-status`。不要覆盖 `/etc/key-status/key-status.env`。

## HTTPS 与备份

四个域名共用 Let's Encrypt SAN 证书，证书目录 `/etc/letsencrypt/live/tmallmodels.com/`，`certbot.timer` 负责自动续期。
Nginx 配置：`/etc/nginx/sites-available/sub2api`。

每天北京时间 03:20（附加最多两分钟随机延迟），`sub2api-backup.timer` 执行 `/usr/local/sbin/sub2api-backup`。
备份目录为 `/var/backups/sub2api/`，保留约七天，包含一致性的 PostgreSQL 自定义格式转储、数据库角色、Sub2API 配置、查询服务凭证及代理配置。目录与文件仅 root 可读。

网页更新前可手动备份：

```bash
systemctl start sub2api-backup.service
journalctl -u sub2api-backup -n 10 --no-pager
```

回滚程序版本不等于回滚数据库迁移；需要回退数据库时应在停机后使用匹配的数据库备份恢复。
宿主机持久化与同机备份能够防止普通升级重建造成的数据丢失，不能覆盖磁盘损坏或整台服务器丢失。需要整机故障保护时，将备份另存到其他服务器或对象存储。
