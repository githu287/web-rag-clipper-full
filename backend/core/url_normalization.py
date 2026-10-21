"""Web page URL validation and conservative canonicalization."""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_KEYS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "scm",
        "spm",
        "twclid",
        "vero_conv",
        "vero_id",
        "yclid",
        "_hsenc",
        "_hsmi",
    }
)

# 路由型 fragment 判定：哈希路由 SPA 的不同页面只体现在 `#/...` 里，剥掉会把
# `#/users/1` 与 `#/users/2` 折叠成同一身份，后一次剪藏原位覆盖上一篇；普通页内
# 锚点（`#part`）仍剥掉。旧写法 `#!/x` 归一成 `#/x`——两侧本来就都会原样保留它，
# 归一的理由是让同一条路由的两种写法共享一个文档键。规则须与 url-utils.js 一致，
# 两侧共享测试向量。
_ROUTE_FRAGMENT_PREFIX = "/"
_LEGACY_ROUTE_FRAGMENT_PREFIX = "!/"


def normalize_web_url(value: str) -> str:
    """Return a stable HTTP(S) URL suitable for webpage identity matching.

    The transformation is intentionally conservative: it removes well-known
    tracking parameters, lowercases the scheme/host, removes default ports, and
    gives an empty path the canonical ``/`` form. Query parameter order and
    non-tracking parameters are preserved because some sites treat them as
    semantically significant.

    Fragments are stripped with one exception: a *route-like* fragment (one
    starting with ``/``, where the legacy ``#!/x`` form folds into ``#/x``) is
    kept, because hash-routing SPA pages differ only there and dropping it would
    let ``#/users/2`` overwrite ``#/users/1``. Plain in-page anchors (``#part``)
    stay stripped so documents already stored under a fragment-free URL keep
    matching; a kept fragment counts toward the 2048-character limit.
    """

    raw = value.strip()
    if not raw:
        raise ValueError("web URL must not be empty")

    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("web URL scheme must be http or https")
        if parts.username is not None or parts.password is not None:
            raise ValueError("web URL must not contain credentials")
        if not parts.hostname:
            raise ValueError("web URL must include a host")

        hostname = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("web URL"):
            raise
        raise ValueError("invalid web URL") from exc

    if not hostname:
        raise ValueError("web URL must include a host")

    display_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{display_host}:{port}"
    else:
        netloc = display_host

    query_items = []
    for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
        normalized_key = key.lower()
        if normalized_key.startswith("utm_") or normalized_key in _TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, item_value))

    path = quote(
        parts.path or "/",
        safe="/:%@!$&'()*+,;=-._~",
    )
    query = urlencode(query_items, doseq=True)
    fragment = parts.fragment
    if fragment.startswith(_LEGACY_ROUTE_FRAGMENT_PREFIX):
        fragment = fragment[1:]
    if fragment.startswith(_ROUTE_FRAGMENT_PREFIX):
        # 浏览器 URL 解析器已把非 ASCII 路由预先百分号编码，这里必须同样编码，
        # 否则后端与扩展端会对同一个页面算出两个不同的键。
        fragment = quote(fragment, safe="/:%@!$&'()*+,;=?-._~")
    else:
        fragment = ""
    normalized = urlunsplit((scheme, netloc, path, query, fragment))
    if len(normalized) > 2048:
        raise ValueError("normalized web URL must be at most 2048 characters")
    return normalized
