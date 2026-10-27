# Native generation and local sprite assembly

## Scope

Start from a single isolated PNG, not a base sprite sheet. The agent generates poses with Codex's native image tool; Python extracts, registers and assembles locally. Generation itself is not offline.

Select this route for an explicit native/no-fal request with authorization covering native pose construction and local assembly. A lateral pixel-art source and permission limited to alpha cleanup do not select this route. Without an explicit native/no-fal choice, follow the generic route in SKILL.md; do not ask for a backend choice merely because edge cleanup is authorized.

Implemented profile: pixel-art quadruped running left or right, retaining the source viewing angle, eight frames covering two four-phase strides. A prior left-facing wolf was artistically favorable but failed full source-scale equivalence; it does not establish production approval or a measured general success rate. Each new result requires its own geometry and visual review. Other actions, frame counts and non-pixel rendering are outside this native profile. Preserve fal for them or for an explicit fal request. A no-fal constraint forbids silent fallback.

The visible flow is PNG + action + direction + duration -> reviewed sprite sheet and GIF. The agent handles annotations and review; never ask the user for JSON, measurements, a base sheet or a run ID. Inspect source anatomy before choosing the profile. Defaults: eight frames at 100 ms = 0.8 s, two strides, source anatomical scale. A shared transparent canvas expands when poses need more space; no shrink-to-fit. Duration means playback duration, not video duration. `total_ms / 8` must be an integer multiple of 10 between 20 and 1000. Otherwise state the nearest achievable timing and resolve the difference before generation. Never silently change timing. Image decoding accepts 16..4096 per dimension and at most 8M pixels; this native geometry profile requires source canvas at most1024 per dimension /1M pixels. New-policy calibration allows final canvas up to4096 per dimension and64M pixels across all eight frames; historical policy retains its1024/1M final limit. Oversized source canvases are rejected before generation.

Reuse explicit user authorization for local Python image edits. If absent, request that narrowly required authorization because image-tool instructions require it for another editing route. Never ask again in the same authorized scope. Inspect the PNG before generation. If the native tool is unavailable, report that; retaining fal alone does not authorize its use in a native-only task.

## Prepare, generate and import

Resolve real bundled Python with Pillow and use `-B`. Commands below run relative to the skill. Angle-bracket paths are placeholders. Outputs must be outside the installed skill.

Resolve the real shared task first using [task-budget-and-corrections.md](task-budget-and-corrections.md). New public `prepare` requires `--task-manifest`; supply `--target-id` if more than one compatible target exists. The request must authorize `native_motion_construction`; permission for alpha/X alone is insufficient. The shared ledger retains earlier attempts, reviews and costs across runs. Never fabricate a FAL operation for local work or use private `_prepare` to avoid managed requirements.

```text
python -B scripts/native_sprite_workflow.py prepare --source <actual-source.png> --source-geometry <agent-annotated-source.json> --workspace-root <external-runs> --orientation left --frame-ms 100 --description "visible character identity and accessories" --task-manifest <actual-task.json> --target-id <actual-target-id> --max-attempts 2
python -B scripts/native_sprite_workflow.py begin --run-manifest <returned-manifest>
```

`prepare` checks PNG/alpha, magenta color collision and the source geometry contract, then returns actual manifest, source copy and prompt file. New public API/CLI runs require geometry; existing manifests without it remain inspectable under their historical contract. Do not call private `_prepare` to bypass this requirement. Never pass a biped, flight or death request as this run profile. Default maximum is two generation attempts; increasing it requires applicable authorization.

If a verified managed manifest remains `PREPARATION_PENDING` after interrupted setup, `begin` can recover that same initial run/attempt and create its prompt only after actual ledger acceptance. Inspect is read-only. Reuse the verified manifest; do not prepare another run to recover it. A pending manifest is not permission to generate, and an exhausted strategy cannot obtain a usable new prompt from recovery. A prior generation claim remains non-repeatable.

Before prepare, inspect the PNG and create a source geometry JSON: `source_sha256`, `anchors` (exactly two named objects with `points: [[x,y],[x,y]]` and observed `notes`), `body_anchor: [x,y]`, `pivot: [body_anchor.x,ground_y]`, `notes`, and `component_decisions`. Use stable visible anatomical length landmarks, such as eye-to-nose and shoulder-to-chest depth; exclude extended limbs, tail and loose chains. Coordinates use original PNG pixels, top-left origin. Optional primary_anchor explicitly selects the scale anchor; otherwise the alphabetically first anchor controls it, consistently across JSON serialization. The second independently checks body proportions. Note manual measurement uncertainty. Inspect `native_components.analyze_components(RGBA)` and explicitly classify each secondary component with `{id, decision: preserve_as_art|remove_as_background, notes}`. Never erase an accessory merely because it is disconnected. The input PNG is preserved; source-reference output reflects those explicit decisions.

