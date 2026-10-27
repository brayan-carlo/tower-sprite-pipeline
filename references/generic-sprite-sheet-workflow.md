# Generic sprite sheet workflow (v3)

Use this reference for a new animation request built from one supplied isolated PNG. The facade owns backend selection, pricing, durable state, bounded retries, cleanup, frame selection and assembly.

## Conversation contract

Resolve the request from the current conversation in this shape:

1. Identify the single supplied source image. Reuse an explicit recent source; ask which image only when the source is absent or ambiguous.
2. Reuse any action, orientation, dimensions, timing, loop preference, cost cap and authorization already stated in the current request context.
3. Ask one compact question containing only the missing action and/or orientation. Do not ask for fields that have defaults.
4. Once source, action and orientation are known, explain the effective defaults briefly and proceed under the existing task authorization. Resolve the shared ledger using [task-budget-and-corrections.md](task-budget-and-corrections.md); this is agent bookkeeping. If no verified v3 manifest exists, the first facade command is `generate` with that actual task context.

Defaults are 8 frames, 100 ms per frame, the source image dimensions as each frame's canvas, pixel-art handling, and a horizontal sprite-sheet delivery. Loop is enabled automatically for walk, run, idle and fly; it is disabled for other actions. The normal operational limit is at most 2 generation attempts and a conservative estimated-cost cap of USD 0.50. Explicit supported overrides win.

The normal supported ranges are 2–16 frames, 20–1000 ms in 10 ms increments, 16–4096 pixels per canvas dimension, and at most 64 million output pixels. A lower or higher explicit cap replaces USD 0.50. Apply the known authorization to the shared task and the new run's `--max-cost-usd`; do not accidentally leave a smaller default local cap and ask for permission already given. If the complete live reservation cannot fit, preserve the actual refusal and remaining exposure. Do not edit an immutable cap or start a replacement task/run to reset it. A genuine budget increase needs explicit authorization and a supported transition that retains history; it is not a fresh budget.

Do not request a guide, run ID, request JSON, runtime path, provider/backend selection, frame count, timing, canvas, loop choice, or budget when the corresponding value is already known or has a documented default. A request to perform the operation is its authorization within the stated/default cap. Price refresh, known-ID resume and an in-scope bounded revision do not create another approval gate.

## Automatic route

With `--strategy auto`:

- `walk` toward `left` or `right` uses `minimax/h3-max/image-to-video`, measured cycle selection, and one `fal-ai/ideogram/remove-background` cleanup only when the selected grid is opaque.
- Other action/orientation pairs use `fal-ai/nano-banana-2/edit`; opaque output similarly receives at most one cleanup per attempt.

This routing is implemented and tested. It does not establish universal semantic or visual quality for every action and orientation. Do not promise a particular gait, identity score, provider result, or cleanup quality before inspecting actual artifacts.

All new v3 scopes preserve the cells' shared origin and one common scale (`grounded=false`) and seal `alignment_policy=body_x_preserve_y_v1`. This retains body motion and foot heights before the separate body-X alignment. Follow [body-alignment.md](body-alignment.md) to annotate the body, create an X-only derivative and verify it before PASS/export. Weapons and tails must not determine the center. Existing manifests and resumes retain their stored request; old normalization cannot regain erased bounce. Provider anatomy/camera/gait defects still require visual rejection or a justified correction.

## Runtime discovery

Run the facade with Python 3.12-compatible `Pillow`; use Node 22 or newer for its native-fetch transport. The video route also requires external FFmpeg. Discover available runtimes on the user's machine and pass `--ffmpeg-exe` only when discovery does not already find the correct binary. Dependency discovery and selection are agent tasks; do not make the user choose executables. Do not install a dependency inside the active skill directory.

Use Python with `-B` and set `PYTHONDONTWRITEBYTECODE=1`. Run examples from the skill root after replacing paths with actual resolved absolute paths:

