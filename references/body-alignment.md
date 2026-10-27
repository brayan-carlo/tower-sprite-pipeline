# Fixed body X with natural Y

Use this contract for new scopes whose immutable request contains `alignment_policy: body_x_preserve_y_v1`. Policy-absent historical scopes retain their contract. A historical v3 animation may be aligned as a new derivative; its parent remains unchanged. New native candidates first require their complete native review, as described below.

## Agent workflow

1. Inspect the source and every output frame. Identify one homologous body reference, usually the projected pelvic or torso core. Record its definition in the source and coordinates in every frame. Use anatomy, not the full alpha bounding box, weapon, hands, tail, horns or moving extremities. The agent supplies this evidence; do not ask the user for coordinate files.
2. Keep anatomical uncertainty explicit where clothing, rotation or occlusion hides the point. A mathematically exact translation does not make an inferred hidden joint an exact measurement. If a reference cannot be justified, keep alignment/review pending rather than substitute silhouette centering.
3. Read the verified parent's `artifact_set_sha256`, source canvas and frame PNG hashes. Write an external UTF-8 annotations file matching the schema below.
4. Run the offline alignment command. It creates a new scope, preserving all input snapshots and the parent's review, including rejections. It translates the WHOLE image by integer X only: `dy=0`, `scale=1`. One common width may expand to avoid clipping; height, natural bounce, pixels and weapon/body proportions are preserved. Limits are 4096 per dimension and 64 million total frame pixels; exceeding them blocks rather than silently shrinking.
5. Inspect all aligned PNGs, sheet and GIF, including the last-to-first transition, on appropriate backgrounds. Verify the body reference, natural Y and motion after translation. A reference centered within 0.5px certifies geometry of that annotated point only; action, identity, scale, edges and loop still need their complete review.
6. Record a fresh full v3 review with the `body_alignment` extension below. Only a successful review permits export. Do not copy a parent's PASS or use alignment to dismiss a confirmed scale/identity/motion defect.

New v3 generation preserves common cell coordinates before alignment (`grounded=false`). Alignment cannot recover bounce already erased by an old normalization. It also cannot fix a changing body/weapon size, camera rotation, wrong action or bad anatomy.

## Annotation schema

The following illustrative JSON has two frames. Use the actual parent frame count, exact hashes and inspected coordinates; it is not a ready-to-run annotation file.

```json
{
  "schema_version": 1,
  "artifact_set_sha256": "ACTUAL_PARENT_TARGET",
  "reference": {
    "name": "projected pelvic core",
    "description": "Homologous core below the belt and between proximal thighs; exclude equipment and tail. Record occlusion uncertainty here.",
    "source_point": [50, 60]
  },
  "frames": [
    {"frame": 1, "sha256": "ACTUAL_FRAME_01_HASH", "point": [48, 63]},
    {"frame": 2, "sha256": "ACTUAL_FRAME_02_HASH", "point": [53, 57]}
  ]
}
```

Points are finite numeric `[x,y]` coordinates in each actual image, with pixel centers from zero through width/height minus one. The source point uses the parent's canonical `source.png`, which may include padding; do not use unadjusted original-image coordinates. Frames must appear in exact 1-based order, once each. Empty frames, stale hashes, bool/nonfinite/out-of-bounds coordinates and incomplete annotation sets are rejected. A justified body midpoint may lie in a transparent gap.

## Commands

Use discovered runtimes and absolute actual paths. These are command templates; paths must come from real files and tool results.

```powershell
& $pythonExe -B scripts/sprite_alignment_workflow.py align --run-manifest 'C:/actual/parent/run-manifest.json' --anchors-file 'C:/actual/annotations.json' --output-root 'C:/actual/aligned-runs'
& $pythonExe -B scripts/generic_sprite_workflow.py inspect --run-manifest 'C:/actual/derived/run-manifest.json'
& $pythonExe -B scripts/generic_sprite_workflow.py review --run-manifest 'C:/actual/derived/run-manifest.json' --review-file 'C:/actual/aligned-review.json'
& $pythonExe -B scripts/generic_sprite_workflow.py export --run-manifest 'C:/actual/derived/run-manifest.json' --output-dir 'C:/actual/centered-delivery'
```

