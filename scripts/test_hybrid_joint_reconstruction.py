import copy
import hashlib
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid_piece_reconstruction as hybrid
import hybrid_joint_reconstruction as joint


class JointContractTests(unittest.TestCase):
    def repair_polygon(self, anchor):
        x, y = anchor
        return [
            [x, y - 24],
            [x + 16, y - 16],
            [x + 24, y],
            [x + 16, y + 16],
            [x, y + 24],
            [x - 16, y + 16],
            [x - 24, y],
            [x - 16, y - 16],
        ]

    def joint_value(self, name, parent_part, child_part, parent_anchor, child_anchor):
        return {
            "name": name,
            "parent_part": parent_part,
            "child_part": child_part,
            "parent_anchor": list(parent_anchor),
            "child_anchor": list(child_anchor),
            "repair_polygon": self.repair_polygon(parent_anchor),
            "max_radius": 24,
        }

    def valid_joint_contract(self):
        return {
            "screen_left_arm": {
                "root_part": "core",
                "joints": [
                    self.joint_value(
                        "left_shoulder",
                        "core",
                        "screen_left_upper_arm",
                        (170, 338),
                        (170, 338),
                    ),
                    self.joint_value(
                        "left_elbow",
                        "screen_left_upper_arm",
                        "screen_left_lower_arm_hand",
                        (124, 466),
                        (124, 466),
                    ),
                ],
            },
            "screen_right_arm": {
                "root_part": "core",
                "joints": [
                    self.joint_value(
                        "right_shoulder",
                        "core",
                        "screen_right_upper_arm",
                        (338, 338),
                        (338, 338),
                    ),
                    self.joint_value(
                        "right_elbow",
                        "screen_right_upper_arm",
                        "screen_right_lower_arm_hand",
                        (384, 466),
                        (384, 466),
                    ),
                ],
            },
            "screen_left_leg": {
                "root_part": "core",
                "joints": [
                    self.joint_value(
                        "left_hip",
                        "core",
                        "screen_left_upper_leg",
                        (200, 518),
                        (200, 518),
                    ),
                    self.joint_value(
                        "left_knee",
                        "screen_left_upper_leg",
                        "screen_left_lower_leg_foot",
                        (178, 666),
                        (178, 666),
                    ),
                ],
            },
            "screen_right_leg": {
                "root_part": "core",
                "joints": [
                    self.joint_value(
                        "right_hip",
                        "core",
                        "screen_right_upper_leg",
                        (308, 518),
                        (308, 518),
                    ),
                    self.joint_value(
                        "right_knee",
                        "screen_right_upper_leg",
                        "screen_right_lower_leg_foot",
                        (330, 666),
                        (330, 666),
                    ),
                ],
            },
        }

    def invalid_contracts(self):
        missing_anchor = self.valid_joint_contract()
        del missing_anchor["screen_left_arm"]["joints"][0]["parent_anchor"]

        duplicate_part = self.valid_joint_contract()
        duplicate_part["screen_right_arm"]["joints"][0]["child_part"] = (
            "screen_left_upper_arm"
        )

        radius_over_24 = self.valid_joint_contract()
        radius_over_24["screen_left_leg"]["joints"][1]["max_radius"] = 25

        nontechnical_anchor = self.valid_joint_contract()
        nontechnical_anchor["screen_right_leg"]["joints"][1]["child_anchor"] = [
            330.0,
            666,
        ]

        core_as_child = self.valid_joint_contract()
        core_as_child["screen_right_leg"]["joints"][1]["child_part"] = "core"

        return (
            missing_anchor,
            duplicate_part,
            radius_over_24,
            nontechnical_anchor,
            core_as_child,
        )

    def valid_chain_transforms(self):
        return {
            "screen_left_arm": {
                "root_translation": [0.0, 0.0],
                "rotation_degrees": {
                    "screen_left_upper_arm": 0.0,
                    "screen_left_lower_arm_hand": 0.0,
                },
            },
            "screen_right_arm": {
                "root_translation": [0.0, 0.0],
                "rotation_degrees": {
                    "screen_right_upper_arm": 0.0,
                    "screen_right_lower_arm_hand": 0.0,
                },
            },
            "screen_left_leg": {
                "root_translation": [0.0, 0.0],
                "rotation_degrees": {
                    "screen_left_upper_leg": 0.0,
                    "screen_left_lower_leg_foot": 0.0,
                },
            },
            "screen_right_leg": {
                "root_translation": [0.0, 0.0],
                "rotation_degrees": {
                    "screen_right_upper_leg": 0.0,
                    "screen_right_lower_leg_foot": 0.0,
                },
            },
        }

    def test_accepts_four_literal_two_link_chains(self):
        chains = joint.validate_joint_contract(
            self.valid_joint_contract(), hybrid.PART_NAMES, hybrid.TECHNICAL_SIZE
        )
        self.assertEqual(
            (
                "screen_left_arm",
                "screen_right_arm",
                "screen_left_leg",
                "screen_right_leg",
            ),
            tuple(chain.name for chain in chains),
        )
        self.assertTrue(all(len(chain.joints) == 2 for chain in chains))
        self.assertEqual(
            (
                "left_shoulder",
                "left_elbow",
                "right_shoulder",
                "right_elbow",
                "left_hip",
                "left_knee",
                "right_hip",
                "right_knee",
            ),
            tuple(joint_definition.name for chain in chains for joint_definition in chain.joints),
        )

    def test_missing_anchor_duplicate_part_or_radius_over_24_blocks(self):
        for broken in self.invalid_contracts():
            with self.subTest(broken=broken):
                with self.assertRaisesRegex(
                    joint.JointReconstructionBlocked, "JOINT_CHAIN_INVALID"
                ):
                    joint.validate_joint_contract(
                        broken, hybrid.PART_NAMES, hybrid.TECHNICAL_SIZE
                    )

    def test_repair_polygon_must_be_centered_bounded_and_non_degenerate(self):
        decentered = self.valid_joint_contract()
        decentered["screen_left_arm"]["joints"][0]["repair_polygon"] = [
            [0, 0],
            [4, 0],
            [0, 4],
        ]

        outside_radius = self.valid_joint_contract()
        outside_radius["screen_left_arm"]["joints"][0]["repair_polygon"][0] = [
            195,
            338,
        ]

        collinear = self.valid_joint_contract()
        collinear["screen_left_arm"]["joints"][0]["repair_polygon"] = [
            [169, 337],
            [170, 338],
            [171, 339],
        ]

        for broken in (decentered, outside_radius, collinear):
            with self.subTest(broken=broken):
                with self.assertRaisesRegex(
                    joint.JointReconstructionBlocked, "JOINT_CHAIN_INVALID"
                ):
                    joint.validate_joint_contract(
                        broken, hybrid.PART_NAMES, hybrid.TECHNICAL_SIZE
                    )

    def test_chain_transforms_require_exact_root_translation_and_rotations(self):
        chains = joint.validate_joint_contract(
            self.valid_joint_contract(), hybrid.PART_NAMES, hybrid.TECHNICAL_SIZE
        )
        joint.require_chain_transforms(self.valid_chain_transforms(), chains)

        broken = copy.deepcopy(self.valid_chain_transforms())
        broken["screen_left_arm"]["translation"] = [1.0, 0.0]
        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_CHAIN_INVALID"
        ):
            joint.require_chain_transforms(broken, chains)

