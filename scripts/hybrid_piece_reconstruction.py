import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops

import hybrid_joint_reconstruction as joint
from sprite_pipeline import polygon_mask, translate, overlay_exact
from sequential_reconstruction import (
    component_checks,
    foot_checks,
    geometry_checks,
    recover_alpha,
)


NORMALIZED_SIZE = (256, 416)
TECHNICAL_SIZE = (512, 832)
SKILL_ROOT = Path(__file__).resolve().parents[1]
_CODEX_HOME_TOKEN = "${CODEX_HOME}"
CONTACT_FRAME_INDICES = (1, 5)
POSE_NAMES = (
    "left_contact",
    "left_down",
    "left_pass",
    "left_up",
    "right_contact",
    "right_down",
    "right_pass",
    "right_up",
)
PART_NAMES = (
    "loincloth_front",
    "screen_left_upper_arm",
    "screen_left_lower_arm_hand",
    "screen_right_upper_arm",
    "screen_right_lower_arm_hand",
    "screen_left_upper_leg",
    "screen_left_lower_leg_foot",
    "screen_right_upper_leg",
    "screen_right_lower_leg_foot",
)
LAYER_NAMES = (*PART_NAMES, "core")
OWNERSHIP_ORDER = (
    "loincloth_front",
    "screen_left_lower_arm_hand",
    "screen_right_lower_arm_hand",
    "screen_left_upper_arm",
    "screen_right_upper_arm",
    "screen_left_lower_leg_foot",
    "screen_right_lower_leg_foot",
    "screen_left_upper_leg",
    "screen_right_upper_leg",
)
CORE_PIVOT = (254, 354)
PART_KINDS = {
    "loincloth_front": "flexible",
    **{name: "articulated" for name in PART_NAMES if name != "loincloth_front"},
}

_TOP_LEVEL_KEYS = frozenset(
    {
        "mode",
        "project_root",
        "source_path",
        "entity",
        "action",
        "direction",
        "orientation",
        "frame_count",
        "frame_width",
        "frame_height",
        "technical_scale",
        "anchor",
        "root_motion",
        "pilot_contact_frames",
        "ownership_order",
        "core_pivot",
        "parts",
        "joint_chains",
        "poses",
        "qa",
    }
)
_OWNERSHIP_MAP_KEYS = frozenset({"path", "sha256", "labels"})
_POLYGON_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS
_EXPLICIT_TOP_LEVEL_KEYS = frozenset(
    (_TOP_LEVEL_KEYS - {"ownership_order"}) | {"ownership_map", "joint_occluders"}
)
_TOP_LEVEL_LITERALS = {
    "mode": "hybrid_piece_reconstruction",
    "entity": "ogre",
    "action": "walk",
    "direction": "S",
    "orientation": "S_3_4",
    "frame_count": 8,
    "frame_width": NORMALIZED_SIZE[0],
    "frame_height": NORMALIZED_SIZE[1],
    "technical_scale": 2,
    "anchor": "bottom-center",
    "root_motion": False,
    "pilot_contact_frames": list(CONTACT_FRAME_INDICES),
}
_PART_KEYS = frozenset({"polygon", "pivot", "kind"})
_POSE_BASE_KEYS = frozenset(
    {
        "frame_index",
        "name",
        "support_foot",
        "phase",
        "stride",
        "counter_swing",
        "body_y",
    }
)
_POSE_CONTACT_KEYS = frozenset(
    {
        "transforms",
        "layer_order",
        "visibility",
        "flexible_polygons",
        "chroma_rgb",
        "chain_transforms",
    }
)
_TRANSFORM_KEYS = frozenset({"translation", "rotation_degrees"})
_QA_KEYS = frozenset(
    {
        "contact_foot_separation_bbox_ratio_min",
        "hand_excursion_bbox_ratio_min",
        "protected_rgba_mismatch_max",
    }
)


class HybridBlocked(RuntimeError):
    pass


def _blocked() -> None:
    raise HybridBlocked("HYBRID_MANIFEST_INVALID")


def _blocked_part_atlas() -> None:
    raise HybridBlocked("PART_ATLAS_INVALID")


@dataclass(frozen=True)
class Part:
    name: str
    image: Image.Image
    ownership_mask: Image.Image
    pivot: tuple[int, int]
    kind: str


@dataclass(frozen=True)
class PoseComposition:
    frame_index: int
    technical: Image.Image
    flexible_mask: Image.Image
    protected_visible_mask: Image.Image
    protected_reference: Image.Image
    part_transforms: dict
    joint_repair_mask: Image.Image = field(
        default_factory=lambda: Image.new("L", TECHNICAL_SIZE, 0)
    )
    joint_repair_rgba: frozenset[joint.RGBA] = frozenset()
    joint_evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExplicitOwnershipContract:
    path: Path
    sha256: str
    labels: dict[str, int]
    joint_occluders: dict[str, tuple[str, ...]]


def _read_json_object(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    if not isinstance(value, dict):
        _blocked()
    return value


def _require_exact_keys(value: dict, expected: frozenset[str], context: str) -> None:
    if not isinstance(value, dict) or frozenset(value) != expected:
        _blocked()


def _blocked_ownership_map() -> None:
    raise HybridBlocked("PART_OWNERSHIP_MAP_INVALID")


def _require_confined_skill_asset(path_value) -> Path:
    if not isinstance(path_value, str):
        _blocked_ownership_map()
    try:
        relative_path = Path(path_value)
        if relative_path.is_absolute():
            _blocked_ownership_map()
        assets_root = (SKILL_ROOT / "assets").resolve(strict=True)
        path = (SKILL_ROOT / relative_path).resolve(strict=True)
        path.relative_to(assets_root)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise HybridBlocked("PART_OWNERSHIP_MAP_INVALID") from error
    if not path.is_file():
        _blocked_ownership_map()
    return path


def _explicit_ownership_contract(manifest: dict) -> ExplicitOwnershipContract | None:
    keys = frozenset(manifest)
    if keys == _POLYGON_TOP_LEVEL_KEYS:
        return None
    if keys != _EXPLICIT_TOP_LEVEL_KEYS:
        _blocked()
    ownership = manifest["ownership_map"]
    if not isinstance(ownership, dict) or frozenset(ownership) != _OWNERSHIP_MAP_KEYS:
        _blocked_ownership_map()
    labels = ownership["labels"]
    if (
        not isinstance(labels, dict)
        or frozenset(labels) != frozenset(LAYER_NAMES)
        or any(not _is_json_integer(value) or not 1 <= value <= 255 for value in labels.values())
        or len(set(labels.values())) != len(LAYER_NAMES)
    ):
        _blocked_ownership_map()
    occluders = manifest["joint_occluders"]
    expected = {"left_hip": ["loincloth_front"], "right_hip": ["loincloth_front"]}
    if occluders != expected:
        _blocked_ownership_map()
    path = _require_confined_skill_asset(ownership["path"])
    declared_hash = ownership["sha256"]
    if not isinstance(declared_hash, str) or len(declared_hash) != 64:
        _blocked_ownership_map()
    try:
        actual_hash = sha256_file(path)
    except HybridBlocked as error:
        raise HybridBlocked("PART_OWNERSHIP_MAP_INVALID") from error
    if actual_hash.upper() != declared_hash.upper():
        _blocked_ownership_map()
    return ExplicitOwnershipContract(
        path, declared_hash.upper(), dict(labels),
        {name: tuple(parts) for name, parts in occluders.items()},
    )


def _require_literal_top_level_contract(
    manifest: dict, explicit_contract: ExplicitOwnershipContract | None = None
) -> None:
    if explicit_contract is None:
        explicit_contract = _explicit_ownership_contract(manifest)
    if not isinstance(manifest["root_motion"], bool):
        _blocked()
    for field in ("frame_count", "frame_width", "frame_height", "technical_scale"):
        if not _is_json_integer(manifest[field]):
            _blocked()
    if (
        not isinstance(manifest["pilot_contact_frames"], list)
        or not all(_is_json_integer(index) for index in manifest["pilot_contact_frames"])
    ):
        _blocked()
    if not all(manifest[field] == value for field, value in _TOP_LEVEL_LITERALS.items()):
        _blocked()
    if not isinstance(manifest["project_root"], str) or not manifest["project_root"]:
        _blocked()
    if not isinstance(manifest["source_path"], str) or not manifest["source_path"]:
        _blocked()
    if explicit_contract is None and manifest["ownership_order"] != list(OWNERSHIP_ORDER):
        _blocked()
    if not _is_technical_point(manifest["core_pivot"]):
        _blocked()
    if tuple(manifest["core_pivot"]) != CORE_PIVOT:
        _blocked()
    if not isinstance(manifest["parts"], dict):
        _blocked()
    if not isinstance(manifest["poses"], dict):
        _blocked()
    if not isinstance(manifest["qa"], dict):
        _blocked()


def _require_exact_part_set(parts: dict, expected: tuple[str, ...]) -> None:
    if not isinstance(parts, dict) or frozenset(parts) != frozenset(expected):
        _blocked()


def _require_exact_pose_set(poses: dict, expected: tuple[str, ...]) -> None:
    if not isinstance(poses, dict) or frozenset(poses) != frozenset(expected):
        _blocked()


def _is_finite_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_json_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_technical_point(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_json_integer(coordinate) for coordinate in value)
        and 0 <= value[0] < TECHNICAL_SIZE[0]
        and 0 <= value[1] < TECHNICAL_SIZE[1]
    )