`align` returns `READY_FOR_VISUAL_REVIEW`, a real new manifest and `delivery_allowed:false`. Its zero additional reservation does not erase the parent's spent/reserved cost or authorize a fresh paid budget. `revise` on a local derivative returns `LOCAL_DERIVATION_REVISION_REQUIRED`; use a new local derivation for corrected annotations, or return to an authorized original generation for an actual art defect.

### Managed context and interrupted setup

A managed parent supplies its existing `request.task_ref`; the child uses the same task and target, with the exact captured source attempt. `body_x_alignment` permission is required. No new task or provider reservation is created. The shared annotation limit is `--alignment-max-attempts` (default2, maximum3); use the same declared limit for that strategy. A chain of X-only derivatives of one generated output shares its source group and quota. A genuinely different generating attempt has its own alignment group without resetting generation limits or budget. Native-origin chains remain prohibited as described below.

After a rejected alignment, pass `--correction-file <actual-correction.json>` when creating the corrected derivative, with the shared-task reference's exact `defect`, `plan`, `evidence` fields. Preserve all other defects: X translation cannot make a source with anatomy/scale/motion failures eligible. A managed child requires current effective PASS and no unresolved inherited defects as well as all artifact and body checks.

If setup was interrupted only while binding/registering a child whose sealed snapshots, proof and output artifacts are already complete, repeat `align` with that returned CHILD manifest and the exact archived annotations. Use its original `--alignment-max-attempts` and correction when present; omit `--output-root` or supply its original compatible root. The command verifies the complete evidence and repeats the same bind/attempt, without another UUID, transformation or quota. This is recovery of setup, not a new derivation. Different annotations, quota, correction, destination or corrupt/missing proof must fail. Incomplete construction before the proof is durable is not automatically recoverable. Ordinary `verify`, review and export never skip ledger validation; generic `resume` is not the setup-recovery command.

## Required PASS extension

Keep all seven standard visual checks, two distinct scale anchors and required flagged-frame findings. Add this object to the full report; include one actual finding per frame, not just the example's single entry:

```json
"body_alignment": {
  "proof_sha256": "ACTUAL_DERIVATION_PROOF_SHA256",
  "reference_name": "projected pelvic core",
  "notes": "Describe observed fixed body X, preserved Y and anatomical reference confidence.",
  "frame_findings": [
    {
      "frame": 1,
      "homologous_body_point": true,
      "fixed_x": true,
      "preserved_y": true,
      "excludes_weapon_tail_box": true,
      "notes": "State the actual reference and the visual evidence for this frame."
    }
  ]
}
```

The proof digest comes from the derived manifest's immutable `derivation.proof_sha256`. The reference name must match the annotation exactly. Every finding is an object, each frame appears exactly once, all four attestations must be literal true and notes must contain actual observations. These are agent judgments, not automatic anatomical certification.

Judge transparency from correct RGBA composites over black, mid-gray and white, including native-size crops of suspected halos. Invisible RGB stored under zero alpha can look colored in some viewers; that alone is not a visible border defect. Inspect the local alpha and the composites before confirming a halo. Keep diagnostic composites separate from the unchanged transparent delivery. For non-loop attacks, assess the strike and recovery without requiring the last frame to join the first as a loop.

Inspect the PNG alpha independently of the GIF. The GIF uses binary transparency and can hide pale residue that remains visible in partially transparent PNG pixels.

Missing proof blocks with `ALIGNMENT_REQUIRED`; malformed or changed evidence blocks with `ALIGNMENT_PROOF_INVALID`; unknown policy blocks with `ALIGNMENT_POLICY_UNSUPPORTED`; incomplete body review blocks with `BODY_ALIGNMENT_REVIEW_INVALID`. A reject remains recordable before alignment. Rejected artifacts cannot become PASS unchanged.

