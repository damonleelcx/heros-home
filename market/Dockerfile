# The market desk: coordinator and workers run from this one image (stdlib only, no pip install).
FROM python:3.12-alpine
RUN adduser -D -u 10001 desk && mkdir -p /var/lib/desk && chown desk /var/lib/desk
WORKDIR /app
COPY desk/ /app/desk/
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DESK_DB=/var/lib/desk/desk.sqlite3 DESK_PUBLISH_DIR=/var/lib/desk/pub DESK_ENV_FILE=/nonexistent
ENTRYPOINT ["python", "-m", "desk"]
CMD ["coordinator"]
