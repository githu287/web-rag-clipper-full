# Spring Boot @Bean 注解与 FactoryBean 深度对比

@Bean 和 FactoryBean 都是 Spring 中「把对象交给 IoC 容器管理」的方式，但适用场景和使用方法完全不同。很多初学者在复杂 Bean 构造时把两者混用，导致循环依赖或初始化顺序错误。

## 一、@Bean（声明式方法级注册）

@Bean 写在 @Configuration 类的方法上，方法返回值就是一个 Bean。Spring 通过 CGLIB 代理 @Configuration 类，保证方法内部再调用其它 @Bean 方法时不会重复实例化（singleton 语义）。

```java
@Configuration
public class AppConfig {
    @Bean
    public RestTemplate restTemplate(RestTemplateBuilder builder) {
        return builder.setConnectTimeout(Duration.ofSeconds(3)).build();
    }
}
```

Bean 的名称默认等于方法名。可通过 @Bean("customName") 指定；通过 @Qualifier 引用。

## 二、FactoryBean（编程式 Bean 构造）

FactoryBean 本身是一个 Bean，但它的作用是「生产另一个 Bean」。实现三个方法：

```java
public class MyObjectFactory implements FactoryBean<BigObject> {
    @Override public BigObject getObject() { return buildBigObjectFromTonsOfSteps(); }
    @Override public Class<?> getObjectType() { return BigObject.class; }
    @Override public boolean isSingleton() { return true; }
}
```

最终容器里有两个东西：`&myObjectFactory`（FactoryBean 实例本身）、`myObjectFactory`（getObject() 返回的 BigObject 实例）。用 `&` 前缀可以取 FactoryBean 本体。

## 三、何时用 FactoryBean 而不是 @Bean

以下四个场景优先选 FactoryBean：
1. **构造过程涉及大量步骤且需要复用**：例如 ORM SessionFactory，需要读 config、buildProperties、addAnnotatedClass、validate、build 五步，而且多模块需要重复。
2. **Bean 的实际类型运行时才能确定**：例如动态代理 Bean，接口是动态生成的，@Bean 方法签名无法写死。
3. **需要提供 Builder 风格 API 给使用者**：FactoryBean 可以暴露 setter 作为配置项，Spring 在 getObject() 之前注入。
4. **需要和第三方库的复杂对象构造流程对接**：如 Apache HttpClient、OkHttp、Netty Bootstrap。

## 四、循环依赖差异

- @Bean 循环依赖：构造器注入无法解决，Setter 注入可由三级缓存解决（但 2.6+ 默认禁止）。
- FactoryBean 循环依赖：如果 A.getObject() 依赖 B，B.getObject() 又依赖 A，会在 getObject() 调用栈里爆栈，三级缓存机制救不了，因为 FactoryBean.getObject() 是「主动创建」，不是属性注入，Spring 无法介入提前暴露早期引用。

## 五、初始化顺序控制

- @Bean：通过 @DependsOn("otherBean") 声明依赖。
- FactoryBean：可以通过 Bean 定义的 depends-on 属性，也可以直接在 getObject() 方法开头从 beanFactory.getBean(other) 显式拉取。

两者都支持 @Lazy 延迟加载。

## 六、实战建议

90% 的业务场景 @Bean 足够。只有当你写框架代码（如 starter）、或者构造一个复杂第三方对象需要超过五行语句且被多处使用时，才考虑 FactoryBean。切忌业务代码到处写 FactoryBean，增加维护负担。
