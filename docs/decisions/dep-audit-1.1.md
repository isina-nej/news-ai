# ADR: dependency audit Phase 1.1

Date: 2026-09-07. Status: accepted.

## cryptography / structlog

Research: attempted PyPI version check (network timed out in CI sandbox).
Local resolution via `uv pip list` / `uv run`:

- `cryptography==44.0.3` installed, satisfies `cryptography==44.*` — Rust-backed,
  wheels for Python 3.12, Django 5.2 happy, not yet used at runtime in Phase 1
  (reserved for future encrypted-at-rest sessions). No breakage seen. Pinned to
  `44.*` to avoid silent Rust-toolchain major bump.
- `structlog==25.5.0` installed, satisfies `structlog==25.*` — latest in 25.x
  line in this env, Django 5.2 / Celery 5.6 compatible, logging wiring still
  thin in Phase 1. Major 26 not pulled blindly.

Decision: keep `cryptography==44.*` and `structlog==25.*` pins unchanged in
Phase 1.1. Re-audit with network before Phase 2 when OTP-session work lands
(then decide on `44.0.3 -> 46.x` etc. explicitly with migration/compat notes).

Other deps (`Django 5.2.17`, `celery 5.6.3`, `redis 8.1`, `django-celery-beat
2.8.1`, `mysqlclient 2.2.8`) already current for the chosen stack; no churn.

## CheckConstraint API

Django 6 deprecates `check=` -> `condition=` on `CheckConstraint`. Migrated
all 8 constraints to `condition=` so `RemovedInDjango60Warning` is gone
(was 11 warnings, now 2 — only unrelated remaining warnings).
