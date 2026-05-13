from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, master_key: str) -> None:
        key = (master_key or "").strip().encode("utf-8")
        if not key:
            raise CryptoError("MASTER_ENCRYPTION_KEY is empty")
        try:
            self._fernet = Fernet(key)
        except Exception as exc:
            raise CryptoError("MASTER_ENCRYPTION_KEY is invalid for Fernet") from exc

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        try:
            clear = self._fernet.decrypt(ciphertext.encode("utf-8"))
        except InvalidToken as exc:
            raise CryptoError("Cannot decrypt secret; invalid token/key") from exc
        return clear.decode("utf-8")