@dataclass(frozen=True)
class SyntheticPart:
    image: Image.Image
    pivot: tuple[int, int]


class JointHierarchyTests(unittest.TestCase):
    """Behavioral tests for deterministic parent-to-child joint placement."""

    size = (64, 64)

    def image_with_pixels(self, *points):
        image = Image.new("RGBA", self.size, (0, 0, 0, 0))
        for point in points:
            image.putpixel(point, (255, 255, 255, 255))
        return image

    def synthetic_atlas(self):
        return {
            "core": SyntheticPart(self.image_with_pixels((20, 20)), (16, 16)),
            "left_upper_arm": SyntheticPart(
                self.image_with_pixels((20, 20), (30, 20)), (20, 20)
            ),
            "left_lower_arm": SyntheticPart(self.image_with_pixels((12, 12)), (12, 12)),
        }

    def chains(self):
        return (
            joint.JointChain(
                name="screen_left_arm",
                root_part="core",
                joints=(
                    joint.JointDefinition(
                        name="left_shoulder",
                        parent_part="core",
                        child_part="left_upper_arm",
                        parent_anchor=(20, 20),
                        child_anchor=(20, 20),
                        repair_polygon=((20, 20), (21, 20), (20, 21)),
                        max_radius=1,
                    ),
                    joint.JointDefinition(
                        name="left_elbow",
                        parent_part="left_upper_arm",
                        child_part="left_lower_arm",
                        parent_anchor=(30, 20),
                        child_anchor=(12, 12),
                        repair_polygon=((30, 20), (31, 20), (30, 21)),
                        max_radius=1,
                    ),
                ),
            ),
        )

    def chain_transforms(self):
        return {
            "screen_left_arm": {
                "root_translation": [2.0, -1.0],
                "rotation_degrees": {
                    "left_upper_arm": 0.0,
                    "left_lower_arm": 0.0,
                },
            }
        }

    def alpha_anchor(self, image):
        points = [
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if image.getpixel((x, y))[3] != 0
        ]
        self.assertEqual(1, len(points))
        return points[0]

    def image_hashes(self, atlas):
        return {
            name: hashlib.sha256(part.image.tobytes()).hexdigest()
            for name, part in atlas.items()
        }

    def test_transform_point_rotates_about_pivot_then_translates(self):
        self.assertEqual(
            (5.0, -1.0),
            joint.transform_point((4, 2), (2, 2), 90.0, (3.0, -1.0)),
        )

    def test_child_anchor_follows_transformed_parent_anchor(self):
        result = joint.resolve_joint_chains(
            self.synthetic_atlas(), self.chains(), self.chain_transforms(), self.size
        )

        self.assertEqual(
            result.target_anchors["left_elbow"],
            self.alpha_anchor(result.transformed["left_lower_arm"]),
        )

    def test_independent_child_translation_is_rejected_by_contract(self):
        broken = self.chain_transforms()
        broken["screen_left_arm"]["child_translation"] = [4, 0]

        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_CHAIN_INVALID"
        ):
            joint.require_chain_transforms(broken, self.chains())

    def test_resolution_is_deterministic_without_mutating_input_images(self):
        atlas = self.synthetic_atlas()
        input_hashes = self.image_hashes(atlas)

        first = joint.resolve_joint_chains(
            atlas, self.chains(), self.chain_transforms(), self.size
        )
        second = joint.resolve_joint_chains(
            atlas, self.chains(), self.chain_transforms(), self.size
        )

        self.assertEqual(first.target_anchors, second.target_anchors)
        self.assertEqual(
            {
                name: hashlib.sha256(image.tobytes()).hexdigest()
                for name, image in first.transformed.items()
            },
            {
                name: hashlib.sha256(image.tobytes()).hexdigest()
                for name, image in second.transformed.items()
            },
        )
        self.assertEqual(input_hashes, self.image_hashes(atlas))

    def test_invalid_resolution_inputs_fail_closed(self):
        cases = []

        nonfinite_translation = self.chain_transforms()
        nonfinite_translation["screen_left_arm"]["root_translation"][0] = float("nan")
        cases.append((self.synthetic_atlas(), self.chains(), nonfinite_translation))

        nonfinite_rotation = self.chain_transforms()
        nonfinite_rotation["screen_left_arm"]["rotation_degrees"][
            "left_upper_arm"
        ] = float("inf")
        cases.append((self.synthetic_atlas(), self.chains(), nonfinite_rotation))

        missing_part = self.synthetic_atlas()
        del missing_part["left_lower_arm"]
        cases.append((missing_part, self.chains(), self.chain_transforms()))

        repeated_write_chain = (
            joint.JointChain(
                name="screen_left_arm",
                root_part="core",
                joints=(
                    self.chains()[0].joints[0],
                    joint.JointDefinition(
                        name="repeat_upper_arm",
                        parent_part="left_upper_arm",
                        child_part="left_upper_arm",
                        parent_anchor=(20, 20),
                        child_anchor=(20, 20),
                        repair_polygon=((20, 20), (21, 20), (20, 21)),
                        max_radius=1,
                    ),
                ),
            ),
        )
        repeated_write_transforms = {
            "screen_left_arm": {
                "root_translation": [0.0, 0.0],
                "rotation_degrees": {"left_upper_arm": 0.0},
            }
        }
        cases.append((self.synthetic_atlas(), repeated_write_chain, repeated_write_transforms))

        outside_canvas = self.chain_transforms()
        outside_canvas["screen_left_arm"]["root_translation"] = [50.0, 0.0]
        cases.append((self.synthetic_atlas(), self.chains(), outside_canvas))

        unresolved_chain = (
            joint.JointChain(
                name="screen_left_arm",
                root_part="core",
                joints=(
                    joint.JointDefinition(
                        name="unresolved_parent",
                        parent_part="left_lower_arm",
                        child_part="left_upper_arm",
                        parent_anchor=(12, 12),
                        child_anchor=(20, 20),
                        repair_polygon=((12, 12), (13, 12), (12, 13)),
                        max_radius=1,
                    ),
                ),
            ),
        )
        unresolved_transforms = {
            "screen_left_arm": {
                "root_translation": [0.0, 0.0],
                "rotation_degrees": {
                    "left_lower_arm": 0.0,
                    "left_upper_arm": 0.0,
                },
            }
        }
        cases.append((self.synthetic_atlas(), unresolved_chain, unresolved_transforms))

        for atlas, chains, transforms in cases:
            with self.subTest(chains=chains, transforms=transforms):
                with self.assertRaisesRegex(
                    joint.JointReconstructionBlocked, "JOINT_CHAIN_INVALID"
                ):
                    joint.resolve_joint_chains(atlas, chains, transforms, self.size)


