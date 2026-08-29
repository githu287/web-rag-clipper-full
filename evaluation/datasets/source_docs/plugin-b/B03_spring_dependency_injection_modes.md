# Spring 依赖注入（DI）的四种模式与选型

依赖注入是 Spring 对「控制反转」原则的具体实现。开发者有四种注入方式可选，其在可测试性、可读性、性能、空值风险上有显著差异。

## 一、模式 1：字段注入（Field Injection）—— 不推荐

```java
@Service
public class OrderService {
    @Autowired private UserService userService;
}
```

优点：代码短，写起来快。
缺点：(1) 字段非 final，无法保证不可变；(2) 单元测试必须启动 Spring Context 或用反射注入，写纯 Mockito 单测繁琐；(3) 循环依赖隐藏性强；(4) 多个依赖关系不直观。Spring 官方文档已经明确不推荐。

## 二、模式 2：Setter 注入（Setter Injection）

```java
@Service
public class OrderService {
    private UserService userService;
    @Autowired
    public void setUserService(UserService userService) { this.userService = userService; }
}
```

适用于可选依赖。配合 `@Autowired(required = false)` 可以显式表达「可能不注入，使用默认值」。代价是字段不能是 final。

## 三、模式 3：构造器注入（Constructor Injection）—— **Spring 4.3+ 官方推荐**

```java
@Service
public class OrderService {
    private final UserService userService;
    private final PaymentService paymentService;
    // Spring 4.3 之后单构造器可省略 @Autowired
    public OrderService(UserService userService, PaymentService paymentService) {
        this.userService = requireNonNull(userService);
        this.paymentService = requireNonNull(paymentService);
    }
}
```

六大优势：
1. 依赖字段可以是 final → 不可变、线程安全。
2. 构造器参数清楚地表达必须依赖与可选依赖。
3. 单元测试不需要启动 Spring，直接 new OrderService(mockUser, mockPayment)。
4. 很容易配合 Lombok @RequiredArgsConstructor 自动生成。
5. 如果依赖缺失，在实例化阶段就抛 NPE，而不是运行时字段为 null。
6. 天然地防止循环依赖：构造器循环会报错，你不得不重构设计。

## 四、模式 4：Lookup 方法注入

用于单例 Bean 依赖 prototype Bean 的场景。每次调用单例的方法都需要一个新 prototype 实例：

```java
@Service
public abstract class TaskProcessor {
    public void process() {
        TaskContext ctx = createContext();  // 每次拿到新的 prototype
        doWork(ctx);
    }
    @Lookup("taskContext")
    protected abstract TaskContext createContext();
}
```

Spring 在运行时用 CGLIB 动态子类覆盖 createContext() 方法，实际调用 beanFactory.getBean("taskContext")。

## 五、@Qualifier 和 @Primary

当同一接口有多个实现时：
- @Primary：定义默认首选项。
- @Qualifier("beanName")：精确指定注入哪一个。
- @Named("xxx")：JSR-330 的等价写法，功能与 @Qualifier 几乎一致。

## 六、最终推荐

- 默认 99% 场景 → 构造器注入 + Lombok @RequiredArgsConstructor。
- 可选依赖 → Setter 注入 + required=false。
- 单例 Bean 需要 prototype → Lookup 方法注入。
- 绝对禁止 → 字段注入。
