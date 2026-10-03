from cryptography.fernet import Fernet, InvalidToken
import pytest

from app.crypto import SecretCipher


SYNTHETIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
# Generated with cryptography 49.0.0, using only synthetic key material and plaintext.
LEGACY_TOKEN = (
    "gAAAAABlU_EAFbxjfY-TxjsyFOWaA5qLXDcdINiTcSQCzjZWZa5Uy71kp6RKFHw4VgrGtLjqMLU"
    "ZXCTwybh8kMXSnRFmXe489KqGYHZnwr8Cp1W0s8STbg3WHJHxDC5fpo6yGcZ2731g"
)
LEGACY_PLAINTEXT = "synthetic-password;{compatibility}-\u4e2d\u6587"


def test_secret_cipher_decrypts_cryptography_49_ciphertext():
    cipher = SecretCipher.from_key_material(SYNTHETIC_KEY)

    assert cipher.decrypt(LEGACY_TOKEN) == LEGACY_PLAINTEXT


def test_secret_cipher_round_trips_legacy_plaintext():
    cipher = SecretCipher.from_key_material(SYNTHETIC_KEY)

    encrypted = cipher.encrypt(LEGACY_PLAINTEXT)

    assert encrypted != LEGACY_PLAINTEXT
    assert cipher.decrypt(encrypted) == LEGACY_PLAINTEXT


def test_secret_cipher_rejects_legacy_token_with_wrong_key():
    cipher = SecretCipher.from_key_material(Fernet.generate_key().decode())

    with pytest.raises(InvalidToken):
        cipher.decrypt(LEGACY_TOKEN)


def test_secret_cipher_rejects_tampered_legacy_ciphertext():
    cipher = SecretCipher.from_key_material(SYNTHETIC_KEY)
    tampered = LEGACY_TOKEN[:30] + ("A" if LEGACY_TOKEN[30] != "A" else "B") + LEGACY_TOKEN[31:]

    with pytest.raises(InvalidToken):
        cipher.decrypt(tampered)
