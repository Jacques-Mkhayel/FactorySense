"""FactorySense API: authentication, telemetry and alert queries, live alerts over WebSocket.
Served behind Traefik under /api. Stateless: sessions live in the database, so any replica answers."""
import asyncio
import contextlib
import logging
import os
import secrets
import socket
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlparse

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel, Field

from .auth import (COOKIE, DUMMY_HASH, SESSION_TTL_H, current_user, hash_password, require_admin,
                   session_user, token_hash, verify_password)
from .db import CONNINFO, pool

SERVICE = os.getenv("SERVICE_NAME", "api")
REPLICA = socket.gethostname()  # tells the --scale api=3 replicas apart

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s level=%(levelname)s service=%(name)s msg=%(message)s")
log = logging.getLogger(SERVICE)

class Credentials(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(max_length=128)


class AlertUpdate(BaseModel):
    status: Literal["acknowledged", "resolved"]


class NewUser(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9._-]{3,32}$")
    password: str = Field(min_length=10, max_length=128)
    role: Literal["admin", "analyst"] = "analyst"


class UserUpdate(BaseModel):
    role: Literal["admin", "analyst"] | None = None
    active: bool | None = None
    password: str | None = Field(None, min_length=10, max_length=128)


# ---------------------------------------------------------------- live alerts fan-out
subscribers: set[asyncio.Queue] = set()


async def listen_alerts():
    """One LISTEN connection per replica: every alert change (trigger in 020-app-schema.sql)
    is pushed to this replica's WebSocket clients, without polling the table."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(CONNINFO, autocommit=True) as conn:
                await conn.execute("LISTEN alerts")
                log.info("listening for alert changes")
                async for note in conn.notifies():
                    for queue in subscribers:
                        with contextlib.suppress(asyncio.QueueFull):  # a stalled client drops, not blocks
                            queue.put_nowait(note.payload)
        except Exception as e:
            log.warning("alert listener lost (%s), retrying in 3s", e)
            await asyncio.sleep(3)


async def bootstrap_admin():
    """First admin from .env; no-op once it exists, so changing it later happens in the dashboard."""
    username, password = os.getenv("ADMIN_USERNAME"), os.getenv("ADMIN_PASSWORD")
    if username and password:
        async with pool.connection() as conn:
            await conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin') "
                               "ON CONFLICT (username) DO NOTHING", (username, hash_password(password)))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await pool.open(wait=True)
    await bootstrap_admin()
    listener = asyncio.create_task(listen_alerts())
    log.info("started replica=%s", REPLICA)
    yield
    listener.cancel()
    await pool.close()


# root_path matches the prefix Traefik strips, so /api/docs resolves correctly behind the proxy.
app = FastAPI(title="FactorySense API", root_path="/api", lifespan=lifespan)


@app.middleware("http")
async def replica_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Replica"] = REPLICA
    return response


async def fetch(sql: str, params=None, one: bool = False):
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await (cur.fetchone() if one else cur.fetchall())


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "replica": REPLICA}


# ---------------------------------------------------------------- authentication
@app.post("/auth/login")
async def login(body: Credentials, request: Request, response: Response):
    user = await fetch("SELECT id, username, role, password_hash FROM users WHERE username = %s AND active",
                       (body.username.lower(),), one=True)
    # scrypt is CPU-bound: run it off the event loop so one login does not stall every request.
    ok = await asyncio.to_thread(verify_password, body.password, user["password_hash"] if user else DUMMY_HASH)
    if not (user and ok):
        log.warning("login failed user=%r ip=%s", body.username, request.client.host)
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        await conn.execute("INSERT INTO sessions VALUES (%s, %s, now() + make_interval(hours => %s))",
                           (token_hash(token), user["id"], SESSION_TTL_H))
        await conn.execute("UPDATE users SET last_login = now() WHERE id = %s", (user["id"],))
    # HttpOnly: page scripts never see the token. SameSite=Strict: no cross-site requests carry it (CSRF).
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL_H * 3600, path="/api",
                        httponly=True, secure=True, samesite="strict")
    log.info("login ok user=%s ip=%s", user["username"], request.client.host)
    return {"username": user["username"], "role": user["role"]}


@app.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    if token := request.cookies.get(COOKIE):
        await fetch("DELETE FROM sessions WHERE token_hash = %s RETURNING 1", (token_hash(token),))
    response.delete_cookie(COOKIE, path="/api")


@app.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "role": user["role"]}


# ---------------------------------------------------------------- alerts
@app.get("/alerts", dependencies=[Depends(current_user)])
async def list_alerts(source: Literal["equipment", "ids"] | None = None,
                      severity: Literal["info", "warning", "critical"] | None = None,
                      status: Literal["active", "acknowledged", "resolved"] | None = None,
                      limit: int = Query(200, ge=1, le=500)):
    return await fetch("""SELECT * FROM alerts
        WHERE (%(source)s::text IS NULL OR source = %(source)s)
          AND (%(severity)s::text IS NULL OR severity = %(severity)s)
          AND (%(status)s::text IS NULL OR status = %(status)s)
        ORDER BY time DESC LIMIT %(limit)s""",
                       {"source": source, "severity": severity, "status": status, "limit": limit})


@app.get("/alerts/summary", dependencies=[Depends(current_user)])
async def alerts_summary():
    return await fetch("""SELECT
        count(*) FILTER (WHERE status = 'active' AND severity = 'critical') AS critical,
        count(*) FILTER (WHERE status = 'active' AND severity = 'warning')  AS warning,
        count(*) FILTER (WHERE status = 'active' AND severity = 'info')     AS info,
        count(*) FILTER (WHERE status <> 'resolved' AND source = 'ids')       AS ids,
        count(*) FILTER (WHERE status <> 'resolved' AND source = 'equipment') AS equipment,
        count(*) FILTER (WHERE status = 'acknowledged') AS acknowledged,
        count(*) FILTER (WHERE status = 'resolved' AND resolved_at > now() - INTERVAL '24 hours') AS resolved_24h
        FROM alerts""", one=True)


# Allowed workflow transitions: an analyst acknowledges an active alert, or closes an open one.
TRANSITIONS = {"acknowledged": ["active"], "resolved": ["active", "acknowledged"]}


@app.patch("/alerts/{alert_id}")
async def update_alert(alert_id: int, body: AlertUpdate, user: dict = Depends(current_user)):
    row = await fetch("""UPDATE alerts SET status = %(status)s,
            acknowledged_at = CASE WHEN %(status)s = 'acknowledged' THEN now() ELSE acknowledged_at END,
            acknowledged_by = CASE WHEN %(status)s = 'acknowledged' THEN %(user)s ELSE acknowledged_by END,
            resolved_at     = CASE WHEN %(status)s = 'resolved' THEN now() END
        WHERE id = %(id)s AND status = ANY(%(from)s) RETURNING *""",
                      {"status": body.status, "user": user["username"], "id": alert_id,
                       "from": TRANSITIONS[body.status]}, one=True)
    if row is None:
        raise HTTPException(409, "Alert not found or transition not allowed")
    log.info("alert id=%d -> %s by %s", alert_id, body.status, user["username"])
    return row


# ---------------------------------------------------------------- telemetry
@app.get("/machines", dependencies=[Depends(current_user)])
async def machines():
    return await fetch("""SELECT DISTINCT ON (machine_id) machine_id, site_id, time AS last_seen
        FROM telemetry WHERE time > now() - INTERVAL '7 days' ORDER BY machine_id, time DESC""")


@app.get("/telemetry", dependencies=[Depends(current_user)])
async def telemetry(machine_id: str, hours: int = Query(1, ge=1, le=168)):
    if hours > 24:  # long ranges come from the hourly continuous aggregate
        return await fetch("""SELECT bucket AS time, temperature_avg AS temperature,
                   vibration_avg AS vibration, pressure_avg AS pressure FROM telemetry_1h
            WHERE machine_id = %s AND bucket > now() - make_interval(hours => %s) ORDER BY bucket""",
                           (machine_id, hours))
    # ~360 points whatever the range: 10 s buckets for 1 h, 1 min for 6 h, 4 min for 24 h.
    return await fetch("""SELECT time_bucket(make_interval(secs => %(width)s), time) AS time,
               avg(temperature) AS temperature, avg(vibration) AS vibration, avg(pressure) AS pressure
        FROM telemetry WHERE machine_id = %(machine)s AND time > now() - make_interval(hours => %(hours)s)
        GROUP BY 1 ORDER BY 1""", {"width": hours * 10, "machine": machine_id, "hours": hours})


# ---------------------------------------------------------------- users (admin only)
@app.get("/users", dependencies=[Depends(require_admin)])
async def list_users():
    return await fetch("SELECT id, username, role, active, created_at, last_login FROM users ORDER BY id")


@app.post("/users", status_code=201)
async def create_user(body: NewUser, admin: dict = Depends(require_admin)):
    password_hash = await asyncio.to_thread(hash_password, body.password)
    try:
        row = await fetch("""INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)
            RETURNING id, username, role, active, created_at, last_login""",
                          (body.username, password_hash, body.role), one=True)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Username already exists")
    log.info("user created username=%s role=%s by %s", body.username, body.role, admin["username"])
    return row


@app.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, admin: dict = Depends(require_admin)):
    if user_id == admin["id"] and (body.role or body.active is not None):
        raise HTTPException(400, "You cannot change your own role or status")  # avoids locking out the last admin
    password_hash = await asyncio.to_thread(hash_password, body.password) if body.password else None
    async with pool.connection() as conn:
        cur = await conn.execute("""UPDATE users SET role = coalesce(%s, role), active = coalesce(%s, active),
                password_hash = coalesce(%s, password_hash)
            WHERE id = %s RETURNING id, username, role, active, created_at, last_login""",
                                 (body.role, body.active, password_hash, user_id))
        row = await cur.fetchone()
        if row and (body.active is False or password_hash):  # revoke every open session at once
            await conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    if row is None:
        raise HTTPException(404, "User not found")
    log.info("user updated id=%d by %s", user_id, admin["username"])
    return row


# ---------------------------------------------------------------- live alerts
@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket):
    user = await session_user(ws.cookies.get(COOKIE))
    # SameSite=Strict already keeps the cookie off cross-site handshakes; the Origin check is defence in depth.
    same_origin = urlparse(ws.headers.get("origin", "")).netloc == ws.headers.get("host")
    if user is None or not same_origin:
        await ws.close(code=1008)
        return
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    subscribers.add(queue)
    try:
        await ws.send_json({"type": "hello", "replica": REPLICA})
        while True:
            await ws.send_text(f'{{"type":"alert","alert":{await queue.get()}}}')
    except Exception:  # client went away; nothing else to clean up than the subscription
        pass
    finally:
        subscribers.discard(queue)