class JointMaskTests(unittest.TestCase):
    """Confinement and palette behavior for Task 3 repair preparation."""

    size = (32, 32)

    def mask(self, *points):
        result = Image.new("L", self.size, 0)
        for point in points:
            result.putpixel(point, 255)
        return result

    def image(self, *points):
        result = Image.new("RGBA", self.size, (0, 0, 0, 0))
        for point in points:
            result.putpixel(point, (180, 90, 45, 255))
        return result

    def joint_polygon(self):
        return ((10, 12), (21, 12), (21, 20), (10, 20))

    def opaque_points(self, mask):
        return {
            (x, y)
            for y in range(mask.height)
            for x in range(mask.width)
            if mask.getpixel((x, y)) != 0
        }

    def test_repair_polygon_follows_target_anchor_with_half_away_rounding(self):
        translated = joint.translate_repair_polygon(
            ((8, 6), (10, 8), (8, 10), (6, 8)),
            (8, 8),
            (10.5, 4.5),
            4,
            (32, 32),
        )
        self.assertEqual(((11, 3), (13, 5), (11, 7), (9, 5)), translated)

    def test_translated_polygon_blocks_out_of_canvas_or_radius_growth(self):
        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked,
            "JOINT_REPAIR_CAPABILITY_BLOCKED",
        ):
            joint.translate_repair_polygon(
                ((0, 0), (4, 0), (4, 4), (0, 4)),
                (2, 2),
                (0, 0),
                4,
                (8, 8),
            )

    def test_effective_mask_subtracts_protected_and_equipment_pixels(self):
        protected = self.mask((14, 16))
        equipment = self.mask((17, 16))

        effective = joint.build_effective_joint_mask(
            self.size, self.joint_polygon(), protected, equipment
        )

        self.assertEqual("L", effective.mode)
        self.assertEqual(0, joint.overlap_count(effective, protected))
        self.assertEqual(0, joint.overlap_count(effective, equipment))
        self.assertEqual(0, effective.getpixel((14, 16)))
        self.assertEqual(0, effective.getpixel((17, 16)))
        self.assertNotEqual(0, effective.getpixel((15, 16)))

    def test_gap_detector_returns_only_one_empty_corridor_pixel(self):
        parent = self.image((14, 16))
        child = self.image((16, 16))
        effective = joint.build_effective_joint_mask(
            self.size, self.joint_polygon(), self.mask(), self.mask()
        )

        gap = joint.detect_joint_gap(parent, child, effective)

        self.assertEqual({(15, 16)}, self.opaque_points(gap))
        self.assertEqual(0, joint.overlap_count(gap, parent.getchannel("A")))
        self.assertEqual(0, joint.overlap_count(gap, child.getchannel("A")))
        self.assertEqual(0, joint.repair_out_of_bounds_count(gap, effective))

    def test_gap_detector_uses_the_local_boundary_pair_not_global_silhouettes(self):
        parent = self.image((14, 16), (0, 0))
        child = self.image((16, 16), (0, 31))
        effective = self.mask((15, 16))

        gap = joint.detect_joint_gap(parent, child, effective)

        self.assertEqual({(15, 16)}, self.opaque_points(gap))
        self.assertEqual(0, joint.overlap_count(gap, parent.getchannel("A")))
        self.assertEqual(0, joint.overlap_count(gap, child.getchannel("A")))
        self.assertEqual(0, joint.repair_out_of_bounds_count(gap, effective))

    def test_gap_beyond_radius_remains_confined_to_declared_effective_mask(self):
        parent = self.image((3, 16))
        child = self.image((29, 16))
        declared = self.mask(*((x, 16) for x in range(14, 19)))

        gap = joint.detect_joint_gap(parent, child, declared)

        self.assertEqual(set((x, 16) for x in range(14, 19)), self.opaque_points(gap))
        self.assertEqual(0, joint.repair_out_of_bounds_count(gap, declared))

    def test_face_protected_overlap_and_fully_blocked_mask_never_expand(self):
        protected = self.mask(*((x, y) for y in range(12, 21) for x in range(10, 22)))
        effective = joint.build_effective_joint_mask(
            self.size, self.joint_polygon(), protected, self.mask()
        )

        self.assertIsNone(effective.getbbox())
        self.assertEqual(0, joint.overlap_count(effective, protected))
        self.assertIsNone(
            joint.detect_joint_gap(
                self.image((14, 16)), self.image((16, 16)), effective
            ).getbbox()
        )

    def test_master_palette_returns_exact_nontransparent_rgba_tuples(self):
        master = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        master.putpixel((0, 0), (1, 2, 3, 255))
        master.putpixel((1, 0), (4, 5, 6, 128))
        master.putpixel((2, 0), (1, 2, 3, 255))

        self.assertEqual(
            frozenset({(1, 2, 3, 255), (4, 5, 6, 128)}),
            joint.master_rgba_palette(master),
        )

    def test_master_palette_rejects_invalid_mode_or_size(self):
        cases = (
            Image.new("RGB", hybrid.TECHNICAL_SIZE),
            Image.new("RGBA", (hybrid.TECHNICAL_SIZE[0] - 1, hybrid.TECHNICAL_SIZE[1])),
        )

        for master in cases:
            with self.subTest(mode=master.mode, size=master.size):
                with self.assertRaisesRegex(
                    joint.JointReconstructionBlocked, "JOINT_PALETTE_INVALID"
                ):
                    joint.master_rgba_palette(master)

    def test_repair_out_of_bounds_and_overlap_count_use_binary_ownership(self):
        repair = self.mask((10, 12), (11, 12), (12, 12))
        effective = self.mask((10, 12), (11, 12))
        other = self.mask((11, 12), (12, 12), (13, 12))

        self.assertEqual(1, joint.repair_out_of_bounds_count(repair, effective))
        self.assertEqual(2, joint.overlap_count(repair, other))


