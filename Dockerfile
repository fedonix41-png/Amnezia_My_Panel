# syntax=docker/dockerfile:1

FROM python:3.14-slim

# Non-root system user — the panel process runs as this user, never as root.
RUN useradd -r -u 1000 -d /app -s /sbin/nologin panel

WORKDIR /app

# Prevent __pycache__ writes so the container can run with a read-only rootfs.
ENV PYTHONDONTWRITEBYTECODE=1

# Install Python dependencies as root (pip needs write access at build time).
COPY requirements.txt requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code owned by the unprivileged user.
COPY --chown=panel:panel . .

# Writable data directory (persisted via volume in docker-compose).
RUN mkdir -p /app/data && chown panel:panel /app/data

USER panel

EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "app.py"]
