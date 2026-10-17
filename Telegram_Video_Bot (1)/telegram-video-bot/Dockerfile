FROM denoland/deno:bin AS deno
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid 10001 --create-home bot \
    && mkdir -p /app/data /app/secrets \
    && chown -R bot:bot /app
COPY --chown=bot:bot videobot ./videobot
USER bot
CMD ["python", "-m", "videobot"]
