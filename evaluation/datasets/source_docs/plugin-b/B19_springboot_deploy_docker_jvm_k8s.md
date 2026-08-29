# Spring Boot 生产部署：Docker 镜像、JVM 参数、K8s 最佳实践

Spring Boot 3.x 对容器化做了深度优化（Docker Image CDS、AOT、GraalVM Native）。但大多数团队仍在使用基于 JDK 的传统部署。本文总结一套稳健的 JVM-on-K8s 生产方案。

## 一、Dockerfile：四阶段构建 + 非 root

```dockerfile
# -------- Stage 1: Maven 构建（Maven Cache 层复用）
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY pom.xml ./
RUN mvn dependency:go-offline -B
COPY src src
RUN mvn -DskipTests package -B

# -------- Stage 2: 瘦身（提取 layers，实现分层镜像）
FROM eclipse-temurin:17-jre-jammy AS extract
WORKDIR /app
COPY --from=build /src/target/*.jar app.jar
RUN java -Djarmode=layertools -jar app.jar extract --destination /layers

# -------- Stage 3: Runtime（最终镜像）
FROM eclipse-temurin:17-jre-jammy AS runtime
RUN groupadd -r app && useradd -r -g app -m -s /sbin/nologin app
WORKDIR /app
COPY --from=extract /layers/dependencies/ ./
COPY --from=extract /layers/spring-boot-loader/ ./
COPY --from=extract /layers/snapshot-dependencies/ ./
COPY --from=extract /layers/application/ ./
USER app
ENV JAVA_OPTS=""
EXPOSE 8080 8081
ENTRYPOINT [ "sh", "-c", "java $JAVA_OPTS org.springframework.boot.loader.launch.JarLauncher" ]
```

分层技巧：dependencies 层极少变，放最底层利用 Docker 缓存。application 层放在最上层，代码修改后重打镜像只需下载几十 KB。

## 二、JVM 参数模板（16 GB Pod，4C）

```bash
JAVA_OPTS="
 -server
 -XX:InitialRAMPercentage=70.0      # 初始堆 = 容器内存 70%
 -XX:MaxRAMPercentage=70.0          # 最大堆 = 容器内存 70%（容器感知，不再写死 Xmx）
 -XX:MaxMetaspaceSize=512m
 -XX:+UseG1GC                       # 默认 G1，8GB+ 堆最合适
 -XX:MaxGCPauseMillis=200
 -XX:+ParallelRefProcEnabled
 -XX:+UseContainerSupport
 -XX:ActiveProcessorCount=4         # 容器 CFS 限制，防止 JVM 读到 Node 64 核创建 64 GC 线程
 -XX:+ExitOnOutOfMemoryError        # OOM 立即退出，让 K8s 重启 Pod
 -Xlog:gc*:file=/var/log/app/gc.log:time,tags,level
 -Dcom.sun.management.jmxremote.rmi.port=9010
 -Dcom.sun.management.jmxremote=true
 -Dcom.sun.management.jmxremote.authenticate=false
 -Dcom.sun.management.jmxremote.ssl=false
 -Duser.timezone=Asia/Shanghai
 -Dfile.encoding=UTF-8
 -Dspring.profiles.active=prod
"
```

关键：不再使用 Xmx/Xms，改用 MaxRAMPercentage。JDK 10+ 支持 Container CPU 感知，但 17 之前 ActiveProcessorCount 需要显式设置才安全。

## 三、Kubernetes 清单核心字段

```yaml
resources:
  requests: { cpu: "1000m", memory: "2Gi" }
  limits:   { cpu: "4000m", memory: "16Gi" }
livenessProbe:
  httpGet: { path: /actuator/health/livenessState, port: 8081 }
  initialDelaySeconds: 60
  periodSeconds: 15
  timeoutSeconds: 3
  failureThreshold: 3
readinessProbe:
  httpGet: { path: /actuator/health/readinessState, port: 8081 }
  initialDelaySeconds: 30
  periodSeconds: 5
  failureThreshold: 3
lifecycle:
  preStop:
    exec: { command: [ "sleep", "20" ] }   # SIGTERM 到 Nginx 摘除 LB 之间有空窗，等待流量切走
terminationGracePeriodSeconds: 60
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector: { matchLabels: { app: order-service } }
```

## 四、启动优化

- **CDS（Class Data Sharing）**：启动时 `-XX:ArchiveClassesAtExit` 先生成 classes.jsa，再启动用 `-XX:SharedArchiveFile` 加载。冷启动时间减少 20-40%。
- **Spring AOT（Spring Boot 3 新）**：构建期 `mvn -Pnative native:compile` 把 Bean 反射信息预生成字节码，Runtime 跳过反射扫描。不是 Native Image 也能用 AOT。
- **JFR（Java Flight Recorder）**：`-XX:StartFlightRecording=settings=profile,dumponexit=true,filename=/var/log/app/rec.jfr` 开启，性能损耗 <2%，但能事后定位 GC、热点方法、锁竞争。

## 五、Pitfall 清单

1. **基础镜像用 alpine JRE**（musl libc）→ 一些 JNI 依赖崩溃、时区不对。生产用官方 ubuntu/jammy。
2. **limits.memory 只给 2GB 却设 MaxRAMPercentage=80 → 元空间 + 堆外内存 + JIT 超出 cgroup 限制被 OOM Kill** → 保守设置 70。
3. **preStop 没有 sleep** → 滚动发布期间 502 暴增。必须 20s 以上让 Ingress Controller 摘流量生效。
4. **JVM 没有开启 OOM Dump** → 线上反复 OOM 无法根因。必加：`-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/var/log/app/oom-$(date +%s).hprof`，并挂载 emptyDir。
5. **只看 Pod memory.usage 指标，没拆 working_set / rss / cache** → 缓存占高内存错误扩容。
