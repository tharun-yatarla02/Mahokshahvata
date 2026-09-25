"""
passwords.py

Local email + password accounts: password hashing and session tokens,
standard library only.

    - Passwords are hashed with scrypt and a random per-user salt; only the
      hash is stored. Stored as "scrypt$n$r$p$salt_hex$hash_hex" so the cost
      settings can be raised later without breaking existing accounts.
    - A session token is 32 random bytes sent to the browser once. The
      database stores only its SHA-256, so a leaked database can't be used
      to sign in.
"""

import hashlib
import hmac
import secrets

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_DAYS = 30


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt_hex, digest_hex = (stored or "").split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def new_session_token():
    return secrets.token_urlsafe(32)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
