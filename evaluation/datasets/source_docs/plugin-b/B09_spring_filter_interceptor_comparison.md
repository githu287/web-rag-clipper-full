# Spring Filter 与 Interceptor：异同点与适用场景

Filter 和 Interceptor 都是 Spring 处理 HTTP 请求的 AOP 机制，但它们所处的层级、能访问的信息、触发时机完全不同。混用会导致日志 MDC 缺失、事务无法生效等难题。

## 一、Filter（Servlet 层）

Filter 是 Jakarta Servlet 规范的一部分，在 DispatcherServlet 之前执行。运行在 Web 容器（Tomcat）层，Spring 只是把注册到 ServletContext 的 Filter Bean 自动管理起来。

```java
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class RequestIdFilter extends OncePerRequestFilter {
    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain)
        throws ServletException, IOException {
        String requestId = UUID.randomUUID().toString();
        MDC.put("requestId", requestId);
        res.setHeader("X-Request-ID", requestId);
        try {
            chain.doFilter(req, res);  // 必须显式调用，否则请求停止在此
        } finally {
            MDC.remove("requestId");
        }
    }
}
```

Filter 可以拿到原始 ServletRequest / ServletResponse，但拿不到 Controller 方法信息（没到那一步）。

## 二、HandlerInterceptor（Spring MVC 层）

Interceptor 在 DispatcherServlet 内部、找到 Handler（Controller 方法）之后执行。核心方法：
- `preHandle(HttpServletRequest, HttpServletResponse, Object handler)`：执行 Controller 之前。
- `postHandle(...)`：Controller 返回 ModelAndView 之后，View 渲染之前。
- `afterCompletion(...)`：整个请求完成，用于资源清理。

```java
@Component
public class AuthInterceptor implements HandlerInterceptor {
    @Override
    public boolean preHandle(HttpServletRequest req, HttpServletResponse res, Object handler) {
        if (!(handler instanceof HandlerMethod hm)) return true;
        if (hm.hasMethodAnnotation(PublicEndpoint.class)) return true;
        String token = req.getHeader("Authorization");
        if (!validate(token)) { res.sendError(401); return false; }
        UserContext.set(parse(token));
        return true;
    }
    @Override
    public void afterCompletion(...) { UserContext.clear(); }
}
```

## 三、关键差异对比表

| 维度 | Filter | HandlerInterceptor |
|------|--------|--------------------|
| 层级 | Servlet 规范（容器层）| Spring MVC 层 |
| 触发时机 | DispatcherServlet 之前/之后 | 找到 Controller 方法前后 |
| 能拿到什么 | ServletRequest, Response | Spring ModelAndView, HandlerMethod |
| 能读 Spring Bean | 能，因为被 Spring 管理 | 天然能 |
| 访问 Controller 注解 | 不能（handler还没确定）| 能（通过 HandlerMethod.getMethod()）|
| 与事务关系 | 事务还没开始 | preHandle 之后才会触发事务拦截器（事务 AOP 在 Controller method 调用时开启）|
| 执行顺序 | 先于所有 Interceptor | 中间 |
| 适用场景 | RequestId、CORS、GZIP、编码、限流、审计日志 | 鉴权、日志上下文清理、用户注入、权限 @RequiresPermission 校验 |

## 四、Aspect 作为第三层补充

如果业务横切关心的是「方法级」而非「请求级」（如某 Service 方法要做调用审计），应该用 Spring AOP @Aspect，而非 Filter 或 Interceptor。

## 五、常见错误

1. **用 Filter 校验 @RequiresPermission 注解** → Filter 还不知道用户会命中哪个 Controller 方法，根本拿不到注解。必须用 Interceptor 或 AOP。
2. **用 Interceptor 写 CORS Header** → Interceptor 在 preHandle 之前，Spring 的 CorsFilter 已经是 Filter，会被覆盖。CORS 必须配置在 WebMvcConfigurer.addCorsMappings() 或 CorsFilter。
3. **Interceptor 里抛异常走不到 afterCompletion** → afterCompletion 只在 preHandle 返回 true 后触发。自定义异常必须在 afterCompletion 之外用 @RestControllerAdvice 处理。
4. **Filter chain.doFilter 忘记写** → 整个请求卡死，返回空白响应。
5. **MDC 放了但没清理** → 线程池中被下一个请求复用，产生日志串号。必须 finally 块清理。
