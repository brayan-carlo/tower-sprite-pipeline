# Native biped walk or planted attack with local articulation

Use for an explicit native/no-fal request for one isolated biped PNG, walk or planted attack, left or right, with authorization covering local motion construction. Permission limited to alpha cleanup or X-only centering does not cover articulation. The agent handles construction and review from PNG + action + direction + duration; do not ask the user for layers, a recipe, landmark coordinates, manifests or a prepared sheet. Keep an explicit fal request on fal. Attacks with stepping, flight or quadruped anatomy and other views are outside this profile.

This route combines observed native image-tool output, source art and local layer articulation. It is not an offline generative model or a one-command PNG animation detector. Preserving fal as an option does not authorize a service call. Use the built-in image tool for missing artwork; use no image API/CLI fallback without explicit authorization.

## Plan and prepare the art

Inspect the source at its actual size, alpha and full equipment extent. Follow [native-motion-review.md](native-motion-review.md). Identify persistent near/far limbs, visible scale landmarks, grip/prop relationships and occluded joints. Keep the original intact and work in an external task directory. Do not introduce a biped into the quadruped `native_sprite_workflow.py` profile.

Use eight poses: contact, down, passing, up, opposite contact, opposite down, opposite passing, opposite up. Validate opposite contacts and passing poses before filling intermediates. Repeated half-cycles, missing alternation or broken joints require correction. Do not infer action quality from a guide, a sine curve or a successful render.

For a planted attack use eight poses: ready, anticipation, load, strike, follow-through, recovery, recovery, settled. Both actual soles remain planted. Validate the raised/load and strike poses before filling the recovery. There is one strike; 8→1 is a comparison of settled state and initial scale, not an instruction to replay the attack. Retain one-shot playback. These labels do not prove a convincing attack: inspect the trajectory, joints, silhouette and timing.

For a heavy whole-body strike, those key poses must show a connected progression through leg loading, pelvis/torso response, shoulder, arm and fist or held weapon, followed by deceleration and recovery. Planted soles do not require a frozen torso or knees. Compare the requested motion and any supplied quality reference at the same timing; reject an isolated arm swing when it does not convey the requested body participation and weight. If the current layers cannot express that action, change the construction or choose another route within existing authorization before polishing edges or filling intermediates. A native-only request still excludes paid fallback. Fixed segment lengths and clean joints establish construction integrity, not the quality of the attack.

The profile supports a shared canvas of 16..2048 px per dimension and RGBA PNG inputs up to 4096 px per dimension / 8 million pixels. Preserve source anatomical scale when selecting the canvas; expand transparent margins or report a size limitation instead of silently shrinking individual frames. Timing is one interval of 20..1000 ms, in multiples of 10 ms, for all eight frames. Total duration is eight times that interval. Default to 100 ms / 0.8 s only when duration is unspecified. If the requested duration cannot be represented exactly, resolve that timing difference explicitly before promising exact duration.

When new art is needed, use native imagegen with the inspected source and a concrete pose/repair prompt. Ask for real alpha and preserve it when provided. Apply existing user authorization only to the operations it covers: extraction, matte cleanup, overlap repairs and articulation are distinct operations. Do not expand an edge-only permission to motion construction. Inspect the actual output before cutting; a key color must not erase similarly colored character pixels. Preserve raw outputs and rejected versions.

Before each image-tool call, record its exact prompt, source/reference file hashes and started status in the task's generation ledger. Once a result is observed, record its actual path/hash and observed result status. An unknown result remains pending; do not resubmit or fabricate a completed output. These are image-service observations, separate from local build attempts. The local build never submits a provider job or charges for generation.

Prepare fixed RGBA ownership layers with overlap at joints throughout their angular range. Keep blade/guard/grip rigid and attached to the hand; articulate shoulder and elbow when the observed action needs it. Inspect restored cloak connections and adjacent equipment for doubled fragments. Leg near/far identity follows attachment, not current horizontal position. For a projected ground walk, solve foot pitch/contact, ankle and knee coherently, then inspect the actual soles and transfer of support.

## Recipe and source geometry

The agent writes a `native_articulated_layers_v1` recipe. All referenced files are bounded relative paths beneath the recipe directory, without `..`, symlinks or junctions, with exact SHA-256 values. Preserve the observed source and native input bytes. Do not relabel local composites as raw tool output.

Recipe fields:

- `schema`, `canvas: [width,height]`, `frame_ms`, `action: walk|attack`, `orientation: left|right`. Walk loops; attack is one-shot. The request is immutable within a run.
- `source: {path,sha256}`; `native_inputs`: 1..8 `{path,sha256,provenance}` records describing the actual observed input and local derivation.
- `assets`: 1..32 named `{path,sha256}` RGBA layers, total at most 64 million asset pixels.
- `frames`: eight ordered `{frame,layers}` records. Each layer is `{id,asset,origin:[x,y],target:[x,y],angle,scale}`. Angles are degrees. Positive rotation follows screen coordinates; origin maps to target. Alpha composition follows array order. Use the same unique instance ids, asset mapping and scale for every frame; targets, angles and draw order may vary. Scale must be finite, 0.1..4. Transformed alpha bounds must fit the shared canvas. The renderer does not automatically solve joints, identify anatomy or resize art.

