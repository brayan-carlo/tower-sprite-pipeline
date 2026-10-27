# Publication validation

Date: **26 October 2026** (America/Montevideo). Scope: the isolated source snapshot described in [PUBLICATION.md](PUBLICATION.md). This is a local publication check, not an independent audit or a live animation benchmark.

## Results

| Check | Result |
| --- | --- |
| Selected Python suite, 22 modules | 556 discovered: **533 passed, 23 skipped, 0 failures/errors** |
| Two Node transport suites | 150 tests: **149 passed, 1 skipped, 0 failed** |
| Python syntax | 63 script files parsed successfully |
| Node syntax | Seven JavaScript module files passed `node --check` |
| Fresh Python environment | Created a separate venv and installed Pillow 12.3.0 from PyPI |
| Fresh-environment smoke check | CLI help succeeded; 20 generic-contract tests passed |
| Scoped publication scan | No matches for checked credential formats, long literal secret assignments, or private user-directory paths |

The 20-test smoke check repeats a subset of the larger Python run; it is not added to a unique-test total. The full Python run took about 607 seconds of test-runner time; the Node run took about 25 seconds. Timing is machine-specific.

Test processes used a local outbound-fetch/socket guard and no inherited provider credentials. Network-backed providers were replaced by test fixtures. No paid FAL generation was executed for publication. Dependency installation separately downloaded Pillow from PyPI.

## Environment

- Windows.
- Python 3.12.14.
- Pillow 12.3.0.
- Node.js 24.19.0.
- External FFmpeg 7.1 for the selected video tests.

The main test run used the available Python runtime. Dependency installation and the short smoke check were also reproduced in a new venv. That is not a claim that every test ran in a newly installed operating system.

## Reproduce the selected Python suite

From the repository root, use the Python environment created in [INSTALL.md](INSTALL.md). Point FFmpeg and Node variables to actual local executables when they are not discoverable.

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Resolve-Path scripts).Path
$modules = @(
    'test_generic_sprite_contract',
    'test_generic_sprite_images',
    'test_sheet_images',
    'test_sheet_gif',
    'test_sprite_alignment',
    'test_sprite_alignment_workflow',
    'test_native_alignment',
    'test_native_components',
    'test_native_geometry',
    'test_native_motion',
    'test_native_arm_motion',
    'test_native_articulation',
    'test_native_exports',
    'test_native_sheet_images',
    'test_native_articulated_review',
    'test_sheet_review',
    'test_sheet_workflow',
    'test_sheet_task_workflow',
    'test_sprite_task_ledger',
    'test_sprite_task_bridge',
    'test_sheet_video',
    'test_endpoint_cycle_contract'
)
& $pythonExe -B -m unittest -v @modules
```

Optional runtime variables are `TOWER_SPRITE_FFMPEG_EXE`, `TOWER_SPRITE_NODE_EXE`, and `TOWER_SPRITE_NODE`. Set them to your actual executable paths; none is a credential.

The publication-specific network guard was kept outside the distributed skill. The tests themselves use fake provider transports; when adding or modifying a test, keep real credentials absent and avoid replacing fixture calls with live calls.

## Reproduce Node and the short Python smoke check

```powershell
node --test scripts/test_fal_sheet_transport.mjs scripts/test_fal_sheet_task_transport.mjs
& $pythonExe -B -m unittest discover -s scripts -p test_generic_sprite_contract.py
& $pythonExe -B scripts/generic_sprite_workflow.py --help
```

## Skipped checks

The 23 Python skips comprise:

- 18 endpoint tests requiring reviewed external provider-schema/response evidence not distributed here.
- Two tests requiring the private wolf diagnostic artwork.
- One historical-ledger replay requiring private run history.
- Two symlink cases for which the Windows test account lacked the required privilege.

The one Node skip requires a private recorded provider response.

Where applicable, the optional fixtures can be supplied through:

| Variable | Expected input |
| --- | --- |
| `TOWER_SPRITE_ENDPOINT_SCHEMA_DIR` | Directory containing the reviewed endpoint schemas expected by the tests |
| `TOWER_SPRITE_NO_MEDIA_FIXTURE` | Exact recorded response expected by the relevant assertions |
| `TOWER_SPRITE_PRIVATE_HISTORY_DIR` | Original historical task fixture directory |
| `TOWER_SPRITE_WOLF_SCALE_FIXTURE` | Original scale-drift diagnostic PNG |

These tests are tied to specific evidence. An arbitrary file with the same name is not a substitute. Do not upload private fixtures merely to make a skip disappear.

## Not verified by this publication

- The entire historical test collection. Some legacy modules require external source art at import time.
- A successful new paid generation.
- Artistic quality for all characters or actions.
- A newly installed Codex application's skill discovery and end-to-end interaction.
- Linux/macOS, Unity, Godot, or Android integration.
- Independent security review.
- A successful final golem run.

For manual acceptance, load the skill in a separate installation, use your own licensed PNG with an explicit provider budget, and inspect every frame and the full playback. Record that separately from the offline test results.