class OccludedJointTests(unittest.TestCase):
    def rgba_points(self, points):
        image = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        for point in points:
            image.putpixel(point, (20, 40, 60, 255))
        return image

    def mask_points(self, points):
        image = Image.new("L", (16, 16), 0)
        for point in points:
            image.putpixel(point, 255)
        return image

    def technical_rgba_points(self, points):
        image = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        for point in points:
            image.putpixel(point, (20, 40, 60, 255))
        return image

    def technical_mask_points(self, points):
        image = Image.new("L", hybrid.TECHNICAL_SIZE, 0)
        for point in points:
            image.putpixel(point, 255)
        return image

    def definition(self):
        return joint.JointDefinition(
            "left_hip", "parent", "child", (8, 8), (8, 8),
            ((8, 4), (12, 8), (8, 12), (4, 8)), 4,
        )

    def bridge_case(self, case):
        y = 5 if case == "anchor_missing" else 8
        occluder_points = [(x, y) for x in range(6, 11)]
        if case == "disconnected":
            occluder_points.remove((8, 8))
        transformed = {
            "parent": self.rgba_points([(4, y), (5, y)]),
            "child": self.rgba_points([(11, y), (12, y)]),
            "occluder": self.rgba_points(occluder_points),
        }
        order = (
            ("occluder", "parent", "child")
            if case == "behind"
            else ("parent", "child", "occluder")
        )
        visible = (
            frozenset({"parent", "child"})
            if case == "invisible"
            else frozenset(transformed)
        )
        return transformed, order, visible

    def evaluate_case(self, case):
        transformed, order, visible = self.bridge_case(case)
        return joint.occluded_joint_bridge_exists(
            transformed, order, visible, self.definition(),
            (8.0, 8.0), ("occluder",),
        )

    def repair_inputs(self, transformed, definition=None, joint_mask=None):
        definition = definition or self.definition()
        chain = joint.JointChain("hip_chain", definition.parent_part, (definition,))
        technical_transformed = {}
        for name, image in transformed.items():
            technical = self.technical_rgba_points([])
            technical.paste(image, (0, 0))
            technical_transformed[name] = technical
        technical_joint_mask = self.technical_mask_points([])
        if joint_mask is None:
            for x in range(6, 11):
                technical_joint_mask.putpixel((x, 8), 255)
        else:
            technical_joint_mask.paste(joint_mask, (0, 0))
        resolution = joint.ChainResolution(
            transformed=technical_transformed,
            target_anchors={definition.name: (8.0, 8.0)},
            joint_masks={definition.name: technical_joint_mask},
        )
        master = self.technical_rgba_points([(0, 0)])
        empty = self.technical_mask_points([])
        return master, resolution, (chain,), empty

    def test_front_occluder_connecting_parent_anchor_and_child_is_accepted(self):
        transformed, order, visible = self.bridge_case("connected")
        self.assertTrue(joint.occluded_joint_bridge_exists(
            transformed, order, visible, self.definition(),
            (8.0, 8.0), ("occluder",),
        ))

    def test_hidden_behind_disconnected_or_anchor_missing_occluder_is_rejected(self):
        for case in ("behind", "disconnected", "anchor_missing", "invisible"):
            with self.subTest(case=case):
                self.assertFalse(self.evaluate_case(case))

    def test_unrelated_part_cannot_complete_declared_bridge(self):
        transformed = {
            "parent": self.rgba_points([(4, 8), (5, 8)]),
            "child": self.rgba_points([(11, 8), (12, 8)]),
            "occluder": self.rgba_points([(8, 8)]),
            "unrelated": self.rgba_points([(6, 8), (7, 8), (9, 8), (10, 8)]),
        }
        self.assertFalse(joint.occluded_joint_bridge_exists(
            transformed,
            ("parent", "child", "unrelated", "occluder"),
            frozenset(transformed),
            self.definition(),
            (8.0, 8.0),
            ("occluder",),
        ))

    def test_true_declared_bridge_records_evidence_without_writing_pixels(self):
        transformed, order, visible = self.bridge_case("connected")
        master, resolution, chains, empty = self.repair_inputs(transformed)
        baseline = Image.new("RGBA", hybrid.TECHNICAL_SIZE, (0, 0, 0, 0))
        for part_name in order:
            baseline.alpha_composite(resolution.transformed[part_name])

        repaired = joint.repair_joint_gaps(
            master,
            resolution,
            chains,
            order,
            empty,
            empty,
            joint_occluders={"left_hip": ("occluder",)},
            visible_parts=visible,
        )

        self.assertEqual("OCCLUDED_BRIDGE", repaired.joint_statuses["left_hip"])
        self.assertEqual(0, repaired.joint_repair_pixel_counts["left_hip"])
        self.assertEqual(0, repaired.repaired_pixel_count)
        self.assertIsNone(repaired.repair_mask.getbbox())
        self.assertEqual(baseline.tobytes(), repaired.image.tobytes())

    def test_same_bridge_without_declaration_receives_no_exemption(self):
        transformed, order, _ = self.bridge_case("connected")
        master, resolution, chains, empty = self.repair_inputs(transformed)

        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
        ):
            joint.repair_joint_gaps(
                master, resolution, chains, order, empty, empty
            )

    def test_false_bridge_falls_through_to_ordinary_repair(self):
        transformed = {
            "occluder": self.rgba_points([(8, 8)]),
            "parent": self.rgba_points([(7, 8)]),
            "child": self.rgba_points([(9, 8)]),
        }
        order = ("occluder", "parent", "child")
        master, resolution, chains, empty = self.repair_inputs(
            transformed, joint_mask=self.mask_points([(8, 8)])
        )

        repaired = joint.repair_joint_gaps(
            master,
            resolution,
            chains,
            order,
            empty,
            empty,
            joint_occluders={"left_hip": ("occluder",)},
            visible_parts=frozenset(transformed),
        )

        self.assertEqual("REPAIRED", repaired.joint_statuses["left_hip"])
        self.assertEqual(1, repaired.joint_repair_pixel_counts["left_hip"])
        self.assertEqual(1, repaired.repaired_pixel_count)
        self.assertEqual(255, repaired.repair_mask.getpixel((8, 8)))

    def test_false_bridge_without_local_boundaries_retains_capability_block(self):
        transformed = {
            "occluder": self.rgba_points([(8, 8)]),
            "parent": self.rgba_points([]),
            "child": self.rgba_points([(9, 8)]),
        }
        order = ("occluder", "parent", "child")
        master, resolution, chains, empty = self.repair_inputs(
            transformed, joint_mask=self.mask_points([(8, 8)])
        )

        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
        ):
            joint.repair_joint_gaps(
                master,
                resolution,
                chains,
                order,
                empty,
                empty,
                joint_occluders={"left_hip": ("occluder",)},
                visible_parts=frozenset(transformed),
            )

    def test_ordinary_success_records_repaired_and_no_gap_exact_counts(self):
        repaired_definition = joint.JointDefinition(
            "repair_joint", "repair_parent", "repair_child", (5, 8), (5, 8),
            ((4, 7), (6, 7), (6, 9), (4, 9)), 4,
        )
        no_gap_definition = joint.JointDefinition(
            "no_gap_joint", "no_gap_parent", "no_gap_child", (11, 8), (11, 8),
            ((10, 7), (12, 7), (12, 9), (10, 9)), 4,
        )
        transformed = {
            "repair_parent": self.technical_rgba_points([(4, 8)]),
            "repair_child": self.technical_rgba_points([(6, 8)]),
            "no_gap_parent": self.technical_rgba_points([(10, 8)]),
            "no_gap_child": self.technical_rgba_points([(11, 8)]),
        }
        resolution = joint.ChainResolution(
            transformed=transformed,
            target_anchors={},
            joint_masks={
                "repair_joint": self.technical_mask_points([(5, 8)]),
                "no_gap_joint": self.technical_mask_points([(11, 8)]),
            },
        )
        chains = (
            joint.JointChain("repair_chain", "repair_parent", (repaired_definition,)),
            joint.JointChain("no_gap_chain", "no_gap_parent", (no_gap_definition,)),
        )
        empty = self.technical_mask_points([])

        repaired = joint.repair_joint_gaps(
            self.technical_rgba_points([(0, 0)]),
            resolution,
            chains,
            tuple(transformed),
            empty,
            empty,
        )

        self.assertEqual(
            {"repair_joint": "REPAIRED", "no_gap_joint": "NO_GAP"},
            repaired.joint_statuses,
        )
        self.assertEqual(
            {"repair_joint": 1, "no_gap_joint": 0},
            repaired.joint_repair_pixel_counts,
        )
        self.assertEqual(1, repaired.repaired_pixel_count)


