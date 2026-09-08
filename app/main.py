"""Mur de messages ephemeres.

Redis detient le mur vivant : chaque message y expire tout seul (TTL).
PostgreSQL detient l'archive : rien n'est perdu, meme apres disparition.
"""

import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

TTL = int(os.getenv("MESSAGE_TTL", "120"))          # duree de vie en secondes
REDIS_URL = os.getenv("REDIS_URL", "redis://cache:6379/0")

DSN = (
    f"host={os.getenv('POSTGRES_HOST', 'db')} "
    f"port={os.getenv('POSTGRES_PORT', '5432')} "
    f"dbname={os.getenv('POSTGRES_DB', 'mur')} "
    f"user={os.getenv('POSTGRES_USER', 'mur')} "
    f"password={os.getenv('POSTGRES_PASSWORD', '')}"
)

WALL_KEY = "wall"          # sorted set : id -> timestamp d'expiration
MSG_KEY = "msg:{}"         # contenu du message, avec TTL


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = AsyncConnectionPool(DSN, min_size=1, max_size=5, open=False)
    await app.state.pool.open(wait=True, timeout=30)
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield
    await app.state.redis.aclose()
    await app.state.pool.close()


app = FastAPI(title="Mur ephemere", lifespan=lifespan)


class NewMessage(BaseModel):
    author: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=280)


@app.get("/health")
async def health():
    try:
        await app.state.redis.ping()
        async with app.state.pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ok"}


@app.post("/api/messages", status_code=201)
async def post_message(msg: NewMessage):
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=TTL)

    # 1. Archive durable en base
    async with app.state.pool.connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO messages (author, body, expires_at) "
                "VALUES (%s, %s, %s) RETURNING id",
                (msg.author.strip(), msg.body.strip(), expires_at),
            )
        ).fetchone()
    message_id = row[0]

    # 2. Copie vivante en cache, qui s'effacera d'elle-meme
    payload = json.dumps(
        {
            "id": message_id,
            "author": msg.author.strip(),
            "body": msg.body.strip(),
            "created_at": now.isoformat(),
            "expires_at": expires_at.timestamp(),
        }
    )
    pipe = app.state.redis.pipeline()
    pipe.setex(MSG_KEY.format(message_id), TTL, payload)
    pipe.zadd(WALL_KEY, {str(message_id): expires_at.timestamp()})
    await pipe.execute()

    return {"id": message_id, "ttl": TTL}


@app.get("/api/messages")
async def list_messages():
    """Lit uniquement Redis : aucune requete SQL sur le chemin chaud."""
    now = time.time()
    r = app.state.redis

    # On purge du sorted set les ids dont le message a deja expire
    await r.zremrangebyscore(WALL_KEY, "-inf", now)

    ids = await r.zrange(WALL_KEY, 0, -1)
    if not ids:
        return {"ttl": TTL, "messages": []}

    raw = await r.mget([MSG_KEY.format(i) for i in ids])
    messages = [json.loads(item) for item in raw if item]
    messages.sort(key=lambda m: m["created_at"], reverse=True)

    for m in messages:
        m["remaining"] = max(0, round(m["expires_at"] - now))

    return {"ttl": TTL, "messages": messages}


@app.get("/api/archive")
async def archive(limit: int = 50):
    """Ce que le mur a oublie mais que la base a garde."""
    limit = max(1, min(limit, 200))
    async with app.state.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT id, author, body, created_at FROM messages "
                "ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
        ).fetchall()
    return {
        "messages": [
            {
                "id": r[0],
                "author": r[1],
                "body": r[2],
                "created_at": r[3].isoformat(),
            }
            for r in rows
        ]
    }


@app.get("/api/stats")
async def stats():
    live = await app.state.redis.zcount(WALL_KEY, time.time(), "+inf")
    async with app.state.pool.connection() as conn:
        total = (await (await conn.execute("SELECT count(*) FROM messages")).fetchone())[0]
    return {"live": live, "archived": total, "expired": total - live, "ttl": TTL}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
