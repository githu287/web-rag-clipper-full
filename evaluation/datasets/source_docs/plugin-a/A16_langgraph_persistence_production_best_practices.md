# LangGraph 状态持久化进阶：Memory、Checkpointer 与生产部署最佳实践

*（注：本文与《LangGraph Memory 与 Checkpointer 持久化方案》属于两篇独立文档。A05 侧重基础 API；本文侧重生产级部署与反模式。两者存在大量共同关键词，专门用于验证检索系统对相似干扰文档的区分能力。）*

## 一、Memory 概念的重新审视

LangGraph 的 Memory 一词其实有两层含义：(1) 狭义的 `MemorySaver` 类，即进程内字典保存的 Checkpointer；(2) 广义的「Agent 在多轮对话中保持上下文」能力——这一层其实是 State.messages 字段 + reducer=operator.add 的效果，与具体 Checkpointer 实现无关。

很多新人混淆：以为启用 MemorySaver 就自动实现了多轮上下文。实际上，多轮上下文的真正来源是：state.messages 始终通过 operator.add 追加，新对话把历史 messages 作为输入 invoke；Checkpointer 的价值是**跨进程恢复**同一个 thread_id，而不是在单次调用里保存历史。

## 二、PostgresSaver 生产部署最佳实践

生产环境强烈推荐 PostgresSaver。关键要点：

1. **表结构初始化**：使用 PostgresSaver.from_conn_string() 首次连接时会自动创建 checkpoints、checkpoint_blobs、checkpoint_writes 三张表；如果用 Alembic 管理 schema，建议把建表 SQL 单独写进迁移脚本，禁用自动建表。
2. **连接池**：传入 SQLAlchemy 的 async_engine，使用 pool_size=20, max_overflow=40。千万不要每个请求新建一次 engine。
3. **写入 TTL**：checkpoint 写量巨大（每步一次）。推荐配置 `ttl_days=7` 或用单独的定时任务删除 7 天前的 checkpoints，否则数据库体积指数增长。
4. **冷热分层**：最近 24 小时活跃 thread_id 的 checkpoint 走 Redis 缓存，冷数据回源 Postgres。

## 三、PostgresSaver vs. RedisSaver 对比

RedisSaver 是 LangGraph 0.2 新加入的 Checkpointer 实现。优点是读写延迟 <1ms、天然支持 TTL、天然支持分布式锁；缺点是不支持时间旅行（get_state_tuple 按 checkpoint_id 回溯）、无持久化保障（Redis 重启丢 checkpoint）。

结论：**对账、金融等需要审计回溯的业务 → PostgresSaver。交互性高、对延迟敏感的聊天机器人 → RedisSaver。**

## 四、Memory 字段的序列化陷阱

Checkpointer 在写 checkpoint 时会对 state 做 `json.dumps`。下列字段会导致静默失败或丢失：

- `datetime.datetime` 未转成 ISO 字符串 → 反序列化后变字符串。
- LangChain 的 `Document` 对象 → 需要用 `document.to_json()` 显式转换。
- Pydantic v2 BaseModel → 默认 model_dump(mode="json") 可以，但自定义类型需要加 json_encoder。
- 自引用循环 dict → `RecursionError`。
- numpy.ndarray → 需要转成 list。

推荐做法：在 CompiledGraph 外面再加一层 SerializationMiddleware，把所有已知非标类型在进入 Graph 前统一转成 JSON-safe 形式。

## 五、Interrupted 但未 Resume 的 Thread 清理

生产运行时间一长，会有大量 Thread 停在 interrupt_before 等待人工审核，但用户可能已经关闭页面永久不会 resume。需要定时任务：每隔 24 小时扫描一次 interrupted > 7 天的 Thread，主动调用 `graph.update_state` 注入 auto_reject 决策，然后 resume 到 END，写入审计日志。否则这些 thread 会无限占据存储空间。

## 六、总结

LangGraph 的状态持久化不是「加上 checkpointer 参数」就万事大吉。它需要序列化、TTL、冷热分层、僵尸 thread 清理、Redis 缓存等一系列工程化配套措施。
