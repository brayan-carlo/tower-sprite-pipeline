import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw


sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid_piece_reconstruction as hybrid
import hybrid_joint_reconstruction as joint


SKILL_ROOT = Path(__file__).resolve().parents[1]


def required_external_file(environment_name: str) -> Path:
    value = os.environ.get(environment_name)
    if not value:
        raise RuntimeError(f"{environment_name}_REQUIRED")
    return Path(value).resolve(strict=True)


ORIGINAL_MASTER = required_external_file("TOWER_SPRITE_ORIGINAL_MASTER")
HYBRID_MANIFEST = SKILL_ROOT / "assets/pilots/ogre_walk_s_8f_hybrid.json"


class HybridTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name)
        self.start_skill_root_patch()
        self.project_root = self.temporary_root / "project"
        self.source_path = self.project_root / "source" / "master.png"
        self.source_path.parent.mkdir(parents=True)
        Image.new("RGBA", hybrid.NORMALIZED_SIZE, (0, 0, 0, 0)).save(
            self.source_path
        )
        self.manifest_path = self.write_manifest(self.valid_manifest())

    def start_skill_root_patch(self):
        self.skill_root = self.temporary_root / "skill"
        (self.skill_root / "assets" / "pilots").mkdir(parents=True)
        patcher = patch.object(hybrid, "SKILL_ROOT", self.skill_root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def valid_joint_occluders(self):
        return {
            "left_hip": ["loincloth_front"],
            "right_hip": ["loincloth_front"],
        }

    def valid_ownership_map_value(self):
        path = self.skill_root / "assets" / "pilots" / "ownership.png"
        Image.new("L", hybrid.TECHNICAL_SIZE, 0).save(path)
        return {
            "path": "assets/pilots/ownership.png",
            "sha256": hybrid.sha256_file(path),
            "labels": {name: index for index, name in enumerate(hybrid.LAYER_NAMES, 1)},
        }

    def valid_explicit_manifest(self):
        manifest = self.valid_manifest()
        del manifest["ownership_order"]
        manifest["ownership_map"] = self.valid_ownership_map_value()
        manifest["joint_occluders"] = self.valid_joint_occluders()
        return manifest

    def valid_manifest(self) -> dict:
        parts = {
            "loincloth_front": {
                "polygon": [[178, 420], [356, 420], [370, 704], [146, 704]],
                "pivot": [254, 450],
                "kind": "flexible",
            },
            "screen_left_upper_arm": {
                "polygon": [[68, 248], [190, 270], [198, 454], [80, 472], [34, 366]],
                "pivot": [170, 338],
                "kind": "articulated",
            },
            "screen_left_lower_arm_hand": {
                "polygon": [[24, 398], [176, 394], [170, 606], [32, 612], [8, 520]],
                "pivot": [124, 466],
                "kind": "articulated",
            },
            "screen_right_upper_arm": {
                "polygon": [[292, 252], [410, 270], [470, 444], [340, 476]],
                "pivot": [338, 338],
                "kind": "articulated",
            },
            "screen_right_lower_arm_hand": {
                "polygon": [[334, 394], [476, 398], [488, 610], [356, 610]],
                "pivot": [384, 466],
                "kind": "articulated",
            },
            "screen_left_upper_leg": {
                "polygon": [[126, 474], [260, 474], [252, 684], [116, 684]],
                "pivot": [200, 518],
                "kind": "articulated",
            },
            "screen_left_lower_leg_foot": {
                "polygon": [[104, 630], [256, 630], [260, 830], [64, 830]],
                "pivot": [178, 666],
                "kind": "articulated",
            },
            "screen_right_upper_leg": {
                "polygon": [[238, 474], [388, 474], [410, 684], [246, 684]],
                "pivot": [308, 518],
                "kind": "articulated",
            },
            "screen_right_lower_leg_foot": {
                "polygon": [[244, 630], [416, 630], [464, 830], [236, 830]],
                "pivot": [330, 666],
                "kind": "articulated",
            },
        }
        poses = {}
        for frame_index, name in enumerate(hybrid.POSE_NAMES, start=1):
            pose = {
                "frame_index": frame_index,
                "name": name,
                "support_foot": "left" if name.startswith("left_") else "right",
                "phase": name.split("_", 1)[1],
                "stride": 0.0,
                "counter_swing": 0.0,
                "body_y": 0.0,
            }
            if frame_index in hybrid.CONTACT_FRAME_INDICES:
                pose.update(
                    {
                        "transforms": {
                            part_name: {
                                "translation": [0.0, 0.0],
                                "rotation_degrees": 0.0,
                            }
                            for part_name in hybrid.PART_NAMES
                        },
                        "layer_order": [
                            "screen_left_upper_leg",
                            "screen_right_upper_leg",
                            "screen_left_lower_leg_foot",
                            "screen_right_lower_leg_foot",
                            "core",
                            "loincloth_front",
                            "screen_left_upper_arm",
                            "screen_right_upper_arm",
                            "screen_left_lower_arm_hand",
                            "screen_right_lower_arm_hand",
                        ],
                        "visibility": {
                            part_name: True for part_name in hybrid.PART_NAMES
                        },
                        "flexible_polygons": {
                            part_name: copy.deepcopy(part["polygon"])
                            for part_name, part in parts.items()
                        },
                        "chroma_rgb": [255, 0, 255],
                        "chain_transforms": self.valid_chain_transforms(),
                    }
                )
            poses[name] = pose

        return {
            "mode": "hybrid_piece_reconstruction",
            "project_root": str(self.project_root),
            "source_path": "source/master.png",
            "entity": "ogre",
            "action": "walk",
            "direction": "S",
            "orientation": "S_3_4",
            "frame_count": 8,
            "frame_width": 256,
            "frame_height": 416,
            "technical_scale": 2,
            "anchor": "bottom-center",
            "root_motion": False,
            "pilot_contact_frames": [1, 5],
            "ownership_order": [
                "loincloth_front",
                "screen_left_lower_arm_hand",
                "screen_right_lower_arm_hand",
                "screen_left_upper_arm",
                "screen_right_upper_arm",
                "screen_left_lower_leg_foot",
                "screen_right_lower_leg_foot",
                "screen_left_upper_leg",
                "screen_right_upper_leg",
            ],
            "core_pivot": [254, 354],
            "parts": parts,
            "joint_chains": self.valid_joint_chains(),
            "poses": poses,
            "qa": {
                "contact_foot_separation_bbox_ratio_min": 0.1,
                "hand_excursion_bbox_ratio_min": 0.05,
                "protected_rgba_mismatch_max": 0,
            },
        }

    def joint_value(self, name, parent_part, child_part, parent_anchor, child_anchor):
        x, y = parent_anchor
        return {
            "name": name,
            "parent_part": parent_part,
            "child_part": child_part,
            "parent_anchor": list(parent_anchor),
            "child_anchor": list(child_anchor),
            "repair_polygon": [
                [x, y - 24],
                [x + 16, y - 16],
                [x + 24, y],
                [x + 16, y + 16],
                [x, y + 24],
                [x - 16, y + 16],
                [x - 24, y],
                [x - 16, y - 16],
            ],
            "max_radius": 24,
        }

    def valid_joint_chains(self) -> dict:
        return {
            "screen_left_arm": {
                "root_part": "core",
                "joints": [
                    self.joint_value("left_shoulder", "core", "screen_left_upper_arm", (170, 338), (170, 338)),
                    self.joint_value("left_elbow", "screen_left_upper_arm", "screen_left_lower_arm_hand", (124, 466), (124, 466)),
                ],
            },
            "screen_right_arm": {
                "root_part": "core",
                "joints": [
                    self.joint_value("right_shoulder", "core", "screen_right_upper_arm", (338, 338), (338, 338)),
                    self.joint_value("right_elbow", "screen_right_upper_arm", "screen_right_lower_arm_hand", (384, 466), (384, 466)),
                ],
            },
            "screen_left_leg": {
                "root_part": "core",
                "joints": [
                    self.joint_value("left_hip", "core", "screen_left_upper_leg", (200, 518), (200, 518)),
                    self.joint_value("left_knee", "screen_left_upper_leg", "screen_left_lower_leg_foot", (178, 666), (178, 666)),
                ],
            },
            "screen_right_leg": {
                "root_part": "core",
                "joints": [
                    self.joint_value("right_hip", "core", "screen_right_upper_leg", (308, 518), (308, 518)),
                    self.joint_value("right_knee", "screen_right_upper_leg", "screen_right_lower_leg_foot", (330, 666), (330, 666)),
                ],
            },
        }

    def valid_chain_transforms(self) -> dict:
        return {
            "screen_left_arm": {"root_translation": [0.0, 0.0], "rotation_degrees": {"screen_left_upper_arm": 0.0, "screen_left_lower_arm_hand": 0.0}},
            "screen_right_arm": {"root_translation": [0.0, 0.0], "rotation_degrees": {"screen_right_upper_arm": 0.0, "screen_right_lower_arm_hand": 0.0}},
            "screen_left_leg": {"root_translation": [0.0, 0.0], "rotation_degrees": {"screen_left_upper_leg": 0.0, "screen_left_lower_leg_foot": 0.0}},
            "screen_right_leg": {"root_translation": [0.0, 0.0], "rotation_degrees": {"screen_right_upper_leg": 0.0, "screen_right_lower_leg_foot": 0.0}},
        }

    def write_manifest(self, value: dict) -> Path:
        path = self.temporary_root / f"manifest-{len(list(self.temporary_root.glob('manifest-*.json')))}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def remove_dotted_key(self, value: dict, dotted_key: str) -> dict:
        result = copy.deepcopy(value)
        parent = result
        keys = dotted_key.split(".")
        for key in keys[:-1]:
            parent = parent[key]
        del parent[keys[-1]]
        return result


class RealOgreContactAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name) / "external-workspace"
        self.workspace.mkdir()
        self.run_id = "20260829-task-6-real-ogre-contact-poses"

    def test_real_contacts_pass_joint_reconstruction_gates(self):
        before = hybrid.sha256_file(ORIGINAL_MASTER)
        run_dir = hybrid.prepare_pilot(
            hybrid.validate_manifest(HYBRID_MANIFEST), self.workspace, self.run_id
        )
        report = json.loads(
            (run_dir / "reports" / "contact_qa.json").read_text(encoding="utf-8")
        )

        self.assertEqual("PASS", report["technical_result"], report["failures"])
        self.assertNotIn("GROUND_LINE_MISMATCH", report["failures"])
        self.assertNotIn("CENTER_DRIFT", report["failures"])
        self.assertNotIn("NEW_ALPHA_COMPONENTS", report["failures"])
        self.assertEqual(0, report["checks"]["protected_pixels"]["rgba_mismatch"])
        self.assertEqual(before, hybrid.sha256_file(ORIGINAL_MASTER))


