import ctypes
import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import struct
import sys
import tempfile
import unittest
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from threading import Event
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

import generic_sprite_pipeline as pipeline
import generic_sprite_contract as contract
import generic_sprite_images as images


EXPECTED_DIRECTORIES = {
    "input",
    "input/requests",
    "input/source",
    "normalized",
    "normalized/frames",
    "previews",
    "raw",
    "reports",
    "reports/frames",
}
EXPECTED_FILES = {
    "input/movement-plan.json",
    "input/source/master.png",
    "input/source/original.png",
    "reports/manifest.json",
    "reports/run_manifest.json",
}
JSON_FILES = {
    "input/movement-plan.json",
    "reports/manifest.json",
    "reports/run_manifest.json",
}
C2_V1_BASELINE = (
    Path(__file__).resolve().parents[3]
    / "work/tower-sprite-pipeline/generic-png-sprite-generator-20260902/evidence/correction-c2-v1-baseline"
)

REQUEST_KEYS = {
    "frame_index",
    "model",
    "references",
    "instructions",
    "request_fingerprint",
}

INVALID_LEDGER_TIMESTAMPS = (
    "2026-09-02 12:00:00Z",
    "2026-09-02X12:00:00Z",
    "2026-09-02t12:00:00Z",
    "2026-09-02T12:00:00+00",
    "2026-09-02T12:00:00+00:00:00",
    "2026-W36-3T12:00:00Z",
    "20260902T120000Z",
    "2026-09-02T12:00:00,123Z",
    "2026-09-02T12:00:00",
    "2026-09-02T12:00:00z",
    "2026-02-30T12:00:00Z",
    "2025-02-29T12:00:00Z",
    "1900-02-29T12:00:00Z",
    "0000-01-01T12:00:00Z",
    "2026-09-02T24:00:00Z",
    "2026-09-02T12:60:00Z",
    "2026-09-02T12:00:60Z",
    "2026-09-02T12:00:00+24:00",
    "2026-09-02T12:00:00-03:60",
)


