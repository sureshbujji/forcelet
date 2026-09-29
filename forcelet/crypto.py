"""Field-level encryption for Forcelet (demo-grade).

Values of fields marked `encrypted: true` are encrypted at rest with Fernet
(AES-128-CBC + HMAC). The key comes from the FORCELET_ENC_KEY environment
variable, or is generated once and stored in `.forcelet.key` (gitignored).

This is demo-grade: for production, source the key from a real KMS and rotate
it; ciphertext here is not searchable or sortable.
"""
import base64
import os

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:"
KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".forcelet.key")


def get_key() -> bytes:
    env = os.environ.get("FORCELET_ENC_KEY")
    if env:
        raw = env.encode()
        # accept a raw Fernet key or a passphrase-ish string (padded/hashed below)
        try:
            Fernet(raw)
            return raw
        except Exception:
            pass
        import hashlib
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def encrypt(plaintext: str) -> str:
    if plaintext is None or plaintext == "":
        return plaintext
    return PREFIX + Fernet(get_key()).encrypt(str(plaintext).encode()).decode()


def decrypt(ciphertext):
    if not isinstance(ciphertext, str) or not ciphertext.startswith(PREFIX):
        return ciphertext
    try:
        return Fernet(get_key()).decrypt(ciphertext[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return ciphertext  # key changed: surface raw rather than crash


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)