## Integrity and compatibility

### Native candidates

New native sheet/articulated scopes seal the same policy. Complete the native geometry, support, component, equipment and motion review required by that profile FIRST. A native semantic pass returns `READY_FOR_ALIGNMENT`, with delivery false; native `export`, `adopt-export` and export recovery cannot bypass this state. The native candidate preserves its original rendering and its review. Do not reduce that review to the generic seven checks.

Use the same `align` command and annotation schema against that native manifest. It verifies the actual native construction and strict review, then creates a v3 child requiring its own full/body review and export. Reference coordinates refer to native source/output canvases; verified native body annotations may inform them, but do not substitute a weapon or full silhouette box.

Native-origin children retain a LIVE dependency on the exact native parent, attempt, geometry/construction evidence and review. A moved/missing/tampered/stale/rejected parent blocks later child approval/export with `ALIGNMENT_PROOF_INVALID`. Previously copied images are not remotely rewritten. Retain the parent workspace and evidence along with the export.

Realigning a native-origin v3 child is refused with `ALIGNMENT_NATIVE_PARENT_REQUIRED`: use the original native parent and corrected annotations. This prevents a second v3 hop from dropping the native review requirement. Legacy native parents without the policy retain their old direct export; they are not accepted as sources by this new native adapter.

### V3 and codec integrity

The derivative binds original source/frame/annotation bytes, a coherent verified parent snapshot and the transform proof into its immutable scope. Verification replays the transform and encoding and compares actual PNG/GIF/sheet/contact/QA bytes. Mutable manifest hashes alone cannot bless a changed output. V3-origin geometry remains replayable from snapshots if the mutable parent manifest later moves. Managed children also retain a live ledger dependency on their exact source attempt: a later source rejection can block their approval/export despite an intact snapshot. Historical v3 children without task context retain their snapshot-only review contract. Native children additionally require the live native workspace and its strict checks.

New alignment children seal `alignment_codec_version: 3`. The verified parent GIF is captured with the other source buffers and saved as `derivation/parent-preview.gif`; its hash is bound to the parent artifact, scope and proof. The proof adds `parent_gif_sha256` and `gif_mode: translated_parent_exact_v1`. The encoder translates each fully decoded parent GIF frame by the same integer X displacement as its PNG, without quantizing or dithering again. Per-frame local palettes preserve exact visible RGB and binary alpha, while a mandatory decoded comparison checks dimensions, frame count, individual durations and loop semantics. Consecutive identical frames retain separate descriptors and their durations.

Codec3 preserves the parent's GIF appearance, including its existing quantization and alpha limitations; it does not claim lossless GIF equivalence to the PNG or repair inherited art defects. PNG pixels remain exact independently. Inspect every decoded frame and its material colors. A parent's successful review cannot replace the child's fresh full/body review.

An absent version or explicit version1 retains historical MEDIANCUT encoding and schemas; version2 retains its MAXCOVERAGE encoding and proof. Neither requires the new GIF snapshot. Unsupported versions block; never edit an old certificate to upgrade it.

The exact codec3 path admits at most255 opaque RGB colors plus one transparent index per composed frame; the union across frames may be larger. It blocks unsupported source decoding, invalid timing/geometry/loop, palette overflow, GIF clipping or an unequal roundtrip instead of silently reducing colors. Actual GIF visible bounds are checked separately from PNG bounds. In the current Pillow runtime it also conservatively rejects a GIF whose decoder initially enters mode `L`, because that path can lose later local-table colors. Some such GIFs are representable; this is a decoder limitation, not a GIF-format impossibility. No global Pillow setting or historical palette is changed to bypass it.

Export adds an `alignment/` evidence directory only for derivatives; historical export layouts remain unchanged. Preserve this evidence with the delivery. Codec or Pillow changes that alter deterministic bytes may require an explicit migration; do not reseal old proofs to suppress the mismatch. The proof is a local integrity record, not a digital signature, an invoice, user acceptance or Unity UAT.
