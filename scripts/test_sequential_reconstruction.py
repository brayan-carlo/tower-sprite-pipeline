import base64
import copy
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parent))
import sequential_reconstruction as seq


def required_external_file(environment_name: str) -> Path:
    value = os.environ.get(environment_name)
    if not value:
        raise RuntimeError(f"{environment_name}_REQUIRED")
    return Path(value).resolve(strict=True)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_relative_to(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
    except ValueError:
        return False
    return True


def opaque_count(image):
    return sum(1 for value in image.getchannel("A").get_flattened_data() if value)


def rectangle_fixture(box, color=(90, 120, 70, 255), size=(256, 416)):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste(color, box)
    return image


def trim_opaque_pixels(image, target):
    result = image.copy()
    pixels = result.load()
    bbox = result.getchannel("A").getbbox()
    remaining = opaque_count(result)
    for y in range(bbox[1] + 1, bbox[3] - 1):
        for x in range(bbox[0] + 1, bbox[2] - 1):
            if remaining <= target:
                return result
            pixels[x, y] = (0, 0, 0, 0)
            remaining -= 1
    if remaining != target:
        raise AssertionError(f"fixture could not reach {target} opaque pixels")
    return result


def hash_pattern_fixture(flips=0):
    values = [0] * 32 + [255] * 32
    for index in range(flips):
        values[index] = 255 - values[index]
    image = Image.new("RGBA", (8, 8), (0, 0, 0, 255))
    image.putdata([(value, value, value, 255) for value in values])
    return image


def foot_fixture(
    support_shift=0,
    detach_support=False,
    island_area=0,
    swing_lifted=False,
    screen_left_shift=0,
):
    image = Image.new("RGBA", (256, 416), (0, 0, 0, 0))
    color = (80, 110, 65, 255)
    image.paste(color, (50, 100, 206, 300))
    image.paste(color, (70 + screen_left_shift, 280, 90 + screen_left_shift, 405))
    screen_left_foot = (
        (65 + screen_left_shift, 380, 95 + screen_left_shift, 399)
        if swing_lifted
        else (65 + screen_left_shift, 398, 95 + screen_left_shift, 415)
    )
    image.paste(color, screen_left_foot)
    support_leg_bottom = 392 if detach_support else 405
    image.paste(color, (165, 280, 185, support_leg_bottom))
    image.paste(
        color,
        (160 + support_shift, 398, 190 + support_shift, 415),
    )
    if island_area:
        image.paste(color, (120, 390, 120 + island_area, 391))
    return image


def qa_manifest(identity_regions=None):
    return {
        "chroma_rgb": [255, 0, 255],
        "poses": [
            {"support_foot": "left"},
            {"support_foot": "left"},
            {"support_foot": "left"},
            {"support_foot": "left"},
            {"support_foot": "right"},
            {"support_foot": "right"},
            {"support_foot": "right"},
            {"support_foot": "right"},
        ],
        "identity_regions": identity_regions or {},
        "foot_regions": {
            "screen_left": {
                "bbox_ratio": [0.00, 0.75, 0.50, 1.00],
                "leg_corridor_ratio": [0.05, 0.45, 0.45, 0.92],
            },
            "screen_right": {
                "bbox_ratio": [0.50, 0.75, 1.00, 1.00],
                "leg_corridor_ratio": [0.55, 0.45, 0.95, 0.92],
            },
        },
        "qa": {
            "center_drift_px_max": 4,
            "opaque_area_ratio_min": 0.90,
            "opaque_area_ratio_max": 1.10,
            "bbox_ratio_delta_max": 0.05,
            "palette_colors": 64,
            "palette_rgb_distance_max": 48,
            "out_of_palette_ratio_max": 0.02,
            "new_alpha_components_max": 2,
            "orphan_area_ratio_max": 0.005,
            "new_foot_island_area_px_max": 3,
            "planted_foot_drift_px_max": 2,
            "chroma_rgb_distance_max": 24,
        },
    }


class SequentialManifestTests(unittest.TestCase):
    def setUp(self):
        self.pilot_path = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "pilots"
            / "ogre_walk_s_8f.json"
        )
        self.manifest = self.load_pilot()

    def load_pilot(self):
        return json.loads(self.pilot_path.read_text(encoding="utf-8"))

    def write_manifest(self, manifest):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        path = Path(temporary_directory.name) / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def write_manifest_without(self, field):
        manifest = copy.deepcopy(self.manifest)
        del manifest[field]
        return self.write_manifest(manifest)

    def test_manifest_accepts_only_the_approved_sequential_contract(self):
        validated = seq.validate_manifest(self.pilot_path)

        self.assertEqual("sequential_reconstruction", validated["mode"])
        self.assertEqual(
            (8, 256, 416),
            (
                validated["frame_count"],
                validated["frame_width"],
                validated["frame_height"],
            ),
        )
        self.assertEqual("fal-ai/qwen-image-2/edit", validated["primary_model"])
        self.assertEqual("fal-ai/nano-banana-2/edit", validated["fallback_model"])
        self.assertEqual(0.50, validated["max_budget_usd"])
        self.assertFalse(validated["root_motion"])
        self.assertEqual(8, len(validated["poses"]))
        self.assertEqual(24, validated["qa"]["chroma_rgb_distance_max"])

    def test_manifest_rejects_missing_security_or_spend_field(self):
        for field in (
            "max_budget_usd",
            "max_primary_submits",
            "max_fallback_submits",
        ):
            with self.subTest(field=field):
                path = self.write_manifest_without(field)
                with self.assertRaisesRegex(seq.PipelineBlocked, f"MISSING_FIELD:{field}"):
                    seq.validate_manifest(path)

    def test_manifest_rejects_missing_safety_or_qa_configuration(self):
        for field in ("poses", "identity_regions", "foot_regions", "qa"):
            with self.subTest(field=field):
                path = self.write_manifest_without(field)
                with self.assertRaisesRegex(seq.PipelineBlocked, f"MISSING_FIELD:{field}"):
                    seq.validate_manifest(path)

    def test_manifest_rejects_incompatible_pose_configuration(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["poses"][0]["stride"] = -0.9

        with self.assertRaisesRegex(seq.PipelineBlocked, "INCOMPATIBLE_FIELD:poses"):
            seq.validate_manifest(self.write_manifest(manifest))

    def test_manifest_rejects_incompatible_project_routes(self):
        for field, value in (
            ("project_root", "C:/UnityProjects/other"),
            ("source_path", "Assets/Art/GeneratedWork/Enemies/Ogro/Walk/source/Other.png"),
        ):
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest[field] = value
                with self.assertRaisesRegex(seq.PipelineBlocked, f"INCOMPATIBLE_FIELD:{field}"):
                    seq.validate_manifest(self.write_manifest(manifest))

    def test_manifest_rejects_incompatible_safety_or_qa_configuration(self):
        changes = {
            "identity_regions": ("head_face", "master_hamming_max", 11),
            "foot_regions": ("screen_left", "leg_corridor_ratio", [0.17, 0.50, 0.49, 0.96]),
            "qa": ("center_drift_px_max", None, 5),
        }

        for field, (region, setting, value) in changes.items():
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                if setting is None:
                    manifest[field][region] = value
                else:
                    manifest[field][region][setting] = value
                with self.assertRaisesRegex(seq.PipelineBlocked, f"INCOMPATIBLE_FIELD:{field}"):
                    seq.validate_manifest(self.write_manifest(manifest))


class WorkspacePreparationTests(unittest.TestCase):
    def setUp(self):
        self.pilot_path = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "pilots"
            / "ogre_walk_s_8f.json"
        )
        self.manifest = json.loads(self.pilot_path.read_text(encoding="utf-8"))
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name) / "project"
        self.manifest["project_root"] = str(self.project_root)
        self.source_path = self.project_root / self.manifest["source_path"]
        self.source_path.parent.mkdir(parents=True)
        shutil.copy2(
            required_external_file("TOWER_SPRITE_ORIGINAL_MASTER"), self.source_path
        )
        self.external_root = Path(self.temporary_directory.name) / "external-runs"
        self.run_id = "20260818-010000-ogre-walk-s-8f"

    def load_pilot(self):
        return copy.deepcopy(self.manifest)

    def test_prepare_creates_unique_external_layout_and_preserves_source(self):
        before = sha256(self.source_path)

        run_dir = seq.prepare_workspace(
            self.load_pilot(), self.external_root, self.run_id
        )

        self.assertFalse(is_relative_to(run_dir, self.project_root))
        self.assertEqual(before, sha256(self.source_path))
        with Image.open(run_dir / "input/master/master-technical.png") as master:
            self.assertEqual((512, 832), master.size)
        self.assertEqual(8, len(list((run_dir / "input/pose-guides").glob("*.png"))))

    def test_prepare_rejects_existing_run_or_workspace_inside_project(self):
        seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)

        with self.assertRaisesRegex(seq.PipelineBlocked, "RUN_ALREADY_EXISTS"):
            seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)
        with self.assertRaisesRegex(seq.PipelineBlocked, "WORKSPACE_INSIDE_PROJECT"):
            seq.prepare_workspace(
                self.load_pilot(),
                self.project_root / "tmp",
                "new-run",
            )
        with self.assertRaisesRegex(seq.PipelineBlocked, "RUN_OUTSIDE_WORKSPACE"):
            seq.prepare_workspace(self.load_pilot(), self.external_root, "../escape")

    def test_build_pose_guides_rejects_project_or_unprepared_run_before_write(self):
        empty_master = Image.new("RGBA", seq.NORMALIZED_SIZE)
        project_run = Path(self.manifest["project_root"]) / "task2-direct-build"

        with self.assertRaisesRegex(seq.PipelineBlocked, "WORKSPACE_INSIDE_PROJECT"):
            seq.build_pose_guides(self.load_pilot(), empty_master, project_run)
        self.assertFalse((project_run / "reports/pose_geometry.json").exists())

        unprepared_run = self.external_root / "unprepared"
        unprepared_run.mkdir(parents=True)
        with self.assertRaisesRegex(seq.PipelineBlocked, "RUN_NOT_PREPARED"):
            seq.build_pose_guides(self.load_pilot(), empty_master, unprepared_run)
        self.assertFalse((unprepared_run / "reports/pose_geometry.json").exists())

    def test_prepare_removes_failed_temporary_run_and_allows_same_id_retry(self):
        for failure_name, target, side_effect in (
            ("master", seq.Image.Image, OSError("injected master failure")),
            ("guides", seq, seq.PipelineBlocked("injected guide failure")),
        ):
            with self.subTest(failure=failure_name):
                run_id = f"{self.run_id}-{failure_name}"
                if failure_name == "master":
                    patcher = patch.object(target, "save", side_effect=side_effect)
                else:
                    patcher = patch.object(target, "build_pose_guides", side_effect=side_effect)

                with patcher:
                    with self.assertRaisesRegex(seq.PipelineBlocked if failure_name == "guides" else OSError, "injected"):
                        seq.prepare_workspace(self.load_pilot(), self.external_root, run_id)

                final_run = self.external_root / run_id
                self.assertFalse(final_run.exists())
                self.assertEqual(
                    [],
                    list(self.external_root.glob(f".{run_id}.preparing-*")),
                )
                self.assertEqual(
                    final_run,
                    seq.prepare_workspace(self.load_pilot(), self.external_root, run_id),
                )

    def test_prepare_persists_symmetric_pose_geometry_before_generation(self):
        run_dir = seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)
        prepared = json.loads(
            (run_dir / "reports/run_manifest.json").read_text(encoding="utf-8")
        )

        self.assertIn("chroma_rgb", prepared)
        self.assertNotIn("selected_chroma", prepared)
        self.assertIn("normalized_master", prepared)
        self.assertIn("resolved_regions", prepared)
        self.assertIn("skeleton", prepared)
        self.assertEqual(8, len(prepared["guides"]))
        self.assertEqual(
            ["left_contact", "left_down", "left_pass", "left_up", "right_contact", "right_down", "right_pass", "right_up"],
            [guide["name"] for guide in prepared["guides"]],
        )
        self.assertEqual(
            prepared["skeleton"]["root"],
            prepared["guides"][0]["root"],
        )
        self.assertEqual(
            [-1.0, 1.0, 0.0],
            prepared["guides"][0]["multipliers"],
        )
        self.assertEqual(
            [1.0, -1.0, 0.0],
            prepared["guides"][4]["multipliers"],
        )
        self.assertEqual(
            8,
            len({sha256(run_dir / guide["path"]) for guide in prepared["guides"]}),
        )

    def test_pose_guides_persist_and_hold_the_anatomical_support_anchor_per_block(self):
        run_dir = seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)
        prepared = json.loads(
            (run_dir / "reports/run_manifest.json").read_text(encoding="utf-8")
        )

        expected_regions = ["screen_right"] * 4 + ["screen_left"] * 4
        for guide, expected_region in zip(prepared["guides"], expected_regions):
            self.assertIn("support_region", guide)
            self.assertIn("coordinates", guide)
            self.assertIn("support_anchor", guide)
            self.assertEqual(expected_region, guide["support_region"])
            self.assertEqual(
                guide["coordinates"][f"{expected_region}_foot"],
                guide["support_anchor"],
            )

        for block in (prepared["guides"][:4], prepared["guides"][4:]):
            anchors = [guide["support_anchor"] for guide in block]
            baseline = anchors[0]
            self.assertLessEqual(
                max(math.dist(baseline, anchor) for anchor in anchors),
                2.0,
            )

        self.assertGreater(
            len(
                {
                    tuple(guide["coordinates"]["screen_left_foot"])
                    for guide in prepared["guides"][:4]
                }
            ),
            1,
        )
        self.assertGreater(
            len(
                {
                    tuple(guide["coordinates"]["screen_right_foot"])
                    for guide in prepared["guides"][4:]
                }
            ),
            1,
        )

    def test_prepare_persists_the_validated_input_for_runtime_qa(self):
        run_dir = seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)
        prepared = json.loads(
            (run_dir / "reports/run_manifest.json").read_text(encoding="utf-8")
        )

        self.assertIn("normalized_input", prepared)
        self.assertEqual(self.manifest, prepared.get("normalized_input"))
        self.assertEqual([255, 0, 255], prepared["chroma_rgb"])

    def test_prepare_persists_confined_orchestration_paths_and_seeds(self):
        run_dir = seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)
        prepared = json.loads(
            (run_dir / "reports/run_manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(str(run_dir.resolve()), prepared["run_dir"])
        self.assertEqual("input/master/master-technical.png", prepared["technical_master"])
        self.assertEqual(
            [guide["path"] for guide in prepared["guides"]],
            prepared["pose_guides"],
        )
        self.assertEqual(8, len(prepared["frame_seeds"]))
        self.assertEqual(8, len(set(prepared["frame_seeds"])))
        self.assertEqual([], prepared["frames"])
        self.assertEqual([], prepared["requests"])
        self.assertEqual(0, prepared["last_accepted_frame"])
        with Image.open(run_dir / prepared["technical_master"]) as technical:
            self.assertEqual("RGB", technical.mode)
            self.assertEqual(tuple(prepared["chroma_rgb"]), technical.getpixel((0, 0)))

    def test_pose_guides_keep_all_labeled_joints_inside_the_technical_canvas(self):
        run_dir = seq.prepare_workspace(self.load_pilot(), self.external_root, self.run_id)
        prepared = json.loads(
            (run_dir / "reports/run_manifest.json").read_text(encoding="utf-8")
        )
        chroma = tuple(prepared["chroma_rgb"])

        for guide in prepared["guides"]:
            with self.subTest(guide=guide["name"]):
                with Image.open(run_dir / guide["path"]) as image:
                    non_chroma = [
                        (x, y)
                        for y in range(image.height)
                        for x in range(image.width)
                        if image.getpixel((x, y)) != chroma
                    ]
                self.assertTrue(non_chroma)
                self.assertGreater(min(x for x, _ in non_chroma), 0)
                self.assertLess(max(x for x, _ in non_chroma), image.width - 1)
                self.assertGreater(min(y for _, y in non_chroma), 0)
                self.assertLess(max(y for _, y in non_chroma), image.height - 1)


class FrameNormalizationAndQATests(unittest.TestCase):
    def setUp(self):
        self.manifest = qa_manifest()

    def test_recover_alpha_removes_only_edge_connected_chroma(self):
        raw = Image.new("RGB", (7, 7), (255, 0, 255))
        raw.paste((40, 90, 30), (1, 1, 6, 6))
        raw.putpixel((3, 3), (255, 0, 255))

        recovered = seq.recover_alpha(raw, (255, 0, 255))

        self.assertEqual(0, recovered.getpixel((0, 0))[3])
        self.assertEqual(255, recovered.getpixel((3, 3))[3])

    def test_recover_alpha_uses_the_literal_24_rgb_distance_boundary(self):
        raw = Image.new("RGB", (5, 5), (231, 0, 255))
        raw.putpixel((2, 2), (230, 0, 255))

        recovered = seq.recover_alpha(raw, (255, 0, 255))

        self.assertEqual(0, recovered.getpixel((0, 0))[3])
        self.assertEqual(255, recovered.getpixel((2, 2))[3])

    def test_recover_alpha_rejects_an_empty_subject(self):
        raw = Image.new("RGB", (5, 5), (255, 0, 255))

        with self.assertRaisesRegex(seq.PipelineBlocked, "EMPTY_ALPHA"):
            seq.recover_alpha(raw, (255, 0, 255))

    def test_normalize_frame_reduces_exactly_two_to_one_and_places_ground_at_415(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        raw_path = Path(temporary_directory.name) / "raw.png"
        raw = Image.new("RGB", (512, 832), (255, 0, 255))
        raw.paste((60, 100, 45), (196, 400, 316, 830))
        raw.save(raw_path)

        normalized = seq.normalize_frame(raw_path, {"chroma_rgb": [255, 0, 255]})

        self.assertEqual((256, 416), normalized.size)
        self.assertEqual("RGBA", normalized.mode)
        self.assertEqual(415, normalized.getchannel("A").getbbox()[3])
        self.assertEqual(0, normalized.getpixel((0, 415))[3])

    def test_normalize_frame_rejects_a_nontechnical_raw_size(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        raw_path = Path(temporary_directory.name) / "raw.png"
        Image.new("RGB", (256, 416), (255, 0, 255)).save(raw_path)

        with self.assertRaisesRegex(seq.PipelineBlocked, "RAW_SIZE_MISMATCH"):
            seq.normalize_frame(raw_path, {"chroma_rgb": [255, 0, 255]})

    def test_geometry_center_drift_accepts_four_and_rejects_five_pixels(self):
        master = rectangle_fixture((100, 390, 140, 415))
        at_limit = rectangle_fixture((104, 390, 144, 415))
        outside = rectangle_fixture((105, 390, 145, 415))

        accepted = seq.geometry_checks(at_limit, master, self.manifest)
        rejected = seq.geometry_checks(outside, master, self.manifest)

        self.assertEqual(4.0, accepted["measurements"]["center_drift_px"])
        self.assertNotIn("CENTER_DRIFT", accepted["failures"])
        self.assertEqual(5.0, rejected["measurements"]["center_drift_px"])
        self.assertIn("CENTER_DRIFT", rejected["failures"])

    def test_geometry_area_accepts_90_percent_and_rejects_89_point_9(self):
        master = rectangle_fixture((100, 390, 140, 415))
        at_limit = trim_opaque_pixels(master, 900)
        outside = trim_opaque_pixels(master, 899)

        accepted = seq.geometry_checks(at_limit, master, self.manifest)
        rejected = seq.geometry_checks(outside, master, self.manifest)

        self.assertEqual(0.9, accepted["measurements"]["opaque_area_ratio"])
        self.assertNotIn("OPAQUE_AREA_RATIO", accepted["failures"])
        self.assertEqual(0.899, rejected["measurements"]["opaque_area_ratio"])
        self.assertIn("OPAQUE_AREA_RATIO", rejected["failures"])

    def test_geometry_area_accepts_110_percent_and_rejects_110_point_1(self):
        master = rectangle_fixture((100, 390, 140, 415))
        at_limit = rectangle_fixture((98, 390, 142, 415))
        outside = trim_opaque_pixels(rectangle_fixture((97, 390, 142, 415)), 1101)

        accepted = seq.geometry_checks(at_limit, master, self.manifest)
        rejected = seq.geometry_checks(outside, master, self.manifest)

        self.assertEqual(1.1, accepted["measurements"]["opaque_area_ratio"])
        self.assertNotIn("OPAQUE_AREA_RATIO", accepted["failures"])
        self.assertEqual(1.101, rejected["measurements"]["opaque_area_ratio"])
        self.assertIn("OPAQUE_AREA_RATIO", rejected["failures"])

    def test_geometry_bbox_accepts_five_percent_and_rejects_five_point_one(self):
        master_at_limit = rectangle_fixture((28, 390, 228, 415))
        at_limit = rectangle_fixture((33, 390, 223, 415))
        master_outside = rectangle_fixture((0, 390, 255, 415))
        outside = rectangle_fixture((6, 390, 248, 415))

        accepted = seq.geometry_checks(at_limit, master_at_limit, self.manifest)
        rejected = seq.geometry_checks(outside, master_outside, self.manifest)

        self.assertAlmostEqual(
            0.05,
            accepted["measurements"]["bbox_width_ratio_delta"],
            places=12,
        )
        self.assertNotIn("BBOX_RATIO_DELTA", accepted["failures"])
        self.assertEqual(
            0.051,
            round(rejected["measurements"]["bbox_width_ratio_delta"], 3),
        )
        self.assertIn("BBOX_RATIO_DELTA", rejected["failures"])

    def test_palette_accepts_two_percent_and_rejects_two_point_one(self):
        master = rectangle_fixture((100, 390, 140, 415), color=(90, 90, 90, 255))
        at_limit = master.copy()
        outside = master.copy()
        for index in range(21):
            point = (101 + index % 20, 391 + index // 20)
            outside.putpixel(point, (255, 0, 0, 255))
            if index < 20:
                at_limit.putpixel(point, (255, 0, 0, 255))

        accepted = seq.palette_checks(at_limit, master, self.manifest)
        rejected = seq.palette_checks(outside, master, self.manifest)

        self.assertEqual(0.02, accepted["measurements"]["out_of_palette_ratio"])
        self.assertNotIn("OUT_OF_PALETTE", accepted["failures"])
        self.assertEqual(0.021, rejected["measurements"]["out_of_palette_ratio"])
        self.assertIn("OUT_OF_PALETTE", rejected["failures"])

    def test_palette_fails_closed_when_strict_pilot_has_no_runtime_chroma(self):
        pilot_path = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "pilots"
            / "ogre_walk_s_8f.json"
        )
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
        master = rectangle_fixture((100, 390, 140, 415))

        report = seq.palette_checks(master, master, pilot)

        self.assertIn("CHROMA_NOT_CONFIGURED", report["failures"])

    def test_prepared_runtime_shape_detects_chroma_halo_below_palette_limit(self):
        master = rectangle_fixture((100, 390, 140, 415), color=(90, 90, 90, 255))
        frame = master.copy()
        frame.putpixel((110, 400), (255, 0, 255, 255))
        prepared = {
            "normalized_input": self.manifest,
            "chroma_rgb": [255, 0, 255],
        }

        try:
            report = seq.palette_checks(frame, master, prepared)
        except KeyError as error:
            self.fail(f"prepared runtime shape was not accepted: {error}")

        self.assertEqual(0.001, report["measurements"]["out_of_palette_ratio"])
        self.assertNotIn("OUT_OF_PALETTE", report["failures"])
        self.assertIn("CHROMA_HALO", report["failures"])

    def test_evaluate_frame_accepts_the_prepared_runtime_shape(self):
        frame = foot_fixture()
        prepared = {
            "normalized_input": self.manifest,
            "chroma_rgb": [255, 0, 255],
        }

        try:
            report = seq.evaluate_frame(frame, frame, None, prepared, 1)
        except KeyError as error:
            self.fail(f"prepared runtime shape was not accepted: {error}")

        self.assertTrue(report["passed"])
        self.assertEqual("ACCEPTED", report["state"])

    def test_orphan_area_accepts_point_five_and_rejects_point_five_one_percent(self):
        master = rectangle_fixture((50, 315, 150, 415))
        at_limit = master.copy()
        at_limit.paste((90, 120, 70, 255), (180, 350, 185, 360))
        outside = master.copy()
        outside.paste((90, 120, 70, 255), (180, 350, 183, 367))

        accepted = seq.component_checks(at_limit, master, self.manifest)
        rejected = seq.component_checks(outside, master, self.manifest)

        self.assertEqual(0.005, accepted["measurements"]["orphan_area_ratio"])
        self.assertNotIn("ORPHAN_ALPHA_AREA", accepted["failures"])
        self.assertEqual(0.0051, rejected["measurements"]["orphan_area_ratio"])
        self.assertIn("ORPHAN_ALPHA_AREA", rejected["failures"])

    def test_replacement_island_is_unmatched_even_when_component_count_is_unchanged(self):
        master = rectangle_fixture((50, 315, 150, 415))
        master.paste((90, 120, 70, 255), (180, 350, 186, 360))
        frame = rectangle_fixture((50, 315, 150, 415))
        frame.paste((90, 120, 70, 255), (20, 350, 26, 360))

        report = seq.component_checks(frame, master, self.manifest)

        self.assertEqual(2, report["measurements"]["master_components"])
        self.assertEqual(2, report["measurements"]["frame_components"])
        self.assertEqual(1, report["measurements"]["new_components"])
        self.assertEqual(60, report["measurements"]["orphan_area_px"])
        self.assertEqual(0.006, report["measurements"]["orphan_area_ratio"])
        self.assertIn("ORPHAN_ALPHA_AREA", report["failures"])

    def test_component_correspondence_accepts_overlap_with_previous_accepted_alpha(self):
        master = rectangle_fixture((10, 390, 20, 415))
        previous = rectangle_fixture((30, 390, 40, 415))
        frame = rectangle_fixture((31, 390, 41, 415))

        try:
            report = seq.component_checks(frame, master, self.manifest, previous)
        except TypeError as error:
            self.fail(f"previous accepted alpha was not supported: {error}")

        self.assertEqual(0, report["measurements"]["new_components"])
        self.assertEqual(0, report["measurements"]["orphan_area_px"])

    def test_component_count_gate_rejects_fragmentation_inside_the_master_silhouette(self):
        master = rectangle_fixture((40, 360, 200, 415))
        frame = Image.new("RGBA", master.size, (0, 0, 0, 0))
        for left in (45, 85, 125, 165):
            frame.paste((90, 120, 70, 255), (left, 370, left + 20, 410))

        report = seq.component_checks(frame, master, self.manifest)

        self.assertEqual(1, report["measurements"]["master_components"])
        self.assertEqual(4, report["measurements"]["frame_components"])
        self.assertEqual(0, report["measurements"]["new_components"])
        self.assertIn("component_count_limit", report["measurements"])
        self.assertEqual(3, report["measurements"]["component_count_limit"])
        self.assertIn("NEW_ALPHA_COMPONENTS", report["failures"])

    def test_identity_master_hamming_boundaries_are_literal(self):
        cases = (
            ("head_face", 10, 11),
            ("chest", 14, 15),
        )
        for region_name, at_limit, outside in cases:
            with self.subTest(region=region_name):
                manifest = qa_manifest(
                    {
                        region_name: {
                            "bbox_ratio": [0, 0, 1, 1],
                            "master_hamming_max": at_limit,
                            "previous_hamming_max": 63,
                        }
                    }
                )
                accepted = seq.identity_checks(
                    hash_pattern_fixture(at_limit),
                    hash_pattern_fixture(),
                    None,
                    manifest,
                )
                rejected = seq.identity_checks(
                    hash_pattern_fixture(outside),
                    hash_pattern_fixture(),
                    None,
                    manifest,
                )
                self.assertEqual(
                    at_limit,
                    accepted["regions"][region_name]["master_hamming"],
                )
                self.assertNotIn("IDENTITY_MASTER_HAMMING", accepted["failures"])
                self.assertEqual(
                    outside,
                    rejected["regions"][region_name]["master_hamming"],
                )
                self.assertIn("IDENTITY_MASTER_HAMMING", rejected["failures"])

    def test_identity_previous_hamming_boundaries_are_literal(self):
        cases = (
            ("head_face", 8, 9),
            ("chest", 12, 13),
        )
        for region_name, at_limit, outside in cases:
            with self.subTest(region=region_name):
                manifest = qa_manifest(
                    {
                        region_name: {
                            "bbox_ratio": [0, 0, 1, 1],
                            "master_hamming_max": 63,
                            "previous_hamming_max": at_limit,
                        }
                    }
                )
                frame = hash_pattern_fixture()
                accepted = seq.identity_checks(
                    frame, hash_pattern_fixture(), hash_pattern_fixture(at_limit), manifest
                )
                rejected = seq.identity_checks(
                    frame, hash_pattern_fixture(), hash_pattern_fixture(outside), manifest
                )
                self.assertEqual(
                    at_limit,
                    accepted["regions"][region_name]["previous_hamming"],
                )
                self.assertNotIn("IDENTITY_PREVIOUS_HAMMING", accepted["failures"])
                self.assertEqual(
                    outside,
                    rejected["regions"][region_name]["previous_hamming"],
                )
                self.assertIn("IDENTITY_PREVIOUS_HAMMING", rejected["failures"])

    def test_rejected_frame_cannot_advance_chain(self):
        previous = foot_fixture()
        detached_foot = foot_fixture(detach_support=True)

        report = seq.evaluate_frame(
            detached_foot, previous, previous, self.manifest, 3
        )

        self.assertFalse(report["passed"])
        self.assertIn("FOOT_DISCONNECTED", report["failures"])
        self.assertFalse(seq.accept_frame(report))

    def test_planted_foot_may_move_at_most_two_pixels_with_anatomical_mapping(self):
        previous = foot_fixture()
        at_limit = seq.foot_checks(foot_fixture(support_shift=2), previous, self.manifest, 2)
        outside = seq.foot_checks(foot_fixture(support_shift=3), previous, self.manifest, 2)

        self.assertEqual(
            {"left": "screen_right", "right": "screen_left"},
            at_limit["anatomical_to_screen_region"],
        )
        self.assertEqual("screen_right", at_limit["support_region"])
        self.assertEqual(2.0, at_limit["measurements"]["planted_foot_drift_px"])
        self.assertNotIn("PLANTED_FOOT_DRIFT", at_limit["failures"])
        self.assertEqual(3.0, outside["measurements"]["planted_foot_drift_px"])
        self.assertIn("PLANTED_FOOT_DRIFT", outside["failures"])

    def test_swing_foot_may_lift_when_it_remains_connected_to_its_leg_corridor(self):
        frame = foot_fixture(swing_lifted=True)

        report = seq.foot_checks(frame, None, self.manifest, 3)

        self.assertNotIn("FOOT_DISCONNECTED", report["failures"])
        self.assertTrue(report["regions"]["screen_left"]["passed"])
        self.assertIsNone(report["regions"]["screen_left"]["anchor"])

    def test_frame_five_starts_a_new_support_baseline_instead_of_comparing_frame_four(self):
        previous_frame_four = foot_fixture()
        frame_five = foot_fixture(screen_left_shift=10)

        report = seq.foot_checks(frame_five, previous_frame_four, self.manifest, 5)

        self.assertEqual("right", report["support_foot"])
        self.assertIn("previous_support_foot", report)
        self.assertEqual("left", report["previous_support_foot"])
        self.assertIsNone(report["measurements"]["planted_foot_drift_px"])
        self.assertNotIn("PLANTED_FOOT_DRIFT", report["failures"])

    def test_new_foot_island_allows_three_pixels_and_rejects_four(self):
        previous = foot_fixture()
        at_limit = seq.foot_checks(foot_fixture(island_area=3), previous, self.manifest, 2)
        outside = seq.foot_checks(foot_fixture(island_area=4), previous, self.manifest, 2)

        self.assertNotIn("NEW_FOOT_ISLAND", at_limit["failures"])
        self.assertIn("NEW_FOOT_ISLAND", outside["failures"])

    def test_evaluate_frame_accepts_only_a_machine_readable_clean_report(self):
        frame = foot_fixture()

        report = seq.evaluate_frame(frame, frame, None, self.manifest, 1)

        self.assertEqual(1, report["frame"])
        self.assertEqual("ACCEPTED", report["state"])
        self.assertTrue(report["passed"])
        self.assertTrue(seq.accept_frame(report))

    def test_sequence_requires_eight_nonempty_distinct_frames(self):
        frames = []
        for index in range(8):
            frame = Image.new("RGBA", (8, 1), (0, 0, 0, 0))
            frame.putpixel((index, 0), (255, 255, 255, 255))
            frames.append(frame)

        accepted = seq.evaluate_sequence(frames)
        duplicate = seq.evaluate_sequence(frames[:-1] + [frames[-2]])
        empty = seq.evaluate_sequence(frames[:-1] + [Image.new("RGBA", (8, 1))])
        short = seq.evaluate_sequence(frames[:-1])

        self.assertTrue(accepted["passed"])
        self.assertEqual("DRAFT", accepted["state"])
        self.assertIn("DUPLICATE_FRAME_HASH", duplicate["failures"])
        self.assertEqual("TECHNICAL_REJECTED", duplicate["state"])
        self.assertIn("EMPTY_SEQUENCE_FRAME", empty["failures"])
        self.assertIn("FRAME_COUNT_MISMATCH", short["failures"])

    def test_sequence_rejects_a_loop_stronger_than_any_consecutive_difference(self):
        frames = []
        for opaque_width in range(1, 9):
            frame = Image.new("RGBA", (8, 1), (0, 0, 0, 0))
            frame.paste((255, 255, 255, 255), (0, 0, opaque_width, 1))
            frames.append(frame)

        report = seq.evaluate_sequence(frames)

        self.assertEqual([1, 1, 1, 1, 1, 1, 1], report["checks"]["consecutive_differences"])
        self.assertEqual(7, report["checks"]["closing_difference"])
        self.assertIn("LOOP_CLOSURE", report["failures"])
        self.assertEqual("TECHNICAL_REJECTED", report["state"])


class AcceptedFrameOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.run_dir = Path(self.temporary_directory.name) / "prepared-run"
        for relative in seq.RUN_DIRS:
            (self.run_dir / relative).mkdir(parents=True, exist_ok=True)
        self.manifest = qa_manifest()
        self.manifest.update(
            {
                "frame_count": 8,
                "frame_width": 256,
                "frame_height": 416,
                "technical_scale": 2,
                "primary_model": "fal-ai/qwen-image-2/edit",
                "fallback_model": "fal-ai/nano-banana-2/edit",
                "max_primary_submits": 10,
                "max_fallback_submits": 2,
            }
        )
        project = Path("C:/UnityProjects/tower1").resolve()
        marker = {
            "format": "tower-sprite-pipeline-prepared-run-v1",
            "project_root": str(project),
            "workspace_root": str(self.run_dir.parent.resolve()),
            "state": "PREPARING",
        }
        (self.run_dir / seq.PREPARED_RUN_MARKER).write_text(
            json.dumps(marker), encoding="utf-8"
        )
        self.manifest["project_root"] = str(project)
        self.master = foot_fixture()
        self._save_reference(
            "input/master/master-technical.png",
            self.master.resize(seq.TECHNICAL_SIZE, Image.Resampling.NEAREST),
        )
        self.pose_paths = []
        for index in range(1, 9):
            pose = Image.new("RGB", seq.TECHNICAL_SIZE, (index, 20, 30))
            relative = f"input/pose-guides/{index:02d}-pose.png"
            self._save_reference(relative, pose)
            self.pose_paths.append(relative)
        self.run = {
            "run_id": "unit-orchestration",
            "run_dir": str(self.run_dir.resolve()),
            "state": "DRAFT",
            "project_root": str(project),
            "chroma_rgb": [255, 0, 255],
            "normalized_input": copy.deepcopy(self.manifest),
            "technical_master": "input/master/master-technical.png",
            "pose_guides": list(self.pose_paths),
            "frame_seeds": [71001 + index for index in range(8)],
            "frames": [],
            "requests": [],
            "last_accepted_frame": 0,
        }
        self.run_manifest_path = self.run_dir / "reports/run_manifest.json"
        self.pilot_path = (
            Path(__file__).resolve().parents[1]
            / "assets/pilots/ogre_walk_s_8f.json"
        )
        self._persist_run()

    def _save_reference(self, relative, image):
        destination = self.run_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        return destination

    def _persist_run(self):
        self.run_manifest_path.write_text(
            json.dumps(self.run, indent=2) + "\n", encoding="utf-8"
        )

    def _raw_result(self, relative, frame=None):
        frame = frame or self.master
        raw = Image.new("RGB", seq.TECHNICAL_SIZE, (255, 0, 255))
        opaque = frame.resize(seq.TECHNICAL_SIZE, Image.Resampling.NEAREST)
        raw.paste(opaque.convert("RGB"), mask=opaque.getchannel("A"))
        return self._save_reference(relative, raw)

    def _model_native_raw_result(self, relative, size, frame=None, format_name="PNG"):
        frame = frame or self.master
        raw = Image.new("RGB", size, tuple(self.run["chroma_rgb"]))
        opaque = frame.resize(size, Image.Resampling.NEAREST)
        raw.paste(opaque.convert("RGB"), mask=opaque.getchannel("A"))
        destination = self.run_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw.save(destination, format=format_name)
        return destination

    def _create_directory_link(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        self.assertTrue(link.is_dir())
        self.assertEqual(target.resolve(), link.resolve())

    def _run_request_cli(self, output, expected_code):
        pilot = json.loads(self.pilot_path.read_text(encoding="utf-8"))
        self.run["normalized_input"] = pilot
        self._persist_run()
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(seq.__file__).resolve()),
                "next-request",
                "--manifest",
                str(self.pilot_path),
                "--run-manifest",
                str(self.run_manifest_path),
                "--request-output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(expected_code, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def _data_uri_bytes(self, value):
        prefix = "data:image/png;base64,"
        self.assertTrue(value.startswith(prefix))
        return base64.b64decode(value[len(prefix):], validate=True)

    def _request_record(
        self, frame_index, model, result_path, status="COMPLETED", fingerprint=None
    ):
        attempt = len(self.run["requests"]) + 1
        fingerprint = fingerprint or hashlib.sha256(
            f"request:{frame_index}:{model}:{attempt}".encode("utf-8")
        ).hexdigest()
        record = {
            "frame_index": frame_index,
            "attempt": attempt,
            "model": model,
            "request_id": f"req-{attempt}",
            "request_fingerprint": fingerprint,
            "estimated_cost_usd": 0.01,
            "status": status,
            "submitted_at": "2026-08-19T12:00:00Z",
            "result_path": result_path,
        }
        result = self.run_dir / result_path if result_path else None
        if result is not None and result.is_file():
            record["result_sha256"] = sha256(result)
        self.run["next_request"] = {
            "fingerprint": fingerprint,
            "frame_index": frame_index,
            "model": model,
        }
        return record

    def _accepted_frame(self, index, technical=None, attempts=None):
        normalized = f"normalized/frames/{index:02d}.png"
        technical = technical or f"normalized/frames/{index:02d}-technical.png"
        frame = self.master.copy()
        frame.putpixel((10 + index, 200), (90, 120, 70, 255))
        self._save_reference(normalized, frame)
        self._save_reference(
            technical, frame.resize(seq.TECHNICAL_SIZE, Image.Resampling.NEAREST)
        )
        record = {
            "frame_index": index,
            "state": "ACCEPTED",
            "passed": True,
            "normalized_path": normalized,
            "technical_path": technical,
            "qa_report_path": f"reports/frame-{index:02d}-qa.json",
            "attempts": attempts or [],
        }
        self.run["frames"] = [
            item for item in self.run["frames"] if item["frame_index"] != index
        ] + [record]
        self.run["frames"].sort(key=lambda item: item["frame_index"])
        self.run["last_accepted_frame"] = max(self.run["last_accepted_frame"], index)
        return record

    def _rejected_frame(self, index, raw_path, model=None):
        model = model or self.manifest["primary_model"]
        record = {
            "frame_index": index,
            "state": "REJECTED",
            "passed": False,
            "qa_report_path": f"reports/frame-{index:02d}-qa.json",
            "attempts": [
                {
                    "raw_path": raw_path,
                    "model": model,
                    "state": "REJECTED",
                    "failures": ["FOOT_DISCONNECTED"],
                }
            ],
        }
        self.run["frames"] = [
            item for item in self.run["frames"] if item["frame_index"] != index
        ] + [record]
        self.run["frames"].sort(key=lambda item: item["frame_index"])
        return record

    def test_frame_one_uses_exact_master_and_pose_png_bytes_with_literal_request_fields(self):
        request = seq.next_request(self.run, self.manifest)

        self.assertEqual(["master", "pose"], request["reference_roles"])
        self.assertEqual(
            ["input/master/master-technical.png", self.pose_paths[0]],
            request["reference_paths"],
        )
        self.assertEqual(
            (self.run_dir / request["reference_paths"][0]).read_bytes(),
            self._data_uri_bytes(request["input"]["image_urls"][0]),
        )
        self.assertEqual(
            (self.run_dir / request["reference_paths"][1]).read_bytes(),
            self._data_uri_bytes(request["input"]["image_urls"][1]),
        )
        self.assertEqual({"width": 512, "height": 832}, request["input"]["image_size"])
        self.assertEqual(1, request["input"]["num_images"])
        self.assertEqual("png", request["input"]["output_format"])
        self.assertIs(False, request["input"]["enable_prompt_expansion"])
        self.assertEqual(71001, request["input"]["seed"])
        self.assertLess(len(request["input"]["prompt"]), 800)
        self.assertIn("master", request["input"]["prompt"].lower())
        self.assertIn("pose", request["input"]["prompt"].lower())
        self.assertNotIn("data:image", json.dumps(self.run))

    def test_request_uses_the_run_chroma_literal_and_fingerprints_that_prompt(self):
        self.run["chroma_rgb"] = [1, 2, 3]
        first = seq.next_request(self.run, self.manifest)
        first_output = self.run_dir / "input/references/frame-01-first-chroma.json"
        seq._write_request_output(first_output, first, self.run_dir)
        first_transport = json.loads(first_output.read_text(encoding="utf-8"))

        self.assertIn(
            "solid uniform chroma background RGB(1, 2, 3)/#010203, no gradient",
            first_transport["input"]["prompt"],
        )
        self.assertNotIn("#FF00FF", first_transport["input"]["prompt"])

        self.run["chroma_rgb"] = [255, 0, 255]
        second = seq.next_request(self.run, self.manifest)
        second_output = self.run_dir / "input/references/frame-01-second-chroma.json"
        seq._write_request_output(second_output, second, self.run_dir)
        second_transport = json.loads(second_output.read_text(encoding="utf-8"))

        self.assertIn(
            "solid uniform chroma background RGB(255, 0, 255)/#FF00FF, no gradient",
            second_transport["input"]["prompt"],
        )
        self.assertNotEqual(
            first_transport["request_fingerprint"],
            second_transport["request_fingerprint"],
        )

    def test_frame_two_uses_master_previous_accepted_and_pose_in_byte_order(self):
        previous = self._accepted_frame(1)

        request = seq.next_request(self.run, self.manifest)

        self.assertEqual(2, request["frame_index"])
        self.assertEqual(
            ["master", "previous_accepted", "pose"], request["reference_roles"]
        )
        self.assertEqual(previous["technical_path"], request["reference_paths"][1])
        for relative, encoded in zip(
            request["reference_paths"], request["input"]["image_urls"]
        ):
            self.assertEqual(
                hashlib.sha256((self.run_dir / relative).read_bytes()).digest(),
                hashlib.sha256(self._data_uri_bytes(encoded)).digest(),
            )

    def test_rejected_frame_is_retried_and_never_becomes_previous_reference(self):
        accepted = self._accepted_frame(1)
        rejected_raw = "raw/frame-02/rejected.png"
        self._save_reference(
            rejected_raw,
            foot_fixture(detach_support=True).resize(
                seq.TECHNICAL_SIZE, Image.Resampling.NEAREST
            ),
        )
        self.run["requests"].append(
            self._request_record(2, self.manifest["primary_model"], rejected_raw)
        )
        self._rejected_frame(2, rejected_raw)

        request = seq.next_request(self.run, self.manifest)

        self.assertEqual(2, request["frame_index"])
        self.assertEqual(self.manifest["primary_model"], request["model"])
        self.assertEqual(accepted["technical_path"], request["reference_paths"][1])
        self.assertNotIn(rejected_raw, request["reference_paths"])

    def test_retry_policy_uses_one_qwen_retry_then_nano_and_respects_global_caps(self):
        for index in (1, 2):
            raw_base = f"raw/frame-{index:02d}/base.png"
            raw_retry = f"raw/frame-{index:02d}/retry.png"
            self.run["requests"].append(
                self._request_record(index, self.manifest["primary_model"], raw_base)
            )
            self.run["requests"].append(
                self._request_record(index, self.manifest["primary_model"], raw_retry)
            )
            self._accepted_frame(
                index,
                attempts=[
                    {"raw_path": raw_base, "model": self.manifest["primary_model"], "state": "REJECTED", "failures": ["QA"]},
                    {"raw_path": raw_retry, "model": self.manifest["primary_model"], "state": "ACCEPTED", "failures": []},
                ],
            )
        raw_three = "raw/frame-03/base.png"
        self.run["requests"].append(
            self._request_record(3, self.manifest["primary_model"], raw_three)
        )
        self._rejected_frame(3, raw_three)

        fallback = seq.next_request(self.run, self.manifest)

        self.assertEqual(self.manifest["fallback_model"], fallback["model"])
        self.assertEqual("0.5K", fallback["input"]["resolution"])

    def test_terminal_exhaustion_sets_technical_rejected_without_another_request(self):
        for index in (1, 2):
            raw_base = f"raw/frame-{index:02d}/base.png"
            raw_retry = f"raw/frame-{index:02d}/retry.png"
            raw_nano = f"raw/frame-{index:02d}/nano.png"
            for model, raw in (
                (self.manifest["primary_model"], raw_base),
                (self.manifest["primary_model"], raw_retry),
                (self.manifest["fallback_model"], raw_nano),
            ):
                self.run["requests"].append(self._request_record(index, model, raw))
            self._accepted_frame(index)
        raw_three = "raw/frame-03/base.png"
        self.run["requests"].append(
            self._request_record(3, self.manifest["primary_model"], raw_three)
        )
        self._rejected_frame(3, raw_three)

        request = seq.next_request(self.run, self.manifest)

        self.assertIsNone(request)
        self.assertEqual("TECHNICAL_REJECTED", self.run["state"])
        self.assertEqual("ATTEMPT_POLICY_EXHAUSTED", self.run["terminal_reason"])

    def test_pending_request_is_rebuilt_idempotently_but_completed_raw_requires_evaluation(self):
        pending = self._request_record(
            1, self.manifest["primary_model"], None, status="IN_PROGRESS"
        )
        self.run["requests"].append(pending)

        first = seq.next_request(self.run, self.manifest)
        second = seq.next_request(self.run, self.manifest)

        self.assertEqual(first, second)
        pending["status"] = "COMPLETED"
        pending["result_path"] = "raw/frame-01/result.png"
        self.assertIsNone(seq.next_request(self.run, self.manifest))
        self.assertEqual("DRAFT", self.run["state"])

    def test_record_evaluation_writes_frame_report_before_manifest_and_resumes_idempotently(self):
        raw_relative = "raw/frame-01/result.png"
        raw_path = self._raw_result(raw_relative)
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 1, raw_path)
        report_hash = sha256(self.run_dir / "reports/frame-01-qa.json")
        manifest_hash = sha256(self.run_manifest_path)
        resumed = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertTrue(report["passed"])
        self.assertEqual(report, resumed)
        self.assertEqual(report_hash, sha256(self.run_dir / "reports/frame-01-qa.json"))
        self.assertEqual(manifest_hash, sha256(self.run_manifest_path))
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(1, persisted["last_accepted_frame"])
        self.assertEqual("ACCEPTED", persisted["frames"][0]["state"])
        self.assertTrue((self.run_dir / "normalized/frames/01.png").is_file())
        self.assertTrue((self.run_dir / "normalized/frames/01-technical.png").is_file())
        self.assertNotIn("data:image", self.run_manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn(
            "data:image",
            (self.run_dir / "reports/frame-01-qa.json").read_text(encoding="utf-8"),
        )

    def test_record_evaluation_persists_request_bound_raw_normalized_and_technical_hashes(self):
        raw_relative = "raw/frame-01/provenance.png"
        raw_path = self._raw_result(raw_relative)
        request = self._request_record(
            1, self.manifest["primary_model"], raw_relative
        )
        self.run["requests"].append(request)
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertIn("provenance", report)
        provenance = report["provenance"]
        self.assertEqual(request["request_id"], provenance["request_id"])
        self.assertEqual(request["attempt"], provenance["attempt"])
        self.assertEqual(request["model"], provenance["model"])
        self.assertEqual(request["result_sha256"], provenance["raw"]["sha256"])
        self.assertEqual([512, 832], provenance["raw"]["dimensions"])
        self.assertEqual(
            sha256(self.run_dir / "normalized/frames/01.png"),
            provenance["normalized"]["sha256"],
        )
        self.assertEqual([256, 416], provenance["normalized"]["dimensions"])
        self.assertEqual(
            sha256(self.run_dir / "normalized/frames/01-technical.png"),
            provenance["technical"]["sha256"],
        )
        self.assertEqual([512, 832], provenance["technical"]["dimensions"])
        self.assertEqual(
            {"geometry", "identity", "palette", "alpha_components", "feet"},
            set(report["checks"]),
        )

    def test_repeated_evaluation_preserves_ready_terminal_state_and_all_bytes(self):
        raw_relative = "raw/frame-01/accepted.png"
        raw_path = self._raw_result(raw_relative)
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()
        first = seq.record_evaluation(self.run_dir, 1, raw_path)
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        persisted["state"] = "READY_FOR_VISUAL_REVIEW"
        persisted["artifacts"] = {"fixture": {"path": "previews/contact_sheet.png"}}
        self.run_manifest_path.write_text(json.dumps(persisted), encoding="utf-8")
        paths = [
            raw_path,
            self.run_manifest_path,
            self.run_dir / "reports/frame-01-qa.json",
            self.run_dir / "normalized/frames/01.png",
            self.run_dir / "normalized/frames/01-technical.png",
        ]
        before = {str(path): path.read_bytes() for path in paths}

        repeated = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(first, repeated)
        self.assertEqual(before, {str(path): path.read_bytes() for path in paths})
        self.assertEqual(
            "READY_FOR_VISUAL_REVIEW",
            json.loads(self.run_manifest_path.read_text(encoding="utf-8"))["state"],
        )

    def test_repeated_evaluation_preserves_technical_rejected_state_and_reason(self):
        raw_relative = "raw/frame-01/rejected.png"
        raw_path = self._raw_result(raw_relative, foot_fixture(detach_support=True))
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()
        first = seq.record_evaluation(self.run_dir, 1, raw_path)
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        persisted["state"] = "TECHNICAL_REJECTED"
        persisted["terminal_reason"] = "ATTEMPT_POLICY_EXHAUSTED"
        self.run_manifest_path.write_text(json.dumps(persisted), encoding="utf-8")
        report_path = self.run_dir / "reports/frame-01-qa.json"
        before_manifest = self.run_manifest_path.read_bytes()
        before_report = report_path.read_bytes()

        repeated = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(first, repeated)
        self.assertEqual(before_manifest, self.run_manifest_path.read_bytes())
        self.assertEqual(before_report, report_path.read_bytes())

    def test_conflicting_evaluation_of_accepted_frame_blocks_without_mutation(self):
        first_relative = "raw/frame-01/accepted.png"
        first_path = self._raw_result(first_relative)
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], first_relative)
        )
        self._persist_run()
        seq.record_evaluation(self.run_dir, 1, first_path)
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        persisted["state"] = "READY_FOR_VISUAL_REVIEW"
        self.run_manifest_path.write_text(json.dumps(persisted), encoding="utf-8")
        conflict_relative = "raw/frame-01/conflict.png"
        conflict_path = self._raw_result(conflict_relative)
        conflict_request = self._request_record(
            1, self.manifest["primary_model"], conflict_relative
        )
        persisted["requests"].append(conflict_request)
        persisted["next_request"] = {
            "fingerprint": conflict_request["request_fingerprint"],
            "frame_index": conflict_request["frame_index"],
            "model": conflict_request["model"],
        }
        self.run_manifest_path.write_text(json.dumps(persisted), encoding="utf-8")
        report_path = self.run_dir / "reports/frame-01-qa.json"
        before_manifest = self.run_manifest_path.read_bytes()
        before_report = report_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "FRAME_EVALUATION_CONFLICT"):
            seq.record_evaluation(self.run_dir, 1, conflict_path)

        self.assertEqual(before_manifest, self.run_manifest_path.read_bytes())
        self.assertEqual(before_report, report_path.read_bytes())

    def test_evaluation_requires_a_persisted_request(self):
        raw_path = self._raw_result("raw/frame-01/no-request.png")
        before = self.run_manifest_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "EVALUATION_REQUEST_NOT_FOUND"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(before, self.run_manifest_path.read_bytes())
        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def test_evaluation_rejects_a_pending_request(self):
        raw_relative = "raw/frame-01/pending.png"
        raw_path = self._raw_result(raw_relative)
        self.run["requests"].append(
            self._request_record(
                1,
                self.manifest["primary_model"],
                raw_relative,
                status="IN_PROGRESS",
            )
        )
        self._persist_run()
        before = self.run_manifest_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "EVALUATION_REQUEST_NOT_COMPLETED"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(before, self.run_manifest_path.read_bytes())

    def test_evaluation_rejects_a_mismatched_result_path(self):
        raw_path = self._raw_result("raw/frame-01/actual.png")
        self.run["requests"].append(
            self._request_record(
                1,
                self.manifest["primary_model"],
                "raw/frame-01/different.png",
            )
        )
        self._persist_run()
        before = self.run_manifest_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "EVALUATION_REQUEST_NOT_FOUND"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(before, self.run_manifest_path.read_bytes())

    def test_evaluation_rejects_an_unapproved_request_model(self):
        raw_relative = "raw/frame-01/unapproved.png"
        raw_path = self._raw_result(raw_relative)
        self.run["requests"].append(
            self._request_record(1, "fal-ai/unapproved", raw_relative)
        )
        self._persist_run()
        before = self.run_manifest_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "EVALUATION_REQUEST_INVALID"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(before, self.run_manifest_path.read_bytes())

    def test_evaluation_blocks_missing_request_fingerprint_before_writes(self):
        raw_relative = "raw/frame-01/missing-fingerprint.png"
        raw_path = self._raw_result(raw_relative)
        request = self._request_record(
            1, self.manifest["primary_model"], raw_relative
        )
        request.pop("request_fingerprint")
        self.run["requests"].append(request)
        self._persist_run()
        before = self.run_manifest_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "EVALUATION_REQUEST_INVALID"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(before, self.run_manifest_path.read_bytes())
        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())
        self.assertFalse((self.run_dir / "normalized/frames/01.png").exists())

    def test_evaluation_blocks_non_sha256_request_fingerprint_before_writes(self):
        for fingerprint in ("not-a-sha256", "A" * 64):
            with self.subTest(fingerprint=fingerprint):
                raw_relative = f"raw/frame-01/invalid-fingerprint-{len(fingerprint)}.png"
                raw_path = self._raw_result(raw_relative)
                request = self._request_record(
                    1,
                    self.manifest["primary_model"],
                    raw_relative,
                    fingerprint=fingerprint,
                )
                self.run["requests"] = [request]
                self._persist_run()
                before = self.run_manifest_path.read_bytes()

                with self.assertRaisesRegex(
                    seq.PipelineBlocked, "EVALUATION_REQUEST_INVALID"
                ):
                    seq.record_evaluation(self.run_dir, 1, raw_path)

                self.assertEqual(before, self.run_manifest_path.read_bytes())
                self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())
                self.assertFalse((self.run_dir / "normalized/frames/01.png").exists())

    def test_evaluation_blocks_fingerprint_decoupled_from_current_handoff_before_writes(self):
        raw_relative = "raw/frame-01/stale-fingerprint.png"
        raw_path = self._raw_result(raw_relative)
        request = self._request_record(
            1, self.manifest["primary_model"], raw_relative
        )
        self.run["requests"].append(request)
        self.run["next_request"]["fingerprint"] = "f" * 64
        self._persist_run()
        before = self.run_manifest_path.read_bytes()

        with self.assertRaisesRegex(seq.PipelineBlocked, "EVALUATION_REQUEST_INVALID"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(before, self.run_manifest_path.read_bytes())
        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())
        self.assertFalse((self.run_dir / "normalized/frames/01.png").exists())

    def test_completed_matching_request_permits_evaluation(self):
        raw_relative = "raw/frame-01/completed.png"
        raw_path = self._raw_result(raw_relative)
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual(self.manifest["primary_model"], report["model"])
        self.assertTrue(report["passed"])

    def test_rejected_evaluation_preserves_raw_and_last_accepted_frame(self):
        self._accepted_frame(1)
        self.run["last_accepted_frame"] = 1
        raw_relative = "raw/frame-02/rejected.png"
        detached = foot_fixture(detach_support=True)
        raw = Image.new("RGB", seq.TECHNICAL_SIZE, (255, 0, 255))
        enlarged = detached.resize(seq.TECHNICAL_SIZE, Image.Resampling.NEAREST)
        raw.paste(enlarged.convert("RGB"), mask=enlarged.getchannel("A"))
        raw_path = self._save_reference(raw_relative, raw)
        self.run["requests"].append(
            self._request_record(2, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 2, raw_path)

        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("REJECTED", report["state"])
        self.assertEqual(1, persisted["last_accepted_frame"])
        self.assertTrue(raw_path.is_file())
        self.assertFalse((self.run_dir / "normalized/frames/02.png").exists())
        self.assertEqual("REJECTED", persisted["frames"][1]["state"])

    def test_opaque_incompatible_background_is_audited_as_rejected_and_retries_qwen(self):
        generated_request = seq.next_request(self.run, self.manifest)
        request_output = self.run_dir / "input/references/frame-01-live-like.json"
        seq._write_request_output(request_output, generated_request, self.run_dir)
        transport_request = json.loads(request_output.read_text(encoding="utf-8"))
        raw_relative = "raw/frame-01/attempt-01-qwen-image-2-edit.png"
        raw_path = self._save_reference(
            raw_relative,
            Image.new("RGB", seq.TECHNICAL_SIZE, (29, 244, 189)),
        )
        request = self._request_record(
            1, self.manifest["primary_model"], raw_relative
        )
        request["request_fingerprint"] = transport_request["request_fingerprint"]
        self.run["requests"].append(request)
        self.run["next_request"] = {
            "fingerprint": transport_request["request_fingerprint"],
            "frame_index": 1,
            "model": self.manifest["primary_model"],
        }
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 1, raw_path)

        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("REJECTED", report["state"])
        self.assertIs(False, report["passed"])
        self.assertEqual(["NORMALIZED_SUBJECT_OUT_OF_BOUNDS"], report["failures"])
        self.assertEqual(
            {"geometry", "identity", "palette", "alpha_components", "feet"},
            set(report["checks"]),
        )
        for group in report["checks"].values():
            self.assertEqual(["NORMALIZED_SUBJECT_OUT_OF_BOUNDS"], group["failures"])
        self.assertEqual(request["request_id"], report["provenance"]["request_id"])
        self.assertEqual(
            request["request_fingerprint"],
            report["provenance"]["request_fingerprint"],
        )
        self.assertEqual(request["result_sha256"], report["provenance"]["raw"]["sha256"])
        self.assertIsNone(report["provenance"]["normalized"]["path"])
        self.assertIsNone(report["provenance"]["technical"]["path"])
        self.assertFalse((self.run_dir / "normalized/frames/01.png").exists())
        self.assertFalse((self.run_dir / "normalized/frames/01-technical.png").exists())
        self.assertEqual(0, persisted["last_accepted_frame"])
        self.assertEqual("REJECTED", persisted["frames"][0]["state"])

        retry = seq.next_request(persisted, self.manifest)

        self.assertEqual(1, retry["frame_index"])
        self.assertEqual(self.manifest["primary_model"], retry["model"])

    def test_uniform_expected_chroma_is_audited_as_empty_alpha_rejection(self):
        generated_request = seq.next_request(self.run, self.manifest)
        request_output = self.run_dir / "input/references/frame-01-empty-alpha.json"
        seq._write_request_output(request_output, generated_request, self.run_dir)
        transport_request = json.loads(request_output.read_text(encoding="utf-8"))
        raw_relative = "raw/frame-01/attempt-01-empty-alpha.png"
        raw_path = self._save_reference(
            raw_relative,
            Image.new("RGB", seq.TECHNICAL_SIZE, tuple(self.run["chroma_rgb"])),
        )
        request = self._request_record(
            1, self.manifest["primary_model"], raw_relative
        )
        request["request_fingerprint"] = transport_request["request_fingerprint"]
        self.run["requests"].append(request)
        self.run["next_request"] = {
            "fingerprint": transport_request["request_fingerprint"],
            "frame_index": 1,
            "model": self.manifest["primary_model"],
        }
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual("REJECTED", report["state"])
        self.assertEqual(["EMPTY_ALPHA"], report["failures"])
        self.assertEqual(
            {"geometry", "identity", "palette", "alpha_components", "feet"},
            set(report["checks"]),
        )
        self.assertTrue(
            all(group["failures"] == ["EMPTY_ALPHA"] for group in report["checks"].values())
        )
        self.assertFalse((self.run_dir / "normalized/frames/01.png").exists())
        self.assertFalse((self.run_dir / "normalized/frames/01-technical.png").exists())

    def test_record_evaluation_keeps_raw_size_mismatch_blocked(self):
        raw_relative = "raw/frame-01/wrong-size.png"
        raw_path = self._save_reference(raw_relative, Image.new("RGB", (511, 832)))
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        with self.assertRaisesRegex(seq.PipelineBlocked, "RAW_SIZE_MISMATCH"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def test_record_evaluation_keeps_unreadable_raw_blocked(self):
        raw_relative = "raw/frame-01/unreadable.png"
        raw_path = self.run_dir / raw_relative
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(b"not a png")
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        with self.assertRaisesRegex(seq.PipelineBlocked, "RAW_FRAME_UNREADABLE"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def test_record_evaluation_keeps_invalid_chroma_configuration_blocked(self):
        raw_relative = "raw/frame-01/invalid-config.png"
        raw_path = self._raw_result(raw_relative)
        self.run["chroma_rgb"] = [255, 0]
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        with self.assertRaisesRegex(seq.PipelineBlocked, "INVALID_CHROMA_RGB"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def test_nano_model_native_raw_is_accepted_with_real_provenance_dimensions(self):
        raw_relative = "raw/frame-01/attempt-01-nano-banana-2-edit.png"
        raw_path = self._model_native_raw_result(raw_relative, (400, 650))
        request = self._request_record(
            1, self.manifest["fallback_model"], raw_relative
        )
        self.run["requests"].append(request)
        self._persist_run()

        report = seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertEqual("ACCEPTED", report["state"])
        self.assertEqual([400, 650], report["provenance"]["raw"]["dimensions"])
        with Image.open(self.run_dir / "normalized/frames/01.png") as normalized:
            self.assertEqual(seq.NORMALIZED_SIZE, normalized.size)
            self.assertEqual("RGBA", normalized.mode)
        with Image.open(self.run_dir / "normalized/frames/01-technical.png") as technical:
            self.assertEqual(seq.TECHNICAL_SIZE, technical.size)
            self.assertEqual("RGB", technical.mode)

    def test_qwen_model_native_sized_raw_remains_blocked(self):
        raw_relative = "raw/frame-01/qwen-400x650.png"
        raw_path = self._model_native_raw_result(raw_relative, (400, 650))
        self.run["requests"].append(
            self._request_record(1, self.manifest["primary_model"], raw_relative)
        )
        self._persist_run()

        with self.assertRaisesRegex(seq.PipelineBlocked, "RAW_SIZE_MISMATCH"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def test_nano_wrong_aspect_or_too_small_raw_remains_blocked(self):
        for label, size in (("wrong-aspect", (400, 651)), ("too-small", (200, 325))):
            with self.subTest(label=label):
                raw_relative = f"raw/frame-01/nano-{label}.png"
                raw_path = self._model_native_raw_result(raw_relative, size)
                self.run["requests"] = []
                self.run["requests"].append(
                    self._request_record(
                        1, self.manifest["fallback_model"], raw_relative
                    )
                )
                self._persist_run()

                with self.assertRaisesRegex(seq.PipelineBlocked, "RAW_SIZE_MISMATCH"):
                    seq.record_evaluation(self.run_dir, 1, raw_path)

                self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def test_nano_disguised_non_png_raw_remains_blocked(self):
        raw_relative = "raw/frame-01/nano-disguised-jpeg.png"
        raw_path = self._model_native_raw_result(
            raw_relative, (400, 650), format_name="JPEG"
        )
        self.run["requests"].append(
            self._request_record(1, self.manifest["fallback_model"], raw_relative)
        )
        self._persist_run()

        with self.assertRaisesRegex(seq.PipelineBlocked, "RAW_FRAME_UNREADABLE"):
            seq.record_evaluation(self.run_dir, 1, raw_path)

        self.assertFalse((self.run_dir / "reports/frame-01-qa.json").exists())

    def _write_assembly_frames(self, count, write_reports=True):
        self.run["frames"] = []
        for index in range(1, count + 1):
            frame = Image.new("RGBA", seq.NORMALIZED_SIZE, (0, 0, 0, 0))
            frame.putpixel((128, 414), (255, 255, 255, 255))
            frame.putpixel((index, 414), (index, 30, 60, 255))
            normalized = f"normalized/frames/{index:02d}.png"
            technical = f"normalized/frames/{index:02d}-technical.png"
            self._save_reference(normalized, frame)
            self._save_reference(
                technical, frame.resize(seq.TECHNICAL_SIZE, Image.Resampling.NEAREST)
            )
            self.run["frames"].append(
                {
                    "frame_index": index,
                    "state": "ACCEPTED",
                    "passed": True,
                    "normalized_path": normalized,
                    "technical_path": technical,
                    "qa_report_path": f"reports/frame-{index:02d}-qa.json",
                    "attempts": [],
                }
            )
            if write_reports:
                (self.run_dir / f"reports/frame-{index:02d}-qa.json").write_text(
                    json.dumps(
                        {
                            "frame": index,
                            "state": "ACCEPTED",
                            "passed": True,
                            "failures": [],
                            "checks": {},
                            "raw_path": f"raw/frame-{index:02d}/accepted.png",
                            "model": self.manifest["primary_model"],
                            "attempts": [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
        self.run["last_accepted_frame"] = count
        self._persist_run()

    def _write_validated_assembly_frames(self):
        for index in range(1, 9):
            frame = self.master.copy()
            frame.putpixel((100 + index, 200), (90 + index, 120, 70, 255))
            raw_relative = f"raw/frame-{index:02d}/accepted.png"
            raw_path = self._raw_result(raw_relative, frame)
            self.run["requests"].append(
                self._request_record(
                    index, self.manifest["primary_model"], raw_relative
                )
            )
            self._persist_run()
            seq.record_evaluation(self.run_dir, index, raw_path)
            self.run = json.loads(
                self.run_manifest_path.read_text(encoding="utf-8")
            )

    def test_assemble_requires_eight_accepted_frames_and_sequence_pass(self):
        self._write_assembly_frames(7)

        report = seq.assemble_outputs(self.run_dir, self.manifest)

        self.assertEqual("TECHNICAL_REJECTED", report["state"])
        self.assertIn("ACCEPTED_FRAME_COUNT_MISMATCH", report["failures"])
        self.assertFalse((self.run_dir / "normalized/Ogre_Walk_S_8f_256x416.png").exists())
        self.assertFalse((self.run_dir / "previews/contact_sheet.png").exists())
        self.assertFalse((self.run_dir / "previews/loop.gif").exists())

    def test_assemble_rejects_manifest_only_acceptance_without_frame_qa_reports(self):
        self._write_assembly_frames(8, write_reports=False)

        report = seq.assemble_outputs(self.run_dir, self.manifest)

        self.assertEqual("TECHNICAL_REJECTED", report["state"])
        self.assertIn("ACCEPTED_FRAME_REPORT_MISSING", report["failures"])
        self.assertFalse((self.run_dir / "normalized/Ogre_Walk_S_8f_256x416.png").exists())
        self.assertFalse((self.run_dir / "previews/contact_sheet.png").exists())
        self.assertFalse((self.run_dir / "previews/loop.gif").exists())

    def test_assemble_rejects_forged_empty_qa_and_two_pixel_frames(self):
        self._write_assembly_frames(8)

        report = seq.assemble_outputs(self.run_dir, self.manifest)

        self.assertEqual("TECHNICAL_REJECTED", report["state"])
        self.assertIn("ACCEPTED_FRAME_REPORT_INVALID", report["failures"])
        self.assertFalse((self.run_dir / "normalized/Ogre_Walk_S_8f_256x416.png").exists())

    def test_ready_assembly_rejects_a_missing_provenance_artifact(self):
        self._write_validated_assembly_frames()
        first = seq.assemble_outputs(self.run_dir, self.manifest)
        self.assertEqual("READY_FOR_VISUAL_REVIEW", first["state"])
        (self.run_dir / "normalized/frames/01-technical.png").unlink()

        with self.assertRaisesRegex(seq.PipelineBlocked, "READY_PROVENANCE_INVALID"):
            seq.assemble_outputs(self.run_dir, self.manifest)

    def test_ready_assembly_rejects_mutated_hash_and_invalid_dimensions(self):
        self._write_validated_assembly_frames()
        first = seq.assemble_outputs(self.run_dir, self.manifest)
        self.assertEqual("READY_FOR_VISUAL_REVIEW", first["state"])
        Image.new("RGBA", (1, 1), (255, 255, 255, 255)).save(
            self.run_dir / "normalized/frames/01.png"
        )

        with self.assertRaisesRegex(seq.PipelineBlocked, "READY_PROVENANCE_INVALID"):
            seq.assemble_outputs(self.run_dir, self.manifest)

    def test_assemble_rejects_report_fingerprint_decoupled_from_completed_request(self):
        self._write_validated_assembly_frames()
        report_path = self.run_dir / "reports/frame-01-qa.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        forged = "f" * 64
        report["provenance"]["request_fingerprint"] = forged
        report["attempts"][-1]["request_fingerprint"] = forged
        report["attempts"][-1]["provenance"]["request_fingerprint"] = forged
        report_path.write_text(json.dumps(report), encoding="utf-8")
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        persisted["frames"][0]["provenance"]["request_fingerprint"] = forged
        self.run_manifest_path.write_text(json.dumps(persisted), encoding="utf-8")

        assembled = seq.assemble_outputs(self.run_dir, self.manifest)

        self.assertEqual("TECHNICAL_REJECTED", assembled["state"])
        self.assertIn("ACCEPTED_FRAME_REPORT_INVALID", assembled["failures"])
        self.assertFalse(
            (self.run_dir / "normalized/Ogre_Walk_S_8f_256x416.png").exists()
        )

    def test_assemble_revalidates_an_accepted_nano_model_native_raw(self):
        for index in range(1, 9):
            frame = self.master.copy()
            frame.putpixel((100 + index, 200), (90 + index, 120, 70, 255))
            raw_relative = f"raw/frame-{index:02d}/accepted.png"
            if index == 1:
                raw_path = self._model_native_raw_result(
                    raw_relative, (400, 650), frame
                )
                model = self.manifest["fallback_model"]
            else:
                raw_path = self._raw_result(raw_relative, frame)
                model = self.manifest["primary_model"]
            self.run["requests"].append(
                self._request_record(index, model, raw_relative)
            )
            self._persist_run()
            seq.record_evaluation(self.run_dir, index, raw_path)
            self.run = json.loads(
                self.run_manifest_path.read_text(encoding="utf-8")
            )

        assembled = seq.assemble_outputs(self.run_dir, self.manifest)

        self.assertEqual("READY_FOR_VISUAL_REVIEW", assembled["state"])

    def test_assemble_builds_exact_review_artifacts_and_stops_at_ready(self):
        self._write_validated_assembly_frames()

        report = seq.assemble_outputs(self.run_dir, self.manifest)

        self.assertEqual("READY_FOR_VISUAL_REVIEW", report["state"])
        self.assertTrue(report["passed"])
        with Image.open(self.run_dir / "normalized/Ogre_Walk_S_8f_256x416.png") as sheet:
            self.assertEqual((2048, 416), sheet.size)
            self.assertEqual("RGBA", sheet.mode)
        with Image.open(self.run_dir / "previews/contact_sheet.png") as contact:
            self.assertEqual((1024, 880), contact.size)
        with Image.open(self.run_dir / "previews/loop.gif") as loop:
            self.assertEqual((256, 416), loop.size)
            self.assertEqual(8, loop.n_frames)
            self.assertEqual(120, loop.info["duration"])
        self.assertTrue((self.run_dir / "reports/consistency_report.json").is_file())
        self.assertTrue((self.run_dir / "reports/qa_report.json").is_file())
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("READY_FOR_VISUAL_REVIEW", persisted["state"])
        self.assertNotIn("APPROVED", json.dumps(persisted))

    def test_python_cli_exposes_only_the_six_documented_commands(self):
        script = Path(seq.__file__).resolve()
        result = subprocess.run(
            [sys.executable, "-B", str(script), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        for command in ("validate", "prepare", "next-request", "evaluate", "assemble", "resume"):
            self.assertIn(command, result.stdout)

    def test_cli_writes_request_inside_confined_references_directory(self):
        output = self.run_dir / "input/references/frame-01-request.json"

        payload = self._run_request_cli(output, expected_code=0)

        self.assertTrue(output.is_file())
        self.assertTrue(payload["request_ready"])
        self.assertEqual(str(output.resolve()), payload["request_output"])

    def test_cli_persists_the_exact_current_request_fingerprint_for_transport(self):
        output = self.run_dir / "input/references/frame-01-fingerprinted.json"

        payload = self._run_request_cli(output, expected_code=0)

        request = json.loads(output.read_text(encoding="utf-8"))
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        self.assertIn("request_fingerprint", request)
        self.assertEqual(64, len(request["request_fingerprint"]))
        self.assertEqual(
            {
                "fingerprint": request["request_fingerprint"],
                "frame_index": request["frame_index"],
                "model": request["model"],
            },
            persisted["next_request"],
        )

    def test_cli_rejects_reports_project_like_and_arbitrary_external_outputs(self):
        destinations = (
            self.run_dir / "reports/request.json",
            self.run_dir.parent / "project/Assets/request.json",
            self.run_dir.parent / "arbitrary/request.json",
        )
        for output in destinations:
            with self.subTest(output=str(output)):
                payload = self._run_request_cli(output, expected_code=2)
                self.assertEqual("REQUEST_OUTPUT_NOT_CONFINED", payload["reason"])
                self.assertFalse(output.exists())

    def test_cli_rejects_an_existing_linked_output_ancestor_inside_references(self):
        target = self.run_dir / "input/references/real-target"
        target.mkdir()
        link = self.run_dir / "input/references/linked"
        self._create_directory_link(target, link)
        output = link / "request.json"

        payload = self._run_request_cli(output, expected_code=2)

        self.assertEqual("REQUEST_OUTPUT_NOT_CONFINED", payload["reason"])
        self.assertFalse((target / "request.json").exists())

    def test_cli_rejects_a_canonical_escape_before_writing(self):
        escaped = self.run_dir.parent / "escaped"
        escaped.mkdir()
        link = self.run_dir / "input/references/escape"
        self._create_directory_link(escaped, link)
        output = link / "request.json"

        payload = self._run_request_cli(output, expected_code=2)

        self.assertEqual("REQUEST_OUTPUT_NOT_CONFINED", payload["reason"])
        self.assertFalse((escaped / "request.json").exists())


class SequentialTransportTests(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parent / "fal_sequential_generate.mjs"
    _USE_PERSISTED_RUN = object()

    def setUp(self):
        self.node = required_external_file("TOWER_SPRITE_NODE_EXE")
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.temp_root = Path(temporary_directory.name).resolve()
        self.pilot_path = (
            Path(__file__).resolve().parents[1]
            / "assets/pilots/ogre_walk_s_8f.json"
        )
        self.manifest = json.loads(self.pilot_path.read_text(encoding="utf-8"))
        self.test_project_root = self.temp_root / "project"
        self.test_source_path = self.test_project_root / self.manifest["source_path"]
        self.test_source_path.parent.mkdir(parents=True)
        shutil.copy2(
            required_external_file("TOWER_SPRITE_ORIGINAL_MASTER"),
            self.test_source_path,
        )
        self.manifest_path = self.temp_root / "manifest.json"
        self.pricing_path = self.temp_root / "pricing.json"
        self.request_path = self.temp_root / "request.json"
        self.events_path = self.temp_root / "fake-events.json"
        self.action_path = self.temp_root / "fake-actions.log"
        self.client_path = self.temp_root / "fake-client.mjs"
        self.workspace_root = self.temp_root / "external-runs"
        self.run_dir = self.workspace_root / "20260819-120000-ogre-walk-s-8f"
        for relative in (
            "input/master",
            "input/pose-guides",
            "input/references",
            "raw",
            "normalized/frames",
            "previews",
            "reports",
        ):
            (self.run_dir / relative).mkdir(parents=True, exist_ok=True)
        project_root = Path(self.manifest["project_root"]).resolve()
        marker = {
            "format": "tower-sprite-pipeline-prepared-run-v1",
            "project_root": str(project_root),
            "workspace_root": str(self.workspace_root.resolve()),
            "state": "PREPARING",
        }
        (self.run_dir / "reports/prepared-run.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
        self.run_manifest_path = self.run_dir / "reports/run_manifest.json"
        self.initial_run = {
            "run_id": self.run_dir.name,
            "run_dir": str(self.run_dir.resolve()),
            "state": "DRAFT",
            "project_root": str(project_root),
            "source_path": str(
                (project_root / self.manifest["source_path"]).resolve()
            ),
            "chroma_rgb": [255, 0, 255],
            "normalized_input": copy.deepcopy(self.manifest),
            "technical_master": "input/master/master-technical.png",
            "pose_guides": [
                f"input/pose-guides/{index:02d}-pose.png"
                for index in range(1, 9)
            ],
            "frame_seeds": [71001 + index for index in range(8)],
            "frames": [],
            "requests": [],
            "last_accepted_frame": 0,
        }
        Image.new("RGB", seq.TECHNICAL_SIZE, (255, 0, 255)).save(
            self.run_dir / self.initial_run["technical_master"]
        )
        for index, relative in enumerate(self.initial_run["pose_guides"], start=1):
            Image.new("RGB", seq.TECHNICAL_SIZE, (index, 20, 30)).save(
                self.run_dir / relative
            )
        self.request = self.authorize_request(self.initial_run)
        project_root = self.test_project_root.resolve()
        marker["project_root"] = str(project_root)
        (self.run_dir / "reports/prepared-run.json").write_text(
            json.dumps(marker), encoding="utf-8"
        )
        self.initial_run["project_root"] = str(project_root)
        self.initial_run["source_path"] = str(self.test_source_path.resolve())
        self.run_manifest_path.write_text(
            json.dumps(self.initial_run), encoding="utf-8"
        )
        self.pricing = self.valid_pricing()
        image_bytes = io.BytesIO()
        Image.new("RGBA", (2, 2), (10, 20, 30, 255)).save(
            image_bytes, format="PNG"
        )
        self.png_bytes = image_bytes.getvalue()
        self.png_data_uri = (
            "data:image/png;base64,"
            + base64.b64encode(self.png_bytes).decode("ascii")
        )
        self.client_path.write_text(
            textwrap.dedent(
                """
                import fs from "node:fs";
                import path from "node:path";

                const root = path.resolve(process.env.FAKE_ROOT);
                const eventsPath = path.resolve(process.env.FAKE_EVENTS_PATH);
                const actionsPath = path.resolve(process.env.FAKE_ACTIONS_PATH);
                function within(candidate) {
                  const relative = path.relative(root, candidate);
                  return relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
                }
                if (!within(eventsPath) || !within(actionsPath)) {
                  throw new Error("fake path escaped temp root");
                }
                const events = JSON.parse(fs.readFileSync(eventsPath, "utf8"));
                function action(value) {
                  fs.appendFileSync(actionsPath, `${value}\\n`, "utf8");
                }
                function next(expected, endpoint, options) {
                  const event = events.shift();
                  if (!event || event.op !== expected) {
                    throw new Error(`expected fake ${expected}`);
                  }
                  if (event.expected_endpoint !== undefined && event.expected_endpoint !== endpoint) {
                    throw new Error(`unexpected endpoint ${endpoint}`);
                  }
                  if (
                    event.expected_input !== undefined
                    && JSON.stringify(event.expected_input) !== JSON.stringify(options?.input)
                  ) {
                    throw new Error("unexpected request input");
                  }
                  if (event.throw) {
                    const error = new Error(event.message || "injected fake failure");
                    error.name = event.error_name || "Error";
                    if (event.status !== undefined) error.status = event.status;
                    if (event.body !== undefined) error.body = event.body;
                    throw error;
                  }
                  return event.value;
                }
                action("import");
                export const fal = {
                  queue: {
                    async submit(endpoint, options) {
                      action("submit");
                      return next("submit", endpoint, options);
                    },
                    async status(endpoint, options) {
                      action(`status:${options.requestId}`);
                      return next("status", endpoint, options);
                    },
                    async result(endpoint, options) {
                      action(`result:${options.requestId}`);
                      return next("result", endpoint, options);
                    },
                  },
                };
                """
            ),
            encoding="utf-8",
        )

    def valid_pricing(self):
        return {
            "verified_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "source_url": "https://api.fal.ai/v1/models/pricing",
            "prices": [
                {
                    "endpoint_id": "fal-ai/qwen-image-2/edit",
                    "unit_price": 0.03,
                    "unit": "images",
                    "currency": "USD",
                },
                {
                    "endpoint_id": "fal-ai/nano-banana-2/edit",
                    "unit_price": 0.04,
                    "unit": "image",
                    "currency": "USD",
                    "resolution_multipliers": {"0.5K": 0.5},
                },
            ],
        }

    def request_record(
        self,
        request_id,
        model="fal-ai/qwen-image-2/edit",
        status="IN_PROGRESS",
        estimate=0.03,
        frame_index=1,
        attempt=1,
        result_path=None,
    ):
        return {
            "frame_index": frame_index,
            "attempt": attempt,
            "model": model,
            "request_id": request_id,
            "estimated_cost_usd": estimate,
            "status": status,
            "submitted_at": "2026-08-19T12:00:00.000Z",
            "result_path": result_path,
            "request_fingerprint": self.request["request_fingerprint"],
        }

    def authorize_request(self, run):
        literal_project = Path(self.manifest["project_root"]).resolve()
        if run["project_root"] == str(literal_project):
            produced = seq.next_request(run, self.manifest)
        else:
            marker_path = self.run_dir / "reports/prepared-run.json"
            original_marker = marker_path.read_bytes()
            marker = json.loads(original_marker)
            marker["project_root"] = str(literal_project)
            request_run = copy.deepcopy(run)
            request_run["project_root"] = str(literal_project)
            request_run["source_path"] = str(
                (literal_project / self.manifest["source_path"]).resolve()
            )
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            try:
                produced = seq.next_request(request_run, self.manifest)
            finally:
                marker_path.write_bytes(original_marker)
        self.assertIsNotNone(produced)
        request = {
            "frame_index": produced["frame_index"],
            "model": produced["model"],
            "input": produced["input"],
        }
        canonical = json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical).hexdigest()
        request["request_fingerprint"] = fingerprint
        run["next_request"] = {
            "fingerprint": fingerprint,
            "frame_index": request["frame_index"],
            "model": request["model"],
        }
        return request

    def rebind_request(self, run, request):
        payload = {
            "frame_index": request["frame_index"],
            "model": request["model"],
            "input": request["input"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        request["request_fingerprint"] = fingerprint
        run["next_request"] = {
            "fingerprint": fingerprint,
            "frame_index": request["frame_index"],
            "model": request["model"],
        }
        return request

    def accepted_transport_frame(self, run, index):
        normalized = f"normalized/frames/{index:02d}.png"
        technical = f"normalized/frames/{index:02d}-technical.png"
        Image.new("RGBA", seq.NORMALIZED_SIZE, (20 + index, 30, 40, 255)).save(
            self.run_dir / normalized
        )
        Image.new("RGB", seq.TECHNICAL_SIZE, (20 + index, 30, 40)).save(
            self.run_dir / technical
        )
        run["frames"].append(
            {
                "frame_index": index,
                "state": "ACCEPTED",
                "passed": True,
                "normalized_path": normalized,
                "technical_path": technical,
                "qa_report_path": f"reports/frame-{index:02d}-qa.json",
                "attempts": [],
            }
        )
        run["last_accepted_frame"] = index

    def fallback_ready_transport_run(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = []
        for index in (1, 2):
            self.accepted_transport_frame(run, index)
            for _ in range(2):
                attempt = len(run["requests"]) + 1
                run["requests"].append(
                    self.request_record(
                        f"primary-{attempt}",
                        status="FAILED",
                        frame_index=index,
                        attempt=attempt,
                    )
                )
        attempt = len(run["requests"]) + 1
        run["requests"].append(
            self.request_record(
                f"primary-{attempt}",
                status="FAILED",
                frame_index=3,
                attempt=attempt,
            )
        )
        request = self.authorize_request(run)
        self.assertEqual(self.manifest["fallback_model"], request["model"])
        return run, request

    def create_directory_link(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        self.assertTrue(link.is_dir())
        self.assertEqual(target.resolve(), link.resolve())

    def png_data_uri_for(self, payload):
        return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")

    def run_transport(
        self,
        *,
        run=_USE_PERSISTED_RUN,
        pricing=None,
        request=None,
        fake_events=(),
        expected_code=0,
        key="unit-test-fal-key-never-persist",
        run_manifest_path=None,
        node_env="test",
        include_client_module=True,
        test_project_root=_USE_PERSISTED_RUN,
        fake_root=_USE_PERSISTED_RUN,
        client_module=_USE_PERSISTED_RUN,
    ):
        if run is not self._USE_PERSISTED_RUN:
            self.run_manifest_path.write_text(json.dumps(run), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.pricing_path.write_text(
            json.dumps(self.pricing if pricing is None else pricing), encoding="utf-8"
        )
        request_value = self.request if request is None else request
        self.request_path.write_text(json.dumps(request_value), encoding="utf-8")
        prepared_events = copy.deepcopy(list(fake_events))
        for event in prepared_events:
            if event.get("op") == "submit":
                event.setdefault("expected_endpoint", request_value.get("model"))
                event.setdefault("expected_input", request_value.get("input"))
        self.events_path.write_text(json.dumps(prepared_events), encoding="utf-8")
        self.action_path.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if fake_root is self._USE_PERSISTED_RUN:
            environment["FAKE_ROOT"] = str(self.temp_root)
        elif fake_root is None:
            environment.pop("FAKE_ROOT", None)
        else:
            environment["FAKE_ROOT"] = str(fake_root)
        environment["FAKE_EVENTS_PATH"] = str(self.events_path)
        environment["FAKE_ACTIONS_PATH"] = str(self.action_path)
        if node_env is None:
            environment.pop("NODE_ENV", None)
        else:
            environment["NODE_ENV"] = node_env
        if test_project_root is self._USE_PERSISTED_RUN:
            environment["TOWER_SPRITE_TEST_PROJECT_ROOT"] = str(
                self.test_project_root
            )
        elif test_project_root is None:
            environment.pop("TOWER_SPRITE_TEST_PROJECT_ROOT", None)
        else:
            environment["TOWER_SPRITE_TEST_PROJECT_ROOT"] = str(test_project_root)
        if key is None:
            environment.pop("FAL_KEY", None)
        else:
            environment["FAL_KEY"] = key
        command = [
            str(self.node),
            str(self.SCRIPT),
            "--manifest",
            str(self.manifest_path),
            "--run-manifest",
            str(run_manifest_path or self.run_manifest_path),
            "--pricing",
            str(self.pricing_path),
            "--request-input",
            str(self.request_path),
        ]
        if include_client_module:
            module_path = (
                self.client_path
                if client_module is self._USE_PERSISTED_RUN
                else client_module
            )
            command.extend(["--client-module", str(module_path)])
        completed = subprocess.run(
            command,
            cwd=self.temp_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(expected_code, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        persisted = json.loads(self.run_manifest_path.read_text(encoding="utf-8"))
        actions = (
            self.action_path.read_text(encoding="utf-8").splitlines()
            if self.action_path.exists()
            else []
        )
        return SimpleNamespace(
            payload=payload,
            run=persisted,
            action_log=actions,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def assert_preflight_blocked(self, **kwargs):
        if "run" in kwargs:
            run = kwargs.pop("run")
            self.run_manifest_path.write_text(json.dumps(run), encoding="utf-8")
        before = self.run_manifest_path.read_bytes()
        result = self.run_transport(expected_code=2, **kwargs)
        self.assertEqual([], result.action_log)
        self.assertEqual(before, self.run_manifest_path.read_bytes())
        return result

    def test_transport_submits_at_most_one_new_request_per_invocation(self):
        result = self.run_transport(
            fake_events=[
                {"op": "submit", "value": {"request_id": "req-1"}},
                {"op": "status", "value": {"status": "COMPLETED"}},
                {
                    "op": "result",
                    "value": {"data": {"images": [{"url": self.png_data_uri}]}},
                },
            ]
        )

        self.assertEqual(1, result.action_log.count("submit"))
        self.assertEqual("req-1", result.run["requests"][0]["request_id"])
        self.assertEqual("COMPLETED", result.run["requests"][0]["status"])
        result_path = self.run_dir / result.run["requests"][0]["result_path"]
        self.assertEqual(self.png_bytes, result_path.read_bytes())
        self.assertIn("result_sha256", result.run["requests"][0])
        self.assertEqual(
            hashlib.sha256(self.png_bytes).hexdigest(),
            result.run["requests"][0]["result_sha256"],
        )
        self.assertTrue(is_relative_to(result_path, self.run_dir / "raw/frame-01"))
        self.assertEqual(1, len(list((self.run_dir / "raw").rglob("*.png"))))

    def test_transport_resumes_pending_id_before_submit(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = [
            self.request_record(
                f"req-failed-{index}",
                status="FAILED",
                estimate=0.01,
                attempt=index,
            )
            for index in range(1, 10)
        ] + [self.request_record("req-existing", estimate=0.01, attempt=10)]

        result = self.run_transport(
            run=run,
            fake_events=[
                {"op": "status", "value": {"status": "IN_PROGRESS"}},
            ],
        )

        self.assertEqual(["import", "status:req-existing"], result.action_log)
        self.assertEqual("REQUEST_PENDING", result.payload["reason"])
        self.assertEqual(10, len(result.run["requests"]))

    def test_transport_fetches_completed_result_before_any_submit(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = [
            self.request_record("req-complete", status="COMPLETED")
        ]

        result = self.run_transport(
            run=run,
            fake_events=[
                {
                    "op": "result",
                    "value": {"data": {"images": [{"url": self.png_data_uri}]}},
                }
            ],
        )

        self.assertEqual(["import", "result:req-complete"], result.action_log)
        self.assertEqual("RESULT_SAVED", result.payload["reason"])
        self.assertTrue((self.run_dir / result.payload["result_path"]).is_file())

    def test_transport_blocks_terminal_runs_before_client_import(self):
        for state in ("READY_FOR_VISUAL_REVIEW", "TECHNICAL_REJECTED"):
            with self.subTest(state=state):
                run = copy.deepcopy(self.initial_run)
                run["state"] = state
                result = self.assert_preflight_blocked(run=run)
                self.assertEqual("RUN_TERMINAL", result.payload["reason"])

    def test_transport_blocks_accepted_frame_replay_and_stale_first_frame_request(self):
        run = copy.deepcopy(self.initial_run)
        self.accepted_transport_frame(run, 1)

        result = self.assert_preflight_blocked(
            run=run,
            request=copy.deepcopy(self.request),
        )

        self.assertEqual("STALE_REQUEST_INPUT", result.payload["reason"])

    def test_transport_blocks_completed_result_until_evaluate_records_it(self):
        run = copy.deepcopy(self.initial_run)
        raw_relative = "raw/frame-01/completed.png"
        raw_path = self.run_dir / raw_relative
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(self.png_bytes)
        record = self.request_record(
            "req-completed",
            status="COMPLETED",
            result_path=raw_relative,
        )
        record["result_sha256"] = sha256(raw_path)
        run["requests"] = [record]

        result = self.assert_preflight_blocked(run=run)

        self.assertEqual("EVALUATION_REQUIRED", result.payload["reason"])

    def test_transport_rejects_incomplete_or_nested_altered_current_request(self):
        cases = []
        incomplete = copy.deepcopy(self.request)
        incomplete.pop("request_fingerprint")
        del incomplete["input"]["negative_prompt"]
        cases.append(("incomplete", incomplete))
        altered = copy.deepcopy(self.request)
        altered.pop("request_fingerprint")
        altered["input"]["image_size"]["width"] = 510
        cases.append(("altered", altered))
        for name, request in cases:
            with self.subTest(case=name):
                run = copy.deepcopy(self.initial_run)
                result = self.assert_preflight_blocked(run=run, request=request)
                self.assertEqual("INVALID_REQUEST_INPUT", result.payload["reason"])

    def test_transport_requires_first_unaccepted_frame_and_current_recovery_model(self):
        wrong_frame = copy.deepcopy(self.request)
        wrong_frame["frame_index"] = 2
        wrong_frame_run = copy.deepcopy(self.initial_run)
        self.rebind_request(wrong_frame_run, wrong_frame)
        frame_result = self.assert_preflight_blocked(
            run=wrong_frame_run, request=wrong_frame
        )
        self.assertEqual("STALE_REQUEST_INPUT", frame_result.payload["reason"])

        wrong_model = copy.deepcopy(self.request)
        wrong_model["model"] = self.manifest["fallback_model"]
        wrong_model["input"]["resolution"] = "0.5K"
        wrong_model_run = copy.deepcopy(self.initial_run)
        self.rebind_request(wrong_model_run, wrong_model)
        model_result = self.assert_preflight_blocked(
            run=wrong_model_run, request=wrong_model
        )
        self.assertEqual("STALE_REQUEST_INPUT", model_result.payload["reason"])

    def test_transport_requires_the_persisted_next_request_fingerprint(self):
        altered = copy.deepcopy(self.request)
        altered["input"]["prompt"] += " altered"

        result = self.assert_preflight_blocked(
            run=copy.deepcopy(self.initial_run),
            request=altered,
        )

        self.assertEqual("REQUEST_FINGERPRINT_MISMATCH", result.payload["reason"])

    def test_transport_validates_the_complete_literal_sequential_manifest(self):
        manifest = copy.deepcopy(self.manifest)
        del manifest["qa"]["planted_foot_drift_px_max"]
        run = copy.deepcopy(self.initial_run)
        run["normalized_input"] = copy.deepcopy(manifest)
        original = self.manifest
        self.manifest = manifest
        try:
            result = self.assert_preflight_blocked(run=run)
        finally:
            self.manifest = original
        self.assertEqual("INVALID_MANIFEST", result.payload["reason"])

    def test_transport_resumes_existing_ids_before_new_submit_only_gates(self):
        cases = []
        stale = self.valid_pricing()
        stale["verified_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=16)
        ).isoformat().replace("+00:00", "Z")
        cases.append(("active-stale", "IN_PROGRESS", stale, "status"))
        over_budget = self.valid_pricing()
        over_budget["prices"][0]["unit_price"] = 1.0
        cases.append(("active-over-budget", "IN_PROGRESS", over_budget, "status"))
        cases.append(("completed-stale", "COMPLETED", stale, "result"))

        for name, status, pricing, operation in cases:
            with self.subTest(case=name):
                run = copy.deepcopy(self.initial_run)
                request_id = f"req-{name}"
                run["requests"] = [
                    self.request_record(request_id, status=status)
                ]
                events = (
                    [{"op": "status", "value": {"status": "IN_PROGRESS"}}]
                    if operation == "status"
                    else [
                        {
                            "op": "result",
                            "value": {
                                "data": {"images": [{"url": self.png_data_uri}]}
                            },
                        }
                    ]
                )
                result = self.run_transport(
                    run=run,
                    pricing=pricing,
                    request={"invalid": "new-submit-only"},
                    fake_events=events,
                )
                self.assertNotIn("submit", result.action_log)
                self.assertEqual(
                    ["import", f"{operation}:{request_id}"],
                    result.action_log,
                )

    def test_transport_blocks_when_full_remaining_policy_exceeds_cap(self):
        pricing = self.valid_pricing()
        pricing["prices"][0]["unit_price"] = 0.06
        pricing["prices"][1]["unit_price"] = 0.05
        pricing["prices"][1]["resolution_multipliers"]["0.5K"] = 1.0

        result = self.assert_preflight_blocked(pricing=pricing)

        self.assertEqual("BUDGET_WORST_CASE_EXCEEDED", result.payload["reason"])

    def test_transport_rejects_missing_or_stale_authenticated_price_evidence(self):
        cases = []
        missing_price = self.valid_pricing()
        missing_price["prices"] = missing_price["prices"][:1]
        cases.append(("missing_endpoint", missing_price, "PRICE_NOT_VERIFIABLE"))
        stale = self.valid_pricing()
        stale["verified_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=16)
        ).isoformat().replace("+00:00", "Z")
        cases.append(("stale", stale, "PRICE_STALE"))
        unauthenticated = self.valid_pricing()
        unauthenticated["source_url"] = "https://example.invalid/pricing"
        cases.append(("unauthenticated", unauthenticated, "PRICE_NOT_VERIFIABLE"))

        for name, pricing, reason in cases:
            with self.subTest(case=name):
                result = self.assert_preflight_blocked(pricing=pricing)
                self.assertEqual(reason, result.payload["reason"])

    def test_transport_rejects_price_currency_unit_or_multiplier_mismatch(self):
        cases = []
        for field, value in (("currency", "EUR"), ("unit", "megapixel")):
            pricing = self.valid_pricing()
            pricing["prices"][0][field] = value
            cases.append((field, pricing))
        multiplier = self.valid_pricing()
        del multiplier["prices"][1]["resolution_multipliers"]["0.5K"]
        cases.append(("multiplier", multiplier))

        for name, pricing in cases:
            with self.subTest(case=name):
                result = self.assert_preflight_blocked(pricing=pricing)
                self.assertEqual("PRICE_NOT_VERIFIABLE", result.payload["reason"])

    def test_transport_requires_a_nonempty_environment_credential(self):
        result = self.assert_preflight_blocked(key=None)

        self.assertEqual("FAL_KEY_MISSING", result.payload["reason"])

    def test_test_project_root_seam_maps_only_the_prepared_project_with_all_guards(self):
        result = self.assert_preflight_blocked(key=None)

        self.assertEqual("FAL_KEY_MISSING", result.payload["reason"])
        self.assertEqual(str(self.test_project_root.resolve()), result.run["project_root"])
        self.assertEqual(str(self.test_source_path.resolve()), result.run["source_path"])
        self.assertTrue(self.test_project_root.resolve().is_relative_to(self.temp_root))
        self.assertTrue(self.test_source_path.resolve().is_relative_to(self.temp_root))
        self.assertTrue(self.test_source_path.is_file())

    def test_test_project_root_seam_rejects_missing_test_mode_or_client_module(self):
        for name, arguments in (
            ("missing_node_env", {"node_env": None}),
            ("wrong_node_env", {"node_env": "production"}),
            ("missing_client_module", {"include_client_module": False}),
        ):
            with self.subTest(case=name):
                result = self.assert_preflight_blocked(**arguments)
                self.assertEqual("INVALID_ARGUMENTS", result.payload["reason"])

    def test_test_project_root_seam_rejects_client_module_outside_fake_root(self):
        external_directory = tempfile.TemporaryDirectory()
        self.addCleanup(external_directory.cleanup)
        external_client = Path(external_directory.name) / "external-client.mjs"
        shutil.copy2(self.client_path, external_client)

        result = self.assert_preflight_blocked(
            client_module=external_client,
            key=None,
        )

        self.assertEqual("INVALID_ARGUMENTS", result.payload["reason"])

    def test_test_project_root_seam_rejects_project_root_outside_fake_root(self):
        external_directory = tempfile.TemporaryDirectory()
        self.addCleanup(external_directory.cleanup)
        external_project = Path(external_directory.name) / "external-project"
        external_project.mkdir()

        result = self.assert_preflight_blocked(test_project_root=external_project)

        self.assertEqual("INVALID_ARGUMENTS", result.payload["reason"])

    def test_test_project_root_seam_rejects_absent_fake_root(self):
        result = self.assert_preflight_blocked(fake_root=None, key=None)

        self.assertEqual("INVALID_ARGUMENTS", result.payload["reason"])

    def test_absent_test_project_root_preserves_literal_production_confinement(self):
        result = self.assert_preflight_blocked(test_project_root=None)

        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", result.payload["reason"])

    def test_transport_counts_all_primary_submits_toward_the_global_cap(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = [
            self.request_record(
                f"req-{index}", status="FAILED", attempt=index, estimate=0.01
            )
            for index in range(1, 11)
        ]

        result = self.assert_preflight_blocked(run=run)

        self.assertEqual("ATTEMPT_LIMIT_REACHED", result.payload["reason"])
        self.assertEqual(10, len(result.run["requests"]))

    def test_transport_counts_two_nano_submits_and_applies_the_0_5k_multiplier(self):
        run, request = self.fallback_ready_transport_run()
        for frame_index in (3, 4):
            attempt = len(run["requests"]) + 1
            run["requests"].append(
                self.request_record(
                    f"nano-{frame_index}",
                    model=self.manifest["fallback_model"],
                    status="FAILED",
                    estimate=0.02,
                    frame_index=frame_index,
                    attempt=attempt,
                )
            )

        result = self.assert_preflight_blocked(run=run, request=request)

        self.assertEqual("ATTEMPT_LIMIT_REACHED", result.payload["reason"])
        self.assertEqual(
            [0.02, 0.02],
            [
                record["estimated_cost_usd"]
                for record in result.run["requests"]
                if record["model"] == self.manifest["fallback_model"]
            ],
        )

    def test_transport_estimates_a_new_nano_submit_with_the_0_5k_multiplier(self):
        run, request = self.fallback_ready_transport_run()

        result = self.run_transport(
            run=run,
            request=request,
            fake_events=[
                {"op": "submit", "value": {"request_id": "nano-new"}},
                {"op": "status", "value": {"status": "IN_PROGRESS"}},
            ],
        )

        self.assertEqual(0.02, result.run["requests"][-1]["estimated_cost_usd"])
        self.assertEqual(["import", "submit", "status:nano-new"], result.action_log)

    def test_transport_persists_unknown_submit_as_counted_null_id_and_never_resends(self):
        result = self.run_transport(
            fake_events=[
                {
                    "op": "submit",
                    "throw": True,
                    "error_name": "TimeoutError",
                    "message": "secret rejected data:image/png;base64,AAAA",
                }
            ],
            expected_code=2,
        )

        self.assertEqual(["import", "submit"], result.action_log)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", result.payload["reason"])
        self.assertEqual(1, len(result.run["requests"]))
        self.assertIsNone(result.run["requests"][0]["request_id"])
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", result.run["requests"][0]["status"])
        self.assertEqual("TimeoutError", result.run["requests"][0]["safe_error_type"])
        persisted_text = self.run_manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("secret rejected", persisted_text)
        self.assertNotIn("data:image", persisted_text)

        blocked_again = self.run_transport(expected_code=2)
        self.assertEqual([], blocked_again.action_log)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", blocked_again.payload["reason"])
        self.assertEqual(1, len(blocked_again.run["requests"]))

    def test_transport_persists_the_id_before_a_status_failure(self):
        result = self.run_transport(
            fake_events=[
                {"op": "submit", "value": {"request_id": "req-durable"}},
                {"op": "status", "throw": True, "error_name": "NetworkError"},
            ],
            expected_code=2,
        )

        self.assertEqual("req-durable", result.run["requests"][0]["request_id"])
        self.assertEqual("IN_QUEUE", result.run["requests"][0]["status"])
        self.assertEqual("REQUEST_STATUS_UNKNOWN", result.payload["reason"])
        self.assertEqual(1, result.payload["attempts"])
        self.assertEqual(["req-durable"], result.payload["request_ids"])

    def test_transport_persists_only_safe_audit_fields_and_never_the_key_or_result_uri(self):
        result = self.run_transport(
            fake_events=[
                {"op": "submit", "value": {"request_id": "req-safe"}},
                {"op": "status", "value": {"status": "COMPLETED"}},
                {
                    "op": "result",
                    "value": {"data": {"images": [{"url": self.png_data_uri}]}},
                },
            ]
        )

        request = result.run["requests"][0]
        self.assertEqual(
            {
                "frame_index",
                "attempt",
                "model",
                "request_id",
                "estimated_cost_usd",
                "status",
                "submitted_at",
                "result_path",
                "result_sha256",
                "request_fingerprint",
            },
            set(request),
        )
        audit = self.run_manifest_path.read_text(encoding="utf-8") + result.stdout
        self.assertNotIn("unit-test-fal-key-never-persist", audit)
        self.assertNotIn("data:image/png", audit)
        self.assertNotIn("authorization", audit.lower())

    def test_transport_keeps_permanent_422_details_to_a_safe_error_type_only(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = [self.request_record("req-422", status="COMPLETED")]

        result = self.run_transport(
            run=run,
            fake_events=[
                {
                    "op": "result",
                    "throw": True,
                    "error_name": "FalError",
                    "status": 422,
                    "message": "secret bearer and rejected data URI",
                    "body": {
                        "detail": [
                            {"type": "image_too_small", "input": "secret-input"}
                        ]
                    },
                }
            ],
        )

        self.assertEqual("FAILED", result.run["requests"][0]["status"])
        self.assertEqual("image_too_small", result.run["requests"][0]["safe_error_type"])
        persisted = self.run_manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("secret", persisted)
        self.assertNotIn("input", result.run["requests"][0])

    def test_transport_never_persists_credential_shaped_error_names(self):
        credential = "CredentialShape123"
        for name, error_name in (
            ("equal", credential),
            ("contains", f"Client-{credential}-Failure"),
        ):
            with self.subTest(case=name):
                result = self.run_transport(
                    run=copy.deepcopy(self.initial_run),
                    key=credential,
                    fake_events=[
                        {
                            "op": "submit",
                            "throw": True,
                            "error_name": error_name,
                            "message": "injected failure without credential text",
                        }
                    ],
                    expected_code=2,
                )
                self.assertEqual(
                    "SubmitError", result.run["requests"][0]["safe_error_type"]
                )
                audit = self.run_manifest_path.read_text(encoding="utf-8")
                self.assertNotIn(credential, audit)

    def test_transport_never_persists_credential_shaped_detail_types(self):
        credential = "CredentialShape123"
        for name, detail_type in (
            ("equal", credential),
            ("contains", f"server_{credential}_detail"),
        ):
            with self.subTest(case=name):
                run = copy.deepcopy(self.initial_run)
                run["requests"] = [
                    self.request_record("req-credential", status="COMPLETED")
                ]
                result = self.run_transport(
                    run=run,
                    key=credential,
                    fake_events=[
                        {
                            "op": "result",
                            "throw": True,
                            "error_name": "FalError",
                            "status": 422,
                            "body": {"detail": [{"type": detail_type}]},
                        }
                    ],
                )
                self.assertEqual(
                    "RESULT_VALIDATION_ERROR",
                    result.run["requests"][0]["safe_error_type"],
                )
                audit = self.run_manifest_path.read_text(encoding="utf-8")
                self.assertNotIn(credential, audit)

    def test_transport_rejects_non_data_non_https_result_without_writing_raw(self):
        result = self.run_transport(
            fake_events=[
                {"op": "submit", "value": {"request_id": "req-url"}},
                {"op": "status", "value": {"status": "COMPLETED"}},
                {
                    "op": "result",
                    "value": {"data": {"images": [{"url": "file:///secret.png"}]}},
                },
            ],
            expected_code=2,
        )

        self.assertEqual("RESULT_URL_NOT_ALLOWED", result.payload["reason"])
        self.assertEqual([], list((self.run_dir / "raw").rglob("*.png")))
        self.assertNotIn("file:///", self.run_manifest_path.read_text(encoding="utf-8"))

    def test_transport_validates_request_shape_and_nano_resolution_before_import(self):
        cases = (
            ("bad_frame", {"frame_index": 0, "model": self.manifest["primary_model"], "input": {}}),
            ("bad_model", {"frame_index": 1, "model": "fal-ai/other", "input": {}}),
            (
                "bad_nano_resolution",
                {
                    "frame_index": 1,
                    "model": self.manifest["fallback_model"],
                    "input": {"resolution": "1K"},
                },
            ),
            (
                "extra_field",
                {
                    "frame_index": 1,
                    "model": self.manifest["primary_model"],
                    "input": {},
                    "credential": "must-not-be-accepted",
                },
            ),
        )
        for name, request in cases:
            with self.subTest(case=name):
                result = self.assert_preflight_blocked(request=request)
                self.assertEqual("INVALID_REQUEST_INPUT", result.payload["reason"])

    def test_transport_rejects_a_run_manifest_outside_the_prepared_reports_path(self):
        wrong_path = self.run_dir / "run_manifest.json"
        wrong_path.write_text(json.dumps(self.initial_run), encoding="utf-8")

        result = self.assert_preflight_blocked(run_manifest_path=wrong_path)

        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", result.payload["reason"])
        self.assertEqual([], list((self.run_dir / "raw").rglob("*.png")))

    def test_transport_rejects_a_linked_output_ancestor_before_any_write(self):
        escaped_directory = self.temp_root / "escaped-raw"
        escaped_directory.mkdir()
        frame_directory = self.run_dir / "raw/frame-01"
        self.create_directory_link(escaped_directory, frame_directory)

        result = self.run_transport(
            fake_events=[
                {"op": "submit", "value": {"request_id": "req-linked"}},
                {"op": "status", "value": {"status": "COMPLETED"}},
                {
                    "op": "result",
                    "value": {"data": {"images": [{"url": self.png_data_uri}]}},
                },
            ],
            expected_code=2,
        )

        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", result.payload["reason"])
        self.assertEqual([], list(escaped_directory.glob("*.png")))
        self.assertIsNone(result.run["requests"][0]["result_path"])

    def test_transport_rejects_structurally_invalid_png_payloads(self):
        corrupt_crc = bytearray(self.png_bytes)
        corrupt_crc[29] ^= 1
        cases = (
            ("signature_only", self.png_bytes[:8]),
            ("corrupt_crc", bytes(corrupt_crc)),
            ("truncated", self.png_bytes[:-4]),
            ("trailing_bytes", self.png_bytes + b"trailing"),
        )

        for name, payload in cases:
            with self.subTest(case=name):
                result = self.run_transport(
                    run=copy.deepcopy(self.initial_run),
                    fake_events=[
                        {"op": "submit", "value": {"request_id": f"req-{name}"}},
                        {"op": "status", "value": {"status": "COMPLETED"}},
                        {
                            "op": "result",
                            "value": {
                                "data": {
                                    "images": [
                                        {"url": self.png_data_uri_for(payload)}
                                    ]
                                }
                            },
                        },
                    ],
                    expected_code=2,
                )

                self.assertEqual("RESULT_NOT_PNG", result.payload["reason"])
                self.assertIsNone(result.run["requests"][0]["result_path"])
        self.assertEqual([], list((self.run_dir / "raw").rglob("*.png")))

    def test_transport_queries_an_over_cap_pending_id_before_cap_decisions(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = [
            self.request_record(
                f"req-failed-{index}",
                status="FAILED",
                estimate=0.01,
                attempt=index,
            )
            for index in range(1, 11)
        ] + [
            self.request_record(
                "req-over-cap",
                status="IN_PROGRESS",
                estimate=0.01,
                attempt=11,
            )
        ]

        result = self.run_transport(
            run=run,
            fake_events=[
                {"op": "status", "value": {"status": "IN_PROGRESS"}},
            ],
        )

        self.assertEqual(["import", "status:req-over-cap"], result.action_log)
        self.assertEqual("REQUEST_PENDING", result.payload["reason"])
        self.assertEqual(11, len(result.run["requests"]))

    def test_transport_rejects_a_gapped_attempt_sequence_before_submit_or_output(self):
        run = copy.deepcopy(self.initial_run)
        run["requests"] = [
            self.request_record("req-1", status="FAILED", attempt=1, estimate=0.01),
            self.request_record("req-3", status="FAILED", attempt=3, estimate=0.01),
        ]

        result = self.assert_preflight_blocked(run=run)

        self.assertEqual("INVALID_RUN_MANIFEST", result.payload["reason"])
        self.assertEqual([], list((self.run_dir / "raw").rglob("*.png")))

if __name__ == "__main__":
    unittest.main()