```powershell
& $pythonExe -B scripts/generic_sprite_workflow.py generate --source 'C:/path/character.png' --action 'caminar' --orientation 'izquierda' --task-manifest 'C:/actual/task/task.json' --target-id 'actual-target'
& $pythonExe -B scripts/generic_sprite_workflow.py resume --run-manifest 'C:/external/run/run-manifest.json' --poll-seconds 45
& $pythonExe -B scripts/generic_sprite_workflow.py inspect --run-manifest 'C:/external/run/run-manifest.json'
& $pythonExe -B scripts/generic_sprite_workflow.py revise --run-manifest 'C:/external/run/run-manifest.json' --feedback 'La pierna cercana debe avanzar en el contacto opuesto.' --correction-file 'C:/actual/correction.json' --poll-seconds 45
```

The public commands are `generate`, `resume`, `inspect`, `revise`, `review`, and `export`. `inspect` is strictly read-only. New public generate requires `--task-manifest`; `--target-id` may be omitted only for one compatible target. Read `python -B scripts/generic_sprite_workflow.py --help` and the relevant subcommand help before adding optional flags. Useful supported generation overrides include `--frames`, `--frame-ms`, `--canvas WIDTHxHEIGHT`, `--loop auto|true|false`, `--max-cost-usd`, `--max-generation-attempts`, `--description`, `--pixel-art true|false`, `--strategy auto|sheet|video`, `--ffmpeg-exe`, `--cycle-start-seconds`, `--cycle-duration-seconds`, and `--poll-seconds`. Managed rejection/cycle revision uses the exact structured correction described in the shared-budget reference; legacy feedback-only revisions retain their old contract.

Do not pass the secret as an argument: there is no key flag. The facade looks for `FAL_KEY` in the current Process environment, then in the Windows User environment. Do not treat prior knowledge that the key is absent as proof that a run exists. When source, action, orientation and shared task context are resolved and no verified run manifest exists, call `generate` once; if both environments lack the key, the facade creates durable local run state and returns `KEY_SETUP_REQUIRED` with `FAL_KEY_MISSING` and its actual `run_manifest`, without exposing a value.

Preserve a returned manifest path only after observing it in the facade result. Direct the user to Windows **Edit environment variables for your account**, create or update the User variable named `FAL_KEY`, and paste the value only into that protected value field. After setup, use `resume` only with that verified returned path. If setup is completed while no verified run manifest exists, the next command is the first `generate`. Never invent a path or use `resume` without a real manifest. Do not ask the user to paste, dictate, confirm, or store the secret in chat or config.

## Durable states and next action

The facade returns `state`, `run_manifest`, `artifact_paths`, `delivery_allowed`, `delivery_artifact_paths`, `artifact_set_sha256`, `consistency`, `spent_estimate_usd`, `next_action`, `retry_safe`, `block_reason`, and `defaults_notice` (plus `missing_fields` for `NEEDS_INPUT`). Managed results also carry authoritative `task_budget` and review eligibility from the live task. `artifact_paths` are inspection evidence; only `delivery_artifact_paths` after review/export are delivery candidates. Follow the state and `delivery_allowed` rather than inventing a command:

| State | Agent action |
|---|---|
| `NEEDS_INPUT` | Ask only the listed missing source/action/orientation fields. No run or cost exists. |
| `KEY_SETUP_REQUIRED` with a nonempty verified `run_manifest` | Preserve the returned path, give secure environment setup instructions, then `resume` that exact manifest. If no verified manifest exists, use the first `generate` after setup instead. |
| `PENDING` with recorded ID | Use `resume` on the same manifest. For that pending job, continuation uses the recorded ID through GET and never resubmits that job. After download, the authorized workflow may still create its required cleanup or bounded technical attempt within the same cap. |
| `SUBMISSION_STATUS_UNKNOWN` | Preserve manifest, marker and reservation. For a managed run, same-manifest resume may reconcile a late durable Node ID through the protected transport; it cannot submit anew. Without a recoverable ID it remains UNKNOWN. Never create a replacement or revise to bypass it; historical contracts stay unchanged. |
| `TECHNICAL_REJECTED` with a cycle diagnosis | Inspect the retained raw evidence. Use `revise` only with concrete visual/cycle feedback and only within the existing cap and attempt count. |
| `BLOCKED` / `COST_CAP_EXCEEDED` or `TASK_BUDGET_EXCEEDED` | Preserve the refusal and existing exposure. Resolve the actual missing context, corruption or budget issue; never assume every BLOCKED state is a price problem or create a new task/run to bypass it. |
| `READY_FOR_VISUAL_REVIEW` | Artifacts exist for inspection, but delivery/export is blocked. Inspect all frames and record an agent review. Existing v3 runs without a review are also gated. |
| `VISUAL_REJECTED` | A recorded visual check failed. Keep evidence; do not deliver as completed. `resume` stays offline and rejected. Correct/revise only within current authorization and attempt limits. |
| `READY_FOR_DELIVERY` | Artifact-bound agent review passed. `export` verifies files and review offline, then copies the reviewed artifacts. User/Unity acceptance is still separate. |

