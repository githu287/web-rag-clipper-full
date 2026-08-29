# Spring MVC Controller 与 REST Controller 的区别与实战

Spring MVC 的 @Controller 和 @RestController 是 Web 开发最常用的注解，但两者行为差异常常被忽略。错误地选择会导致 404、406、TemplateInputException 等迷惑性错误。

## 一、核心差异一句话

- @Controller：方法返回值默认被解释为「视图名」，走 ViewResolver 渲染为 HTML/JSP/Thymeleaf 模板。
- @RestController = @Controller + @ResponseBody：方法返回值直接作为 HTTP Response Body，通过 HttpMessageConverter 序列化成 JSON/XML。

## 二、@RestController 的数据转换管线

请求进入 DispatcherServlet → HandlerMapping 匹配方法 → HandlerAdapter 调用：
1. 入参解析：@RequestBody 通过 `MappingJackson2HttpMessageConverter` 把 JSON 反序列化为 Java 对象（Jackson ObjectMapper）。
2. 方法执行业务逻辑。
3. 返回值处理：若是 Pojo → MappingJackson2HttpMessageConverter 转为 JSON；若是 String → StringHttpMessageConverter；若是 ResponseEntity\<Void> → 空 body。
4. @ResponseStatus 修饰方法会修改 HTTP 状态码；但方法内抛出异常由 @ControllerAdvice 统一处理走异常管线。

## 三、典型 CRUD 写法

```java
@RestController
@RequestMapping("/api/v1/orders")
@RequiredArgsConstructor
public class OrderController {
    private final OrderService orderService;

    @GetMapping("/{id}")
    public ResponseEntity<OrderDTO> get(@PathVariable Long id) {
        return orderService.findById(id)
            .map(ResponseEntity::ok)
            .orElseThrow(() -> new OrderNotFoundException(id));
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public OrderDTO create(@Valid @RequestBody CreateOrderRequest req) {
        return orderService.create(req);
    }
}
```

@Valid 在入参前触发 JSR-380 Bean Validation，校验失败抛出 MethodArgumentNotValidException，由统一 @RestControllerAdvice 拦截返回 422 + 字段级错误列表。

## 四、常见错误

1. 在 @RestController 里返回 "redirect:/xxx" → 它会被当作 JSON 字符串输出，不会真的重定向。要重定向必须用 @Controller。
2. @PathVariable 里变量名和参数名不一致但省略了 @PathVariable("name") → Spring 6 之前需要 `-parameters` 编译参数才能自动推断。保险写法永远显式写 @PathVariable("id")。
3. POST 方法用 @ModelAttribute 接受 JSON → 应该用 @RequestBody。@ModelAttribute 对应 form-encoded。
4. 返回 List\<Object> 且元素为 null → Jackson 默认序列化 null；需要 `spring.jackson.default-property-inclusion=non_null`。
5. 方法参数直接用 HttpServletRequest/Response → 破坏可测试性，除非必要请避免。

## 五、统一异常处理

```java
@RestControllerAdvice
public class GlobalExceptionHandler extends ResponseEntityExceptionHandler {
    @ExceptionHandler(OrderNotFoundException.class)
    public ProblemDetail handleNotFound(OrderNotFoundException ex) {
        return ProblemDetail.forStatusAndDetail(HttpStatus.NOT_FOUND, ex.getMessage());
    }
}
```

继承 ResponseEntityExceptionHandler 能覆盖 Spring MVC 内置的 10+ 标准异常（如 MethodArgumentNotValidException、MissingServletRequestParameterException），避免每种都自己写。
