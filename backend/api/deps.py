"""
FastAPI 依赖（Phase 3.5 Step 2-D 新增 Plugin 身份；Step 2-H 移除旧 User 身份）。

身份体系唯一来源：
    get_current_plugin（Phase 3.5 Step 2-D）：
        `X-Plugin-ID` + `X-Plugin-Secret` → PluginService.authenticate()
        → PluginWorkspace ORM。

设计要点：
    1) 当前 Plugin 身份唯一来源：X-Plugin-ID + X-Plugin-Secret；
       API Key 完全退出身份认证（不允许从 API Key 推断 plugin_id、
       不允许前端传 plugin_id、不允许 plugin_name 作为业务权限依据）。
    2) 只负责「请求者是谁」：API Key 解密由业务链路显式调用
       PluginService.decrypt_api_key(plugin)，不在本依赖内隐式执行。
    3) 错误消息不含任何 plugin_secret 片段（明文 / hash 一律不进日志）。
    4) PluginRepository 查询失败（PluginOperationError）不在这里捕获，
       由 main.py 全局 handler 映射 503。
    5) 禁止从 plugin_name / document_id / request body / query parameter /
       document.plugin_id / API Key 推断 Plugin 身份；
    6) Header 缺失 → PluginCredentialsMissingError（main.py handler → 401）；
    7) authenticate() 抛出的 PluginNotFoundError / PluginSecretMismatchError
       （→401）、PluginDisabledError（→403）向上传播，由 main.py handler 映射。

范围边界：
    - 不检查 IP / User-Agent / 限流（未来可扩展为独立中间件）。
"""

from __future__ import annotations

from fastapi import Depends, Header

from ..core.di import get_plugin_service
from ..core.exceptions import PluginCredentialsMissingError
from ..models.plugin import PluginWorkspace
from ..services.plugin_service import PluginService


def get_current_plugin(
    plugin_id: str | None = Header(default=None, alias="X-Plugin-ID"),
    plugin_secret: str | None = Header(default=None, alias="X-Plugin-Secret"),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> PluginWorkspace:
    """
    当前 Plugin 依赖：`X-Plugin-ID` + `X-Plugin-Secret` → PluginWorkspace ORM。

    Args:
        plugin_id: 请求头 X-Plugin-ID（Workspace 标识；默认 None = 未提供）。
        plugin_secret: 请求头 X-Plugin-Secret（Workspace 认证凭证；默认 None）。
        plugin_service: PluginService（Protocol 依赖，DI 注入）。

    Returns:
        认证通过的 PluginWorkspace ORM 对象（status = ACTIVE）。

    Raises:
        PluginCredentialsMissingError(401)：X-Plugin-ID 或 X-Plugin-Secret
            任一缺失 / 为空。
        PluginNotFoundError(401) / PluginSecretMismatchError(401)：由
            PluginService.authenticate 抛出（plugin_id 不存在 / secret 不匹配）。
        PluginDisabledError(403)：workspace status != ACTIVE（DISABLED）。
    """
    if not plugin_id or not plugin_secret:
        raise PluginCredentialsMissingError("plugin credentials are missing")
    return plugin_service.authenticate(plugin_id, plugin_secret)
