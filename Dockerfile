# Forcelet — production image (gunicorn + TLS-terminating proxy in front)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORCELET_BEHIND_PROXY=1

WORKDIR /app
COPY pyproject.toml ./
COPY forcelet ./forcelet
COPY web ./web
COPY metadata ./metadata
COPY wsgi.py gunicorn.conf.py ./

RUN pip install --no-cache-dir . gunicorn

# Data lives outside the image: mount volumes for the DB and backups.
VOLUME ["/data"]
ENV FORCELET_DB=/data/forcelet.db \
    FORCELET_BACKUP_DIR=/data/backups

EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