class HybridManifestTests(HybridTestCase):
    def test_codex_home_project_root_token_resolves_inside_codex_home(self):
        portable = self.temporary_root / "portable-codex"
        source_root = portable / "work" / "tower-sprite-pipeline" / "master"
        source_root.mkdir(parents=True)
        Image.new("RGBA", hybrid.TECHNICAL_SIZE).save(source_root / "master.png")
        manifest = self.valid_manifest()
        manifest["project_root"] = "${CODEX_HOME}/work/tower-sprite-pipeline/master"
        manifest["source_path"] = "master.png"
        with patch.dict(os.environ, {"CODEX_HOME": str(portable)}):
            validated = hybrid.validate_manifest(self.write_manifest(manifest))
        self.assertEqual(manifest["project_root"], validated["project_root"])

    def test_project_root_rejects_unknown_and_escaping_tokens(self):
        portable = self.temporary_root / "portable-codex"
        portable.mkdir()
        with patch.dict(os.environ, {"CODEX_HOME": str(portable)}):
            for value in ("${HOME}/master", "${CODEX_HOME}/../outside", "%USERPROFILE%/master"):
                with self.subTest(value=value), self.assertRaisesRegex(
                    hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"
                ):
                    hybrid._resolve_project_root(value)

    def test_polygon_and_explicit_ownership_contracts_are_mutually_exclusive(self):
        broken = self.valid_manifest()
        broken["ownership_map"] = self.valid_ownership_map_value()
        broken["joint_occluders"] = self.valid_joint_occluders()
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(broken))

    def test_explicit_contract_requires_exact_labels_hash_and_hip_occluders(self):
        manifest = self.valid_explicit_manifest()
        validated = hybrid.validate_manifest(self.write_manifest(manifest))
        contract = hybrid._explicit_ownership_contract(validated)
        self.assertEqual(frozenset(hybrid.LAYER_NAMES), frozenset(contract.labels))
        self.assertEqual(
            {"left_hip": ("loincloth_front",), "right_hip": ("loincloth_front",)},
            contract.joint_occluders,
        )

    def test_explicit_ownership_hash_failures_map_to_part_ownership_error(self):
        manifest = self.valid_explicit_manifest()
        with patch.object(
            hybrid,
            "sha256_file",
            side_effect=hybrid.HybridBlocked("ARTIFACT_INVALID"),
        ):
            with self.assertRaisesRegex(
                hybrid.HybridBlocked, "PART_OWNERSHIP_MAP_INVALID"
            ):
                hybrid.validate_manifest(self.write_manifest(manifest))

    def test_manifest_requires_joint_chains_and_exact_contact_chain_transforms(self):
        manifest = hybrid.validate_manifest(self.manifest_path)
        self.assertEqual(
            ("screen_left_arm", "screen_right_arm", "screen_left_leg", "screen_right_leg"),
            tuple(manifest["joint_chains"]),
        )

        missing_chains = self.valid_manifest()
        del missing_chains["joint_chains"]
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(missing_chains))

        child_translation = self.valid_manifest()
        child_translation["poses"]["left_contact"]["chain_transforms"]["screen_left_arm"]["translation"] = [1.0, 0.0]
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(child_translation))

    def test_accepts_only_the_literal_hybrid_contract(self):
        manifest = hybrid.validate_manifest(self.manifest_path)
        self.assertEqual("hybrid_piece_reconstruction", manifest["mode"])
        self.assertEqual([1, 5], manifest["pilot_contact_frames"])
        self.assertEqual((256, 416), hybrid.NORMALIZED_SIZE)
        self.assertEqual((512, 832), hybrid.TECHNICAL_SIZE)

    def test_contact_layer_order_requires_core_exactly_once(self):
        manifest = hybrid.validate_manifest(self.manifest_path)
        self.assertEqual(1, manifest["poses"]["left_contact"]["layer_order"].count("core"))
        self.assertLess(
            manifest["poses"]["left_contact"]["layer_order"].index(
                "screen_left_lower_leg_foot"
            ),
            manifest["poses"]["left_contact"]["layer_order"].index("core"),
        )
        self.assertLess(
            manifest["poses"]["left_contact"]["layer_order"].index("core"),
            manifest["poses"]["left_contact"]["layer_order"].index("loincloth_front"),
        )

        for malformed_order in (
            [name for name in manifest["poses"]["left_contact"]["layer_order"] if name != "core"],
            [*manifest["poses"]["left_contact"]["layer_order"], "core"],
        ):
            with self.subTest(malformed_order=malformed_order):
                broken = self.valid_manifest()
                broken["poses"]["left_contact"]["layer_order"] = malformed_order
                with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
                    hybrid.validate_manifest(self.write_manifest(broken))

    def test_json_equivalent_but_wrong_types_block(self):
        cases = (
            ("root_motion_integer", "root_motion", 0),
            ("frame_count_float", "frame_count", 8.0),
            ("frame_width_float", "frame_width", 256.0),
            ("frame_height_float", "frame_height", 416.0),
            ("technical_scale_float", "technical_scale", 2.0),
            ("pilot_contact_boolean", "pilot_contact_frames", [True, 5]),
            ("pilot_contact_float", "pilot_contact_frames", [1.0, 5]),
        )
        for name, field, value in cases:
            with self.subTest(case=name):
                broken = self.valid_manifest()
                broken[field] = value
                with self.assertRaisesRegex(
                    hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"
                ):
                    hybrid.validate_manifest(self.write_manifest(broken))

        broken_frame_index = self.valid_manifest()
        broken_frame_index["poses"]["left_contact"]["frame_index"] = 1.0
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(broken_frame_index))

    def test_missing_nested_piece_pose_or_qa_field_blocks(self):
        for dotted_key in (
            "ownership_order",
            "core_pivot",
            "parts.screen_left_upper_arm.polygon",
            "parts.screen_left_upper_arm.pivot",
            "parts.screen_left_upper_arm.kind",
            "poses.left_contact.transforms",
            "poses.right_contact.layer_order",
            "qa.contact_foot_separation_bbox_ratio_min",
            "qa.hand_excursion_bbox_ratio_min",
            "qa.protected_rgba_mismatch_max",
        ):
            with self.subTest(dotted_key=dotted_key):
                broken = self.remove_dotted_key(self.valid_manifest(), dotted_key)
                with self.assertRaisesRegex(
                    hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"
                ):
                    hybrid.validate_manifest(self.write_manifest(broken))

    def test_atlas_configuration_types_and_ownership_order_are_required(self):
        invalid_cases = (
            ("duplicate_owner", "ownership_order", [hybrid.PART_NAMES[0]] * len(hybrid.PART_NAMES)),
            ("wrong_order", "ownership_order", list(reversed(hybrid.PART_NAMES))),
            ("core_pivot_wrong", "core_pivot", [128, 208]),
            ("core_pivot_float", "core_pivot", [254.0, 354]),
            ("kind_empty", "parts.screen_left_upper_arm.kind", ""),
            ("kind_nonstring", "parts.screen_left_upper_arm.kind", 1),
            ("kind_wrong_literal", "parts.loincloth_front.kind", "articulated"),
        )
        for name, dotted_key, value in invalid_cases:
            with self.subTest(case=name):
                broken = self.valid_manifest()
                parent = broken
                keys = dotted_key.split(".")
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                with self.assertRaisesRegex(
                    hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"
                ):
                    hybrid.validate_manifest(self.write_manifest(broken))

    def test_sequential_or_deterministic_manifest_cannot_route_into_hybrid_mode(self):
        broken = self.valid_manifest()
        broken["mode"] = "sequential_reconstruction"
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(broken))

    def test_unknown_nested_contract_field_blocks(self):
        broken = self.valid_manifest()
        broken["parts"]["screen_left_upper_arm"]["unexpected"] = True
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(broken))

    def test_contact_payload_is_required_only_on_pilot_contact_frames(self):
        missing_contact_payload = self.valid_manifest()
        del missing_contact_payload["poses"]["left_contact"]["visibility"]
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(missing_contact_payload))

        unexpected_intermediate_payload = self.valid_manifest()
        unexpected_intermediate_payload["poses"]["left_down"]["chroma_rgb"] = [255, 0, 255]
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(unexpected_intermediate_payload))

    def test_source_must_exist_below_project_root(self):
        missing_source = self.valid_manifest()
        missing_source["source_path"] = "source/missing.png"
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(missing_source))

        escaped_source = self.valid_manifest()
        escaped_source["source_path"] = "../outside.png"
        with self.assertRaisesRegex(hybrid.HybridBlocked, "HYBRID_MANIFEST_INVALID"):
            hybrid.validate_manifest(self.write_manifest(escaped_source))