For a prepared run, read its actual prompt and call `begin` to persist `NATIVE_GENERATION_STARTED`. If setup was interrupted at `PREPARATION_PENDING`, `begin` first recovers the same ledger identity and materializes the deferred prompt after acceptance, as described above; read that actual prompt after successful recovery. The agent then calls the native image tool once with that exact prompt and the returned source copy as `referenced_image_paths`. The prompt requires a flat magenta backdrop, support/collected/propulsion/extended poses, stable anatomy and secondary motion. Never use the fal sheet as the source in a source-only comparison.

Record the tool's observed local output path, then:

```text
python -B scripts/native_sprite_workflow.py ingest --run-manifest <returned-manifest> --raw <observed-generated.png> --provenance "Native tool result observed in this task: actual returned path"
python -B scripts/native_sprite_workflow.py inspect --run-manifest <returned-manifest>
```

An existing observed result can be imported directly from `PREPARED`; provenance must explain its origin. The manifest records `generation_claimed: false` instead of inventing a past claim. This verifies processing of an experiment, not that its historical prompt equals the prepared prompt; compare and report that distinction.

An unknown outcome remains `NATIVE_GENERATION_STARTED`. Recover only an observed result. Never call `begin` or the image tool again, invent a result, or create a replacement run to evade the claim. Interrupted `ASSEMBLING` can re-ingest the same recorded raw; conflicting content is refused. Fresh processing directories preserve partial evidence.

## Processing contract

The processor validates a magenta key, applies a difference matte and edge decontamination, and finds exactly four separated poses per row. Ambiguous detached content, merged poses or clipping reject assembly. Historical far-edge residue exceptions are logged; new strict runs require explicit component review. Magenta-colored source characters are unsupported; never erase their details or switch keys and claim validation.

Registration uses translation only and produces `READY_FOR_GEOMETRY_REVIEW` with hash-bound alpha, registered PNG, processing report and per-frame component reports. New scopes seal `body_x_preserve_y_v1` and use a single Y origin across all eight cells, preserving differences between rows and airborne poses. Registration is not a verified paw plane. Strict registration accounts for every post-matte pixel with alpha>0, with no automatic corner exceptions. Detached faint content survives into diagnostics when assignable; ambiguous spans, loss/duplication or clipping fail UNREVIEWED_ALPHA_LOSS. Historical runs retain their stored row-origin and logged alpha>=16/two-pixel crop behavior.

If strict registration rejects unassigned background fragments, run diagnose-matte --run-manifest <path> and inspect its alpha PNG plus global component report. Create a matte review with image_pixel_sha256 from that report and decisions for every secondary: preserve the other valid poses explicitly, remove only observed background. Re-run ingest with the same raw/provenance plus --matte-review-file <review.json>. This is local correction of the recorded result and does not spend a generation attempt. Stale/invalid review can be corrected and re-ingested; different raw content remains forbidden. Original alpha, reviewed alpha, pixel hashes and the full decision report are preserved. Never delete a whole pose to force four spans.

Inspect all eight 512x512 registered cells. Write a report with the returned `geometry_target_sha256` and `frames` ordered 1..8. Each frame has the same two `anchors` objects as the source, a visible `body_anchor`, `support_point` on the actual supporting paw for 1/3/5/7 (null for suspended 2/4/6/8), specific `notes`, and explicit `component_decisions` for every secondary component in that cell. Cell-local coordinates use top-left origin. Component decisions are pixel-hash bound; missing, stale or primary-removal decisions fail. Numeric checks require anchor points on visible art but cannot identify anatomy automatically.

```text
python -B scripts/native_sprite_workflow.py calibrate --run-manifest <returned-manifest> --geometry-file <agent-annotated-eight-frames.json>
```

Calibration applies one source-reference scale using the median primary length of all eight frames. Both anchor lengths in every frame must be within max(4 source pixels, 20%) of source. This is an explicit manual-measurement/pose tolerance, not a statistical quality claim; visual errors still fail. Never move annotation points or relax tolerance to force approval. Real anatomy failure requires `revise`; annotation mistakes can be corrected while still awaiting geometry. Horizontal alignment follows the same visible body anchor. Actual support points still require at most four source pixels disagreement per lane. New policy uses one common Y origin for every frame, including across rows; legacy policy keeps row origins. Flight/bounce are preserved under the common scale. The transparent canvas expands uniformly for all poses rather than changing their scale; final X centering follows the separate derivative contract.

