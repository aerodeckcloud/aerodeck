"""Redis-backed sliding-window rate limiter for public webhook receivers (#249)."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from redis import asyncio as aioredis

from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger("services.webhook_rate_limit")

_MAX_REQUESTS = 60
_WINDOW_SECONDS = 60
_fallback_windows: dict[str, deque[float]] = defaultdict(deque)


class WebhookRateLimitError(AppError):
    status_code = 429
    code = "webhook_rate_limited"


async def enforce_webhook_rate_limit(client_ip: str, redis_url: str | None) -> None:
    """Enforce per-IP rate limiting via Redis INCR/EXPIRE (or local fallback in tests)."""
    if redis_url:
        key = f"aerodeck:ratelimit:webhook:{client_ip}"
        client = aioredis.from_url(redis_url, socket_connect_timeout=1.0, socket_timeout=1.0)
        try:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, _WINDOW_SECONDS)
            if count > _MAX_REQUESTS:
                raise WebhookRateLimitError("Too many webhook requests; please retry shortly.")
            return
        except WebhookRateLimitError:
            raise
        except Exception as exc:
            logger.warning("redis webhook rate-limit fallback: %s", type(exc).__name__)
        finally:
            await client.aclose()

    now = time.monotonic()
    window = _fallback_windows[client_ip]
    cutoff = now - _WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= _MAX_REQUESTS:
        raise WebhookRateLimitError("Too many webhook requests; please retry shortly.")
    window.append(now)
