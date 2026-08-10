# Tomato Eval Bilingual Overlay and Local Voice

## Goal

Make the offline tomato-to-fridge result understandable without reading debug counters. The rendered video must clearly show the recognized phase, the next instruction, and spoken Chinese guidance at phase transitions.

## Visual design

- Use a high-contrast black header occupying roughly 16% of a 1080p frame.
- Show the recognized phase in large Chinese text with a smaller English translation directly below it.
- Show `下一步 / NEXT` as a second high-contrast block using the next SOP instruction in Chinese plus a concise English translation.
- Use green for a confirmed phase, amber for the next instruction, and red only for a mismatch or recovery warning.
- Draw tomato, hand, and refrigerator annotations with thicker, distinct colors.
- Keep confidence, latency, score, and evidence counters in one small debug line at the bottom so they do not compete with the instruction.
- Continue to compare predictions against annotations, but make `MATCH / MISMATCH` visually secondary to the user instruction.

## Voice design

- Use the installed macOS `Meijia` voice through the local `say` command. It is a classic system Chinese voice and requires no iFlytek or cloud service.
- Speak only when a recognized phase changes; never repeat on every frame.
- Each utterance confirms what was recognized and gives the next instruction. The final utterance confirms task completion.
- Reuse `server.pipeline.narrate.narrate_run` to synthesize clips and mux them into `annotated_narrated.mp4` with FFmpeg.
- Add an explicit local-narration option to the evaluator so normal tests and headless runs do not require a system voice.

## Data flow

1. StateEngine returns a completed-step transition.
2. The evaluator stores the recognized step and reads the next step from the SOP.
3. The same bilingual presentation model drives both the on-frame overlay and the Chinese narration item.
4. After rendering, the existing local narration pipeline schedules the transition clips and produces the narrated video.

Human annotations remain evaluation-only and never affect prediction, instructions, or speech.

## Acceptance

- The same `IMG_9789` video still reaches `completed` with at least 90% labeled-frame agreement.
- At 1080p, the recognized phase and next instruction are legible at normal playback size.
- The overlay contains both Chinese and English.
- The narrated output has an AAC audio stream and uses no network API.
- Speech occurs once per meaningful phase transition and includes the next instruction.
- Existing non-E2E tests remain green.
