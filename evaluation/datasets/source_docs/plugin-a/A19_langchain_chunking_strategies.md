# LangChain Chunking 策略对比（Recursive / Semantic / Parent-Child）

Chunking 是 RAG 质量的第一道工序。切得太小丢失上下文，切得太大引入噪声、降低 Embedding 语义密度。本文系统对比三种策略。

## 一、RecursiveCharacterTextSplitter（基准方案）

当前项目使用的方案。按分隔符优先级 `["\n\n", "\n", "。！？；，、", " ", ""]` 递归切分，直至每块 ≤ chunk_size 字符。相邻块尾部 overlap 个字符作为 overlap。

**优点**：可预测、零额外模型调用、确定性结果。
**缺点**：切分点不考虑语义边界，容易把同一个句子的两半分到不同 chunk，或把两个完全无关的段落拼入同一 chunk。

推荐默认参数：chunk_size 500-800 字符（中文），overlap 50-150 字符。当前项目 700/100 是合理基准。

## 二、SemanticChunker（语义切分）

核心思想：用 Embedding 计算相邻句子的向量相似度，相似度骤降的地方作为切分边界。需要每句都做一次 Embedding → 成本是 Recursive 的 10-30 倍。延迟也高得多。

适用场景：chunk 语义纯度对结果极度敏感，且 token 预算充足的场合。

## 三、Parent-Child Chunking（父子分块，当前项目可考虑的下一步优化）

切分两层：
- Child：100-300 字符。用于 Embedding 和检索，精准命中。
- Parent：1500-2500 字符，1 个 Parent 对应若干 Child。

Milvus 中只存 Child 的向量，但每条 Child 额外字段 `parent_id` 指向 Parent。检索时检索 Top-k Child，返回时去重并返回对应 Parent 的完整文本给 LLM。

解决了「既想精准定位句子，又想保留充分上下文」的矛盾。

## 四、MarkdownHeaderTextSplitter（结构感知）

对于 .md 文件，可基于 `# ## ###` 标题层级切分。每一块自动携带所属标题路径 metadata（如 ["A10 LangChain Embeddings", "三、批大小"]）。这极大地帮助 Reranker 和 LLM 判断 chunk 的主题。当前项目使用 ChunkText，完全丢失 Markdown 层级信息——这是已知弱点。

## 五、参数影响灵敏度测试

在一份 100 篇技术文档的数据集上观察到：
- chunk_size 从 500 → 2000：Recall@5 下降 15%-22%，但 Context 冗余度下降 35%。
- overlap 从 0 → 200：跨 chunk 问题 Recall@5 提升 9%-14%，但 Milvus 总 chunk 数增长 18%。
- Semantic 比 Recursive 在多段信息类问题上 Recall@5 提升 6%-10%，但成本高。

## 六、选型建议

**当前 Baseline（Recursive 700/100）适合作为起点**。后续优化按成本顺序：(1) 加 Markdown 层级 metadata（低成本）；(2) 改 Parent-Child（中等成本，需改 Milvus Schema 支持 parent_id）；(3) 引入 SemanticChunker 只对长文档使用（高成本）。