def _require_point(value, context: str) -> None:
    if not _is_technical_point(value):
        _blocked()


def _require_polygon(value, context: str) -> None:
    if not isinstance(value, list) or len(value) < 3:
        _blocked()
    for point in value:
        _require_point(point, context)


def _require_vector(value, context: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(_is_finite_number(component) for component in value)
    ):
        _blocked()


def _require_piece_contract(parts: dict) -> None:
    for part in PART_NAMES:
        value = parts[part]
        _require_exact_keys(value, _PART_KEYS, f"parts.{part}")
        _require_polygon(value["polygon"], f"parts.{part}.polygon")
        _require_point(value["pivot"], f"parts.{part}.pivot")
        if value["kind"] != PART_KINDS[part]:
            _blocked()


def _require_part_mapping(value, expected_value_keys: frozenset[str], context: str) -> None:
    _require_exact_part_set(value, PART_NAMES)
    for part in PART_NAMES:
        item = value[part]
        _require_exact_keys(item, expected_value_keys, f"{context}.{part}")


def _require_chain_pose_contract(
    value: dict, chains: tuple[joint.JointChain, ...]
) -> None:
    try:
        joint.require_chain_transforms(value, chains)
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error


def _require_pose_contact_payload(
    pose: dict, chains: tuple[joint.JointChain, ...]
) -> None:
    _require_part_mapping(pose["transforms"], _TRANSFORM_KEYS, "transforms")
    for transform in pose["transforms"].values():
        _require_vector(transform["translation"], "transform.translation")
        if not _is_finite_number(transform["rotation_degrees"]):
            _blocked()

    if (
        not isinstance(pose["layer_order"], list)
        or len(pose["layer_order"]) != len(LAYER_NAMES)
        or frozenset(pose["layer_order"]) != frozenset(LAYER_NAMES)
    ):
        _blocked()

    _require_exact_part_set(pose["visibility"], PART_NAMES)
    if not all(isinstance(value, bool) for value in pose["visibility"].values()):
        _blocked()

    _require_exact_part_set(pose["flexible_polygons"], PART_NAMES)
    for polygon in pose["flexible_polygons"].values():
        _require_polygon(polygon, "flexible_polygons")

    chroma_rgb = pose["chroma_rgb"]
    if (
        not isinstance(chroma_rgb, list)
        or len(chroma_rgb) != 3
        or any(
            not isinstance(component, int)
            or isinstance(component, bool)
            or component < 0
            or component > 255
            for component in chroma_rgb
        )
    ):
        _blocked()
    _require_chain_pose_contract(pose["chain_transforms"], chains)


def _require_pose_contract(poses: dict, chains: tuple[joint.JointChain, ...]) -> None:
    for frame_index, pose_name in enumerate(POSE_NAMES, start=1):
        pose = poses[pose_name]
        expected_keys = _POSE_BASE_KEYS
        if frame_index in CONTACT_FRAME_INDICES:
            expected_keys |= _POSE_CONTACT_KEYS
        _require_exact_keys(pose, expected_keys, f"poses.{pose_name}")

        if (
            not _is_json_integer(pose["frame_index"])
            or pose["frame_index"] != frame_index
            or pose["name"] != pose_name
            or pose["support_foot"]
            != ("left" if pose_name.startswith("left_") else "right")
            or pose["phase"] != pose_name.split("_", 1)[1]
            or not all(
                _is_finite_number(pose[field])
                for field in ("stride", "counter_swing", "body_y")
            )
        ):
            _blocked()
        if frame_index in CONTACT_FRAME_INDICES:
            _require_pose_contact_payload(pose, chains)


def _require_qa_contract(qa: dict) -> None:
    _require_exact_keys(qa, _QA_KEYS, "qa")
    for field in (
        "contact_foot_separation_bbox_ratio_min",
        "hand_excursion_bbox_ratio_min",
    ):
        value = qa[field]
        if not _is_finite_number(value) or not 0 <= value <= 1:
            _blocked()
    mismatch = qa["protected_rgba_mismatch_max"]
    if not isinstance(mismatch, int) or isinstance(mismatch, bool) or mismatch < 0:
        _blocked()


def _resolve_project_root(value: str) -> Path:
    if not isinstance(value, str) or not value:
        _blocked()
    if value == _CODEX_HOME_TOKEN or value.startswith(f"{_CODEX_HOME_TOKEN}/"):
        configured = os.environ.get("CODEX_HOME")
        try:
            codex_home = (Path(configured) if configured else Path.home() / ".codex").resolve(strict=True)
            suffix = value[len(_CODEX_HOME_TOKEN):].lstrip("/")
            resolved = (codex_home / Path(suffix)).resolve(strict=True)
        except OSError as error:
            raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
        try:
            resolved.relative_to(codex_home)
        except ValueError as error:
            raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
        return resolved
    if "$" in value or "%" in value:
        _blocked()
    return Path(value).resolve(strict=True)


