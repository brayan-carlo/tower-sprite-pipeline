# Sprite Run Contract

## Deterministic contract only (legacy)

Everything in this file is for the deterministic route. In particular, the
six-frame, `1536x416`, USD 0.17, `fal-ai/kling-image/o1`,
`fal-ai/nano-banana-2/edit` and legacy `3+1` attempt values below must not be
applied to a manifest whose `mode` is `sequential_reconstruction`; use
[sequential-reconstruction-contract.md](sequential-reconstruction-contract.md)
for that route.

## Manifest

Require these fields with no implicit values:

`project_root`, `source_path`, `entity`, `action`, `direction`, `frame_count`,
`frame_width`, `frame_height`, `anchor`, `max_budget_usd`, `primary_model`,
`fallback_model`.

The deterministic ogre pilot requires `ogre`, `walk`, `S`, `6`, `256`, `416`,
`bottom-center`, budget at most `0.17`, primary
`fal-ai/kling-image/o1`, and fallback `fal-ai/nano-banana-2/edit`.

## Workspace

Use a new external directory named `YYYYMMDD-HHMMSS-ogre-walk-s` containing:

```text
input/
raw/
normalized/
previews/
reports/
```

Never reuse a run directory. Resolve `project_root`, source, workspace and each
destination. Reject traversal, a workspace inside the project, or any output
outside the run directory.

For an approved visual correction, create a new run and preserve the prior run.
An existing accepted guide may be copied with its redacted receipts and request
audit, retaining the original cost and request IDs; do not submit Fal again.
Record `parent_run_id` and the correction reason in the new manifest.

The rig may define `requiredLayerCoverage` entries with `layer`, polygonal
`region` and `maxUncoveredOpaquePixels`. It may also define
`minimumLimbExcursionPixels` by limb. Both become technical QA gates when
present; malformed entries fail QA rather than being ignored.

## Increment A offline commands (deterministic-only)

```powershell
& $python scripts/sprite_pipeline.py prepare --manifest assets/pilots/ogre_walk_s.json --workspace-root $externalRoot --run-id $runId
& $python scripts/sprite_pipeline.py run-offline --manifest assets/pilots/ogre_walk_s.json --rig-manifest $rigCopy --guide-fixture $fixture --workspace-root $externalRoot --run-id $runId
& $python scripts/sprite_pipeline.py resume --run-manifest $runManifest
```

Required outputs are `reports/run_manifest.json`, `reports/qa_report.json`,
`previews/contact_sheet.png`, `previews/loop.gif`, and
`normalized/Ogre_Walk_S_6f_256x416.png`.

`run_manifest.json` records normalized input, runtime versions, model, request
IDs, attempts, estimated cost and state. Resume reads this file and performs no
automatic generation.

## Fal real transport (deterministic-only)

Real mode requires an explicitly authorized Increment B. Install
`@fal-ai/client`, `package.json`, lockfile and `node_modules` only below an
external dependency directory. A wrapper module in that directory exports
`fal` and is passed with `--client-module`; never install beside the skill or
inside Unity.

Before every possible submit, require a pricing evidence JSON no older than
15 minutes:

```json
{
  "verified_at": "2026-08-17T23:00:00Z",
  "source_url": "https://api.fal.ai/v1/models/pricing",
  "prices": [
    {"endpoint_id": "fal-ai/kling-image/o1", "unit_price": 0.028, "unit": "images", "currency": "USD"},
    {"endpoint_id": "fal-ai/nano-banana-2/edit", "unit_price": 0.08, "unit": "images", "currency": "USD"}
  ]
}
```

The numbers above are an example shape, not reusable pricing. Refresh them
from the official model pages and authenticated Platform Pricing API. Require
both exact endpoint IDs, a per-image unit reported as `image` or `images`, USD
currency, `FAL_KEY` existence and the complete
remaining worst case at or below the manifest budget. Never print, copy,
persist or pass the credential as a CLI argument.

Prepare one run first, then create a request JSON containing only `model` and
the official `input` object. Invoke at most one new submit:

```powershell
& $node scripts/fal_generate.mjs --mode real --manifest $manifest --run-manifest $runManifest --pricing $pricing --request-input $requestInput --client-module $externalClientModule
```

Every submitted attempt is appended immediately to `fal_requests` with
`attempt`, `model`, `estimated_cost_usd`, `request_id`, `submitted_at`,
`status` and `result_path`. The script queries any nonterminal request before
submitting. A submit timeout without an ID records a null ID and
`SUBMISSION_STATUS_UNKNOWN`; no automatic resend is allowed. The deterministic
limits are three Kling attempts and one Nano Banana fallback (`3+1`). Stop
submitting when
`guide_accepted` is true.

A completed result is stored under `raw/` with a redacted receipt under
`reports/`. It remains `DRAFT` until the five structural guide observations
are recorded. Finalize the same prepared run with:

A `COMPLETED` request without `result_path` remains resumable and must fetch
its existing result before any new submit. If result retrieval returns a
permanent HTTP 422 validation error, mark that request `FAILED` and persist
only its safe error type (for example `image_too_small`), never the rejected
input or data URI. Other retrieval failures remain resumable and block a new
submit.

```powershell
& $python scripts/sprite_pipeline.py run-prepared --manifest $manifest --run-manifest $runManifest --rig-manifest $rig --guide-assessment $assessment
```

`run-prepared` preserves request IDs, attempts and estimated cost. It emits no
candidate for a structural failure. The deterministic sequence remains
allowed only when all structural checks pass and local pose extraction is
explicitly unreliable.

## States

Use only `DRAFT`, `BLOCKED`, `GUIDE_REJECTED`, `TECHNICAL_REJECTED`,
`READY_FOR_VISUAL_REVIEW`, `READY_FOR_UNITY_UAT`, `APPROVED`,
`APPROVED_WITH_CORRECTIONS`, and `REJECTED`.
