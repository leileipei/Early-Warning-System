from cryptography.fernet import Fernet


class SecretCipher:
    def __init__(self, fernet: Fernet):
        self._fernet = fernet

    @classmethod
    def from_key_material(cls, key_material: str) -> "SecretCipher":
        return cls(Fernet(key_material.encode("utf-8")))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        return self._fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