class PartAtlasTests(HybridTestCase):
    def synthetic_master(self) -> Image.Image:
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(master)
        draw.rectangle((10, 10, 20, 20), fill=(25, 50, 75, 255))
        draw.rectangle((100, 100, 104, 104), fill=(125, 150, 175, 255))
        return master

    def test_part_atlas_owns_every_opaque_pixel_exactly_once(self):
        master = self.synthetic_master()
        atlas = hybrid.extract_piece_atlas(master, self.valid_manifest())
        masks = [part.ownership_mask for part in atlas.values()]
        self.assertEqual(0, hybrid.overlap_pixel_count(masks))
        self.assertEqual(
            hybrid.opaque_pixel_count(master), hybrid.owned_opaque_pixel_count(atlas)
        )

    def test_extracted_part_rgba_is_byte_exact_to_master(self):
        master = self.synthetic_master()
        atlas = hybrid.extract_piece_atlas(master, self.valid_manifest())
        for part in atlas.values():
            self.assertEqual(0, hybrid.part_source_rgba_mismatch(part, master))

    def test_invalid_polygon_pivot_or_unowned_opaque_pixel_blocks(self):
        manifest = self.valid_manifest()
        manifest["parts"]["screen_left_upper_arm"]["polygon"] = [[0, 0], [1, 1]]
        with self.assertRaisesRegex(hybrid.HybridBlocked, "PART_ATLAS_INVALID"):
            hybrid.extract_piece_atlas(self.synthetic_master(), manifest)

    def test_ownership_order_assigns_an_overlapping_pixel_to_its_first_owner(self):
        manifest = self.valid_manifest()
        first, second = manifest["ownership_order"][:2]
        overlap_polygon = [[200, 400], [300, 400], [300, 500], [200, 500]]
        manifest["parts"][first]["polygon"] = overlap_polygon
        manifest["parts"][second]["polygon"] = overlap_polygon
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        master.putpixel((250, 450), (11, 22, 33, 255))

        atlas = hybrid.extract_piece_atlas(master, manifest)

        self.assertEqual(255, atlas[first].ownership_mask.getpixel((250, 450)))
        self.assertEqual(0, atlas[second].ownership_mask.getpixel((250, 450)))

    def test_technical_polygon_and_pivot_are_used_without_scaling(self):
        manifest = self.valid_manifest()
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        master.putpixel((254, 500), (11, 22, 33, 255))

        atlas = hybrid.extract_piece_atlas(master, manifest)

        first = hybrid.PART_NAMES[0]
        self.assertEqual(255, atlas[first].ownership_mask.getpixel((254, 500)))
        self.assertEqual((254, 450), atlas[first].pivot)
        self.assertEqual((254, 354), atlas["core"].pivot)

    def test_partial_alpha_pixel_has_one_binary_owner_and_exact_source_rgba(self):
        manifest = self.valid_manifest()
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        master.putpixel((254, 500), (11, 22, 33, 128))

        atlas = hybrid.extract_piece_atlas(master, manifest)

        first = hybrid.PART_NAMES[0]
        self.assertEqual(255, atlas[first].ownership_mask.getpixel((254, 500)))
        self.assertEqual(0, atlas["core"].ownership_mask.getpixel((254, 500)))
        self.assertEqual((11, 22, 33, 128), atlas[first].image.getpixel((254, 500)))
        self.assertEqual(0, hybrid.overlap_pixel_count([part.ownership_mask for part in atlas.values()]))
        self.assertEqual(1, hybrid.owned_opaque_pixel_count(atlas))


