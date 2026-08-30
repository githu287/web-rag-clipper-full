# Spring Data JPA 核心用法与 N+1 问题解决

Spring Data JPA 是对 Jakarta Persistence API（JPA，原 Java Persistence API）的高层封装，基于 Hibernate 实现。它通过接口方法名自动生成 SQL，极大简化了 CRUD。

## 一、三层接口

- **Repository\<T, ID>**：最顶层标记接口，只有 save / findById / findAll / deleteById 等基础方法。
- **CrudRepository\<T, ID>**：继承 Repository，提供完整增删改查。
- **JpaRepository\<T, ID>**：继承 CrudRepository + PagingAndSortingRepository。额外包含 flush()、saveAndFlush()、findAll(Sort)、findAll(Pageable)、deleteInBatch() 等 JPA 特有方法。

业务代码通常继承 JpaRepository：

```java
public interface OrderRepository extends JpaRepository<Order, Long> {
    List<Order> findByStatusAndCreatedAtBetween(OrderStatus status, LocalDateTime from, LocalDateTime to);
    Page<Order> findByUserId(Long userId, Pageable pageable);
    Optional<Order> findFirstByUserIdOrderByCreatedAtDesc(Long userId);
    boolean existsByOrderNo(String orderNo);
}
```

方法名解析规则：`findBy`/`countBy`/`existsBy`/`deleteBy` + 属性名 + (`And` / `Or`) + (`LessThan` / `Between` / `Like` / `Containing` / `IgnoreCase` 等)。Spring Data 在启动期解析并生成实现。

## 二、@Query：复杂 JPQL 与原生 SQL

```java
@Query("""
    SELECT o FROM Order o
    LEFT JOIN FETCH o.items
    WHERE o.userId = :userId AND o.status = 'PAID'
""")
List<Order> findPaidOrdersWithItems(@Param("userId") Long userId);
```

原生 SQL：`@Query(value = "SELECT ...", nativeQuery = true)`。分页必须额外加 `countQuery`。

## 三、N+1 查询问题（JPA 第一大坑）

查询 1 条订单 → 懒加载 Order.items，循环访问 1 条订单的 items 时每条再发 1 条 SQL，产生 N+1 条 SQL。

危害：原本预期 1 条 SQL，实际产生 101 / 1001 条，延迟 10-100 倍增长，DB 连接池瞬间耗尽。

**三种解法**：
1. **JOIN FETCH**（最常用）：`@Query("SELECT o FROM Order o LEFT JOIN FETCH o.items WHERE ...")`。一次性取回集合。缺点：不能与 Pageable 分页直接联合用（Hibernate 会发内存分页警告）。
2. **@EntityGraph**：定义命名 EntityGraph，声明属性路径一起加载。
3. **Batch Size**：`@BatchSize(size = 50)`（Hibernate 特有注解）。访问第一对象未加载集合后，Hibernate 一次 in(...) 拉 50 个对象的集合，N+1 变成 N/50 + 1。缺点：仍有 2+ 次查询，但可控。

## 四、@Transactional 传播行为

Spring Data JPA Repository 的方法默认 REQUIRED 传播级别。关键点：

1. **读操作**必须加 `@Transactional(readOnly = true)` → Hibernate 跳过 Dirty Check、FlushMode=MANUAL、DB 只读副本路由（如果配了）。
2. **写操作默认 REQUIRED**。不要在 Controller 上打事务注解——事务边界应该在 Service 层。
3. **REQUIRES_NEW**：当前方法挂起外部事务独立提交（如日志记录不能因为主事务回滚而丢失）。
4. **NESTED**（仅 DataSource 支持 savepoint 时生效）：嵌套事务。

## 五、常见错误

1. 误以为「方法名没写对」抛异常 → Spring Data 启动阶段 Method Name Parse 就会抛异常，但拼写错误的属性名（例如把 status 写成 state）可能在启动期不报错，需要 `@EnableJpaRepositories(repositoryBaseClass = ..., bootstrapMode = BootstrapMode.LAZY)` 并加测试覆盖。
2. 分页里用 JOIN FETCH → 产生「firstResult/maxResults specified with collection fetch」警告，返回的 page.totalElements 不是预期值。必须改成两条查询：主查询分页 ID + 第二条用 IN + JOIN FETCH 拉详情。
3. 事务方法互相调用走 this.xxx() 而不是代理 → AOP 失效，事务不起作用（常见坑：同 Service 内一个事务方法调用另一个事务方法不生效）。解决方案：注入自身 @Lazy，或把方法提取到另一个 Service。
4. 在事务中做外部 HTTP 调用 → 事务开太久，DB 锁持有时长不可控。应当：先做 HTTP 调用，再进事务方法只做 DB 操作。
