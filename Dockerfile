FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app

# FFmpeg plays the music (1.3.0); libopus is what Discord voice is encoded with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# yt-dlp[default] brings the scripts it needs for YouTube, and deno runs them. discord.py 2.7 caps PyNaCl
# below 1.6, which misses security fixes; 1.6.2 works the same for voice (1.4.1).
RUN pip install -r requirements.txt \
    && pip install --no-deps "PyNaCl==1.6.2"

COPY . .

# Run as a non-root user that owns /data (Exocomp mounts the unit's volume there).
RUN useradd --create-home --uid 10001 ursula \
    && mkdir -p /data \
    && chown ursula:ursula /data
USER ursula

# on_ready touches /tmp/ready; a bot that starts but never connects fails the deploy.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD test -f /tmp/ready

CMD ["python", "bot.py"]
