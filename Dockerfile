FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    SSHPORT_CONFIG=/app/config/config.toml

RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 sudo tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 sshport \
    && useradd --uid 10001 --gid sshport --create-home --shell /usr/sbin/nologin sshport

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY docker/healthcheck.py ./docker/healthcheck.py
COPY docker/sshport-cleanup /usr/local/libexec/sshport-cleanup

RUN chmod 0750 /usr/local/libexec/sshport-cleanup \
    && chown root:root /usr/local/libexec/sshport-cleanup \
    && printf '%s\n' \
        'Defaults:sshport !requiretty, secure_path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
        'sshport ALL=(root) NOPASSWD: /usr/local/libexec/sshport-cleanup' \
        > /etc/sudoers.d/sshport \
    && chmod 0440 /etc/sudoers.d/sshport \
    && visudo -cf /etc/sudoers.d/sshport \
    && mkdir -p /app/config /app/data /app/logs /app/keys \
    && chown -R sshport:sshport /app/data /app/logs /app/keys

USER sshport

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

CMD ["python", "-m", "app"]
