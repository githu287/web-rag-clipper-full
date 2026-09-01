# LangChain Chunking 策略对比（Recursive / Semantic / Parent-Child）

Chunking 是 RAG 质量的第一道工序。切得太小丢失上下文，切得太大引入噪声、降低 Embedding 语义密度。本文系统对比三种策略。

## 一、RecursiveCharacterTextSplitter（基准方案）

当前项目使用的方案。按分隔符优先级 `["\n\n", "\n", "。！？；，、", " ", ""]` 递归切分，直至每块 ≤ chunk_size 字符。相邻块尾部 overlap 个字符作为 overlap。

**优点**：可预测、零额外模型调用、确定性结果。
**缺点**：切分点不考虑语义边界，容易把同一个句子的两半分到不同 chunk，或把两个完全无关的段落拼入同一 chunk。

推荐默认参数：chunk_size 500-800 字符（中文），overlap 50-150 字符。当前项目 700/100 是合理基准。

### 中文字符编码陷阱

`RecursiveCharacterTextSplitter` 的 `chunk_size` 默认按 Python 字符数计算，而不是按 UTF-8 字节数计算。中文字符通常占 3 个 UTF-8 字节，因此 `chunk_size=700` 表示最多约 700 个中文字符，而不是 700 字节。存入向量库前仍应单独检查目标字段的字节或字符上限，不能把切分器的字符数配置直接当成存储层容量保证。

中文文本不像英文主要依靠空格分词。分隔符应按从粗到细的边界排列：`["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]`。也可以把连续中文标点作为同一策略层理解，但在当前自实现切分器中它们是依次尝试的独立 separator。中文句读必须排在英文空格和空字符串之前，否则长中文段落容易退化为按字符硬切。

### 语义块合并与 overlap

`chunk_overlap=100` 是字符级兜底，不天然等于 100 个完整语义字符。为了避免把逻辑关联词或完整句子切断，生产实现可增加 `smart_merge_overlap` 策略：

1. 优先在同一段落内产生 overlap，不跨越空行强行拼接无关段落。
2. overlap 起点向前对齐到最近的句号、问号、感叹号或分号边界。
3. 如果边界落在“但是、因此、然而”等关联词附近，将关联词及其完整句子保留在同一个 Chunk 中。

当前 Baseline 仍冻结为确定性的字符级 700/100；上述语义合并只能作为后续对照实验，不能在同一份 Baseline 中悄悄启用。

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
