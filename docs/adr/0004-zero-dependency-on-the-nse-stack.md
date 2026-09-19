# ADR-0004 — Zero dependency on the Kotsin NSE stack

**Status:** accepted · 2026-09-20

## Decision
This repository shares no code, package, database, topic or process with the NSE stack. Lessons are
carried as rules (`docs/LEARNINGS.md`) and as fresh implementations with their own tests. Time is UTC
everywhere in the backend; IST is a display option in the frontend. No 5paisa, no scrip codes, no lot
sizes, no session boundaries.

## Consequences
- The two systems can be deployed, broken and retired independently.
- Anything that looks like a copy of an NSE class is a review finding.
