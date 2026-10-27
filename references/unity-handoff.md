# Unity Handoff Contract

## Increment A boundary

Stop at `READY_FOR_VISUAL_REVIEW`. Do not copy into the Unity project, open
Unity, create scenes/clips/controllers/workbooks, modify `.meta` files or claim
Unity/Android coverage.

## Human gate

Show Nicolas the numbered contact sheet and nearest-neighbor loop GIF. Record
exactly one decision:

- `APPROVED`: permit preparation of isolated Unity UAT.
- `APPROVED_WITH_CORRECTIONS`: return to composition/generation and repeat the
  visual gate.
- `REJECTED`: stop; do not prepare Unity.

Technical QA, paid attempts, deadlines and stakeholder authority do not replace
Nicolas's approval.

## Future approved handoff

Only after `APPROVED`, copy the candidate, previews and reports to:

`Assets/Art/GeneratedWork/Enemies/Ogro/Walk/uat/<run-id>/`

Use only an isolated future UAT scene and exclusive clip/controller. Never
modify production scenes, prefabs, controllers, scripts or
`Assets/Art/Enemies/Ogro/`. Report Unity, Android, commit and push states
separately; none is implied by a file copy or technical PASS.