class ExplicitOwnershipMapTests(HybridTestCase):
    partial_alpha_label = "core"

    def synthetic_explicit_master_and_manifest(self):
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        manifest = self.valid_explicit_manifest()
        labels = manifest["ownership_map"]["labels"]
        ownership = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        for name in hybrid.LAYER_NAMES:
            label_id = labels[name]
            point = (20 + label_id, 20)
            rgba = (10 + label_id, 20 + label_id, 30 + label_id, 128)
            if name != self.partial_alpha_label:
                rgba = rgba[:3] + (255,)
            master.putpixel(point, rgba)
            ownership.putpixel(point, label_id)
        path = self.skill_root / manifest["ownership_map"]["path"]
        ownership.save(path)
        manifest["ownership_map"]["sha256"] = hybrid.sha256_file(path)
        return master, manifest

    def invalid_label_maps(self):
        master, manifest = self.synthetic_explicit_master_and_manifest()
        labels = manifest["ownership_map"]["labels"]
        base = Image.open(self.skill_root / manifest["ownership_map"]["path"])
        try:
            valid_map = base.copy()
        finally:
            base.close()
        invalid = []
        for name, image in (
            ("RGBA_MODE", valid_map.convert("RGBA")),
            ("WRONG_SIZE", Image.new("L", (1, 1), 0)),
            ("LABEL_OVER_TRANSPARENCY", valid_map.copy()),
            ("MISSING_DECLARED_LABEL", valid_map.copy()),
        ):
            if name == "LABEL_OVER_TRANSPARENCY":
                image.putpixel((0, 0), labels[hybrid.LAYER_NAMES[0]])
            if name == "MISSING_DECLARED_LABEL":
                missing_label = labels[self.partial_alpha_label]
                image.putpixel((20 + missing_label, 20), labels[hybrid.LAYER_NAMES[0]])
            path = self.skill_root / "assets" / "pilots" / f"invalid-{name}.png"
            image.save(path)
            broken_manifest = copy.deepcopy(manifest)
            broken_manifest["ownership_map"]["path"] = path.relative_to(self.skill_root).as_posix()
            broken_manifest["ownership_map"]["sha256"] = hybrid.sha256_file(path)
            invalid.append((name, master, broken_manifest))
        return invalid

    def test_explicit_map_owns_every_opaque_pixel_once_and_preserves_rgba(self):
        master, manifest = self.synthetic_explicit_master_and_manifest()
        atlas = hybrid.extract_piece_atlas(master, manifest)

        self.assertEqual(0, hybrid.overlap_pixel_count([p.ownership_mask for p in atlas.values()]))
        self.assertEqual(hybrid.opaque_pixel_count(master), hybrid.owned_opaque_pixel_count(atlas))
        self.assertTrue(all(hybrid.part_source_rgba_mismatch(p, master) == 0 for p in atlas.values()))
        partial_label = manifest["ownership_map"]["labels"][self.partial_alpha_label]
        partial_point = (20 + partial_label, 20)
        self.assertEqual(
            (10 + partial_label, 20 + partial_label, 30 + partial_label, 128),
            atlas[self.partial_alpha_label].image.getpixel(partial_point),
        )

    def test_explicit_map_rejects_wrong_mode_size_background_label_and_missing_part(self):
        for name, master, manifest in self.invalid_label_maps():
            with self.subTest(case=name):
                with self.assertRaisesRegex(hybrid.HybridBlocked, "PART_OWNERSHIP_MAP_INVALID"):
                    hybrid.extract_piece_atlas(master, manifest)

    def test_explicit_map_rejects_labeled_bmp_with_valid_hash(self):
        master, manifest = self.synthetic_explicit_master_and_manifest()
        source = Image.open(self.skill_root / manifest["ownership_map"]["path"])
        try:
            label_map = source.copy()
        finally:
            source.close()
        path = self.skill_root / "assets" / "pilots" / "invalid-format.bmp"
        label_map.save(path, format="BMP")
        manifest["ownership_map"]["path"] = path.relative_to(self.skill_root).as_posix()
        manifest["ownership_map"]["sha256"] = hybrid.sha256_file(path)

        with self.assertRaisesRegex(hybrid.HybridBlocked, "PART_OWNERSHIP_MAP_INVALID"):
            hybrid.extract_piece_atlas(master, manifest)


class PoseCompositionTests(HybridTestCase):
    overlap_probe = (250, 300)
    expected_front_hand_pixel = (220, 40, 60, 255)

    def synthetic_master(self) -> Image.Image:
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        master.putpixel(self.overlap_probe, (20, 40, 60, 255))
        master.putpixel((200, 200), (25, 50, 75, 255))
        return master

    def _part(self, name: str, pixels: dict[tuple[int, int], tuple[int, int, int, int]], pivot: tuple[int, int] = (250, 300)) -> hybrid.Part:
        image = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        ownership = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        for point, color in pixels.items():
            image.putpixel(point, color)
            ownership.putpixel(point, 255)
        kind = "protected" if name == "core" else hybrid.PART_KINDS[name]
        return hybrid.Part(name, image, ownership, pivot, kind)

    def synthetic_atlas(self) -> dict[str, hybrid.Part]:
        atlas = {
            name: self._part(name, {}) for name in hybrid.PART_NAMES
        }
        atlas["screen_left_upper_leg"] = self._part(
            "screen_left_upper_leg", {(210, 300): (40, 80, 120, 255)}
        )
        atlas["loincloth_front"] = self._part(
            "loincloth_front", {(220, 300): (80, 120, 160, 255)}
        )
        atlas["screen_right_lower_arm_hand"] = self._part(
            "screen_right_lower_arm_hand", {(269, 299): self.expected_front_hand_pixel}
        )
        atlas["core"] = self._part(
            "core",
            {
                self.overlap_probe: (20, 40, 60, 255),
                (200, 200): (25, 50, 75, 255),
            },
        )
        return atlas

    def left_contact_pose(self) -> dict:
        pose = copy.deepcopy(self.valid_manifest()["poses"]["left_contact"])
        pose["layer_order"] = [
            "screen_left_upper_leg",
            "core",
            "loincloth_front",
            *(
                name
                for name in hybrid.PART_NAMES
                if name
                not in {
                    "screen_left_upper_leg",
                    "loincloth_front",
                    "screen_right_lower_arm_hand",
                }
            ),
            "screen_right_lower_arm_hand",
        ]
        pose["transforms"]["screen_left_upper_leg"]["translation"] = [40.0, 0.0]
        pose["transforms"]["loincloth_front"]["translation"] = [30.0, 0.0]
        pose["transforms"]["screen_right_lower_arm_hand"]["translation"] = [20.0, 0.0]
        pose["transforms"]["screen_right_lower_arm_hand"]["rotation_degrees"] = 180.0
        pose["flexible_polygons"] = {
            name: [[240, 290], [260, 290], [260, 310], [240, 310]]
            for name in hybrid.PART_NAMES
        }
        return pose

    def contact_composition(self):
        return hybrid.compose_pose(
            self.synthetic_master(), self.synthetic_atlas(), self.left_contact_pose()
        )

    def test_pose_composition_uses_nearest_neighbor_and_declared_layer_order(self):
        composition = self.contact_composition()

        self.assertEqual((512, 832), composition.technical.size)
        self.assertEqual("RGBA", composition.technical.mode)
        self.assertEqual((255, 0, 255, 255), composition.technical.getpixel((0, 0)))
        self.assertEqual(
            self.expected_front_hand_pixel,
            composition.technical.getpixel(self.overlap_probe),
        )

    def test_declared_visibility_reveals_each_layer_from_rear_to_front(self):
        cases = (
            ((), self.expected_front_hand_pixel),
            (("screen_right_lower_arm_hand",), (80, 120, 160, 255)),
            (
                ("screen_right_lower_arm_hand", "loincloth_front"),
                (20, 40, 60, 255),
            ),
        )
        for hidden_layers, expected in cases:
            with self.subTest(hidden_layers=hidden_layers):
                pose = self.left_contact_pose()
                for name in hidden_layers:
                    pose["visibility"][name] = False

                composition = hybrid.compose_pose(
                    self.synthetic_master(), self.synthetic_atlas(), pose
                )

                self.assertEqual(
                    expected, composition.technical.getpixel(self.overlap_probe)
                )

    def test_core_visible_mask_ignores_rear_occlusion_and_subtracts_front_layers(self):
        pose = self.left_contact_pose()
        pose["visibility"]["screen_right_lower_arm_hand"] = False
        pose["visibility"]["loincloth_front"] = False

        composition = hybrid.compose_pose(
            self.synthetic_master(), self.synthetic_atlas(), pose
        )

        self.assertEqual((20, 40, 60, 255), composition.technical.getpixel(self.overlap_probe))
        self.assertEqual(255, composition.protected_visible_mask.getpixel(self.overlap_probe))

        pose["visibility"]["loincloth_front"] = True
        covered = hybrid.compose_pose(self.synthetic_master(), self.synthetic_atlas(), pose)
        self.assertEqual(0, covered.protected_visible_mask.getpixel(self.overlap_probe))

    def test_body_y_compares_protected_rgba_in_transformed_coordinates(self):
        pose = self.left_contact_pose()
        pose["body_y"] = 4.0
        pose["flexible_polygons"] = {
            name: [[190, 194], [210, 194], [210, 214], [190, 214]]
            for name in hybrid.PART_NAMES
        }
        composition = hybrid.compose_pose(
            self.synthetic_master(), self.synthetic_atlas(), pose
        )
        translated_protected_pixel = (200, 204)
        malicious_result = composition.technical.copy()
        malicious_result.putpixel(translated_protected_pixel, (1, 2, 3, 255))
        repair = Image.new("RGBA", composition.technical.size, (1, 2, 3, 255))

        self.assertEqual((25, 50, 75, 255), composition.technical.getpixel(translated_protected_pixel))
        self.assertEqual(0, hybrid.protected_rgba_mismatch(composition.technical, composition))
        self.assertEqual(1, hybrid.protected_rgba_mismatch(malicious_result, composition))
        repaired = hybrid.apply_local_repair(composition, repair)
        self.assertEqual(0, hybrid.protected_rgba_mismatch(repaired, composition))
        self.assertEqual((25, 50, 75, 255), repaired.getpixel(translated_protected_pixel))

    def test_repair_pixels_outside_flexible_mask_are_discarded(self):
        composition = self.contact_composition()
        malicious = Image.new("RGBA", composition.technical.size, (1, 2, 3, 255))

        repaired = hybrid.apply_local_repair(composition, malicious)
        outside = ImageChops.invert(composition.flexible_mask)

        self.assertEqual(
            0,
            hybrid.masked_difference_count(
                repaired, composition.technical, outside
            ),
        )
        self.assertEqual((1, 2, 3, 255), repaired.getpixel((245, 295)))

    def test_local_repair_restores_visible_protected_rgba(self):
        pose = self.left_contact_pose()
        pose["flexible_polygons"] = {
            name: [[190, 190], [210, 190], [210, 210], [190, 210]]
            for name in hybrid.PART_NAMES
        }
        composition = hybrid.compose_pose(
            self.synthetic_master(), self.synthetic_atlas(), pose
        )
        repair = Image.new("RGBA", composition.technical.size, (1, 2, 3, 255))

        repaired = hybrid.apply_local_repair(composition, repair)

        self.assertEqual(0, hybrid.protected_rgba_mismatch(repaired, composition))
        self.assertEqual((25, 50, 75, 255), repaired.getpixel((200, 200)))

    def test_visible_protected_pixels_keep_exact_local_source_rgba(self):
        composition = self.contact_composition()

        self.assertEqual(0, hybrid.protected_rgba_mismatch(composition.technical, composition))
        self.assertEqual((25, 50, 75, 255), composition.technical.getpixel((200, 200)))

    def test_invalid_repair_format_or_dimensions_block(self):
        composition = self.contact_composition()

        for repair in (
            Image.new("RGB", composition.technical.size, (1, 2, 3)),
            Image.new("RGBA", (1, 1), (1, 2, 3, 255)),
        ):
            with self.subTest(repair=repair.mode, size=repair.size):
                with self.assertRaisesRegex(hybrid.HybridBlocked, "PART_ATLAS_INVALID"):
                    hybrid.apply_local_repair(composition, repair)


