# Native action and equipment review

Use this alongside the applicable native profile. These checks supplement geometry and file validation; they do not extend a facade to unsupported actions. A pose guide describes the target, not what the generated pixels achieved.

## Plan from the source

Identify limbs by their persistent near/far attachment and equipment. For each frame, record the support limb, swing limb, body height, hand position and equipment orientation appropriate to the action. For a biped walk, a complete cycle has alternating contact, down, passing and up phases. A passing frame has one planted leg while the opposite lifted foot travels past it; the next passing phase swaps those roles. A foot at the bottom of the silhouette is not proof of weight-bearing support.

Validate the opposite contact and passing key poses before generating intermediates. If the generator repeats the same limb arrangement, correct those key poses first. Extra in-between images do not turn repeated half-cycles into alternation. A colored pose guide may help communicate the target, but inspect the actual limb attachment and occlusion rather than assuming guide adherence.

Inspect the widest source extent, including weapons and cape, before choosing a layout. The complete pose needs a clear margin inside each cell. If adjacent artwork touches, reject separation. Removing weapon/cape pixels or shrinking each frame differently does not repair the animation. Fewer poses per generation may provide useful space; preserve the exact prompts, source/guide hashes, observed outputs, composition mapping and unknown-call state when evaluating that strategy.

## Local articulated recovery, when authorized

This is an experimental recovery strategy unless the applicable facade/profile explicitly supports it. Keep its evidence separate from delivery states. Describe the result as native generation plus local articulation when source pixels, native parts and local joint transforms are combined; do not attribute the locally constructed motion to the image model.

Use a fixed ownership map for each body part and equipment item. Cutting a master into disjoint pieces may reconstruct the neutral image perfectly while opening holes after rotation. Restore hidden art and provide overlap or joint caps throughout the actual angular range. Check hips, knees, shoulders, elbows and ankles on both light and dark backgrounds in all frames. A second copy of a knee plate left in a cloth layer is an anatomical defect, even when connected-component checks pass. Preserve dated/hash-bound before-and-after evidence so an old diagnostic crop cannot be mistaken for the current frame.

When a layer has disconnected fragments, compare its ownership mask against the complete source before classifying them as background or intentional details. A mask can cut a continuous strip of cloak into floating islands. Recover the intended source connection instead of deleting foreground to silence the component report. Include the attachment above a fringe, not just its isolated tip; recheck neighboring equipment so the repair does not duplicate a shield rim or an old limb. Do not classify all components by size: a tiny raster edge detail can be retained with explicit visual evidence, while a severed part needs a construction fix.

For a grounded walk, identify each foot's visible heel/toe or sole and its projected support path. Solve the ankle from the intended contact after boot rotation, then solve the knee. A fixed ankle with changing boot pitch generally moves the sole; a lowest-alpha-pixel constraint alone establishes only a floor bound, not weight bearing, natural foot roll or correct support transfer. A three-quarter view may place the two feet on different projected ground paths; inspect each foot over its own support interval rather than forcing both to equal Y. Check planted-foot progression against body travel and inspect pitch recovery across both half-cycle boundaries.

If a native repaint repairs seams, compare the resulting limb positions and equipment angles against the pre-repaint pixels before generating intermediates. A cleaner drawing that overwrites opposite passing/contact poses fails the action check. Do not assume an edit request preserves geometry. Prefer a bounded repair whose affected parts can be rechecked; preserve the original source and the rejected candidate.

## Articulated rigid equipment

For a held rigid prop such as a sword, inspect the shoulder, elbow, wrist/hand, grip, guard and tip in every frame. The same hand stays closed around the same grip. The prop follows the arm rather than moving independently across it. Mark an occluded landmark as occluded and explain the visible evidence; do not invent a visible coordinate.

Treat the blade, guard and handle as one rigid object. Compare blade length, straightness and proportions relative to the calibrated body. An apparent length change needs a continuous visible perspective explanation. Reject stretching, bending, disconnected grip, swapped hand or changing equipment design. Use the same principle for a shield's outline, rim and center.

