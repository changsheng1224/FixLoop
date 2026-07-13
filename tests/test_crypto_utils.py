"""crypto_utils Fernet 加密/解密单测（V1.4-Bonus4c）。"""

from __future__ import annotations

import os

import pytest

from agent_runtime.crypto_utils import (
    _FERNET,
    _FERNET_LOADED,
    _get_fernet,
    decrypt,
    encrypt,
    encrypt_if_enabled,
    is_encryption_enabled,
)


# ---------------------------------------------------------------------------
# 环境隔离 helper
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fernet_cache():
    """每个测试前后重置 Fernet 缓存。"""
    import agent_runtime.crypto_utils as mod

    mod._FERNET = None
    mod._FERNET_LOADED = False
    yield
    mod._FERNET = None
    mod._FERNET_LOADED = False
    os.environ.pop("FIXLOOP_ENCRYPT_KEY", None)


def _set_key():
    """设置有效的 Fernet 密钥。"""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ["FIXLOOP_ENCRYPT_KEY"] = key
    return key


# ---------------------------------------------------------------------------
# 加密启用检测
# ---------------------------------------------------------------------------


class TestEncryptionEnabled:
    def test_disabled_when_no_key(self):
        assert not is_encryption_enabled()

    def test_enabled_when_key_set(self):
        _set_key()
        assert is_encryption_enabled()

    def test_disabled_when_key_empty(self):
        os.environ["FIXLOOP_ENCRYPT_KEY"] = ""
        assert not is_encryption_enabled()

    def test_fernet_cached_after_first_call(self):
        _set_key()
        f1 = _get_fernet()
        f2 = _get_fernet()
        assert f1 is f2  # 单例


# ---------------------------------------------------------------------------
# encrypt / decrypt
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    def test_encrypt_no_key_returns_plaintext(self):
        text = "secret patch content"
        result = encrypt(text)
        assert result == text  # 明文回退

    def test_decrypt_no_key_returns_input(self):
        token = "some_encrypted_token"
        result = decrypt(token)
        assert result == token

    def test_roundtrip_with_key(self):
        _set_key()
        text = "sensitive issue: fix app.py line 42"
        encrypted = encrypt(text)
        assert encrypted != text
        decrypted = decrypt(encrypted)
        assert decrypted == text

    def test_encrypt_empty_string(self):
        result = encrypt("")
        assert result == ""

    def test_decrypt_empty_string(self):
        result = decrypt("")
        assert result == ""

    def test_decrypt_plaintext_returns_unchanged(self):
        """非加密内容解密时返回原文（容错）。"""
        _set_key()
        result = decrypt("this is plain text, not a token")
        assert result == "this is plain text, not a token"

    def test_roundtrip_unicode(self):
        _set_key()
        text = "修复中文注释：第 42 行缺少 None 检查 🐛"
        encrypted = encrypt(text)
        decrypted = decrypt(encrypted)
        assert decrypted == text

    def test_roundtrip_multiline(self):
        _set_key()
        text = "--- a/app.py\n+++ b/app.py\n@@ -42,6 +42,8 @@\n+    if x is None:\n+        return 0"
        encrypted = encrypt(text)
        decrypted = decrypt(encrypted)
        assert decrypted == text


# ---------------------------------------------------------------------------
# encrypt_if_enabled
# ---------------------------------------------------------------------------


class TestEncryptIfEnabled:
    def test_encrypts_when_key_present(self):
        _set_key()
        text = "patch here"
        result = encrypt_if_enabled(text)
        assert result != text

    def test_passes_through_when_no_key(self):
        text = "patch here"
        result = encrypt_if_enabled(text)
        assert result == text


# ---------------------------------------------------------------------------
# 错误密钥
# ---------------------------------------------------------------------------


class TestWrongKey:
    def test_decrypt_with_wrong_key_returns_input(self):
        """密钥不匹配时 decrypt 返回原文（不抛异常）。"""
        from cryptography.fernet import Fernet

        # 用 key1 加密
        key1 = Fernet.generate_key().decode()
        os.environ["FIXLOOP_ENCRYPT_KEY"] = key1
        # 重置缓存让新 key 生效
        import agent_runtime.crypto_utils as mod
        mod._FERNET = None
        mod._FERNET_LOADED = False

        encrypted = encrypt("secret")

        # 换 key2 解密
        key2 = Fernet.generate_key().decode()
        os.environ["FIXLOOP_ENCRYPT_KEY"] = key2
        mod._FERNET = None
        mod._FERNET_LOADED = False

        result = decrypt(encrypted)
        # 密钥不匹配 → 返回原始输入（容错）
        assert result == encrypted
