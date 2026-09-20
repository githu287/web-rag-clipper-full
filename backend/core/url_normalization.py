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


def normalize_web_url(value: str) -> str:
    """Return a stable HTTP(S) URL suitable for webpage identity matching.

    The transformation is intentionally conservative: it removes fragments and
    well-known tracking parameters, lowercases the scheme/host, removes default
    ports, and gives an empty path the canonical ``/`` form. Query parameter
    order and non-tracking parameters are preserved because some sites treat
    them as semantically significant.
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
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if len(normalized) > 2048:
        raise ValueError("normalized web URL must be at most 2048 characters")
    return normalized
