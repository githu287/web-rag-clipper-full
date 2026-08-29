# Flyway 数据库版本迁移最佳实践

Alembic 是 Python 生态的数据库迁移工具；对应 Java 生态最成熟的是 Flyway（社区版免费）。它以 SQL 文件为中心，按版本号顺序执行。相比自动建表（Hibernate `ddl-auto=update`），可追溯、可审查、可重复部署。

## 一、命名规范

Flyway 在 `classpath:db/migration/` 目录下扫描 SQL 文件，格式：
`V{VERSION}__{DESCRIPTION}.sql`（版本化迁移，只执行一次）
`U{VERSION}__{DESCRIPTION}.sql`（回滚脚本，pro 版功能）
`R__{DESCRIPTION}.sql`（可重复迁移，每次 checksum 变化就重跑——适合 view/function/procedure）

```
V1__init_schema.sql
V2__create_orders_table.sql
V2.1__add_order_items.sql
V2.2__create_orders_status_idx.sql
R__create_order_summary_view.sql
```

版本号不强制是整数，可以 2 / 2.1 / 2.2 / 3。执行顺序严格按版本号字符串排序（符合语义化版本规则）。

## 二、Flyway 在 Spring Boot 中的集成

Spring Boot 自动配置 Flyway。只需要加 `flyway-core` 依赖，Spring 启动时优先于 Hibernate DDL-auto 执行 migrate。

application.yml 关键配置：
```yaml
spring:
  flyway:
    enabled: true
    locations: classpath:db/migration
    baseline-on-migrate: false   # 新环境必须 false；老项目首次接入才 true
    out-of-order: false          # 禁止乱序。多分支开发合并分支时版本号冲突必须重命名
    validate-on-migrate: true    # 每次 migrate 前 validate 已执行脚本 checksum 是否变了
    clean-disabled: true         # 生产禁用 clean（删所有表），防误杀
```

Hibernate DDL-Auto 在 Flyway 引入后一律设置为 `validate`：只检查 Entity 与表结构一致性，不自动 DDL。任何 Schema 变化都必须写 Flyway 脚本。

## 三、七个常见坑

坑 1：**版本号冲突**。多人开发各自加 V3 脚本 → Flyway 启动抛「Duplicate version」。团队约定：分支名加子版本号。例如 feature/login 用 V3_1__xxx，feature/payment 用 V3_2__xxx。合并时必须统一重命名为 V4。

坑 2：**改了已执行的 V 脚本**。Flyway 检测到 checksum 变了直接启动失败。解法：写新的 V 脚本做 ALTER，不要改历史。

坑 3：**大表 DDL 在高峰期执行**。ALTER TABLE 加列或加索引可能锁表 30 分钟。必须走 pt-online-schema-change / gh-ost / MySQL 8.0 的 Online DDL，Flyway 只负责调用。

坑 4：**多数据源没指定**。Flyway 默认只对 primary DataSource migrate，其他手动创建 Flyway 实例分别 migrate。

坑 5：**把种子数据（reference data）写进 V 脚本**。种子数据可能更新，应该用 R 脚本 + MERGE/UPSERT 语句反复重跑。

坑 6：**Flyway 配置与 Liquibase 混用**。两个都有自己的 metadata 表，顺序冲突。选一个坚持到底。

坑 7：**测试环境没跑迁移就用 Hibernate ddl-auto=create-drop**。测试环境必须完全跑 Flyway 迁移，否则生产与测试 schema 不一致。

## 四、最佳实践清单

1. **所有 Schema 变更必须通过 Flyway**，禁止任何手工 DDL。
2. **CI 中校验**：Pull Request 执行 mvn flyway:validate + Hibernate ddl-auto=validate 双重校验。
3. **回滚策略**：社区版 Flyway 没有 Undo，应采取「每个迁移脚本都可前向兼容」——加列 NOT NULL 必须给 DEFAULT；删列延迟一版本（先 app 不引用，下一版本删）；重命名三步走（add_new→app_switch→drop_old）。
4. **metadata 表独立管理**：Flyway 默认 flyway_schema_history，不要与业务表混。
5. **SQL 脚本末尾加空行 + `;` 分隔符**。每个语句以 `;` 结尾，复杂 procedure 用 DELIMITER。
6. **迁移命名与 Ticket 编号关联**（例如 `V3_17__TICKET-482_add_user_avatar.sql`），方便后续审计「为什么加这一列」。
7. **生产执行前先在 Staging 用真实数据量跑一遍**：验证加索引是否锁表、迁移耗时。
