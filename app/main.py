"""shop-app: a deliberately small, deliberately breakable service.

One image, three roles (ROLE env):
  webapp    - public API. /api/orders does a bit of CPU work and calls inventory.
  inventory - dependency. /stock answers after FAULT_LATENCY_MS.
  loadgen   - calls webapp in a loop so failures show up in logs and metrics.

Every incident in incidents/ is a configuration change to this service; the
code itself never changes. Logs are plain lines on stdout so `kubectl logs`
shows exactly what an on-call engineer would see.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

ROLE = os.environ.get("ROLE", "webapp")
VERSION = os.environ.get("APP_VERSION", "1.0.0")

logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s " + ROLE + " %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(ROLE)


class ConfigError(Exception):
    pass


def load_config() -> dict:
    """Validate configuration at startup. A bad value is fatal (exit 1), which is
    exactly how a typo in a ConfigMap turns into CrashLoopBackOff."""
    cfg: dict = {}
    raw_timeout = os.environ.get("UPSTREAM_TIMEOUT_MS", "800")
    if not raw_timeout.isdigit():
        raise ConfigError(
            f"UPSTREAM_TIMEOUT_MS must be an integer number of milliseconds, got {raw_timeout!r}"
        )
    cfg["upstream_timeout_ms"] = int(raw_timeout)
    level = os.environ.get("LOG_LEVEL", "info")
    if level not in ("debug", "info", "warning", "error"):
        raise ConfigError(f"LOG_LEVEL must be one of debug,info,warning,error, got {level!r}")
    cfg["log_level"] = level
    cfg["cache_warm_mb"] = int(os.environ.get("CACHE_WARM_MB", "16"))
    cfg["fault_latency_ms"] = int(os.environ.get("FAULT_LATENCY_MS", "0"))
    cfg["upstream_url"] = os.environ.get("UPSTREAM_URL", "http://inventory:8080/stock")
    cfg["api_key_present"] = bool(os.environ.get("API_KEY"))
    return cfg


# ---------------------------------------------------------------- metrics
class Metrics:
    """Rolling 60 s window; rendered in Prometheus text format."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.window: deque[tuple[float, float, bool]] = deque()  # (ts, seconds, ok)
        self.requests_total = 0
        self.errors_total = 0
        self.upstream_errors_total = 0

    def observe(self, seconds: float, ok: bool) -> None:
        now = time.time()
        with self.lock:
            self.requests_total += 1
            self.errors_total += 0 if ok else 1
            self.window.append((now, seconds, ok))
            while self.window and self.window[0][0] < now - 60:
                self.window.popleft()

    def render(self) -> str:
        with self.lock:
            durs = sorted(d for _, d, _ in self.window)
            errs = sum(1 for _, _, ok in self.window if not ok)
            n = len(durs)

        def q(p: float) -> float:
            return durs[min(n - 1, int(p * n))] if n else 0.0

        lines = [
            f'shop_requests_total{{role="{ROLE}"}} {self.requests_total}',
            f'shop_errors_total{{role="{ROLE}"}} {self.errors_total}',
            f'shop_upstream_errors_total{{role="{ROLE}"}} {self.upstream_errors_total}',
            f'shop_request_seconds{{role="{ROLE}",quantile="0.5",window="60s"}} {q(0.5):.4f}',
            f'shop_request_seconds{{role="{ROLE}",quantile="0.95",window="60s"}} {q(0.95):.4f}',
            f'shop_error_ratio{{role="{ROLE}",window="60s"}} {(errs / n if n else 0):.3f}',
        ]
        # cgroup v2 CPU throttling counters: the ground truth for "CPU limit too low".
        try:
            with open("/sys/fs/cgroup/cpu.stat") as f:
                for line in f:
                    k, v = line.split()
                    if k in ("nr_periods", "nr_throttled", "throttled_usec"):
                        lines.append(f"container_cpu_{k} {v}")
        except OSError:
            pass
        return "\n".join(lines) + "\n"


METRICS = Metrics()


