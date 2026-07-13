"""敏感产物加密（V1.4-Bonus4c）。

基于 Fernet 对称加密，opt-in：仅当 ``FIXLOOP_ENCRYPT_KEY`` 设置时启用。
``cryptography`` 未安装或不设置密钥时，静默降级为明文。

Usage::

    # 生成密钥（一次性）
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # 设置环境变量
    export FIXLOOP_ENCRYPT_KEY="<生成的密钥>"

    # 应用层
    from agent_runtime.crypto_utils import encrypt, decrypt
    ciphertext = encrypt("sensitive patch content")
    plaintext = decrypt(ciphertext)  # 密钥匹配则返回原文，否则返回密文
"""

from __future__ import annotations

import os

_FERNET = None
_FERNET_LOADED = False  # 区分"未尝试加载"与"加载失败"


def _get_fernet():
    """延迟加载 Fernet 实例（模块级单例）。"""
    global _FERNET, _FERNET_LOADED
    if _FERNET_LOADED:
        return _FERNET
    _FERNET_LOADED = True

    key = os.environ.get("FIXLOOP_ENCRYPT_KEY", "").strip()
    if not key:
        return None

    try:
        from cryptography.fernet import Fernet
        _FERNET = Fernet(key.encode())
    except Exception:
        _FERNET = None
    return _FERNET


def is_encryption_enabled() -> bool:
    """检查加密是否可用。"""
    return _get_fernet() is not None


def encrypt(text: str) -> str:
    """加密文本。加密不可用时返回明文。

    Args:
        text: 明文内容。

    Returns:
        若加密启用：Fernet token（base64 字符串）。
        若加密不可用：原始明文。
    """
    if not text:
        return text
    f = _get_fernet()
    if f is None:
        return text
    try:
        return f.encrypt(text.encode()).decode()
    except Exception:
        return text


def decrypt(token: str) -> str:
    """解密文本。加密不可用或密钥不匹配时返回原文。

    Args:
        token: Fernet token 或明文。

    Returns:
        解密成功返回明文；否则返回原始输入。
    """
    if not token:
        return token
    f = _get_fernet()
    if f is None:
        return token
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        return token


def encrypt_if_enabled(text: str) -> str:
    """便捷方法：加密可用的明文内容。"""
    return encrypt(text) if is_encryption_enabled() else text
