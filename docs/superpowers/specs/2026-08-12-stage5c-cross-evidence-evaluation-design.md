# Stage 5C Cross-Evidence Evaluation

## Goal

Measure which existing evidence streams are present around human-reviewed outcomes without letting evaluation data change live task state. Build the evaluator first on the current Stage 5B sessions, then use four new uniquely identified pilot captures to expand the sample.

## Scope

Stage 5C reads existing capture artifacts only:

- `review/review_queue.jsonl`
- `review/gold_labels.jsonl`
- `events.jsonl`
- `observations.jsonl`
- `summary.json`

It does not call a VLM, train a model, add a database, add a service, or modify `StateEngine`.

## Identity and eligibility

- Identify an evaluation item by `(resolved session directory, review_item_id)`. Historical sessions share one legacy `session_id`, so `review_item_id` alone is unsafe across sessions.
- Include only `is_ground_truth=true` labels in correctness metrics.
- Track `uncertain` labels separately as ambiguity cases. Never count them as correct, incorrect, or Ground Truth.
- Reject duplicate identities, missing queue items, invalid clip windows, and malformed Gold Labels before producing a report.

## Evidence alignment

For each reviewed item, use its inclusive `start_pts_ms` to `end_pts_ms` window to collect:

- event-stream event types, sources, and confidence values;
- object detections from frame observations;
- valid hand observations and gripping-hand observations;
- StateEngine step IDs seen in frame machine labels;
- the session final step and completion status.

The evaluator reports presence and coverage of these streams. It does not invent a new task-state decision or replay evidence into the live engine.

## Report

Write one deterministic `noma.cross_evidence_eval.v1` JSON report containing:

- evaluated session paths;
- Gold and uncertain item counts;
- one aligned record per reviewed item;
- evidence-stream coverage per item;
- groups by evidence-stream combination with raw correct and incorrect counts;
- uncertain cases as `ambiguity_candidates`;
- explicit limitations, including the legacy session collision and sample size.

For any aggregate group with fewer than five Gold items, set `accuracy` to `null` and `sample_status` to `insufficient_sample`. Preserve raw counts so the report remains useful without presenting four correct labels as a reliable 100% result.

The current hand-near conflict remains an ambiguity candidate with a note that continuous tomato holding may require a future bounded VLM question. Stage 5C must not call that VLM.

## Implementation shape

- Add one data module for alignment, validation, aggregation, and report construction.
- Add one small command-line harness that accepts repeated session directories and an output path.
- Add one focused test file using temporary synthetic sessions. Keep the two current Stage 5B sessions as a manual real-data smoke because `data/evals/` is not a versioned test fixture.
- Reuse the existing JSONL reader and review-label validator. Add no dependency.

## Pilot expansion

After the evaluator works on the current sessions, record four new captures with automatically unique session IDs:

1. clean table-to-fridge completion;
2. table-to-fridge completion with temporary hand or tomato occlusion;
3. pickup followed by returning the tomato to the table;
4. approach or interact with the fridge without releasing the tomato inside.

Run capture validation, build review queues, and obtain at least one human Gold Label from each pilot. Re-run the evaluator across the current two sessions and the four new pilots.

## Acceptance

- Synthetic tests prove inclusive window alignment, composite identity, Gold-only accuracy, uncertain exclusion, and deterministic output.
- Current real-data smoke finds exactly four Gold Labels and one uncertain label.
- Current aggregate accuracy is `null` with `sample_status=insufficient_sample`.
- The report includes the hand-near conflict as an ambiguity candidate without invoking VLM.
- Four new pilot sessions have unique session IDs, pass capture and label validation, and contribute at least one Gold Label each.
- The six-session report is generated without changing live task state.
- Focused tests and the full non-e2e suite pass.

## Offline VLM SHADOW evaluation (approved 2026-08-13)

The user approved a bounded offline VLM shadow evaluation. It is strictly an
evaluation-side cross-check and never touches live task state.

Boundaries:

- Reuse the existing `GeminiVLMClient` and its existing `GEMINI_API_KEY`
  configuration. No new provider.
- Never feed VLM output into `StateEngine.consume()`.
- Never call `ValidatedVLMResult.to_event()`, which emits a `runtime_mode=RUN`
  envelope into the live event log.
- VLM output lives in a separate `vlm_shadow_eval.json` file, never in
  `events.jsonl`.
- When no API credential is present, emit `status=skipped` and the deterministic
  evaluator still succeeds.

Trigger conditions (all four are gated, not a blanket scan):

1. human label is `uncertain`
2. review reason contains `evidence conflict`
3. transient object or hand occlusion inside the key time window
4. task completion or session-ending boundary

For each triggered window, extract three representative frames (before, middle,
after) from the raw video and compose one OpenCV contact sheet. The question is
specific and must not leak the Gold Label, e.g.:

- "Across these frames, is the person continuously holding the tomato?"
- "Was the tomato released inside the refrigerator?"
- "Is the tomato visibly present in the scene?"

Each VLM record captures trigger reason, question, sampled timestamps, answer,
confidence, Gold comparison eligibility, and agreement with Gold (null when no
Gold exists).

## Explicit exclusions

- model training or threshold tuning
- Web UI
- Redis, database, or network service
- changes to StateEngine authority
- VLM advancing task state or announcing completion
- claims of production accuracy from this pilot-sized dataset
