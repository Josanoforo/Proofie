"""Shared 45/minute budget (failure-mode Design Change 10), with rolling-window protection."""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from urllib.parse import urlsplit

LIMIT_PER_MINUTE = 45
WINDOW_SECONDS = 60


class BudgetUnavailable(RuntimeError):
    pass


class Budget:
    def __init__(self, limit=LIMIT_PER_MINUTE, clock=time.monotonic, jitter=random.uniform):
        if limit < 1 or limit > LIMIT_PER_MINUTE:
            raise ValueError("owned budget must be between 1 and 45 per minute")
        self.limit, self.clock, self.jitter = limit, clock, jitter
        self.tokens, self.last, self.blocked_until = float(limit), clock(), 0.0
        self.stamps, self.lock, self.bursts = deque(), threading.Lock(), 0

    def take(self):
        with self.lock:
            now = self.clock()
            if now < self.last:
                raise BudgetUnavailable("monotonic clock moved backwards")
            self.tokens = min(self.limit, self.tokens + (now - self.last) * self.limit / WINDOW_SECONDS)
            self.last = now
            while self.stamps and now - self.stamps[0] >= WINDOW_SECONDS:
                self.stamps.popleft()
            if now < self.blocked_until or self.tokens < 1 or len(self.stamps) >= self.limit:
                raise BudgetUnavailable("GET deferred by shared rate budget")
            self.tokens -= 1
            self.stamps.append(now)

    def on_429(self):
        with self.lock:
            self.bursts += 1
            delay = self.jitter(0, min(WINDOW_SECONDS, 2 ** min(self.bursts, 6)))
            self.blocked_until = max(self.blocked_until, self.clock() + delay)
            return delay


SHARED_BUDGET = Budget()


class ReadClient:
    """Every owned caller uses this wrapper; no retries or write forwarding."""
    def __init__(self, client, budget=None):
        self.client, self.budget = client, budget if budget is not None else SHARED_BUDGET

    def call(self, method, path, op, **kwargs):
        if method != "GET":
            raise ValueError("event/deadline readers only permit GET")
        # Deadline pagination reuses API-provided links; reject a foreign absolute link before budget or transport.
        parsed = urlsplit(path)
        if any(c in path for c in "\r\n") or parsed.fragment or (parsed.netloc and
                (parsed.scheme != "https" or parsed.hostname != "api-m.sandbox.paypal.com" or
                 parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443))):
            raise ValueError("owned GET link must stay in the sandbox")
        if parsed.scheme and not parsed.netloc:
            raise ValueError("owned GET link must be an HTTP path or sandbox URL")
        self.budget.take()
        try:
            return self.client.call(method, path, op, **kwargs)
        except Exception as exc:
            if getattr(exc, "status", None) == 429:
                self.budget.on_429()
            raise
