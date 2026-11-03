# The recipe for the bot's "box" (container). Cloudflare, Railway and Fly.io read this
# file, build the box, and run it.

FROM python:3.13-slim

# ffmpeg is the video tool used to make animated stickers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stickerbot ./stickerbot

# The bot's small database lives in /data - attach a persistent volume there
# (not needed on Cloudflare, where it lives in D1). Port 8080 is used in webhook mode.
EXPOSE 8080
ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data

CMD ["python", "-m", "stickerbot"]
