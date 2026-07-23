# Context-based detection

YOLO-World should not receive the full kitchen vocabulary on every frame. `DetectionContext` derives a small prompt set from the frozen SOP step and the current `SessionContext.active_objects`.

For example, the seasoning step expands `soy sauce bottle`, `salt`, and `pepper` into visually concrete prompts such as `dark condiment bottle`, `salt shaker`, and `pepper container`. Bottle-like confusers are included explicitly so a detected oil or vinegar bottle is not silently canonicalized as soy sauce.

The layer also:

- maps prompt aliases back to stable snake-case object labels;
- applies initial per-concept confidence thresholds;
- merges overlapping detections from aliases such as `wok` and `frying pan`;
- marks targets as `primary`, `anchor`, or `confuser`;
- updates the underlying detector vocabulary only when the effective prompt tuple changes.

Thresholds are starting values, not accuracy claims. They must be calibrated against fixed first-person fixture frames later. Object detection answers “where is a candidate object”; liquid identity, cooking state, doneness, and safety remain multi-signal/VLM decisions.

## Live check

The default demo opens this Mac's verified camera source 1 using the fried-rice seasoning context:

```bash
.venv/bin/python -m server.perception.live_context_demo
```

Hold up soy-sauce, oil, or vinegar bottles one at a time. Green boxes are current-step primary targets; orange boxes are explicit confusers. Other steps can be selected without changing code:

```bash
.venv/bin/python -m server.perception.live_context_demo --list-steps
.venv/bin/python -m server.perception.live_context_demo --step step_01_prepare
```
