# NomaCook Backend, Hardware, and Accuracy Integration Spec

> Status: integration handoff draft
> Updated: 2026-08-14
> Repository: `DWSDavid/NomaCook`
> Delivery branch: `agent/cv-live-camera`
> Product slice: kitchen SOP execution, beginning with tomato-to-fridge

## 1. Decision

Keep perception, task-state authority, evaluation, backend integration contracts,
and hardware adapters in the same NomaCook repository. Do not create a second
algorithm repository. Use separate branches and pull requests so the backend
engineer can integrate against explicit contracts without copying model logic.

NomaCook is not an object-recognition demo. It is a closed-loop SOP system:

```text
Camera / MP4 / network stream
  -> task-scoped object + hand perception
  -> Evidence Events
  -> StateEngine (only task-truth authority)
  -> TaskSnapshot + append-only Event Log
  -> HotMemory
  -> grounded product interaction
  -> capture + Silver labels
  -> human review + Gold labels
  -> replay / cross-evidence evaluation
  -> one measured improvement
  -> repeat the same acceptance scenarios
```

The LLM, VLM, backend, UI, and hardware must never directly mark a step complete.
Only `StateEngine` may advance, recover, or complete task state from accepted
evidence.

## 2. What is implemented on this branch

The branch contains the current end-to-end vertical slice through Stage 5C:

- webcam, MP4, and HTTP/RTSP-style frame sources;
- task-scoped YOLO + MediaPipe + interaction tracking;
- immutable `EventEnvelope` records and deterministic replay;
- tomato-to-fridge task graph and `StateEngine` transitions/recovery;
- read-only `TaskSnapshot`, bounded `HotMemory`, and grounded voice delivery;
- Qwen Realtime voice adapter that cannot mutate task truth;
- training-ready frame observations, optional raw video, and capture validation;
- Silver/weak labels, review queues, review clips, and human-only Gold labels;
- deterministic cross-evidence evaluation and an offline Qwen VLM SHADOW path;
- four Stage 5C pilot sessions, stored locally and intentionally not committed.

Current acceptance status:

- Stage 5A capture: complete;
- Stage 5B review workflow: code complete and initial human Gold labels present;
- Stage 5C: implementation checkpoint only, not accepted;
- four new pilot reviews are pending;
- Qwen VLM SHADOW has zero successful answers because the configured Beijing
  workspace returned `AccessDenied.Unpurchased`;
- external datasets, segmentation models, nutrition analysis, and training are
  not active work.

## 3. Stable integration contracts

### 3.1 Frame input

Use `server/live/frame_source.py` as the source boundary. `open_source(spec)`
already routes:

- `"0"`, `"1"`, etc. to a local webcam;
- HTTP/HTTPS/RTSP URLs to a network camera stream;
- other strings to local video files.

Every source yields:

```python
(pts_ms: float, frame_bgr: numpy.ndarray)
```

The first hardware integration should expose an MJPEG URL and reuse this
boundary. Do not place device-specific decoding or AI logic inside
`StateEngine`.

### 3.2 Evidence event

`server/events/schema.py::EventEnvelope` is the immutable fact contract. The
backend may transport and persist it, but may not rewrite its task meaning.
Important fields are:

```text
event_id
session_id
seq
type
t_device_ms
t_server_est
received_at
source
confidence
payload
context_version
runtime_mode: RUN | SHADOW | REPLAY_EVAL
```

Backend rules:

- deduplicate by `event_id`;
- preserve per-session `seq` order;
- reject or quarantine stale `context_version` writes;
- never mix `SHADOW` or `REPLAY_EVAL` events into the live product stream;
- never translate an LLM sentence into `STEP_COMPLETE`.

### 3.3 Product state

`server/engine/snapshot.py::TaskSnapshot` is the read-only product contract:

```text
session_id
task_id
task_goal
state
step_title
step_instruction
status: ON_TRACK | UNCERTAIN | DEVIATING | CRITICAL | COMPLETE
belief
active_objects
missing_evidence
pending_question
last_event_seq
context_version
```

