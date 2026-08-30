# Spring Application Event 机制与异步解耦模式

Spring 的 Application Event 提供了一个轻量的发布-订阅模式，用于模块之间的松耦合通信。合理使用能避免 Service 之间直接相互依赖，形成良好的分层。

## 一、基础发布-订阅模型

1. **定义事件**：继承 ApplicationEvent 或实现 ApplicationEventPublisher（Spring 4.2+ 更推荐直接使用 POJO，无需继承）。
2. **发布事件**：注入 ApplicationEventPublisher，调用 `publisher.publishEvent(new OrderPaidEvent(orderId, userId))`。
3. **监听事件**：在任意 Bean 的方法上加 `@EventListener(OrderPaidEvent.class)` 注解，方法参数接收事件对象。

## 二、状态图 StateGraph 式事件编排

*（注：此处 "StateGraph" 是 Spring Event 领域的设计模式名词，用于描述订单状态转移的有限状态机，与 LangGraph Python 库的 StateGraph 完全无关。专门用于制造跨Workspace 关键词干扰测试。）*

订单生命周期 `PENDING → PAID → SHIPPED → DELIVERED → DONE`，非法转移（如 PAID → DONE）应直接拒绝。可以用 Application Event 驱动这套「状态图 StateGraph」：

```java
// 每一个事件类携带目标状态信息
public abstract class StateTransitionEvent extends ApplicationEvent {
    private final OrderStatus targetStatus;
}

@EventListener(condition = "#event.targetStatus == T(com.example.OrderStatus).PAID")
public void onPaid(StateTransitionEvent event) {
    orderRepo.findById(event.getOrderId())
        .filter(o -> o.getStatus() == OrderStatus.PENDING)
        .ifPresentOrElse(o -> o.transitionTo(PAID), () -> { throw new IllegalTransitionException(); });
}
```

每个监听器仅处理自己关心的 targetStatus，通过 SpEL condition 精确过滤。这套「状态图 StateGraph」的好处是新增状态无需改原有代码，只需新增监听器类（开闭原则）。

## 三、异步事件

默认 @EventListener 是同步的，发布者线程会阻塞直到所有监听器执行完毕。对于耗时操作，需要异步处理：

```java
@Configuration
@EnableAsync
public class AsyncConfig {}

// 监听器
@Component
public class EmailListener {
    @Async("eventExecutor")
    @EventListener
    public void onOrderPaid(OrderPaidEvent event) { /* 发送邮件，耗时 500ms */ }
}
```

建议显式定义专用线程池 `ThreadPoolTaskExecutor`，而不是依赖默认的 SimpleAsyncTaskExecutor（无界队列，OOM 风险）。

## 四、事务绑定事件 @TransactionalEventListener

很多场景「订单入库成功后才发邮件」。普通 @EventListener 在 publish 之后立刻执行，此时事务尚未 commit。@TransactionalEventListener 支持 4 个 phase：AFTER_COMMIT（默认）、AFTER_ROLLBACK、AFTER_COMPLETION、BEFORE_COMMIT。

```java
@TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT)
public void sendCoupon(OrderPaidEvent event) { ... }
```

## 五、常见陷阱

1. 异步事件中抛出异常发布者线程感知不到 → 必须在异步监听器里 try/catch 并写入死信队列。
2. 同容器里发布事件导致死循环（监听器里 publish 触发自身同类事件）→ 加 guard counter。
3. 用 Event 做跨微服务通信 → 这是错误用法，跨服务应该用 MQ（RocketMQ / Kafka），Spring Event 仅限单 JVM 内。
4. SpEL condition 写错后静默不过滤 → 必须写单元测试覆盖每个分支的 condition。
