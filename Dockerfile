FROM python:3.12-slim

# discord.py needs no build toolchain, so this stays a single small layer.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY relay/ ./relay/

# Nothing here needs root, and the mounts it reads are world-readable.
RUN useradd -r -u 1000 relay
USER relay

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-m", "relay"]
