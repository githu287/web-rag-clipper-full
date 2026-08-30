# Spring WebFlux 响应式 Web 开发核心概念

Spring WebFlux 是 Spring 5 引入的响应式 Web 框架，目标是解决传统 Servlet 模型在线程资源上的瓶颈。它基于 Reactor（Mono/Flux）实现非阻塞。

## 一、与 Spring MVC 的根本区别

Spring MVC 构建在 Servlet API 之上，每个请求 1 个线程。当调用下游服务或查数据库阻塞时，这个线程就被占着不能复用。I/O 密集型服务的线程池通常 200-500 线程，线程切换开销和内存占用都高。

Spring WebFlux 基于事件循环模型，少量线程（= CPU 核数×2）处理所有请求，I/O 操作发起后线程立刻释放，I/O 完成后通过事件回调继续业务逻辑。线程数稳定 16-32，内存占用低一个数量级。

## 二、两种技术栈

WebFlux 支持两种服务器：
1. **Servlet 3.1+ 容器**（Tomcat/Jetty）：利用异步 Servlet 的非阻塞 API。
2. **Reactor Netty / Undertow**：原生响应式服务器，Spring Boot WebFlux Starter 默认。

Controller 编程模型与 MVC 共享 @GetMapping / @RestController 注解，代码迁移成本低。

## 三、响应式控制器示例

```java
@RestController
@RequestMapping("/api/v2/users")
public class UserReactiveController {
    private final ReactiveUserRepository repo;

    @GetMapping("/{id}")
    public Mono<ResponseEntity<UserDTO>> get(@PathVariable Long id) {
        return repo.findById(id)
            .map(UserDTO::fromEntity)
            .map(ResponseEntity::ok)
            .defaultIfEmpty(ResponseEntity.notFound().build());
    }

    @GetMapping
    public Flux<UserDTO> list() {
        return repo.findAll().map(UserDTO::fromEntity);
    }

    @PostMapping
    public Mono<ResponseEntity<UserDTO>> create(@Valid @RequestBody Mono<CreateUserRequest> bodyMono) {
        return bodyMono
            .flatMap(body -> repo.save(new User(body.name(), body.email())))
            .map(saved -> ResponseEntity.status(HttpStatus.CREATED).body(UserDTO.fromEntity(saved)));
    }
}
```

关键：所有返回类型都是 Mono\<X> 或 Flux\<X>。控制器方法内部绝对禁止 block() 调用，否则立即抛 IllegalStateException。

## 四、R2DBC：响应式数据库访问

WebFlux + JDBC 是「伪响应式」，因为 JDBC 本身阻塞。必须用 R2DBC（Reactive Relational Database Connectivity）：

```java
interface ReactiveUserRepository extends ReactiveCrudRepository<User, Long> {}
```

支持的数据库：PostgreSQL、MySQL（MariaDB 也有驱动）、H2、MSSQL、Oracle。

## 五、常见陷阱

1. **误用 MVC 的阻塞调用**：Controller 里调用 restTemplate.getForObject() 或 repository.findAll() 同步方法 → 阻塞 Netty EventLoop，性能比 MVC 还差。必须全部改为 Reactive 版本（WebClient / R2DBC）。
2. **订阅到错误线程**：`.subscribeOn(Schedulers.boundedElastic())` 只应该用于包装遗留阻塞代码，正常 Reactive 链不要乱加。
3. **Flux 流未背压控制**：大数据量查询时下游消费慢导致上游爆内存。需要 `.onBackpressureBuffer(1000, BufferOverflowStrategy.DROP_OLDEST)`。
4. **错误处理不完整**：只写 doOnError，没写 onErrorReturn / onErrorResume → 异常传播到根订阅者变成 500。
5. **Context 透传丢失**：Reactor Context 传播时不要中途切线程造成 Context 丢失，用 subscriberContext 统一注入。

## 六、何时不要用 WebFlux

- 业务逻辑主要是 CPU 密集（加密、图形计算）→ 不会比 MVC 快，反而更复杂。
- 团队对响应式编程无经验 → 学习曲线陡，生产故障排查难。
- 下游服务全部是同步 JDBC，无法在一个 Sprint 内迁移到 R2DBC → 先别上 WebFlux。
