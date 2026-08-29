# Spring Actuator 健康检查与指标监控实战

Spring Boot Actuator 是生产环境必备 starter。它通过 HTTP 或 JMX 暴露一系列运维端点：健康检查、指标、配置、线程 dump、堆 dump、Bean 列表等。

## 一、Starter 与基础配置

```xml
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
</dependency>
```

核心 application.yml 配置：
```yaml
management:
  endpoints:
    web:
      exposure:
        include: "health,info,prometheus,metrics,logfile"
        exclude: "shutdown,env,beans,heapdump"   # 敏感端点默认关闭
  endpoint:
    health:
      show-details: when-authorized    # always / never / when-authorized
      roles: ADMIN
      probes:
        enabled: true                  # 开启 Kubernetes liveness/readiness 组
  metrics:
    tags:
      application: order-service
      environment: production
      team: commerce
```

## 二、Health 核心端点

`/actuator/health` 聚合所有 HealthIndicator 的状态。常见内置：
- **DataSourceHealthIndicator**：检查数据库连接，执行 `SELECT 1`。
- **DiskSpaceHealthIndicator**：剩余空间阈值。
- **PingHealthIndicator**：总是 UP，用于存活探针。
- **RedisHealthIndicator**：Redis PING。
- **CassandraHealthIndicator / MongoHealthIndicator** 等。

K8s 集成：
- `/actuator/health/liveness`：容器是否存活（假死时 Kubelet 重启 Pod）。
- `/actuator/health/readiness`：容器是否准备好接流量（LB 摘除未就绪 Pod）。
- 两者使用 `livenessStateHealthIndicator` 和 `readinessStateHealthIndicator`，与 AvailabilityState 事件联动。

## 三、自定义 HealthIndicator

```java
@Component
public class DownstreamPaymentHealthIndicator implements HealthIndicator {
    private final PaymentClient paymentClient;

    @Override
    public Health health() {
        try {
            PaymentStatus s = paymentClient.ping();
            return Health.up().withDetail("latency_ms", s.latencyMs()).build();
        } catch (Exception e) {
            return Health.down()
                .withDetail("error_type", e.getClass().getSimpleName())
                .withDetail("message", e.getMessage())
                .withException(e).build();
        }
    }
}
```

关键原则：HealthIndicator 的 ping 操作必须有超时（建议 <3s），否则一个下游挂掉会把整个健康检查端点拖慢 30s。

## 四、Prometheus 指标集成

加 `micrometer-registry-prometheus` 依赖后访问 `/actuator/prometheus` 得到 Prometheus 文本格式指标。Spring Boot 自动上报 100+ 默认指标：
- JVM：jvm_memory_used_bytes、jvm_threads_live、jvm_gc_pause_seconds。
- HTTP：http_server_requests_seconds_count、http_server_requests_seconds_sum（含 uri/status/exception 标签）。
- 数据源：hikaricp_connections_active、hikaricp_connections_idle。
- 日志：logback_events_total（level 标签区分 info/error）。

自定义业务指标推荐 MeterRegistry：

```java
@Service
public class OrderService {
    private final Counter orderCreated;
    private final Timer orderProcessTimer;

    public OrderService(MeterRegistry reg) {
        this.orderCreated = Counter.builder("orders.created_total").tag("channel", "app").register(reg);
        this.orderProcessTimer = Timer.builder("orders.process_duration_seconds").publishPercentiles(0.5, 0.95, 0.99).register(reg);
    }

    public void create(Order order) {
        orderProcessTimer.record(() -> doCreate(order));
        orderCreated.increment();
    }
}
```

## 五、安全注意事项

Actuator 默认暴露的数据非常敏感（/env 泄露属性明文、/heapdump 泄露内存镜像、/beans 泄露依赖关系）。生产必须：
1. 内网网段隔离 + 反向代理拦截所有 `/actuator/**` 非白名单路径。
2. 加 Spring Security：`requestMatchers(toAnyEndpoint()).hasRole("ACTUATOR_ADMIN")`。
3. shutdown 端点即使开了也要加 CSRF + 双重确认。
4. 永远不要把 actuator 端口和业务端口对外暴露为同一端口（建议 management.server.port=8081 单独管理端口）。
