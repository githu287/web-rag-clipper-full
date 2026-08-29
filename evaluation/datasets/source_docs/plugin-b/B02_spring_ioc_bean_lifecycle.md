# Spring IoC 容器 Bean 生命周期完全解析

Spring IoC 容器负责 Bean 的实例化、依赖注入、初始化、销毁。理解完整生命周期是解决各种「为什么我的 Bean 为 null」问题的前提。

## 一、Bean 定义的来源

BeanDefinition 是 Bean 在容器内部的元数据，来源包括：
- XML `<bean>` 定义（传统方式，Spring 6+ 基本废弃）。
- @Component / @Service / @Repository / @Controller 注解扫描。
- @Configuration 类中的 @Bean 方法。
- 编程式 `GenericApplicationContext.registerBean()`。

BeanDefinition 记录 scope（singleton/prototype）、init/destroy 方法名、是否懒加载、depends-on、构造参数、property 值等。

## 二、标准生命周期（13 步）

对一个典型的 singleton Bean：
1. 容器启动，读取 BeanDefinition。
2. 实例化 Bean：调用构造函数（或工厂方法）生成裸对象。
3. 属性注入：@Autowired / setter / 构造参数注入依赖（如果依赖未就绪，递归走同样流程实例化依赖）。
4. **BeanNameAware.setBeanName()**：注入 Bean 的 id。
5. **BeanClassLoaderAware.setBeanClassLoader()**。
6. **BeanFactoryAware.setBeanFactory()**：注入当前 BeanFactory。
7. **ApplicationContextAware.setApplicationContext()**：若是 ApplicationContext 容器，额外注入上下文。
8. **BeanPostProcessor.postProcessBeforeInitialization()**：所有自定义后处理器的 before 钩子。例如 @PostConstruct 注解是由 CommonAnnotationBeanPostProcessor 在这一步触发的。
9. **InitializingBean.afterPropertiesSet()**：标准接口回调。
10. **init-method / @Bean(initMethod)**：自定义初始化方法。
11. **BeanPostProcessor.postProcessAfterInitialization()**：AOP 代理就是在这里织入的（AbstractAutoProxyCreator）。
12. 业务运行期：Bean 处于可用状态。
13. 容器关闭：依次调用 **DisposableBean.destroy()**、destroy-method、@PreDestroy。

## 三、循环依赖

Spring 通过「三级缓存」解决单例构造 setter 注入的循环依赖：
- 一级缓存：singletonObjects（已完成初始化的完整 Bean）。
- 二级缓存：earlySingletonObjects（裸对象引用，尚未注入属性）。
- 三级缓存：singletonFactories（ObjectFactory 工厂，能在需要时产生 AOP 代理早期引用）。

但构造器注入的循环依赖无法解决，必须报错。Spring Boot 2.6 之后默认禁止循环依赖，必须显式配置 `spring.main.allow-circular-references=true` 才开启。

## 四、常见错误

1. 混淆 @PostConstruct 和 InitializingBean.afterPropertiesSet → 执行顺序是 @PostConstruct 先于 afterPropertiesSet 先于 initMethod。
2. 在 @PostConstruct 里调用被 AOP 代理的自身方法 → 此时 this 是原始对象，走不到代理。解决：@Lazy 自我注入或用 ApplicationContext.getBean()。
3. prototype scope 的 Bean 销毁方法不执行 → 必须手动显式销毁。Spring 容器只负责 singleton 的完整生命周期。
4. 懒加载的 Bean 在事务方法里未被代理 → 加了 @Lazy 的同时 @Transactional 必须写在 public 方法上。
