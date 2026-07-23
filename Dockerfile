FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY broadcaster ./broadcaster

RUN addgroup --system bot && adduser --system --ingroup bot bot \
    && mkdir -p /data && chown bot:bot /data

USER bot

ENV DATABASE_PATH=/data/broadcaster.sqlite3

CMD ["python", "-m", "broadcaster"]

