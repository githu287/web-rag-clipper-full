# LangChain Retriever 接口与七类实现对比

Retriever 是 LangChain 定义的「从某种存储检索相关文档片段」的统一接口。所有 Retriever 实现 `get_relevant_documents(query: str) -> list[Document]` 这一同步方法和其异步异步版本 `aget_relevant_documents`。

## 一、VectorStoreRetriever（最常见）

基于向量相似度，由 `vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 5})` 产生。search_type 支持三种：
- `similarity`：纯向量相似度 Top-K。
- `mmr`：最大边缘相关性，平衡相似度与多样性。
- `similarity_score_threshold`：相似度低于 threshold 的全部过滤。

## 二、EnsembleRetriever（混合检索）

融合多路召回。典型用法是 BM25Retriever 与 VectorStoreRetriever 按权重加权：

```python
from langchain.retrievers import EnsembleRetriever
ensemble = EnsembleRetriever(retrievers=[bm25, vs], weights=[0.4, 0.6])
```

内部使用 RRF（Reciprocal Rank Fusion）合并排序。

## 三、MultiQueryRetriever（查询改写）

让 LLM 从用户原问题生成 N 个不同角度的查询，并行检索后去重。适合用户提问措辞模糊的场景。但缺点是 N 倍 Embedding 成本和 N 倍召回耗时。

## 四、ContextualCompressionRetriever（上下文压缩）

在普通 Retriever 之后追加一个 BaseDocumentCompressor。最常用的是 LLMChainExtractor：让 LLM 仅提取文档中与问题直接相关的句子。适合 Retrieved 文档很长但有效信息只占一小部分的情况。

## 五、ParentDocumentRetriever（父子分块）

Embedding 时把文档切成小 chunk（父是大段、子是小段）。检索粒度是子 chunk，但最终返回父 Document。解决「chunk 太小丢上下文，太大召回不准」的经典矛盾。

## 六、SelfQueryRetriever（自查询）

LLM 先把用户问题翻译成「结构化过滤条件 + 语义查询字符串」两部分，再调用 VectorStore 的过滤能力。需要 VectorStore 支持 metadata filtering（Milvus、Chroma、Qdrant 都支持）。

## 七、TimeWeightedVectorStoreRetriever（时间加权）

在 Cosine 相似度上额外乘以时间衰减因子。适合新闻、聊天记录等时效性强的数据。

## 八、选型建议

| 场景 | 推荐组合 |
|------|---------|
| MVP 起步 | VectorStoreRetriever (mmr, k=10) |
| 需要关键词兜底 | EnsembleRetriever(BM25 0.3 + Vector 0.7) |
| 上下文长且杂 | ContextualCompressionRetriever + EmbeddingsFilter |
| FAQ 问答 | MultiQueryRetriever + SelfQuery |
| 跨 chunk 长答案 | ParentDocumentRetriever（父 1500 / 子 300）|

## 九、通用陷阱

1. `k=3` 太小导致漏召回 → 默认最小 k=10。
2. 把 Retriever 直接作为最终上下文，完全不做 Rerank → 至少加 MMR。
3. 忽略 metadata filter 把跨租户数据混进来 → 生产环境默认 filter plugin_id。
4. MultiQueryRetriever 的 LLM 用了和问答相同的模型 → 成本爆炸，用更小更快的模型专门改写。