The UI may display `COMPLETE` only when the snapshot says `COMPLETE`. A voice
model response, VLM answer, button click, or backend timer is not completion.

### 3.4 Hot context

`server/engine/hot_memory.py::HotMemory` stores only the latest bounded context
needed for grounded interaction. It is not the durable system of record. The
backend owns session lifecycle and durable storage; the ordered event stream is
the replay source of truth.

## 4. Backend service boundary

The current repository does not yet expose a production network service. The
backend engineer should integrate against the contracts above, not import
detector internals or duplicate state rules.

Minimum planned service surface:

```text
POST   /v1/sessions
GET    /v1/sessions/{session_id}/snapshot
WS     /v1/sessions/{session_id}/events
DELETE /v1/sessions/{session_id}
GET    /health/live
GET    /health/ready
```

Session creation input should remain small:

```json
{
  "task_id": "tomato_to_fridge_v1",
  "source": "0 or an MJPEG/RTSP URL",
  "runtime_mode": "RUN"
}
```

The backend owns:

- authentication and user/session ownership;
- recipe/SOP selection;
- process lifecycle and health;
- durable event/snapshot storage;
- UI subscriptions and reconnects;
- device registration and source URLs.

The Python intelligence process owns:

- frame consumption;
- perception and tracking;
- evidence creation;
- StateEngine state;
- snapshot construction;
- capture/review/evaluation artifacts.

Do not add Redis, Kafka, LiveKit, or a microservice split until one local
backend-to-Python session works end to end. A single Python worker plus the
existing backend is the first deployment target.

## 5. Hardware adaptation

### First supported path

```text
ESP32 / fixed kitchen camera
  -> MJPEG URL on the same network
  -> open_source(url)
  -> current perception and StateEngine loop
```

Acceptance conditions:

- stable 640x480 or better stream at 5-10 useful FPS;
- monotonic timestamps and reconnect without creating a new task session;
- fixed camera view includes the relevant work surface and destination ROI;
- lighting does not make the target object disappear for long intervals;
- no cloud API key is stored on the device;
- disconnect never causes a false completion.

Only move to WebRTC, custom binary WebSocket media packets, or more powerful
hardware after MJPEG fails a measured latency, reliability, or audio need.

## 6. Kitchen SOP domain model

Each SOP/domain pack should define only observable task facts:

```text
Task
Step
Required Object
Object State
Region / Destination
Evidence Policy
Recovery Transition
Risk
Instruction
```

Object identity and object state must be separate. Example:

```text
identity: tomato
state: whole | cut | peeled | cooked | unknown
location: table | held | transit | fridge | unknown
hand_relation: none | near | holding | released | unknown
```

This avoids creating a new detector class for every combination such as
`whole_red_tomato_on_table`. The task graph consumes composable evidence.

## 7. Accuracy roadmap and dataset gates

### Now: finish the local acceptance loop

1. Obtain one human decision for each of the four Stage 5C pilot clips.
2. Rebuild Gold-only deterministic metrics.
3. Assign each wrong/uncertain result one primary failure category:
   `OBJECT_DETECTION`, `HAND_OBJECT_CONTINUITY`, `STATE_CHANGE_TIMING`, or
   `PROCEDURE_ORDER`.
4. Fix only the most frequent measured failure.
5. Re-run the same four pilots before recording more data.

Do not start model training from four pilots.

### Local NomaCook data: first training source

Collect a small local dataset only when the same `OBJECT_DETECTION` failure is
confirmed in at least three Gold clips across at least two sessions. Use the
actual deployment camera, kitchen, lighting, distance, and object states. Start
with 20-50 targeted clips, not thousands of generic food images.

Minimum labels:

- object identity;
- object state;
- visible/occluded;
- hand contact/holding;
- region;
- transition boundary;
- human Gold outcome.

### VISOR: conditional hand-object segmentation

