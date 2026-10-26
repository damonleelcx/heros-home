# The market desk: coordinator and workers run from this one image. The only dependency is the
# PostgreSQL driver; the desk falls back to SQLite when DESK_DB is a file path.
FROM python:3.12-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3" \
 && useradd -u 10001 -m desk && mkdir -p /var/lib/desk/pub && chown -R desk /var/lib/desk
WORKDIR /app
COPY desk/ /app/desk/
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DESK_PUBLISH_DIR=/var/lib/desk/pub DESK_ENV_FILE=/nonexistent
ENTRYPOINT ["python", "-m", "desk"]
CMD ["coordinator"]
