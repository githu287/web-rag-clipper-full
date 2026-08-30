# Web RAG Clipper

网页内容剪藏 + RAG 知识库问答系统。通过 Chrome 扩展采集网页正文，构建个人知识库，支持语义检索和 AI 问答。

## 核心能力

| 能力 | 说明 |
|------|------|
| Chrome 扩展 Side Panel | 网页剪藏、聊天式问答、知识库管理，一站式体验 |
| Plugin Workspace 多租户 | 独立身份（`X-Plugin-ID` + `X-Plugin-Secret`）、独立 API Key、数据隔离 |
| 文档全链路自动化 | 上传/剪藏 → 解析 → 切块 → 向量化 → 入库，一步到位 |
| RAG 检索 + 问答 | Milvus 语义检索 + 百炼 qwen-plus 生成回答，返回答案与来源引用 |
| Docker Compose 一键启动 | MySQL / Redis / etcd / MinIO / Milvus 五大基础设施 |

> 技术栈、系统架构、API 参考、数据模型等详见 [ARCHITECTURE.md](ARCHITECTURE.md)

## 快速开始

### 前置要求

- Docker Desktop
- Python 3.11+
- 阿里云百炼 API Key（`text-embedding-v3` + `qwen-plus`）

### 1. 启动基础设施

```powershell
docker compose up -d
```

| 服务 | 宿主端口 | 说明 |
|------|----------|------|
| MySQL 8.0 | `33066` | 容器内 3306，数据库 `rag_clipper` |
| Redis 7 | `6379` | 当前预留 |
| etcd v3.5.5 | `2379` | Milvus 元数据 |
| MinIO | `9000` / `9001` | Milvus 对象存储 |
| Milvus v2.4.4 | `19530` | 向量数据库 |

### 2. 配置环境变量

```powershell
copy .env.example .env
```

编辑 `.env`，至少修改：

| 变量 | 说明 |
|------|------|
| `BAILIAN_API_KEY` | 百炼 API Key |
| `MYSQL_PORT` | 使用本 compose 时填 `33066` |
| `APP_MASTER_KEY` | **必填**。AES-256-GCM 主密钥，用于加密用户 API Key |

生成 `APP_MASTER_KEY`（32 个 hex 字符）：

```powershell
python -c "import secrets; print(secrets.token_hex(16))"
```

### 3. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt
```

### 4. 初始化数据库

```powershell
alembic upgrade head
```

### 5. 启动 API

```powershell
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

启动后访问 <http://localhost:8000/docs> 查看 Swagger UI。

### 6. 加载 Chrome 扩展

1. Chrome 打开 `chrome://extensions/`，开启 **Developer mode**
2. 点击 **Load unpacked**，选择本项目 `extension/` 目录
3. 点击工具栏插件图标，打开 Side Panel
4. Settings → 注册 Workspace → 配置百炼 API Key
5. 打开任意网页 → 点击剪藏按钮 → 文档入库
6. 切换到 Chat → 提问，验证 RAG 问答返回答案和来源

## 使用流程

```
注册 Workspace → 配置百炼 API Key → 剪藏网页 / 上传文件 → 自动切块向量化 → RAG 问答
```

### Side Panel 页面

| 页面 | 功能 |
|------|------|
| **Chat** | 聊天式 RAG 问答，支持"当前网页"和"全部知识库"两种检索模式 |
| **Library** | 知识库管理：浏览 / 搜索 / 删除文档，支持上传 `.txt` / `.md` / `.markdown` 文件 |
| **Settings** | Workspace 信息、百炼 API Key 配置 |
| **欢迎页** | 首次使用引导注册 Workspace |

## 测试

在运行测试前请先确保：基础设施（Docker Compose）已启动、`.env` 已配置、数据库已初始化（`alembic upgrade head`）。

### 运行方式

**Windows（PowerShell）：**
```powershell
.venv\Scripts\python.exe -m pytest backend/tests -v
```

**类 Unix（macOS / Linux / WSL）：**
```bash
.venv/bin/python -m pytest backend/tests -v
```

### 推荐首次运行：快速冒烟测试（≤ 30 秒）
第一次贡献代码时，建议先跑核心链路冒烟测试，无需等全量跑完：
```powershell
.venv\Scripts\python.exe -m pytest backend/tests -v -k "smoke or rag or ingest"
```

### 测试覆盖范围
`backend/tests/` 目录按层次组织，覆盖以下核心领域（具体用例数随代码演进，以 pytest 运行输出为准）：

| 类别 | 说明 |
|------|------|
| 单元测试 | Plugin Workspace、API Key 加解密、Chunker 切分、Document 状态机等纯逻辑 |
| 集成测试 | Documents Ingest 全链路（上传 → 切块 → Embedding → Milvus + MySQL 入库） |
| RAG 测试 | Milvus 检索（current / all 两种模式）、RagService 5 层隔离、Sources 返回、Answer Prompt 构造 |
| API 测试 | 各 REST 端点的输入校验、权限 Header（X-Plugin-ID / X-Plugin-Secret）、错误响应码（404/409/401） |

> 所有 RAG 相关的**质量评估**（Recall@K / Hallucination / Plugin Isolation 等基线指标）属于独立的评估体系，不放在本章节，请详见 [evaluation/datasets/DATASET_MANIFEST.md](evaluation/datasets/DATASET_MANIFEST.md)。

## 已知限制

- 仅支持纯文本：`.txt` / `.md` / `.markdown`（PDF / DOCX / OCR 未实现）
- ingest 为同步处理，无异步任务队列
- 扩展正文提取为轻量 MVP，强 JS 渲染 / 分页 / paywall 页面可能不完整