Track hand motion relative to the torso and tip motion relative to the grip. Natural motion may accelerate, slow or reverse; it need not have constant speed. Reject an abrupt jump, vibration or unexplained reversal. Review secondary cloth and equipment motion as a response to the body/action, not as unrelated frame-to-frame changes.

### Separate prop rigidity from arm articulation

A sword's rigidity is an equipment constraint, not a reason to rotate shoulder, upper arm, forearm and hand as one block. When the observed result has a mechanical arm swing, inspect which joints actually change and whether the action permits elbow flexion. A deliberately braced arm can be valid; do not add arbitrary oscillation to every held item or treat zero elbow variation as an automatic failure.

For an authorized articulated correction, locate the visible elbow/cuff and retain uncertainty where armor or cloth hides the anatomical center. Attach the forearm-hand-prop layer to the transformed upper-arm joint, preserving rigid prop dimensions and the grip. Use bounded joint motion tied to the action phase and inspect its response across the complete cycle. Never move the sword independently to manufacture more apparent motion. A sine curve or phase offset is a construction choice, not proof of natural movement.

Compare a small number of amplitudes against the unchanged candidate at the same canvas scale and timing. Inspect the actual transformed elbow overlap, wrist, grip and blade in all frames on light and dark backgrounds. Overlapping cuff pixels may repair coverage but can also double armor bands or darken partially transparent edges. Preserve the baseline and select a variant only from the resulting art and motion; more amplitude alone is not better quality.

`scripts/native_arm_motion.py` exposes `arm_prop_diagnostics` for the existing body/hand/grip/guard/tip annotations plus shoulder and elbow. Supply `annotation_method` (`manual_visible_landmarks` or `construction_transformed`) and `uncertainty_pixels`. It reports upper/forearm segment lengths, elbow angle and every cyclic angle change alongside rigid-prop diagnostics. Coordinates must land on visible pixels; this cannot identify a hidden joint. Keep the method and uncertainty in the artifact-bound review. Segment lengths and periodic angles verify annotations/construction, not perceived fluency, grip attachment or weight transfer; the helper never grants delivery.

## Required evidence for motion and loop checks

Before `review`, retain an artifact-bound motion report containing:

- The actual frame/artifact hashes and requested timing.
- Per-frame observed support/swing roles, visible grip attachment and prop rigidity, with specific findings.
- Every cyclic transition: 1→2, 2→3, 3→4, 4→5, 5→6, 6→7, 7→8 and 8→1. Describe hand/prop path and support transfer; explicitly inspect 7→8→1→2 and the half-cycle change.
- Playback findings at requested timing and at slower diagnostic timing, with the source and comparison shown at one recorded scale.
- Numeric measurements when useful, with their manual uncertainty and predeclared tolerances. Angles or distances computed from annotations are diagnostics, not automated recognition of natural motion.

The final `motion` and `loop` checks must agree with this evidence. A disconnected prop, wrong support order, missing alternating phase or broken transition fails even if identity, alpha, dimensions and automated tests pass. A nearby first/last pose does not establish continuity of direction or rhythm. Do not copy a prior report onto changed frames.

`scripts/native_motion.py` exposes `rigid_prop_diagnostics(frames, annotations, frame_ms)` for manual measurements. Each RGBA frame annotation has ordered `frame`, `body`, `hand`, `grip`, `guard`, `tip` pixel coordinates and observed `notes`. The rounded coordinate itself must have visible alpha. It returns pixel hashes, blade lengths, hand/grip distance, body-relative hand/grip paths and all cyclic angular differences. Angular derivatives assume the shortest arc between poses; half-turn direction is explicitly ambiguous. The helper does not recognize anatomy, certify physical contact or approve movement. An occluded landmark is a reason to use visual evidence instead of inventing a point.

When comparing providers, apply the same criteria to both and record defects in the baseline. Retaining fal does not authorize a call to fal in a native-only request. Do not lower approval standards to match a baseline defect or describe one success as a measured general reliability rate.

If landmark coordinates were transported from a source by a known affine transform, label them as source-traced model coordinates and verify their actual rendered visibility. Their constant lengths describe the construction; they are not an independent measurement of perceived fluency or anatomical correctness. Keep that distinction in the evidence and final claim.
