# ADR 0002: PostgreSQL Durable Pipeline

## Status

Accepted

## Context

Scans and external deliveries must survive API restarts, worker termination, and duplicate scheduling. Incident resolution must account for filtered, failed, timed-out, and cancelled checks.

## Decision

PostgreSQL owns all business tables and every scan event. Scan, investigation, and action workers claim rows with `FOR UPDATE SKIP LOCKED`, expiring leases, and heartbeats. Scans permit three attempts with one- and five-minute recovery delays. Investigations permit three attempts with bounded exponential recovery. Delivery permits an initial call and five retries at one minute, five minutes, fifteen minutes, one hour, and four hours.

The scan stage marker is persisted after each idempotent stage. Terminal checks and completed external attempts are not rerun during lease recovery. Findings are stored before transactional correlation. An incident resolves only when all checks responsible for its active occurrence were selected and succeeded.

Valkey streams are best-effort notifications after the PostgreSQL event commit. SSE replay and five-second catch-up always read PostgreSQL.

## Consequences

API processes only enqueue and query, including manual build analysis and reinvestigation. Worker capacity can scale independently. PostgreSQL availability is required for all business operations, while Valkey outages only reduce live-update latency.
