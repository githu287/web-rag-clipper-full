# Spring Security 认证授权：SecurityFilterChain 与 JWT 实战

Spring Security 6.x 引入全新的 SecurityFilterChain 配置风格（取代旧的 WebSecurityConfigurerAdapter）。结合 JWT 做前后端分离认证是目前最主流的组合。

## 一、Spring Security 三层核心概念

- **Authentication（认证）**：你是谁？验证 username/password、JWT、API Key、OAuth2。
- **Authorization（授权）**：你能做什么？基于角色（ROLE_ADMIN）或权限（perm:order:delete）。
- **Protection**：CSRF、CORS、X-Frame-Options、HSTS、Session Fixation、重定向 Host 校验等安全头。

## 二、SecurityFilterChain Bean（Spring Security 6 标配）

```java
@Configuration
@EnableWebSecurity
@EnableMethodSecurity(prePostEnabled = true)
public class SecurityConfig {

    @Bean
    public SecurityFilterChain apiFilterChain(HttpSecurity http) throws Exception {
        return http
            .csrf(csrf -> csrf.disable())                 // 纯 JWT Token API，无 Session，禁用 CSRF
            .sessionManagement(sm -> sm.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .cors(cors -> cors.configurationSource(corsConfigurationSource()))
            .exceptionHandling(ex -> ex
                .authenticationEntryPoint((req, res, authEx) -> res.sendError(401, authEx.getMessage()))
                .accessDeniedHandler((req, res, deniedEx) -> res.sendError(403))
            )
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/api/v1/auth/**", "/actuator/health/**", "/public/**").permitAll()
                .requestMatchers("/api/v1/admin/**").hasRole("ADMIN")
                .requestMatchers(HttpMethod.DELETE, "/api/v1/orders/**").hasAuthority("perm:order:delete")
                .anyRequest().authenticated()
            )
            .addFilterBefore(jwtAuthenticationFilter(), UsernamePasswordAuthenticationFilter.class)
            .build();
    }
}
```

`@EnableMethodSecurity` 允许在 Service 方法上用 `@PreAuthorize("hasRole('ADMIN')")` 或 `@PreAuthorize("@orderSecurity.canAccess(authentication, #orderId)")` 做细粒度 SpEL 授权。

## 三、JWT Filter 实现

```java
@Component
@RequiredArgsConstructor
public class JwtAuthenticationFilter extends OncePerRequestFilter {
    private final JwtService jwtService;

    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain)
        throws ServletException, IOException {
        String header = req.getHeader(HttpHeaders.AUTHORIZATION);
        if (header == null || !header.startsWith("Bearer ")) {
            chain.doFilter(req, res);
            return;
        }
        String token = header.substring(7);
        try {
            if (jwtService.validate(token)) {
                Authentication auth = jwtService.toAuthentication(token);
                SecurityContextHolder.getContext().setAuthentication(auth);
            }
        } catch (JwtException e) {
            // token 无效：直接放行交给后面的 AuthorizationFilter 返回 401。
            // 绝对不要在这里 res.sendError，否则后续过滤器不执行导致跨中间件不一致
        }
        chain.doFilter(req, res);
    }
}
```

JWT 推荐：算法 RS256（非对称，网关验签只需要公钥）、access_token TTL 15 分钟、refresh_token 7 天、黑名单走 Redis SET。

## 四、密码存储

必须使用 BCrypt：
```java
@Bean public PasswordEncoder passwordEncoder() { return new BCryptPasswordEncoder(); }
```
绝对禁止：明文、MD5、SHA-1。BCrypt 内部自带盐值 + 可调 cost 因子（默认 10，生产建议 12）。

## 五、常见安全陷阱

1. **把 /error 路径 permitAll → 意外泄露堆栈** → Spring Security 默认不会放行 error，自定义白名单时不要 include。
2. **authorizeHttpRequests 顺序写错** → 规则是「从上到下 first match wins」。anyRequest().authenticated() 必须放最后！
3. **JWT 里放权限** → Token 一旦签发权限无法吊销。权限应该走 UserDetailsService 查库，JWT 只放 userId。
4. **Filter 里手动 res.sendError 之后继续 chain.doFilter** → 提交了 Response 再写会报 IllegalStateException。
5. **@PreAuthorize 打在 Controller public 方法（走代理）有效，但打在 Service 的 private/protected 方法无效**。因为 Controller 走 AOP，方法内 this 调用自己 Service 私有方法绕过代理（同样的自调用坑）。
6. **CSRF 全禁用 + Cookie 做 Session → 极易受到 CSRF 攻击**：有 Cookie 必须开 CSRF，API Token 才能关。