Outputs include `geometry.json`, its hash-bound annotation report, and `source-reference.png` at original anatomical scale in the same canvas/pivot. `delivery.json` includes canvas, source/output pivots in top-left pixels, a bottom-left normalized pivot, and scale. These describe export coordinates; actual Unity import/animation/scene behavior still requires an engine test. Use the same pixels-per-unit for source and generated frames. Never equalize individual silhouette heights or force all paws to ground.

## Review, correction and export

Technical `READY_FOR_VISUAL_REVIEW` has `delivery_allowed: false`. Inspect source, sheet, all frames, GIF, edges on light/dark backgrounds, and transitions 4->5 and 8->1. Verify real support/flight, skull and torso scale, limb anatomy, accessory motion, direction and margins. Eight unique frames or no area flags do not prove animation quality.

Apply [native-motion-review.md](native-motion-review.md) to action phases, held equipment and every cyclic transition. Preserve the artifact-bound motion evidence alongside the final review. The current facade validates the seven-check report and artifact hashes; the agent must supply the additional motion evidence and its interpretation. The standalone measurement helper is diagnostic and does not turn an unsupported action into a supported profile.

Write a JSON report with `artifact_set_sha256` from `inspect`, `decision: pass|reject`, and exactly seven `checks`: `identity`, `scale`, `orientation`, `framing`, `background`, `motion`, `loop`. Each check has `status: pass|fail` and specific observed `notes` of at least ten characters. Any failed check requires rejection. A pass also requires two distinct `scale_anchors` objects with `name` and observed `notes`, and `flagged_frame_findings` objects with integer `frame` and specific `notes` for every flagged frame. Use an empty list when no frames are flagged. Do not copy an earlier approval onto changed pixels.

```text
python -B scripts/native_sprite_workflow.py review --run-manifest <returned-manifest> --review-file <actual-review.json>
python -B scripts/native_sprite_workflow.py export --run-manifest <returned-manifest> --output-dir <external-final>
```

New-policy native review pass returns `READY_FOR_ALIGNMENT`, delivery false. Follow [body-alignment.md](body-alignment.md): create the X-only child with the complete native evidence retained, perform its new full/body review and export that child through v3. The native direct `export` command above applies only to policy-absent historical scopes that reach `READY_FOR_DELIVERY`. Review and decision share one atomic manifest write. Changed hashes invalidate approval. A rejection cannot become a pass without correction and a new attempt. User acceptance and Unity validation stay pending; show rejected assets only as diagnostic evidence.

Exports register destinations before writing. A later rejection revokes `delivery.json` and `visual-review.json` in registered exports, retaining image evidence. Conflicting or inaccessible receipts stay `REVOCATION_PENDING` and forbid delivery. Use `recover-exports --run-manifest <path>` after an interruption. For a known historical unregistered export, use `adopt-export --run-manifest <path> --output-dir <actual-existing-export>`; it verifies source/raw/artifact hashes, request and original review before adoption. No arbitrary filesystem search or overwrite of third-party receipts is allowed. Previously copied exports outside known registered destinations cannot be discovered or remotely revoked.

For terminal technical or visual rejection, use remaining attempts with concrete feedback:

```text
python -B scripts/native_sprite_workflow.py revise --run-manifest <returned-manifest> --feedback "Observed defect and required correction." --correction-file <actual-correction.json>
```

Read the new prompt, `begin`, generate once, ingest and review again. Reimporting identical rejected RGBA pixels is not correction, even with different PNG compression or metadata. If `revise` is interrupted before saving the manifest, repeat it with the same feedback: an identical prepared prompt can be recovered, but any generation evidence prevents resetting that attempt. At the limit, or for unsupported source/profile, report the unresolved reason. Switch to fal only when route and spending are already authorized; otherwise preserve the option and request only the necessary scope change. Never lower standards to force approval.

For managed revisions, the correction file uses the exact `defect`, `plan`, `evidence` contract in the shared-task reference. `--max-attempts` is the fixed shared strategy limit, default2 and maximum3, not a fresh allowance per folder. `task_budget` describes the retained task exposure; local processing adds no provider reservation and does not establish that native image generation was free. Native review receipts and live ledger approval must agree. A later validated rejection can revoke PASS and blocks dependent delivery; read-only `inspect` diagnoses pending reconciliation without rewriting evidence. Historical scopes retain their stored contracts.
