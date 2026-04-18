import hashlib
import secrets

password = "ManaDaakuuAdmin2026!"  # Change this if you want a different admin password

salt = secrets.token_bytes(16)
password_hash = hashlib.pbkdf2_hmac(
    "sha256",
    password.encode("utf-8"),
    salt,
    100_000,
)

print(f"{salt.hex()}${password_hash.hex()}")

