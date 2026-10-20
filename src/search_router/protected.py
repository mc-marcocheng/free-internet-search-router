import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ProtectionError(Exception):
    pass


class SecretCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ProtectionError("invalid_master_key")
        self.cipher = AESGCM(key)

    def encrypt(self, identity: str, value: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, value.encode(), identity.encode())

    def decrypt(self, identity: str, value: bytes) -> str:
        try:
            return self.cipher.decrypt(value[:12], value[12:], identity.encode()).decode()
        except Exception:
            raise ProtectionError("credential_decryption_failed") from None


def system_cipher(database: Path, key_file: Path | None = None, create=False) -> SecretCipher:
    try:
        if key_file is not None:
            encoded = key_file.read_bytes().strip()
        else:
            import keyring

            backend = keyring.get_keyring()
            candidates = getattr(backend, "backends", [backend])
            secure = next(
                (
                    item
                    for item in candidates
                    if type(item).__module__
                    in {
                        "keyring.backends.Windows",
                        "keyring.backends.macOS",
                        "keyring.backends.SecretService",
                        "keyring.backends.kwallet",
                        "keyring.backends.libsecret",
                    }
                ),
                None,
            )
            if secure is None:
                raise ProtectionError("os_credential_store_unavailable")
            identity = hashlib.sha256(str(database.resolve()).encode()).hexdigest()
            encoded = secure.get_password("internet-search-router", identity)
            if encoded is None and create:
                encoded = base64.urlsafe_b64encode(os.urandom(32)).decode()
                secure.set_password("internet-search-router", identity, encoded)
            if encoded is None:
                raise ProtectionError("master_key_missing")
        return SecretCipher(base64.b64decode(encoded, altchars=b"-_", validate=True))
    except ProtectionError:
        raise
    except Exception:
        raise ProtectionError("protected_key_store_unavailable") from None
