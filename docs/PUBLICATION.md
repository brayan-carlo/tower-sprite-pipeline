# Publication scope and provenance

This repository was requested as a public copy for community inspection, feedback, and improvements. The owner selected the MIT license.

## Included

- `SKILL.md` and agent metadata.
- Python/JavaScript runtime scripts and regression tests.
- Workflow references.
- Synthetic pricing fixtures and legacy example manifests.
- Public setup, status, roadmap, licensing, and contribution documents.

## Excluded

- Original character art and historical generated animations.
- The private wolf scale-drift diagnostic image.
- Provider credentials, private histories, billing records, raw run directories, and publication tooling.
- Machine-specific development plans and progress logs.

Legacy example manifests are templates. Their project-relative asset paths do not mean those assets are distributed or licensed by this repository.

## Source preservation

A file allowlist was copied with before/after hashes to detect concurrent changes. The initial copy contained 87 files. The private verification record remains outside the repository.

`SKILL.md` and all production runtime source files are kept byte-equivalent to the captured version, apart from Git's configured text line-ending normalization. Five test files have narrow portability changes documented in [STATUS.md](STATUS.md).

The public source inventory is recorded in [source-snapshot.json](source-snapshot.json). Its hashes describe repository content; they are not a signature, independent audit, or promise that upstream development has stopped.

## Licensing boundaries

The [MIT license](../LICENSE) applies to this project's code and original documentation. External dependencies, service documentation/schema material, model services, and artwork retain their own terms.

The test suite includes provider-schema conformance data originating from public FAL documentation. This is interoperability evidence, not a model implementation, API credential, live price quote, or redistributed model weight. The project's MIT grant does not relicense that third-party material.

Pillow, Node.js, Python, and FFmpeg are external prerequisites; their binaries are not vendored here.

## Verification boundaries

The publication review is a local review and test run. No independent reviewer or live paid generation was used to approve this repository snapshot. See [VALIDATION.md](VALIDATION.md) for the exact checks and omissions.
