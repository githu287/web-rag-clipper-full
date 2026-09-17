# 真实基线运行手册（含 APP_MASTER_KEY 修复）

> 适用于 Phase 3.7 评估环境。前提：Docker 全服务 green，FastAPI 后端以
> `uvicorn backend.main:app` 运行在 `127.0.0.1:8000`。

## 1. 当前阻塞：根因

`/rag/search` 会调用 `PluginService.decrypt_api_key` 解密插件 Workspace 里
**已加密存储的百炼 API Key**（`plugin_workspaces.api_key_ciphertext/nonce`，
AES-256-GCM，密钥为 `.env` 中的 `APP_MASTER_KEY`）。

- 根 `.env` 当前 **缺少 `APP_MASTER_KEY`** → 服务端报
  `SecurityConfigurationError: APP_MASTER_KEY must be exactly 32 bytes, got 0 bytes`，所有请求 HTTP 500。
- 曾尝试从 `f7` 遗留的 35 字节残 key（`.env.f7r_keys` 第 1 行）枚举删除 3 字符
  （6545 组合）对两个 eval 插件密文逐一做 GCM tag 校验，**全部 NO_MATCH**，原 key 不可恢复。
- 结论：必须走 **方案 A（找回原 key）** 或 **方案 B（轮换 key + 重配插件 Key）**。

涉及的 eval 插件（来自 `evaluation/private/credentials.json`）：
- `JV34VHguPWEN_we5wcF_0YuREeCAC1NGL-POlYItyEA`（RAG Eval Plugin-A Python）
- `bJbMbMFxHaSTIlTLMMUANcPOwo0uQkRFKDdxmzCmIFg`（RAG Eval Plugin-B Java）

## 2. 通用前置步骤

1. Docker 服务须 running：`docker compose ps`（mysql/milvus/attu/redis 等全 up）。
2. 后端以项目根目录为工作目录启动，使其加载根 `.env`（见第 4 节重启命令）。
3. 连通性统一使用 `http://127.0.0.1:8000`，**不要用 `localhost`**
   ——本机 `localhost` 可能被 attu / wslrelay 的 IPv6 监听抢先命中，导致误判 404。
   健康检查：
   ```powershell
   (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/openapi.json').StatusCode   # 期望 200
   ```

## 3. 方案 A：找回原 APP_MASTER_KEY（零 DB 变更，优先）

若你仍持有当初加密插件 Key 时使用的 32 字节 `APP_MASTER_KEY`
（或含它的 `.env` 备份），直接在根 `.env` 补齐即可：

```powershell
# .env 追加一行（示例，切勿直接使用此占位值）：
# APP_MASTER_KEY=<原 32 字节 ASCII key>
```

校验 key 是否正确（能解密现有密文才算对），替换下面 key 后执行：

```python
# 临时文件 _verify_key.py（用完即删）
import base64, subprocess
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
KEY = "在此填入32字节候选key"
out = subprocess.run(["docker","exec","rag-clipper-mysql","mysql","-uroot","-proot_password",
    "-D","rag_clipper","-N","-e",
    "SELECT api_key_ciphertext, api_key_nonce FROM plugin_workspaces "
    "WHERE plugin_id='JV34VHguPWEN_we5wcF_0YuREeCAC1NGL-POlYItyEA'"],
    capture_output=True, text=True)
ct, n = out.stdout.strip().splitlines()[0].split("\t")
try:
    pt = AESGCM(KEY.encode()).decrypt(base64.b64decode(n), base64.b64decode(ct), None)
    print("MATCH, plaintext_len=", len(pt), "sk_prefix=", pt.startswith(b"sk-"))
except Exception:
    print("NO_MATCH")
```

验证通过后重启后端（第 4 节），再执行第 6 节验证；密文无需任何改动。

## 4. 重启后端（方案 A / B 均需要）

先杀掉当前 uvicorn（`127.0.0.1:8000` 上的 python 进程）：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'uvicorn backend\.main:app' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

再用项目根目录作为工作目录启动（确保加载根 `.env`，python 路径按本机调整）：

```powershell
Start-Process -FilePath 'D:\pyenv\pyenv-win\versions\3.11.9\python.exe' `
  -ArgumentList '-m','uvicorn','backend.main:app','--host','0.0.0.0','--port','8000' `
  -WorkingDirectory 'D:\杂\项目\web-rag-clipper-full' -WindowStyle Hidden
Start-Sleep -Seconds 5
(Invoke-WebRequest -Uri 'http://127.0.0.1:8000/openapi.json').StatusCode   # 期望 200
```

## 5. 方案 B：轮换 key + 重配插件 Key（原 key 不可得时）

