# Spring Boot 单元测试、集成测试与测试切片

Spring Boot Test 框架对 JUnit 5 做了深度集成，提供多种「测试切片」精确控制启动的上下文范围。选对测试切片可以把测试从分钟级降到毫秒级。

## 一、金字塔分层与对应实现

- **E2E 测试（1%）**：启动全 Spring Context + 真实数据库（Testcontainers）+ MockWebServer 下游，验证完整链路。
- **集成测试（10%）**：启动部分 Spring Context（测试切片）。
- **单元测试（80%+）**：完全不启动 Spring，纯 Mockito 测试 Service 逻辑。

## 二、单元测试：Mockito + JUnit 5

```java
@ExtendWith(MockitoExtension.class)   // 不启动 Spring
class OrderServiceTest {
    @Mock OrderRepository orderRepo;
    @Mock PaymentService paymentService;
    @InjectMocks OrderService orderService;

    @Test
    void createOrder_should_set_status_pending() {
        // Given
        CreateOrderRequest req = new CreateOrderRequest(1L, 2L, BigDecimal.TEN);
        when(orderRepo.save(any(Order.class))).thenAnswer(inv -> inv.getArgument(0));

        // When
        OrderDTO result = orderService.create(req);

        // Then
        assertThat(result.status()).isEqualTo(OrderStatus.PENDING);
        verify(orderRepo).save(any());
        verifyNoMoreInteractions(paymentService);
    }
}
```

## 三、切片测试：@WebMvcTest、@DataJpaTest、@JsonTest

**@WebMvcTest（只加载 Controller 层）**：

```java
@WebMvcTest(OrderController.class)
class OrderControllerTest {
    @Autowired MockMvc mvc;
    @MockBean OrderService orderService;   // 仅替换 Service 为 Mock

    @Test
    void get_should_return_404_when_not_found() throws Exception {
        when(orderService.findById(999L)).thenReturn(Optional.empty());
        mvc.perform(get("/api/v1/orders/999"))
            .andExpect(status().isNotFound())
            .andExpect(jsonPath("$.title").value("Not Found"));
    }
}
```

启动时间通常 <3s，仅加载 Spring MVC 相关 Bean。

**@DataJpaTest（只加载 JPA 层）**：默认使用内存 H2（或 Testcontainers 真实数据库）。Repository 方法测试神器。

```java
@DataJpaTest
@Testcontainers
@AutoConfigureTestDatabase(replace = AutoConfigureTestDatabase.Replace.NONE)
class OrderRepositoryTest {
    @Container
    static MySQLContainer<?> mysql = new MySQLContainer<>("mysql:8.0");

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", mysql::getJdbcUrl);
        r.add("spring.datasource.username", mysql::getUsername);
        r.add("spring.datasource.password", mysql::getPassword);
    }

    @Autowired OrderRepository orderRepo;

    @Test
    void findByStatus_should_filter_correctly() {
        orderRepo.save(sampleOrder(OrderStatus.PAID));
        orderRepo.save(sampleOrder(OrderStatus.CANCELLED));
        assertThat(orderRepo.findByStatus(OrderStatus.PAID)).hasSize(1);
    }
}
```

## 四、E2E 测试：@SpringBootTest + Testcontainers

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Testcontainers
class FullApplicationTest {
    @Autowired TestRestTemplate rest;
    @Container static PostgreSQLContainer<?> pg = new PostgreSQLContainer<>("postgres:16");
    @Container static GenericContainer<?> redis = new GenericContainer<>("redis:7").withExposedPorts(6379);

    @DynamicPropertySource static void props(DynamicPropertyRegistry r) { /* 连接属性 */ }

    @Test
    void placeOrder_fullFlow_should_return_201_with_pending_status() {
        ResponseEntity<OrderDTO> r = rest.postForEntity("/api/v1/orders",
            new CreateOrderRequest(1L, 2L, new BigDecimal("99.9")),
            OrderDTO.class);
        assertThat(r.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        assertThat(r.getBody().status()).isEqualTo(OrderStatus.PENDING);
    }
}
```

## 五、常见错误

1. **所有测试都用 @SpringBootTest(classes=Application.class)** → 每个测试类都启动全 Context 一次，100 个测试类就是 100 次启动，跑 20 分钟。必须按切片分类。
2. **@MockBean 写在 @SpringBootTest 里导致 Context 被标记为 Dirty，缓存失效** → 尽量复用同一 Context，不要滥用 @MockBean。
3. **断言 assertEquals 把 expected 和 actual 写反** → 用 AssertJ 的 assertThat(actual).isEqualTo(expected)，永远 actual 在前。
4. **H2 内存数据库行为与 MySQL 不一致** → 索引、LIMIT、Date 函数、JSON 函数差异巨大。生产 DB 是 MySQL → 集成测试必须用 Testcontainers MySQL 8.0，别用 H2。
5. **测试方法之间依赖执行顺序** → JUnit 5 默认按方法名字典序排序，不是书写顺序。每个方法必须独立：BeforeEach 清库。
6. **使用@Transactional 测试后忘记 rollback** → @DataJpaTest 和 @SpringBootTest 默认自动回滚，除非显式设置 @Commit。这点是 Spring Test 的友好默认。