class JointCompositionIntegrationTests(HybridTestCase):
    """Exercise the hybrid-only contact path with Task 1-4 chain contracts."""

    def _chains(self):
        manifest = self.valid_manifest()
        return joint.validate_joint_contract(
            manifest["joint_chains"], hybrid.PART_NAMES, hybrid.TECHNICAL_SIZE
        )

    def _joint_master_and_atlas(self, detached_child: bool = False):
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        pixels = {name: {} for name in (*hybrid.PART_NAMES, "core")}
        joint_edges = (
            ("core", (168, 338)),
            ("screen_left_upper_arm", (172, 338)),
            ("screen_left_upper_arm", (122, 466)),
            ("screen_left_lower_arm_hand", (126, 466)),
            ("core", (336, 338)),
            ("screen_right_upper_arm", (340, 338)),
            ("screen_right_upper_arm", (382, 466)),
            ("screen_right_lower_arm_hand", (386, 466)),
            ("core", (198, 518)),
            ("screen_left_upper_leg", (202, 518)),
            ("screen_left_upper_leg", (176, 666)),
            ("screen_left_lower_leg_foot", (180, 666)),
            ("core", (306, 518)),
            ("screen_right_upper_leg", (310, 518)),
            ("screen_right_upper_leg", (328, 666)),
            ("screen_right_lower_leg_foot", (332, 666)),
        )
        for index, (name, point) in enumerate(joint_edges, start=1):
            color = (index, 80, 120, 255)
            pixels[name][point] = color
            master.putpixel(point, color)
        if detached_child:
            pixels["screen_left_lower_arm_hand"][(20, 20)] = (25, 50, 75, 255)

        atlas = {}
        for name, part_pixels in pixels.items():
            image = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
            ownership = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
            for point, color in part_pixels.items():
                image.putpixel(point, color)
                ownership.putpixel(point, 255)
            atlas[name] = hybrid.Part(
                name,
                image,
                ownership,
                (254, 354) if name == "core" else tuple(self.valid_manifest()["parts"][name]["pivot"]),
                "protected" if name == "core" else hybrid.PART_KINDS[name],
            )
        return master, atlas

    def _contact_pose(self):
        return copy.deepcopy(self.valid_manifest()["poses"]["left_contact"])

    def test_contact_composition_uses_chain_result_and_confines_repairs(self):
        master, atlas = self._joint_master_and_atlas()

        composition = hybrid.compose_pose(master, atlas, self._contact_pose(), self._chains())

        self.assertIsNotNone(composition.joint_repair_mask.getbbox())
        self.assertEqual(0, hybrid.protected_rgba_mismatch(composition.technical, composition))
        self.assertEqual(0, hybrid.repair_outside_flexible_count(composition))
        self.assertEqual(
            0,
            joint.overlap_count(
                composition.joint_repair_mask, composition.protected_visible_mask
            ),
        )
        self.assertGreater(composition.joint_evidence["repaired_pixel_count"], 0)
        self.assertEqual(
            (90.25, 333.25),
            composition.joint_evidence["landmark_centers"]["screen_left_lower_leg_foot"],
        )

    def test_moving_elbow_translates_effective_mask_to_rounded_target_anchor(self):
        master, atlas = self._joint_master_and_atlas()
        chains = self._chains()
        source_pose = self._contact_pose()
        moved_pose = self._contact_pose()
        moved_pose["chain_transforms"]["screen_left_arm"]["rotation_degrees"][
            "screen_left_upper_arm"
        ] = 1.0

        source = hybrid.compose_pose(master, atlas, source_pose, chains)
        moved = hybrid.compose_pose(master, atlas, moved_pose, chains)
        resolution = joint.resolve_joint_chains(
            atlas, chains, moved_pose["chain_transforms"], hybrid.TECHNICAL_SIZE
        )
        target = resolution.target_anchors["left_elbow"]
        rounded_target = tuple(
            math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)
            for value in target
        )
        delta = (rounded_target[0] - 124, rounded_target[1] - 466)
        source_bbox = source.joint_evidence["joint_masks"]["left_elbow"].getbbox()
        moved_bbox = moved.joint_evidence["joint_masks"]["left_elbow"].getbbox()

        self.assertIsNotNone(source_bbox)
        self.assertIsNotNone(moved_bbox)
        self.assertNotEqual((0, 0), delta)
        self.assertEqual(
            (
                source_bbox[0] + delta[0],
                source_bbox[1] + delta[1],
                source_bbox[2] + delta[0],
                source_bbox[3] + delta[1],
            ),
            moved_bbox,
        )

    def test_joint_masks_exclude_the_visible_protected_mask(self):
        pose = self._contact_pose()
        flexible = hybrid._polygons_to_mask(
            hybrid.TECHNICAL_SIZE, list(pose["flexible_polygons"].values())
        )
        protected = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        protected.putpixel((170, 338), 255)

        target_anchors = {
            definition.name: tuple(float(value) for value in definition.parent_anchor)
            for chain in self._chains()
            for definition in chain.joints
        }

        masks = hybrid._chain_joint_masks(
            self._chains(), target_anchors, flexible, protected
        )

        self.assertEqual(0, joint.overlap_count(masks["left_shoulder"], protected))

    def test_joint_masks_reject_mismatched_target_anchor_keys(self):
        pose = self._contact_pose()
        flexible = hybrid._polygons_to_mask(
            hybrid.TECHNICAL_SIZE, list(pose["flexible_polygons"].values())
        )
        protected = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        target_anchors = {
            definition.name: tuple(float(value) for value in definition.parent_anchor)
            for chain in self._chains()
            for definition in chain.joints
        }

        for invalid in ({}, {**target_anchors, "unexpected": (0.0, 0.0)}):
            with self.subTest(keys=tuple(sorted(invalid))):
                with self.assertRaisesRegex(
                    hybrid.HybridBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
                ):
                    hybrid._chain_joint_masks(
                        self._chains(), invalid, flexible, protected
                    )

    def test_grounding_rejects_clipping_any_support_chain_member(self):
        master, atlas = self._joint_master_and_atlas()
        upper_point = (176, 831)
        foot_point = (180, 830)
        color = (25, 50, 75, 255)
        master.putpixel(upper_point, color)
        master.putpixel(foot_point, color)
        atlas["screen_left_upper_leg"].image.putpixel(upper_point, color)
        atlas["screen_left_lower_leg_foot"].image.putpixel(foot_point, color)

        with self.assertRaisesRegex(hybrid.HybridBlocked, "SUPPORT_FOOT_GROUNDING_INVALID"):
            hybrid._resolve_grounded_chains(
                master, atlas, self._chains(), self._contact_pose()
            )

    def test_grounding_accepts_a_nonzero_uncropped_support_chain_correction(self):
        master, atlas = self._joint_master_and_atlas()
        master.putpixel((400, 667), (25, 50, 75, 255))
        pose = self._contact_pose()
        before = joint.resolve_joint_chains(
            atlas, self._chains(), pose["chain_transforms"], hybrid.TECHNICAL_SIZE
        )

        corrected = hybrid._resolve_grounded_chains(
            master, atlas, self._chains(), pose
        )

        for name in ("screen_left_upper_leg", "screen_left_lower_leg_foot"):
            before_bbox = before.transformed[name].getchannel("A").getbbox()
            after_bbox = corrected.transformed[name].getchannel("A").getbbox()
            self.assertEqual(
                (before_bbox[0], before_bbox[1] + 1, before_bbox[2], before_bbox[3] + 1),
                after_bbox,
            )

    def test_contact_composition_rejects_detached_chain_child(self):
        master, atlas = self._joint_master_and_atlas(detached_child=True)

        with self.assertRaisesRegex(hybrid.HybridBlocked, "NEW_ALPHA_COMPONENTS"):
            hybrid.compose_pose(master, atlas, self._contact_pose(), self._chains())

    def test_contact_composition_rejects_contact_core_translation(self):
        master, atlas = self._joint_master_and_atlas()
        pose = self._contact_pose()
        pose["body_y"] = 1.0

        with self.assertRaisesRegex(hybrid.HybridBlocked, "CORE_TRANSLATION_FORBIDDEN"):
            hybrid.compose_pose(master, atlas, pose, self._chains())


