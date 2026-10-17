"""Lapsed-customer reorder reminders
(spec docs/superpowers/specs/2026-10-09-lapsed-customer-reorder-reminders-design.md).

One job per module. Import the module you need: this package re-exports nothing, so importing
one piece never drags the engine's import graph (orders, outlets, notifications) into a bot,
a migration, a Telegram login or the beat schedule.

* ``config``      — the tunables as one validated ``ReminderConfig``, and the send window;
* ``events``      — ``record_event`` / ``record_event_at``, the append-only log writer;
* ``settings``    — the effective per-customer setting and its only writer (D7, D20);
* ``facts``       — batched, cluster- and place-aware facts (D18, D21);
* ``eligibility`` — the one pure rule, ``evaluate``;
* ``engine``      — the daily lapse-episode state machine;
* ``actions``     — Later, Stop, the reply hook, resolve and account merges;
* ``messaging``   — the send-time re-check and the channel adapters.
"""
