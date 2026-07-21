# ADR 0001: v2 Hexagonal Core

## Status

Accepted

## Context

The legacy watchdog coupled API routes, scan execution, Valkey state, destructive
finding grouping, and reasoning in one runtime path. v2 requires durable scans,
PostgreSQL persistence, idempotent workers, and auditable automation.

## Decision

The runtime is split into these packages:

- `jenkins_watchdog.domain`: dependency-free domain model and policies.
- `jenkins_watchdog.application`: use cases and port protocols.
- `jenkins_watchdog.infrastructure`: adapters for persistence and external systems.
- `jenkins_watchdog.entrypoints`: HTTP, CLI, and worker entrypoints.

Only composition/bootstrap code may wire concrete adapters to application services.
Domain objects use immutable/value-oriented dataclasses where practical. Finding
identity is computed as a full SHA-256 over compact canonical JSON containing only
`[rule_id, resource_id, identity_dimensions]`.

## Consequences

The legacy routes and Valkey business-state keys were removed after the v2 browser,
API, migration, and worker gates passed. The supported contract is `/api/v2`.
