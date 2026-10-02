"""Password hashing, server-side sessions and the FastAPI dependencies that enforce them."""
import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, Request

from .db import pool

COOKIE = "fs_session"
SESSION_TTL_H = int(os.getenv("SESSION_TTL_H", "8"))
# scrypt is memory-hard (16 MiB per guess here), which makes offline cracking of a leaked hash costly.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(candidate.hex(), digest)


# Checked when the username does not exist, so "unknown user" and "wrong password" take the same time.
DUMMY_HASH = hash_password(secrets.token_hex())


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = %s AND s.expires_at > now() AND u.active""", (token_hash(token),))
        return await cur.fetchone()


async def current_user(request: Request) -> dict:
    user = await session_user(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return user