改动范围：`.env` 新增 `APP_MASTER_KEY`；通过官方 API 为两个 eval 插件
重新配置百炼 Key（覆盖旧密文，**不需要手改 DB**）。重配所用明文取根 `.env`
的 `BAILIAN_API_KEY`（须为你想让评估实际使用的百炼 Key，且当前有效）。

### 5.1 生成并写入新 key（恰好 32 字节，不打印确认内容）

```powershell
$k = & 'D:\pyenv\pyenv-win\versions\3.11.9\python.exe' -c "import secrets; print(secrets.token_hex(16))"
Add-Content -Path '.env' -Value "APP_MASTER_KEY=$k"
# 可校验行数/长度（不打印值）：
& 'D:\pyenv\pyenv-win\versions\3.11.9\python.exe' -c "print([len(l.split('=',1)[1].strip()) for l in open('.env') if l.startswith('APP_MASTER_KEY=')])"  # [32]
```

### 5.2 重启后端（见第 4 节）

### 5.3 为两个 eval 插件重新配置百炼 Key

从 `evaluation/private/credentials.json` 取每个插件的 `plugin_secret` 填入下方
`$secret`，逐插件执行：

```powershell
$headers = @{
  'Content-Type'  = 'application/json'
  'X-Plugin-ID'   = 'JV34VHguPWEN_we5wcF_0YuREeCAC1NGL-POlYItyEA'
  'X-Plugin-Secret' = '从credentials.json复制'
}
$apiKey = (& 'D:\pyenv\pyenv-win\versions\3.11.9\python.exe' -c "import re,os; m=re.search(r'^BAILIAN_API_KEY=(.+)$', open('.env',encoding='utf-8').read(), re.M); print(m.group(1).strip())")
$body = @{ api_key = $apiKey } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri 'http://127.0.0.1:8000/plugins/me/api-key' -Headers $headers -Body $body
# 期望：plugin_id=... api_key_configured=True
```

插件 B（`bJbMbMFxHaSTIlTLMMUANcPOwo0uQkRFKDdxmzCmIFg`）重复一次，
`X-Plugin-Secret` 换为其在 credentials.json 中对应的值。

> 注意：`PUT /plugins/me/api-key` 会用该 key 先做一次最小 embedding 验证
> （失败返回 400），随后才加密入库。若密钥已过期会在这里直接暴露。
> 此操作同样会让历史遗留的 `users` 表旧密文（若有，属 Auth 功能，不在本基线范围）
> 变为不可解，与插件无关，无需处理。

### 5.4 验证

```powershell
$h = @{ 'X-Plugin-ID'='JV34VHguPWEN_we5wcF_0YuREeCAC1NGL-POlYItyEA'; 'X-Plugin-Secret'='从credentials.json复制' }
Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8000/plugins/me' -Headers $h   # api_key_configured: True
```

建议先做一次含 warmup 的极小子集冒烟（warmup 会产生真实 embedding，能提前暴露
key 无效 / 网络问题），确认无 500/502/503 后再跑全量基线。

## 6. 运行真实基线（修复完成后执行）

```powershell
# 1) 增强版 Baseline（生成 JSON）—— 用 127.0.0.1 规避 localhost 坑
& 'D:\pyenv\pyenv-win\versions\3.11.9\python.exe' -m evaluation.run_baseline `
  --credentials evaluation/private/credentials.json `
  --dataset evaluation/aligned-document/rag_eval.jsonl `
  --top-k 5 --warmup-runs 3 --max-retries 3 `
  --base-url http://127.0.0.1:8000 `
  --output evaluation/reports/retrieval-baseline.json

# 2) 渲染 Markdown 报告
& 'D:\pyenv\pyenv-win\versions\3.11.9\python.exe' -m evaluation.render_report `
  --input evaluation/reports/retrieval-baseline.json `
  --output evaluation/reports/retrieval-baseline.md
```

## 7. 常见失败对照

| 现象 | 含义 / 处理 |
| --- | --- |
| `/openapi.json` 404，但 `127.0.0.1` 可通 | `localhost` 命中 attu/wslrelay，一律改用 `127.0.0.1` |
| case 500 `APP_MASTER_KEY must be exactly 32 bytes` | `.env` 缺 key / 未重启，见 §3/§5 |
| case 500 `SecurityDecryptionError`（wrong key 类） | key 非当初加密所用，走方案 B 重配 |
| 重配返回 400 `ApiKeyValidationError` | `BAILIAN_API_KEY` 已失效或非 `sk-` 前缀，需提供有效百炼 Key |
| case 状态 200 但 `retrieved=[]` | Milvus 中无该插件文档向量，与密钥无关，单独排查 ingestion |
