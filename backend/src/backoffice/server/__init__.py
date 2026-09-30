"""The production back office: real tenants, sign-in, saved data, security (§44–§55).

``BACKOFFICE_MODE=production`` turns the API from the single frozen demo
tenant into a multi-tenant service:

* :mod:`.config`    every setting, from the environment (documented in .env.example);
* :mod:`.passwords` scrypt password hashing;
* :mod:`.store`     the storage contract, plus an in-memory store for tests;
* :mod:`.postgres`  the PostgreSQL store (psycopg 3, row-level security per request);
* :mod:`.events`    the append-only, hash-chained tenant event log and state digests;
* :mod:`.runtime`   event-sourced tenants: record, apply, replay, cache, locks, read guard;
* :mod:`.reads`     documents read before an event is recorded; replays use the recorded reading;
* :mod:`.links`     invoice links opened before an event is recorded; replays use what came back;
* :mod:`.sync`      the sync worker: mailboxes and banks into events, each tenant's day;
* :mod:`.worker`    ``python -m backoffice.server.worker``;
* :mod:`.auth`      sign-up, sign-in, sessions, CSRF guard, roles, rate limits;
* :mod:`.notify`    Expo push notifications, for the few things that need the owner;
* :mod:`.account`   GDPR export and erasure;
* :mod:`.http`      the FastAPI application that puts it together.

Nothing here is imported by the demo, the tests of the engine or the browser
build: database drivers, boto3 and the Anthropic SDK are imported lazily.
"""

from __future__ import annotations

__all__: list[str] = []