def burn_cpu(ms_of_work: int = 15) -> None:
    """Roughly `ms_of_work` of hashing on an unthrottled core."""
    h = b"shop"
    for _ in range(ms_of_work * 400):
        h = hashlib.sha256(h).digest()


# ---------------------------------------------------------------- web roles
def build_app(cfg: dict):
    from fastapi import FastAPI, HTTPException, Response

    app = FastAPI(title=f"shop-{ROLE}")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/ready")
    def ready():
        return {"ready": True, "role": ROLE, "version": VERSION}

    @app.get("/metrics")
    def metrics():
        return Response(METRICS.render(), media_type="text/plain")

    if ROLE == "inventory":

        @app.get("/stock")
        def stock():
            t0 = time.time()
            if cfg["fault_latency_ms"]:
                time.sleep(cfg["fault_latency_ms"] / 1000)
            METRICS.observe(time.time() - t0, True)
            return {"sku": "gpu-h100-hour", "available": 42}

    else:

        @app.get("/api/orders")
        def orders():
            t0 = time.time()
            burn_cpu()
            timeout = cfg["upstream_timeout_ms"] / 1000
            try:
                with urllib.request.urlopen(cfg["upstream_url"], timeout=timeout) as r:
                    r.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                METRICS.upstream_errors_total += 1
                METRICS.observe(time.time() - t0, False)
                reason = getattr(e, "reason", e)
                if "timed out" in str(reason):
                    log.error(
                        "upstream inventory timed out after %dms url=%s",
                        cfg["upstream_timeout_ms"], cfg["upstream_url"],
                    )
                    raise HTTPException(504, "upstream timeout") from None
                log.error("upstream inventory request failed: %s url=%s", reason, cfg["upstream_url"])
                raise HTTPException(502, "upstream error") from None
            elapsed = time.time() - t0
            METRICS.observe(elapsed, True)
            if elapsed > 0.5:
                log.warning("slow request /api/orders took %.0fms", elapsed * 1000)
            return {"order": "ok", "latency_ms": round(elapsed * 1000)}

    return app


# ---------------------------------------------------------------- loadgen
def run_loadgen() -> None:
    target = os.environ.get("TARGET_URL", "http://webapp:8080/api/orders")
    interval = float(os.environ.get("INTERVAL_S", "1.0"))
    log.info("loadgen started target=%s interval=%.1fs", target, interval)
    fails = 0
    while True:
        t0 = time.time()
        try:
            with urllib.request.urlopen(target, timeout=5) as r:
                r.read()
            if fails:
                log.info("target recovered after %d failed requests", fails)
            fails = 0
        except urllib.error.HTTPError as e:
            fails += 1
            log.warning("GET %s -> HTTP %d after %.0fms", target, e.code, (time.time() - t0) * 1000)
        except Exception as e:  # noqa: BLE001 - loadgen reports everything
            fails += 1
            log.warning("GET %s failed: %s", target, getattr(e, "reason", e))
        time.sleep(interval)


def main() -> None:
    if ROLE == "loadgen":
        run_loadgen()
        return
    try:
        cfg = load_config()
    except ConfigError as e:
        log.critical("ConfigError: %s", e)
        log.critical("refusing to start with invalid configuration")
        sys.exit(1)
    logging.getLogger().setLevel(cfg["log_level"].upper())
    log.info("starting shop-%s version=%s", ROLE, VERSION)
    if ROLE == "webapp" and not cfg["api_key_present"]:
        log.warning("API_KEY not set; partner calls will be rejected")
    if cfg["cache_warm_mb"]:
        log.info("warming in-memory catalog cache: %d MB", cfg["cache_warm_mb"])
        global _CACHE
        _CACHE = bytearray(b"\x01") * (cfg["cache_warm_mb"] * 1024 * 1024)
        log.info("cache warm complete")
    import uvicorn

    uvicorn.run(build_app(cfg), host="0.0.0.0", port=8080, log_level="warning", access_log=False)


_CACHE: bytearray | None = None

if __name__ == "__main__":
    main()
