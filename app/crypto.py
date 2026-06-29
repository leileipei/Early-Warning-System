import base64
import hashlib

from cryptography.fernet import Fernet


class SecretCipher:
    def __init__(self, fernet: Fernet):
        self._fernet = fernet

    @classmethod
    def from_key_material(cls, key_material: str) -> "SecretCipher":
        digest = hashlib.sha256(key_material.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        return cls(Fernet(key))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        return self._fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