A supplemental joint patch may declare `role: joint_overlap` and boolean `visible`. This role must remain fixed across all frames and its asset must be nonempty. Only this supplemental role may be hidden; body, limb and equipment layers remain present. Keep at least one primary layer. Use this for overlap pixels that would double the source alpha in a neutral pose, never to relabel a limb as optional. Inspect the patch contents, local alpha and all joint angles. Near-opaque source pixels need not have alpha 255; verify that selection actually contains the intended artwork. Missing artwork requires a real repair, not an empty patch or a falsely recorded success.

When only source pixels are used, `native_inputs` records the actual source file and describes source-only local construction. Never label a local composite as raw generated art or fabricate an image-service observation. A build is local and records `generation_claimed: false`.

Source geometry JSON contains `source_sha256`, two unique `anchors` (`name`, two visible `points`, specific `notes`), visible `body_anchor`, source-canvas `pivot`, nonnegative `uncertainty_pixels`, `held_items` (`id`, `notes`; empty only for an unarmed source), and `notes`. Points use top-left pixels. Landmark lengths must be at least 4 px and rounded pixels have alpha >=16. A pivot is an export coordinate, may be transparent and must be within its own canvas. Hidden joints are not visible landmarks.

## Build, inspect and recover

Run from this skill with the resolved Python/Pillow runtime and `-B`. The agent supplies the internal file arguments:

Resolve the real shared task through [task-budget-and-corrections.md](task-budget-and-corrections.md) before a new public build. It must cover `native_motion_construction` and the actual original source, action, orientation and requirements. `--task-manifest` is required; omit `--target-id` only when exactly one target is compatible. The ledger preserves costs, attempts and reviews across folders; do not create another task or use private `_build` to evade a limit. Historical scopes retain their stored contract.

```text
python -B scripts/native_articulated_workflow.py build --recipe <recipe.json> --source-geometry <source-geometry.json> --workspace-root <external-runs-directory> --task-manifest <actual-task.json> --target-id <actual-target-id> --max-attempts 2
python -B scripts/native_articulated_workflow.py inspect --run-manifest <returned-run-manifest>
```

Build snapshots declared inputs and writes eight exact RGBA frames, a horizontal sheet, GIF, contact sheet, source reference and geometry, component/QA/provenance data and a render report. New scopes seal `body_x_preserve_y_v1`, preserve all recipe Y coordinates and return `READY_FOR_VISUAL_REVIEW`, delivery false. The build's `generation_claimed: false` means this local invocation did not generate images through a service; native input provenance remains recorded separately. It is not a claim that the project used no native generator.

`ASSEMBLING` after interruption uses `resume --run-manifest <path>` to finish from its existing snapshot without regeneration. `TECHNICAL_REJECTED` and `INTEGRITY_REJECTED` are failed states, not export permissions. Preserve the returned run and evidence. Source, action, direction, duration and canvas are immutable within a run.

## Artifact-bound review

Inspect source, all eight frames, sheet, GIF, and light/dark edges at a common source-pixel scale. Compare identity, two stable anatomical dimensions, direction/view, margins, alpha, body-part ownership, opposite phases and every cyclic transition. Use requested and slower playback. Record observations as an agent review of frames/sequence/playback; do not claim human continuous observation or engine validation that did not occur.

The JSON report uses the current `artifact_set_sha256` from `inspect`, `decision: pass|reject`, and exactly seven `checks`: identity, scale, orientation, framing, background, motion, loop. Each check has `status: pass|fail` and specific `notes` of 10..4000 characters. Any confirmed failed check requires rejection. A pass additionally contains:

- Two distinct `scale_anchors` with `name` and observed `notes`, plus `flagged_frame_findings` (`frame`, `notes`) for every flagged frame; use `[]` when none are flagged.
- `geometry`: one output-canvas `pivot`, `first_support_limb: near|far`, eight ordered `frames` with `frame`, the two source-named `anchors` with current points/notes, visible `body_anchor`, `notes`, and `supports`. Supports is exactly `{near:{role,point},far:{role,point}}`. Both roles are `contact` at 1/5. First limb is `stance` at 2..4 and `swing` at 6..8; the other reverses. Swing point is null; contact/stance points are actual visible sole pixels. Their Y spread is at most 4 px per projected lane. Different lanes may differ in Y. This validates annotations, not physical weight-bearing.
- Anchor length deviation is bounded by max(4 source pixels, 20% of source length). Visible disproportion still fails inside that tolerance. Never move endpoints, equate silhouette heights or relax tolerances to force a pass. No art is rescaled during review.
- `components`: eight `{frame,image_pixel_sha256,component_decisions}` records from actual component reports. Explicitly preserve/remove every secondary component. A removal that changes pixels requires correction and rebuilding before approval.
- `motion.frames`: eight records with `frame`, `phase` (the eight phases above), specific `notes` and `equipment`. Each equipment record has a source-held-item `id`, `attachment: visible|occluded`, `rigidity: pass|fail`, and specific `notes`. Include every item exactly once. Explain occlusion without invented grip coordinates.
- `motion.transitions`: all eight ordered `{from,to,status,notes,equipment}` records, including 8→1. Each equipment record has `id,status,notes`. Describe support transfer and arm/prop continuity, including 4→5 and 7→8→1→2. Every status must pass for approval.
- `motion.playback`: requested interval then a strictly slower interval, each with `frame_ms,status,method,notes`. Method is `agent_frame_and_playback_review` or `human_continuous_review`, as actually performed. Describe rhythm/support/prop findings; presence of these fields alone is not proof of quality.