class ContactPoseQATests(HybridTestCase):
    def _normalized_contact_image(self) -> Image.Image:
        image = Image.new("RGBA", hybrid.NORMALIZED_SIZE, (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = (25, 50, 75, 255)
        draw.rectangle((100, 100, 155, 280), fill=color)
        draw.rectangle((75, 270, 99, 397), fill=color)
        draw.rectangle((60, 397, 100, 414), fill=color)
        draw.rectangle((156, 270, 180, 397), fill=color)
        draw.rectangle((155, 397, 195, 414), fill=color)
        draw.rectangle((80, 160, 100, 260), fill=color)
        draw.rectangle((155, 160, 175, 260), fill=color)
        return image

    def _composition(
        self,
        frame_index: int,
        normalized: Image.Image,
        protected_reference: Image.Image | None = None,
        protected_mask: Image.Image | None = None,
    ) -> hybrid.PoseComposition:
        technical = normalized.resize(hybrid.TECHNICAL_SIZE, Image.Resampling.NEAREST)
        reference = (
            protected_reference
            if protected_reference is not None
            else technical.copy()
        )
        mask = protected_mask or Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        if protected_mask is None:
            ImageDraw.Draw(mask).rectangle((240, 240, 270, 300), fill=255)
        return hybrid.PoseComposition(
            frame_index,
            technical,
            Image.new("L", hybrid.TECHNICAL_SIZE, 0),
            mask,
            reference,
            {},
        )

    def contact_pair(
        self,
        foot_separation_ratio: float = 0.18,
        left_hand_ratio: float = 0.10,
        right_hand_ratio: float = 0.10,
        mutation: str | None = None,
    ):
        manifest = self.valid_manifest()
        bbox_width = 136
        left_pose = manifest["poses"]["left_contact"]
        right_pose = manifest["poses"]["right_contact"]
        for pose in (left_pose, right_pose):
            pose["transforms"]["screen_left_lower_leg_foot"]["translation"] = [
                2 * (94 - foot_separation_ratio * bbox_width),
                0.0,
            ]
            pose["transforms"]["screen_right_lower_leg_foot"]["translation"] = [
                0.0,
                0.0,
            ]
        left_pose["transforms"]["screen_left_lower_arm_hand"]["translation"] = [
            0.0,
            0.0,
        ]
        right_pose["transforms"]["screen_left_lower_arm_hand"]["translation"] = [
            -2 * left_hand_ratio * bbox_width,
            0.0,
        ]
        left_pose["transforms"]["screen_right_lower_arm_hand"]["translation"] = [
            0.0,
            0.0,
        ]
        right_pose["transforms"]["screen_right_lower_arm_hand"]["translation"] = [
            2 * right_hand_ratio * bbox_width,
            0.0,
        ]

        normalized = self._normalized_contact_image()
        master = normalized.resize(hybrid.TECHNICAL_SIZE, Image.Resampling.NEAREST)
        left = self._composition(1, normalized)
        right = self._composition(5, normalized)
        if mutation == "root_x_plus_one":
            shifted = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
            shifted.alpha_composite(right.technical, (2, 0))
            shifted_reference = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
            shifted_reference.alpha_composite(right.protected_reference, (2, 0))
            shifted_mask = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
            shifted_mask.paste(right.protected_visible_mask, (2, 0))
            right = self._composition(5, normalized, shifted_reference, shifted_mask)
            right = hybrid.PoseComposition(
                right.frame_index,
                shifted,
                right.flexible_mask,
                shifted_mask,
                shifted_reference,
                right.part_transforms,
            )
        elif mutation == "protected_rgba_plus_one":
            altered = right.technical.copy()
            altered.putpixel((250, 250), (1, 2, 3, 255))
            right = hybrid.PoseComposition(
                right.frame_index,
                altered,
                right.flexible_mask,
                right.protected_visible_mask,
                right.protected_reference,
                right.part_transforms,
            )
        elif mutation == "one_pixel_foot_gap":
            gapped = normalized.copy()
            ImageDraw.Draw(gapped).rectangle((60, 397, 100, 397), fill=(0, 0, 0, 0))
            left = self._composition(1, gapped)
        elif mutation == "front_facing":
            frontal = Image.new("RGBA", hybrid.NORMALIZED_SIZE, (0, 0, 0, 0))
            draw = ImageDraw.Draw(frontal)
            color = (25, 50, 75, 255)
            draw.rectangle((100, 100, 155, 280), fill=color)
            draw.rectangle((95, 270, 120, 397), fill=color)
            draw.rectangle((135, 270, 160, 397), fill=color)
            draw.rectangle((90, 397, 125, 414), fill=color)
            draw.rectangle((130, 397, 165, 414), fill=color)
            left = self._composition(1, frontal)
            right = self._composition(5, frontal)
        return master, left, right, manifest

    def composed_contact_pair(self):
        master, _, _, manifest = self.contact_pair()
        for pose_name in ("left_contact", "right_contact"):
            for transform in manifest["poses"][pose_name]["transforms"].values():
                transform["translation"] = [0.0, 0.0]
                transform["rotation_degrees"] = 0.0
        atlas = hybrid.extract_piece_atlas(master, manifest)
        return (
            master,
            hybrid.compose_pose(master, atlas, manifest["poses"]["left_contact"]),
            hybrid.compose_pose(master, atlas, manifest["poses"]["right_contact"]),
            manifest,
        )

    def test_composed_chroma_is_transparent_before_geometry_and_motion_qa(self):
        master, left, right, manifest = self.composed_contact_pair()

        self.assertEqual((255, 0, 255, 255), left.technical.getpixel((0, 0)))
        report = hybrid.evaluate_contact_pair(master, left, right, manifest)

        self.assertNotEqual(
            [0, 0, 256, 416],
            report["checks"]["geometry"][1]["measurements"]["alpha_bbox"],
        )
        self.assertNotIn("FOOT_DISCONNECTED", report["failures"])

    def test_contact_foot_separation_accepts_18_percent_and_rejects_below(self):
        accepted = self.contact_pair(foot_separation_ratio=0.18)
        rejected = self.contact_pair(foot_separation_ratio=0.179)
        self.assertNotIn(
            "CONTACT_FOOT_SEPARATION",
            hybrid.evaluate_contact_pair(*accepted)["failures"],
        )
        self.assertIn(
            "CONTACT_FOOT_SEPARATION",
            hybrid.evaluate_contact_pair(*rejected)["failures"],
        )

    def test_each_hand_excursion_accepts_10_percent_and_rejects_below(self):
        accepted = self.contact_pair(left_hand_ratio=0.10, right_hand_ratio=0.10)
        rejected = self.contact_pair(left_hand_ratio=0.099, right_hand_ratio=0.10)
        self.assertNotIn("HAND_EXCURSION", hybrid.evaluate_contact_pair(*accepted)["failures"])
        self.assertIn("HAND_EXCURSION", hybrid.evaluate_contact_pair(*rejected)["failures"])

    def test_contact_thresholds_reject_the_next_float_below_each_minimum(self):
        failures = []

        hybrid._append_minimum_failure(
            math.nextafter(0.18, 0.0),
            0.18,
            "CONTACT_FOOT_SEPARATION",
            failures,
        )
        hybrid._append_minimum_failure(
            math.nextafter(0.10, 0.0),
            0.10,
            "HAND_EXCURSION",
            failures,
        )

        self.assertEqual(["CONTACT_FOOT_SEPARATION", "HAND_EXCURSION"], failures)

    def test_root_protected_pixels_and_connected_feet_are_fail_closed(self):
        for mutation, reason in (
            ("root_x_plus_one", "ROOT_HORIZONTAL_DRIFT"),
            ("protected_rgba_plus_one", "PROTECTED_RGBA_MISMATCH"),
            ("one_pixel_foot_gap", "FOOT_DISCONNECTED"),
        ):
            with self.subTest(mutation=mutation):
                report = hybrid.evaluate_contact_pair(*self.contact_pair(mutation=mutation))
                self.assertIn(reason, report["failures"])

    def test_two_frame_pilot_does_not_claim_full_cycle_bob_or_planted_drift(self):
        report = hybrid.evaluate_contact_pair(*self.contact_pair())
        self.assertEqual("NOT_EVALUATED_UNTIL_EIGHT_FRAMES", report["checks"]["movement"]["torso_bob"])
        self.assertEqual("NOT_EVALUATED_UNTIL_EIGHT_FRAMES", report["checks"]["feet"]["planted_foot_drift"])

    def test_unverified_visual_orientation_does_not_fail_machine_qa_or_autoapprove(self):
        master, left, right, manifest = self.contact_pair(mutation="front_facing")
        report = hybrid.evaluate_contact_pair(master, left, right, manifest)

        self.assertEqual("S_3_4", manifest["orientation"])
        self.assertEqual("PASS", report["technical_result"])
        self.assertTrue(hybrid.contact_pair_passed(report))
        self.assertNotIn("ORIENTATION_NOT_VERIFIED", report["failures"])
        self.assertEqual(
            "PENDING_VISUAL_ORIENTATION_REVIEW",
            report["checks"]["orientation"]["status"],
        )
        self.assertEqual(
            ["ORIENTATION_NOT_VERIFIED"],
            report["checks"]["orientation"]["failures"],
        )

    def test_explicit_orientation_mismatch_remains_a_technical_rejection(self):
        master, left, right, manifest = self.contact_pair()
        manifest["orientation"] = "N"

        report = hybrid.evaluate_contact_pair(master, left, right, manifest)

        self.assertIn("ORIENTATION_MISMATCH", report["failures"])
        self.assertFalse(hybrid.contact_pair_passed(report))

    def test_contact_pair_passed_requires_a_passing_technical_report(self):
        accepted = hybrid.evaluate_contact_pair(*self.contact_pair())
        rejected = hybrid.evaluate_contact_pair(
            *self.contact_pair(foot_separation_ratio=0.179)
        )

        self.assertTrue(hybrid.contact_pair_passed(accepted))
        self.assertFalse(hybrid.contact_pair_passed(rejected))

    def test_chain_rendered_landmarks_override_divergent_legacy_transforms(self):
        master, left, right, manifest = self.contact_pair()
        for pose_name in ("left_contact", "right_contact"):
            for transform in manifest["poses"][pose_name]["transforms"].values():
                transform["translation"] = [0.0, 0.0]
                transform["rotation_degrees"] = 0.0
        normalized = {
            1: hybrid._normalized_contact_image(left.technical, (255, 0, 255)),
            5: hybrid._normalized_contact_image(right.technical, (255, 0, 255)),
        }
        legacy = hybrid.measure_contact_landmarks(normalized, manifest)
        rendered = {
            1: {
                "screen_left_lower_leg_foot": (80.0, 400.0),
                "screen_right_lower_leg_foot": (160.0, 400.0),
                "screen_left_lower_arm_hand": (100.0, 200.0),
                "screen_right_lower_arm_hand": (160.0, 200.0),
            },
            5: {
                "screen_left_lower_leg_foot": (80.0, 400.0),
                "screen_right_lower_leg_foot": (160.0, 400.0),
                "screen_left_lower_arm_hand": (130.0, 200.0),
                "screen_right_lower_arm_hand": (130.0, 200.0),
            },
        }
        left = hybrid.PoseComposition(
            left.frame_index,
            left.technical,
            left.flexible_mask,
            left.protected_visible_mask,
            left.protected_reference,
            left.part_transforms,
            joint_evidence={"landmark_centers": rendered[1]},
        )
        right = hybrid.PoseComposition(
            right.frame_index,
            right.technical,
            right.flexible_mask,
            right.protected_visible_mask,
            right.protected_reference,
            right.part_transforms,
            joint_evidence={"landmark_centers": rendered[5]},
        )

        report = hybrid.evaluate_contact_pair(master, left, right, manifest)

        self.assertEqual(0.0, legacy["left_hand_excursion_ratio"])
        self.assertNotIn("HAND_EXCURSION", report["failures"])
        self.assertAlmostEqual(
            30.0 / (2 * 136),
            report["checks"]["movement"]["left_hand_excursion_ratio"],
        )


class HybridWorkspaceAndCliTests(HybridTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.project_root
        self.source = self.source_path
        self.workspace = self.temporary_root / "external-workspace"
        self.workspace.mkdir()
        self.run_id = "20260825-120000-ogre-walk-s-hybrid"
        self.existing_run = self.workspace / self.run_id
        self.linked_workspace = self.project / ".."
        source_image = Image.new("RGBA", hybrid.NORMALIZED_SIZE, (0, 0, 0, 0))
        source_image.putpixel((128, 200), (40, 80, 120, 255))
        source_image.save(self.source)
        self.source_sha256 = hybrid.sha256_file(self.source)

    def manifest(self) -> dict:
        return self.valid_manifest()

    def read_run(self, run_dir: Path) -> dict:
        return json.loads(
            (run_dir / "reports" / "run_manifest.json").read_text(encoding="utf-8")
        )

    def workspace_contacts(self) -> dict[int, hybrid.PoseComposition]:
        contacts = {}
        for frame_index in hybrid.CONTACT_FRAME_INDICES:
            technical = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (255, 0, 255, 255))
            technical.putpixel((250, 300), (25, 50, 75, 255))
            contacts[frame_index] = hybrid.PoseComposition(
                frame_index,
                technical,
                Image.new("L", hybrid.TECHNICAL_SIZE, 0),
                Image.new("L", hybrid.TECHNICAL_SIZE, 0),
                Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0)),
                {},
            )
        return contacts

    def prepare_ready_run(self) -> Path:
        approved_report = {"technical_result": "PASS", "passed": True, "failures": []}
        with patch.object(
            hybrid, "_compose_contact_frames", return_value=self.workspace_contacts()
        ), patch.object(hybrid, "evaluate_contact_pair", return_value=approved_report):
            return hybrid.prepare_pilot(self.manifest(), self.workspace, self.run_id)

    def test_prepare_pilot_writes_only_new_external_atomic_run(self):
        run_dir = self.prepare_ready_run()

        self.assertFalse(hybrid.is_within(run_dir, self.project))
        self.assertEqual("READY_FOR_POSE_REVIEW", self.read_run(run_dir)["state"])
        self.assertEqual({1, 5}, set(self.read_run(run_dir)["pilot_frames"]))
        self.assertEqual(self.source_sha256, hybrid.sha256_file(self.source))

    def test_prepare_uses_source_resolved_before_destination_validation(self):
        manifest = self.manifest()
        replacement = self.project / "source" / "replacement.png"
        Image.new("RGBA", hybrid.NORMALIZED_SIZE, (180, 20, 30, 255)).save(
            replacement
        )
        replacement_sha256 = hybrid.sha256_file(replacement)
        validate_destination = hybrid._validate_new_external_destination

        def mutate_manifest_after_destination_validation(*args, **kwargs):
            result = validate_destination(*args, **kwargs)
            manifest["source_path"] = "source/replacement.png"
            return result

        approved_report = {
            "technical_result": "PASS",
            "passed": True,
            "failures": [],
        }
        with patch.object(
            hybrid,
            "_validate_new_external_destination",
            side_effect=mutate_manifest_after_destination_validation,
        ), patch.object(
            hybrid, "_compose_contact_frames", return_value=self.workspace_contacts()
        ), patch.object(
            hybrid, "evaluate_contact_pair", return_value=approved_report
        ):
            run_dir = hybrid.prepare_pilot(manifest, self.workspace, self.run_id)

        copied_sha256 = hybrid.sha256_file(
            run_dir / "input" / "master" / "source.png"
        )
        self.assertEqual(self.source_sha256, copied_sha256)
        self.assertNotEqual(replacement_sha256, copied_sha256)

    def test_prepare_blocks_project_assets_existing_run_and_linked_ancestor(self):
        for destination in (
            self.project,
            self.project / "Assets",
            self.linked_workspace,
        ):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(hybrid.HybridBlocked, "WORKSPACE_UNSAFE"):
                    hybrid.prepare_pilot(self.manifest(), destination, self.run_id)

        self.existing_run.mkdir()
        with self.assertRaisesRegex(hybrid.HybridBlocked, "WORKSPACE_UNSAFE"):
            hybrid.prepare_pilot(self.manifest(), self.workspace, self.run_id)

    def test_failed_prepare_publishes_no_partial_final_run(self):
        with patch.object(hybrid, "evaluate_contact_pair", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                hybrid.prepare_pilot(self.manifest(), self.workspace, self.run_id)

        self.assertFalse((self.workspace / self.run_id).exists())

    def test_failed_joint_repair_publishes_no_partial_final_run(self):
        transparent = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        resolution = joint.ChainResolution(
            {name: transparent.copy() for name in hybrid.LAYER_NAMES if name != "core"},
            {},
            {},
        )
        with patch.object(
            joint,
            "repair_joint_gaps",
            side_effect=joint.JointReconstructionBlocked("JOINT_REPAIR_CAPABILITY_BLOCKED"),
        ), patch.object(hybrid, "_resolve_grounded_chains", return_value=resolution):
            with self.assertRaisesRegex(hybrid.HybridBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"):
                hybrid.prepare_pilot(self.manifest(), self.workspace, self.run_id)

        self.assertFalse((self.workspace / self.run_id).exists())

    def test_persisted_joint_evidence_has_relative_paths_hashes_and_counts(self):
        technical = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (255, 0, 255, 255))
        technical.putpixel((250, 300), (25, 50, 75, 255))
        repair_mask = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        repair_mask.putpixel((250, 300), 255)
        joint_mask = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        joint_mask.putpixel((250, 300), 255)
        composition = hybrid.PoseComposition(
            1,
            technical,
            Image.new("L", hybrid.TECHNICAL_SIZE, 255),
            Image.new("L", hybrid.TECHNICAL_SIZE, 0),
            Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0)),
            {},
            repair_mask,
            frozenset({(25, 50, 75, 255)}),
            {
                "joint_masks": {"left_shoulder": joint_mask},
                "repaired_pixel_count": 1,
                "used_rgba": frozenset({(25, 50, 75, 255)}),
            },
        )
        contacts = {1: composition, 5: composition}
        approved_report = {"technical_result": "PASS", "passed": True, "failures": []}
        with patch.object(hybrid, "_compose_contact_frames", return_value=contacts), patch.object(
            hybrid, "evaluate_contact_pair", return_value=approved_report
        ):
            run_dir = hybrid.prepare_pilot(self.manifest(), self.workspace, self.run_id)

        qa = json.loads((run_dir / "reports" / "contact_qa.json").read_text(encoding="utf-8"))
        artifacts = json.loads(
            (run_dir / "reports" / "pilot_artifacts.json").read_text(encoding="utf-8")
        )
        for evidence in (qa["joint_evidence"], artifacts["joint_evidence"]):
            frame = evidence["1"]
            mask = frame["joint_masks"]["left_shoulder"]
            self.assertFalse(Path(mask["path"]).is_absolute())
            self.assertEqual(64, len(mask["sha256"]))
            self.assertEqual(1, frame["repaired_pixel_count"])
            self.assertEqual([[25, 50, 75, 255]], frame["used_rgba"])
            self.assertTrue((run_dir / mask["path"]).is_file())

    def test_only_nicolas_can_record_pose_approval(self):
        run_manifest = self.prepare_ready_run() / "reports" / "run_manifest.json"

        with self.assertRaisesRegex(hybrid.HybridBlocked, "VISUAL_REVIEWER_INVALID"):
            hybrid.record_pose_review(run_manifest, "APPROVED", "automation")

        result = hybrid.record_pose_review(run_manifest, "APPROVED", "Nicolas")
        self.assertEqual("POSE_APPROVED", result["state"])

    def test_machine_pass_prepares_for_nicolas_and_only_nicolas_can_reject(self):
        machine_pass_report = {
            "technical_result": "PASS",
            "passed": True,
            "failures": [],
            "checks": {
                "orientation": {
                    "status": "PENDING_VISUAL_ORIENTATION_REVIEW",
                    "failures": ["ORIENTATION_NOT_VERIFIED"],
                },
            },
        }
        with patch.object(
            hybrid, "_compose_contact_frames", return_value=self.workspace_contacts()
        ), patch.object(hybrid, "evaluate_contact_pair", return_value=machine_pass_report):
            run_dir = hybrid.prepare_pilot(self.manifest(), self.workspace, self.run_id)
        run_manifest = run_dir / "reports" / "run_manifest.json"
        qa_report = json.loads(
            (run_dir / "reports" / "contact_qa.json").read_text(encoding="utf-8")
        )

        self.assertEqual("READY_FOR_POSE_REVIEW", self.read_run(run_dir)["state"])
        self.assertEqual("PASS", qa_report["technical_result"])
        self.assertNotIn("ORIENTATION_NOT_VERIFIED", qa_report["failures"])
        self.assertEqual(
            "PENDING_VISUAL_ORIENTATION_REVIEW",
            qa_report["checks"]["orientation"]["status"],
        )
        with self.assertRaisesRegex(hybrid.HybridBlocked, "VISUAL_REVIEWER_INVALID"):
            hybrid.record_pose_review(run_manifest, "REJECTED", "automation")

        result = hybrid.record_pose_review(run_manifest, "REJECTED", "Nicolas")
        self.assertEqual("REJECTED", result["state"])

    def test_cli_help_exposes_only_the_three_offline_commands(self):
        result = subprocess.run(
            [sys.executable, str(Path(hybrid.__file__)), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        for command in ("validate", "prepare-pilot", "record-pose-review"):
            self.assertIn(command, result.stdout)
        self.assertNotIn("run-offline", result.stdout)

    def test_cli_blocks_a_prepare_without_a_support_foot(self):
        manifest_path = self.write_manifest(self.manifest())
        result = subprocess.run(
            [
                sys.executable,
                str(Path(hybrid.__file__)),
                "prepare-pilot",
                "--manifest",
                str(manifest_path),
                "--workspace-root",
                str(self.workspace),
                "--run-id",
                self.run_id,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        payload = json.loads(result.stderr)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual("PART_ATLAS_INVALID", payload["error"])


if __name__ == "__main__":
    unittest.main()
