# Art and Technical Contract

Select exactly one section from the manifest route. The six-frame section is
the `deterministic` contract; the `sequential_reconstruction` section is a
separate contract with its own dimensions, identity checks and budget.

## Deterministic contract (legacy)

## Canonical pilot

- Asset: ogre, walk, South.
- Style: current approved pictorial 2.5D; do not convert to pixel art.
- Source: `Assets/Art/GeneratedWork/Enemies/Ogro/Walk/source/Ogre_Walk_S_Base.png`.
- Confirmed source: RGBA `242x399`, alpha `(0,255)`, bbox `(20,11,229,383)`.
- Output: six horizontal RGBA frames of `256x416`; sheet `1536x416`.
- Anchor: `bottom-center`; expected alpha bbox bottom is 415 in every frame.
- Frame 1: exact normalized source with a one-pixel bottom margin.

## Identity and motion

Use only source pixels in the candidate. Keep head, face, chest, chains,
bracers, belt, main loincloth details, proportions, palette, lighting and
three-quarter South orientation. Permit only subtle heavy walking: alternating
arms/legs and torso bounce within `rig_manifest.json` limits.

Fal output, when a later increment enables it, is motion guidance only. Never
copy generated face, torso, skin, clothing, accessories or texture.

## Guide structural gate

Require all five observations before any deterministic fallback:

- six complete bodies;
- distinct gait phases;
- stable South orientation;
- no duplicated limbs;
- one valid row.

Any structural failure produces `GUIDE_REJECTED` and no PNG/GIF candidate.
Only when all five pass and local pose extraction is unreliable may the
confirmed deterministic rig sequence be used.

## Technical QA

Require all checks to pass:

- exact sheet and cell dimensions;
- six nonempty cells and real transparency;
- stable bottom-center and ground line;
- exact normalized frame 1;
- six distinct frame hashes;
- source-pixel identity preservation;
- zero uncovered opaque pixels in every configured `requiredLayerCoverage`
  foot region;
- minimum alpha-centroid excursion of 14 px per arm and 17 px per leg when
  `minimumLimbExcursionPixels` is configured for this pilot;
- nonzero consecutive pose differences;
- loop closing difference no stronger than the largest consecutive difference;
- workspace confinement;
- estimated cost no greater than USD 0.17 (deterministic-only).

A technical PASS means `READY_FOR_VISUAL_REVIEW`, never artistic approval.

## `sequential_reconstruction` contract

Use only with `assets/pilots/ogre_walk_s_8f.json`. This route reconstructs eight
South-facing walk frames in the declared pose order:

- eight RGBA cells of `256x416`; technical generation references are `512x832`
  and the assembled sheet is `2048x416`;
- `bottom-center` anchor, fixed root (`root_motion: false`) and alpha ground at
  row 415 after normalization;
- primary `fal-ai/qwen-image-2/edit`, fallback
  `fal-ai/nano-banana-2/edit`, with a manifest cap of USD 0.50;
- identity is checked against the master and, from frame 2 onward, the
  immediately previous `ACCEPTED` frame. A rejected frame is never a continuity
  reference and cannot advance the chain;
- reference roles are ordered `master`, optional `previous_accepted`, then
  `pose`. The target pose is the only motion instruction; preserve the master's
  face, proportions, palette, lighting, chains, bracers, belt and loincloth;
- foot regions and anatomical leg corridors must remain connected. The planted
  foot may drift by at most 2 normalized pixels; new foot islands are limited by
  the manifest QA gate.

Frame QA produces `ACCEPTED` or `REJECTED`. Sequence QA requires eight nonempty,
distinct frames and a closing difference no stronger than the largest
consecutive difference. A complete technical pass produces
`READY_FOR_VISUAL_REVIEW`; it does not grant artistic approval or authorize a
Unity copy.