Use the VISOR subset of EPIC-KITCHENS only if `HAND_OBJECT_CONTINUITY` remains
the leading failure after local tracking improvements, with at least three Gold
failures across two sessions. Start with annotations and a small benchmark
subset. Do not download or train on the full EPIC-KITCHENS corpus first.

### Ego4D FHO: conditional state-change timing

Use Ego4D Forecasting Hands and Objects only if object and destination detection
are correct but release/state-change timing fails in at least three Gold clips.
Borrow the `pre / point-of-no-return / post` evaluation structure before
training any FHO model.

### EPIC-KITCHENS Core: later action vocabulary

EPIC-KITCHENS Core is not needed for the tomato-to-fridge slice. Consider its
verb-noun action annotations only when multiple kitchen SOPs require general
action recognition that cannot be expressed reliably through current object,
hand, region, and state-change evidence.

### Ego-Exo4D: later procedural understanding

Ego-Exo4D becomes relevant only when NomaCook supports at least one recipe with
five or more observable keysteps and has at least ten human-reviewed sessions.
Use cooking keysteps and procedural dependencies for missing-step and ordering
evaluation. Do not introduce proficiency estimation until coaching execution
quality is an explicit product requirement.

### Not active

- FastSAM or another segmentation dependency;
- FoodSeg103 / UECFoodPix;
- broad EPIC-KITCHENS training;
- nutrition or intake estimation;
- VLM output as Ground Truth;
- real-time VLM control of StateEngine.

## 8. VLM and voice boundary

Qwen Realtime remains a voice/language adapter. It receives grounded
`TaskSnapshot` context and cannot see all live frames or advance state.

Offline VLM SHADOW is an evaluation experiment. Before another paid run:

- stop the whole batch after the first access/authorization error;
- compare VLM answers with Gold only when both represent the same proposition;
- do not ask a yes/no completion question from an evidence package that does
  not show the necessary transition;
- run one contact sheet first, then decide whether to evaluate more.

VLM is not required for the backend/hardware integration milestone.

## 9. Integration sequence

### Milestone A: contract smoke

- backend starts one `tomato_to_fridge_v1` session;
- Python reads a local MP4 or webcam;
- backend receives ordered events and the latest snapshot;
- UI displays current step and uncertainty;
- only StateEngine can produce `COMPLETE`.

### Milestone B: fixed kitchen camera

- replace the source with one MJPEG/RTSP camera;
- verify reconnect, timestamps, ROI, latency, and no false completion;
- replay the captured session through the same evaluator.

### Milestone C: human review loop

- backend exposes session artifacts to an operator;
- operator records correct/incorrect/uncertain;
- only correct/incorrect with a complete event type become Gold;
- metrics remain null below the minimum sample size.

### Milestone D: one measured accuracy improvement

- select the largest Gold-confirmed failure category;
- use the smallest matching local or external data source;
- re-run unchanged acceptance videos and live scenarios;
- ship only if the regression is measurable.

## 10. Explicit handoff warnings

- `data/evals/`, raw videos, review clips, model weights, and API credentials are
  local artifacts and must not be committed.
- The uncommitted local whole-tomato/egg CLIP verifier is an experiment, not a
  backend contract. It currently depends on local weights and lacks real-video
  acceptance. Integrate it only through a separate reviewed PR if it later
  proves necessary.
- Existing architecture documents contain future proposals such as Redis and a
  generalized SessionCore. Treat this document and implemented schemas as the
  current handoff boundary.
- A manual UI action may produce user evidence, but it must not directly set
  task completion.

## 11. Definition of the product loop being complete

The kitchen SOP loop is complete only when one unchanged task can pass all of
the following through the backend and target camera:

1. correct live task progression;
2. no completion on negative or return scenarios;
3. grounded UI/voice output from the latest snapshot;
4. capture artifacts that pass validation;
5. human-reviewable clips;
6. Gold-only evaluation with explicit uncertainty;
7. deterministic replay of the same outcome;
8. no secrets, no task-state writes from LLM/VLM, and no unrelated regressions.