The revisable cycle diagnoses are `CYCLE_UNRELIABLE_ZERO_MOTION`, `CYCLE_UNRELIABLE_TOO_SHORT`, and `CYCLE_UNRELIABLE_FLAT_SCORES`. A bounded `revise` preserves the finished attempt and changes the prompt using explicit feedback. Pending work and unknown submissions are never revision candidates. Do not describe `BLOCKED`, runtime failure, or tamper detection as a visual correction opportunity.

## Cost meaning

For managed runs, use `task_budget` from the shared ledger, including held cleanup reservations. `spent_estimate_usd` remains a local compatibility estimate and must not be added again to the task total. Neither is an actual invoice, and the cap is not enforced by the provider's billing system. Prices, especially H3 video prices, are dynamic and resolution-dependent. The transport refreshes and reconciles its current evidence before a new submit. Do not hardcode a promotional rate, expiry date, or claim that two attempts will always fit the cap.

## Visual review and delivery

`READY_FOR_VISUAL_REVIEW` means the expected paths and hashes for the sheet, frame PNGs, contact image, preview GIF and QA report passed technical verification. It is not a deliverable result yet. Inspect the actual source and output and record concrete findings for:

- character identity, silhouette, palette and clothing continuity;
- requested orientation and full-body framing;
- clipping, background transparency, halos or added objects;
- distinct phases and, for walking/running, opposite leg contacts and weight transfer;
- loop continuity or a natural one-shot start and finish, as applicable.

Cycle selection measures image repetition; it does not prove contact alternation, gait quality, identity preservation or artistic quality. A confirmed size/proportion drift, wrong orientation, clipping, broken action or broken loop fails review. Show such artifacts only as rejected diagnostic evidence, not as the completed animation with a disclaimer. Cosmetic findings may pass only when the requested animation and consistency remain intact. Label user acceptance and Unity UAT only after those events occur; agent review is separate.

### Record review, then export

For a policy-bearing scope, first complete the offline derivation in [body-alignment.md](body-alignment.md). Use its new artifact target for review and add the required `body_alignment` object to the standard report below. An unaligned new scope remains inspectable and rejectable, but cannot pass/export. A local derivative's zero additional reservation does not reset the original task's spending.

The agent performs review; do not turn it into another user permission gate. `inspect` supplies `artifact_set_sha256` and fresh `consistency` measurements without changing files or spending money. An area ratio above 1.25 or below 0.80 versus peer frames is a scrutiny signal, not proof of anatomical scale drift. Width/height alone cannot distinguish a jump, crouch or foreshortening from a resize. No flags also requires visual review. Compare at least two stable anatomical anchors in comparable views and the last-to-first transition.

Write an external UTF-8 JSON review file with this structure, filling every note from actual inspection and copying the exact target digest returned by `inspect`:

```json
{
  "artifact_set_sha256": "COPY_THE_VERIFIED_TARGET_DIGEST",
  "decision": "reject",
  "checks": {
    "identity": {"status": "pass", "notes": "Describe the observed identity continuity."},
    "scale": {"status": "fail", "notes": "Describe the observed size/proportion defect and affected frames."},
    "orientation": {"status": "pass", "notes": "Describe the observed direction in all frames."},
    "framing": {"status": "pass", "notes": "Describe full-body margins and any clipping."},
    "background": {"status": "pass", "notes": "Describe transparency and edge quality."},
    "motion": {"status": "pass", "notes": "Describe the actual action phases and contacts."},
    "loop": {"status": "fail", "notes": "Describe the last-to-first discontinuity; for one-shots inspect start/end."}
  },
  "scale_anchors": [
    {"name": "head", "notes": "Describe skull/muzzle dimensions in comparable poses."},
    {"name": "torso", "notes": "Describe torso mass and its relation to the limbs."}
  ],
  "flagged_frame_findings": [
    {"frame": 8, "notes": "Describe whether the flag is legitimate pose change or an actual defect."}
  ]
}
```

