# GeoLook Hosted 单租户 MVP 部署

这一步把 GeoLook 从“本机工具”变成“可部署在服务器上的单租户服务”。它仍然保留文件存储和单令牌登录，不包含完整 SaaS 的账号、团队、计费、多租户隔离。

## 适合场景

- 你自己或一个小团队通过网址使用 GeoLook
- 给少量客户演示或代运营，不要求客户之间自助注册
- 数据仍由你控制在自己的服务器上

## 不包含

- 多租户账号体系
- 团队角色权限
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

首次访问输入 `GEOLOOK_TOKEN`。

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

## 7. 安全边界

- 公开访问必须设置 `GEOLOOK_TOKEN`
- 正式域名访问必须使用 HTTPS，并设置 `GEOLOOK_COOKIE_SECURE=1`
- `.env` 和 `data/` 含有客户数据与 API Key，不要提交到 git
- 当前版本是单租户服务，不要把同一个实例开放给彼此无关的客户共用
- Chrome 采样助手仍默认面向本机 `127.0.0.1`，Hosted 域名支持放到后续阶段

## 8. 健康检查

服务提供公开健康检查：

```text
/healthz
```

返回：

```json
{"ok": true, "service": "geolook"}
```