For `action: attack`, the same seven checks, two visible anatomical anchors, component decisions, equipment evidence and two playback reviews remain mandatory. Use these action-specific fields instead of walk evidence:

- `geometry.first_support_limb` is not required. Both near/far supports have `role: stance` and visible sole points in every frame. Each support's X and Y spread must be at most 4 px; a stepping attack needs another profile.
- `motion.frames` phases are exactly `ready`, `anticipation`, `load`, `strike`, `follow_through`, `recovery`, `recovery`, `settled`. Explain the single strike and subsequent return; do not insert a second strike to fill eight frames.
- Keep all eight ordered transition records. Describe 8→1 as settled-to-ready continuity and scale comparison; the exported GIF has no loop extension. The `loop` check covers this one-shot/settled behavior.
- Attack GIFs use a shared palette and binary transparency at alpha 128; PNG alpha stays intact. Inspect the decoded GIF on black, gray and white because thin features may need a separately traced correction. Existing walk GIF encoding is unchanged so prior manifests remain verifiable.

Validator tolerances are rejection bounds, not artistic acceptance thresholds. Retain stricter task-specific anatomical tolerances and distinguish visible landmarks from construction pivots. Occluded endpoints cannot be invented to satisfy review.

```text
python -B scripts/native_articulated_workflow.py review --run-manifest <path> --review-file <actual-review.json>
```

Reject needs the target and seven checks with at least one failure; extended pass evidence is not required. Incomplete pass evidence leaves the candidate unapproved. A rejected attempt cannot be reapproved; use corrected pixels and a new attempt. No numerical helper automatically grants artistic approval.

## Correction and verified delivery

```text
python -B scripts/native_articulated_workflow.py revise --run-manifest <path> --recipe <corrected-recipe.json> --feedback "Observed defect and actual correction." --correction-file <actual-correction.json>
python -B scripts/native_articulated_workflow.py export --run-manifest <path> --destination <external-final-directory>
python -B scripts/native_articulated_workflow.py recover-exports --run-manifest <path>
```

Revise snapshots the new recipe, atomically withdraws prior approval and starts the new local attempt before rendering. Exact previously rejected frame pixels are refused even when PNG encoding changes. Prior versions and registered export images remain evidence; owned approval receipts are revoked. Local correction does not itself authorize or submit another image-service call.

Managed revisions use the exact `defect`, `plan`, `evidence` correction contract from the shared-task reference. The build seals `--max-attempts` (default2, maximum3) for the shared strategy; another folder, prompt or recipe does not renew it. The returned `task_budget` retains earlier task exposure without a new provider reservation for local rendering. External image-tool costs remain separately unverified. Canonical native review receipts must reconcile with the live ledger; later validated rejection revokes eligibility for this attempt and its dependent child. `inspect` diagnoses without writing a review.

For new-policy scopes, complete native review pass returns `READY_FOR_ALIGNMENT`, delivery false. Follow [body-alignment.md](body-alignment.md) to create a separate X-only v3 child; retain the native parent's strict anatomy/support/components/equipment/transitions evidence and its live workspace. The child requires its own full/body review before v3 export. Never translate native recipe outputs in place or bypass native review with a generic pass.

The native direct export commands above apply only to historical policy-absent scopes. They require `READY_FOR_DELIVERY`, use verified source-run buffers, check destination conflicts and write approval receipts last. Historical delivery contains source/output canvas and pivots, normalized output pivot and scale1; use the same pixels-per-unit when comparing the source. New X-only derivatives preserve scale1 and document the common output canvas in their proof; engine import still requires verification. User acceptance and Unity validation remain pending until separately performed.

`qa.json` retains the immutable build-time diagnostics and unreviewed state. The final artistic decision and delivery permission are recorded in `visual-review.json` and `delivery.json`; do not rewrite build evidence to pretend it was already approved.

Registered exports are checked for changed artifacts/receipts. Recovery handles interrupted writes and revocations; conflicting foreign files are reported and preserved, with delivery blocked as appropriate. Do not delete or overwrite them to silence a conflict. Unregistered copies outside known destinations cannot be discovered or remotely revoked. This profile has no `adopt-export` command.

Deliver only the verified export and its concrete review findings. One accepted character/action proves viability for that case, not a general reliability percentage or that fal can be discarded. Keep the fal route available.
