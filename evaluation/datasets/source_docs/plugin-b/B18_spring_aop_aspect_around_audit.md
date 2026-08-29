# Spring AOP 切面编程：@Around 日志与事务扩展实战

Spring AOP（Aspect Oriented Programming）通过动态代理在方法调用前后插入横切逻辑。最常见用途：日志、鉴权、审计、性能统计、重试、缓存。和前面 Interceptor 同属 AOP 家族，但粒度是方法级。

## 一、五大约束术语

- **Aspect（切面）**：横切逻辑的类，@Aspect 注解。
- **Pointcut（切点）**：表达式声明在哪些方法触发。常用 `execution(public * com.example.service.*.*(..))`。
- **Join Point（连接点）**：被选中的方法调用。
- **Advice（通知）**：@Before（之前）、@AfterReturning（返回后）、@AfterThrowing（抛异常）、@After（之后，不管抛不抛）、@Around（最强大，包裹整个方法调用）。
- **Weaving（织入）**：Spring 默认在运行期织入（CGLIB 动态子类），不是编译期（AspectJ compile-time weaving）。

## 二、@Around 审计日志切面示例

```java
@Aspect
@Component
@Slf4j
public class AuditLoggingAspect {

    @Pointcut("""
        execution(* com.example.service.*.*(..))
        && @annotation(com.example.annotations.AuditLog)
    """)
    public void auditableServiceMethod() {}

    @Around("auditableServiceMethod()")
    public Object around(ProceedingJoinPoint pjp) throws Throwable {
        MethodSignature sig = (MethodSignature) pjp.getSignature();
        String methodName = sig.getDeclaringTypeName() + "#" + sig.getName();
        Object[] args = pjp.getArgs();

        // Before
        long start = System.nanoTime();
        AuditLogEvent event = new AuditLogEvent()
            .setMethod(methodName)
            .setArgs(toSafeJson(args))
            .setOperatorId(getCurrentUserId())
            .setAt(LocalDateTime.now());

        Object result = null;
        try {
            result = pjp.proceed();                 // 继续调用真实方法。如果 proceed() 不调用或传错 args，方法不会执行！
            event.setResult(toSafeResultJson(result))
                 .setStatus("SUCCESS");
            return result;
        } catch (Throwable t) {
            event.setStatus("FAILED")
                 .setExceptionType(t.getClass().getSimpleName())
                 .setExceptionMessage(StringUtils.abbreviate(t.getMessage(), 500));
            throw t;                                 // 异常必须原样重抛，否则吞错。
        } finally {
            event.setDurationNs(System.nanoTime() - start);
            auditLogPublisher.publish(event);       // 异步写入审计表
            log.info("AUDIT {} {} cost {} us", event.getStatus(), methodName, event.getDurationNs()/1000);
        }
    }
}
```

## 三、切点表达式六类常用组件

1. `execution(modifiers? ret-type declaring-type? name(params) throws?)`：最常用。
2. `within(com.example.service.*)`：包内所有类。
3. `this(com.example.Xxx)`：代理对象实现的接口。
4. `target(com.example.Xxx)`：目标对象（未代理前）实现的接口。
5. `args(java.io.Serializable,..)`：参数类型匹配。
6. `@annotation(com.example.AuditLog)`：带某注解的方法。

组合用 `&&` `||` `!`。最简洁的切点是「execution + @annotation 双保险」。

## 四、常见陷阱

1. **@Around 没有调用 proceed()** → 目标方法根本不执行。
2. **@Around 调用 proceed() 没把原 args 传进去且原方法有参数** → args 传 null 或空数组导致目标方法 NPE。传参正确姿势：`pjp.proceed(pjp.getArgs())`。
3. **修改 args 没传回** → 切面想脱敏参数，必须用 `pjp.proceed(newArgs)`。
4. **private 方法加 @AuditLog 无效** → 动态代理只能代理 public 方法。同自调用问题：this.method() 不走代理。
5. **Aspect 内部出现 this 调用自身 Advice 方法** → 自调用不会再次进切面。切面内递归要显式 ((XxxService) AopContext.currentProxy()).method()。
6. **proceed 返回值没向外 return** → 方法返回 null，引起下游 NPE。

## 五、AOP 与 Interceptor 选型口诀

请求级横切（鉴权、CORS Header、RequestId）→ Interceptor / Filter。
方法级横切（Service 耗时、审计、重试、缓存）→ AOP @Around。
异常级统一处理 → @RestControllerAdvice。
不要把所有横切都塞进 AOP，维护成本会爆炸。