Use the actual flagged frame numbers; use an empty list when none. Names of anatomical anchors adapt to the character. A pass requires all seven checks to pass, at least two distinct named anchors, and a finding for every flagged frame. A reject requires at least one failed check. Notes are agent evidence, not a mathematical guarantee of anatomy. The CLI validates completeness and binds the review to the run, attempt and exact artifact hashes; changing/revising artifacts invalidates prior approval.

When a particular failed check concerns exclusively X position or alpha residue, use the optional strict `repairable_defects` contract in [task-budget-and-corrections.md](task-budget-and-corrections.md). Classification applies only to that check; retain every other failure. Do not classify a framing/background check that mixes repairable and nonrepairable defects, attest to facts contradicted by other findings, or edit a prior terminal rejection. A later validated reject can revoke a managed PASS; keep all receipts. A managed delivery also needs effective PASS with no unresolved inherited defects; artifact snapshots alone cannot override those live review gates.

```powershell
& $pythonExe -B scripts/generic_sprite_workflow.py review --run-manifest 'C:/actual/run/run-manifest.json' --review-file 'C:/actual/review.json'
& $pythonExe -B scripts/generic_sprite_workflow.py export --run-manifest 'C:/actual/run/run-manifest.json' --output-dir 'C:/actual/delivery'
```

`review` and `export` are offline. A recorded reject returns `VISUAL_REJECTED` and exit code 2; the rejection was persisted. A confirmed rejection cannot become a pass on the same unchanged attempt: correct the assets in a new authorized revision. `export` requires `READY_FOR_DELIVERY`, verifies the review and files again, and rejects output conflicts. Do not bypass a rejection by copying or assembling a different set outside this gate. Corrective transformations also need a fresh artifact-bound review before final delivery.

## Managed regional video repair

Use `scripts/regional_video_workflow.py` through its Python API for the closed
`regional_video_inpaint_v1` / `vace81_24_720_v1` profile. `prepare` accepts the real
task, target, provenance, media and reviewed preflight documents, plus explicit
output root, prompts and FFmpeg path. It validates imported historical evidence,
canonical PNG padding, provider preparation, decoded 81-frame CFR video and
binary mask, RGB24 hashes, source ordinals and inverse geometry. The files in the
run are immutable snapshots; original historical evidence is rechecked live.

`quote` obtains the primary execution quote and cleanup planning quote before
`begin_attempt(max_attempts=1)` or the joint reservation. `record_gate` archives an
explicit `input_ready` report without sending anything; `run` requires its PASS
and both verified receipts. It stops at `REGIONAL_RAW_REVIEW_REQUIRED` after the
primary RAW and never grants an automatic artistic PASS. Use the same manifest
with `resume` after recording `raw_ready` for the exact RAW, probe, selection,
native grid and physical `result-transform.json` hash. The required RAW checks
are identity, anatomy, scale, material, orientation, framewise_motion, mask_scope,
exterior, crop_background and loop_selection. A hold/reject blocks cleanup.

The exact selected native ordinals form a 4-by-2 grid; cleanup runs on that grid.
Only then are eight RGBA cells split and transformed with one common NEAREST
factor derived from observed output width, followed by the declared background
crop. Subject pixels outside the crop block assembly. No union-alpha sizing,
retiming, automatic cycle search or per-pose scale adjustment occurs. Continue
with existing `align_manifest`, `record_visual_review` and `export_artifacts`.
Alignment fixes horizontal homologous BODY center while preserving natural Y.
A new artifact-bound visual review is required after alignment. Generic
`resume`/`revise` return `REGIONAL_DRIVER_REQUIRED` for the regional parent.

`inspect` is read-only. Durable Node ID recovery uses the same operation; a
marker without ID remains UNKNOWN. The bridge rechecks physical evidence before
the ledger marker, writes only the ledger during `before-submit`, and reconciles
Python mirrors after transport returns. Input reports and numeric checks do not
approve anatomy, occlusion, crop background or the output's artistic quality.
