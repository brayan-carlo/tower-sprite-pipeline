# Shared task budget and correction evidence

This is agent bookkeeping. The user still supplies the image, action, direction and timing; do not ask for task IDs, JSON, receipts or runtime paths.

## One human task, one ledger

For new managed generation, resolve the existing task from the current request, working notes or a verified manifest's `request.task_ref`. Use `sprite_task_ledger.inspect_task` to verify it. Reuse that exact ledger for the requested set of characters/actions, revisions and local derivatives. A new run, prompt, UUID, padding or retry is not a new budget. Never call `create_task` to replace a missing, corrupt, exhausted or rejected task.

For a genuinely new task with no prior operations, the agent may call the actual Python API `create_task(output_root, authorization, targets)`, imported from `scripts/sprite_task_ledger.py` using the discovered Python runtime. It returns the real `task_manifest`, `task_id` and `authorization_sha256`; retain them in the working notes. Keep the output outside the skill and game project. There is no separate public ledger CLI.

`authorization` has exactly `task_id`, `cap_usd`, `budget_basis`, `allowed_operations` and `evidence_ref`. Use a stable task identifier, an exact decimal string for the applicable total cap, and evidence of the actual request/authorization. For FAL operational reservations use `budget_basis='fal_operational_reservations'`; do not label unknown external generation costs as zero or claim an invoice. Allowed operations are separate: `provider_animation`, `alpha_edge_cleanup`, `body_x_alignment`, `native_motion_construction`. Include only operations covered by the request/context. Cleanup or centering permission does not authorize motion construction or a provider change.

Each target has exactly `target_id`, `source_sha256`, `action`, `orientation`, `requirements`. Hash the actual original PNG; normalize action/orientation to the request values. Requirements use the request's represented keys, such as `frames`, `frame_ms`, `pixel_art` and `loop`. Preserve explicit constraints. An unsupported requirement must block, not be discarded. Do not create another target for a retry, prepared image or alternative prompt. The agent resolves these fields; the user does not fill a form.

Pass the returned path to new v3 `generate --task-manifest <actual-task.json>`, with `--target-id` when needed. Without target ID, exactly one compatible target must exist. The facade seals `request.task_ref`, binds the run, and verifies the context before pricing. Do not call internal `_generate_legacy` for new work. Historical scopes without task_ref retain their stored resume/revise/cleanup contract; do not insert a task_ref into them.

If the human task already has costs or failed attempts outside this ledger, preserve and reconcile that history before any new managed spend. `import_reservations` alone records money; it does not import strategy attempts or reviews. Do not use an economically loaded but historically empty task to retry an exhausted mechanism. Use the sealed history migration below; it is not automatic and never replaces an existing shared ledger.

## Migrating an existing task with external history

Only when the human task has no existing shared ledger, prepare its complete finite history and the unchanged effective authorization before creating that ledger. Preserve the original cap, retained exposure, uncertain charges, actual attempts, source ancestry, reviews and unknown costs. The caller verifies physical evidence and the scope/cutoff of its inventory. A new directory, prompt or runtime version is not permission to omit earlier failures.

The actual API is `create_task(output_root, authorization, targets, historical_import=requirement)`. The requirement has exactly `schema='sprite_history_requirement_v1'`, a stable `migration_id` and `bundle_sha256` computed from the ledger's canonical `encoded(bundle)`. It is part of the task seal from creation. Do not initialize without it and add money later. If creation or import is interrupted, locate and verify that same task; never create a replacement to bypass pending history.

Use `import_task_history(task_manifest, bundle)` once the exact reviewed bundle is ready. The bundle has `schema='sprite_task_history_v1'`, matching `migration_id`, `eligibility_policy='historical_generation_closed_local_source_v1'`, complete target coverage, reservations and separate historical attempts. Its closed schema is validated by `sprite_task_ledger.py`; do not invent fields or drop original facts to fit it. Each reservation has one historical owner and its physical `evidence_sha256` must occur in that owner's evidence refs. A semantic configuration digest is a different contract from a physical file digest.

The single import event is atomic; an exact repeat does not duplicate costs or attempts. A different bundle or an unsealed import fails. Imported attempts do not become executable binds, source attempts or artistic PASS. Review sequence numbers express only supported relative order within one attempt; null means order is unknown, and array order never proves chronology or current approval. Known legacy IDs retain their original recovery contracts.

`inspect_task` reports `history_status`, `historical_attempt_count` and `history_blocked_targets` when the task has this requirement. Pending history has unknown reserved/available amounts (`None`), not zero or a fresh cap. New scope creation, preparation, pricing, rendering, alignment and submission are guarded. Existing verified markers/IDs retain their protected recovery path; an unknown submission never gains another POST. Completing the import can still leave targets blocked by unresolved generation history.

Previously attempted equivalent generation mechanisms remain closed under this policy. Proven local transformations stay tied to their actual source and do not globally prohibit centering a genuinely new eligible output. Prompt, seed, padding, UUID or alignment-source changes cannot reopen generation. A future model or mechanism extension must reconcile its equivalence with sealed historical records; importing history does not enable unsupported routes. The agent resolves this bookkeeping without asking the user to supply IDs, JSON or receipts.

## Price, submission and recovery

