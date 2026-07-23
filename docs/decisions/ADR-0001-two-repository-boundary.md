# ADR-0001: Use separate core and frontend repositories

Status: Accepted for project bootstrap.

## Context

Gemini can access and commit through GitHub from AI Studio but does not share the full local development environment. Codex owns local backend, Runtime, integration, and browser verification. Two empty repositories already exist.

## Decision

- `Purewo/mutiAI` stores the product core, backend, orchestration, Runtime adapters, and authoritative contracts.
- `Purewo/mutiAI-aistdio-gemini` stores the web frontend and frontend-specific documentation.
- The core repository is the only source of truth for product and transport contracts.
- The frontend consumes generated clients or contract snapshots that identify their source core commit.
- Gemini works through bounded branches or pull requests. Codex performs final local integration and verification.

## Consequences

The split reduces frontend context pressure and matches Gemini's available access. It also introduces contract synchronization risk. Generated clients, source-commit metadata, CI checks, and explicit integration gates must control that risk.
