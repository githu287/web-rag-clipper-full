# Hibernate 实体状态与 Flush 机制详解

Hibernate 的 Persistence Context（一级缓存）是 JPA 能自动同步变更的底层原理。不理解实体四种状态就看不懂为什么 save() 没 INSERT、为什么改了字段但没 UPDATE。

## 一、实体四种状态

1. **Transient（瞬态）**：用 new 创建的对象，没有 id、没关联到任何 PersistenceContext。save/persist 之后进入 Managed。
2. **Managed（托管）**：已经关联到 Session / EntityManager。此状态下修改任何 @Column 字段，**不需要显式调用 save**，Hibernate 在事务提交前的 Flush 阶段会自动生成 UPDATE（脏检查 Dirty Checking）。
3. **Detached（游离）**：Session 关闭后，原本 Managed 的对象还在内存里，但不再有 PersistenceContext 跟踪。此时修改字段不会自动同步。需要 merge() 把对象重新变成 Managed（merge 返回的是新对象！不是你传进去的那个）。
4. **Removed（删除中）**：调用 entityManager.remove() 后状态进入 Removed。Flush 时发 DELETE SQL。

关键代码验证：
```java
@Transactional
public void updateName(Long id, String newName) {
    User u = repo.findById(id).orElseThrow();   // Managed
    u.setName(newName);                         // Dirty Flag 标记
    // 这里不需要 repo.save(u)！Hibernate 自动 Flush。
}
```

## 二、一级缓存（PersistenceContext）

同一个 EntityManager 内，两次 find(id) 只会发一条 SQL：第二次从缓存里取。这就是为什么同一个事务里更新后立刻查询能看到最新值，而不是 DB 行。

缓存的负面：批量导入 10000 条对象会撑爆一级缓存。必须每 50 条 batch 手动 flush() + clear()。

```java
@PersistenceContext EntityManager em;

public void batchImport(List<User> users) {
    for (int i = 0; i < users.size(); i++) {
        em.persist(users.get(i));
        if ((i + 1) % 50 == 0) {
            em.flush();
            em.clear();   // 释放 PersistenceContext
        }
    }
}
```

## 三、FlushMode 与触发 Flush 的三种时机

Hibernate 默认 FlushMode=AUTO。触发 Flush 有三个时机：
1. **事务提交前**（最常见）。
2. **执行任何查询之前**（如果 PersistenceContext 中有 dirty，先 flush，保证查询能看到自己的写入）。
3. **显式调用 em.flush()**。

FlushMode.COMMIT：只在事务提交时 flush。查询之前不刷新，性能略高但可能读到旧数据。

## 四、二级缓存（Ehcache / Infinispan / Redis）

跨 EntityManager 共享的缓存。配置：

```yaml
spring:
  jpa:
    properties:
      hibernate:
        cache:
          use_second_level_cache: true
          region.factory_class: org.hibernate.cache.jcache.internal.JCacheRegionFactory
  javax:
    cache:
      provider: org.ehcache.jsr107.EhcacheCachingProvider
```

实体上加 `@Cache(usage = CacheConcurrencyStrategy.READ_WRITE)`。

适用场景：读多写少的字典表。
不适用：写频繁、强一致要求的订单/账户表（缓存穿透/击穿/雪崩三件套风险）。

## 五、常见错误

1. **在事务外修改 Managed 对象后 save** → 事务结束后 Detached 的对象，直接 save 会抛「另一个同 ID 对象已经在 PersistenceContext 中」异常 → 必须用 merge()。
2. **merge() 返回值被忽略** → merge 会把输入对象的字段拷贝到新建 Managed 对象上，但输入对象仍处于 Detached！必须使用返回值。
3. **@OneToMany 级联操作没开 CascadeType.ALL** → 子对象在父对象 save 后没 INSERT。
4. **用 getReferenceById() 后访问除 id 外的字段** → getReferenceById 返回代理，没命中时访问其他属性抛 LazyInitializationException（Session 已关场景尤其多）。
5. **批量 delete 删除后马上查询** → 如果 delete 语句走 JPQL bulk update，不走 remove()，PersistContext 不会同步更新，需要手动 flush+clear，否则缓存里还是旧状态。
