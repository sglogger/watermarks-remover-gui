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

# exiftool for the optional local metadata check (GUI_EXIFTOOL=1). Present but
# unused when the check is off, which is the default: the engine runs its own
# copy in its own container, and this one only exists to read the tags the
# engine's report leaves out.
#
# Archive::Zip is not optional despite being a recommendation: without it
# exiftool cannot open a ZIP container at all, so every DOCX, XLSX, PPTX, ODT
# and EPUB comes back as "FileType: ZIP" with a handful of archive fields and
# none of the document metadata — which is precisely what this check exists to
# read. exiftool says so in a warning that is easy to miss.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libimage-exiftool-perl \
        libarchive-zip-perl \
    && rm -rf /var/lib/apt/lists/*

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
