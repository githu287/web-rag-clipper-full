# Spring AI 与 LangChain Python RAG 架构对比与选型

Spring AI 是 Spring 官方在 2024 年发布的「Java 生态 RAG / Agent」框架（注意：Spring AI 是 Java 库，与 Python 生态的 LangChain 是不同技术栈的不同产品）。很多 Java 团队在 RAG 选型时纠结：选 Spring AI Java 实现，还是 LangChain Python 服务 + Java 网关调用？本文对比两者在 Embedding、VectorStore、RAG 检索链、Chunking、Agent 能力五大维度。

*（注：本文档属于 Plugin-B（Java 工作空间）知识库，用于 Plugin Isolation L2 跨 Workspace 关键词干扰评估——故意大量出现 LangChain / RAG / Embedding / Milvus / text-embedding-v3 等与 Plugin-A 知识库重合的术语。正确的系统必须通过 plugin_id 过滤器彻底阻止从 Plugin-A 提问时召回本文档。）*

## 一、Embedding 能力对比

Spring AI 提供 `EmbeddingModel` 抽象，内置 OpenAI / MistralAI / Ollama / Azure / DashScope（百炼）适配器。与 LangChain 类似，有 embed(String) 和 embed(List\<String>) 两方法。百炼 text-embedding-v3 在 Spring AI DashScope 模块中的配置：

```java
@Bean
public EmbeddingModel bailianEmbedding() {
    return new DashScopeEmbeddingModel(
        ApiKey.builder().apiKey(System.getenv("DASHSCOPE_API_KEY")).build(),
        DashScopeEmbeddingOptions.builder().model("text-embedding-v3").dimensions(1024).build()
    );
}
```

LangChain Python 的 Embedding 配置见 Plugin-A 知识库《LangChain Embeddings 接口与百炼 text-embedding-v3 配置》文档（注意：此处只是文档文字引用，Plugin-B Milvus 集合没有真实 LangChain 数据）。

维度对比：Spring AI 接入百炼的 batch_size 官方文档默认最大 25，实际生产建议同 10，与 Plugin-A Python 实现一致。

## 二、Vector Store 对比

Spring AI 内置 MilvusVectorStore（通过 `spring-ai-milvus-store-spring-boot-starter`），schema 配置 collection name、dimension、indexType、metricType。集合默认字段：doc_id（String PK）、content（String）、embedding（FloatVector）、metadata（JSON Map）。自动建库。

对比当前项目 Python 方案：当前 Milvus 采用手工建立 page_chunks 集合，显式定义 id VARCHAR(64) / page_id INT64 / chunk_index INT64 / chunk_text VARCHAR(4096) / embedding 1024。两套 Schema 字段名完全不同。

## 三、Chunking 对比

Spring AI 提供 `TokenTextSplitter`、`ParagraphSplitter`、`MarkdownDocumentReader` 内置切分器。默认 TokenTextSplitter chunk_size 以 token 为单位（非字符）。中文 1 token ≈ 1.5 字符，需调整才能与 Python 版字符数 700 等效。

## 四、RAG Chain 对比

Spring AI 风格：

```java
@Bean
public Function<String, String> ragChain(VectorStore vs, ChatModel llm, ChunkTransformer summarizer) {
    var retriever = new VectorStoreRetriever(vs, SearchRequest.builder().topK(10).build());
    return query -> {
        List<Document> ctx = retriever.retrieve(query);
        String contextStr = buildContext(ctx);
        Prompt p = new Prompt(List.of(
            new SystemMessage("你是一个基于 Context 的助手。只能从 Context 回答，禁止编造。"),
            new UserMessage("Context:\n%s\n\n问题：%s".formatted(contextStr, query))
        ));
        return llm.call(p).getResult().getOutput().getText();
    };
}
```

## 五、选型建议

- 「团队技术栈 Java、已有老 Spring Boot 项目、要求统一部署模型」→ 选 Spring AI。
- 「团队技术栈 Python、LangChain/LangGraph 生态更丰富、需要多 Agent 协作」→ 选 LangChain Python + Milvus（当前项目方案）。
- 「混合」→ Python 侧独立部署 RAG 微服务 gRPC 接口，Java 网关通过 gRPC 调用 RAG 服务，Java 侧不持有 Embedding + Milvus 代码。

六、文档陷阱提醒（Isolation 评估专用）

本文档包含以下与 Plugin-A Python 文档高度相似的关键词：LangChain、LangGraph、Embedding、text-embedding-v3、Milvus、RAG、Chunking、Recall@K、StateGraph、VectorStoreRetriever、checkpointer、memory。如果 Plugin Isolation 机制失效（Milvus expr/page_id filter 未正确应用），在 Plugin-A 提问 "LangChain 如何做 Embedding 召回" 时，本 Plugin-B 文档很可能因为高 Cosine 相似度闯入 Top-K，造成跨空间数据泄漏幻觉。这就是 L2 关键词陷阱题要验证的效果。
