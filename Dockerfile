FROM python:3.14-slim

ARG APP_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    REQUEST_TIMEOUT=20 \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data/backups

EXPOSE 8080

# python:slim has no curl/wget, so the check uses the standard library.
# /healthz is exempt from Cloudflare Access and from the setup gate.
# ProxyHandler({}) ignores HTTP_PROXY, which would otherwise catch the loopback probe.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/healthz', timeout=5)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
