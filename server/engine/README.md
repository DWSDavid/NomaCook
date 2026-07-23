# State engine

The state engine is the only component allowed to complete a recipe step. Models and tools contribute evidence events; none of them can call an `advance_step` operation.

## Deterministic rules

- one engine instance owns one `session_id` and consumes strictly increasing `seq` values;
- repeated `event_id` values are ignored, while unseen out-of-order events are rejected;
- evidence received more than 3 seconds after its estimated session time is retained as stale audit context but contributes no score;
- each evidence rule contributes its weight once, while later matching observations can satisfy the consecutive-hit requirement;
- a step advances only after its score reaches the configured threshold for the configured number of consecutive matching observations;
- an intermediate score that remains unresolved for the configured timeout creates `pending_question`;
- voice confirmation must reference its transcript event; a high-risk step must also reference the system question it answers.

The initial [fried-rice SOP](../../sop/fried_rice.json) uses a `0.7` threshold and two consecutive hits. Its `completion_check` fields describe visible end states rather than actions.

## Minimal use

```python
from datetime import UTC, datetime

from server.engine import StateEngine, load_recipe

recipe = load_recipe("sop/fried_rice.json")
engine = StateEngine(
    session_id="ses_example",
    recipe=recipe,
    started_at=datetime.now(UTC),
)

result = engine.consume(event_envelope)
snapshot = result.context
```

The context snapshot is versioned after every newly consumed event. Live and VLM integrations should read this snapshot; they must not maintain a separate current-step truth.
