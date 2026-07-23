# Unified event stream

`server.events` is the stable boundary between perception, voice, VLM, and the state engine. Every producer emits an `EventEnvelope`; only the session service assigns `seq`, and downstream consumers replay by `seq` rather than JSONL append order.

## Contract rules

- `t_device_ms` comes from the source's monotonic media clock.
- `t_server_est` is the backend estimate on the shared session timeline.
- `received_at` is transport/audit metadata and is the only field ignored by the default deterministic comparison.
- retries reuse the same `event_id`; identical retries are ignored and conflicting reuse is rejected.
- late uploads set `backfill=true` but retain their original device time and server-assigned sequence.
- perception conclusions keep `relation_confidence`, `phase_confidence`, and raw `signals` separate.

## CLI

```bash
# Strictly validate a stream and reject duplicate sequence numbers
.venv/bin/python -m server.events.replay validate data/sessions/example.jsonl

# Write canonical sequence order (received_at omitted)
.venv/bin/python -m server.events.replay normalize input.jsonl canonical.jsonl

# Acceptance check: prints "equal" and exits 0 when two runs match
.venv/bin/python -m server.events.replay compare run-a.jsonl run-b.jsonl
```

## Legacy logs

The pre-envelope perception JSONL format (`t`, `event`, `detections`, and similar top-level fields) is schema version 0 and is intentionally rejected by the strict reader. Existing files remain preserved as raw evidence; do not silently rewrite them or invent missing device timestamps. A future migration must use the original video/frame clock and emit a separately named envelope log.
