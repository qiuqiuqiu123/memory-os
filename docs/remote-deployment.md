# 139.199.231.195 调试部署与在线配置

## 服务布局

部署根目录 `/opt/memory-os`，发布目录 `/opt/memory-os/releases/20261102-hooks-v1`，敏感配置 `/opt/memory-os/shared/.env`（600权限）。独立Compose项目名 `memory-os`，不使用现有业务端口。核心API绑定服务器 `0.0.0.0:18080`，BGE/PG/Qdrant只在Compose内部网络使用。

服务器实际为8核/16GB内存/AMD64。本次部署方案为核心+CPU BGE，不启动本地完整Langfuse。BGE单进程2CPU/4GB上限，输入默认512tokens截断，内部batch2。数据库使用持久卷；远端通过 `/opt/memory-os/shared/compose.host.yml` 将已校验的模型目录绑定到容器，重建应用不会重新下载权重。LLM仍为远端API，未提供真实密钥时不能自动提取。

当前 BGE 镜像已构建，API、PostgreSQL、Qdrant、BGE四项健康检查通过，worker进程运行。真实CPU推理、中英文语义检索与远端浏览器流程均已验证通过。

## 访问

本机执行：

```bash
ssh -N -L 18080:127.0.0.1:18080 root@139.199.231.195
```

打开 http://127.0.0.1:18080 。如果18080已占用，换本机端口，例如 `-L 18081:127.0.0.1:18080`，然后打开18081。

在有权限的终端查看管理访问密钥（不要分享终端输出或将其写入URL）：

```bash
ssh root@139.199.231.195 "sed -n 's/^API_KEY=//p' /opt/memory-os/shared/.env"
```

该密钥用于页面“访问密钥”。GPT API key不同，在“模型设置”中填写，不需要写进聊天。

## 在线模型设置

- LLM：默认建议 `gpt-4.1-mini`，Base URL为实际GPT服务的 `/v1` 地址；填写API key，保存后测试。直接OpenAI端点是否可达/账户是否有模型权限需真实验证，也可以填写用户自己的兼容网关。
- Embedding：默认本机 `http://bge-m3:8080/v1`，`BAAI/bge-m3`，1024维，`send_dimensions=false`。配置为真实BGE-M3 CPU模型，不需要云厂商embedding账单；真实推理已通过验证。
- 密钥留空表示保留；GET只返回`api_key_set`，不回显值。设置加密后存在PostgreSQL，通过部署`SETTINGS_ENCRYPTION_KEY`解密；备份数据库时必须保留该主密钥。
- 所有模型设置为部署级，只有管理员凭据可修改；用户数据仍按user_id/scope隔离。
- 保存对API/worker后续请求即时生效，无需重启。正在执行的任务继续用其启动时配置。
- 修改embedding地址/模型/维度会创建新向量集合；有记忆时必须确认重建，后台从事实投影重新索引。重建期间召回暂时不完整，旧集合保留供恢复，不自动删除。
- “测试LLM”会发起真实、少量模型调用，可能产生费用；“测试向量”调用本机BGE。

## 运维

```bash
cd /opt/memory-os/releases/20261101-capture
docker compose --env-file /opt/memory-os/shared/.env -p memory-os \
  -f docker-compose.yml -f compose.bge.yml \
  -f /opt/memory-os/shared/compose.host.yml ps

docker compose --env-file /opt/memory-os/shared/.env -p memory-os \
  -f docker-compose.yml -f compose.bge.yml \
  -f /opt/memory-os/shared/compose.host.yml logs --tail=100 api worker bge-m3

# 检查三层合并后的配置；quiet 避免将解析后的密钥打印到终端。
docker compose --env-file /opt/memory-os/shared/.env -p memory-os \
  -f docker-compose.yml -f compose.bge.yml \
  -f /opt/memory-os/shared/compose.host.yml config --quiet

# 源码或镜像更新后重建并启动。
docker compose --env-file /opt/memory-os/shared/.env -p memory-os \
  -f docker-compose.yml -f compose.bge.yml \
  -f /opt/memory-os/shared/compose.host.yml up -d --build
```

