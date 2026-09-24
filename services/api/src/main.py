"""API stub: REST + WebSocket query layer, served behind Traefik under /api."""
import logging
import os
import socket
from contextlib import asynccontextmanager

from fastapi import FastAPI

SERVICE = os.getenv("SERVICE_NAME", "api")
CONFIG = {k: os.getenv(k) for k in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER")}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s level=%(levelname)s service=%(name)s msg=%(message)s")
log = logging.getLogger(SERVICE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The hostname identifies the replica, which matters once we run --scale api=3.
    log.info("started replica=%s config=%s", socket.gethostname(), CONFIG)
    yield


# root_path matches the prefix Traefik strips, so /api/docs resolves correctly behind the proxy.
app = FastAPI(title="FactorySense API", root_path="/api", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# TODO(phase 2): /assets, /alerts, /telemetry, /ws/alerts, and expose the replica id in responses.
