# Docker Compose 调试与测试部署

## 资源与服务

默认启动 PostgreSQL、Qdrant、API（含控制台/MCP）与后台 worker。Mem0 是 worker 内的 Python SDK，不需要另一套 OpenMemory 服务或数据库。记忆提取和 embedding 调用 `.env` 配置的 OpenAI 兼容服务，数据会发送给你选择的模型服务。

Langfuse 是正式支持的观测组件，但为适应调试机器，它的完整依赖通过 `langfuse` profile 按需启动。不开这个 profile 时，记忆操作、同步、直接上报的 trace 和分析仍可使用；Langfuse 原始执行明细与其 adapter 需要可用的 Langfuse。也可以连接另一台机器的 Langfuse。

| 机器内存 | 建议调试方式 |
| --- | --- |
| 8GB | 默认核心栈；需要 Langfuse 时优先连接外部实例，或分阶段调试 |
| 12GB | 核心栈常驻，按需启用本地完整 Langfuse；保持低并发并观察内存 |
| 16GB 及以上 | 完整栈有更充足的余量 |

Compose 设置了调试用途的容器内存上限；上限不是容量保证，也不预留资源。完整栈的上限之和约 10GB，宿主系统和 Docker 本身还需要内存。ClickHouse 迁移、首次构建、Mem0 导入以及大批量 ingestion 都可能产生峰值。Docker Desktop 用户须检查虚拟机内存设置；Linux 上可用 `docker stats --no-stream` 检查实际用量。模型不在本机运行。

## 启动核心栈

需要 Docker Engine 与 Compose v2，部署文件面向 Linux，镜像同时提供的 CPU 架构以镜像仓库为准。

```sh
cp .env.example .env
```

编辑 `.env`，至少填写：

- `API_KEY`：控制台、REST API 与 MCP 使用的访问密钥。
- `POSTGRES_PASSWORD`：核心数据库密码；建议使用不含 URI 保留字符的随机值，或对 `DATABASE_URL` 中的密码做 URL 编码。
- `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`：Mem0 记忆提取服务。
- `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSIONS`：向量服务。维度必须与该模型实际输出一致。

LLM 端点需支持 `/chat/completions` 与 JSON object response_format；embedding 端点需支持 `/embeddings`。兼容性仍需用实际模型验证。

两个模型接口独立配置，不能假设相同密钥或网关都支持 embedding。不要在已有向量集合上直接改变 embedding 模型或维度；应按项目的重建流程使用新集合重新索引。

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 api worker
curl -fsS http://127.0.0.1:8000/health
```

打开 [Memory OS 控制台](http://localhost:8000)，按页面提示输入 API key。`/health` 是不调用付费模型的服务存活检查，不能据此断言模型接入成功；还需要实际提交一条记忆、等待后台处理并验证语义检索。

默认只绑定本机 `127.0.0.1:8000`，数据库和 Qdrant 不发布宿主端口。从电脑访问远程 Linux 调试机器可建立 SSH 转发：

```sh
ssh -L 8000:127.0.0.1:8000 user@server
```

若明确需要局域网访问，可设置 `API_BIND=0.0.0.0` 并重新启动 API；跨公网访问应使用提供 HTTPS 的反向代理。API 密钥不是 TLS 的替代品。

## 启用完整 Langfuse

本 Compose 的服务与存储配置以 [Langfuse v3.132.0 官方 Compose](https://github.com/langfuse/langfuse/blob/v3.132.0/docker-compose.yml) 为依据，并固定镜像版本。它包含独立 PostgreSQL、ClickHouse、Redis、MinIO、Langfuse web 和 worker，与核心 PostgreSQL 分离。

先填写 `.env` 中 Langfuse 一组配置，替换全部 `replace-...` 值。加密密钥必须是 64 个十六进制字符，可以在自己的终端生成：

```sh
openssl rand -hex 32
```

将结果填入 `LANGFUSE_ENCRYPTION_KEY`，为 `LANGFUSE_NEXTAUTH_SECRET`、`LANGFUSE_SALT`、数据库、ClickHouse、Redis、MinIO 设置独立随机值。保留这些配置，尤其是加密密钥，后续恢复数据库时仍需要它们。

```sh
docker compose --profile langfuse config --quiet
docker compose --profile langfuse up -d --build
docker compose --profile langfuse ps
docker compose --profile langfuse logs --tail=100 langfuse-web langfuse-worker
```

第一次启动包含数据库迁移，可能需要几分钟。在 [Langfuse UI](http://localhost:3000) 创建用户和项目，并在该项目中创建 API keys。填入 `.env` 的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`，并把 `LANGFUSE_PROJECT_ID` 改为实际项目 ID，然后让核心服务读取更新后的环境：