def create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def remove_junction(path: Path) -> None:
    if os.path.lexists(path) and hasattr(path, "is_junction") and path.is_junction():
        os.rmdir(path)


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.source = self.inputs / "source.png"
        self.write_source(self.source, (20, 30, 40, 255))
        self.plan = self.inputs / "plan.json"
        self.plan.write_text(
            json.dumps(
                [
                    {"frame_index": 1, "phase": "contact", "body_motion": "left foot forward"},
                    {"frame_index": 2, "phase": "passing", "body_motion": "right foot forward"},
                ]
            ),
            encoding="utf-8",
        )
        self.pricing = self.inputs / "pricing.json"
        self.pricing.write_text(
            json.dumps(
                {
                    "model": "fal-ai/nano-banana-2/edit",
                    "source_url": "https://api.fal.ai/v1/models/pricing",
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "currency": "USD",
                    "unit": "image",
                    "base_unit_price_usd": "0.10",
                    "resolution": "0.5K",
                    "resolution_multiplier": "1",
                }
            ),
            encoding="utf-8",
        )
        self.workspace = self.root / "runs"

    def tearDown(self) -> None:
        for path in self.root.rglob("*"):
            remove_junction(path)
        self.temp.cleanup()

    @staticmethod
    def write_source(path: Path, color: tuple[int, int, int, int]) -> None:
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        image.paste(color, (2, 1, 6, 8))
        image.save(path)

    def prepare(self, run_id: str = "hero-walk-south-001", **changes: object) -> Path:
        arguments = {
            "source": self.source,
            "movement_plan_path": self.plan,
            "pricing_path": self.pricing,
            "animation": "walk",
            "direction": "south",
            "frame_count": 2,
            "frame_size": None,
            "max_budget_usd": Decimal("0.20"),
            "workspace_root": self.workspace,
            "run_id": run_id,
        }
        arguments.update(changes)
        return pipeline.prepare_run(**arguments)

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def copy_historic_v1_run(self, fixture: str) -> tuple[Path, Path]:
        source = C2_V1_BASELINE / fixture
        run_id = self.read_json(source / "reports/run_manifest.json")["artifacts"]["run_id"]
        self.workspace.mkdir(parents=True, exist_ok=True)
        run_dir = self.workspace / run_id
        shutil.copytree(source, run_dir)
        return run_dir, run_dir / "reports/run_manifest.json"

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def write_canonical_json(path: Path, value: object) -> None:
        path.write_bytes(
            (
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            ).encode("utf-8")
        )

    def authorize(self, run_id: str) -> tuple[Path, Path]:
        run_dir = self.prepare(run_id)
        run_manifest = run_dir / "reports/run_manifest.json"
        pipeline.authorize_run(run_manifest, Decimal("0.20"))
        return run_dir, run_manifest

    def accept_frame(self, run_manifest: Path, frame_index: int) -> Path:
        run_dir = run_manifest.parent.parent
        raw_path = self.seed_completed_raw(
            run_manifest,
            frame_index,
            self.generated_rgb(foreground=(20 + frame_index, 30, 40)),
        )
        report = pipeline.record_evaluation(run_manifest, frame_index, raw_path)
        self.assertEqual("PASS", report["technical_result"])
        return run_dir / f"normalized/frames/frame-{frame_index:02d}.png"

    def seed_completed_raw(
        self,
        run_manifest: Path,
        frame_index: int,
        image: Image.Image,
    ) -> Path:
        run_dir = run_manifest.parent.parent
        request_plan_path = run_dir / f"input/requests/frame-{frame_index:02d}.json"
        request_plan = pipeline.next_request_plan(run_manifest, request_plan_path)
        raw_path = run_dir / f"raw/frame-{frame_index:02d}/attempt-01.png"
        raw_path.parent.mkdir()
        image.save(raw_path)
        run = self.read_json(run_manifest)
        manifest = self.read_json(run_dir / "reports/manifest.json")
        run["requests"].append(
            {
                "frame_index": frame_index,
                "attempt": 1,
                "model": contract.GENERIC_MODEL,
                "request_fingerprint": request_plan["request_fingerprint"],
                "request_id": f"request-{frame_index}",
                "status": "COMPLETED",
                "estimated_cost_usd": manifest["provider"]["pricing"]["effective_unit_price_usd"],
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": f"raw/frame-{frame_index:02d}/attempt-01.png",
                "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            }
        )
        self.write_json(run_manifest, run)
        return raw_path

    def forge_accepted_evaluation(self, run_manifest: Path, frame_index: int) -> None:
        run_dir = run_manifest.parent.parent
        normalized = run_dir / f"normalized/frames/frame-{frame_index:02d}.png"
        report_path = run_dir / f"reports/frames/frame-{frame_index:02d}.json"
        self.write_source(normalized, (201, 31, 41, 255))
        normalized_sha256 = hashlib.sha256(normalized.read_bytes()).hexdigest()
        report = self.read_json(report_path)
        report["advisory"]["visible_pixels"] += 1
        report["provenance"]["normalized_sha256"] = normalized_sha256
        self.write_canonical_json(report_path, report)
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        run = self.read_json(run_manifest)
        frame = next(item for item in run["frames"] if item["frame_index"] == frame_index)
        frame["normalized_sha256"] = normalized_sha256
        frame["report_sha256"] = report_sha256
        claim = run["artifacts"]["evaluation"]
        claim["normalized_sha256"] = normalized_sha256
        claim["report_sha256"] = report_sha256
        self.write_json(run_manifest, run)

    def reforge_request_plan(self, run_manifest: Path, frame_index: int) -> dict:
        run_dir = run_manifest.parent.parent
        plan_path = run_dir / f"input/requests/frame-{frame_index:02d}.json"
        plan = self.read_json(plan_path)
        unsigned = copy.deepcopy(plan)
        unsigned.pop("request_fingerprint")
        run = self.read_json(run_manifest)
        manifest = self.read_json(run_dir / "reports/manifest.json")
        references = [
            {
                "role": "master",
                "path": "input/source/master.png",
                "sha256": manifest["source"]["normalized_sha256"],
            }
        ]
        if frame_index > 1:
            previous = next(
                item
                for item in run["frames"]
                if item["frame_index"] == frame_index - 1
            )
            references.append(
                {
                    "role": "previous_accepted",
                    "path": previous["normalized_path"],
                    "sha256": previous["normalized_sha256"],
                }
            )
        plan["request_fingerprint"] = contract.canonical_sha256(
            {
                "schema_version": 1,
                "request": unsigned,
                "run_id": run["artifacts"]["run_id"],
                "manifest_sha256": run["manifest_sha256"],
                "references": references,
            }
        )
        self.write_canonical_json(plan_path, plan)
        for request in run["requests"]:
            if request["frame_index"] == frame_index:
                request["request_fingerprint"] = plan["request_fingerprint"]
        self.write_json(run_manifest, run)
        return plan

    @staticmethod
    def generated_rgb(
        *,
        background: tuple[int, int, int] = (231, 17, 199),
        foreground: tuple[int, int, int] = (20, 30, 40),
        box: tuple[int, int, int, int] = (2, 1, 6, 8),
    ) -> Image.Image:
        image = Image.new("RGB", (8, 8), background)
        image.paste(foreground, box)
        return image

    def prepare_assembly_run(
        self,
        run_id: str,
        frame_count: int = 3,
    ) -> tuple[Path, Path]:
        self.plan.write_text(
            json.dumps(
                [
                    {
                        "frame_index": index,
                        "phase": f"phase-{index}",
                        "body_motion": f"pose-{index}",
                    }
                    for index in range(1, frame_count + 1)
                ]
            ),
            encoding="utf-8",
        )
        budget = Decimal("0.10") * frame_count
        run_dir = self.prepare(
            run_id,
            frame_count=frame_count,
            frame_size=(12, 18),
            max_budget_usd=budget,
        )
        run_manifest = run_dir / "reports/run_manifest.json"
        pipeline.authorize_run(run_manifest, budget)
        return run_dir, run_manifest

    def complete_assembly_run(
        self,
        run_id: str,
        frame_count: int = 3,
    ) -> tuple[Path, Path]:
        run_dir, run_manifest = self.prepare_assembly_run(run_id, frame_count)
        for frame_index in range(1, frame_count + 1):
            raw = Image.new("RGB", (12, 18), (231, 17, 199))
            raw.paste(
                (20 + frame_index * 25, 30 + frame_index * 15, 40 + frame_index * 10),
                (3, 2, 9, 18),
            )
            raw_path = self.seed_completed_raw(run_manifest, frame_index, raw)
            report = pipeline.record_evaluation(run_manifest, frame_index, raw_path)
            self.assertEqual("PASS", report["technical_result"])
        return run_dir, run_manifest

    def assert_no_final_assembly(self, run_dir: Path) -> None:
        for relative in (
            "normalized/sprite-sheet.png",
            "previews/loop.gif",
            "previews/contact-sheet.png",
            "reports/qa-report.json",
            "reports/provenance-report.json",
        ):
            self.assertFalse((run_dir / relative).exists(), relative)

    @staticmethod
    def structurally_valid_but_undecodable_png() -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
            b"IDAT", b"not-a-zlib-stream"
        ) + chunk(b"IEND", b"")

    def set_frame_record(
        self,
        run_manifest: Path,
        frame_index: int,
        status: str,
        *,
        create_png: bool = True,
    ) -> None:
        run_dir = run_manifest.parent.parent
        relative = f"normalized/frames/frame-{frame_index:02d}.png"
        frame = run_dir / relative
        digest = "0" * 64
        if create_png:
            self.write_source(frame, (20 + frame_index, 30, 40, 255))
            digest = hashlib.sha256(frame.read_bytes()).hexdigest()
        run = self.read_json(run_manifest)
        run["frames"].append(
            {
                "frame_index": frame_index,
                "status": status,
                "normalized_path": relative,
                "normalized_sha256": digest,
            }
        )
        self.write_json(run_manifest, run)

    def assert_reason(self, expected: str, callback) -> None:
        with self.assertRaises(ValueError) as caught:
            callback()
        self.assertEqual(expected, str(caught.exception))

    @staticmethod
    def snapshot_run_tree(run_dir: Path) -> dict:
        result = {}
        for path in (run_dir, *run_dir.rglob("*")):
            metadata = path.lstat()
            result[path.relative_to(run_dir).as_posix()] = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        return result

    def partials(self, run_id: str) -> list[Path]:
        if not self.workspace.exists():
            return []
        return sorted(self.workspace.glob(f".{run_id}.*.partial"))

    @unittest.skipUnless(os.name == "nt", "requires Windows guarded handles")
    def test_windows_path_guard_tolerates_transient_attribute_change_on_open(self) -> None:
        stable = pipeline._directory_identity(self.root)
        transient = type(stable)(
            device=stable.device,
            inode=stable.inode,
            file_type=stable.file_type,
            attributes=stable.attributes | 0x10000000,
        )
        calls = {"count": 0}

        def hydrated_identity(path: Path):
            calls["count"] += 1
            return transient if calls["count"] == 1 else stable

        with mock.patch.object(
            pipeline,
            "_directory_identity",
            side_effect=hydrated_identity,
        ):
            with pipeline._PathGuard(self.root, directory=True) as guard:
                guard.verify()

        self.assertGreaterEqual(calls["count"], 3)

    @unittest.skipUnless(os.name == "nt", "requires Windows guarded handles")
    def test_windows_path_guard_closes_handle_if_post_open_identity_pin_fails(self) -> None:
        stable = pipeline._directory_identity(self.root)
        handle_identity = pipeline._WindowsHandleIdentity(1, 2, 16)

        with (
            mock.patch.object(
                pipeline,
                "_directory_identity",
                side_effect=(stable, ValueError("REPARSE_PATH")),
            ),
            mock.patch.object(pipeline, "_open_windows_handle", return_value=123),
            mock.patch.object(
                pipeline,
                "_windows_handle_identity",
                return_value=handle_identity,
            ),
            mock.patch.object(pipeline, "_close_windows_handle") as close_handle,
            self.assertRaisesRegex(ValueError, "REPARSE_PATH"),
        ):
            pipeline._PathGuard(self.root, directory=True)

        close_handle.assert_called_once_with(123)

    @unittest.skipUnless(os.name == "nt", "requires Windows guarded handles")
    def test_windows_path_guard_rejects_open_time_identity_swaps_and_closes_handle(self) -> None:
        original = pipeline._Identity(1, 100, stat.S_IFDIR, 16)
        scenarios = (
            ("inode-100-to-200", pipeline._Identity(1, 200, stat.S_IFDIR, 16), pipeline._WindowsHandleIdentity(1, 200, 16)),
            ("volume", pipeline._Identity(2, 100, stat.S_IFDIR, 16), pipeline._WindowsHandleIdentity(2, 100, 16)),
            ("type", pipeline._Identity(1, 100, stat.S_IFREG, 16), pipeline._WindowsHandleIdentity(1, 100, 16)),
            ("stable-attribute", pipeline._Identity(1, 100, stat.S_IFDIR, 17), pipeline._WindowsHandleIdentity(1, 100, 17)),
            ("handle-inode", original, pipeline._WindowsHandleIdentity(1, 200, 16)),
            ("handle-volume", original, pipeline._WindowsHandleIdentity(2, 100, 16)),
            ("handle-type", original, pipeline._WindowsHandleIdentity(1, 100, 32)),
            ("handle-stable-attribute", original, pipeline._WindowsHandleIdentity(1, 100, 17)),
        )
        for label, post_open, opened in scenarios:
            with self.subTest(case=label):
                reads = iter((original,))

                def path_identity(path: Path):
                    return next(reads, post_open)

                with (
                    mock.patch.object(pipeline, "_directory_identity", side_effect=path_identity),
                    mock.patch.object(pipeline, "_open_windows_handle", return_value=123),
                    mock.patch.object(pipeline, "_windows_handle_identity", return_value=opened),
                    mock.patch.object(pipeline, "_windows_path_identity", return_value=opened),
                    mock.patch.object(pipeline, "_close_windows_handle") as close_handle,
                ):
                    try:
                        with self.assertRaisesRegex(ValueError, "^PATH_IDENTITY_CHANGED$"):
                            with pipeline._PathGuard(self.root, directory=True):
                                pass
                    finally:
                        close_handle.assert_called_once_with(123)

    def test_prepare_allows_only_transient_tree_attribute_changes(self) -> None:
        original_snapshot = pipeline._snapshot_tree

        for phase in ("seal", "published", "both"):
            with self.subTest(phase=phase):
                run_id = "transient-tree-" + phase

                def transient_attributes(root):
                    records = original_snapshot(root)
                    published = Path(root).name == run_id
                    if phase == "both" or published == (phase == "published"):
                        return tuple(
                            replace(record, identity=replace(
                                record.identity,
                                attributes=record.identity.attributes ^ 0x10000000,
                            ))
                            for record in records
                        )
                    return records

                with mock.patch.object(pipeline, "_snapshot_tree", transient_attributes):
                    self.prepare(run_id)
                self.assertTrue((self.workspace / run_id / "reports/manifest.json").is_file())

    def test_publish_tree_rejects_nontransient_record_changes(self) -> None:
        original_snapshot = pipeline._snapshot_tree
        changes = {
            "device": lambda record: replace(record, identity=replace(record.identity, device=record.identity.device + 1)),
            "inode": lambda record: replace(record, identity=replace(record.identity, inode=record.identity.inode + 1)),
            "type": lambda record: replace(record, identity=replace(record.identity, file_type=stat.S_IFDIR)),
            "attribute": lambda record: replace(record, identity=replace(record.identity, attributes=record.identity.attributes ^ 0x2)),
            "size": lambda record: replace(record, size=record.size + 1),
            "mtime": lambda record: replace(record, modified_ns=record.modified_ns + 1),
            "content": lambda record: replace(record, digest="0" * 64),
            "path": lambda record: replace(record, relative_path=record.relative_path + ".different"),
            "kind": lambda record: replace(record, kind="directory"),
            "inventory": None,
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                run_id = "tree-change-" + label

                def changed_snapshot(root):
                    records = list(original_snapshot(root))
                    if Path(root).name == run_id:
                        index = next(i for i, record in enumerate(records) if record.kind == "file")
                        if change is None:
                            records.pop(index)
                        else:
                            records[index] = change(records[index])
                    return tuple(records)

                with mock.patch.object(pipeline, "_snapshot_tree", changed_snapshot):
                    self.assert_reason("PUBLISHED_TREE_CHANGED", lambda: self.prepare(run_id))

    def test_prepare_creates_exact_directory_and_file_inventory(self) -> None:
        run_dir = self.prepare("inventory")
        directories = {
            path.relative_to(run_dir).as_posix()
            for path in run_dir.rglob("*")
            if path.is_dir()
        }
        files = {
            path.relative_to(run_dir).as_posix()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(EXPECTED_DIRECTORIES, directories)
        self.assertEqual(EXPECTED_FILES, files)

    def test_manifests_contain_only_confined_relative_file_paths(self) -> None:
        run_dir = self.prepare("relative-paths")
        manifest = json.loads((run_dir / "reports/manifest.json").read_text(encoding="utf-8"))
        lifecycle = json.loads((run_dir / "reports/run_manifest.json").read_text(encoding="utf-8"))
        path_values = (
            manifest["source"]["original_path"],
            manifest["source"]["normalized_path"],
            lifecycle["manifest_path"],
        )
        self.assertEqual(
            (
                "input/source/original.png",
                "input/source/master.png",
                "reports/manifest.json",
            ),
            path_values,
        )
        for value in path_values:
            path = PurePosixPath(value)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertNotIn("\\", value)
        serialized = json.dumps((manifest, lifecycle))
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.source), serialized)

    def test_every_json_artifact_uses_exact_canonical_pretty_encoding(self) -> None:
        run_dir = self.prepare("json-format")
        for relative in JSON_FILES:
            with self.subTest(path=relative):
                path = run_dir / relative
                parsed = json.loads(path.read_text(encoding="utf-8"))
                expected = json.dumps(
                    parsed,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ) + "\n"
                self.assertEqual(expected.encode("utf-8"), path.read_bytes())

    def test_prepare_persists_master_hash_and_exact_manifest_movement_plan(self) -> None:
        run_dir = self.prepare("prepared-provenance")
        manifest = self.read_json(run_dir / "reports/manifest.json")
        lifecycle = self.read_json(run_dir / "reports/run_manifest.json")
        master = run_dir / "input/source/master.png"
        movement = run_dir / "input/movement-plan.json"
        self.assertEqual(
            hashlib.sha256(master.read_bytes()).hexdigest(),
            manifest["source"]["normalized_sha256"],
        )
        self.assertEqual(manifest["movement_plan"], self.read_json(movement))
        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual(2, manifest["provider"]["prompt_template_version"])
        self.assertEqual(2, lifecycle["schema_version"])
        self.assertEqual(
            json.dumps(manifest["movement_plan"], ensure_ascii=False, sort_keys=True, indent=2)
            .__add__("\n")
            .encode("utf-8"),
            movement.read_bytes(),
        )

    def test_loader_accepts_only_matching_v1_or_v2_pairs_and_exact_provider_shape(self) -> None:
        historic_dir, historic_run_path = self.copy_historic_v1_run("partial")
        historic_summary = pipeline.run_authorization_summary(historic_run_path)
        self.assertEqual(
            {
                "source",
                "animation",
                "direction",
                "frame_count",
                "output_dimensions",
                "movement_plan",
                "provider",
                "model",
                "effective_unit_price_usd",
                "planned_requests",
                "worst_case_usd",
                "max_budget_usd",
            },
            set(historic_summary),
        )

        cases = (
            ("manifest-bool", "MANIFEST_INVALID", lambda manifest, run: manifest.update(schema_version=True)),
            ("manifest-unknown", "MANIFEST_INVALID", lambda manifest, run: manifest.update(schema_version=3)),
            ("provider-bool", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].update(prompt_template_version=True)),
            ("provider-wrong", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].update(prompt_template_version=1)),
            ("provider-missing", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].pop("prompt_template_version", None)),
            ("provider-extra", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].update(unexpected="blocked")),
            ("run-bool", "RUN_MANIFEST_INVALID", lambda manifest, run: run.update(schema_version=True)),
            ("run-unknown", "RUN_MANIFEST_INVALID", lambda manifest, run: run.update(schema_version=3)),
            ("mixed", "RUN_MANIFEST_INVALID", lambda manifest, run: run.update(schema_version=1)),
        )
        for name, reason, mutate in cases:
            with self.subTest(case=name):
                run_dir = self.prepare(f"version-{name}")
                manifest_path = run_dir / "reports/manifest.json"
                run_path = run_dir / "reports/run_manifest.json"
                manifest = self.read_json(manifest_path)
                run = self.read_json(run_path)
                mutate(manifest, run)
                self.write_json(manifest_path, manifest)
                self.write_json(run_path, run)
                before = (manifest_path.read_bytes(), run_path.read_bytes())
                self.assert_reason(reason, lambda run_path=run_path: pipeline.run_authorization_summary(run_path))
                self.assertEqual(before, (manifest_path.read_bytes(), run_path.read_bytes()))

    def test_authorize_freezes_exact_contract_and_is_idempotent(self) -> None:
        run_dir = self.prepare("authorize")
        run_manifest = run_dir / "reports/run_manifest.json"
        manifest_path = run_dir / "reports/manifest.json"
        before = self.read_json(manifest_path)
        expected_contract_hash = contract.authorization_contract_sha256(before)

        run = pipeline.authorize_run(run_manifest, Decimal("0.20"))
        manifest = self.read_json(manifest_path)
        first_bytes = manifest_path.read_bytes()
        self.assertEqual("AUTHORIZED", run["state"])
        self.assertEqual("AUTHORIZED", manifest["authorization"]["status"])
        self.assertEqual("0.20", manifest["authorization"]["authorized_budget_usd"])
        self.assertEqual(expected_contract_hash, manifest["authorization"]["contract_sha256"])
        self.assertNotIn("approver", manifest["authorization"])
        authorized_at = datetime.fromisoformat(
            manifest["authorization"]["authorized_at"].replace("Z", "+00:00")
        )
        self.assertEqual(timezone.utc, authorized_at.tzinfo)
        self.assertEqual(contract.canonical_sha256(manifest), run["manifest_sha256"])
        self.assertEqual("authorize", run["artifacts"]["run_id"])

        repeated = pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual(run, repeated)
        self.assertEqual(first_bytes, manifest_path.read_bytes())

    def test_authorize_requires_draft_exact_budget_and_unmutated_inputs(self) -> None:
        run_dir = self.prepare("wrong-budget")
        run_manifest = run_dir / "reports/run_manifest.json"
        before = run_manifest.read_bytes()
        self.assert_reason(
            "AUTHORIZED_BUDGET_MISMATCH",
            lambda: pipeline.authorize_run(run_manifest, Decimal("0.21")),
        )
        self.assertEqual(before, run_manifest.read_bytes())

        mutation_cases = (
            ("manifest", "reports/manifest.json"),
            ("movement", "input/movement-plan.json"),
            ("master", "input/source/master.png"),
        )
        for label, relative in mutation_cases:
            with self.subTest(case=label):
                changed_dir = self.prepare(f"mutated-{label}")
                changed_run = changed_dir / "reports/run_manifest.json"
                target = changed_dir / relative
                data = bytearray(target.read_bytes())
                data[max(0, len(data) // 2)] ^= 1
                target.write_bytes(bytes(data))
                expected = {
                    "manifest": "MANIFEST_HASH_MISMATCH",
                    "movement": "MOVEMENT_PLAN_MISMATCH",
                    "master": "MASTER_HASH_MISMATCH",
                }[label]
                self.assert_reason(
                    expected,
                    lambda changed_run=changed_run: pipeline.authorize_run(
                        changed_run,
                        Decimal("0.20"),
                    ),
                )
                self.assertEqual([], list((changed_dir / "input/requests").iterdir()))

    def test_authorize_recovers_without_rewriting_manifest_after_interrupted_second_write(self) -> None:
        run_dir = self.prepare("authorization-recovery")
        run_manifest = run_dir / "reports/run_manifest.json"
        manifest_path = run_dir / "reports/manifest.json"
        original_write = pipeline._rewrite_existing_json
        calls = {"count": 0}

        def fail_second(path: Path, value: object, expected: bytes) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("INTERRUPTED_AFTER_MANIFEST")
            original_write(path, value, expected)

        with mock.patch.object(pipeline, "_rewrite_existing_json", fail_second):
            with self.assertRaisesRegex(RuntimeError, "INTERRUPTED_AFTER_MANIFEST"):
                pipeline.authorize_run(run_manifest, Decimal("0.20"))
        authorized_bytes = manifest_path.read_bytes()
        self.assertEqual("DRAFT", self.read_json(run_manifest)["state"])
        self.assertEqual("AUTHORIZED", self.read_json(manifest_path)["authorization"]["status"])

        recovered = pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual("AUTHORIZED", recovered["state"])
        self.assertEqual(authorized_bytes, manifest_path.read_bytes())

    def test_atomic_authorization_cut_during_manifest_temp_write_preserves_old_target(self) -> None:
        run_dir = self.prepare("cut-mid-manifest")
        run_manifest = run_dir / "reports/run_manifest.json"
        manifest_path = run_dir / "reports/manifest.json"
        old_manifest = manifest_path.read_bytes()
        old_run = run_manifest.read_bytes()
        original_write = pipeline._write_temp_payload

        def cut(stream, payload: bytes, target: Path) -> None:
            if target == manifest_path:
                stream.write(payload[: len(payload) // 2])
                stream.flush()
                os.fsync(stream.fileno())
                raise RuntimeError("CUT_DURING_MANIFEST_WRITE")
            original_write(stream, payload, target)

        with mock.patch.object(pipeline, "_write_temp_payload", cut):
            with self.assertRaisesRegex(RuntimeError, "CUT_DURING_MANIFEST_WRITE"):
                pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual(old_manifest, manifest_path.read_bytes())
        self.assertEqual(old_run, run_manifest.read_bytes())

        recovered = pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual("AUTHORIZED", recovered["state"])

    def test_atomic_authorization_cut_during_run_temp_write_recovers_without_manifest_rewrite(self) -> None:
        run_dir = self.prepare("cut-mid-run")
        run_manifest = run_dir / "reports/run_manifest.json"
        manifest_path = run_dir / "reports/manifest.json"
        old_run = run_manifest.read_bytes()
        original_write = pipeline._write_temp_payload

        def cut(stream, payload: bytes, target: Path) -> None:
            if target == run_manifest:
                stream.write(payload[: len(payload) // 2])
                stream.flush()
                os.fsync(stream.fileno())
                raise RuntimeError("CUT_DURING_RUN_WRITE")
            original_write(stream, payload, target)

        with mock.patch.object(pipeline, "_write_temp_payload", cut):
            with self.assertRaisesRegex(RuntimeError, "CUT_DURING_RUN_WRITE"):
                pipeline.authorize_run(run_manifest, Decimal("0.20"))
        authorized_manifest = manifest_path.read_bytes()
        self.assertEqual(old_run, run_manifest.read_bytes())
        self.assertEqual("AUTHORIZED", self.read_json(manifest_path)["authorization"]["status"])

        recovered = pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual("AUTHORIZED", recovered["state"])
        self.assertEqual(authorized_manifest, manifest_path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "requires Windows handle replacement")
    def test_atomic_publish_hook_cannot_substitute_verified_sibling(self) -> None:
        run_dir = self.prepare("atomic-substitution")
        run_manifest = run_dir / "reports/run_manifest.json"
        manifest_path = run_dir / "reports/manifest.json"
        old_manifest = manifest_path.read_bytes()
        attack = {"write_blocked": False, "rename_blocked": False}
        original_rename = pipeline._rename_windows_handle

        def attack_before_rename(
            handle: int,
            destination: Path,
            replace_if_exists: bool = False,
        ) -> None:
            if destination != manifest_path or attack["write_blocked"]:
                return original_rename(handle, destination, replace_if_exists)
            self.assertFalse(replace_if_exists)
            candidates = list(destination.parent.glob(f".{destination.name}.*.partial"))
            self.assertEqual(1, len(candidates))
            temporary = candidates[0]
            try:
                temporary.write_bytes(b"ATTACKER")
            except OSError:
                attack["write_blocked"] = True
            try:
                os.rename(temporary, temporary.with_suffix(".substituted"))
            except OSError:
                attack["rename_blocked"] = True
            raise RuntimeError("CUT_AT_EXACT_PUBLISH_HOOK")

        with mock.patch.object(pipeline, "_rename_windows_handle", attack_before_rename):
            with self.assertRaisesRegex(RuntimeError, "CUT_AT_EXACT_PUBLISH_HOOK"):
                pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual({"write_blocked": True, "rename_blocked": True}, attack)
        self.assertEqual(old_manifest, manifest_path.read_bytes())

        recovered = pipeline.authorize_run(run_manifest, Decimal("0.20"))
        self.assertEqual("AUTHORIZED", recovered["state"])

    @unittest.skipUnless(os.name == "nt", "requires Windows handle replacement")
    def test_atomic_replace_renames_the_same_verified_temporary_handle(self) -> None:
        run_dir = self.prepare("atomic-handle-identity")
        run_manifest = run_dir / "reports/run_manifest.json"
        original_replace = pipeline._replace_existing_file
        original_rename = pipeline._rename_windows_handle
        observed = {}

        def capture_guard(temporary_guard, target: Path) -> None:
            if target.name == "manifest.json":
                observed["verified_handle"] = temporary_guard.handle
                observed["verified_identity"] = pipeline._windows_handle_identity(
                    temporary_guard.handle
                )
            original_replace(temporary_guard, target)

        def capture_rename(
            handle: int,
            destination: Path,
            replace_if_exists: bool = False,
        ) -> None:
            if destination.name == "manifest.json":
                observed["rename_handle"] = handle
                observed["replace_if_exists"] = replace_if_exists
            original_rename(handle, destination, replace_if_exists)

        with mock.patch.object(pipeline, "_replace_existing_file", capture_guard):
            with mock.patch.object(pipeline, "_rename_windows_handle", capture_rename):
                pipeline.authorize_run(run_manifest, Decimal("0.20"))

        self.assertEqual(observed["verified_handle"], observed["rename_handle"])
        self.assertFalse(observed["replace_if_exists"])
        manifest_path = run_dir / "reports/manifest.json"
        self.assertEqual(
            observed["verified_identity"],
            pipeline._windows_path_identity(manifest_path, directory=False),
        )

    @unittest.skipUnless(os.name == "nt", "requires native Windows replacement")
    def test_windows_rewrite_denies_native_replacement_before_quarantine(self) -> None:
        target = self.root / "lifecycle.json"
        target.write_bytes(b'{"version": 1}\n')
        other = self.root / "concurrent.json"
        other.write_bytes(b'{"version": 99}\n')
        original_rename = pipeline._rename_windows_handle
        attempted = False

        def replace_before_quarantine(handle, destination, replace_if_exists=False):
            nonlocal attempted
            if destination.name.endswith(".rollback"):
                attempted = True
                with pipeline._PathGuard(other, directory=False, writable_rename=True) as guard:
                    with self.assertRaises(OSError):
                        original_rename(guard.handle, target, replace_if_exists=True)
            original_rename(handle, destination, replace_if_exists)

        with mock.patch.object(pipeline, "_rename_windows_handle", replace_before_quarantine):
            pipeline._rewrite_existing_json(target, {"version": 2}, b'{"version": 1}\n')
        self.assertTrue(attempted)
        self.assertEqual({"version": 2}, self.read_json(target))
        self.assertEqual(b'{"version": 99}\n', other.read_bytes())
        self.assertEqual([], list(self.root.glob(".*.rollback")))
        self.assertEqual([], list(self.root.glob(".*.partial")))

    @unittest.skipUnless(os.name == "nt", "requires native Windows replacement")
    def test_windows_rewrite_restores_same_original_after_publication_failure(self) -> None:
        target = self.root / "lifecycle.json"
        original_bytes = b'{"version": 1}\n'
        target.write_bytes(original_bytes)
        original_inode = target.stat().st_ino
        original_rename = pipeline._rename_windows_handle
        failed = False

        def fail_publication(handle, destination, replace_if_exists=False):
            nonlocal failed
            if destination == target and not failed:
                failed = True
                self.assertFalse(destination.exists())
                raise OSError("INJECTED_PUBLICATION_FAILURE")
            original_rename(handle, destination, replace_if_exists)

        with mock.patch.object(pipeline, "_rename_windows_handle", fail_publication):
            with self.assertRaisesRegex(OSError, "INJECTED_PUBLICATION_FAILURE"):
                pipeline._rewrite_existing_json(target, {"version": 2}, original_bytes)
        self.assertEqual(original_bytes, target.read_bytes())
        self.assertEqual(original_inode, target.stat().st_ino)
        self.assertEqual([], list(self.root.glob(".*.rollback")))

    @unittest.skipUnless(os.name == "nt", "requires native Windows replacement")
    def test_windows_rewrite_rejects_replaced_temporary_before_moving_target(self) -> None:
        target = self.root / "lifecycle.json"
        original_bytes = b'{"version": 1}\n'
        target.write_bytes(original_bytes)
        original_inode = target.stat().st_ino
        original_snapshot = pipeline._file_snapshot
        replaced = None

        def replace_after_snapshot(path, payload):
            nonlocal replaced
            snapshot = original_snapshot(path, payload)
            if path.name.endswith(".partial") and replaced is None:
                other = path.with_suffix(".concurrent")
                other.write_bytes(payload)
                with pipeline._PathGuard(other, directory=False, writable_rename=True) as guard:
                    pipeline._rename_windows_handle(guard.handle, path, replace_if_exists=True)
                replaced = (path, path.stat().st_ino, payload)
            return snapshot

        with mock.patch.object(pipeline, "_file_snapshot", replace_after_snapshot):
            with self.assertRaises(ValueError):
                pipeline._rewrite_existing_json(target, {"version": 2}, original_bytes)
        self.assertIsNotNone(replaced)
        self.assertEqual(original_bytes, target.read_bytes())
        self.assertEqual(original_inode, target.stat().st_ino)
        self.assertEqual(replaced[1], replaced[0].stat().st_ino)
        self.assertEqual(replaced[2], replaced[0].read_bytes())
        self.assertEqual([], list(self.root.glob(".*.rollback")))

    @unittest.skipUnless(os.name == "nt", "requires native Windows replacement")
    def test_windows_rewrite_preserves_preplant_after_quarantine_and_retains_original(self) -> None:
        for label, planted_bytes in (("same", b'{\n  "version": 2\n}\n'), ("foreign", b'foreign')):
            with self.subTest(label=label):
                folder = self.root / label
                folder.mkdir()
                target = folder / "lifecycle.json"
                original_bytes = b'{"version": 1}\n'
                target.write_bytes(original_bytes)
                original_inode = target.stat().st_ino
                other = folder / "preplant.json"
                other.write_bytes(planted_bytes)
                planted_inode = other.stat().st_ino
                original_rename = pipeline._rename_windows_handle
                attempted = False

                def preplant_after_quarantine(handle, destination, replace_if_exists=False):
                    nonlocal attempted
                    if destination == target and not attempted:
                        attempted = True
                        self.assertFalse(target.exists())
                        with pipeline._PathGuard(other, directory=False, writable_rename=True) as guard:
                            original_rename(guard.handle, target, replace_if_exists=True)
                    original_rename(handle, destination, replace_if_exists)

                with mock.patch.object(pipeline, "_rename_windows_handle", preplant_after_quarantine):
                    with self.assertRaises(ValueError):
                        pipeline._rewrite_existing_json(target, {"version": 2}, original_bytes)
                self.assertTrue(attempted)
                self.assertEqual(planted_bytes, target.read_bytes())
                self.assertEqual(planted_inode, target.stat().st_ino)
                retained = list(folder.glob(".*.rollback"))
                self.assertEqual(1, len(retained))
                self.assertEqual(original_bytes, retained[0].read_bytes())
                self.assertEqual(original_inode, retained[0].stat().st_ino)

    def test_first_request_plan_has_exact_shape_and_canonical_fingerprint(self) -> None:
        run_dir, run_manifest = self.authorize("request-one")
        output = run_dir / "input/requests/frame-01.json"
        request = pipeline.next_request_plan(run_manifest, output)
        self.assertEqual(REQUEST_KEYS, set(request))
        self.assertEqual(1, request["frame_index"])
        self.assertEqual(contract.GENERIC_MODEL, request["model"])
        self.assertEqual(
            [{"role": "master", "path": "input/source/master.png"}],
            request["references"],
        )
        self.assertEqual(
            {
                "animation": "walk",
                "direction": "south",
                "phase": "contact",
                "body_motion": "left foot forward",
                "identity": "preserve the same character, face, proportions, clothing, equipment, palette and art style",
                "framing": "one complete character, bottom-center grounded, no crop",
                "background": "solid uniform run chroma, no shadow, no gradient",
            },
            request["instructions"],
        )
        unsigned = {key: value for key, value in request.items() if key != "request_fingerprint"}
        run = self.read_json(run_manifest)
        manifest = self.read_json(run_dir / "reports/manifest.json")
        expected = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": 1,
                    "request": unsigned,
                    "run_id": "request-one",
                    "manifest_sha256": run["manifest_sha256"],
                    "references": [
                        {
                            "role": "master",
                            "path": "input/source/master.png",
                            "sha256": manifest["source"]["normalized_sha256"],
                        }
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, request["request_fingerprint"])
        self.assertEqual(request, self.read_json(output))

    def test_request_fingerprint_changes_with_run_manifest_or_reference_identity(self) -> None:
        unsigned = {
            "frame_index": 1,
            "model": contract.GENERIC_MODEL,
            "references": [{"role": "master", "path": "input/source/master.png"}],
            "instructions": {
                "animation": "walk",
                "direction": "south",
                "phase": "contact",
                "body_motion": "left foot forward",
                "identity": "preserve the same character, face, proportions, clothing, equipment, palette and art style",
                "framing": "one complete character, bottom-center grounded, no crop",
                "background": "solid uniform run chroma, no shadow, no gradient",
            },
        }
        references = [
            {
                "role": "master",
                "path": "input/source/master.png",
                "sha256": "a" * 64,
            }
        ]
        base = pipeline._request_fingerprint(unsigned, "run-a", "b" * 64, references)
        self.assertNotEqual(
            base,
            pipeline._request_fingerprint(unsigned, "run-b", "b" * 64, references),
        )
        self.assertNotEqual(
            base,
            pipeline._request_fingerprint(unsigned, "run-a", "c" * 64, references),
        )
        changed_reference = copy.deepcopy(references)
        changed_reference[0]["sha256"] = "d" * 64
        self.assertNotEqual(
            base,
            pipeline._request_fingerprint(unsigned, "run-a", "b" * 64, changed_reference),
        )

    def test_distinct_runs_and_distinct_masters_produce_distinct_fingerprints(self) -> None:
        first_dir, first_run = self.authorize("fingerprint-run-a")
        first = pipeline.next_request_plan(
            first_run,
            first_dir / "input/requests/frame-01.json",
        )
        second_source = self.inputs / "source-two.png"
        self.write_source(second_source, (80, 90, 100, 255))
        second_dir = self.prepare("fingerprint-run-b", source=second_source)
        second_run = second_dir / "reports/run_manifest.json"
        pipeline.authorize_run(second_run, Decimal("0.20"))
        second = pipeline.next_request_plan(
            second_run,
            second_dir / "input/requests/frame-01.json",
        )
        self.assertNotEqual(first["request_fingerprint"], second["request_fingerprint"])

    def test_second_request_uses_master_then_previous_accepted_with_valid_hash(self) -> None:
        run_dir, run_manifest = self.authorize("request-two")
        self.accept_frame(run_manifest, 1)
        request = pipeline.next_request_plan(
            run_manifest,
            run_dir / "input/requests/frame-02.json",
        )
        self.assertEqual(
            [
                {"role": "master", "path": "input/source/master.png"},
                {
                    "role": "previous_accepted",
                    "path": "normalized/frames/frame-01.png",
                },
            ],
            request["references"],
        )

    def test_request_planning_blocks_missing_rejected_uncertain_and_hash_mismatch(self) -> None:
        cases = ("missing", "rejected", "uncertain", "hash-mismatch")
        for case in cases:
            with self.subTest(case=case):
                run_dir, run_manifest = self.authorize(f"blocked-{case}")
                run = self.read_json(run_manifest)
                run["next_request"] = 2
                if case != "missing":
                    frame = run_dir / "normalized/frames/frame-01.png"
                    self.write_source(frame, (25, 30, 40, 255))
                    run["frames"] = [
                        {
                            "frame_index": 1,
                            "status": "REJECTED" if case == "rejected" else "ACCEPTED",
                            "normalized_path": "normalized/frames/frame-01.png",
                            "normalized_sha256": (
                                "0" * 64
                                if case == "hash-mismatch"
                                else hashlib.sha256(frame.read_bytes()).hexdigest()
                            ),
                        }
                    ]
                if case == "uncertain":
                    run["requests"] = [{"frame_index": 1, "status": "SUBMISSION_STATUS_UNKNOWN"}]
                self.write_json(run_manifest, run)
                expected = {
                    "missing": "PREVIOUS_FRAME_MISSING",
                    "rejected": "PREVIOUS_FRAME_REJECTED",
                    "uncertain": "REMOTE_STATE_UNCERTAIN",
                    "hash-mismatch": "PREVIOUS_FRAME_HASH_MISMATCH",
                }[case]
                output = run_dir / "input/requests/frame-02.json"
                self.assert_reason(
                    expected,
                    lambda run_manifest=run_manifest, output=output: pipeline.next_request_plan(
                        run_manifest,
                        output,
                    ),
                )
                self.assertFalse(output.exists())

    def test_request_planning_requires_complete_ordered_accepted_prefix(self) -> None:
        run_dir, run_manifest = self.authorize("skipped-prefix")
        self.set_frame_record(run_manifest, 2, "ACCEPTED")
        run = self.read_json(run_manifest)
        run["next_request"] = 3
        self.write_json(run_manifest, run)
        self.assert_reason(
            "PREVIOUS_FRAME_MISSING",
            lambda: pipeline.next_request_plan(
                run_manifest,
                run_dir / "input/requests/frame-03.json",
            ),
        )

    def test_request_planning_returns_none_only_after_complete_valid_chain(self) -> None:
        incomplete_dir, incomplete_run = self.authorize("incomplete-done")
        self.accept_frame(incomplete_run, 1)
        run = self.read_json(incomplete_run)
        run["next_request"] = 3
        self.write_json(incomplete_run, run)
        self.assert_reason(
            "PREVIOUS_FRAME_MISSING",
            lambda: pipeline.next_request_plan(
                incomplete_run,
                incomplete_dir / "input/requests/frame-03.json",
            ),
        )

        complete_dir, complete_run = self.authorize("complete-done")
        self.accept_frame(complete_run, 1)
        self.accept_frame(complete_run, 2)
        self.assertIsNone(
            pipeline.next_request_plan(
                complete_run,
                complete_dir / "input/requests/frame-03.json",
            )
        )

    def test_missing_previous_png_maps_to_domain_reason(self) -> None:
        run_dir, run_manifest = self.authorize("missing-previous-png")
        self.set_frame_record(run_manifest, 1, "ACCEPTED", create_png=False)
        run = self.read_json(run_manifest)
        run["next_request"] = 2
        self.write_json(run_manifest, run)
        self.assert_reason(
            "PREVIOUS_FRAME_MISSING",
            lambda: pipeline.next_request_plan(
                run_manifest,
                run_dir / "input/requests/frame-02.json",
            ),
        )

    def test_requested_state_errors_are_reachable_with_valid_inputs(self) -> None:
        draft_dir = self.prepare("state-not-authorized")
        draft_run = draft_dir / "reports/run_manifest.json"
        self.assert_reason(
            "RUN_NOT_AUTHORIZED",
            lambda: pipeline.next_request_plan(
                draft_run,
                draft_dir / "input/requests/frame-01.json",
            ),
        )

        blocked_dir = self.prepare("state-not-draft")
        blocked_run = blocked_dir / "reports/run_manifest.json"
        blocked = self.read_json(blocked_run)
        blocked["state"] = "BLOCKED"
        self.write_json(blocked_run, blocked)
        self.assert_reason(
            "RUN_NOT_DRAFT",
            lambda: pipeline.authorize_run(blocked_run, Decimal("0.20")),
        )

        terminal_dir, terminal_run = self.authorize("state-terminal")
        terminal = self.read_json(terminal_run)
        terminal["terminal_reason"] = "STOPPED"
        self.write_json(terminal_run, terminal)
        self.assert_reason(
            "RUN_TERMINAL",
            lambda: pipeline.next_request_plan(
                terminal_run,
                terminal_dir / "input/requests/frame-01.json",
            ),
        )

        unavailable_dir, unavailable_run = self.authorize("state-unavailable")
        self.set_frame_record(unavailable_run, 1, "EVALUATION_REQUIRED")
        unavailable = self.read_json(unavailable_run)
        unavailable["next_request"] = 2
        self.write_json(unavailable_run, unavailable)
        self.assert_reason(
            "PREVIOUS_FRAME_UNAVAILABLE",
            lambda: pipeline.next_request_plan(
                unavailable_run,
                unavailable_dir / "input/requests/frame-02.json",
            ),
        )

        invalid_dir, invalid_run = self.authorize("state-invalid-authorization")
        invalid_manifest_path = invalid_dir / "reports/manifest.json"
        invalid_manifest = self.read_json(invalid_manifest_path)
        invalid_manifest["authorization"]["authorized_at"] = "not-utc"
        self.write_json(invalid_manifest_path, invalid_manifest)
        invalid_lifecycle = self.read_json(invalid_run)
        invalid_lifecycle["manifest_sha256"] = contract.canonical_sha256(invalid_manifest)
        self.write_json(invalid_run, invalid_lifecycle)
        self.assert_reason(
            "AUTHORIZATION_RECORD_INVALID",
            lambda: pipeline.next_request_plan(
                invalid_run,
                invalid_dir / "input/requests/frame-01.json",
            ),
        )

    def test_request_plan_rejects_escape_hardlink_and_mutated_existing_file(self) -> None:
        run_dir, run_manifest = self.authorize("request-paths")
        outside = run_dir / "outside.json"
        self.assert_reason(
            "REQUEST_PATH_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, outside),
        )
        self.assertFalse(outside.exists())

        traversal = run_dir / "input/requests/../requests/traversal.json"
        self.assert_reason(
            "REQUEST_PATH_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, traversal),
        )
        self.assertFalse(traversal.exists())

        ads = Path(str(run_dir / "input/requests/frame-01.json") + ":hidden")
        self.assert_reason(
            "REQUEST_PATH_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, ads),
        )
        self.assertFalse((run_dir / "input/requests/frame-01.json").exists())
        self.assertFalse(os.path.lexists(ads))

        wrong_name = run_dir / "input/requests/CON"
        self.assert_reason(
            "REQUEST_PATH_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, wrong_name),
        )
        wrong_frame = run_dir / "input/requests/frame-02.json"
        self.assert_reason(
            "REQUEST_PATH_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, wrong_frame),
        )

        hardlink = run_dir / "input/requests/frame-01.json"
        sentinel = self.root / "request-sentinel.json"
        sentinel.write_bytes(b"SENTINEL")
        os.link(sentinel, hardlink)
        self.assert_reason(
            "REQUEST_PATH_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, hardlink),
        )
        self.assertEqual(b"SENTINEL", sentinel.read_bytes())
        hardlink.unlink()

        request = pipeline.next_request_plan(run_manifest, hardlink)
        original = hardlink.read_bytes()
        mutated = bytearray(original)
        mutated[len(mutated) // 2] ^= 1
        hardlink.write_bytes(bytes(mutated))
        self.assert_reason(
            "REQUEST_PLAN_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, hardlink),
        )
        self.assertNotEqual(original, hardlink.read_bytes())
        self.assertEqual(REQUEST_KEYS, set(request))

    def test_request_plan_rejects_preexisting_symlink_or_reparse_file(self) -> None:
        run_dir, run_manifest = self.authorize("request-symlink")
        target = self.root / "request-link-target.json"
        target.write_bytes(b"SENTINEL")
        output = run_dir / "input/requests/frame-01.json"
        try:
            os.symlink(target, output)
        except OSError:
            original_lexists = pipeline.os.path.lexists
            original_is_symlink = Path.is_symlink
            output_absolute = Path(os.path.abspath(output))

            def linked_output_exists(value) -> bool:
                if Path(os.path.abspath(value)) == output_absolute:
                    return True
                return original_lexists(value)

            def linked_output(candidate: Path) -> bool:
                if Path(os.path.abspath(candidate)) == output_absolute:
                    return True
                return original_is_symlink(candidate)

            with mock.patch.object(pipeline.os.path, "lexists", linked_output_exists):
                with mock.patch.object(Path, "is_symlink", linked_output):
                    self.assert_reason(
                        "LINKED_PATH",
                        lambda: pipeline.next_request_plan(run_manifest, output),
                    )
            self.assertEqual(b"SENTINEL", target.read_bytes())
            return
        self.assert_reason(
            "LINKED_PATH",
            lambda: pipeline.next_request_plan(run_manifest, output),
        )
        self.assertEqual(b"SENTINEL", target.read_bytes())

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_request_plan_rejects_junction_requests_directory(self) -> None:
        run_dir, run_manifest = self.authorize("request-junction")
        requests = run_dir / "input/requests"
        requests.rmdir()
        external = self.root / "request-junction-target"
        external.mkdir()
        create_junction(requests, external)
        try:
            self.assert_reason(
                "REPARSE_PATH",
                lambda: pipeline.next_request_plan(
                    run_manifest,
                    requests / "frame-01.json",
                ),
            )
            self.assertEqual([], list(external.iterdir()))
        finally:
            remove_junction(requests)

    @unittest.skipUnless(os.name == "nt", "requires Windows no-delete handles")
    def test_request_directory_swap_during_create_writes_nothing_external(self) -> None:
        run_dir, run_manifest = self.authorize("request-swap")
        requests = run_dir / "input/requests"
        output = requests / "frame-01.json"
        retained = requests.with_name("requests-retained")
        external = self.root / "request-swap-external"
        external.mkdir()
        original_open = Path.open
        attack = {"attempted": False, "blocked": False, "junction": False}

        def swap_then_open(path: Path, *args, **kwargs):
            if Path(path) == output and args and args[0] == "xb" and not attack["attempted"]:
                attack["attempted"] = True
                try:
                    os.rename(requests, retained)
                    create_junction(requests, external)
                    attack["junction"] = True
                except OSError:
                    attack["blocked"] = True
            return original_open(path, *args, **kwargs)

        try:
            with mock.patch.object(Path, "open", swap_then_open):
                try:
                    pipeline.next_request_plan(run_manifest, output)
                except ValueError:
                    pass
            self.assertTrue(attack["attempted"])
            self.assertTrue(attack["blocked"] or not attack["junction"])
            self.assertEqual([], list(external.iterdir()))
        finally:
            if attack["junction"]:
                remove_junction(requests)

    def test_request_plan_repeat_is_idempotent_only_for_identical_valid_file(self) -> None:
        run_dir, run_manifest = self.authorize("request-repeat")
        output = run_dir / "input/requests/frame-01.json"
        first = pipeline.next_request_plan(run_manifest, output)
        before = output.read_bytes()
        second = pipeline.next_request_plan(run_manifest, output)
        self.assertEqual(first, second)
        self.assertEqual(before, output.read_bytes())

    def test_existing_request_plan_is_bound_to_current_reference_bytes(self) -> None:
        run_dir, run_manifest = self.authorize("request-reference-binding")
        frame = self.accept_frame(run_manifest, 1)
        output = run_dir / "input/requests/frame-02.json"
        pipeline.next_request_plan(run_manifest, output)
        before = output.read_bytes()

        self.write_source(frame, (110, 120, 130, 255))
        run = self.read_json(run_manifest)
        run["frames"][0]["normalized_sha256"] = hashlib.sha256(frame.read_bytes()).hexdigest()
        self.write_json(run_manifest, run)
        self.assert_reason(
            "EVALUATION_EVIDENCE_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, output),
        )
        self.assertEqual(before, output.read_bytes())

    def test_authorized_run_identity_must_match_directory_name(self) -> None:
        run_dir, run_manifest = self.authorize("run-identity")
        run = self.read_json(run_manifest)
        run["artifacts"]["run_id"] = "other-run"
        self.write_json(run_manifest, run)
        self.assert_reason(
            "RUN_IDENTITY_MISMATCH",
            lambda: pipeline.next_request_plan(
                run_manifest,
                run_dir / "input/requests/frame-01.json",
            ),
        )

    def test_authorized_manifest_mutation_blocks_summary_and_request(self) -> None:
        run_dir, run_manifest = self.authorize("immutable")
        manifest_path = run_dir / "reports/manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["request"]["animation"] = "attack"
        self.write_json(manifest_path, manifest)
        self.assert_reason(
            "MANIFEST_HASH_MISMATCH",
            lambda: pipeline.run_authorization_summary(run_manifest),
        )
        self.assert_reason(
            "MANIFEST_HASH_MISMATCH",
            lambda: pipeline.next_request_plan(
                run_manifest,
                run_dir / "input/requests/frame-01.json",
            ),
        )
        self.assert_reason(
            "MANIFEST_HASH_MISMATCH",
            lambda: pipeline.authorize_run(run_manifest, Decimal("0.20")),
        )

    def test_original_source_mutation_blocks_every_post_prepare_command(self) -> None:
        for command in ("summary", "authorize", "request"):
            with self.subTest(command=command):
                run_dir = self.prepare(f"original-mutated-{command}")
                run_manifest = run_dir / "reports/run_manifest.json"
                if command == "request":
                    pipeline.authorize_run(run_manifest, Decimal("0.20"))
                original = run_dir / "input/source/original.png"
                data = bytearray(original.read_bytes())
                data[len(data) // 2] ^= 1
                original.write_bytes(bytes(data))
                if command == "summary":
                    callback = lambda: pipeline.run_authorization_summary(run_manifest)
                elif command == "authorize":
                    callback = lambda: pipeline.authorize_run(run_manifest, Decimal("0.20"))
                else:
                    callback = lambda: pipeline.next_request_plan(
                        run_manifest,
                        run_dir / "input/requests/frame-01.json",
                    )
                self.assert_reason("ORIGINAL_HASH_MISMATCH", callback)

    def test_record_evaluation_accepts_persists_evidence_and_is_idempotent(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-accepted")
        raw_path = self.seed_completed_raw(
            run_manifest,
            1,
            self.generated_rgb(box=(2, 1, 6, 8)),
        )

        report = pipeline.record_evaluation(run_manifest, 1, raw_path)

        normalized = run_dir / "normalized/frames/frame-01.png"
        report_path = run_dir / "reports/frames/frame-01.json"
        self.assertEqual("PASS", report["technical_result"])
        self.assertEqual([2, 1, 6, 8], report["normalization"]["raw_alpha_bbox"])
        self.assertEqual([2, 1, 6, 8], report["normalization"]["alpha_bbox"])
        self.assertEqual(
            {
                "schema_version",
                "frame_index",
                "technical_result",
                "failures",
                "normalization",
                "advisory",
                "provenance",
            },
            set(report),
        )
        self.assertEqual(
            hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            report["provenance"]["raw_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(normalized.read_bytes()).hexdigest(),
            report["provenance"]["normalized_sha256"],
        )
        self.assertEqual(report, self.read_json(report_path))
        run = self.read_json(run_manifest)
        self.assertEqual("AUTHORIZED", run["state"])
        self.assertEqual(2, run["next_request"])
        self.assertEqual("COMPLETED", run["requests"][0]["status"])
        self.assertEqual(
            {
                "frame_index": 1,
                "status": "ACCEPTED",
                "raw_path": "raw/frame-01/attempt-01.png",
                "raw_sha256": report["provenance"]["raw_sha256"],
                "normalized_path": "normalized/frames/frame-01.png",
                "normalized_sha256": report["provenance"]["normalized_sha256"],
                "report_path": "reports/frames/frame-01.json",
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
            run["frames"][0],
        )
        request_two = pipeline.next_request_plan(
            run_manifest,
            run_dir / "input/requests/frame-02.json",
        )
        self.assertEqual("previous_accepted", request_two["references"][1]["role"])

        before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in (normalized, report_path, run_manifest)
        }
        repeated = pipeline.record_evaluation(run_manifest, 1, raw_path)
        self.assertEqual(report, repeated)
        self.assertEqual(
            before,
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in (normalized, report_path, run_manifest)
            },
        )

    def test_evaluation_rejects_wrong_frame_raw_path_and_raw_hash(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-mismatch")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        outside = self.root / "outside.png"
        outside.write_bytes(raw_path.read_bytes())
        before = run_manifest.read_bytes()

        self.assert_reason(
            "FRAME_REQUEST_MISMATCH",
            lambda: pipeline.record_evaluation(run_manifest, 2, raw_path),
        )
        self.assert_reason(
            "FRAME_REQUEST_MISMATCH",
            lambda: pipeline.record_evaluation(run_manifest, 1, outside),
        )
        raw_path.write_bytes(raw_path.read_bytes() + b"mutation")
        self.assert_reason(
            "RAW_PROVENANCE_MISMATCH",
            lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
        )
        self.assertFalse((run_dir / "normalized/frames/frame-01.png").exists())
        self.assertFalse((run_dir / "reports/frames/frame-01.json").exists())
        self.assertEqual(before, run_manifest.read_bytes())

    def test_evaluation_validates_every_required_request_field_and_cost_before_write(self) -> None:
        cases = (
            ("request-id", lambda record: record.pop("request_id")),
            ("attempt", lambda record: record.pop("attempt")),
            ("submitted-at", lambda record: record.pop("submitted_at")),
            ("estimated-cost", lambda record: record.pop("estimated_cost_usd")),
            ("raw-provenance", lambda record: record.pop("raw_sha256")),
            ("bad-cost", lambda record: record.update(estimated_cost_usd="999")),
            ("bad-timestamp", lambda record: record.update(submitted_at="not-a-time")),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                run_dir, run_manifest = self.authorize(f"evaluate-ledger-{label}")
                raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                run = self.read_json(run_manifest)
                mutate(run["requests"][0])
                self.write_json(run_manifest, run)
                before = run_manifest.read_bytes()

                self.assert_reason(
                    "RUN_MANIFEST_INVALID",
                    lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
                )
                self.assertEqual(before, run_manifest.read_bytes())
                self.assertFalse((run_dir / "normalized/frames/frame-01.png").exists())
                self.assertFalse((run_dir / "reports/frames/frame-01.json").exists())

    def test_evaluation_rejects_non_rfc3339_ledger_timestamps_without_mutation(self) -> None:
        for index, submitted_at in enumerate(INVALID_LEDGER_TIMESTAMPS):
            with self.subTest(submitted_at=submitted_at):
                run_dir, run_manifest = self.authorize(f"timestamp-current-{index}")
                raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                run = self.read_json(run_manifest)
                run["requests"][0]["submitted_at"] = submitted_at
                self.write_json(run_manifest, run)
                before = self.snapshot_run_tree(run_dir)

                try:
                    self.assert_reason(
                        "RUN_MANIFEST_INVALID",
                        lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
                    )
                finally:
                    self.assertEqual(before, self.snapshot_run_tree(run_dir))

    def test_historical_evaluation_rejects_non_rfc3339_timestamps_without_mutation(self) -> None:
        for index, submitted_at in enumerate(INVALID_LEDGER_TIMESTAMPS):
            with self.subTest(submitted_at=submitted_at):
                run_dir, run_manifest = self.authorize(f"timestamp-historical-{index}")
                raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                pipeline.record_evaluation(run_manifest, 1, raw_path)
                run = self.read_json(run_manifest)
                run["requests"][0]["submitted_at"] = submitted_at
                self.write_json(run_manifest, run)
                before = self.snapshot_run_tree(run_dir)

                for operation, reason, callback in (
                    ("recompute", "RUN_MANIFEST_INVALID", lambda: pipeline.record_evaluation(run_manifest, 1, raw_path)),
                    ("next-plan", "EVALUATION_EVIDENCE_INVALID", lambda: pipeline.next_request_plan(run_manifest, run_dir / "input/requests/frame-02.json")),
                ):
                    with self.subTest(operation=operation):
                        try:
                            self.assert_reason(reason, callback)
                        finally:
                            self.assertEqual(before, self.snapshot_run_tree(run_dir))

    def test_evaluation_accepts_node_rfc3339_calendar_fraction_and_zone_forms(self) -> None:
        valid_values = (
            "2026-09-02T12:00:00Z",
            "2026-09-02T12:00:00.1Z",
            "2026-09-02T12:00:00.123456789Z",
            "2026-09-02T09:00:00-03:00",
            "2026-09-02T12:00:00+23:59",
            "2026-09-02T12:00:00-00:00",
            "2000-02-29T23:59:59.123+00:00",
            "2024-02-29T00:00:00Z",
        )
        for index, submitted_at in enumerate(valid_values):
            with self.subTest(submitted_at=submitted_at):
                run_dir, run_manifest = self.authorize(f"timestamp-valid-{index}")
                raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                run = self.read_json(run_manifest)
                run["requests"][0]["submitted_at"] = submitted_at
                self.write_json(run_manifest, run)

                report = pipeline.record_evaluation(run_manifest, 1, raw_path)
                self.assertEqual("PASS", report["technical_result"])
                before = self.snapshot_run_tree(run_dir)
                self.assertEqual(report, pipeline.record_evaluation(run_manifest, 1, raw_path))
                self.assertEqual(before, self.snapshot_run_tree(run_dir))

    def test_idempotent_evaluation_revalidates_request_report_and_artifact_state(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-revalidate-request")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        pipeline.record_evaluation(run_manifest, 1, raw_path)
        run = self.read_json(run_manifest)
        run["requests"][0]["estimated_cost_usd"] = "999"
        self.write_json(run_manifest, run)
        before = run_manifest.read_bytes()
        self.assert_reason(
            "RUN_MANIFEST_INVALID",
            lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
        )
        self.assertEqual(before, run_manifest.read_bytes())

    def test_idempotent_evaluation_recomputes_report_and_normalized_from_raw(self) -> None:
        for label in ("advisory", "normalized"):
            with self.subTest(case=label):
                run_dir, run_manifest = self.authorize(f"evaluate-recompute-{label}")
                raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                pipeline.record_evaluation(run_manifest, 1, raw_path)
                normalized_path = run_dir / "normalized/frames/frame-01.png"
                report_path = run_dir / "reports/frames/frame-01.json"
                run = self.read_json(run_manifest)
                report = self.read_json(report_path)
                if label == "advisory":
                    report["advisory"]["opaque_pixels"] += 1
                else:
                    self.write_source(normalized_path, (201, 31, 41, 255))
                    normalized_hash = hashlib.sha256(normalized_path.read_bytes()).hexdigest()
                    run["frames"][0]["normalized_sha256"] = normalized_hash
                    run["artifacts"]["evaluation"]["normalized_sha256"] = normalized_hash
                    report["provenance"]["normalized_sha256"] = normalized_hash
                self.write_canonical_json(report_path, report)
                report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
                run["frames"][0]["report_sha256"] = report_hash
                run["artifacts"]["evaluation"]["report_sha256"] = report_hash
                self.write_json(run_manifest, run)
                before = run_manifest.read_bytes()

                self.assert_reason(
                    "EVALUATION_EVIDENCE_INVALID",
                    lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
                )
                self.assertEqual(before, run_manifest.read_bytes())

        run_dir, run_manifest = self.authorize("evaluate-revalidate-report")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        pipeline.record_evaluation(run_manifest, 1, raw_path)
        report_path = run_dir / "reports/frames/frame-01.json"
        report = self.read_json(report_path)
        report["provenance"]["raw_sha256"] = "0" * 64
        self.write_canonical_json(report_path, report)
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        run = self.read_json(run_manifest)
        run["frames"][0]["report_sha256"] = report_hash
        run["artifacts"]["evaluation"]["report_sha256"] = report_hash
        self.write_json(run_manifest, run)
        before = run_manifest.read_bytes()
        self.assert_reason(
            "EVALUATION_EVIDENCE_INVALID",
            lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
        )
        self.assertEqual(before, run_manifest.read_bytes())

        run_dir, run_manifest = self.authorize("evaluate-revalidate-artifact")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        pipeline.record_evaluation(run_manifest, 1, raw_path)
        run = self.read_json(run_manifest)
        run["artifacts"]["evaluation"]["status"] = "PENDING_PUBLICATION"
        self.write_json(run_manifest, run)
        before = run_manifest.read_bytes()
        self.assert_reason(
            "EVALUATION_EVIDENCE_INVALID",
            lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
        )
        self.assertEqual(before, run_manifest.read_bytes())

    def test_prior_accepted_evaluation_is_recomputed_before_plan_or_next_evaluation(self) -> None:
        run_dir, run_manifest = self.authorize("prior-evidence-before-plan")
        first_raw = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        pipeline.record_evaluation(run_manifest, 1, first_raw)
        self.forge_accepted_evaluation(run_manifest, 1)
        before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        second_plan = run_dir / "input/requests/frame-02.json"

        self.assert_reason(
            "EVALUATION_EVIDENCE_INVALID",
            lambda: pipeline.next_request_plan(run_manifest, second_plan),
        )
        self.assertFalse(second_plan.exists())
        self.assertEqual(
            before,
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
        )

        run_dir, run_manifest = self.authorize("prior-evidence-before-evaluate")
        first_raw = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        pipeline.record_evaluation(run_manifest, 1, first_raw)
        second_plan = pipeline.next_request_plan(
            run_manifest,
            run_dir / "input/requests/frame-02.json",
        )
        second_raw = run_dir / "raw/frame-02/attempt-01.png"
        second_raw.parent.mkdir()
        self.generated_rgb(foreground=(21, 31, 41)).save(second_raw)
        run = self.read_json(run_manifest)
        run["requests"].append(
            {
                "frame_index": 2,
                "attempt": 1,
                "model": contract.GENERIC_MODEL,
                "request_fingerprint": second_plan["request_fingerprint"],
                "request_id": "request-2",
                "status": "COMPLETED",
                "estimated_cost_usd": "0.10",
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": "raw/frame-02/attempt-01.png",
                "raw_sha256": hashlib.sha256(second_raw.read_bytes()).hexdigest(),
            }
        )
        self.write_json(run_manifest, run)
        self.forge_accepted_evaluation(run_manifest, 1)
        self.reforge_request_plan(run_manifest, 2)
        before = run_manifest.read_bytes()

        self.assert_reason(
            "EVALUATION_EVIDENCE_INVALID",
            lambda: pipeline.record_evaluation(run_manifest, 2, second_raw),
        )
        self.assertEqual(before, run_manifest.read_bytes())
        self.assertFalse((run_dir / "reports/frames/frame-02.json").exists())
        self.assertFalse((run_dir / "normalized/frames/frame-02.png").exists())

    def test_authorized_budget_tamper_with_rehashed_manifest_blocks_without_mutation(self) -> None:
        for operation in ("plan", "evaluate"):
            with self.subTest(operation=operation):
                run_dir, run_manifest = self.authorize(f"budget-rehash-{operation}")
                raw_path = None
                if operation == "evaluate":
                    raw_path = self.seed_completed_raw(
                        run_manifest,
                        1,
                        self.generated_rgb(),
                    )
                manifest_path = run_dir / "reports/manifest.json"
                manifest = self.read_json(manifest_path)
                manifest["authorization"]["authorized_budget_usd"] = "999"
                self.write_canonical_json(manifest_path, manifest)
                run = self.read_json(run_manifest)
                run["manifest_sha256"] = contract.canonical_sha256(manifest)
                self.write_json(run_manifest, run)
                before = run_manifest.read_bytes()

                if operation == "plan":
                    callback = lambda: pipeline.next_request_plan(
                        run_manifest,
                        run_dir / "input/requests/frame-01.json",
                    )
                else:
                    callback = lambda: pipeline.record_evaluation(
                        run_manifest,
                        1,
                        raw_path,
                    )
                self.assert_reason("AUTHORIZED_BUDGET_MISMATCH", callback)
                self.assertEqual(before, run_manifest.read_bytes())

    def test_clipped_frame_is_durably_rejected_and_stops_later_request(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-clipped")
        clipped = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        clipped.paste((20, 30, 40, 255), (0, 1, 5, 8))
        raw_path = self.seed_completed_raw(run_manifest, 1, clipped)

        report = pipeline.record_evaluation(run_manifest, 1, raw_path)

        self.assertEqual("FAIL", report["technical_result"])
        self.assertEqual(["FRAME_CLIPPED"], report["failures"])
        self.assertEqual([0, 1, 5, 8], report["advisory"]["alpha_bbox"])
        self.assertEqual(35, report["advisory"]["opaque_pixels"])
        self.assertEqual(35, report["advisory"]["visible_pixels"])
        self.assertEqual(
            {"status": "AVAILABLE", "space": "raw_after_background_recovery"},
            report["advisory"]["measurement"],
        )
        self.assertFalse((run_dir / "normalized/frames/frame-01.png").exists())
        report_path = run_dir / "reports/frames/frame-01.json"
        self.assertTrue(report_path.is_file())
        run = self.read_json(run_manifest)
        self.assertEqual("TECHNICAL_REJECTED", run["state"])
        self.assertEqual("FRAME_CLIPPED", run["terminal_reason"])
        self.assertEqual("COMPLETED", run["requests"][0]["status"])
        self.assertEqual("raw/frame-01/attempt-01.png", run["requests"][0]["result_path"])
        self.assertEqual(
            {
                "frame_index": 1,
                "status": "REJECTED",
                "raw_path": "raw/frame-01/attempt-01.png",
                "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "normalized_path": None,
                "normalized_sha256": None,
                "report_path": "reports/frames/frame-01.json",
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            },
            run["frames"][0],
        )
        self.assertIsNone(
            pipeline.next_request_plan(
                run_manifest,
                run_dir / "input/requests/frame-02.json",
            )
        )
        self.assertFalse((run_dir / "input/requests/frame-02.json").exists())
        self.assertEqual(report, pipeline.record_evaluation(run_manifest, 1, raw_path))

    def test_post_resize_margin_is_durably_accepted_while_bottom_contact_is_allowed(self) -> None:
        run_dir = self.prepare("evaluate-post-resize-margin", frame_size=(4, 8))
        run_manifest = run_dir / "reports/run_manifest.json"
        pipeline.authorize_run(run_manifest, Decimal("0.20"))
        raw = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        raw.paste((20, 30, 40, 255), (2, 1, 6, 8))
        raw_path = self.seed_completed_raw(run_manifest, 1, raw)

        report = pipeline.record_evaluation(run_manifest, 1, raw_path)

        self.assertEqual("PASS", report["technical_result"])
        self.assertEqual([], report["failures"])
        self.assertEqual([1, 5, 3, 8], report["advisory"]["alpha_bbox"])
        self.assertEqual(8, report["advisory"]["alpha_bbox"][3])
        self.assertTrue((run_dir / "normalized/frames/frame-01.png").is_file())
        persisted = self.read_json(run_manifest)
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertIsNone(persisted["terminal_reason"])
        self.assertEqual("COMPLETED", persisted["requests"][0]["status"])
        self.assertEqual("ACCEPTED", persisted["frames"][0]["status"])

    def test_duplicate_normalized_frame_is_rejected_without_publishing_duplicate(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-duplicate")
        first_raw = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        pipeline.record_evaluation(run_manifest, 1, first_raw)
        second_raw = self.seed_completed_raw(run_manifest, 2, self.generated_rgb())

        report = pipeline.record_evaluation(run_manifest, 2, second_raw)

        self.assertEqual("FAIL", report["technical_result"])
        self.assertEqual(["FRAME_DUPLICATE"], report["failures"])
        self.assertFalse((run_dir / "normalized/frames/frame-02.png").exists())
        run = self.read_json(run_manifest)
        self.assertEqual("TECHNICAL_REJECTED", run["state"])
        self.assertEqual("FRAME_DUPLICATE", run["terminal_reason"])
        self.assertEqual(["ACCEPTED", "REJECTED"], [item["status"] for item in run["frames"]])

    def test_evaluation_recovers_after_cut_between_report_and_lifecycle(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-recovery")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        original_rewrite = pipeline._rewrite_existing_json
        calls = {"count": 0}

        def cut_final_run(path: Path, value: object, expected: bytes) -> None:
            if Path(path) == run_manifest:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise RuntimeError("CUT_BEFORE_FINAL_LIFECYCLE")
            original_rewrite(path, value, expected)

        with mock.patch.object(pipeline, "_rewrite_existing_json", cut_final_run):
            with self.assertRaisesRegex(RuntimeError, "CUT_BEFORE_FINAL_LIFECYCLE"):
                pipeline.record_evaluation(run_manifest, 1, raw_path)

        normalized = run_dir / "normalized/frames/frame-01.png"
        report_path = run_dir / "reports/frames/frame-01.json"
        self.assertTrue(normalized.is_file())
        self.assertTrue(report_path.is_file())
        interrupted = self.read_json(run_manifest)
        self.assertEqual([], interrupted["frames"])
        self.assertEqual("PENDING_PUBLICATION", interrupted["artifacts"]["evaluation"]["status"])
        frozen = (normalized.read_bytes(), report_path.read_bytes())

        recovered = pipeline.record_evaluation(run_manifest, 1, raw_path)

        self.assertEqual("PASS", recovered["technical_result"])
        self.assertEqual(frozen, (normalized.read_bytes(), report_path.read_bytes()))
        self.assertEqual("COMPLETED", self.read_json(run_manifest)["artifacts"]["evaluation"]["status"])

    def test_evaluation_never_overwrites_preplanted_or_hardlinked_evidence(self) -> None:
        for target_kind in ("normalized", "report"):
            with self.subTest(target=target_kind):
                run_dir, run_manifest = self.authorize(f"evaluate-preplant-{target_kind}")
                raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                target = (
                    run_dir / "normalized/frames/frame-01.png"
                    if target_kind == "normalized"
                    else run_dir / "reports/frames/frame-01.json"
                )
                sentinel = self.root / f"sentinel-{target_kind}.bin"
                sentinel.write_bytes(b"SENTINEL")
                os.link(sentinel, target)

                self.assert_reason(
                    "EVALUATION_EVIDENCE_EXISTS",
                    lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
                )
                self.assertEqual(b"SENTINEL", sentinel.read_bytes())
                self.assertEqual([], self.read_json(run_manifest)["frames"])

    def test_evaluation_rejects_hardlinked_raw_without_writing_evidence(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-hardlinked-raw")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        alias = self.root / "raw-alias.png"
        os.link(raw_path, alias)

        self.assert_reason(
            "RAW_PROVENANCE_MISMATCH",
            lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
        )
        self.assertFalse((run_dir / "normalized/frames/frame-01.png").exists())
        self.assertFalse((run_dir / "reports/frames/frame-01.json").exists())

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_evaluation_rejects_raw_junction_without_mutating_run_or_external_data(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-raw-junction")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        before = run_manifest.read_bytes()
        raw_bytes = raw_path.read_bytes()
        actual_parent = self.root / "real-raw-frame"
        raw_path.parent.replace(actual_parent)
        external = self.root / "external-evaluation-raw"
        external.mkdir()
        external_raw = external / "attempt-01.png"
        external_raw.write_bytes(raw_bytes)
        create_junction(raw_path.parent, external)
        try:
            self.assert_reason(
                "WORKSPACE_CONFINEMENT_FAILED",
                lambda: pipeline.record_evaluation(run_manifest, 1, raw_path),
            )
            self.assertEqual(before, run_manifest.read_bytes())
            self.assertEqual(raw_bytes, external_raw.read_bytes())
            self.assertFalse((run_dir / "normalized/frames/frame-01.png").exists())
            self.assertFalse((run_dir / "reports/frames/frame-01.json").exists())
        finally:
            remove_junction(raw_path.parent)
            actual_parent.replace(raw_path.parent)

    @unittest.skipUnless(os.name == "nt", "requires Windows guarded publication")
    def test_evaluation_publishes_png_using_same_verified_handle_without_overwrite(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-handle")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        original_rename = pipeline._rename_windows_handle
        observed = {}

        def capture(
            handle: int,
            destination: Path,
            replace_if_exists: bool = False,
        ) -> None:
            if destination.name == "frame-01.png" and destination.parent.name == "frames":
                observed["handle"] = handle
                observed["before"] = pipeline._windows_handle_identity(handle)
                observed["replace"] = replace_if_exists
            original_rename(handle, destination, replace_if_exists)

        with mock.patch.object(pipeline, "_rename_windows_handle", capture):
            pipeline.record_evaluation(run_manifest, 1, raw_path)

        normalized = run_dir / "normalized/frames/frame-01.png"
        self.assertFalse(observed["replace"])
        self.assertEqual(observed["before"], pipeline._windows_path_identity(normalized, False))

    @unittest.skipUnless(os.name == "nt", "requires Windows no-delete handles")
    def test_evaluation_blocks_raw_mutation_and_parent_swap_during_normalization(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-guarded-source")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        original_normalize = pipeline._normalize_generated_frame_bytes
        attempts = {"raw_write_blocked": False, "parent_rename_blocked": False}

        def attack(raw_bytes: bytes, manifest: dict):
            try:
                raw_path.write_bytes(b"ATTACK")
            except OSError:
                attempts["raw_write_blocked"] = True
            try:
                os.rename(raw_path.parent, raw_path.parent.with_name("swapped"))
            except OSError:
                attempts["parent_rename_blocked"] = True
            return original_normalize(raw_bytes, manifest)

        with mock.patch.object(pipeline, "_normalize_generated_frame_bytes", attack):
            pipeline.record_evaluation(run_manifest, 1, raw_path)

        self.assertEqual(
            {"raw_write_blocked": True, "parent_rename_blocked": True},
            attempts,
        )

    def test_current_evaluation_uses_captured_raw_bytes_during_a_b_a_path_swap(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-captured-bytes")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        raw_a = raw_path.read_bytes()
        alternate = self.inputs / "alternate-current.png"
        self.generated_rgb(foreground=(90, 80, 70)).save(alternate)
        raw_b = alternate.read_bytes()
        original_guard = pipeline._PathGuard
        original_read_guard = pipeline._read_guard_bytes
        calls = {"bytes": 0, "path": 0}

        class PermissiveRawGuard:
            def __init__(self, path: Path):
                self.path = Path(path)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def verify(self) -> None:
                return None

        def guard_factory(path: Path, directory: bool, **kwargs):
            if Path(path) == raw_path and not directory:
                return PermissiveRawGuard(path)
            return original_guard(path, directory, **kwargs)

        def read_guard(guard, reason: str) -> bytes:
            if isinstance(guard, PermissiveRawGuard):
                return raw_a
            return original_read_guard(guard, reason)

        def bytes_attack(payload: bytes, manifest: dict):
            calls["bytes"] += 1
            self.assertEqual(raw_a, payload)
            raw_path.write_bytes(raw_b)
            try:
                return images._normalize_generated_frame_bytes(payload, manifest)
            finally:
                raw_path.write_bytes(raw_a)

        def path_attack(path: Path, manifest: dict):
            calls["path"] += 1
            raw_path.write_bytes(raw_b)
            try:
                return images.normalize_generated_frame(path, manifest)
            finally:
                raw_path.write_bytes(raw_a)

        with (
            mock.patch.object(pipeline, "_PathGuard", side_effect=guard_factory),
            mock.patch.object(pipeline, "_read_guard_bytes", side_effect=read_guard),
            mock.patch.object(
                pipeline,
                "_normalize_generated_frame_bytes",
                side_effect=bytes_attack,
                create=True,
            ),
            mock.patch.object(pipeline, "normalize_generated_frame", side_effect=path_attack),
        ):
            report = pipeline.record_evaluation(run_manifest, 1, raw_path)

        self.assertEqual("PASS", report["technical_result"])
        self.assertEqual({"bytes": 1, "path": 0}, calls)
        self.assertEqual(raw_a, raw_path.read_bytes())
        with Image.open(run_dir / "normalized/frames/frame-01.png") as normalized:
            self.assertEqual((20, 30, 40, 255), normalized.convert("RGBA").getpixel((2, 1)))

    def test_evaluation_recompute_uses_captured_raw_bytes_during_a_b_a_path_swap(self) -> None:
        _run_dir, run_manifest = self.authorize("recompute-captured-bytes")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        original_report = pipeline.record_evaluation(run_manifest, 1, raw_path)
        raw_a = raw_path.read_bytes()
        alternate = self.inputs / "alternate-recompute.png"
        self.generated_rgb(foreground=(90, 80, 70)).save(alternate)
        raw_b = alternate.read_bytes()
        calls = {"bytes": 0, "path": 0}

        def bytes_attack(payload: bytes, manifest: dict):
            calls["bytes"] += 1
            self.assertEqual(raw_a, payload)
            raw_path.write_bytes(raw_b)
            try:
                return images._normalize_generated_frame_bytes(payload, manifest)
            finally:
                raw_path.write_bytes(raw_a)

        def path_attack(path: Path, manifest: dict):
            calls["path"] += 1
            raw_path.write_bytes(raw_b)
            try:
                return images.normalize_generated_frame(path, manifest)
            finally:
                raw_path.write_bytes(raw_a)

        blocked_reason = None
        replay = None
        with (
            mock.patch.object(
                pipeline,
                "_normalize_generated_frame_bytes",
                side_effect=bytes_attack,
                create=True,
            ),
            mock.patch.object(pipeline, "normalize_generated_frame", side_effect=path_attack),
        ):
            try:
                replay = pipeline.record_evaluation(run_manifest, 1, raw_path)
            except ValueError as error:
                blocked_reason = str(error)

        self.assertIsNone(blocked_reason)
        self.assertEqual(original_report, replay)
        self.assertEqual({"bytes": 1, "path": 0}, calls)
        self.assertEqual(raw_a, raw_path.read_bytes())

    def test_evaluate_cli_prints_persisted_report(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-cli")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("generic_sprite_pipeline.py")),
                "evaluate",
                "--run-manifest",
                str(run_manifest),
                "--frame-index",
                "1",
                "--raw",
                str(raw_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.read_json(run_dir / "reports/frames/frame-01.json"), json.loads(result.stdout))

    def test_malformed_png_and_decompression_bomb_are_durable_sanitized_failures(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-malformed-cli")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        malformed = self.structurally_valid_but_undecodable_png()
        raw_path.write_bytes(malformed)
        run = self.read_json(run_manifest)
        run["requests"][0]["raw_sha256"] = hashlib.sha256(malformed).hexdigest()
        self.write_json(run_manifest, run)
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("generic_sprite_pipeline.py")),
                "evaluate",
                "--run-manifest",
                str(run_manifest),
                "--frame-index",
                "1",
                "--raw",
                str(raw_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertNotIn(str(self.root), result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(["FRAME_NOT_PNG"], report["failures"])
        rejected = self.read_json(run_manifest)
        self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
        self.assertEqual("FRAME_NOT_PNG", rejected["terminal_reason"])
        self.assertEqual("REJECTED", rejected["frames"][0]["status"])
        self.assertEqual("COMPLETED", rejected["requests"][0]["status"])

        for label, maximum in (("warning", 40), ("error", 1)):
            with self.subTest(bomb=label):
                run_dir, run_manifest = self.authorize(
                    f"evaluate-decompression-bomb-{label}"
                )
                raw_path = self.seed_completed_raw(
                    run_manifest,
                    1,
                    self.generated_rgb(),
                )
                with warnings.catch_warnings(record=True) as emitted:
                    warnings.simplefilter("always")
                    with mock.patch.object(Image, "MAX_IMAGE_PIXELS", maximum):
                        bomb_report = pipeline.record_evaluation(
                            run_manifest,
                            1,
                            raw_path,
                        )
                self.assertEqual([], emitted)
                self.assertEqual(["FRAME_NOT_PNG"], bomb_report["failures"])
                bomb_run = self.read_json(run_manifest)
                self.assertEqual("TECHNICAL_REJECTED", bomb_run["state"])
                self.assertEqual("FRAME_NOT_PNG", bomb_run["terminal_reason"])

    def test_malformed_ancillary_chunks_are_durable_failures_via_api_and_cli(self) -> None:
        for kind, payload in ((b"pHYs", b"\0"), (b"sRGB", b"")):
            for operation in ("api", "cli"):
                with self.subTest(chunk=kind, operation=operation):
                    run_dir, run_manifest = self.authorize(
                        f"ancillary-{kind.decode('ascii').lower()}-{operation}"
                    )
                    raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
                    valid = raw_path.read_bytes()
                    chunk = (
                        struct.pack(">I", len(payload)) + kind + payload
                        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
                    )
                    malformed = valid[:33] + chunk + valid[33:]
                    self.assertTrue(pipeline._task5_structural_png(malformed))
                    raw_path.write_bytes(malformed)
                    run = self.read_json(run_manifest)
                    run["requests"][0]["raw_sha256"] = hashlib.sha256(malformed).hexdigest()
                    self.write_json(run_manifest, run)

                    if operation == "api":
                        report = pipeline.record_evaluation(run_manifest, 1, raw_path)
                    else:
                        result = subprocess.run(
                            [sys.executable, str(Path(__file__).with_name("generic_sprite_pipeline.py")),
                             "evaluate", "--run-manifest", str(run_manifest),
                             "--frame-index", "1", "--raw", str(raw_path)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(0, result.returncode, result.stderr)
                        self.assertEqual("", result.stderr)
                        self.assertNotIn(str(self.root), result.stdout)
                        report = json.loads(result.stdout)

                    self.assertEqual(["FRAME_NOT_PNG"], report["failures"])
                    self.assertEqual(report, self.read_json(run_dir / "reports/frames/frame-01.json"))
                    rejected = self.read_json(run_manifest)
                    self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
                    self.assertEqual("FRAME_NOT_PNG", rejected["terminal_reason"])
                    self.assertEqual("REJECTED", rejected["frames"][0]["status"])
                    self.assertEqual(run["requests"], rejected["requests"])
                    self.assertFalse((run_dir / "normalized/frames/frame-01.png").exists())
                    self.assertIsNone(pipeline.next_request_plan(run_manifest, run_dir / "input/requests/frame-02.json"))
                    self.assertFalse((run_dir / "input/requests/frame-02.json").exists())
                    before = self.snapshot_run_tree(run_dir)
                    self.assertEqual(report, pipeline.record_evaluation(run_manifest, 1, raw_path))
                    self.assertEqual(before, self.snapshot_run_tree(run_dir))

    def test_cli_domain_failure_is_structured_sanitized_and_nonzero(self) -> None:
        run_dir, run_manifest = self.authorize("evaluate-cli-raw-mismatch")
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        raw_path.write_bytes(raw_path.read_bytes() + b"mutation")
        before = run_manifest.read_bytes()

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("generic_sprite_pipeline.py")),
                "evaluate",
                "--run-manifest",
                str(run_manifest),
                "--frame-index",
                "1",
                "--raw",
                str(raw_path),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertEqual(
            {"state": "BLOCKED", "reason": "RAW_PROVENANCE_MISMATCH"},
            json.loads(result.stdout),
        )
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(before, run_manifest.read_bytes())

    def test_next_request_cli_is_idempotent_and_returns_the_persisted_plan(self) -> None:
        run_dir = self.prepare("cli-next-request")
        manifest_path = run_dir / "reports/run_manifest.json"
        pipeline.authorize_run(manifest_path, Decimal("0.20"))
        destination = run_dir / "input/requests/frame-01.json"
        command = [
            sys.executable, "-B", str(Path(pipeline.__file__)),
            "next-request", "--run-manifest", str(manifest_path),
            "--output", str(destination),
        ]
        before_run = manifest_path.read_bytes()
        first = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(0, first.returncode, first.stderr)
        first_bytes = destination.read_bytes()
        first_mtime = destination.stat().st_mtime_ns
        self.assertEqual(json.loads(first.stdout), json.loads(first_bytes))
        second = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(second.stdout))
        self.assertEqual(first_bytes, destination.read_bytes())
        self.assertEqual(first_mtime, destination.stat().st_mtime_ns)
        self.assertEqual(before_run, manifest_path.read_bytes())

    def test_next_request_cli_draft_and_invalid_destination_do_not_write(self) -> None:
        script = str(Path(pipeline.__file__))
        run_dir = self.prepare("cli-next-request-draft")
        manifest_path = run_dir / "reports/run_manifest.json"
        draft_destination = run_dir / "input/requests/frame-01.json"
        before_draft = self.snapshot_run_tree(run_dir)
        self.assertFalse(draft_destination.exists())
        draft = subprocess.run(
            [sys.executable, "-B", script, "next-request", "--run-manifest", str(manifest_path), "--output", str(draft_destination)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(2, draft.returncode)
        self.assertEqual({"state": "BLOCKED", "reason": "RUN_NOT_AUTHORIZED"}, json.loads(draft.stdout))
        self.assertFalse(draft_destination.exists())
        self.assertEqual(before_draft, self.snapshot_run_tree(run_dir))

        pipeline.authorize_run(manifest_path, Decimal("0.20"))
        invalid_destination = run_dir / "input/requests/frame-02.json"
        before_invalid = self.snapshot_run_tree(run_dir)
        self.assertFalse((run_dir / "input/requests/frame-01.json").exists())
        self.assertFalse(invalid_destination.exists())
        invalid = subprocess.run(
            [sys.executable, "-B", script, "next-request", "--run-manifest", str(manifest_path), "--output", str(invalid_destination)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(2, invalid.returncode)
        self.assertEqual({"state": "BLOCKED", "reason": "REQUEST_PATH_INVALID"}, json.loads(invalid.stdout))
        self.assertFalse(invalid_destination.exists())
        self.assertFalse((run_dir / "input/requests/frame-01.json").exists())
        self.assertEqual(before_invalid, self.snapshot_run_tree(run_dir))

    def test_next_request_cli_complete_and_technical_rejection_return_null_without_request(self) -> None:
        script = str(Path(pipeline.__file__))

        complete_dir, complete_manifest = self.authorize("cli-next-request-complete")
        self.accept_frame(complete_manifest, 1)
        self.accept_frame(complete_manifest, 2)
        complete_destination = complete_dir / "input/requests/frame-03.json"
        complete = subprocess.run(
            [sys.executable, "-B", script, "next-request", "--run-manifest", str(complete_manifest), "--output", str(complete_destination)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, complete.returncode, complete.stderr)
        self.assertIsNone(json.loads(complete.stdout))
        self.assertFalse(complete_destination.exists())

        rejected_dir, rejected_manifest = self.authorize("cli-next-request-rejected")
        raw_path = self.seed_completed_raw(rejected_manifest, 1, self.generated_rgb())
        malformed = self.structurally_valid_but_undecodable_png()
        raw_path.write_bytes(malformed)
        run = self.read_json(rejected_manifest)
        run["requests"][0]["raw_sha256"] = hashlib.sha256(malformed).hexdigest()
        self.write_json(rejected_manifest, run)
        report = pipeline.record_evaluation(rejected_manifest, 1, raw_path)
        self.assertEqual("FAIL", report["technical_result"])
        rejected_destination = rejected_dir / "input/requests/frame-02.json"
        rejected = subprocess.run(
            [sys.executable, "-B", script, "next-request", "--run-manifest", str(rejected_manifest), "--output", str(rejected_destination)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, rejected.returncode, rejected.stderr)
        self.assertIsNone(json.loads(rejected.stdout))
        self.assertFalse(rejected_destination.exists())

    def test_cli_invalid_decimal_and_missing_absolute_paths_are_structured_and_sanitized(self) -> None:
        run_dir = self.prepare("cli-invalid-budget")
        run_manifest = run_dir / "reports/run_manifest.json"
        script = str(Path(__file__).with_name("generic_sprite_pipeline.py"))
        before = run_manifest.read_bytes()

        invalid_budget = subprocess.run(
            [
                sys.executable,
                script,
                "authorize",
                "--run-manifest",
                str(run_manifest),
                "--authorized-budget-usd",
                "not-a-decimal",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, invalid_budget.returncode)
        self.assertEqual("", invalid_budget.stderr)
        self.assertEqual(
            {"state": "BLOCKED", "reason": "AUTHORIZED_BUDGET_MISMATCH"},
            json.loads(invalid_budget.stdout),
        )
        self.assertNotIn("Traceback", invalid_budget.stdout + invalid_budget.stderr)
        self.assertNotIn(str(self.root), invalid_budget.stdout + invalid_budget.stderr)
        self.assertEqual(before, run_manifest.read_bytes())

        missing_manifest = self.root / "missing" / "reports" / "run_manifest.json"
        missing_raw = self.root / "missing" / "raw" / "frame-01" / "attempt-01.png"
        missing = subprocess.run(
            [
                sys.executable,
                script,
                "evaluate",
                "--run-manifest",
                str(missing_manifest),
                "--frame-index",
                "1",
                "--raw",
                str(missing_raw),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, missing.returncode)
        self.assertEqual("", missing.stderr)
        self.assertEqual(
            {"state": "BLOCKED", "reason": "RUN_MANIFEST_INVALID"},
            json.loads(missing.stdout),
        )
        self.assertNotIn("Traceback", missing.stdout + missing.stderr)
        self.assertNotIn(str(self.root), missing.stdout + missing.stderr)

        pipeline.authorize_run(run_manifest, Decimal("0.20"))
        raw_path = self.seed_completed_raw(run_manifest, 1, self.generated_rgb())
        raw_path.unlink()
        authorized_before = run_manifest.read_bytes()
        missing_raw_result = subprocess.run(
            [
                sys.executable,
                script,
                "evaluate",
                "--run-manifest",
                str(run_manifest),
                "--frame-index",
                "1",
                "--raw",
                str(raw_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, missing_raw_result.returncode)
        self.assertEqual("", missing_raw_result.stderr)
        self.assertEqual(
            {"state": "BLOCKED", "reason": "RUN_MANIFEST_INVALID"},
            json.loads(missing_raw_result.stdout),
        )
        self.assertNotIn(
            "Traceback",
            missing_raw_result.stdout + missing_raw_result.stderr,
        )
        self.assertNotIn(
            str(self.root),
            missing_raw_result.stdout + missing_raw_result.stderr,
        )
        self.assertEqual(authorized_before, run_manifest.read_bytes())

    def test_summary_cli_is_read_only_and_authorize_cli_requires_budget(self) -> None:
        run_dir = self.prepare("cli-summary")
        run_manifest = run_dir / "reports/run_manifest.json"
        before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        script = str(Path(__file__).with_name("generic_sprite_pipeline.py"))
        summary = subprocess.run(
            [sys.executable, script, "summary", "--run-manifest", str(run_manifest)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, summary.returncode, summary.stderr)
        self.assertEqual(contract.authorization_summary(self.read_json(run_dir / "reports/manifest.json")), json.loads(summary.stdout))
        after = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

        missing = subprocess.run(
            [sys.executable, script, "authorize", "--run-manifest", str(run_manifest)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, missing.returncode)
        self.assertIn("--authorized-budget-usd", missing.stderr)

        authorized = subprocess.run(
            [
                sys.executable,
                script,
                "authorize",
                "--run-manifest",
                str(run_manifest),
                "--authorized-budget-usd",
                "0.20",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, authorized.returncode, authorized.stderr)
        self.assertEqual("AUTHORIZED", json.loads(authorized.stdout)["state"])

    def test_existing_run_id_is_rejected_without_reuse_or_partial(self) -> None:
        run_dir = self.prepare("same-id")
        before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assert_reason("RUN_EXISTS", lambda: self.prepare("same-id"))
        after = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual([], self.partials("same-id"))

    def test_traversal_and_workspace_inside_source_are_rejected(self) -> None:
        self.assert_reason("RUN_ID_INVALID", lambda: self.prepare("..\\escape"))
        self.assert_reason(
            "WORKSPACE_INSIDE_SOURCE",
            lambda: self.prepare("inside", workspace_root=self.inputs / "runs"),
        )

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_workspace_junction_is_rejected_without_external_children(self) -> None:
        external = self.root / "workspace-external"
        external.mkdir()
        create_junction(self.workspace, external)
        try:
            self.assert_reason("REPARSE_PATH", lambda: self.prepare("junction-root"))
            self.assertEqual([], list(external.iterdir()))
        finally:
            remove_junction(self.workspace)

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_stage_replacement_after_safe_chain_creates_no_external_children(self) -> None:
        self.workspace.mkdir()
        external = self.root / "stage-external"
        external.mkdir()
        original_safe_chain = pipeline._safe_chain
        attack = {"attempted": False, "blocked": False, "junction": None}

        def replace_after_validation(path: Path) -> Path:
            result = original_safe_chain(path)
            candidate = Path(path)
            if (
                not attack["attempted"]
                and candidate.parent == self.workspace
                and candidate.name.startswith(".stage-race.")
                and candidate.name.endswith(".partial")
            ):
                attack["attempted"] = True
                retained = candidate.with_name(candidate.name + ".retained")
                try:
                    os.rename(candidate, retained)
                    create_junction(candidate, external)
                    attack["junction"] = candidate
                except OSError:
                    attack["blocked"] = True
            return result

        try:
            with mock.patch.object(pipeline, "_safe_chain", replace_after_validation):
                try:
                    self.prepare("stage-race")
                except ValueError:
                    pass
            self.assertTrue(attack["attempted"])
            self.assertEqual([], list(external.iterdir()), "EXTERNAL_CHILDREN must be empty")
            self.assertTrue(attack["blocked"] or not (self.workspace / "stage-race").exists())
        finally:
            if attack["junction"] is not None:
                remove_junction(attack["junction"])

    @unittest.skipUnless(os.name == "nt", "requires Windows FILE_RENAME_INFO")
    def test_windows_rename_info_buffer_is_aligned_absolute_and_no_replace(self) -> None:
        destination = self.root / "published-ógre"
        buffer, size = pipeline._build_windows_rename_info(destination)
        information = ctypes.cast(
            buffer,
            ctypes.POINTER(pipeline._FileRenameInfo),
        ).contents
        encoded = str(destination).encode("utf-16-le")

        self.assertEqual(0, ctypes.addressof(buffer) % ctypes.alignment(pipeline._FileRenameInfo))
        self.assertEqual(0, pipeline._FileRenameInfo.replace_if_exists.offset)
        self.assertEqual(
            0,
            pipeline._FileRenameInfo.root_directory.offset % ctypes.alignment(ctypes.c_void_p),
        )
        self.assertEqual(0, information.replace_if_exists)
        self.assertFalse(information.root_directory)
        self.assertEqual(len(encoded), information.file_name_length)
        self.assertEqual(
            encoded,
            ctypes.string_at(
                ctypes.addressof(buffer) + pipeline._FileRenameInfo.file_name.offset,
                information.file_name_length,
            ),
        )
        self.assertEqual(
            b"\0\0",
            ctypes.string_at(
                ctypes.addressof(buffer)
                + pipeline._FileRenameInfo.file_name.offset
                + information.file_name_length,
                2,
            ),
        )
        self.assertEqual(pipeline._FileRenameInfo.file_name.offset + len(encoded) + 2, size)
        self.assert_reason(
            "PUBLISH_DESTINATION_INVALID",
            lambda: pipeline._build_windows_rename_info(Path("relative-run")),
        )

    @unittest.skipUnless(os.name == "nt", "requires Windows handle rename")
    def test_windows_publish_renames_the_open_stage_handle_to_new_destination(self) -> None:
        self.workspace.mkdir()
        stage = self.workspace / ".direct-publish.partial"
        destination = self.workspace / "direct-publish"
        reports = stage / "reports"
        stage.mkdir()
        reports.mkdir()
        manifest = reports / "manifest.json"
        manifest.write_bytes(b'{"schema_version":1}\n')

        with pipeline._PathGuard(self.workspace, directory=True) as root_guard:
            with pipeline._PathGuard(
                stage,
                directory=True,
                writable_rename=True,
            ) as stage_guard:
                stage_guard.add_child(pipeline._PathGuard(reports, directory=True))
                stage_guard.add_child(pipeline._PathGuard(manifest, directory=False))
                before = pipeline._windows_handle_identity(stage_guard.handle)

                pipeline._publish_stage(stage_guard, root_guard, destination)

                self.assertIsNotNone(stage_guard.handle)
                self.assertEqual(before, pipeline._windows_handle_identity(stage_guard.handle))
                self.assertEqual(before, pipeline._windows_path_identity(destination, directory=True))

        self.assertFalse(stage.exists())
        self.assertEqual(b'{"schema_version":1}\n', (destination / "reports/manifest.json").read_bytes())

    @unittest.skipUnless(os.name == "nt", "requires Windows handle rename")
    def test_stage_release_hook_cannot_replace_name_before_handle_publish(self) -> None:
        original_release = pipeline._PathGuard.release_handle
        original_publish = pipeline._publish_stage
        attack = {"attempted": False}
        observed = {}

        def release_then_replace(guard) -> None:
            stage_path = guard.path
            is_unpublished_stage = (
                guard.directory
                and stage_path.parent == self.workspace
                and stage_path.name.startswith(".release-window.")
                and stage_path.name.endswith(".partial")
            )
            original_release(guard)
            if is_unpublished_stage:
                attack["attempted"] = True
                retained = stage_path.with_name(stage_path.name + ".retained")
                os.rename(stage_path, retained)
                stage_path.mkdir()
                (stage_path / "attacker-marker.txt").write_text("ATTACK", encoding="utf-8")

        def capture_identity(stage_guard, root_guard, destination: Path) -> None:
            observed["before"] = pipeline._windows_handle_identity(stage_guard.handle)
            original_publish(stage_guard, root_guard, destination)
            observed["after"] = pipeline._windows_handle_identity(stage_guard.handle)

        with mock.patch.object(pipeline._PathGuard, "release_handle", release_then_replace):
            with mock.patch.object(pipeline, "_publish_stage", capture_identity):
                run_dir = self.prepare("release-window")

        self.assertFalse(attack["attempted"])
        self.assertEqual(observed["before"], observed["after"])
        self.assertEqual(
            observed["before"],
            pipeline._windows_path_identity(run_dir, directory=True),
        )
        self.assertFalse((run_dir / "attacker-marker.txt").exists())

    def test_safe_chain_rejects_mocked_dangling_symlink_and_reparse(self) -> None:
        candidate = self.root / "missing-link"
        with mock.patch.object(pipeline.os.path, "lexists", return_value=True):
            with mock.patch.object(Path, "is_symlink", return_value=True):
                self.assert_reason("LINKED_PATH", lambda: pipeline._safe_chain(candidate))
        fake_stat = mock.Mock(st_file_attributes=0x400)
        with mock.patch.object(pipeline.os.path, "lexists", return_value=True):
            with mock.patch.object(Path, "is_symlink", return_value=False):
                with mock.patch.object(Path, "is_junction", return_value=False):
                    with mock.patch.object(pipeline.os, "lstat", return_value=fake_stat):
                        self.assert_reason("REPARSE_PATH", lambda: pipeline._safe_chain(candidate))

    def test_hardlink_sentinels_are_preserved_for_every_artifact_target(self) -> None:
        targets = (
            "input/source/original.png",
            "input/source/master.png",
            "input/movement-plan.json",
            "reports/manifest.json",
            "reports/run_manifest.json",
        )
        for index, target in enumerate(targets):
            with self.subTest(target=target):
                run_id = f"hardlink-{index}"
                sentinel = self.root / f"sentinel-{index}.bin"
                sentinel.write_bytes(b"SENTINEL")
                original_write = pipeline._exclusive_bytes
                injected = {"done": False}

                def inject_hardlink(path: Path, data: bytes, *args, **kwargs) -> None:
                    stage_value = next(
                        value
                        for value in args
                        if Path(getattr(value, "path", value)).name.endswith(".partial")
                    )
                    stage = Path(getattr(stage_value, "path", stage_value))
                    relative = Path(path).relative_to(stage).as_posix()
                    if relative == target and not injected["done"]:
                        os.link(sentinel, path)
                        injected["done"] = True
                    original_write(path, data, *args, **kwargs)

                with mock.patch.object(pipeline, "_exclusive_bytes", inject_hardlink):
                    with self.assertRaises(FileExistsError):
                        self.prepare(run_id)
                self.assertTrue(injected["done"])
                self.assertEqual(b"SENTINEL", sentinel.read_bytes())
                self.assertFalse((self.workspace / run_id).exists())
                self.assertEqual(1, len(self.partials(run_id)))

    def test_source_mutation_during_copy_is_detected_and_partial_is_retained(self) -> None:
        original_read = pipeline._read_stable_bytes
        changed = {"attempted": False, "done": False}

        def mutate_after_read(path: Path, *args, **kwargs) -> bytes:
            data = original_read(path, *args, **kwargs)
            if Path(path) == self.source.resolve() and not changed["done"]:
                self.write_source(self.source, (90, 80, 70, 255))
                changed["done"] = True
            return data

        with mock.patch.object(pipeline, "_read_stable_bytes", mutate_after_read):
            self.assert_reason("SOURCE_MUTATED_DURING_COPY", lambda: self.prepare("mutate-copy"))
        self.assertFalse((self.workspace / "mutate-copy").exists())
        self.assertEqual(1, len(self.partials("mutate-copy")))

    def test_source_mutation_after_copy_is_detected_and_partial_is_retained(self) -> None:
        original_normalize = pipeline.normalize_source

        def mutate_after_normalize(path: Path, size):
            result = original_normalize(path, size)
            self.write_source(self.source, (70, 60, 50, 255))
            return result

        with mock.patch.object(pipeline, "normalize_source", mutate_after_normalize):
            self.assert_reason("SOURCE_MUTATED_AFTER_COPY", lambda: self.prepare("mutate-after"))
        self.assertFalse((self.workspace / "mutate-after").exists())
        self.assertEqual(1, len(self.partials("mutate-after")))

    def test_final_destination_race_preserves_marker_and_partial(self) -> None:
        original_publish = pipeline._publish_stage

        def create_destination_then_publish(stage_guard, root_guard, destination: Path) -> None:
            destination.mkdir()
            (destination / "marker.txt").write_text("RACE", encoding="utf-8")
            original_publish(stage_guard, root_guard, destination)

        with mock.patch.object(pipeline, "_publish_stage", create_destination_then_publish):
            self.assert_reason("RUN_EXISTS", lambda: self.prepare("publish-race"))
        marker = self.workspace / "publish-race" / "marker.txt"
        self.assertEqual("RACE", marker.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.partials("publish-race")))

    @unittest.skipUnless(os.name == "nt", "requires Windows handle rename")
    def test_windows_destination_race_at_handle_rename_preserves_marker(self) -> None:
        original_rename = pipeline._rename_windows_handle

        def create_destination_then_rename(handle: int, destination: Path) -> None:
            destination.mkdir()
            (destination / "marker.txt").write_text("RACE", encoding="utf-8")
            original_rename(handle, destination)

        with mock.patch.object(
            pipeline,
            "_rename_windows_handle",
            create_destination_then_rename,
        ):
            self.assert_reason("RUN_EXISTS", lambda: self.prepare("handle-race"))
        marker = self.workspace / "handle-race" / "marker.txt"
        self.assertEqual("RACE", marker.read_text(encoding="utf-8"))
        self.assertEqual(1, len(self.partials("handle-race")))

    @unittest.skipUnless(os.name == "nt", "requires Windows handle rename")
    def test_windows_publish_detects_manifest_tampering_after_handle_rename(self) -> None:
        original_rename = pipeline._rename_windows_handle

        def rename_then_tamper(handle: int, destination: Path) -> None:
            original_rename(handle, destination)
            (destination / "reports/manifest.json").write_bytes(b"{}\n")

        with mock.patch.object(pipeline, "_rename_windows_handle", rename_then_tamper):
            self.assert_reason(
                "PUBLISHED_TREE_CHANGED",
                lambda: self.prepare("post-rename-tamper"),
            )
        self.assertTrue((self.workspace / "post-rename-tamper").is_dir())

    def test_failure_keeps_run_absent_and_retains_partial(self) -> None:
        with mock.patch.object(pipeline, "normalize_source", side_effect=RuntimeError("STOP")):
            with self.assertRaisesRegex(RuntimeError, "STOP"):
                self.prepare("retained")
        self.assertFalse((self.workspace / "retained").exists())
        partials = self.partials("retained")
        self.assertEqual(1, len(partials))
        self.assertTrue((partials[0] / "input/source/original.png").is_file())

    def cli_arguments(self, run_id: str) -> list[str]:
        return [
            sys.executable,
            str(Path(__file__).with_name("generic_sprite_pipeline.py")),
            "prepare",
            "--source",
            str(self.source),
            "--movement-plan",
            str(self.plan),
            "--pricing",
            str(self.pricing),
            "--animation",
            "walk",
            "--direction",
            "south",
            "--frame-count",
            "2",
            "--max-budget-usd",
            "0.20",
            "--workspace-root",
            str(self.workspace),
            "--run-id",
            run_id,
        ]

    def test_assemble_outputs_publishes_exact_ordered_deterministic_artifacts(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-success")
        frame_paths = tuple(
            run_dir / f"normalized/frames/frame-{index:02d}.png"
            for index in range(1, 4)
        )
        before = tuple(path.read_bytes() for path in frame_paths)
        ledger_fingerprints = [
            item["request_fingerprint"]
            for item in self.read_json(run_manifest)["requests"]
        ]

        result = pipeline.assemble_outputs(run_manifest)

        self.assertEqual("READY_FOR_VISUAL_REVIEW", result["state"])
        self.assertEqual("PASS", result["technical_result"])
        self.assertTrue(result["loop_review_required"])
        self.assertEqual(before, tuple(path.read_bytes() for path in frame_paths))
        with Image.open(run_dir / "normalized/sprite-sheet.png") as sheet:
            self.assertEqual("RGBA", sheet.mode)
            self.assertEqual((36, 18), sheet.size)
            for index, path in enumerate(frame_paths):
                with Image.open(path) as frame:
                    expected = frame.convert("RGBA").tobytes()
                self.assertEqual(
                    expected,
                    sheet.crop((index * 12, 0, (index + 1) * 12, 18)).tobytes(),
                )
        with Image.open(run_dir / "previews/loop.gif") as loop:
            self.assertEqual(3, loop.n_frames)
            self.assertEqual(0, loop.info["loop"])
        with Image.open(run_dir / "previews/contact-sheet.png") as contact:
            self.assertEqual((36, 18), contact.size)
        run = self.read_json(run_manifest)
        self.assertEqual("READY_FOR_VISUAL_REVIEW", run["state"])
        self.assertIsNone(run["terminal_reason"])
        self.assertEqual(100, run["artifacts"]["assembly"]["duration_ms"])
        self.assertEqual("COMPLETED", run["artifacts"]["assembly"]["status"])
        self.assertEqual(
            ledger_fingerprints,
            [item["request_fingerprint"] for item in run["requests"]],
        )
        qa = self.read_json(run_dir / "reports/qa-report.json")
        provenance = self.read_json(run_dir / "reports/provenance-report.json")
        self.assertEqual("READY_FOR_VISUAL_REVIEW", qa["state"])
        self.assertTrue(qa["loop_review_required"])
        self.assertEqual(
            self.read_json(run_dir / "reports/manifest.json")["provider"],
            provenance["provider"],
        )
        self.assertNotIn("run_id", json.dumps(provenance))
        self.assertNotIn("request_fingerprint", json.dumps(provenance))
        for index, request in enumerate(provenance["requests"], 1):
            plan = self.read_json(run_dir / f"input/requests/frame-{index:02d}.json")
            unsigned = dict(plan)
            unsigned.pop("request_fingerprint")
            reference_hashes = [
                {
                    "role": "master",
                    "path": "input/source/master.png",
                    "sha256": self.read_json(run_dir / "reports/manifest.json")["source"]["normalized_sha256"],
                }
            ]
            if index > 1:
                previous = self.read_json(run_manifest)["frames"][index - 2]
                reference_hashes.append(
                    {
                        "role": "previous_accepted",
                        "path": previous["normalized_path"],
                        "sha256": previous["normalized_sha256"],
                    }
                )
            self.assertEqual(
                contract.canonical_sha256(
                    {
                        "schema_version": 1,
                        "request": unsigned,
                        "references": reference_hashes,
                    }
                ),
                request["request_content_sha256"],
            )

    def test_historic_v1_ready_reassembly_is_byte_idempotent(self) -> None:
        run_dir, run_manifest = self.copy_historic_v1_run("ready")
        before = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        first = pipeline.assemble_outputs(run_manifest)
        second = pipeline.assemble_outputs(run_manifest)
        self.assertEqual("READY_FOR_VISUAL_REVIEW", first["state"])
        self.assertEqual(first, second)
        self.assertEqual(
            before,
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            },
        )

    def test_v2_long_decimal_price_prepares_authorizes_and_assembles_exactly(self) -> None:
        self.pricing.write_text(
            json.dumps(
                {
                    "model": contract.GENERIC_MODEL,
                    "source_url": "https://api.fal.ai/v1/models/pricing",
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "currency": "USD",
                    "unit": "image",
                    "base_unit_price_usd": "0.123456789012345678",
                    "resolution": "0.5K",
                    "resolution_multiplier": "0.333333333333333333",
                }
            ),
            encoding="utf-8",
        )
        budget = Decimal("0.082304526008230451917695473991769548")
        run_dir = self.prepare(
            "v2-long-decimal",
            frame_size=(12, 18),
            max_budget_usd=budget,
        )
        run_manifest = run_dir / "reports/run_manifest.json"
        pipeline.authorize_run(run_manifest, budget)
        for frame_index in (1, 2):
            raw = Image.new("RGB", (12, 18), (231, 17, 199))
            raw.paste((20 + frame_index, 30, 40), (3, 2, 9, 18))
            raw_path = self.seed_completed_raw(run_manifest, frame_index, raw)
            pipeline.record_evaluation(run_manifest, frame_index, raw_path)
        result = pipeline.assemble_outputs(run_manifest)
        self.assertEqual("READY_FOR_VISUAL_REVIEW", result["state"])
        manifest = self.read_json(run_dir / "reports/manifest.json")
        self.assertEqual(
            "0.082304526008230451917695473991769548",
            manifest["authorization"]["worst_case_usd"],
        )

    def test_ready_reentry_degrades_after_raw_or_each_final_mutates(self) -> None:
        targets = (
            "raw/frame-01/attempt-01.png",
            "normalized/sprite-sheet.png",
            "previews/loop.gif",
            "previews/contact-sheet.png",
            "reports/qa-report.json",
            "reports/provenance-report.json",
        )
        for index, relative in enumerate(targets):
            with self.subTest(relative=relative):
                run_dir, run_manifest = self.complete_assembly_run(f"ready-reentry-race-{index}")
                pipeline.assemble_outputs(run_manifest)
                before_run = self.read_json(run_manifest)
                target = run_dir / relative
                original_bytes = target.read_bytes()
                mutated_bytes = original_bytes + b"reentry-mutation"
                original_capture = pipeline._capture_assembly_evidence

                def mutate_after_capture(*args):
                    captured = original_capture(*args)
                    target.write_bytes(mutated_bytes)
                    return captured

                with mock.patch.object(pipeline, "_capture_assembly_evidence", mutate_after_capture):
                    with self.assertRaises(ValueError):
                        pipeline.assemble_outputs(run_manifest)

                expected_pending = copy.deepcopy(before_run)
                expected_pending["state"] = "AUTHORIZED"
                expected_pending["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
                self.assertEqual(expected_pending, self.read_json(run_manifest))
                self.assertEqual(mutated_bytes, target.read_bytes())
                # Corrupt finals/evidence are never adopted during recovery.
                with self.assertRaises(ValueError):
                    pipeline.assemble_outputs(run_manifest)
                self.assertEqual(mutated_bytes, target.read_bytes())
                # Restoring the fixture's exact bytes makes the pending claim resumable.
                target.write_bytes(original_bytes)
                self.assertEqual("READY_FOR_VISUAL_REVIEW", pipeline.assemble_outputs(run_manifest)["state"])

    def test_ready_reentry_degrades_on_evidence_failure_before_capture(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("ready-reentry-early-failure")
        pipeline.assemble_outputs(run_manifest)
        raw = run_dir / "raw/frame-01/attempt-01.png"
        raw.write_bytes(raw.read_bytes() + b"preexisting-corruption")
        with self.assertRaises(ValueError):
            pipeline.assemble_outputs(run_manifest)
        persisted = self.read_json(run_manifest)
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual("PENDING_PUBLICATION", persisted["artifacts"]["assembly"]["status"])

    def test_ready_reentry_is_idempotent_without_lifecycle_or_final_writes(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("ready-reentry-idempotent")
        expected = pipeline.assemble_outputs(run_manifest)
        paths = [run_manifest] + [run_dir / name for name in pipeline._ASSEMBLY_FINAL_PATHS]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths}
        self.assertEqual(expected, pipeline.assemble_outputs(run_manifest))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in paths})

    def assert_corrupt_ready_claim_recovers(self, name, corrupt) -> None:
        run_dir, run_manifest = self.complete_assembly_run(name)
        pipeline.assemble_outputs(run_manifest)
        original = self.read_json(run_manifest)
        expected_pending = copy.deepcopy(original)
        expected_pending["state"] = "AUTHORIZED"
        expected_pending["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
        changed = copy.deepcopy(original)
        corrupt(changed["artifacts"])
        self.write_json(run_manifest, changed)
        files = {path: (path.read_bytes(), path.stat().st_ino) for path in run_dir.rglob("*") if path.is_file() and path != run_manifest}
        with self.assertRaises(ValueError):
            pipeline.assemble_outputs(run_manifest)
        self.assertEqual(expected_pending, self.read_json(run_manifest))
        self.assertEqual(files, {path: (path.read_bytes(), path.stat().st_ino) for path in files})
        self.assertEqual("READY_FOR_VISUAL_REVIEW", pipeline.assemble_outputs(run_manifest)["state"])
        self.assertEqual(original, self.read_json(run_manifest))
        self.assertEqual(files, {path: (path.read_bytes(), path.stat().st_ino) for path in files})

    def test_ready_missing_assembly_claim_demotes_to_canonical_recoverable_pending(self) -> None:
        self.assert_corrupt_ready_claim_recovers("ready-missing-claim", lambda artifacts: artifacts.pop("assembly"))

    def test_ready_invalid_assembly_status_demotes_to_canonical_recoverable_pending(self) -> None:
        self.assert_corrupt_ready_claim_recovers("ready-invalid-status", lambda artifacts: artifacts["assembly"].update(status="BROKEN"))

    def test_ready_altered_assembly_duration_demotes_to_canonical_recoverable_pending(self) -> None:
        self.assert_corrupt_ready_claim_recovers("ready-invalid-duration", lambda artifacts: artifacts["assembly"].update(duration_ms=250))

    def test_ready_altered_assembly_files_or_digest_demotes_to_canonical_recoverable_pending(self) -> None:
        changes = (
            ("files", lambda artifacts: artifacts["assembly"].update(files=[])),
            ("digest", lambda artifacts: artifacts["assembly"]["files"][0].update(sha256="0" * 64)),
        )
        for name, change in changes:
            with self.subTest(name=name):
                self.assert_corrupt_ready_claim_recovers(f"ready-invalid-{name}", change)

    def test_invalid_ready_claim_demotion_preserves_exact_concurrent_lifecycle(self) -> None:
        changes = (
            ("missing", lambda artifacts: artifacts.pop("assembly")),
            ("status", lambda artifacts: artifacts["assembly"].update(status="BROKEN")),
            ("duration", lambda artifacts: artifacts["assembly"].update(duration_ms=250)),
            ("files", lambda artifacts: artifacts["assembly"].update(files=[])),
        )
        for name, change in changes:
            with self.subTest(name=name):
                _run_dir, run_manifest = self.complete_assembly_run(f"ready-cas-{name}")
                pipeline.assemble_outputs(run_manifest)
                concurrent = self.read_json(run_manifest)
                invalid = copy.deepcopy(concurrent)
                change(invalid["artifacts"])
                self.write_json(run_manifest, invalid)
                entry_bytes = run_manifest.read_bytes()
                concurrent["state"] = "AUTHORIZED"
                concurrent["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
                concurrent["requests"][0]["submitted_at"] = "2026-09-02T13:45:00Z"
                original_rewrite = pipeline._rewrite_existing_json
                attempted = False

                def replace_before_demotion(path, value, expected):
                    nonlocal attempted
                    attempted = True
                    self.assertEqual(entry_bytes, expected)
                    self.write_json(run_manifest, concurrent)
                    return original_rewrite(path, value, expected)

                with mock.patch.object(pipeline, "_rewrite_existing_json", replace_before_demotion):
                    with self.assertRaises(ValueError):
                        pipeline.assemble_outputs(run_manifest)
                self.assertTrue(attempted)
                self.assertEqual(concurrent, self.read_json(run_manifest))

    def assert_ready_demotion_rejects_identical_foreign_inode(self, recoverable, conversion=None, conversion_name="unchanged") -> None:
        run_dir, run_manifest = self.complete_assembly_run(f"ready-entry-inode-{recoverable}-{conversion_name}")
        pipeline.assemble_outputs(run_manifest)
        invalid = self.read_json(run_manifest)
        invalid["artifacts"].pop("assembly")
        self.write_json(run_manifest, invalid)
        if not recoverable:
            for name in ("normalized/frames/frame-01.png", "normalized/sprite-sheet.png"):
                target = run_dir / name
                target.write_bytes(target.read_bytes() + b"unreconstructable-corruption")
        entry_bytes = run_manifest.read_bytes()
        entry_stat = run_manifest.stat()
        preserved = {path: (path.read_bytes(), path.stat().st_ino) for path in run_dir.rglob("*") if path.is_file() and path != run_manifest}
        original_rewrite = pipeline._rewrite_existing_json
        foreign_inode = None

        def replace_after_entry_guard_closes(path, value, expected):
            nonlocal foreign_inode
            self.assertEqual(entry_bytes, expected)
            if conversion is not None:
                try:
                    expected = conversion(expected)
                except TypeError as error:
                    self.fail(f"ordinary expected bytes must support {conversion_name}: {error}")
                self.assertIs(type(expected), bytes)
                self.assertEqual(entry_bytes, expected)
            self.assertEqual("AUTHORIZED", value["state"])
            if recoverable:
                self.assertEqual("PENDING_PUBLICATION", value["artifacts"]["assembly"]["status"])
            else:
                self.assertNotIn("assembly", value["artifacts"])
            foreign = self.root / f"foreign-ready-entry-{recoverable}.json"
            foreign.write_bytes(entry_bytes)
            os.utime(foreign, ns=(entry_stat.st_atime_ns, entry_stat.st_mtime_ns))
            foreign_inode = foreign.stat().st_ino
            self.assertNotEqual(entry_stat.st_ino, foreign_inode)
            with pipeline._PathGuard(foreign, directory=False, writable_rename=True) as guard:
                pipeline._rename_windows_handle(guard.handle, run_manifest, replace_if_exists=True)
            self.assertEqual(foreign_inode, run_manifest.stat().st_ino)
            self.assertEqual(entry_stat.st_mtime_ns, run_manifest.stat().st_mtime_ns)
            self.assertEqual(entry_bytes, run_manifest.read_bytes())
            return original_rewrite(path, value, expected)

        with mock.patch.object(pipeline, "_rewrite_existing_json", replace_after_entry_guard_closes):
            with self.assertRaises(ValueError) as caught:
                pipeline.assemble_outputs(run_manifest)
        self.assertIsNotNone(foreign_inode)
        self.assertEqual(foreign_inode, run_manifest.stat().st_ino)
        self.assertEqual(entry_bytes, run_manifest.read_bytes())
        self.assertEqual(entry_stat.st_mtime_ns, run_manifest.stat().st_mtime_ns)
        self.assertEqual("ASSEMBLY_CLAIM_INVALID", str(caught.exception))
        self.assertEqual(preserved, {path: (path.read_bytes(), path.stat().st_ino) for path in preserved})
        # The failed scoped demotion must not constrain an unrelated later call.
        pipeline._rewrite_existing_json(run_manifest, {"after_scope": True}, entry_bytes)
        self.assertEqual({"after_scope": True}, self.read_json(run_manifest))

    @unittest.skipUnless(os.name == "nt", "native Windows replacement race")
    def test_ready_pending_demotion_rejects_identical_foreign_inode_after_entry_guard(self) -> None:
        self.assert_ready_demotion_rejects_identical_foreign_inode(True)

    @unittest.skipUnless(os.name == "nt", "native Windows replacement race")
    def test_ready_claimless_demotion_rejects_identical_foreign_inode_after_entry_guard(self) -> None:
        self.assert_ready_demotion_rejects_identical_foreign_inode(False)

    def assert_ready_demotion_survives_expected_payload_conversions(self, recoverable) -> None:
        for name, conversion in (
            ("bytes", bytes),
            ("slice", lambda value: value[:]),
            ("concat", lambda value: b"" + value),
            ("copy", copy.copy),
            ("deepcopy", copy.deepcopy),
        ):
            with self.subTest(conversion=name):
                self.assert_ready_demotion_rejects_identical_foreign_inode(recoverable, conversion, name)

    @unittest.skipUnless(os.name == "nt", "native Windows replacement race")
    def test_ready_pending_demotion_preserves_identity_after_expected_payload_conversions(self) -> None:
        self.assert_ready_demotion_survives_expected_payload_conversions(True)

    @unittest.skipUnless(os.name == "nt", "native Windows replacement race")
    def test_ready_claimless_demotion_preserves_identity_after_expected_payload_conversions(self) -> None:
        self.assert_ready_demotion_survives_expected_payload_conversions(False)

    def test_rewrite_snapshot_context_restores_nested_binding_and_cleans_up_after_exception(self) -> None:
        target = self.root / "nested-rewrite.json"
        other = self.root / "unrelated-rewrite.json"
        payload = b'{"original": true}'
        target.write_bytes(payload)
        other.write_bytes(payload)
        stale_snapshot = pipeline._file_snapshot(target, payload)
        replacement = self.root / "nested-replacement.json"
        replacement.write_bytes(payload)
        replacement.replace(target)
        current_snapshot = pipeline._file_snapshot(target, payload)
        with pipeline._bind_rewrite_snapshot(target, stale_snapshot):
            self.assert_reason("PATH_IDENTITY_CHANGED", lambda: pipeline._rewrite_existing_json(target, {}, payload))
            with pipeline._bind_rewrite_snapshot(other, pipeline._file_snapshot(other, payload)):
                self.assert_reason("PATH_IDENTITY_CHANGED", lambda: pipeline._rewrite_existing_json(target, {}, payload))
                pipeline._rewrite_existing_json(other, {"other": True}, payload)
            with self.assertRaisesRegex(RuntimeError, "leave-inner"):
                with pipeline._bind_rewrite_snapshot(target, current_snapshot):
                    pipeline._rewrite_existing_json(target, {"inner": True}, payload)
                    raise RuntimeError("leave-inner")
            inner_bytes = target.read_bytes()
            self.assert_reason("PATH_IDENTITY_CHANGED", lambda: pipeline._rewrite_existing_json(target, {}, inner_bytes))
            # A binding for target must not affect an unbound, different path.
            pipeline._rewrite_existing_json(other, {"unbound_other": True}, other.read_bytes())
        pipeline._rewrite_existing_json(target, {"after_scope": True}, inner_bytes)
        self.assertEqual({"after_scope": True}, self.read_json(target))
        self.assertEqual({"unbound_other": True}, self.read_json(other))

    def test_rewrite_snapshot_context_isolates_concurrent_same_target_bindings(self) -> None:
        target = self.root / "concurrent-context.json"
        payload = b'{"original": true}'
        target.write_bytes(payload)
        stale_snapshot = pipeline._file_snapshot(target, payload)
        replacement = self.root / "concurrent-replacement.json"
        replacement.write_bytes(payload)
        replacement.replace(target)
        current_snapshot = pipeline._file_snapshot(target, payload)
        stale_entered, current_entered, stale_finished = Event(), Event(), Event()

        def stale_writer():
            with pipeline._bind_rewrite_snapshot(target, stale_snapshot):
                stale_entered.set()
                self.assertTrue(current_entered.wait(10))
                try:
                    self.assert_reason("PATH_IDENTITY_CHANGED", lambda: pipeline._rewrite_existing_json(target, {"stale": True}, payload))
                finally:
                    stale_finished.set()

        def current_writer():
            self.assertTrue(stale_entered.wait(10))
            with pipeline._bind_rewrite_snapshot(target, current_snapshot):
                current_entered.set()
                self.assertTrue(stale_finished.wait(10))
                pipeline._rewrite_existing_json(target, {"current": True}, payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(stale_writer), executor.submit(current_writer)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual({"current": True}, self.read_json(target))
        pipeline._rewrite_existing_json(target, {"after_threads": True}, target.read_bytes())
        self.assertEqual({"after_threads": True}, self.read_json(target))

    def test_invalid_ready_claim_with_unreconstructable_evidence_demotes_without_claim(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("ready-unreconstructable")
        pipeline.assemble_outputs(run_manifest)
        canonical = self.read_json(run_manifest)
        invalid = copy.deepcopy(canonical)
        invalid["artifacts"].pop("assembly")
        self.write_json(run_manifest, invalid)
        normalized = run_dir / "normalized/frames/frame-01.png"
        sheet = run_dir / "normalized/sprite-sheet.png"
        originals = {path: path.read_bytes() for path in (normalized, sheet)}
        corrupted = {path: data + b"corruption" for path, data in originals.items()}
        for path, data in corrupted.items():
            path.write_bytes(data)
        self.assert_reason("ASSEMBLY_RECOVERY_REQUIRED", lambda: pipeline.assemble_outputs(run_manifest))
        expected = copy.deepcopy(invalid)
        expected["state"] = "AUTHORIZED"
        self.assertEqual(expected, self.read_json(run_manifest))
        self.assertEqual(corrupted, {path: path.read_bytes() for path in corrupted})
        for path, data in originals.items():
            path.write_bytes(data)
        self.assert_reason("FINAL_ARTIFACT_EXISTS", lambda: pipeline.assemble_outputs(run_manifest))
        self.assertIsNone(pipeline.next_request_plan(run_manifest, run_dir / "input/requests/frame-04.json"))
        self.write_json(run_manifest, canonical)
        self.assertEqual("READY_FOR_VISUAL_REVIEW", pipeline.assemble_outputs(run_manifest)["state"])

    def test_invalid_ready_claim_uses_exact_final_outputs_without_accepting_invalid_request_metadata(self) -> None:
        for bad_metadata in (False, True):
            with self.subTest(bad_metadata=bad_metadata):
                run_dir, run_manifest = self.complete_assembly_run(f"ready-final-recovery-{bad_metadata}")
                pipeline.assemble_outputs(run_manifest)
                canonical = self.read_json(run_manifest)
                invalid = copy.deepcopy(canonical)
                invalid["artifacts"]["assembly"]["status"] = "BROKEN"
                if bad_metadata:
                    invalid["requests"][0]["submitted_at"] = "invalid-timestamp"
                self.write_json(run_manifest, invalid)
                raw = run_dir / "raw/frame-01/attempt-01.png"
                original_raw = raw.read_bytes()
                corrupted_raw = original_raw + b"raw-corruption"
                raw.write_bytes(corrupted_raw)
                finals = {run_dir / name: (run_dir / name).read_bytes() for name in pipeline._ASSEMBLY_FINAL_PATHS}
                with self.assertRaises(ValueError):
                    pipeline.assemble_outputs(run_manifest)
                persisted = self.read_json(run_manifest)
                self.assertEqual("AUTHORIZED", persisted["state"])
                self.assertEqual(corrupted_raw, raw.read_bytes())
                self.assertEqual(finals, {path: path.read_bytes() for path in finals})
                if bad_metadata:
                    self.assertNotIn("assembly", persisted["artifacts"])
                    self.assertEqual("invalid-timestamp", persisted["requests"][0]["submitted_at"])
                else:
                    expected = copy.deepcopy(canonical)
                    expected["state"] = "AUTHORIZED"
                    expected["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
                    self.assertEqual(expected, persisted)
                    with self.assertRaises(ValueError):
                        pipeline.assemble_outputs(run_manifest)
                    raw.write_bytes(original_raw)
                    self.assertEqual("READY_FOR_VISUAL_REVIEW", pipeline.assemble_outputs(run_manifest)["state"])

    def test_ready_reentry_rollback_never_overwrites_concurrent_lifecycle(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("ready-reentry-cas-race")
        pipeline.assemble_outputs(run_manifest)
        concurrent = self.read_json(run_manifest)
        concurrent["requests"][0]["submitted_at"] = "2026-09-02T13:45:00Z"
        concurrent["state"] = "AUTHORIZED"
        concurrent["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
        original_capture = pipeline._capture_assembly_evidence
        original_rewrite = pipeline._rewrite_existing_json

        def mutate_after_capture(*args):
            captured = original_capture(*args)
            raw = run_dir / "raw/frame-01/attempt-01.png"
            raw.write_bytes(raw.read_bytes() + b"rollback-trigger")
            return captured

        def concurrent_change_before_cas(path, value, expected):
            self.write_json(run_manifest, concurrent)
            return original_rewrite(path, value, expected)

        with mock.patch.object(pipeline, "_capture_assembly_evidence", mutate_after_capture):
            with mock.patch.object(pipeline, "_rewrite_existing_json", concurrent_change_before_cas):
                with self.assertRaises(ValueError):
                    pipeline.assemble_outputs(run_manifest)
        self.assertEqual(concurrent, self.read_json(run_manifest))

    @unittest.skipUnless(os.name == "nt", "requires native Windows replacement")
    def test_first_commit_rollback_preserves_concurrent_ready_bytes(self) -> None:
        _run_dir, run_manifest = self.complete_assembly_run("first-ready-rollback-cas")
        original_rewrite = pipeline._rewrite_existing_json
        concurrent_bytes = None

        def replace_committed_ready(path, value, expected):
            nonlocal concurrent_bytes
            result = original_rewrite(path, value, expected)
            if value.get("state") == "READY_FOR_VISUAL_REVIEW":
                concurrent = copy.deepcopy(value)
                concurrent["requests"][0]["submitted_at"] = "2026-09-02T13:45:00Z"
                other = run_manifest.with_name("concurrent-ready.json")
                self.write_json(other, concurrent)
                concurrent_bytes = other.read_bytes()
                with pipeline._PathGuard(other, directory=False, writable_rename=True) as guard:
                    pipeline._rename_windows_handle(guard.handle, run_manifest, replace_if_exists=True)
                raise ValueError("INJECTED_POST_COMMIT_FAILURE")
            return result

        with mock.patch.object(pipeline, "_rewrite_existing_json", replace_committed_ready):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)
        self.assertIsNotNone(concurrent_bytes)
        self.assertEqual(concurrent_bytes, run_manifest.read_bytes())

    @unittest.skipUnless(os.name == "nt", "requires native Windows replacement")
    def test_ready_rollback_cas_preserves_native_replacement_at_publish_boundary(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("native-cas-publish-race")
        pipeline.assemble_outputs(run_manifest)
        concurrent = self.read_json(run_manifest)
        concurrent["requests"][0]["submitted_at"] = "2026-09-02T13:45:00Z"
        concurrent["state"] = "AUTHORIZED"
        concurrent["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
        other = run_manifest.with_name("concurrent-pending.json")
        self.write_json(other, concurrent)
        concurrent_bytes = other.read_bytes()
        raw = run_dir / "raw/frame-01/attempt-01.png"
        raw.write_bytes(raw.read_bytes() + b"rollback-trigger")
        original_replace = pipeline._replace_existing_file
        attempted = False

        def replace_at_publish_boundary(replacement_guard, target):
            nonlocal attempted
            attempted = True
            with pipeline._PathGuard(other, directory=False, writable_rename=True) as guard:
                pipeline._rename_windows_handle(guard.handle, target, replace_if_exists=True)
            original_replace(replacement_guard, target)

        with mock.patch.object(pipeline, "_replace_existing_file", replace_at_publish_boundary):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)
        self.assertTrue(attempted)
        self.assertEqual(concurrent_bytes, run_manifest.read_bytes())

    def test_assemble_prevalidation_rejects_incomplete_reordered_or_mutated_chain_without_finals(self) -> None:
        for case in (
            "incomplete",
            "missing",
            "reordered",
            "mutated",
            "request-count",
            "request-cost",
            "report-hash",
            "requests-type",
            "frames-type",
        ):
            with self.subTest(case=case):
                if case == "incomplete":
                    run_dir, run_manifest = self.prepare_assembly_run(f"assemble-{case}")
                    raw = Image.new("RGB", (12, 18), (231, 17, 199))
                    raw.paste((50, 60, 70), (3, 2, 9, 18))
                    raw_path = self.seed_completed_raw(run_manifest, 1, raw)
                    pipeline.record_evaluation(run_manifest, 1, raw_path)
                else:
                    run_dir, run_manifest = self.complete_assembly_run(f"assemble-{case}")
                    run = self.read_json(run_manifest)
                    if case == "missing":
                        (run_dir / "normalized/frames/frame-02.png").unlink()
                    elif case == "reordered":
                        run["frames"].reverse()
                        self.write_json(run_manifest, run)
                    elif case == "mutated":
                        target = run_dir / "normalized/frames/frame-02.png"
                        with Image.open(target) as opened:
                            changed = opened.convert("RGBA").copy()
                        changed.putpixel((6, 8), (1, 2, 3, 255))
                        changed.save(target)
                    elif case == "request-count":
                        run["requests"].pop()
                        self.write_json(run_manifest, run)
                    elif case == "request-cost":
                        run["requests"][1]["estimated_cost_usd"] = "0.11"
                        self.write_json(run_manifest, run)
                    elif case == "report-hash":
                        report = run_dir / "reports/frames/frame-02.json"
                        report.write_bytes(report.read_bytes() + b" ")
                    elif case == "requests-type":
                        run["requests"] = None
                        self.write_json(run_manifest, run)
                    elif case == "frames-type":
                        run["frames"] = None
                        self.write_json(run_manifest, run)
                before = run_manifest.read_bytes()

                with self.assertRaises(ValueError):
                    pipeline.assemble_outputs(run_manifest)

                self.assert_no_final_assembly(run_dir)
                self.assertEqual(before, run_manifest.read_bytes())

    def test_rejected_duplicate_and_submission_unknown_never_publish_or_plan_later(self) -> None:
        rejected_dir, rejected_manifest = self.prepare_assembly_run(
            "assemble-duplicate-rejected",
            2,
        )
        duplicate = Image.new("RGB", (12, 18), (231, 17, 199))
        duplicate.paste((50, 60, 70), (3, 2, 9, 18))
        first_raw = self.seed_completed_raw(rejected_manifest, 1, duplicate)
        pipeline.record_evaluation(rejected_manifest, 1, first_raw)
        second_raw = self.seed_completed_raw(rejected_manifest, 2, duplicate)
        rejected = pipeline.record_evaluation(rejected_manifest, 2, second_raw)
        self.assertEqual(["FRAME_DUPLICATE"], rejected["failures"])
        with self.assertRaises(ValueError):
            pipeline.assemble_outputs(rejected_manifest)
        self.assert_no_final_assembly(rejected_dir)
        self.assertIsNone(
            pipeline.next_request_plan(
                rejected_manifest,
                rejected_dir / "input/requests/frame-03.json",
            )
        )

        unknown_dir, unknown_manifest = self.prepare_assembly_run(
            "assemble-submission-unknown",
            2,
        )
        plan = pipeline.next_request_plan(
            unknown_manifest,
            unknown_dir / "input/requests/frame-01.json",
        )
        run = self.read_json(unknown_manifest)
        run["requests"].append(
            {
                "frame_index": 1,
                "attempt": 1,
                "model": contract.GENERIC_MODEL,
                "request_fingerprint": plan["request_fingerprint"],
                "request_id": None,
                "status": "SUBMISSION_STATUS_UNKNOWN",
                "estimated_cost_usd": "0.10",
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": None,
                "safe_error_type": "TimeoutError",
            }
        )
        self.write_json(unknown_manifest, run)
        with self.assertRaises(ValueError):
            pipeline.assemble_outputs(unknown_manifest)
        self.assert_no_final_assembly(unknown_dir)
        self.assert_reason(
            "REMOTE_STATE_UNCERTAIN",
            lambda: pipeline.next_request_plan(
                unknown_manifest,
                unknown_dir / "input/requests/frame-01.json",
            ),
        )

    def test_assemble_claim_recovers_matching_files_and_never_replaces_preexisting_target(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-recovery")
        original_publish = pipeline._publish_new_bytes
        calls = {"count": 0}

        def cut_after_second_publish(path: Path, payload: bytes, exists_reason: str):
            published = original_publish(path, payload, exists_reason)
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("CUT_DURING_ASSEMBLY")
            return published

        with mock.patch.object(pipeline, "_publish_new_bytes", cut_after_second_publish):
            with self.assertRaisesRegex(RuntimeError, "CUT_DURING_ASSEMBLY"):
                pipeline.assemble_outputs(run_manifest)
        interrupted = self.read_json(run_manifest)
        self.assertEqual("AUTHORIZED", interrupted["state"])
        self.assertEqual("PENDING_PUBLICATION", interrupted["artifacts"]["assembly"]["status"])
        frozen = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in (
                run_dir / "normalized/sprite-sheet.png",
                run_dir / "previews/loop.gif",
            )
        }

        recovered = pipeline.assemble_outputs(run_manifest)

        self.assertEqual("READY_FOR_VISUAL_REVIEW", recovered["state"])
        self.assertEqual(
            frozen,
            {
                path.relative_to(run_dir).as_posix(): path.read_bytes()
                for path in (
                    run_dir / "normalized/sprite-sheet.png",
                    run_dir / "previews/loop.gif",
                )
            },
        )

        blocked_dir, blocked_manifest = self.complete_assembly_run("assemble-no-replace")
        sentinel = blocked_dir / "normalized/sprite-sheet.png"
        sentinel.write_bytes(b"do-not-replace")
        before = blocked_manifest.read_bytes()
        self.assert_reason(
            "FINAL_ARTIFACT_EXISTS",
            lambda: pipeline.assemble_outputs(blocked_manifest),
        )
        self.assertEqual(b"do-not-replace", sentinel.read_bytes())
        self.assertEqual(before, blocked_manifest.read_bytes())
        self.assertFalse((blocked_dir / "previews/loop.gif").exists())

    def test_assemble_revalidates_the_chain_after_persisting_its_claim(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-claim-race")
        original_rewrite = pipeline._rewrite_existing_json
        changed = {"attempted": False, "done": False}

        def mutate_after_claim(path: Path, value: object, expected: bytes):
            published = original_rewrite(path, value, expected)
            if (
                Path(path) == run_manifest
                and isinstance(value, dict)
                and value.get("artifacts", {}).get("assembly", {}).get("status")
                == "PENDING_PUBLICATION"
                and not changed["done"]
            ):
                changed["done"] = True
                target = run_dir / "normalized/frames/frame-02.png"
                with Image.open(target) as opened:
                    image = opened.convert("RGBA").copy()
                image.putpixel((6, 8), (1, 2, 3, 255))
                image.save(target)
            return published

        with mock.patch.object(pipeline, "_rewrite_existing_json", mutate_after_claim):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        self.assertTrue(changed["done"])
        self.assert_no_final_assembly(run_dir)
        self.assertEqual(
            "PENDING_PUBLICATION",
            self.read_json(run_manifest)["artifacts"]["assembly"]["status"],
        )

    def test_assemble_detects_raw_or_final_mutation_during_publication_and_stays_pending(self) -> None:
        for case in ("raw-after-first-final", "sheet-after-publish"):
            with self.subTest(case=case):
                run_dir, run_manifest = self.complete_assembly_run(
                    f"assemble-publish-mutation-{case}"
                )
                original_publish = pipeline._publish_new_bytes
                calls = {"count": 0}

                def mutate_after_publish(path: Path, payload: bytes, exists_reason: str):
                    published = original_publish(path, payload, exists_reason)
                    calls["count"] += 1
                    if calls["count"] == 1:
                        target = (
                            run_dir / "raw/frame-01/attempt-01.png"
                            if case == "raw-after-first-final"
                            else run_dir / "normalized/sprite-sheet.png"
                        )
                        target.write_bytes(target.read_bytes() + b"mutation")
                    return published

                with mock.patch.object(pipeline, "_publish_new_bytes", mutate_after_publish):
                    with self.assertRaises(ValueError):
                        pipeline.assemble_outputs(run_manifest)

                persisted = self.read_json(run_manifest)
                self.assertEqual("AUTHORIZED", persisted["state"])
                self.assertEqual(
                    "PENDING_PUBLICATION",
                    persisted["artifacts"]["assembly"]["status"],
                )
                self.assertNotEqual("READY_FOR_VISUAL_REVIEW", persisted["state"])

    def test_assemble_does_not_adopt_raw_mutation_at_authoritative_capture(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-capture-race")
        original_capture = pipeline._capture_assembly_evidence
        changed = {"done": False}

        def mutate_before_capture(run_dir_arg: Path, run: dict, manifest: dict):
            if (
                run.get("artifacts", {}).get("assembly", {}).get("status")
                == "PENDING_PUBLICATION"
                and not changed["done"]
            ):
                changed["done"] = True
                raw = run_dir / "raw/frame-01/attempt-01.png"
                raw.write_bytes(raw.read_bytes() + b"mutation-at-capture")
            return original_capture(run_dir_arg, run, manifest)

        with mock.patch.object(
            pipeline,
            "_capture_assembly_evidence",
            mutate_before_capture,
        ):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        persisted = self.read_json(run_manifest)
        self.assertTrue(changed["done"])
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual(
            "PENDING_PUBLICATION",
            persisted["artifacts"]["assembly"]["status"],
        )
        self.assert_no_final_assembly(run_dir)

    def test_new_claim_rejects_identical_inode_replacement_after_each_publish(self) -> None:
        for index, relative in enumerate(pipeline._ASSEMBLY_FINAL_PATHS, 1):
            with self.subTest(relative=relative):
                run_dir, run_manifest = self.complete_assembly_run(
                    f"assemble-published-identity-race-{index}"
                )
                original_publish = pipeline._publish_new_bytes
                replaced = {"done": False}

                def replace_after_publish(
                    path: Path,
                    payload: bytes,
                    exists_reason: str,
                ):
                    published = original_publish(path, payload, exists_reason)
                    if path == run_dir / relative and not replaced["done"]:
                        replacement = path.with_name(f".{path.name}.replacement")
                        replacement.write_bytes(payload)
                        os.replace(replacement, path)
                        replaced["done"] = True
                    return published

                with mock.patch.object(
                    pipeline,
                    "_publish_new_bytes",
                    replace_after_publish,
                ):
                    with self.assertRaises(ValueError):
                        pipeline.assemble_outputs(run_manifest)

                persisted = self.read_json(run_manifest)
                self.assertTrue(replaced["done"])
                self.assertEqual("AUTHORIZED", persisted["state"])
                self.assertEqual(
                    "PENDING_PUBLICATION",
                    persisted["artifacts"]["assembly"]["status"],
                )
                self.assertEqual(
                    pipeline._assembly_payloads(*pipeline._assembly_context(run_manifest)[3:7])[2][relative],
                    (run_dir / relative).read_bytes(),
                )

    def test_assemble_never_adopts_valid_lifecycle_change_after_claim_capture(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-lifecycle-baseline-race")
        original_capture = pipeline._capture_exact_file
        changed = {"attempted": False, "done": False}

        def mutate_after_claim_capture(path: Path, expected: bytes, reason: str):
            captured = original_capture(path, expected, reason)
            if Path(path) == run_manifest and not changed["attempted"]:
                current = self.read_json(run_manifest)
                assembly = current.get("artifacts", {}).get("assembly")
                if isinstance(assembly, dict) and assembly.get("status") == "PENDING_PUBLICATION":
                    changed["attempted"] = True
                    current["requests"][0]["submitted_at"] = "2026-09-02T13:45:00Z"
                    self.write_json(run_manifest, current)
                    changed["done"] = True
            return captured

        with mock.patch.object(pipeline, "_capture_exact_file", mutate_after_claim_capture):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        persisted = self.read_json(run_manifest)
        self.assertTrue(changed["attempted"])
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual(
            "PENDING_PUBLICATION",
            persisted["artifacts"]["assembly"]["status"],
        )
        self.assert_no_final_assembly(run_dir)

    def test_capture_exact_file_rejects_identical_inode_replacement_in_snapshot_hook(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-capture-identity-race")
        target = run_dir / "raw/frame-01/attempt-01.png"
        original_snapshot = pipeline._file_snapshot
        replaced = {"attempted": False, "done": False}

        def replace_before_snapshot(path: Path, data: bytes):
            if Path(path) == target and not replaced["attempted"]:
                replaced["attempted"] = True
                replacement = target.with_name(f".{target.name}.replacement")
                replacement.write_bytes(data)
                os.replace(replacement, target)
                replaced["done"] = True
            return original_snapshot(path, data)

        with mock.patch.object(pipeline, "_file_snapshot", replace_before_snapshot):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        persisted = self.read_json(run_manifest)
        self.assertTrue(replaced["attempted"])
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual(
            "PENDING_PUBLICATION",
            persisted["artifacts"]["assembly"]["status"],
        )
        self.assert_no_final_assembly(run_dir)

    def test_assemble_rechecks_evidence_while_constructing_ready_return(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-post-evidence-race")
        original_verify = pipeline._verify_captured_files
        changed = {"attempted": False, "done": False}

        def mutate_after_last_snapshot_verify(captured, reason: str) -> None:
            original_verify(captured, reason)
            persisted = self.read_json(run_manifest)
            if persisted.get("state") == "READY_FOR_VISUAL_REVIEW" and not changed["attempted"]:
                changed["attempted"] = True
                raw = run_dir / "raw/frame-01/attempt-01.png"
                raw.write_bytes(raw.read_bytes() + b"postcommit-mutation")
                changed["done"] = True

        with mock.patch.object(
            pipeline,
            "_verify_captured_files",
            mutate_after_last_snapshot_verify,
        ):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        persisted = self.read_json(run_manifest)
        self.assertTrue(changed["attempted"])
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual(
            "PENDING_PUBLICATION",
            persisted["artifacts"]["assembly"]["status"],
        )

    def test_assemble_rechecks_ready_lifecycle_while_constructing_return(self) -> None:
        _run_dir, run_manifest = self.complete_assembly_run("assemble-post-lifecycle-race")
        original_verify = pipeline._verify_lifecycle_bytes
        changed = {"attempted": False, "done": False, "ready_calls": 0}

        def restore_pending_after_ready_verify(path: Path, expected: bytes) -> None:
            original_verify(path, expected)
            expected_value = json.loads(expected.decode("utf-8"))
            if expected_value.get("state") == "READY_FOR_VISUAL_REVIEW":
                changed["ready_calls"] += 1
            if changed["ready_calls"] == 2 and not changed["attempted"]:
                changed["attempted"] = True
                pending = copy.deepcopy(expected_value)
                pending["state"] = "AUTHORIZED"
                pending["artifacts"]["assembly"]["status"] = "PENDING_PUBLICATION"
                self.write_json(run_manifest, pending)
                changed["done"] = True

        with mock.patch.object(
            pipeline,
            "_verify_lifecycle_bytes",
            restore_pending_after_ready_verify,
        ):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        persisted = self.read_json(run_manifest)
        self.assertTrue(changed["attempted"])
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual(
            "PENDING_PUBLICATION",
            persisted["artifacts"]["assembly"]["status"],
        )

    def test_assemble_rolls_back_ready_if_final_changes_during_final_run_rewrite(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-final-rewrite-race")
        original_rewrite = pipeline._rewrite_existing_json
        changed = {"done": False}

        def mutate_around_final_rewrite(path: Path, value: object, expected: bytes):
            if (
                Path(path) == run_manifest
                and isinstance(value, dict)
                and value.get("state") == "READY_FOR_VISUAL_REVIEW"
                and not changed["done"]
            ):
                changed["done"] = True
                sheet = run_dir / "normalized/sprite-sheet.png"
                sheet.write_bytes(sheet.read_bytes() + b"mutation")
            return original_rewrite(path, value, expected)

        with mock.patch.object(pipeline, "_rewrite_existing_json", mutate_around_final_rewrite):
            with self.assertRaises(ValueError):
                pipeline.assemble_outputs(run_manifest)

        persisted = self.read_json(run_manifest)
        self.assertTrue(changed["done"])
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual(
            "PENDING_PUBLICATION",
            persisted["artifacts"]["assembly"]["status"],
        )

    def test_new_claim_never_adopts_exact_or_wrong_late_preplant(self) -> None:
        for case in ("exact", "wrong"):
            with self.subTest(case=case):
                run_dir, run_manifest = self.complete_assembly_run(
                    f"assemble-late-preplant-{case}"
                )
                context = pipeline._assembly_context(run_manifest)
                payloads = pipeline._assembly_payloads(
                    context[3], context[4], context[5], context[6]
                )[2]
                planted = run_dir / "normalized/sprite-sheet.png"
                planted_bytes = (
                    payloads["normalized/sprite-sheet.png"]
                    if case == "exact"
                    else b"late-preplant"
                )
                original_rewrite = pipeline._rewrite_existing_json
                planted_once = {"done": False}

                def plant_after_claim(path: Path, value: object, expected: bytes):
                    published = original_rewrite(path, value, expected)
                    if (
                        Path(path) == run_manifest
                        and isinstance(value, dict)
                        and value.get("artifacts", {}).get("assembly", {}).get("status")
                        == "PENDING_PUBLICATION"
                        and not planted_once["done"]
                    ):
                        planted_once["done"] = True
                        planted.write_bytes(planted_bytes)
                    return published

                with mock.patch.object(pipeline, "_rewrite_existing_json", plant_after_claim):
                    self.assert_reason(
                        "FINAL_ARTIFACT_EXISTS",
                        lambda: pipeline.assemble_outputs(run_manifest),
                    )

                persisted = self.read_json(run_manifest)
                self.assertEqual("AUTHORIZED", persisted["state"])
                self.assertEqual(
                    "PENDING_PUBLICATION",
                    persisted["artifacts"]["assembly"]["status"],
                )
                self.assertEqual(planted_bytes, planted.read_bytes())
                for relative in (
                    "previews/loop.gif",
                    "previews/contact-sheet.png",
                    "reports/qa-report.json",
                    "reports/provenance-report.json",
                ):
                    self.assertFalse((run_dir / relative).exists(), relative)

    def test_assemble_requires_exact_run_keys_and_strict_integer_attempt_without_mutation(self) -> None:
        for case in ("extra-key", "missing-key", "bool-attempt"):
            with self.subTest(case=case):
                run_dir, run_manifest = self.complete_assembly_run(
                    f"assemble-schema-{case}"
                )
                run = self.read_json(run_manifest)
                if case == "extra-key":
                    run["unexpected"] = True
                elif case == "missing-key":
                    run.pop("terminal_reason")
                else:
                    run["requests"][0]["attempt"] = True
                self.write_json(run_manifest, run)
                before = run_manifest.read_bytes()

                with self.assertRaises(ValueError):
                    pipeline.assemble_outputs(run_manifest)

                self.assertEqual(before, run_manifest.read_bytes())
                self.assert_no_final_assembly(run_dir)

    def test_offline_e2e_is_content_deterministic_across_distinct_run_ids(self) -> None:
        final_relatives = (
            "normalized/frames/frame-01.png",
            "normalized/frames/frame-02.png",
            "normalized/frames/frame-03.png",
            "normalized/sprite-sheet.png",
            "previews/loop.gif",
            "previews/contact-sheet.png",
            "reports/qa-report.json",
            "reports/provenance-report.json",
        )
        runs = []
        manifests = []
        for run_id in ("offline-e2e-a", "offline-e2e-b"):
            self.plan.write_text(
                json.dumps(
                    [
                        {
                            "frame_index": index,
                            "phase": f"phase-{index}",
                            "body_motion": f"pose-{index}",
                        }
                        for index in range(1, 4)
                    ]
                ),
                encoding="utf-8",
            )
            run_dir = self.prepare(
                run_id,
                frame_count=3,
                frame_size=(12, 18),
                max_budget_usd=Decimal("0.30"),
            )
            run_manifest = run_dir / "reports/run_manifest.json"
            summary = pipeline.run_authorization_summary(run_manifest)
            self.assertEqual(3, summary["frame_count"])
            pipeline.authorize_run(run_manifest, Decimal("0.30"))
            for frame_index in range(1, 4):
                raw = Image.new("RGB", (12, 18), (231, 17, 199))
                raw.paste(
                    (
                        20 + frame_index * 25,
                        30 + frame_index * 15,
                        40 + frame_index * 10,
                    ),
                    (3, 2, 9, 18),
                )
                raw_path = self.seed_completed_raw(run_manifest, frame_index, raw)
                self.assertEqual(
                    "PASS",
                    pipeline.record_evaluation(
                        run_manifest,
                        frame_index,
                        raw_path,
                    )["technical_result"],
                )
            pipeline.assemble_outputs(run_manifest)
            runs.append(
                {
                    relative: (run_dir / relative).read_bytes()
                    for relative in final_relatives
                }
            )
            manifest = self.read_json(run_dir / "reports/manifest.json")
            manifest["authorization"].pop("authorized_at")
            manifests.append(manifest)

        self.assertEqual(runs[0], runs[1])
        self.assertEqual(manifests[0], manifests[1])

    def test_assemble_cli_prints_qa_and_never_approves(self) -> None:
        run_dir, run_manifest = self.complete_assembly_run("assemble-cli")
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("generic_sprite_pipeline.py")),
                "assemble",
                "--run-manifest",
                str(run_manifest),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            self.read_json(run_dir / "reports/qa-report.json"),
            json.loads(result.stdout),
        )
        self.assertEqual("READY_FOR_VISUAL_REVIEW", json.loads(result.stdout)["state"])
        self.assertNotIn("APPROVED", result.stdout)

    def test_valid_cli_prepares_run(self) -> None:
        result = subprocess.run(self.cli_arguments("cli-valid"), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.workspace / "cli-valid", Path(result.stdout.strip()))
        self.assertEqual(EXPECTED_FILES, {
            path.relative_to(self.workspace / "cli-valid").as_posix()
            for path in (self.workspace / "cli-valid").rglob("*")
            if path.is_file()
        })

    def test_cli_width_without_height_fails_without_run(self) -> None:
        arguments = self.cli_arguments("cli-width-only")
        arguments.extend(("--frame-width", "8"))
        result = subprocess.run(arguments, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("frame width and height must appear together", result.stderr)
        self.assertFalse((self.workspace / "cli-width-only").exists())
        self.assertEqual([], self.partials("cli-width-only"))


class RegionalFacadeDispatchTests(unittest.TestCase):
    def test_aligned_regional_export_loader_revalidates_live_parent(self):
        from unittest.mock import patch
        import generic_sprite_workflow as workflow
        import regional_video_workflow
        import sprite_alignment_workflow
        from sheet_workflow_state import encoded,scope_sha,sha_file,WorkflowError
        with tempfile.TemporaryDirectory(prefix='regional-parent-live-') as temp:
            root=Path(temp);filename=root/'run-manifest.json'
            Image.new('RGBA',(16,16),(80,90,100,255)).save(root/'source.png')
            (root/'attempt-01').mkdir();(root/'derivation').mkdir()
            d=dict(parent_manifest=str(root/'old-regional/run-manifest.json'))
            (root/'derivation/parent-manifest.json').write_bytes(encoded(dict(request=dict(route='regional_video_inpaint_v1'),run_id='parent')))
            q=dict(route='regional_video_inpaint_v1',derivation_sha256=scope_sha(d))
            scope=dict(run_id=root.name,source=dict(path=str(root/'source.png'),sha256=sha_file(root/'source.png')),request=q,authorization=dict(max_generation_attempts=1,max_cost_usd=0),derivation=d)
            m=dict(schema_version=3,mode='whole_sheet',state=workflow.READY,attempts=[dict(index=1,jobs={})],artifact_hashes={},authorization_scope_sha256=scope_sha(scope),**scope)
            filename.write_bytes(encoded(m));(root/'scope.json').write_bytes(encoded(scope));(root/'scope.sha256').write_text(scope_sha(scope)+'\n')
            with patch.object(sprite_alignment_workflow,'verify_alignment',return_value={}),patch.object(regional_video_workflow,'validate_regional_attempt',side_effect=WorkflowError('REGIONAL_RAW_CHANGED')):
                with self.assertRaisesRegex(WorkflowError,'REGIONAL_RAW_CHANGED'):workflow._load(filename)

    def test_regional_load_uses_consumer_without_calling_generic_loader_again(self):
        from unittest.mock import patch
        import generic_sprite_workflow as workflow
        import regional_video_workflow
        from sheet_workflow_state import encoded,scope_sha
        with tempfile.TemporaryDirectory(prefix='regional-facade-') as temp:
            root=Path(temp);filename=root/'run-manifest.json'
            scope=dict(run_id=root.name,source={},request={'route':'regional_video_inpaint_v1'},authorization={})
            m=dict(schema_version=3,attempts=[],authorization_scope_sha256=scope_sha(scope),**scope)
            filename.write_bytes(encoded(m));(root/'scope.json').write_bytes(encoded(scope));(root/'scope.sha256').write_text(scope_sha(scope)+'\n')
            with patch.object(regional_video_workflow,'validate_regional_attempt',return_value=(filename,m,{})) as validate:
                self.assertEqual((filename,m),workflow._load(filename))
                validate.assert_called_once_with(filename,require_artifacts=False)


if __name__ == "__main__":
    unittest.main()