所有远端运维命令都必须保留最后的 `compose.host.yml`，确保使用同一个已校验模型绑定目录，避免切回空的默认缓存卷。

修改网页模型设置不需要重建；源码更新才需要重新构建。BGE权重首次下载约2.27GB，使用固定版本ModelScope文件与SHA256校验，后续从缓存加载。CPU Torch wheel从官方PyTorch源下载；一般Python依赖可用`PIP_INDEX_URL`配置公开镜像。

没有配置公网域名与TLS时，使用SSH隧道访问；不在公网明文HTTP中填写模型密钥。后续有域名可独立配置HTTPS入口，不必修改应用内部地址。

## 本次验证结果

真实CPU BGE-M3、1024维输出、双向中英文召回及Playwright主要流程均通过。LLM尚未填key，因此自动记忆提取尚未做真实GPT验证。BGE约750MiB、五容器合计约1.22GiB为小流量快照。按用户选择已改为公网监听18080；外部连接超时，云侧防火墙放行待处理。SSH隧道仍可使用。完整结果见 `verification.md`。

## 2026-10-29：vectorplanets.com 子路径

已配置独立Nginx站点 `/www/server/panel/vhost/nginx/vectorplanets.com.conf`，源码样例在 `deploy/nginx/vectorplanets.com.conf`。`/memoryos` 返回308到 `/memoryos/`，该前缀代理到内部API根路径；前端使用相对于页面目录的API地址，同时兼容原根目录部署。域名根路径继续使用原默认静态站点。配置测试通过后平滑reload，原IP站点及ego站点仍200。

目标访问地址为 `http://vectorplanets.com/memoryos/`。服务器本机指定该Host时health200。通过SSH转发到Nginx，并在Chromium解析该域名到本地转发端口，真实网页登录、全部导航、设置读取、真实BGE测试、刷新及手机布局均通过，无console/page错误、失败请求或HTTP错误。此项验证不代表公网域名已可用。

公网请求当前返回302至 `https://dnspod.qcloud.com/static/webblock.html?d=vectorplanets.com`；DNS解析到正确139.199.231.195，即使显式固定IP仍被拦截。腾讯公开域名状态接口显示备案/接入状态未通过，未显示Ban标记；需用户在腾讯云检查ICP备案及接入要求。HTTPS连接当前reset，尚无本域名证书。已准备隔离Certbot，但没有签发或添加续期任务，待公网域名恢复后完成HTTPS与外部浏览器验证。暂仍使用SSH隧道填写模型密钥。

## Gitee BGE-M3 配置

Embedding Base URL填写 `https://ai.gitee.com/v1`，不是完整的 `/v1/embeddings` 地址；OpenAI SDK会自动追加 `/embeddings`。模型为 `bge-m3`，向量维度1024，关闭“请求中发送dimensions”。2026-11-01从云端使用已保存密钥真实调用成功，SDK默认编码和显式float都返回1024维。更换提供商后重建索引，避免不同提供商模型版本产生的向量混用。

## Codex 轨迹采集当前版本

2026-11-01升级发布目录为 `/opt/memory-os/releases/20261101-capture`，旧目录保留。采集器、后台复盘与候选批准已部署，接入方式和本机启停见 `codex-capture.md`。MCP_ALLOWED_HOSTS已包括公网IP18080与域名，真实公网MCP initialize/tools/list通过；本机Codex使用SSH隧道18082。原来Gitee完整接口URL配置已修正为Base URL `/v1`，1024维真实调用与网页测试通过。

## 2026-11-02 当前：原生Hook + MCP插件

应用release为 `/opt/memory-os/releases/20261102-hooks-v1`，Compose项目、shared配置和数据库/向量卷保持不变。仅重建API/worker，其他应用与BGE未改。MCP新增capture_report（共9工具），复用鉴权/批次幂等/复盘链路。本机原生6类Hook已安装；日志watcher保持停止。插件与多机器接入说明见 ../clients/codex-plugin/README.md。线上GPT/embedding提供方继续由在线设置决定。
