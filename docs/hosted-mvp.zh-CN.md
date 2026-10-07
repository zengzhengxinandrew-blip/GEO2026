# GeoLook Hosted 单租户 MVP 部署

这一步把 GeoLook 从“本机工具”变成“可部署在服务器上的服务”。它使用本地文件存储、用户名密码登录和项目成员权限；还不是完整的多租户 SaaS，不含支付、套餐或独立数据库。

## 适合场景

- 你自己或一个小团队通过网址使用 GeoLook
- 给少量客户演示或代运营，不要求客户之间自助注册
- 数据仍由你控制在自己的服务器上

## 不包含

- 客户自行注册和组织级多租户隔离
- 支付和套餐
- Chrome 采样助手的云端登录
- 数据库化和分布式任务队列

这些属于下一阶段 SaaS 架构改造。

## 1. 准备服务器

建议用一台 Linux 服务器：

- Ubuntu 22.04 / 24.04
- Docker + Docker Compose
- 一个已经解析到服务器的域名

## 2. 配置环境变量

复制示例文件：

```bash
cp .env.example .env
```

至少填写：

```bash
GEOLOOK_TOKEN=换成一串足够长的随机令牌
```

如果要启用 HTTPS 域名访问，再填写：

```bash
GEOLOOK_DOMAIN=geo.example.com
GEOLOOK_COOKIE_SECURE=1
```

可选填写各引擎 API Key，例如 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等。

生成随机令牌可以用：

```bash
openssl rand -hex 32
```

## 3. 本机端口模式

适合先验证服务是否能跑：

```bash
docker compose up -d geolook
```

服务只监听服务器本机：

```text
http://127.0.0.1:8765
```

远程访问可用 SSH 隧道：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@your-server
```

然后在本地浏览器打开：

```text
http://127.0.0.1:8765
```

首次启动会用 `GEOLOOK_ADMIN_PASSWORD`（未设时用 `GEOLOOK_TOKEN`）创建管理员；在登录页输入管理员用户名和密码。

## 4. HTTPS 域名模式

确认 `.env` 里已经设置：

```bash
GEOLOOK_DOMAIN=geo.example.com
GEOLOOK_TOKEN=...
GEOLOOK_COOKIE_SECURE=1
```

启动：

```bash
docker compose --profile https up -d
```

Caddy 会自动申请 HTTPS 证书。访问：

```text
https://geo.example.com
```

## 5. 数据目录

compose 会把所有可变数据放到：

```text
./data
```

其中：

- `./data/work`：项目、采样、报告、交付物
- `./data/jobs`：后台任务状态和日志
- `./data/.env`：看板里保存的密钥配置

升级后，既有项目只对全局管理员可见。请先由全局管理员创建用户，再到「设置 → 项目成员权限」逐项目分配管理员、可编辑或只读。全局 API Key 和采样保护上限只能由全局管理员修改。

同一项目的后台任务通过跨进程文件锁互斥；不同项目可以同时运行。任务使用启动时的项目快照，成功后发布结果，失败时保留旧结果；运行中修改的配置会保留给下一期。默认全局 API 并发为 2、每日逻辑答题调用上限为 300；金额上限需另行配置每个引擎的单次尝试最高预估费用，仍建议在供应商后台设置硬预算或告警。

备份时重点备份整个 `data/` 目录。

## 6. 更新版本

```bash
git pull
docker compose build geolook
docker compose --profile https up -d
```

如果只用本机端口模式：

```bash
docker compose up -d --build geolook
```

## 7. 客户网站抓取与证书

GEOLOOK 自己域名的 HTTPS 证书只保护“浏览器 → GEOLOOK”；抓客户公开网站时，
使用的是另一条“GEOLOOK 容器 → 客户网站”连接。更换前者的证书不会修复后者。

镜像会安装系统公共 CA，并在普通请求遇到 403/406 或浏览器验证页时，自动尝试一次
浏览器 TLS/HTTP2 指纹回退。回退仍然校验证书，不执行 JavaScript，不绕过登录、验证码
或 `robots.txt`。

如果日志显示域名解析到了 `198.18.0.0/15` fake-IP，或 HTTPS 被企业代理/安全网关解密，
应把该代理的 PEM 根证书放进持久化目录并在 `.env` 指向容器内路径：

```bash
mkdir -p data/certs
cp /实际路径/proxy-root-ca.pem data/certs/proxy-root-ca.pem
chmod 644 data/certs/proxy-root-ca.pem
printf '\nGEOLOOK_CA_BUNDLE=/data/certs/proxy-root-ca.pem\n' >> .env
docker compose up -d --build --force-recreate geolook
```

如果只有 DNS 被 fake-IP 改写、没有 HTTPS 解密，可选配置可信 DoH：

```bash
printf '\nGEOLOOK_DOH_URL=https://1.1.1.1/dns-query\n' >> .env
docker compose up -d --force-recreate geolook
```

DoH 只解决 DNS，不会让不受信任的代理证书变可信。不要配置 `verify=False`。

## 8. 安全边界

- 公开访问必须设置 `GEOLOOK_TOKEN`
- 正式域名访问必须使用 HTTPS，并设置 `GEOLOOK_COOKIE_SECURE=1`
- `.env` 和 `data/` 含有客户数据与 API Key，不要提交到 git
- 当前版本是单租户服务，不要把同一个实例开放给彼此无关的客户共用
- Chrome 采样助手仍默认面向本机 `127.0.0.1`，Hosted 域名支持放到后续阶段

## 9. 健康检查

服务提供公开健康检查：

```text
/healthz
```

返回：

```json
{"ok": true, "service": "geolook"}
```
