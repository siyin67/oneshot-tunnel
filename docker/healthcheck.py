#!/usr/bin/env python3
"""Container health check: load the mounted config and probe /healthz."""

import json
import os
import sys
import urllib.error
import urllib.request

from app.config import CONFIG_ENV, DEFAULT_CONFIG_PATH, ConfigError, load_config


def main() -> int:
    path = os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    try:
        config = load_config(path)
    except (ConfigError, OSError) as exc:
        print(f"healthcheck: config error: {exc}", file=sys.stderr)
        return 1

    host = config.server.listen_host
    if host in {"0.0.0.0", "*"}:
        host = "127.0.0.1"
    else:
        if host in {"::", "[::]"}:
            host = "::1"
        # urllib needs brackets around a literal IPv6 address.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
    url = f"http://{host}:{config.server.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"healthcheck: request failed: {exc}", file=sys.stderr)
        return 1

    if payload.get("status") != "ok" or payload.get("host_network") != "ok":
        print(f"healthcheck: unhealthy response: {payload}", file=sys.stderr)
        return 1
    print("healthcheck: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