def _require_existing_confined_source(manifest: dict) -> tuple[Path, Path]:
    try:
        project_root = _resolve_project_root(manifest["project_root"])
        source_path = Path(manifest["source_path"])
        if source_path.is_absolute():
            _blocked()
        source = (project_root / source_path).resolve(strict=True)
        source.relative_to(project_root)
    except (OSError, ValueError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    if not project_root.is_dir() or not source.is_file():
        _blocked()
    return project_root, source


def validate_manifest(path: Path) -> dict:
    manifest = _read_json_object(path)
    _require_literal_top_level_contract(manifest)
    _require_exact_part_set(manifest["parts"], PART_NAMES)
    _require_exact_pose_set(manifest["poses"], POSE_NAMES)
    _require_piece_contract(manifest["parts"])
    try:
        chains = joint.validate_joint_contract(
            manifest["joint_chains"], PART_NAMES, TECHNICAL_SIZE
        )
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    _require_pose_contract(manifest["poses"], chains)
    _require_qa_contract(manifest["qa"])
    _require_existing_confined_source(manifest)
    return manifest


def _require_rgba_technical_master(master: Image.Image) -> None:
    if not isinstance(master, Image.Image) or master.mode != "RGBA" or master.size != TECHNICAL_SIZE:
        _blocked_part_atlas()


def _opaque_mask_pixel_count(mask: Image.Image) -> int:
    return sum(1 for alpha in mask.get_flattened_data() if alpha)


def opaque_pixel_count(image: Image.Image) -> int:
    try:
        return _opaque_mask_pixel_count(image.getchannel("A"))
    except (AttributeError, ValueError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error


def overlap_pixel_count(masks: list[Image.Image]) -> int:
    if not masks:
        return 0
    if any(mask.mode != "L" or mask.size != masks[0].size for mask in masks):
        _blocked_part_atlas()
    return sum(
        1
        for values in zip(*(mask.get_flattened_data() for mask in masks))
        if sum(bool(value) for value in values) > 1
    )


def owned_opaque_pixel_count(atlas: dict[str, Part]) -> int:
    return sum(_opaque_mask_pixel_count(part.ownership_mask) for part in atlas.values())


def part_source_rgba_mismatch(part: Part, master: Image.Image) -> int:
    if (
        not isinstance(part, Part)
        or part.image.mode != "RGBA"
        or master.mode != "RGBA"
        or part.image.size != master.size
        or part.ownership_mask.mode != "L"
        or part.ownership_mask.size != master.size
    ):
        _blocked_part_atlas()
    return sum(
        1
        for owned, source_pixel, part_pixel in zip(
            part.ownership_mask.get_flattened_data(),
            master.get_flattened_data(),
            part.image.get_flattened_data(),
        )
        if owned and source_pixel != part_pixel
    )


def _require_complete_disjoint_ownership(master: Image.Image, atlas: dict[str, Part]) -> None:
    masks = [part.ownership_mask for part in atlas.values()]
    if (
        not atlas
        or overlap_pixel_count(masks) != 0
        or opaque_pixel_count(master) != owned_opaque_pixel_count(atlas)
        or any(part_source_rgba_mismatch(part, master) != 0 for part in atlas.values())
    ):
        _blocked_part_atlas()


def _require_extractable_part_contract(
    manifest: dict,
) -> ExplicitOwnershipContract | None:
    try:
        explicit_contract = _explicit_ownership_contract(manifest)
        _require_literal_top_level_contract(manifest, explicit_contract)
        _require_exact_part_set(manifest["parts"], PART_NAMES)
        _require_piece_contract(manifest["parts"])
    except (HybridBlocked, KeyError, TypeError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error
    return explicit_contract


def load_ownership_label_map(
    contract: ExplicitOwnershipContract, master: Image.Image
) -> Image.Image:
    try:
        with Image.open(contract.path) as loaded:
            if (
                loaded.format != "PNG"
                or loaded.mode != "L"
                or loaded.size != TECHNICAL_SIZE
            ):
                _blocked_ownership_map()
            labels = loaded.copy()
    except (OSError, ValueError) as error:
        raise HybridBlocked("PART_OWNERSHIP_MAP_INVALID") from error
    allowed = frozenset({0, *contract.labels.values()})
    for alpha, label in zip(
        master.getchannel("A").get_flattened_data(), labels.get_flattened_data()
    ):
        if label not in allowed or (alpha == 0) != (label == 0):
            _blocked_ownership_map()
    used = frozenset(value for value in labels.get_flattened_data() if value)
    if used != frozenset(contract.labels.values()):
        _blocked_ownership_map()
    return labels


def extract_piece_atlas(master: Image.Image, manifest: dict) -> dict[str, Part]:
    _require_rgba_technical_master(master)
    if not isinstance(manifest, dict):
        _blocked_part_atlas()
    explicit_contract = _require_extractable_part_contract(manifest)
    if explicit_contract is not None:
        label_map = load_ownership_label_map(explicit_contract, master)
        alpha = master.getchannel("A")
        atlas: dict[str, Part] = {}
        for name in LAYER_NAMES:
            owned = label_map.point(
                lambda label, expected=explicit_contract.labels[name]: 255
                if label == expected
                else 0
            )
            image = master.copy()
            image.putalpha(ImageChops.multiply(alpha, owned))
            if name == "core":
                atlas[name] = Part(
                    name,
                    image,
                    owned,
                    tuple(manifest["core_pivot"]),
                    "protected",
                )
            else:
                part_config = manifest["parts"][name]
                atlas[name] = Part(
                    name,
                    image,
                    owned,
                    tuple(part_config["pivot"]),
                    part_config["kind"],
                )
        _require_complete_disjoint_ownership(master, atlas)
        return atlas
    alpha = master.getchannel("A")
    visible = alpha.point(lambda value: 255 if value else 0)
    claimed = Image.new("L", master.size, 0)
    atlas: dict[str, Part] = {}
    for name in manifest["ownership_order"]:
        part_config = manifest["parts"][name]
        configured = polygon_mask(master.size, part_config["polygon"])
        owned = ImageChops.multiply(
            visible, ImageChops.subtract(configured, claimed)
        )
        claimed = ImageChops.lighter(claimed, owned)
        image = master.copy()
        image.putalpha(ImageChops.multiply(alpha, owned))
        atlas[name] = Part(
            name,
            image,
            owned,
            tuple(part_config["pivot"]),
            part_config["kind"],
        )
    core_mask = ImageChops.multiply(visible, ImageChops.invert(claimed))
    core_image = master.copy()
    core_image.putalpha(ImageChops.multiply(alpha, core_mask))
    core_pivot = tuple(manifest["core_pivot"])
    atlas["core"] = Part("core", core_image, core_mask, core_pivot, "protected")
    _require_complete_disjoint_ownership(master, atlas)
    return atlas


def transform_part(part: Part, transform: dict) -> Image.Image:
    rotated = part.image.rotate(
        transform["rotation_degrees"],
        resample=Image.Resampling.NEAREST,
        center=part.pivot,
        expand=False,
    )
    dx, dy = transform["translation"]
    return translate(rotated, dx, dy)


def _polygons_to_mask(
    size: tuple[int, int], polygons: list[list[list[int]]]
) -> Image.Image:
    mask = Image.new("L", size, 0)
    for polygon in polygons:
        mask = ImageChops.lighter(mask, polygon_mask(size, polygon))
    return mask


def _opaque_alpha_mask(image: Image.Image) -> Image.Image:
    return image.getchannel("A").point(lambda alpha: 255 if alpha else 0)


def _build_protected_visible_mask(
    transformed: dict[str, Image.Image], pose: dict
) -> Image.Image:
    visible = _opaque_alpha_mask(transformed["core"])
    core_index = pose["layer_order"].index("core")
    for name in pose["layer_order"][core_index + 1 :]:
        if pose["visibility"][name]:
            visible = ImageChops.subtract(visible, _opaque_alpha_mask(transformed[name]))
    return visible


def _build_protected_reference(
    transformed: dict[str, Image.Image], pose: dict
) -> Image.Image:
    reference = Image.new("RGBA", TECHNICAL_SIZE, (0, 0, 0, 0))
    overlay_exact(reference, transformed["core"])
    return reference


def _restore_visible_protected_rgba(
    canvas: Image.Image, transformed: dict[str, Image.Image], pose: dict
) -> None:
    visible = _build_protected_visible_mask(transformed, pose)
    reference = _build_protected_reference(transformed, pose)
    canvas.paste(reference, (0, 0), visible)


def _restore_composition_protected_pixels(
    result: Image.Image, composition: PoseComposition
) -> None:
    result.paste(
        composition.protected_reference,
        (0, 0),
        composition.protected_visible_mask,
    )


def _require_rgba_technical_repair(repair: Image.Image) -> None:
    if (
        not isinstance(repair, Image.Image)
        or repair.mode != "RGBA"
        or repair.size != TECHNICAL_SIZE
    ):
        _blocked_part_atlas()


def _chain_member_names(chains: tuple[joint.JointChain, ...]) -> frozenset[str]:
    try:
        members = frozenset(
            definition.child_part
            for chain in chains
            for definition in chain.joints
        )
    except (AttributeError, TypeError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error
    if not members or members.intersection({"core", "loincloth_front"}):
        _blocked_part_atlas()
    return members


def _support_chain_name(pose: dict) -> tuple[str, str]:
    try:
        support_foot = pose["support_foot"]
    except (KeyError, TypeError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error
    values = {
        "left": ("screen_left_leg", "screen_left_lower_leg_foot"),
        "right": ("screen_right_leg", "screen_right_lower_leg_foot"),
    }
    if support_foot not in values:
        _blocked_part_atlas()
    return values[support_foot]


def _opaque_ground_line(image: Image.Image) -> int:
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        _blocked_part_atlas()
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        _blocked_part_atlas()
    return bbox[3] - 1


def _resolve_grounded_chains(
    master: Image.Image,
    atlas: dict[str, Part],
    chains: tuple[joint.JointChain, ...],
    pose: dict,
) -> joint.ChainResolution:
    try:
        chain_transforms = pose["chain_transforms"]
        chain_name, support_part = _support_chain_name(pose)
        resolution = joint.resolve_joint_chains(
            atlas, chains, chain_transforms, TECHNICAL_SIZE
        )
        support_image = resolution.transformed[support_part]
        correction = _opaque_ground_line(master) - _opaque_ground_line(support_image)
        if not isinstance(correction, int):
            _blocked_part_atlas()
        if correction == 0:
            return resolution
        support_members = tuple(
            definition.child_part
            for chain in chains
            if chain.name == chain_name
            for definition in chain.joints
        )
        if not support_members:
            _blocked_part_atlas()
        before = {
            name: _opaque_image_signature(resolution.transformed[name])
            for name in support_members
        }
        corrected_transforms = json.loads(json.dumps(chain_transforms))
        corrected_transforms[chain_name]["root_translation"][1] += correction
        corrected = joint.resolve_joint_chains(
            atlas, chains, corrected_transforms, TECHNICAL_SIZE
        )
        corrected_support = corrected.transformed[support_part]
        def correction_preserves_member(name: str) -> bool:
            before_count, before_bbox = before[name]
            after_count, after_bbox = _opaque_image_signature(corrected.transformed[name])
            if before_count != after_count:
                return False
            if before_bbox is None:
                return after_bbox is None
            expected_bbox = (
                before_bbox[0],
                before_bbox[1] + correction,
                before_bbox[2],
                before_bbox[3] + correction,
            )
            return (
                0 <= expected_bbox[0] < expected_bbox[2] <= TECHNICAL_SIZE[0]
                and 0 <= expected_bbox[1] < expected_bbox[3] <= TECHNICAL_SIZE[1]
                and after_bbox == expected_bbox
            )
        if (
            _opaque_ground_line(corrected_support) != _opaque_ground_line(master)
            or any(
                not correction_preserves_member(name)
                for name in support_members
            )
        ):
            raise HybridBlocked("SUPPORT_FOOT_GROUNDING_INVALID")
        return corrected
    except HybridBlocked:
        raise
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked(str(error)) from error
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error


def _opaque_image_signature(image: Image.Image) -> tuple[int, tuple[int, int, int, int] | None]:
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        _blocked_part_atlas()
    alpha = image.getchannel("A")
    return (
        sum(value != 0 for value in alpha.get_flattened_data()),
        alpha.getbbox(),
    )


def _chain_joint_masks(
    chains: tuple[joint.JointChain, ...],
    target_anchors: dict[str, tuple[float, float]],
    flexible_mask: Image.Image,
    protected_visible_mask: Image.Image,
) -> dict[str, Image.Image]:
    forbidden = ImageChops.invert(flexible_mask)
    try:
        definitions = tuple(
            definition for chain in chains for definition in chain.joints
        )
        expected_names = {definition.name for definition in definitions}
        if (
            not isinstance(target_anchors, dict)
            or len(expected_names) != len(definitions)
            or set(target_anchors) != expected_names
        ):
            raise HybridBlocked("JOINT_REPAIR_CAPABILITY_BLOCKED")
        return {
            definition.name: joint.build_effective_joint_mask(
                TECHNICAL_SIZE,
                joint.translate_repair_polygon(
                    definition.repair_polygon,
                    definition.parent_anchor,
                    target_anchors[definition.name],
                    definition.max_radius,
                    TECHNICAL_SIZE,
                ),
                protected_visible_mask,
                forbidden,
            )
            for definition in definitions
        }
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked(str(error)) from error


def _transparent_composite(
    transformed: dict[str, Image.Image], pose: dict
) -> Image.Image:
    result = Image.new("RGBA", TECHNICAL_SIZE, (0, 0, 0, 0))
    for name in pose["layer_order"]:
        if name == "core" or pose["visibility"][name]:
            result.alpha_composite(transformed[name])
    return result


def _rendered_alpha_center(image: Image.Image) -> tuple[float, float]:
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        _blocked_part_atlas()
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        _blocked_part_atlas()
    return ((bbox[0] + bbox[2]) / 4, (bbox[1] + bbox[3]) / 4)


def _chain_landmark_centers(resolution: joint.ChainResolution) -> dict[str, tuple[float, float]]:
    names = (
        "screen_left_lower_leg_foot",
        "screen_right_lower_leg_foot",
        "screen_left_lower_arm_hand",
        "screen_right_lower_arm_hand",
    )
    try:
        return {
            name: _rendered_alpha_center(resolution.transformed[name])
            for name in names
        }
    except (AttributeError, KeyError, TypeError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error


def repair_outside_flexible_count(composition: PoseComposition) -> int:
    if not isinstance(composition, PoseComposition):
        _blocked_part_atlas()
    try:
        return joint.repair_out_of_bounds_count(
            composition.joint_repair_mask, composition.flexible_mask
        )
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked(str(error)) from error


def compose_pose(
    master: Image.Image,
    atlas: dict[str, Part],
    pose: dict,
    chains: tuple[joint.JointChain, ...] | None = None,
) -> PoseComposition:
    _require_rgba_technical_master(master)
    try:
        core = atlas["core"]
        if chains is not None and pose["body_y"] != 0:
            raise HybridBlocked("CORE_TRANSLATION_FORBIDDEN")
        transformed = {"core": translate(core.image, 0, pose["body_y"])}
        if chains is None:
            transformed.update(
                {
                    name: transform_part(atlas[name], pose["transforms"][name])
                    for name in pose["layer_order"]
                    if name != "core"
                },
            )
        else:
            chain_members = _chain_member_names(chains)
            resolution = _resolve_grounded_chains(master, atlas, chains, pose)
            transformed.update(resolution.transformed)
            transformed["loincloth_front"] = transform_part(
                atlas["loincloth_front"], pose["transforms"]["loincloth_front"]
            )
            if (
                set(transformed) != set(pose["layer_order"])
                or not chain_members.issubset(transformed)
            ):
                _blocked_part_atlas()
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error

    canvas = Image.new("RGBA", TECHNICAL_SIZE, tuple(pose["chroma_rgb"]) + (255,))
    for name in pose["layer_order"]:
        if name == "core" or pose["visibility"][name]:
            overlay_exact(canvas, transformed[name])
    flexible = _polygons_to_mask(
        TECHNICAL_SIZE, list(pose["flexible_polygons"].values())
    )
    protected_visible = _build_protected_visible_mask(transformed, pose)
    protected_reference = _build_protected_reference(transformed, pose)
    joint_repair_mask = Image.new("L", TECHNICAL_SIZE, 0)
    joint_repair_rgba: frozenset[joint.RGBA] = frozenset()
    joint_evidence = {}
    if chains is not None:
        try:
            joint_masks = _chain_joint_masks(
                chains, resolution.target_anchors, flexible, protected_visible
            )
            resolution = joint.ChainResolution(
                transformed,
                resolution.target_anchors,
                joint_masks,
            )
            joint_result = joint.repair_joint_gaps(
                master,
                resolution,
                chains,
                pose["layer_order"],
                protected_visible,
                ImageChops.invert(flexible),
            )
        except joint.JointReconstructionBlocked as error:
            raise HybridBlocked(str(error)) from error
        if joint.component_count(_transparent_composite(transformed, pose)) > joint.component_count(master):
            raise HybridBlocked("NEW_ALPHA_COMPONENTS")
        canvas.paste(joint_result.image, (0, 0), joint_result.repair_mask)
        joint_repair_mask = joint_result.repair_mask
        joint_repair_rgba = joint_result.used_rgba
        joint_evidence = {
            "joint_masks": joint_masks,
            "repaired_pixel_count": joint_result.repaired_pixel_count,
            "used_rgba": joint_result.used_rgba,
            "landmark_centers": _chain_landmark_centers(resolution),
        }
    _restore_visible_protected_rgba(canvas, transformed, pose)
    return PoseComposition(
        pose["frame_index"],
        canvas,
        flexible,
        protected_visible,
        protected_reference,
        pose["transforms"],
        joint_repair_mask,
        joint_repair_rgba,
        joint_evidence,
    )


def apply_local_repair(composition: PoseComposition, repair: Image.Image) -> Image.Image:
    _require_rgba_technical_repair(repair)
    result = composition.technical.copy()
    result.paste(repair, (0, 0), composition.flexible_mask)
    _restore_composition_protected_pixels(result, composition)
    return result


def masked_difference_count(
    left: Image.Image, right: Image.Image, mask: Image.Image
) -> int:
    if (
        not isinstance(left, Image.Image)
        or not isinstance(right, Image.Image)
        or not isinstance(mask, Image.Image)
        or left.mode != "RGBA"
        or right.mode != "RGBA"
        or mask.mode != "L"
        or left.size != right.size
        or left.size != mask.size
    ):
        _blocked_part_atlas()
    return sum(
        1
        for left_pixel, right_pixel, selected in zip(
            left.get_flattened_data(),
            right.get_flattened_data(),
            mask.get_flattened_data(),
        )
        if selected and left_pixel != right_pixel
    )


def protected_rgba_mismatch(result: Image.Image, composition: PoseComposition) -> int:
    return masked_difference_count(
        result,
        composition.protected_reference,
        composition.protected_visible_mask,
    )


def nearest_downsample(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    if (
        not isinstance(image, Image.Image)
        or image.mode != "RGBA"
        or image.size != TECHNICAL_SIZE
        or not isinstance(size, tuple)
        or len(size) != 2
        or not all(_is_json_integer(value) and value > 0 for value in size)
    ):
        _blocked_part_atlas()
    return image.resize(size, Image.Resampling.NEAREST)


def _contact_chroma(manifest: dict) -> tuple[int, int, int]:
    try:
        chromas = {
            tuple(_pose_for_frame(manifest, index)["chroma_rgb"])
            for index in CONTACT_FRAME_INDICES
        }
    except (KeyError, TypeError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    if (
        len(chromas) != 1
        or any(
            len(chroma) != 3
            or any(not _is_json_integer(channel) or not 0 <= channel <= 255 for channel in chroma)
            for chroma in chromas
        )
    ):
        _blocked()
    return chromas.pop()


def _normalized_contact_image(image: Image.Image, chroma: tuple[int, int, int]) -> Image.Image:
    return nearest_downsample(recover_alpha(image, chroma), NORMALIZED_SIZE)


def _pose_for_frame(manifest: dict, frame_index: int) -> dict:
    try:
        poses = manifest["poses"]
        return next(
            pose
            for pose in poses.values()
            if pose["frame_index"] == frame_index
        )
    except (AttributeError, KeyError, StopIteration, TypeError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error


def _transformed_part_center(
    pose: dict, part_name: str, part: dict
) -> tuple[float, float]:
    try:
        polygon = part["polygon"]
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        center_x = (min(xs) + max(xs)) / 2
        center_y = (min(ys) + max(ys)) / 2
        pivot_x, pivot_y = part["pivot"]
        transform = pose["transforms"][part_name]
        translation_x, translation_y = transform["translation"]
        angle = math.radians(transform["rotation_degrees"])
    except (KeyError, TypeError, ValueError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    relative_x = center_x - pivot_x
    relative_y = center_y - pivot_y
    rotated_x = relative_x * math.cos(angle) - relative_y * math.sin(angle)
    rotated_y = relative_x * math.sin(angle) + relative_y * math.cos(angle)
    return (
        (pivot_x + rotated_x + translation_x) / 2,
        (pivot_y + rotated_y + translation_y) / 2,
    )


def _part_relative_horizontal_position(
    pose: dict, part_name: str, part: dict
) -> float:
    try:
        polygon = part["polygon"]
        center_x = (min(point[0] for point in polygon) + max(point[0] for point in polygon)) / 2
        center_y = (min(point[1] for point in polygon) + max(point[1] for point in polygon)) / 2
        pivot_x, pivot_y = part["pivot"]
        transform = pose["transforms"][part_name]
        translation_x, _ = transform["translation"]
        angle = math.radians(transform["rotation_degrees"])
    except (KeyError, TypeError, ValueError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    relative_x = center_x - pivot_x
    relative_y = center_y - pivot_y
    return relative_x * math.cos(angle) - relative_y * math.sin(angle) + translation_x


def _frame_bbox_width(frame: Image.Image) -> float:
    bbox = frame.getchannel("A").getbbox()
    if bbox is None or bbox[2] <= bbox[0]:
        return 0.0
    return float(bbox[2] - bbox[0])


def _frame_horizontal_center(frame: Image.Image) -> float | None:
    bbox = frame.getchannel("A").getbbox()
    if bbox is None:
        return None
    return (bbox[0] + bbox[2]) / 2


def _evidence_landmark_centers(
    compositions: dict[int, PoseComposition] | None,
) -> dict[int, dict[str, tuple[float, float]]] | None:
    if compositions is None:
        return None
    names = (
        "screen_left_lower_leg_foot",
        "screen_right_lower_leg_foot",
        "screen_left_lower_arm_hand",
        "screen_right_lower_arm_hand",
    )
    try:
        evidence = {
            frame_index: compositions[frame_index].joint_evidence
            for frame_index in CONTACT_FRAME_INDICES
        }
    except (AttributeError, KeyError, TypeError) as error:
        raise HybridBlocked("PART_ATLAS_INVALID") from error
    present = {
        frame_index: "landmark_centers" in frame_evidence
        for frame_index, frame_evidence in evidence.items()
    }
    if not any(present.values()):
        return None
    if not all(present.values()):
        _blocked_part_atlas()
    centers = {}
    for frame_index, frame_evidence in evidence.items():
        frame_centers = frame_evidence["landmark_centers"]
        if (
            not isinstance(frame_centers, dict)
            or set(frame_centers) != set(names)
            or any(
                not isinstance(frame_centers[name], tuple)
                or len(frame_centers[name]) != 2
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    for value in frame_centers[name]
                )
                for name in names
            )
        ):
            _blocked_part_atlas()
        centers[frame_index] = frame_centers
    return centers


def measure_contact_landmarks(
    frames: dict[int, Image.Image],
    manifest: dict,
    compositions: dict[int, PoseComposition] | None = None,
) -> dict:
    try:
        left = frames[1]
        right = frames[5]
        left_pose = _pose_for_frame(manifest, 1)
        right_pose = _pose_for_frame(manifest, 5)
        parts = manifest["parts"]
        left_width = _frame_bbox_width(left)
        right_width = _frame_bbox_width(right)
        hand_width = max(left_width, right_width)
        left_foot = parts["screen_left_lower_leg_foot"]
        right_foot = parts["screen_right_lower_leg_foot"]
        left_hand = parts["screen_left_lower_arm_hand"]
        right_hand = parts["screen_right_lower_arm_hand"]
    except (AttributeError, KeyError, TypeError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error

    rendered_centers = _evidence_landmark_centers(compositions)

    def foot_separation(
        pose: dict, width: float, frame_index: int
    ) -> float:
        if width <= 0:
            return 0.0
        if rendered_centers is None:
            left_center = _transformed_part_center(
                pose, "screen_left_lower_leg_foot", left_foot
            )
            right_center = _transformed_part_center(
                pose, "screen_right_lower_leg_foot", right_foot
            )
        else:
            left_center = rendered_centers[frame_index]["screen_left_lower_leg_foot"]
            right_center = rendered_centers[frame_index]["screen_right_lower_leg_foot"]
        return abs(right_center[0] - left_center[0]) / width

    if rendered_centers is None:
        left_hand_start = _part_relative_horizontal_position(
            left_pose, "screen_left_lower_arm_hand", left_hand
        )
        left_hand_end = _part_relative_horizontal_position(
            right_pose, "screen_left_lower_arm_hand", left_hand
        )
        right_hand_start = _part_relative_horizontal_position(
            left_pose, "screen_right_lower_arm_hand", right_hand
        )
        right_hand_end = _part_relative_horizontal_position(
            right_pose, "screen_right_lower_arm_hand", right_hand
        )
    else:
        left_hand_start = rendered_centers[1]["screen_left_lower_arm_hand"][0]
        left_hand_end = rendered_centers[5]["screen_left_lower_arm_hand"][0]
        right_hand_start = rendered_centers[1]["screen_right_lower_arm_hand"][0]
        right_hand_end = rendered_centers[5]["screen_right_lower_arm_hand"][0]
    left_root = _frame_horizontal_center(left)
    right_root = _frame_horizontal_center(right)
    root_drift = (
        math.inf
        if left_root is None or right_root is None
        else abs(right_root - left_root)
    )
    return {
        "foot_separation_ratio": {
            1: foot_separation(left_pose, left_width, 1),
            5: foot_separation(right_pose, right_width, 5),
        },
        "left_hand_excursion_ratio": (
            abs(left_hand_end - left_hand_start) / (2 * hand_width)
            if hand_width > 0
            else 0.0
        ),
        "right_hand_excursion_ratio": (
            abs(right_hand_end - right_hand_start) / (2 * hand_width)
            if hand_width > 0
            else 0.0
        ),
        "root_horizontal_drift_px": root_drift,
    }


def _append_minimum_failure(
    value: float, minimum: float, reason: str, failures: list[str]
) -> None:
    if value < minimum and reason not in failures:
        failures.append(reason)


def _append_exact_failure(
    value: int | float, expected: int | float, reason: str, failures: list[str]
) -> None:
    if value != expected and reason not in failures:
        failures.append(reason)


def _sequential_contact_manifest(manifest: dict) -> dict:
    try:
        poses = [
            {"support_foot": _pose_for_frame(manifest, index)["support_foot"]}
            for index in range(1, len(POSE_NAMES) + 1)
        ]
    except (KeyError, TypeError) as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    return {
        "poses": poses,
        "foot_regions": {
            "screen_left": {
                "bbox_ratio": [0.00, 0.95, 0.50, 1.00],
                "leg_corridor_ratio": [0.05, 0.45, 0.45, 0.92],
            },
            "screen_right": {
                "bbox_ratio": [0.50, 0.95, 1.00, 1.00],
                "leg_corridor_ratio": [0.55, 0.45, 0.95, 0.92],
            },
        },
        "qa": {
            "center_drift_px_max": 0,
            "opaque_area_ratio_min": 0.50,
            "opaque_area_ratio_max": 1.50,
            "bbox_ratio_delta_max": 0.50,
            "new_alpha_components_max": 0,
            "orphan_area_ratio_max": 0.0,
            "new_foot_island_area_px_max": 0,
            "planted_foot_drift_px_max": 2,
        },
    }


def _append_group_failures(failures: list[str], group: dict) -> None:
    for reason in group.get("failures", []):
        if reason not in failures:
            failures.append(reason)


def _append_existing_qa_failures(
    failures: list[str],
    frames: dict[int, Image.Image],
    master: Image.Image,
    manifest: dict,
) -> dict:
    adapted_manifest = _sequential_contact_manifest(manifest)
    geometry = {
        index: geometry_checks(frame, master, adapted_manifest)
        for index, frame in frames.items()
    }
    alpha_components = {
        index: component_checks(frame, master, adapted_manifest)
        for index, frame in frames.items()
    }
    foot_reports = {
        index: foot_checks(frame, None, adapted_manifest, index)
        for index, frame in frames.items()
    }
    for groups in (geometry, alpha_components, foot_reports):
        for group in groups.values():
            _append_group_failures(failures, group)
    orientation_failures = ["ORIENTATION_NOT_VERIFIED"]
    if manifest.get("orientation") != "S_3_4":
        orientation_failures.append("ORIENTATION_MISMATCH")
        _append_group_failures(failures, {"failures": ["ORIENTATION_MISMATCH"]})
    return {
        "geometry": geometry,
        "orientation": {
            "expected": "S_3_4",
            "declared": manifest.get("orientation"),
            "status": "PENDING_VISUAL_ORIENTATION_REVIEW",
            "failures": orientation_failures,
        },
        "alpha_components": alpha_components,
        "feet": {
            "frames": foot_reports,
            "planted_foot_drift": "NOT_EVALUATED_UNTIL_EIGHT_FRAMES",
            "loop_closure": "NOT_EVALUATED_UNTIL_EIGHT_FRAMES",
            "failures": [
                reason
                for report in foot_reports.values()
                for reason in report["failures"]
            ],
        },
    }


def _build_contact_report(
    failures: list[str], measurements: dict, existing: dict
) -> dict:
    movement_failures = [
        reason
        for reason in failures
        if reason
        in {
            "CONTACT_FOOT_SEPARATION",
            "HAND_EXCURSION",
            "ROOT_HORIZONTAL_DRIFT",
        }
    ]
    protected_failures = [
        reason for reason in failures if reason == "PROTECTED_RGBA_MISMATCH"
    ]
    checks = {
        "geometry": existing["geometry"],
        "orientation": existing["orientation"],
        "movement": {
            "foot_separation_ratio": measurements["foot_separation_ratio"],
            "foot_separation_ratio_min": min(
                measurements["foot_separation_ratio"].values()
            ),
            "left_hand_excursion_ratio": measurements["left_hand_excursion_ratio"],
            "right_hand_excursion_ratio": measurements["right_hand_excursion_ratio"],
            "root_horizontal_drift_px": measurements["root_horizontal_drift_px"],
            "torso_bob": "NOT_EVALUATED_UNTIL_EIGHT_FRAMES",
            "failures": movement_failures,
        },
        "protected_pixels": {
            "rgba_mismatch": measurements["protected_rgba_mismatch"],
            "failures": protected_failures,
        },
        "alpha_components": existing["alpha_components"],
        "feet": existing["feet"],
    }
    passed = not failures
    return {
        "technical_result": "PASS" if passed else "FAIL",
        "checks": checks,
        "failures": failures,
        "passed": passed,
    }


def evaluate_contact_pair(
    master: Image.Image,
    left: PoseComposition,
    right: PoseComposition,
    manifest: dict,
) -> dict:
    chroma = _contact_chroma(manifest)
    normalized = {
        1: _normalized_contact_image(left.technical, chroma),
        5: _normalized_contact_image(right.technical, chroma),
    }
    master_normalized = _normalized_contact_image(master, chroma)
    measurements = measure_contact_landmarks(normalized, manifest, {1: left, 5: right})
    measurements["protected_rgba_mismatch"] = (
        protected_rgba_mismatch(left.technical, left)
        + protected_rgba_mismatch(right.technical, right)
    )
    failures = []
    _append_minimum_failure(
        measurements["foot_separation_ratio"][1],
        0.18,
        "CONTACT_FOOT_SEPARATION",
        failures,
    )
    _append_minimum_failure(
        measurements["foot_separation_ratio"][5],
        0.18,
        "CONTACT_FOOT_SEPARATION",
        failures,
    )
    _append_minimum_failure(
        measurements["left_hand_excursion_ratio"],
        0.10,
        "HAND_EXCURSION",
        failures,
    )
    _append_minimum_failure(
        measurements["right_hand_excursion_ratio"],
        0.10,
        "HAND_EXCURSION",
        failures,
    )
    _append_exact_failure(
        measurements["root_horizontal_drift_px"],
        0,
        "ROOT_HORIZONTAL_DRIFT",
        failures,
    )
    _append_exact_failure(
        measurements["protected_rgba_mismatch"],
        0,
        "PROTECTED_RGBA_MISMATCH",
        failures,
    )
    existing = _append_existing_qa_failures(
        failures,
        normalized,
        master_normalized,
        manifest,
    )
    return _build_contact_report(failures, measurements, existing)


def contact_pair_passed(report: dict) -> bool:
    return report.get("technical_result") == "PASS" and report.get("passed") is True


_RUN_LAYOUT = (
    "input/master",
    "input/parts",
    "poses/technical",
    "poses/normalized",
    "previews",
    "reports",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise HybridBlocked("ARTIFACT_INVALID") from error
    return digest.hexdigest()


def is_within(path: Path, parent: Path) -> bool:
    try:
        resolved_path = Path(path).resolve(strict=False)
        resolved_parent = Path(parent).resolve(strict=False)
        resolved_path.relative_to(resolved_parent)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _validate_manifest_value(manifest: dict) -> tuple[Path, Path]:
    if not isinstance(manifest, dict):
        _blocked()
    _require_literal_top_level_contract(manifest)
    _require_exact_part_set(manifest["parts"], PART_NAMES)
    _require_exact_pose_set(manifest["poses"], POSE_NAMES)
    _require_piece_contract(manifest["parts"])
    try:
        chains = joint.validate_joint_contract(
            manifest["joint_chains"], PART_NAMES, TECHNICAL_SIZE
        )
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    _require_pose_contract(manifest["poses"], chains)
    _require_qa_contract(manifest["qa"])
    return _require_existing_confined_source(manifest)


def _validate_new_external_destination(
    project_root: Path,
    workspace_root: Path,
    run_id: str,
) -> tuple[Path, Path]:
    try:
        project_root = Path(project_root)
        workspace = Path(workspace_root).resolve(strict=True)
        workspace.relative_to(project_root)
    except ValueError:
        pass
    except (OSError, RuntimeError, TypeError) as error:
        raise HybridBlocked("WORKSPACE_UNSAFE") from error
    else:
        raise HybridBlocked("WORKSPACE_UNSAFE")
    try:
        project_root.relative_to(workspace)
    except ValueError:
        pass
    except (OSError, RuntimeError, TypeError) as error:
        raise HybridBlocked("WORKSPACE_UNSAFE") from error
    else:
        raise HybridBlocked("WORKSPACE_UNSAFE")
    if not project_root.is_dir() or not workspace.is_dir():
        raise HybridBlocked("WORKSPACE_UNSAFE")
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in {".", ".."}
        or Path(run_id).name != run_id
    ):
        raise HybridBlocked("WORKSPACE_UNSAFE")
    destination = workspace / run_id
    temporary = workspace / f".{run_id}.temporary"
    if (
        not is_within(destination, workspace)
        or not is_within(temporary, workspace)
        or destination.exists()
        or temporary.exists()
    ):
        raise HybridBlocked("WORKSPACE_UNSAFE")
    return destination, temporary


def _create_exact_layout(run_dir: Path) -> None:
    try:
        run_dir.mkdir()
        for relative_directory in _RUN_LAYOUT:
            (run_dir / relative_directory).mkdir(parents=True)
    except OSError as error:
        raise HybridBlocked("WORKSPACE_UNSAFE") from error


def _copy_and_normalize_master(source: Path, run_dir: Path) -> tuple[Image.Image, dict]:
    copied_source = run_dir / "input" / "master" / "source.png"
    technical_path = run_dir / "input" / "master" / "technical.png"
    try:
        shutil.copyfile(source, copied_source)
        with Image.open(source) as loaded:
            source_image = loaded.convert("RGBA")
        if source_image.size == NORMALIZED_SIZE:
            master = source_image.resize(TECHNICAL_SIZE, Image.Resampling.NEAREST)
        elif source_image.size == TECHNICAL_SIZE:
            master = source_image
        else:
            raise HybridBlocked("MASTER_SIZE_INVALID")
        master.save(technical_path)
    except HybridBlocked:
        raise
    except (OSError, ValueError) as error:
        raise HybridBlocked("MASTER_INVALID") from error
    return master, {
        "source_sha256": sha256_file(source),
        "source_copy": copied_source.relative_to(run_dir).as_posix(),
        "technical_master": technical_path.relative_to(run_dir).as_posix(),
    }


def _compose_contact_frames(
    master: Image.Image, atlas: dict[str, Part], manifest: dict
) -> dict[int, PoseComposition]:
    try:
        chains = joint.validate_joint_contract(
            manifest["joint_chains"], PART_NAMES, TECHNICAL_SIZE
        )
    except joint.JointReconstructionBlocked as error:
        raise HybridBlocked("HYBRID_MANIFEST_INVALID") from error
    return {
        frame_index: compose_pose(
            master,
            atlas,
            _pose_for_frame(manifest, frame_index),
            chains,
        )
        for frame_index in CONTACT_FRAME_INDICES
    }


def _write_json_atomic(path: Path, value: dict) -> None:
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.temporary")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise HybridBlocked("ARTIFACT_INVALID") from error


def _persist_pilot_artifacts(
    run_dir: Path,
    master_provenance: dict,
    atlas: dict[str, Part],
    contacts: dict[int, PoseComposition],
    report: dict,
) -> None:
    parts = {}
    for name in (*PART_NAMES, "core"):
        path = run_dir / "input" / "parts" / f"{name}.png"
        atlas[name].image.save(path)
        parts[name] = path.relative_to(run_dir).as_posix()

    technical_frames = {}
    normalized_frames = {}
    frame_sha256 = {}
    for frame_index, composition in contacts.items():
        technical_path = run_dir / "poses" / "technical" / f"frame_{frame_index:02d}.png"
        normalized_path = run_dir / "poses" / "normalized" / f"frame_{frame_index:02d}.png"
        composition.technical.save(technical_path)
        chroma = composition.technical.getpixel((0, 0))[:3]
        _normalized_contact_image(composition.technical, chroma).save(normalized_path)
        technical_frames[str(frame_index)] = technical_path.relative_to(run_dir).as_posix()
        normalized_frames[str(frame_index)] = normalized_path.relative_to(run_dir).as_posix()
        frame_sha256[str(frame_index)] = sha256_file(normalized_path)

    preview = Image.new("RGBA", (NORMALIZED_SIZE[0] * len(CONTACT_FRAME_INDICES), NORMALIZED_SIZE[1]))
    for position, frame_index in enumerate(CONTACT_FRAME_INDICES):
        with Image.open(run_dir / normalized_frames[str(frame_index)]) as frame:
            preview.alpha_composite(frame.convert("RGBA"), (position * NORMALIZED_SIZE[0], 0))
    preview_path = run_dir / "previews" / "contact_pair.png"
    preview.save(preview_path)

    joint_evidence = {}
    joint_mask_paths = {}
    for frame_index, composition in contacts.items():
        try:
            source_evidence = composition.joint_evidence
            masks = source_evidence.get("joint_masks", {})
            repaired_pixel_count = source_evidence.get("repaired_pixel_count", 0)
            used_rgba = source_evidence.get("used_rgba", frozenset())
            landmark_centers = source_evidence.get("landmark_centers", {})
            if (
                not isinstance(masks, dict)
                or not isinstance(repaired_pixel_count, int)
                or isinstance(repaired_pixel_count, bool)
                or repaired_pixel_count < 0
                or not isinstance(used_rgba, frozenset)
                or not isinstance(landmark_centers, dict)
            ):
                raise TypeError("joint evidence")
        except (AttributeError, TypeError, ValueError) as error:
            raise HybridBlocked("ARTIFACT_INVALID") from error

        persisted_masks = {}
        artifact_mask_paths = {}
        for name, mask in sorted(masks.items()):
            if (
                not isinstance(name, str)
                or not isinstance(mask, Image.Image)
                or mask.mode != "L"
                or mask.size != TECHNICAL_SIZE
            ):
                raise HybridBlocked("ARTIFACT_INVALID")
            mask_path = run_dir / "reports" / "joints" / f"frame_{frame_index:02d}-{name}.png"
            mask_path.parent.mkdir(parents=True, exist_ok=True)
            mask.save(mask_path)
            relative_path = mask_path.relative_to(run_dir).as_posix()
            persisted_masks[name] = {
                "path": relative_path,
                "sha256": sha256_file(mask_path),
            }
            artifact_mask_paths[name] = relative_path
        serialized_rgba = [list(rgba) for rgba in sorted(used_rgba)]
        if any(
            len(rgba) != 4
            or any(
                not isinstance(channel, int)
                or isinstance(channel, bool)
                or not 0 <= channel <= 255
                for channel in rgba
            )
            for rgba in serialized_rgba
        ):
            raise HybridBlocked("ARTIFACT_INVALID")
        serialized_landmarks = {}
        for name, center in sorted(landmark_centers.items()):
            if (
                not isinstance(name, str)
                or not isinstance(center, tuple)
                or len(center) != 2
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(value)
                    for value in center
                )
            ):
                raise HybridBlocked("ARTIFACT_INVALID")
            serialized_landmarks[name] = list(center)
        joint_evidence[str(frame_index)] = {
            "joint_masks": persisted_masks,
            "repaired_pixel_count": repaired_pixel_count,
            "used_rgba": serialized_rgba,
            "landmark_centers": serialized_landmarks,
        }
        joint_mask_paths[str(frame_index)] = artifact_mask_paths

    qa_path = run_dir / "reports" / "contact_qa.json"
    qa_report = dict(report)
    qa_report["joint_evidence"] = joint_evidence
    _write_json_atomic(qa_path, qa_report)
    artifacts = {
        "master": master_provenance["technical_master"],
        "source_copy": master_provenance["source_copy"],
        "parts": parts,
        "technical_frames": technical_frames,
        "normalized_frames": normalized_frames,
        "preview": preview_path.relative_to(run_dir).as_posix(),
        "qa_report": qa_path.relative_to(run_dir).as_posix(),
        "joint_masks": joint_mask_paths,
        "run_manifest": "reports/run_manifest.json",
    }
    _write_json_atomic(
        run_dir / "reports" / "pilot_artifacts.json",
        {
            "artifacts": artifacts,
            "master_sha256": master_provenance["source_sha256"],
            "frame_sha256": frame_sha256,
            "qa_failures": list(report.get("failures", [])),
            "joint_evidence": joint_evidence,
        },
    )


def _persist_run_manifest_last(run_dir: Path, state: str, report: dict) -> None:
    evidence = _read_json_object(run_dir / "reports" / "pilot_artifacts.json")
    try:
        run = {
            "state": state,
            "pilot_frames": list(CONTACT_FRAME_INDICES),
            "artifacts": evidence["artifacts"],
            "master_sha256": evidence["master_sha256"],
            "frame_sha256": evidence["frame_sha256"],
            "qa_failures": list(report.get("failures", [])),
        }
    except (KeyError, TypeError) as error:
        raise HybridBlocked("ARTIFACT_INVALID") from error
    _write_json_atomic(run_dir / "reports" / "run_manifest.json", run)


def _remove_only_verified_temporary_run(path: Path, workspace_root: Path) -> None:
    temporary = Path(path)
    if not temporary.exists():
        return
    try:
        workspace = Path(workspace_root).resolve(strict=True)
        resolved_temporary = temporary.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HybridBlocked("WORKSPACE_UNSAFE") from error
    if (
        temporary.is_symlink()
        or resolved_temporary.parent != workspace
        or not temporary.name.startswith(".")
        or not temporary.name.endswith(".temporary")
    ):
        raise HybridBlocked("WORKSPACE_UNSAFE")
    try:
        shutil.rmtree(resolved_temporary)
    except OSError as error:
        raise HybridBlocked("WORKSPACE_UNSAFE") from error


def prepare_pilot(manifest: dict, workspace_root: Path, run_id: str) -> Path:
    project_root, source = _validate_manifest_value(manifest)
    destination, temporary = _validate_new_external_destination(
        project_root,
        workspace_root,
        run_id,
    )
    try:
        _create_exact_layout(temporary)
        master, master_provenance = _copy_and_normalize_master(source, temporary)
        atlas = extract_piece_atlas(master, manifest)
        contacts = _compose_contact_frames(master, atlas, manifest)
        report = evaluate_contact_pair(master, contacts[1], contacts[5], manifest)
        _persist_pilot_artifacts(temporary, master_provenance, atlas, contacts, report)
        state = "READY_FOR_POSE_REVIEW" if contact_pair_passed(report) else "POSE_REJECTED"
        _persist_run_manifest_last(temporary, state, report)
        temporary.replace(destination)
        return destination
    except Exception:
        _remove_only_verified_temporary_run(temporary, workspace_root)
        raise


def _artifact_paths(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        paths = []
        for item in value.values():
            paths.extend(_artifact_paths(item))
        return paths
    raise HybridBlocked("RUN_MANIFEST_UNSAFE")


def _read_confined_run_manifest(path: Path) -> dict:
    try:
        run_manifest = Path(path).resolve(strict=True)
        reports = run_manifest.parent
        run_dir = reports.parent
    except (OSError, RuntimeError) as error:
        raise HybridBlocked("RUN_MANIFEST_UNSAFE") from error
    if (
        run_manifest.name != "run_manifest.json"
        or reports.name != "reports"
        or any(not (run_dir / relative).is_dir() for relative in _RUN_LAYOUT)
    ):
        raise HybridBlocked("RUN_MANIFEST_UNSAFE")
    run = _read_json_object(run_manifest)
    try:
        artifacts = run["artifacts"]
        if not isinstance(artifacts, dict):
            raise TypeError("artifacts")
        for relative_path in _artifact_paths(artifacts):
            artifact = Path(relative_path)
            if artifact.is_absolute():
                raise HybridBlocked("RUN_MANIFEST_UNSAFE")
            resolved_artifact = (run_dir / artifact).resolve(strict=True)
            if not is_within(resolved_artifact, run_dir):
                raise HybridBlocked("RUN_MANIFEST_UNSAFE")
        if not isinstance(run["state"], str):
            raise TypeError("state")
    except HybridBlocked:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise HybridBlocked("RUN_MANIFEST_UNSAFE") from error
    return run


def record_pose_review(run_manifest_path: Path, decision: str, reviewer: str) -> dict:
    if reviewer != "Nicolas":
        raise HybridBlocked("VISUAL_REVIEWER_INVALID")
    if decision not in {"APPROVED", "APPROVED_WITH_CORRECTIONS", "REJECTED"}:
        raise HybridBlocked("VISUAL_DECISION_INVALID")
    run = _read_confined_run_manifest(run_manifest_path)
    if run["state"] != "READY_FOR_POSE_REVIEW":
        raise HybridBlocked("VISUAL_REVIEW_STATE_INVALID")
    run["visual_review"] = {"reviewer": reviewer, "decision": decision}
    run["state"] = "POSE_APPROVED" if decision == "APPROVED" else decision
    _write_json_atomic(run_manifest_path, run)
    return run


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline hybrid contact-pose pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--manifest", required=True, type=Path)
    prepare = commands.add_parser("prepare-pilot")
    prepare.add_argument("--manifest", required=True, type=Path)
    prepare.add_argument("--workspace-root", required=True, type=Path)
    prepare.add_argument("--run-id", required=True)
    review = commands.add_parser("record-pose-review")
    review.add_argument("--run-manifest", required=True, type=Path)
    review.add_argument("--decision", required=True)
    review.add_argument("--reviewer", required=True)
    return parser


def _run_payload(run: dict) -> dict:
    return {
        "state": run["state"],
        "artifacts": run["artifacts"],
        "master_sha256": run["master_sha256"],
        "frame_sha256": run["frame_sha256"],
        "qa_failures": run["qa_failures"],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = _build_cli_parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            manifest = validate_manifest(arguments.manifest)
            _, source = _require_existing_confined_source(manifest)
            print(json.dumps({
                "state": "VALID",
                "mode": manifest["mode"],
                "pilot_frames": manifest["pilot_contact_frames"],
                "normalized_size": list(NORMALIZED_SIZE),
                "technical_size": list(TECHNICAL_SIZE),
                "source": manifest["source_path"],
                "source_sha256": sha256_file(source),
            }, sort_keys=True))
            return 0
        if arguments.command == "prepare-pilot":
            manifest = validate_manifest(arguments.manifest)
            run_dir = prepare_pilot(manifest, arguments.workspace_root, arguments.run_id)
            payload = _run_payload(_read_confined_run_manifest(run_dir / "reports" / "run_manifest.json"))
            print(json.dumps(payload, sort_keys=True))
            return 0 if payload["state"] == "READY_FOR_POSE_REVIEW" else 2
        run = record_pose_review(
            arguments.run_manifest, arguments.decision, arguments.reviewer
        )
        print(json.dumps(_run_payload(run), sort_keys=True))
        return 0
    except HybridBlocked as error:
        print(json.dumps({"state": "BLOCKED", "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
