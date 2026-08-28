"""
Plugin Workspace HTTP 路由（Phase 3.5 Step 2-D 新增）。

端点：
    POST   /plugins/register      —— 注册新 Workspace（无需任何 Plugin Header）
    GET    /plugins/me            —— 当前 Workspace 信息（需 X-Plugin-ID + X-Plugin-Secret）
    PUT    /plugins/me            —— 修改显示名
    PUT    /plugins/me/api-key    —— 配置 / 更换百炼 API Key
    DELETE /plugins/me/api-key    —— 清除 API Key（204）
    DELETE /plugins/me            —— 删除 Workspace（双重确认，204）

身份链路：
    X-Plugin-ID + X-Plugin-Secret
        ↓ Depends(get_current_plugin)
    PluginService.authenticate()
        ↓
    PluginWorkspace（Depends 注入到所有受保护端点）

分层边界：
    - Router 不写业务规则：plugin_name 校验 / API Key 验证 / 查重 / 删除前置确认
      全部由 PluginService 统一处理（register 与 update_name 行为天然一致）；
    - Router 不触碰 Repository / ORM / SQL；
    - plugin_secret 明文只在 POST /plugins/register 响应中返回一次；
      后续端点任何响应都不含 secret / hash / ciphertext / nonce / api_key 明文。

安全红线：
    - 认证只依赖 X-Plugin-ID + X-Plugin-Secret（get_current_plugin）；
    - 禁止从 plugin_name / document_id / request body / query parameter /
      document.plugin_id / API Key 推断身份；
    - 错误消息不含 plugin_id / plugin_secret 片段（由 main.py handler 保证）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ...api.deps import get_current_plugin
from ...core.di import get_plugin_service
from ...models.api_schema import (
    PluginDeleteRequest,
    PluginMeResponse,
    PluginRegisterRequest,
    PluginRegisterResponse,
    PluginUpdateApiKeyRequest,
    PluginUpdateApiKeyResponse,
    PluginUpdateNameRequest,
    PluginUpdateNameResponse,
)
from ...models.plugin import PluginWorkspace
from ...services.plugin_service import PluginService

router = APIRouter(prefix="/plugins", tags=["plugins"])


def _api_key_configured(plugin: PluginWorkspace) -> bool:
    """ciphertext 与 nonce 均非 NULL 才视为已配置 API Key。"""
    return plugin.api_key_ciphertext is not None and plugin.api_key_nonce is not None


@router.post(
    "/register",
    status_code=201,
    response_model=PluginRegisterResponse,
)
def register_plugin(
    body: PluginRegisterRequest,
    plugin_service: PluginService = Depends(get_plugin_service),
) -> PluginRegisterResponse:
    """
    注册新 Plugin Workspace（无需任何 Plugin Header）。

    流程：Request → PluginService.register(plugin_name) →
        (PluginWorkspace, plugin_secret) → 201 Response。

    安全红线：plugin_secret 明文只在此响应中返回一次；不写 DB / 日志 /
    响应以外任何位置。客户端必须立即保存（后端不提供找回入口）。

    Raises:
        PluginNameValidationError → 422（名称非法）
        PluginNameTakenError → 409（归一化名已被占用）
        PluginOperationError → 503（plugin_workspaces 表写入异常）
    """
    workspace, plugin_secret = plugin_service.register(body.plugin_name)
    return PluginRegisterResponse(
        plugin_id=workspace.plugin_id,
        plugin_name=workspace.plugin_name,
        plugin_secret=plugin_secret,
    )


@router.get(
    "/me",
    response_model=PluginMeResponse,
)
def get_plugin_me(
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> PluginMeResponse:
    """
    获取当前 Workspace 信息（需 X-Plugin-ID + X-Plugin-Secret）。

    响应绝对禁止包含：plugin_secret / plugin_secret_hash /
    api_key_ciphertext / api_key_nonce / api_key 明文。

    Raises:
        PluginCredentialsMissingError / PluginNotFoundError /
        PluginSecretMismatchError → 401；PluginDisabledError → 403。
    """
    # 身份已由 get_current_plugin 认证；此处以 plugin_id 重新查询最新数据
    # （current_plugin 仅用于身份，不直接透传）。
    plugin = plugin_service.get_plugin(current_plugin.plugin_id)
    return PluginMeResponse(
        plugin_id=plugin.plugin_id,
        plugin_name=plugin.plugin_name,
        status=plugin.status,
        api_key_configured=_api_key_configured(plugin),
        created_at=plugin.created_at,
        updated_at=plugin.updated_at,
    )


@router.put(
    "/me",
    response_model=PluginUpdateNameResponse,
)
def update_plugin_name(
    body: PluginUpdateNameRequest,
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> PluginUpdateNameResponse:
    """
    修改当前 Workspace 显示名（plugin_id 不变）。

    必须保证：plugin_id 不变 / plugin_secret_hash 不变 / API Key 不变 /
    documents 不变。名称规则由 PluginService 统一校验（与 register 一致）。

    Raises:
        PluginNameValidationError → 422；PluginNameTakenError → 409；
        认证类异常同 GET /plugins/me。
    """
    plugin = plugin_service.update_plugin_name(
        current_plugin.plugin_id, body.plugin_name
    )
    return PluginUpdateNameResponse(
        plugin_id=plugin.plugin_id,
        plugin_name=plugin.plugin_name,
    )


@router.put(
    "/me/api-key",
    response_model=PluginUpdateApiKeyResponse,
)
def update_plugin_api_key(
    body: PluginUpdateApiKeyRequest,
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> PluginUpdateApiKeyResponse:
    """
    配置 / 更换当前 Workspace 的百炼 API Key（AES-256-GCM 加密入库）。

    流程：PluginService.update_api_key(plugin_id, body.api_key)：
        - 用「用户提交的 Key」调 EmbeddingClient 最小 embedding 验证（失败 400）；
        - encrypt_api_key(key, APP_MASTER_KEY) → ciphertext + nonce 入库。

    安全红线：响应禁止返回 api_key / ciphertext / nonce / secret。

    Raises:
        ApiKeyValidationError → 400（Key 为空 / 非 sk- 前缀 / 验证失败）
        ApiKeyNotConfiguredError → 409（由 main.py 既有 handler 复用）
        SecurityConfigurationError → 500（APP_MASTER_KEY 配置缺陷）
    """
    plugin = plugin_service.update_api_key(
        current_plugin.plugin_id, body.api_key
    )
    return PluginUpdateApiKeyResponse(
        plugin_id=plugin.plugin_id,
        api_key_configured=_api_key_configured(plugin),
    )


@router.delete(
    "/me/api-key",
    status_code=204,
)
def remove_plugin_api_key(
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> Response:
    """
    清除当前 Workspace 的百炼 API Key（204 No Content）。

    仅清除 api_key_ciphertext / nonce → NULL；plugin_id / plugin_name /
    plugin_secret_hash / documents / Milvus 均不变。

    Raises:
        PluginNotFoundError → 401；PluginOperationError → 503。
    """
    plugin_service.remove_api_key(current_plugin.plugin_id)
    return Response(status_code=204)


@router.delete(
    "/me",
    status_code=204,
)
def delete_plugin_workspace(
    body: PluginDeleteRequest,
    current_plugin: PluginWorkspace = Depends(get_current_plugin),
    plugin_service: PluginService = Depends(get_plugin_service),
) -> Response:
    """
    删除当前 Workspace（危险操作；双重确认后 204 No Content）。

    确认条件（由 PluginService.delete_workspace 校验）：
        - body.confirm == True；
        - body.plugin_name == 当前 Workspace 显示名。

    本阶段仅删除 plugin_workspaces 行；documents → Milvus → FileStorage
    的级联删除属于 Step 2-E 的 Service 级联删除，本阶段不涉及。

    Raises:
        PluginDeleteConfirmationError → 400（confirm 缺失或名称不匹配）
        PluginNotFoundError → 401；PluginOperationError → 503。
    """
    plugin_service.delete_workspace(
        plugin_id=current_plugin.plugin_id,
        confirm=body.confirm,
        plugin_name=body.plugin_name,
    )
    return Response(status_code=204)
