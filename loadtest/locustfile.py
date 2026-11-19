"""Load test for EnterpriseRAG (Locust, MIT). See loadtest/README.md.

Each simulated user logs in as a distinct imported user (credentials from LOAD_USERS_CSV), sticks to one app
replica (LOAD_HOSTS, comma-separated; sessions are shared through Postgres, so any replica would do), and
mostly asks questions in the chat, reading the whole answer stream. It also lists containers and its usage.

Reported per request type: response times and failures. "chat" is the time to the end of the stream; "chat
first token" is the time to the first answer token; queue waits are counted from the stream's queue events.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
import random
import threading
import time

from locust import HttpUser, between, events, task

QUESTIONS = [
    "How many vacation days do I get per year?",
    "When are we allowed to deploy to production?",
    "What is new in version 4.2 of the mobile app?",
    "How do I report a lost laptop?",
    "What are the core collaboration hours?",
    "¿Cuántos días de vacaciones tengo al año?",
    "What does the expenses policy say about travel?",
    "What is the Q3 2026 operating budget?",
]

_lock = threading.Lock()
_creds = None
_hosts = itertools.cycle([h.strip() for h in os.environ.get("LOAD_HOSTS", "").split(",") if h.strip()] or [""])
QUEUE_EVENTS = {"waited": 0}


def next_user() -> tuple[str, str]:
    global _creds
    with _lock:
        if _creds is None:
            with open(os.environ["LOAD_USERS_CSV"], newline="") as fh:
                rows = [(r["email"], r["password"]) for r in csv.DictReader(fh)]
            random.Random(1).shuffle(rows)
            _creds = itertools.cycle(rows)
        return next(_creds)


class Employee(HttpUser):
    host = (os.environ.get("LOAD_HOSTS", "") or "http://127.0.0.1:8000").split(",")[0].strip()
    wait_time = between(2, 6)

    def on_start(self) -> None:
        with _lock:
            host = next(_hosts)
        if host:
            self.client.base_url = host
        email, password = next_user()
        r = self.client.post("/api/auth/login", json={"email": email, "password": password}, name="login")
        if r.status_code != 200:
            raise RuntimeError(f"login failed: {r.status_code}")
        self.client.headers["X-CSRF-Token"] = r.json()["csrf_token"]

    @task(6)
    def chat(self) -> None:
        start = time.perf_counter()
        first: float | None = None
        ok, error = True, ""
        with self.client.post(
            "/api/chat",
            json={"message": random.choice(QUESTIONS)},
            stream=True,
            name="chat",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"HTTP {r.status_code}")
                return
            for line in r.iter_lines():
                if not line or not line.startswith(b"data:"):
                    continue
                ev = json.loads(line[5:])
                if ev.get("type") == "delta" and first is None:
                    first = time.perf_counter()
                elif ev.get("type") == "queue":
                    QUEUE_EVENTS["waited"] += 1
                elif ev.get("type") == "error":
                    ok, error = False, f"{ev.get('code', 'error')}: {ev.get('message', '')[:80]}"
            if ok:
                r.success()
            else:
                r.failure(error)
        if first is not None:
            events.request.fire(
                request_type="SSE",
                name="chat first token",
                response_time=(first - start) * 1000,
                response_length=0,
                exception=None,
                context={},
            )

    @task(2)
    def containers(self) -> None:
        self.client.get("/api/containers", name="containers")

    @task(1)
    def my_usage(self) -> None:
        self.client.get("/api/usage/me", name="usage")


@events.quitting.add_listener
def _report(environment, **_kw) -> None:  # type: ignore[no-untyped-def]
    print(f"chat requests that waited in the generation queue: {QUEUE_EVENTS['waited']}")
