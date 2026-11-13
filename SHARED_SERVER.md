# 同一服务端与跨电脑登录（0.18.0）

本轮已实现服务端选择、已有工作区登录、加密登录文件导出/恢复、退出登录及重连。你目前没有共享服务器，因此没有执行公网部署或数据迁移。下面的服务器配置是部署模板，不代表已经提供了可从另一台电脑访问的地址。

## 为什么换电脑后看不到旧知识库

文档元数据和工作区在后端MySQL中，向量在Milvus中，文件正文在后端上传目录中。插件只保存当前工作区的登录凭证。`localhost`始终指正在使用的那台电脑；在新电脑克隆Git仓库并启动Docker，会得到另一套本地数据，GitHub不包含你的数据库、凭证或知识库正文。

关掉侧边栏或浏览器会保留本机已保存的身份，重开会验证并连接原服务端。卸载扩展、清空浏览器配置或换新电脑后，需要登录原工作区。现在使用工作区ID和随机登录密钥认证，也支持导入加密登录文件；本版没有用户名密码注册、邮箱找回或通过同名认领的功能。模型API Key不能代替登录密钥。

## 两台电脑怎么使用

1. 在保有原工作区身份的旧电脑上更新原扩展目录、重新加载扩展。不要换成另一个新目录重新安装，否则可能得到新的扩展ID，读不到原浏览器存储。
2. 打开设置的“登录与跨电脑使用”，输入10至256字符的文件密码，点击“导出加密登录文件”。文件和密码分开保存；它包含原服务端地址、工作区ID和登录密钥，不包含文档、模型Key或聊天记录。
3. 准备一个两台电脑都能访问的固定HTTPS后端。如果原数据还在旧电脑，应先按下节迁移整套后端数据；不要在空服务端重新创建同名工作区。
4. 新电脑加载扩展，在欢迎页展开“使用加密登录文件恢复”，选择文件并输入文件密码。解密后只填入登录表单，尚未联网或登录。若服务端已迁移，改为新的固定HTTPS地址，核对后点击“登录已有知识库”。
5. 登录会先用给定地址和凭证调用`GET /plugins/me`；成功后才一起保存服务端与身份。打开“我的知识库”，两台电脑读取同一服务端的同一工作区，不需要各自导入文档或再次向量化。

服务端地址允许HTTPS与可选API路径前缀（例如`https://kb.example.com/api`）；前缀需与反向代理配置一致。HTTP仅允许`localhost`、`127.0.0.1`或`[::1]`本机测试。共享部署不要填写另一台电脑自己的localhost。拒绝带用户名密码、查询串或片段的服务地址，并拒绝API重定向，防止登录密钥随重定向发送到另一地址。

“退出登录”只删除本浏览器当前身份，保留服务端知识库；同一浏览器的其他面板会同步离开，另一台电脑不受影响。聊天记录、未发送草稿和Tab上下文仍是各浏览器本地内容，本版没有云端同步这些记录。断网时保留已保存身份，可点击“重新连接已保存的工作区”。