The facade reserves the complete primary-plus-possible-cleanup plan before the first POST. Cleanup's future source has a sealed deferred plan and becomes executable only after the exact completed primary is verified. Repricing must fit the same cap; a lower quote does not erase retained exposure. A failed or unknown submission is not a refund. Do not manually edit reservations or create another task/run to bypass a refusal.

Use `task_budget` for the authoritative shared cap, retained reservations, available estimate, basis and uncertainty. `spent_estimate_usd` remains a compatibility estimate for that run; never add it again to the shared total. Local alignment adds no provider reservation but retains the task's earlier costs. `invoice_verified` remains false without actual invoice evidence; external image generation may have unverified costs outside this FAL journal.

Resume the same managed manifest to reconcile an interrupted mirror write or a known request ID, including an ID that arrived in Node after the facade had recorded UNKNOWN. This uses the existing operation and protected transport. Marker/no-ID remains UNKNOWN and cannot be resubmitted or revised. Do not interpret missing claim/state files as proof that no POST occurred. `inspect` diagnoses without repairing files.

## Corrections after a rejection

Read the exact recorded defect list and immutable evidence. A managed `revise` after rejection needs `--correction-file` containing exactly `defect`, `plan`, `evidence`: the defect must match the actual rejected strategy; the plan must describe a pertinent different correction, supported by actual evidence. Feedback still informs the prompt. A prompt-only change does not become a new strategy, and repeating the same defect/plan does not renew attempts. A terminal cycle heuristic is technical evidence (`technical_cycle:<actual code>`), not a fabricated artistic review.

Use X translation only for displacement of an otherwise valid body. It cannot repair scale, anatomy, missing pixels, Y motion, material or gait. Use alpha cleanup only for removable background residue; it cannot restore subject pixels or repaint a character. Actual corrections need their own proof and complete new review. Prefer an honest unresolved result over lowering the acceptance criteria.

The seven visual checks retain their meanings. A new rejection may optionally include `repairable_defects`, a list of at most two entries with exactly `check`, `kind`, `frame_findings`, `attestations`:

| check → kind | Exact attestations, all literal true |
|---|---|
| `framing` → `position_x` | `all_frames_inspected`, `only_horizontal_body_translation`, `no_clipping_or_missing_pixels`, `no_y_scale_pose_anatomy_change` |
| `background` → `alpha_edge` | `all_frames_inspected`, `only_exterior_alpha_edge_residue`, `no_missing_subject_pixels`, `no_rgb_anatomy_pose_change` |

Each classified check must be failed, and the overall decision must be reject. `frame_findings` lists all affected frames as unique `{frame, notes}` objects: actual 1-based integer frame numbers and specific observed notes of 10–4000 characters. Inspect every frame, even if only some are affected. These statements are reviewer judgments, not facts inferred automatically from booleans. Visible background between limbs may contain exterior residue; an artificial hole through the subject is not such residue.

A classification replaces only that failed check's defect prefix. Other failures remain. If a framing/background failure mixes reparable and nonreparable problems, omit the classification. No field or an empty list keeps the ordinary check prefix; words such as "X" or "alpha" in notes never classify a failure. Key order in a JSON object has no semantic meaning: mapped defects use a deterministic check order.

The immutable review receipt binds classification, frame findings and attestations to the exact artifact set. Do not edit an old terminal rejection to add a subtype later. A valid later reject can revoke PASS while preserving both reviews; REJECT cannot return to PASS on that same attempt. New managed delivery also requires a current effective PASS and no unresolved inherited defects, in addition to the actual artifact/alignment/native checks. Historical scopes retain their earlier contract. None of these bookkeeping gates grants artistic approval by itself.

## Regional max1 reservation and gates

The closed regional family is keyed by canonical target plus
`spatial_video_mask_reference_v1`; prompts/media/runs do not create another quota.
Historical provider generation remains an imported historical source, with its
actual evidence and canonical identity verified physically by the consumer.
No fabricated managed `source_attempt` is created for it.

Primary and cleanup must both have complete, fresh verified quotes before the
regional attempt begins. At a verified USD 0.08/second primary price the planning
formula is 81/16 seconds (USD 0.405 estimate), while the conservative primary
reservation is 7 seconds (USD 0.56), plus the separately quoted cleanup image.
These are reservation estimates, not invoices or guaranteed provider rounding.
Always re-read the shared cap and available amount; no client-specific budget is
hardcoded in the consumer. The reducer reserves both operations atomically.

Cleanup remains deferred until the explicit RAW gate passes. Its execution quote
must be obtained for the actual native grid and passed through `reprice_plan`,
retaining the submitted primary quote and all prior exposure. The planning quote
cannot authorize a cleanup POST. Physical proof digests and the current state
token bind the exact gate, plan, inputs, binding and price at compare-and-mark.
Repricing, gate changes, changed historical evidence, UNKNOWN or missing reviewed
proof block a new submission. `record_gate` itself never submits. Known IDs are
recovered without another POST; markers without an ID retain uncertain exposure.
Neither released reservations nor a new run renew the regional max1 attempt.

A fresh quote for an existing held regional plan uses `reprice_plan` on that same
attempt. Retained exposure is counted once: a same-price refresh can succeed
with zero available remainder, while only a positive reservation delta consumes
additional availability. Submitted quotes and uncertain exposure remain retained.