class JointRepairTests(unittest.TestCase):
    """Task 4 deterministic, palette-bound, depth-aware repair behavior."""

    size = hybrid.TECHNICAL_SIZE
    parent_rgba = (20, 40, 60, 255)
    child_rgba = (80, 100, 120, 255)
    core_rgba = (140, 160, 180, 255)

    def image(self, pixels=()):
        result = Image.new("RGBA", self.size, (0, 0, 0, 0))
        for point, rgba in pixels:
            result.putpixel(point, rgba)
        return result

    def mask(self, points=()):
        result = Image.new("L", self.size, 0)
        for point in points:
            result.putpixel(point, 255)
        return result

    def master(self, *rgba_values):
        result = Image.new("RGBA", self.size, (0, 0, 0, 0))
        for index, rgba in enumerate(rgba_values):
            result.putpixel((index, 0), rgba)
        return result

    def chain(self, name, parent_part, child_part, joint_name, max_radius=24):
        return joint.JointChain(
            name=name,
            root_part=parent_part,
            joints=(
                joint.JointDefinition(
                    name=joint_name,
                    parent_part=parent_part,
                    child_part=child_part,
                    parent_anchor=(100, 100),
                    child_anchor=(102, 100),
                    repair_polygon=((99, 99), (103, 99), (103, 101)),
                    max_radius=max_radius,
                ),
            ),
        )

    def one_pixel_gap_inputs(self):
        chain = self.chain("rear_chain", "rear_parent", "rear_child", "rear_joint")
        resolution = joint.ChainResolution(
            transformed={
                "rear_parent": self.image([((100, 100), self.parent_rgba)]),
                "rear_child": self.image([((102, 100), self.child_rgba)]),
            },
            target_anchors={},
            joint_masks={"rear_joint": self.mask([(101, 100)])},
        )
        return chain, resolution

    def test_repair_connects_one_pixel_gap_using_only_master_rgba(self):
        chain, resolution = self.one_pixel_gap_inputs()
        repaired = joint.repair_joint_gaps(
            self.master(self.parent_rgba, self.child_rgba),
            resolution,
            (chain,),
            ("rear_parent", "rear_child"),
            self.mask(),
            self.mask(),
        )

        self.assertEqual(1, joint.component_count(repaired.image))
        self.assertEqual(
            self.parent_rgba,
            repaired.image.getpixel((101, 100)),
            "rear-chain donor ties must prefer the parent side",
        )
        self.assertEqual(1, repaired.repaired_pixel_count)
        self.assertEqual(frozenset({self.parent_rgba}), repaired.used_rgba)
        self.assertEqual(0, joint.joint_palette_mismatch_count(
            repaired, joint.master_rgba_palette(self.master(self.parent_rgba, self.child_rgba))
        ))
        self.assertEqual(0, joint.repair_out_of_bounds_count(
            repaired.repair_mask, resolution.joint_masks["rear_joint"]
        ))

    def test_too_wide_gap_blocks_without_expanding_mask(self):
        chain = self.chain(
            "rear_chain", "rear_parent", "rear_child", "rear_joint", max_radius=2
        )
        resolution = joint.ChainResolution(
            transformed={
                "rear_parent": self.image([((100, 100), self.parent_rgba)]),
                "rear_child": self.image([((106, 100), self.child_rgba)]),
            },
            target_anchors={},
            joint_masks={"rear_joint": self.mask([(x, 100) for x in range(101, 106)])},
        )

        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
        ):
            joint.repair_joint_gaps(
                self.master(self.parent_rgba, self.child_rgba),
                resolution,
                (chain,),
                ("rear_parent", "rear_child"),
                self.mask(),
                self.mask(),
            )

    def test_repair_uses_complete_palette_donor_tuple_without_synthesis(self):
        palette_donor = (17, 34, 51, 128)
        chain, resolution = self.one_pixel_gap_inputs()
        resolution = joint.ChainResolution(
            transformed={
                "rear_parent": self.image([((100, 100), palette_donor)]),
                "rear_child": resolution.transformed["rear_child"],
            },
            target_anchors=resolution.target_anchors,
            joint_masks=resolution.joint_masks,
        )
        repaired = joint.repair_joint_gaps(
            self.master(palette_donor, self.child_rgba),
            resolution,
            (chain,),
            ("rear_parent", "rear_child"),
            self.mask(),
            self.mask(),
        )

        self.assertEqual(palette_donor, repaired.image.getpixel((101, 100)))
        self.assertEqual(frozenset({palette_donor}), repaired.used_rgba)
        self.assertEqual(0, joint.joint_palette_mismatch_count(
            repaired, joint.master_rgba_palette(self.master(palette_donor, self.child_rgba))
        ))

    def test_protected_and_forbidden_fully_blocked_corridor_fails_closed(self):
        chain, resolution = self.one_pixel_gap_inputs()
        protected = self.mask([(101, 100)])
        forbidden = self.mask([(101, 100)])
        protected_source = self.image([((101, 100), self.core_rgba)])
        resolution = joint.ChainResolution(
            transformed={**resolution.transformed, "protected_source": protected_source},
            target_anchors=resolution.target_anchors,
            joint_masks=resolution.joint_masks,
        )
        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
        ):
            joint.repair_joint_gaps(
                self.master(self.parent_rgba, self.child_rgba, self.core_rgba),
                resolution,
                (chain,),
                ("rear_parent", "rear_child", "protected_source"),
                protected,
                forbidden,
            )

    def test_rear_repairs_stay_behind_core_and_front_repairs_remain_visible(self):
        rear_chain = self.chain("rear_chain", "rear_parent", "rear_child", "rear_joint")
        front_chain = joint.JointChain(
            name="front_chain",
            root_part="front_parent",
            joints=(
                joint.JointDefinition(
                    name="front_joint",
                    parent_part="front_parent",
                    child_part="front_child",
                    parent_anchor=(120, 100),
                    child_anchor=(122, 100),
                    repair_polygon=((119, 99), (123, 99), (123, 101)),
                    max_radius=24,
                ),
            ),
        )
        resolution = joint.ChainResolution(
            transformed={
                "rear_parent": self.image([((100, 100), self.parent_rgba)]),
                "rear_child": self.image([((102, 100), self.child_rgba)]),
                "core": self.image([((101, 100), self.core_rgba)]),
                "front_parent": self.image([((120, 100), self.parent_rgba)]),
                "front_child": self.image([((122, 100), self.child_rgba)]),
            },
            target_anchors={},
            joint_masks={
                "rear_joint": self.mask([(101, 100)]),
                "front_joint": self.mask([(121, 100)]),
            },
        )
        repaired = joint.repair_joint_gaps(
            self.master(self.parent_rgba, self.child_rgba, self.core_rgba),
            resolution,
            (rear_chain, front_chain),
            ("rear_parent", "rear_child", "core", "front_parent", "front_child"),
            self.mask(),
            self.mask(),
        )

        self.assertEqual(self.core_rgba, repaired.image.getpixel((101, 100)))
        self.assertEqual(self.child_rgba, repaired.image.getpixel((121, 100)))
        self.assertEqual(2, repaired.repaired_pixel_count)

    def test_core_root_chain_uses_noncore_members_for_manifest_like_rear_depth(self):
        chain = joint.JointChain(
            name="screen_left_arm",
            root_part="core",
            joints=(
                joint.JointDefinition(
                    name="left_shoulder",
                    parent_part="core",
                    child_part="screen_left_upper_arm",
                    parent_anchor=(100, 100),
                    child_anchor=(102, 100),
                    repair_polygon=((99, 99), (103, 99), (103, 101)),
                    max_radius=24,
                ),
                joint.JointDefinition(
                    name="left_elbow",
                    parent_part="screen_left_upper_arm",
                    child_part="screen_left_lower_arm_hand",
                    parent_anchor=(104, 100),
                    child_anchor=(106, 100),
                    repair_polygon=((103, 99), (107, 99), (107, 101)),
                    max_radius=24,
                ),
            ),
        )
        resolution = joint.ChainResolution(
            transformed={
                "screen_left_upper_arm": self.image(
                    [
                        ((102, 100), self.child_rgba),
                        ((103, 100), self.child_rgba),
                        ((104, 100), self.child_rgba),
                    ]
                ),
                "screen_left_lower_arm_hand": self.image(
                    [((106, 100), self.child_rgba)]
                ),
                "core": self.image([((100, 100), self.parent_rgba)]),
                "loincloth_front": self.image(),
            },
            target_anchors={},
            joint_masks={
                "left_shoulder": self.mask([(101, 100)]),
                "left_elbow": self.mask([(105, 100)]),
            },
        )

        repaired = joint.repair_joint_gaps(
            self.master(self.parent_rgba, self.child_rgba),
            resolution,
            (chain,),
            (
                "screen_left_upper_arm",
                "screen_left_lower_arm_hand",
                "core",
                "loincloth_front",
            ),
            self.mask(),
            self.mask(),
        )

        self.assertEqual(2, repaired.repaired_pixel_count)
        self.assertEqual(1, joint.component_count(repaired.image))

    def test_effective_corridor_with_only_one_legal_boundary_blocks(self):
        chain, resolution = self.one_pixel_gap_inputs()
        resolution = joint.ChainResolution(
            transformed={
                "rear_parent": self.image(),
                "rear_child": resolution.transformed["rear_child"],
            },
            target_anchors=resolution.target_anchors,
            joint_masks=resolution.joint_masks,
        )

        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
        ):
            joint.repair_joint_gaps(
                self.master(self.child_rgba),
                resolution,
                (chain,),
                ("rear_parent", "rear_child"),
                self.mask(),
                self.mask(),
            )

    def test_effective_corridor_with_no_legal_boundaries_blocks(self):
        chain, resolution = self.one_pixel_gap_inputs()
        resolution = joint.ChainResolution(
            transformed={
                "rear_parent": self.image(),
                "rear_child": self.image(),
            },
            target_anchors=resolution.target_anchors,
            joint_masks=resolution.joint_masks,
        )

        with self.assertRaisesRegex(
            joint.JointReconstructionBlocked, "JOINT_REPAIR_CAPABILITY_BLOCKED"
        ):
            joint.repair_joint_gaps(
                self.master(),
                resolution,
                (chain,),
                ("rear_parent", "rear_child"),
                self.mask(),
                self.mask(),
            )

    def test_repair_is_repeatable_without_mutating_resolution_images_or_masks(self):
        chain, resolution = self.one_pixel_gap_inputs()
        before_images = {
            name: hashlib.sha256(image.tobytes()).hexdigest()
            for name, image in resolution.transformed.items()
        }
        before_masks = {
            name: hashlib.sha256(mask.tobytes()).hexdigest()
            for name, mask in resolution.joint_masks.items()
        }
        master = self.master(self.parent_rgba, self.child_rgba)
        arguments = (
            master,
            resolution,
            (chain,),
            ("rear_parent", "rear_child"),
            self.mask(),
            self.mask(),
        )

        first = joint.repair_joint_gaps(*arguments)
        second = joint.repair_joint_gaps(*arguments)

        self.assertEqual(
            hashlib.sha256(first.image.tobytes()).hexdigest(),
            hashlib.sha256(second.image.tobytes()).hexdigest(),
        )
        self.assertEqual(
            hashlib.sha256(first.repair_mask.tobytes()).hexdigest(),
            hashlib.sha256(second.repair_mask.tobytes()).hexdigest(),
        )
        self.assertEqual(first.repaired_pixel_count, second.repaired_pixel_count)
        self.assertEqual(first.used_rgba, second.used_rgba)
        self.assertEqual(
            before_images,
            {
                name: hashlib.sha256(image.tobytes()).hexdigest()
                for name, image in resolution.transformed.items()
            },
        )
        self.assertEqual(
            before_masks,
            {
                name: hashlib.sha256(mask.tobytes()).hexdigest()
                for name, mask in resolution.joint_masks.items()
            },
        )


if __name__ == "__main__":
    unittest.main()
