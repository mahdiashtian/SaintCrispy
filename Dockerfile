FROM node:24-bookworm-slim AS js-runtime
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=js-runtime /usr/local/bin/node /usr/local/bin/node
COPY pyproject.toml ./
COPY README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
RUN useradd --create-home bot && mkdir -p /app/logs && chown bot:bot /app/logs
USER bot
CMD ["python", "-m", "downloader_bot"]
