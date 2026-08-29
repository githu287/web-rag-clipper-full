# Spring 事务管理：@Transactional 九大陷阱与最佳实践

声明式事务 `@Transactional` 是 Spring 最常用的功能，但也是隐藏坑最多的功能。本文系统总结九大类常见陷阱和七条最佳实践。

## 一、传播行为（Propagation）定义

Propagation.REQUIRED（默认）：已有事务就加入，没有就新建。99% 业务场景正确。
Propagation.REQUIRES_NEW：挂起当前事务，新起独立事务。适合「审计日志必须落库，不管主事务成败」。
Propagation.NESTED：savepoint 嵌套事务，仅 DataSource 支持 savepoint。
Propagation.MANDATORY：必须在已有事务内调用，否则抛异常。用于断言底层 service 不能直接调用。
Propagation.NEVER：必须不在事务内，否则抛异常。
Propagation.NOT_SUPPORTED：以非事务方式执行，挂起当前事务。适合长 SQL 查询避免持锁。
Propagation.SUPPORTS：有事务就加入，没有就非事务（慎用：查询是否走只读连接取决于外部环境）。

## 二、隔离级别（Isolation）

- READ_UNCOMMITTED：允许脏读，几乎不用。
- READ_COMMITTED：Oracle / Postgres / SQL Server 默认。只能看到已提交的数据。
- REPEATABLE_READ：MySQL InnoDB 默认。同一事务内多次读同一行结果一致。MySQL 使用 MVCC + Gap Lock 解决幻读。
- SERIALIZABLE：最高级别，全串行化。性能极低，只在金融强一致场景偶用。

## 三、九大陷阱

陷阱 1：**方法不是 public**。Spring AOP 基于动态代理，private/protected/package-private 方法调用不会走代理 → @Transactional 不生效。IDE 的 Spring 插件会有警告，但很多人忽略。

陷阱 2：**自调用（同 Service 内用 this.xxx() 调用另一个事务方法）** → 不走代理，事务失效。解：@Lazy 注入自身；或把另一个方法提取到独立 Service。

陷阱 3：**默认只回滚 RuntimeException 和 Error**。检查异常（如 IOException）不会回滚。解：`@Transactional(rollbackFor = Exception.class)`。

陷阱 4：**try-catch 吞了异常后事务不知晓** → 不回滚。解：catch 内设置回滚 `TransactionAspectSupport.currentTransactionStatus().setRollbackOnly()`；或重新抛出运行时异常。

陷阱 5：**final 方法 / final 类** → CGLIB 无法生成子类代理，事务失效。Spring 6 引入 AOT 后更严格。

陷阱 6：**@Transactional 打在 Controller 方法上** → 事务开得太早，包含参数校验、外部 HTTP。应在 Service 层方法上打。

陷阱 7：**线程池里调用 @Transactional 方法** → `TransactionalTemplate` 内绑定的是 ThreadLocal，异步线程拿不到事务上下文。解：在 runnable 开头手动开启 PlatformTransactionManager。

陷阱 8：**数据库引擎不支持事务（MyISAM）** → @Transactional 看起来生效但其实没 COMMIT/ROLLBACK。MySQL 必须确认表是 InnoDB。

陷阱 9：**多数据源没指定 TransactionManager** → 回滚了错误数据源的事务。解：`@Transactional("orderTransactionManager")` 显式指定。

## 四、七条最佳实践

1. 默认加 `@Transactional(rollbackFor = Exception.class)`。
2. 只在 Service 层 Public 方法上打注解，粒度尽量小。
3. 读方法强制 `@Transactional(readOnly = true)`。
4. 不要在事务方法内做外部 RPC、文件 IO、缓存操作。
5. 使用 TransactionTemplate 处理细粒度事务边界。
6. REQUIRES_NEW 用在独立的 Service 类中，避免与主事务类混淆 self-invocation。
7. 写单元测试验证回滚：`@DataJpaTest @Transactional(defaultRollback = true)` 或手动 assert 数据未落库。
