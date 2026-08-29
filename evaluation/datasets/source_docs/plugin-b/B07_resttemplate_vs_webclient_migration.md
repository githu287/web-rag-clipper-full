# Spring WebClient vs RestTemplate：选型与迁移指南

Spring Web 中调用远程 HTTP 服务有三种方式：RestTemplate（同步阻塞）、WebClient（响应式非阻塞）、RestClient（6.1+ 新简化版）。选错客户端会造成吞吐或复杂度问题。

## 一、RestTemplate（Spring 3 登场，Spring 5 进入维护模式）

同步阻塞模型：基于 JDK HttpURLConnection（默认）或 Apache HttpClient / OkHttp（可替换）。每个请求占用一个线程直到响应返回。

```java
@Bean
public RestTemplate restTemplate() {
    ClientHttpRequestFactory factory = new HttpComponentsClientHttpRequestFactory(
        HttpClientBuilder.create()
            .setConnectionTimeToLive(30, TimeUnit.SECONDS)
            .evictIdleConnections(5, TimeUnit.SECONDS)
            .build()
    );
    RestTemplate rt = new RestTemplate(factory);
    rt.setInterceptors(List.of(new LoggingInterceptor(), new AuthInterceptor()));
    rt.getMessageConverters().addFirst(new MappingJackson2HttpMessageConverter(objectMapper));
    return rt;
}
```

适用场景：低 QPS 内部服务调用、同步批处理脚本、已有老代码。
不适用场景：高并发对外网关、需要并发合并多个下游结果的场景。

## 二、WebClient（Spring 5 引入，基于 Reactor Netty）

响应式模型：基于 Reactor Mono/Flux，请求和响应都是异步事件驱动，线程占用极低（Netty event loop 线程数 = CPU 核数 × 2）。

```java
@Bean
public WebClient webClient() {
    return WebClient.builder()
        .baseUrl("https://api.example.com")
        .defaultHeader(HttpHeaders.CONTENT_TYPE, "application/json")
        .clientConnector(new ReactorClientHttpConnector(
            HttpClient.create().responseTimeout(Duration.ofSeconds(3)).option(ChannelOption.CONNECT_TIMEOUT_MILLIS, 2000)
        ))
        .filter(basicAuthentication("user", "pass"))
        .filter((request, next) -> next.exchange(request)
            .timeout(Duration.ofSeconds(8))
            .onErrorMap(TimeoutException.class, e -> new DownstreamTimeoutException())
        )
        .build();
}
```

## 三、关键差异对比

| 维度 | RestTemplate | WebClient |
|------|-------------|-----------|
| 模型 | 同步阻塞（线程模型 1 req → 1 thread）| 异步非阻塞（少量 event loop 线程）|
| QPS上限 | 取决于线程池大小（通常 200-500 并发）| 单实例可到 5000+ |
| 错误处理 | try/catch + RestClientException 子类 | onErrorMap / doOnError / retryWhen |
| 并发组合 | 用 ExecutorService + Future 手写 | Mono.zip / Mono.first / flatMapSequential 内置 |
| 响应式集成 | 无，需手工包 Mono | 天然返回 Mono，可直接组合到 Reactor 链 |
| 调试 | Stack trace 直观，断点简单 | 异步堆栈难读，需开启 Reactor Debug Agent |

## 四、Spring 6.1 RestClient（中庸之选）

Spring 6.1 引入的 RestClient，提供 WebClient 风格的 fluent API，但底层仍基于 RestTemplate 的 ClientHttpRequestFactory（默认同步阻塞）。适合「想要新 API 写法但不想引入 Reactor」的团队。

## 五、迁移指南

RestTemplate → WebClient 迁移必须分阶段：
1. 先替换底层 HTTP 引擎为 Apache HttpClient（保证连接池配置不变）。
2. 对 GET 请求批量改为 WebClient，bodyToMono(Xxx.class).block() 保持同步语义，先不改业务逻辑。
3. 再把 block() 一个个改成返回 Mono，逐层上推至 Controller 返回 Mono。
4. 最后移除 RestTemplate Bean。

阶段 2 是最关键的一步——它保证你可以在一个 Sprint 内完成 80% 的迁移而不引入行为回归。
