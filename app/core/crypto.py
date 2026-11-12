"""连接凭证对称加解密与脱敏工具."""

import base64
import hashlib
from urllib.parse import urlparse
from cryptography.fernet import Fernet, InvalidToken


def _build_fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_uri(raw_uri: str, secret_key: str) -> str:
    """使用 Fernet 加密外部数据源连接串."""
    return _build_fernet(secret_key).encrypt(raw_uri.encode("utf-8")).decode("utf-8")


def decrypt_uri(encrypted_value: str, secret_key: str) -> str:
    """解密外部数据源连接串."""
    try:
        return _build_fernet(secret_key).decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("数据源凭证解密失败，请检查 SECRET_KEY 配置") from exc


def mask_connection_uri(raw_uri: str) -> str:
    """提取脱敏端点信息，隐藏用户名与密码凭证."""
    parsed = urlparse(raw_uri)
    if parsed.scheme and parsed.hostname:
        port_part = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path or ""
        return f"{parsed.hostname}{port_part}{path_part}"
    return raw_uri[:64]
