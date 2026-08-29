# Spring Boot 自动装配（AutoConfiguration）原理与使用

Spring Boot 最核心的创新是「约定优于配置」的 AutoConfiguration 机制。开发者不再需要手写 XML 或 @Configuration 注解组合，只需要引入 starter 依赖，Spring 容器就会根据 classpath 内容自动完成相关 Bean 的注册。

## 一、@SpringBootApplication 的合成结构

启动类的 @SpringBootApplication 其实是三个注解的组合：
- @SpringBootConfiguration：标记当前类是配置源，相当于 @Configuration。
- @EnableAutoConfiguration：触发 AutoConfiguration 加载流程（核心）。
- @ComponentScan：默认扫描启动类所在包及其子包，识别 @Component / @Service / @Controller 等。

## 二、AutoConfiguration 的加载流程

启动过程中的关键步骤：
1. SpringFactoriesLoader 扫描所有依赖 jar 的 `META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports` 文件（Spring Boot 3.x 新格式），收集所有候选自动配置类全限定名列表。
2. 每个自动配置类通过 @Conditional 系列注解做条件筛选。常见条件：@ConditionalOnClass（classpath 存在特定类）、@ConditionalOnMissingBean（用户未手动定义时才生效）、@ConditionalOnProperty（配置值匹配）、@ConditionalOnWebApplication（Web 应用类型）。
3. 匹配通过的配置类，其内部 @Bean 方法依次执行，把组件注册到 ApplicationContext。
4. 支持 @AutoConfigureBefore / @AutoConfigureAfter 控制配置之间的先后顺序。

## 三、自己写 Starter 的七步法

典型内部 starter 结构：
1. `xxx-spring-boot-autoconfigure` 模块：编写 `XxxAutoConfiguration` 类 + `XxxProperties` @ConfigurationProperties。
2. `xxx-spring-boot-starter` 模块：仅 pom 依赖 autoconfigure + 真正的 runtime 库。
3. 写入 `META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports`。
4. 在 Additional-spring-configuration-metadata.json 补充属性描述，供 IDE 自动补全。
5. 关键 Bean 必须加 @ConditionalOnMissingBean，保证用户可以覆盖。
6. 提供自动注册的 BeanPostProcessor 做初始化埋点。
7. 加 spring.factories 的 Deprecation 兼容（若需要支持 Spring Boot 2.x）。

## 四、调试为什么不生效

Debug AutoConfiguration 未生效的三种方法：
1. 启动加 `--debug`：输出 CONDITIONS EVALUATION REPORT 报告，展示每个配置类 matched/unmatched 及原因。
2. 引入 `spring-boot-starter-actuator` 访问 `/actuator/beans` 和 `/actuator/conditions`。
3. IDEA 使用 Debug 模式在 `AutoConfigurationImportSelector.selectImports()` 打断点一步步看筛选逻辑。

常见失败原因：starter 包不在启动类扫描路径；classpath 缺少某个 @ConditionalOnClass 要求的类；用户手动声明了同名 Bean 触发了 @ConditionalOnMissingBean 的 false 分支。

## 五、版本迁移注意

Spring Boot 2.x → 3.x 最大破坏变更：
- `spring.factories` 不再支持 AutoConfiguration 声明方式，必须改成 `.imports` 文件。
- javax.* 命名空间全部迁移到 jakarta.*。
- 部分废弃的 @ConditionalOnSingleCandidate 行为调整。
- AutoConfiguration 类现在建议显式加 @AutoConfiguration（而不是再用 @Configuration + @Import）。
