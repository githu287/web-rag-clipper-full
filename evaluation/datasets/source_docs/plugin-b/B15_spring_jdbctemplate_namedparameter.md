# Spring JdbcTemplate 与 NamedParameterJdbcTemplate 实战

当 SQL 非常复杂（多层子查询、Window 函数、Reporting SQL），JPA 力不从心。Spring JdbcTemplate 提供接近原生 JDBC 的控制力，但免除了 DriverManager、getConnection、try-with-resources、SQLException 转译的样板代码。

## 一、JdbcTemplate 五类核心用法

```java
@Repository
@RequiredArgsConstructor
public class OrderReportRepository {
    private final JdbcTemplate jdbc;

    // 1. 查询单个对象 queryForObject
    public Long countByStatus(OrderStatus s) {
        return jdbc.queryForObject(
            "SELECT COUNT(*) FROM orders WHERE status = ?",
            Long.class,
            s.name()
        );
    }

    // 2. 查询对象列表 query
    public List<OrderReportDTO> reportForMonth(YearMonth ym) {
        String sql = """
            SELECT DATE(created_at) AS day, status, COUNT(*) AS cnt, SUM(amount) AS amount
            FROM orders WHERE created_at BETWEEN ? AND ?
            GROUP BY DATE(created_at), status
            ORDER BY day, status
        """;
        return jdbc.query(sql,
            (rs, rowNum) -> new OrderReportDTO(
                rs.getDate("day").toLocalDate(),
                OrderStatus.valueOf(rs.getString("status")),
                rs.getLong("cnt"),
                rs.getBigDecimal("amount")
            ),
            ym.atDay(1).atStartOfDay(),
            ym.atEndOfMonth().atTime(23, 59, 59)
        );
    }

    // 3. 查询 Map 列表 queryForList
    public List<Map<String, Object>> rawList(Long userId) {
        return jdbc.queryForList("SELECT * FROM orders WHERE user_id = ? LIMIT 10", userId);
    }

    // 4. 增删改 update
    public int updateStatus(Long orderId, OrderStatus newStatus) {
        return jdbc.update("UPDATE orders SET status = ?, updated_at = NOW() WHERE id = ?",
            newStatus.name(), orderId);
    }

    // 5. 批量操作 batchUpdate
    public int[][] batchInsert(List<OrderItem> items) {
        return jdbc.batchUpdate(
            "INSERT INTO order_items(order_id, sku_id, qty, price) VALUES (?,?,?,?)",
            items,
            50,
            (ps, item) -> {
                ps.setLong(1, item.orderId());
                ps.setString(2, item.skuId());
                ps.setInt(3, item.qty());
                ps.setBigDecimal(4, item.price());
            }
        );
    }
}
```

## 二、NamedParameterJdbcTemplate（可读性强）

JdbcTemplate 用 ? 占位符当 SQL 参数很多（>8）时顺序容易错。NamedParameter 版本用 `:paramName`：

```java
private final NamedParameterJdbcTemplate npJdbc;

public List<Order> search(SearchQuery q) {
    MapSqlParameterSource params = new MapSqlParameterSource()
        .addValue("statuses", q.statuses().stream().map(Enum::name).toList())   // IN 子句
        .addValue("minAmount", q.minAmount(), Types.DECIMAL)
        .addValue("userId", q.userId());

    String sql = """
        SELECT * FROM orders
        WHERE user_id = :userId
          AND status IN (:statuses)
          AND amount >= COALESCE(:minAmount, amount)
        ORDER BY created_at DESC
        LIMIT 50
    """;
    return npJdbc.query(sql, params, BeanPropertyRowMapper.newInstance(Order.class));
}
```

BeanPropertyRowMapper 根据列名 → Java Bean 属性名（驼峰转换）自动映射，适合 DTO 查询。

## 三、与 JPA 混合使用场景

推荐规则：**写操作用 JPA（利用脏检查自动 UPDATE），复杂查询用 JdbcTemplate**。两者共用同一个 DataSource + TransactionManager，在同一个 @Transactional 里完全兼容。JdbcTemplate 的变更 JPA flush 时可见；JPA 的 em.flush 之后 JdbcTemplate 也能读到。

## 四、常见错误

1. **queryForObject 查无结果直接抛 EmptyResultDataAccessException** → 必须 try-catch 或改用 query.stream().findFirst()。
2. **IN 子句用字符串拼接** → 用 NamedParameter + List 参数，Spring 自动展开，防止 SQL 注入。
3. **batchUpdate 不加 batchSize** → 默认一条一条发 DB，性能差。
4. **RowMapper 用字段下标而不是列名** → SQL 加列后顺序错位，维护噩梦。
5. **LocalDateTime/LocalDate 直接 setObject** → 某些老驱动需要显式 `Types.TIMESTAMP WITH TIME ZONE`。
6. **结果集超大用 RowMapper 一次性全存内存** → 用 `query(sql, resultSetExtractor)` 流式处理。
