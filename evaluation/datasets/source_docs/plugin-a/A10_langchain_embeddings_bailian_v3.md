# LangChain Embeddings 接口与百炼 text-embedding-v3 配置

Embeddings 模块是 RAG 的第一个质量瓶颈。LangChain 提供统一的 `Embeddings` 接口，包含 `embed_documents(texts)` 和 `embed_query(text)` 两个方法。本文以阿里云百炼 text-embedding-v3 为例，说明配置的全部细节。

## 一、接口约定

embed_documents 用于批量处理 chunk，通常一次 10~1000 条。embed_query 用于处理用户检索问句，一次 1 条。两方法必须使用同一模型同一边缘 case 处理逻辑，否则相似度计算会出现系统性偏移。

```python
class Embeddings(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

## 二、百炼兼容模式配置

百炼提供 OpenAI 兼容端点。正确配置：

```python
from langchain_openai import OpenAIEmbeddings

embeddings = OpenAIEmbeddings(
    model="text-embedding-v3",
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    dimensions=1024,  # text-embedding-v3 支持 1024/2048，推荐 1024
)
```

必须显式设置 dimensions。text-embedding-v3 的默认 dimension 是 1024，但一旦 Milvus collection 使用 dimension=1024 建了索引，后续任何改动都需要删库重建 + 重新 Embedding 全量 chunk。

## 三、批大小（batch_size）

百炼 text-embedding-v3 单次请求限制为最多 10 条文本。因此生产代码必须：

```python
EMBEDDING_BATCH_SIZE = 10
all_vecs = []
for i in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
    batch = chunks[i : i + EMBEDDING_BATCH_SIZE]
    all_vecs.extend(embeddings.embed_documents(batch))
```

超过 10 条会返回 400 "The input length exceeds the maximum length" 错误。

## 四、失败与重试

推荐用 tenacity 装饰 embed_documents 方法：对 429/5xx 指数退避最多 5 次，对 400 直接抛错（是输入问题不是服务端）。

## 五、长文本截断

text-embedding-v3 的上下文窗口是 8192 tokens，中文大约 6000 字符。超过的部分会被**静默截断**，不会报错。因此 chunk_size 设置在 3500 字符以内是安全上限。当前项目使用 chunk_size=700 远小于此，所以没问题。

## 六、Query 侧特殊处理

很多项目在 Embed Query 前先拼接「请检索与下面问题相关的文档：」前缀。对于 text-embedding-v3，官方建议**不要**加前缀——它在训练时已经对齐了自然语言问句分布，加前缀反而降低 Recall。

## 七、常见错误

1. 用了 `embed_documents` 来嵌入 query → 两者前处理逻辑不同导致相似性评分系统性偏低。
2. batch_size=16 → 批量失败。
3. dimensions 设为 1536（OpenAI 默认）→ 与 Milvus collection dim=1024 冲突。
4. 前处理去了标点符号 → 改变了文本分布，Embedding 质量下降。
