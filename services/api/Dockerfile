FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home app

COPY src/ ./src/

USER app
EXPOSE 8000
# Access log off: the healthcheck would flood it; Traefik keeps the request log.
# --proxy-headers: client IPs in the login audit log come from Traefik's X-Forwarded-For. Only Traefik
# reaches the api (no host port), but any cloud_net container could spoof it: see README "Limits".
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips=*"]