```sh
docker compose up -d --force-recreate api worker
```

也可填写 `LANGFUSE_INIT_USER_EMAIL`、`LANGFUSE_INIT_USER_PASSWORD` 与项目 API keys，使用 Langfuse 自带的首次初始化功能。初始化变量用于首次创建，不是对已有项目的密码轮换机制。

容器内 adapter 地址是 `LANGFUSE_BASE_URL=http://langfuse-web:3000`；浏览器跳转地址是 `LANGFUSE_UI_URL=http://localhost:3000`。这两个值用途不同。连接外部 Langfuse 时，只需修改这两个地址与项目 API keys，保持本地 profile 关闭。

MinIO 对浏览器可见的媒体 URL 默认是 `http://localhost:9090`，由 `MINIO_PUBLIC_URL` 配置。远程机器使用 SSH 调试时一并转发端口：

```sh
ssh -L 8000:127.0.0.1:8000 -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 user@server
```

调试完可以停止这组容器，核心继续运行：

```sh
docker compose --profile langfuse stop langfuse-web langfuse-worker clickhouse redis minio langfuse-postgres
```

不要在容器仍运行时修改 Redis/数据库密码后只重启部分服务。PostgreSQL 的 `POSTGRES_PASSWORD` 初始化变量也不会自动修改已有卷内的数据库用户密码。

## 日常检查与数据保留

```sh
docker compose ps
docker stats --no-stream
docker compose logs --tail=200 api worker
docker compose --profile langfuse logs --tail=200 langfuse-web langfuse-worker clickhouse
```

后台任务失败时，先检查 worker 日志中的模型认证、模型名称、embedding 维度与网络错误。Langfuse adapter 未接收到记录时，应先验证项目 API keys、Langfuse ingestion/worker 和 trace 本身是否存在。

核心 `postgres-data` 保存事件、投影与后台任务；`qdrant-data` 保存语义索引。Langfuse 的 PostgreSQL、ClickHouse、Redis、MinIO 各有独立持久卷。普通 `docker compose stop` 或不附带卷删除参数的 `down` 会保留这些卷。不要把源码目录或镜像当作数据备份。

PostgreSQL 逻辑备份示例（在宿主机执行，文件包含记忆原文）：

```sh
mkdir -p backups
docker compose exec -T postgres pg_dump -U memory -d memory -Fc > backups/memory.dump
docker compose --profile langfuse exec -T langfuse-postgres pg_dump -U langfuse -d langfuse -Fc > backups/langfuse.dump
```

恢复应在隔离环境中使用 `pg_restore` 验证，并同时保留 `.env` 中的密钥配置。Qdrant 的快照/恢复参照 [Qdrant snapshots 文档](https://qdrant.tech/documentation/concepts/snapshots/)；Langfuse 的 PostgreSQL 备份不能替代 ClickHouse 与对象存储备份。完整备份需在停止写入后对对应持久卷或数据库分别备份并测试恢复。

## 验证边界

`docker compose config --quiet` 仅确认配置解析与变量插值成功，不确认镜像可拉取、应用可启动或服务版本组合可运行。完整验收应包含容器启动、健康检查、真实模型提取与语义检索、Langfuse 原始 trace ingestion 和 adapter 处理；实际执行结果以根目录 README 与任务回顾中的记录为准。

## 镜像构建下载源

默认从官方 PyPI 安装锁定依赖。若调试网络无法访问它，可以使用公开镜像构建（首次安装依赖仍需要联网）：

```sh
docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
docker compose up -d --no-build
```

首次构建会预缓存 tokenizer 文件，运行容器无需为token计数另行下载该文件。
