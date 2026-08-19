# Frontend + proxy for the watermarks-remover engine.
#
# This image contains only our own code. The engine runs in its own published
# image (see compose.yml) and is never copied, vendored or rebuilt here.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GUI_BIND=0.0.0.0 \
    GUI_PORT=8080

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

# Unprivileged, and owning nothing it could write to.
RUN useradd --system --uid 10101 --create-home --home-dir /home/wrgui wrgui
USER wrgui

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/ping', timeout=4).status == 200 else 1)"

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