登录文件采用浏览器Web Crypto的PBKDF2-SHA256（600000轮、随机16字节salt）派生密钥，AES-256-GCM（随机12字节nonce和固定版本附加认证数据）加密。仅接受固定版本参数及不超过32KB的文件，错误密码或篡改不会改变当前连接。文件密码不保存到浏览器或服务器。加密算法接口依据[W3C Web Cryptography API](https://www.w3.org/TR/webcrypto-1/)。

## 原数据仍在旧电脑时

先确认哪一台机器持有文档。2026-11-13本机只读核对结果为3个工作区、0篇文档，不能把本机空库当成旧知识库上传覆盖。

迁移对象必须一起处理：MySQL、Milvus、etcd、MinIO、Redis持久数据卷，后端上传/视频目录，以及原`.env`中的`APP_MASTER_KEY`和兼容配置。MySQL内的工作区ID、登录密钥哈希及模型配置密文需要保留；重新生成主密钥会使原模型配置无法解密。路径改变时也要核对数据库中的文件路径及配置目录。

在旧服务器停止API、入库Worker和视频Worker，等待在途任务完成，制作整套一致备份，并在新主机的独立目录恢复验证。已有卷的凭据不能仅通过改变Compose环境变量来重置；首次初始化密码与已存在数据库密码须一致。确认文档列表、原文读取及向量检索后再切换固定域名，保留旧备份供回退。本轮未触碰或替换任何用户数据卷；实际迁移需等旧电脑数据与目标服务器可用后单独实施。

若原浏览器身份与登录文件都丢失，服务器只保存登录密钥的哈希，无法反推出原密钥。不能仅凭工作区名称或模型Key自动认领。需要在原服务器确认数据归属后设计受控的管理员恢复流程；本版未提供公开找回接口。

## 单台Linux服务器部署模板

以下假设仓库在`/srv/web-rag-clipper-full`，服务账户为`ragclipper`，域名为`kb.example.com`。准备Python3.11、Docker Compose2.24.4及以上、Nginx和有效TLS证书。命令在服务器执行，不在客户端电脑重复启动另一套后端。

### 基础设施和Python环境

以部署账户克隆仓库到上述目录，建立虚拟环境，安装主依赖与视频依赖。需要OCR时再安装OCR依赖。

```bash
cd /srv/web-rag-clipper-full
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install -r backend/requirements-video.txt
```

新部署才从`.env.example`创建`.env`，配置`MYSQL_PORT=33066`、`REDIS_PORT=16379`、正确的`MYSQL_USER`/`MYSQL_DATABASE`，为`MYSQL_PASSWORD`和新增的`MYSQL_ROOT_PASSWORD`填写独立密码，`APP_MASTER_KEY`必须恰好32字节。恢复旧数据时保留原主密钥和数据库实际密码。服务用户需能读取`.env`与项目，并能写配置的上传/视频目录；限制`.env`仅服务账户可读。

```bash
docker compose -p web-rag-clipper-full -f docker-compose.yml -f docker-compose.override.yml -f deploy/compose.shared.yml up -d --wait --wait-timeout 180
.venv/bin/python -m alembic current
.venv/bin/python -m alembic upgrade head
```

迁移前先备份，当前数据库head为`0015`，本次扩展升级本身不增加迁移。`deploy/compose.shared.yml`把数据库、向量、Redis和MinIO的宿主端口全部替换为127.0.0.1；`!override`需要Compose2.24.4以上，避免端口列表合并后仍残留开发环境的公网映射。依据[Docker Compose合并规则](https://docs.docker.com/reference/compose-file/merge/)及[端口发布说明](https://docs.docker.com/get-started/docker-concepts/running-containers/publishing-ports/)。本模板只做配置合并验证，未在远端Linux实际启用。

### API与Worker常驻

复制`deploy/web-rag-api.service.example`为`/etc/systemd/system/web-rag-api.service`，替换用户/组和绝对项目路径。复制两份为`web-rag-ingest.service`、`web-rag-video.service`，分别修改Description及ExecStart为：

```ini
ExecStart=/srv/web-rag-clipper-full/.venv/bin/python -m backend.workers.ingest_worker
```

```ini
ExecStart=/srv/web-rag-clipper-full/.venv/bin/python -m backend.workers.video_worker
```

每份文件只保留一个ExecStart。准备服务账户和目录权限后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now web-rag-api web-rag-ingest web-rag-video
sudo systemctl status web-rag-api web-rag-ingest web-rag-video
curl --fail http://127.0.0.1:18001/docs
```

API只绑定127.0.0.1；入库和视频处理各运行一个Worker，不重复开启。新主机资源和云端模型仍需分别验证，视频Groq403问题继续按待办暂缓。

### HTTPS入口

将`deploy/nginx.shared.conf.example`安装至Nginx的http配置上下文（通常为sites-enabled），替换域名与证书路径。模板转发到127.0.0.1:18001，放宽上传至512MB与读取等待600秒，限制新建工作区请求；后端自身仍执行文件、时长和工作区配额限制。

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail https://kb.example.com/docs
```

只有固定HTTPS入口对客户端开放；MySQL、Redis、MinIO、Milvus与18001不应作为公网入口。代理路径及请求体限制参考[Nginx proxy模块](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)与[HTTP核心模块](https://nginx.org/en/docs/http/ngx_http_core_module.html)。

两台电脑都填写相同HTTPS地址并登录同一工作区，检查两边文档列表一致。登录文件负责身份恢复，数据库和上传目录的定期备份仍需要服务器运维单独安排。
