FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
# The buffer dir is created here, owned by the app user, so the named volume inherits that ownership.
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --no-create-home app \
 && mkdir -p /var/lib/gateway \
 && chown app /var/lib/gateway

COPY src/ ./src/

USER app
CMD ["python", "src/main.py"]
