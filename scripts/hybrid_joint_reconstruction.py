import math
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageChops, ImageDraw


RGBA = tuple[int, int, int, int]

_CORE_PART = "core"
_CHAIN_NAMES = (
    "screen_left_arm",
    "screen_right_arm",
    "screen_left_leg",
    "screen_right_leg",
)
_CHAIN_KEYS = frozenset({"root_part", "joints"})
_JOINT_KEYS = frozenset(
    {
        "name",
        "parent_part",
        "child_part",
        "parent_anchor",
        "child_anchor",
        "repair_polygon",
        "max_radius",
    }
)
_CHAIN_TRANSFORM_KEYS = frozenset({"root_translation", "rotation_degrees"})


class JointReconstructionBlocked(RuntimeError):
    pass


class PartLike(Protocol):
    image: Image.Image
    pivot: tuple[int, int]


@dataclass(frozen=True)
class JointDefinition:
    name: str
    parent_part: str
    child_part: str
    parent_anchor: tuple[int, int]
    child_anchor: tuple[int, int]
    repair_polygon: tuple[tuple[int, int], ...]
    max_radius: int


@dataclass(frozen=True)
class JointChain:
    name: str
    root_part: str
    joints: tuple[JointDefinition, ...]


@dataclass(frozen=True)
class ChainResolution:
    transformed: dict[str, Image.Image]
    target_anchors: dict[str, tuple[float, float]]
    joint_masks: dict[str, Image.Image]


@dataclass(frozen=True)
class JointRepairResult:
    image: Image.Image
    repair_mask: Image.Image
    repaired_pixel_count: int
    used_rgba: frozenset[RGBA]
    joint_statuses: dict[str, str]
    joint_repair_pixel_counts: dict[str, int]


REAL_JOINTS = (
    ("left_shoulder", "core", "screen_left_upper_arm", (170, 338), (170, 338)),
    (
        "left_elbow",
        "screen_left_upper_arm",
        "screen_left_lower_arm_hand",
        (124, 466),
        (124, 466),
    ),
    ("right_shoulder", "core", "screen_right_upper_arm", (338, 338), (338, 338)),
    (
        "right_elbow",
        "screen_right_upper_arm",
        "screen_right_lower_arm_hand",
        (384, 466),
        (384, 466),
    ),
    ("left_hip", "core", "screen_left_upper_leg", (200, 518), (200, 518)),
    (
        "left_knee",
        "screen_left_upper_leg",
        "screen_left_lower_leg_foot",
        (178, 666),
        (178, 666),
    ),
    ("right_hip", "core", "screen_right_upper_leg", (308, 518), (308, 518)),
    (
        "right_knee",
        "screen_right_upper_leg",
        "screen_right_lower_leg_foot",
        (330, 666),
        (330, 666),
    ),
)

_EXPECTED_CHAIN_JOINTS = {
    "screen_left_arm": REAL_JOINTS[0:2],
    "screen_right_arm": REAL_JOINTS[2:4],
    "screen_left_leg": REAL_JOINTS[4:6],
    "screen_right_leg": REAL_JOINTS[6:8],
}


def _blocked() -> None:
    raise JointReconstructionBlocked("JOINT_CHAIN_INVALID")


def _is_json_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_technical_point(value, technical_size: tuple[int, int]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_json_integer(coordinate) for coordinate in value)
        and 0 <= value[0] < technical_size[0]
        and 0 <= value[1] < technical_size[1]
    )


def _require_exact_keys(value, expected: frozenset[str]) -> None:
    if not isinstance(value, dict) or frozenset(value) != expected:
        _blocked()


def _polygon_double_area(polygon: list[list[int]]) -> int:
    return sum(
        point[0] * polygon[(index + 1) % len(polygon)][1]
        - point[1] * polygon[(index + 1) % len(polygon)][0]
        for index, point in enumerate(polygon)
    )


def _parse_joint(value, expected, technical_size: tuple[int, int]) -> JointDefinition:
    _require_exact_keys(value, _JOINT_KEYS)
    name, parent_part, child_part, parent_anchor, child_anchor = expected
    if (
        value["name"] != name
        or value["parent_part"] != parent_part
        or value["child_part"] != child_part
        or value["parent_anchor"] != list(parent_anchor)
        or value["child_anchor"] != list(child_anchor)
        or not _is_technical_point(value["parent_anchor"], technical_size)
        or not _is_technical_point(value["child_anchor"], technical_size)
    ):
        _blocked()

    polygon = value["repair_polygon"]
    if (
        not isinstance(polygon, list)
        or len(polygon) < 3
        or not all(_is_technical_point(point, technical_size) for point in polygon)
        or not _is_json_integer(value["max_radius"])
        or not 1 <= value["max_radius"] <= 24
        or any(
            (point[0] - parent_anchor[0]) ** 2
            + (point[1] - parent_anchor[1]) ** 2
            > value["max_radius"] ** 2
            for point in polygon
        )
        or _polygon_double_area(polygon) == 0
    ):
        _blocked()

    return JointDefinition(
        name=name,
        parent_part=parent_part,
        child_part=child_part,
        parent_anchor=parent_anchor,
        child_anchor=child_anchor,
        repair_polygon=tuple(tuple(point) for point in polygon),
        max_radius=value["max_radius"],
    )


def validate_joint_contract(
    value: dict,
    part_names: tuple[str, ...],
    technical_size: tuple[int, int],
) -> tuple[JointChain, ...]:
    if (
        not isinstance(value, dict)
        or frozenset(value) != frozenset(_CHAIN_NAMES)
        or not isinstance(part_names, tuple)
        or not isinstance(technical_size, tuple)
        or len(technical_size) != 2
        or not all(_is_json_integer(component) and component > 0 for component in technical_size)
    ):
        _blocked()

    articulated_parts = set(part_names)
    expected_children = {
        joint[2]
        for expected_joints in _EXPECTED_CHAIN_JOINTS.values()
        for joint in expected_joints
    }
    if not expected_children.issubset(articulated_parts):
        _blocked()

    chains = []
    child_parts = set()
    for chain_name in _CHAIN_NAMES:
        chain_value = value[chain_name]
        _require_exact_keys(chain_value, _CHAIN_KEYS)
        expected_joints = _EXPECTED_CHAIN_JOINTS[chain_name]
        if (
            chain_value["root_part"] != _CORE_PART
            or not isinstance(chain_value["joints"], list)
            or len(chain_value["joints"]) != len(expected_joints)
        ):
            _blocked()

        joints = tuple(
            _parse_joint(joint_value, expected, technical_size)
            for joint_value, expected in zip(chain_value["joints"], expected_joints)
        )
        for joint in joints:
            if joint.child_part == _CORE_PART or joint.child_part in child_parts:
                _blocked()
            child_parts.add(joint.child_part)
        chains.append(JointChain(chain_name, _CORE_PART, joints))

    if child_parts != expected_children:
        _blocked()
    return tuple(chains)


def require_chain_transforms(value: dict, chains: tuple[JointChain, ...]) -> None:
    if (
        not isinstance(value, dict)
        or not isinstance(chains, tuple)
        or frozenset(value) != frozenset(chain.name for chain in chains)
        or len({chain.name for chain in chains}) != len(chains)
    ):
        _blocked()

    for chain in chains:
        transform = value[chain.name]
        _require_exact_keys(transform, _CHAIN_TRANSFORM_KEYS)
        root_translation = transform["root_translation"]
        if (
            not isinstance(root_translation, list)
            or len(root_translation) != 2
            or not all(_is_finite_number(component) for component in root_translation)
            or not isinstance(transform["rotation_degrees"], dict)
            or frozenset(transform["rotation_degrees"])
            != frozenset(joint.child_part for joint in chain.joints)
            or not all(
                _is_finite_number(rotation)
                for rotation in transform["rotation_degrees"].values()
            )
        ):
            _blocked()


def _require_finite_pair(value) -> tuple[float, float]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not all(_is_finite_number(component) for component in value)
    ):
        _blocked()
    return (float(value[0]), float(value[1]))


def _round_half_away_from_zero(value: float) -> int:
    if not _is_finite_number(value):
        _blocked()
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _translate_nearest(image: Image.Image, translation: tuple[float, float]) -> Image.Image:
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        _blocked()
    dx = _round_half_away_from_zero(translation[0])
    dy = _round_half_away_from_zero(translation[1])
    return image.transform(
        image.size,
        Image.Transform.AFFINE,
        (1, 0, -dx, 0, 1, -dy),
        resample=Image.Resampling.NEAREST,
    )


def _require_resolution_size(size) -> tuple[int, int]:
    if (
        not isinstance(size, tuple)
        or len(size) != 2
        or not all(_is_json_integer(component) and component > 0 for component in size)
    ):
        _blocked()
    return size


def _require_atlas_part(atlas: dict, name: str, size: tuple[int, int]):
    if not isinstance(name, str) or name not in atlas:
        _blocked()
    part = atlas[name]
    try:
        image = part.image
        pivot = _require_finite_pair(part.pivot)
    except (AttributeError, TypeError):
        _blocked()
    if not isinstance(image, Image.Image) or image.mode != "RGBA" or image.size != size:
        _blocked()
    return part, pivot


def _require_anchor_in_canvas(anchor: tuple[float, float], size: tuple[int, int]) -> None:
    if not (0 <= anchor[0] < size[0] and 0 <= anchor[1] < size[1]):
        _blocked()


def transform_point(
    point: tuple[int, int],
    pivot: tuple[int, int],
    rotation_degrees: float,
    translation: tuple[float, float],
) -> tuple[float, float]:
    """Return an image-space point after Pillow-compatible rotation and translation."""
    x, y = _require_finite_pair(point)
    pivot_x, pivot_y = _require_finite_pair(pivot)
    translation_x, translation_y = _require_finite_pair(translation)
    if not _is_finite_number(rotation_degrees):
        _blocked()

    radians = math.radians(rotation_degrees)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    if not math.isfinite(cosine) or not math.isfinite(sine):
        _blocked()
    offset_x = x - pivot_x
    offset_y = y - pivot_y
    return (
        pivot_x + cosine * offset_x + sine * offset_y + translation_x,
        pivot_y - sine * offset_x + cosine * offset_y + translation_y,
    )


def resolve_joint_chains(
    atlas: dict[str, PartLike],
    chains: tuple[JointChain, ...],
    chain_transforms: dict,
    size: tuple[int, int],
) -> ChainResolution:
    """Resolve each declared chain in parent-to-child order without mutating inputs."""
    try:
        if not isinstance(atlas, dict) or not isinstance(chains, tuple):
            _blocked()
        resolution_size = _require_resolution_size(size)
        require_chain_transforms(chain_transforms, chains)

        transformed: dict[str, Image.Image] = {}
        target_anchors: dict[str, tuple[float, float]] = {}
        written_parts: set[str] = set()
        written_anchors: set[str] = set()

        for chain in chains:
            if (
                not isinstance(chain, JointChain)
                or not isinstance(chain.name, str)
                or not isinstance(chain.root_part, str)
                or not isinstance(chain.joints, tuple)
            ):
                _blocked()
            root_part, root_pivot = _require_atlas_part(
                atlas, chain.root_part, resolution_size
            )
            del root_part
            root_translation_values = chain_transforms[chain.name]["root_translation"]
            root_translation = (float(root_translation_values[0]), float(root_translation_values[1]))
            resolved = {
                chain.root_part: (root_pivot, 0.0, root_translation),
            }

            for definition in chain.joints:
                if (
                    not isinstance(definition, JointDefinition)
                    or not isinstance(definition.name, str)
                    or definition.name in written_anchors
                    or not isinstance(definition.parent_part, str)
                    or not isinstance(definition.child_part, str)
                    or definition.child_part in written_parts
                    or definition.parent_part not in resolved
                ):
                    _blocked()
                parent_pivot, parent_rotation, parent_translation = resolved[
                    definition.parent_part
                ]
                target = transform_point(
                    definition.parent_anchor,
                    parent_pivot,
                    parent_rotation,
                    parent_translation,
                )
                _require_anchor_in_canvas(target, resolution_size)

                child_part, child_pivot = _require_atlas_part(
                    atlas, definition.child_part, resolution_size
                )
                child_anchor = _require_finite_pair(definition.child_anchor)
                child_rotation = chain_transforms[chain.name]["rotation_degrees"][
                    definition.child_part
                ]
                if not _is_finite_number(child_rotation):
                    _blocked()
                translation = (
                    target[0] - child_anchor[0],
                    target[1] - child_anchor[1],
                )
                raster_translation = (
                    _round_half_away_from_zero(translation[0]),
                    _round_half_away_from_zero(translation[1]),
                )
                raster_anchor = (
                    child_anchor[0] + raster_translation[0],
                    child_anchor[1] + raster_translation[1],
                )
                _require_anchor_in_canvas(raster_anchor, resolution_size)
                rotated_child = child_part.image.rotate(
                    child_rotation,
                    resample=Image.Resampling.NEAREST,
                    center=definition.child_anchor,
                    expand=False,
                )
                transformed[definition.child_part] = _translate_nearest(
                    rotated_child, raster_translation
                )
                target_anchors[definition.name] = target
                written_parts.add(definition.child_part)
                written_anchors.add(definition.name)
                resolved[definition.child_part] = (
                    child_pivot,
                    child_rotation,
                    raster_translation,
                )

        return ChainResolution(transformed, target_anchors, {})
    except JointReconstructionBlocked:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        _blocked()


def _palette_blocked() -> None:
    raise JointReconstructionBlocked("JOINT_PALETTE_INVALID")


def _require_mask_size(size) -> tuple[int, int]:
    if (
        not isinstance(size, tuple)
        or len(size) != 2
        or not all(_is_json_integer(component) and component > 0 for component in size)
    ):
        _blocked()
    return size


def _require_mask_image(image, size: tuple[int, int]) -> Image.Image:
    if (
        not isinstance(image, Image.Image)
        or image.size != size
        or image.mode not in ("L", "RGBA")
    ):
        _blocked()
    return image.getchannel("A") if image.mode == "RGBA" else image


def _binary_ownership(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    channel = _require_mask_image(image, size)
    binary = Image.new("L", size, 0)
    binary.putdata([255 if value else 0 for value in channel.get_flattened_data()])
    return binary


def _require_declared_polygon(polygon, size: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
        _blocked()
    points = []
    for point in polygon:
        if (
            not isinstance(point, (list, tuple))
            or len(point) != 2
            or not all(_is_json_integer(coordinate) for coordinate in point)
            or not (0 <= point[0] < size[0] and 0 <= point[1] < size[1])
        ):
            _blocked()
        points.append((point[0], point[1]))
    if _polygon_double_area([list(point) for point in points]) == 0:
        _blocked()
    return tuple(points)


def master_rgba_palette(master: Image.Image) -> frozenset[RGBA]:
    """Return the exact nontransparent technical-master colors, or fail closed."""
    if (
        not isinstance(master, Image.Image)
        or master.mode != "RGBA"
        or master.size != (512, 832)
    ):
        _palette_blocked()
    return frozenset(
        pixel for pixel in master.get_flattened_data() if pixel[3] != 0
    )


def build_effective_joint_mask(
    size: tuple[int, int], polygon, protected_mask: Image.Image, forbidden_mask: Image.Image
) -> Image.Image:
    """Rasterize the declared polygon and remove every protected ownership pixel."""
    mask_size = _require_mask_size(size)
    points = _require_declared_polygon(polygon, mask_size)
    polygon_mask = Image.new("L", mask_size, 0)
    ImageDraw.Draw(polygon_mask).polygon(points, fill=255)
    blocked_mask = ImageChops.lighter(
        _binary_ownership(protected_mask, mask_size),
        _binary_ownership(forbidden_mask, mask_size),
    )
    return ImageChops.subtract(polygon_mask, blocked_mask)


_LOCAL_JOINT_SEARCH_RADIUS = 24


def _require_rgba_image(image, size: tuple[int, int]) -> Image.Image:
    if not isinstance(image, Image.Image) or image.mode != "RGBA" or image.size != size:
        _blocked()
    return image


def _nearest_local_boundary(
    alpha: Image.Image, effective: Image.Image
) -> tuple[int, int] | None:
    effective_points = [
        (x, y)
        for y in range(effective.height)
        for x in range(effective.width)
        if effective.getpixel((x, y)) != 0
    ]
    if not effective_points:
        return None
    min_x = max(0, min(x for x, _ in effective_points) - _LOCAL_JOINT_SEARCH_RADIUS)
    max_x = min(
        alpha.width - 1,
        max(x for x, _ in effective_points) + _LOCAL_JOINT_SEARCH_RADIUS,
    )
    min_y = max(0, min(y for _, y in effective_points) - _LOCAL_JOINT_SEARCH_RADIUS)
    max_y = min(
        alpha.height - 1,
        max(y for _, y in effective_points) + _LOCAL_JOINT_SEARCH_RADIUS,
    )
    radius_squared = _LOCAL_JOINT_SEARCH_RADIUS**2
    boundary = None
    best_key = None
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            if alpha.getpixel((x, y)) == 0:
                continue
            distance_squared = min(
                (x - effective_x) ** 2 + (y - effective_y) ** 2
                for effective_x, effective_y in effective_points
            )
            key = (distance_squared, y, x)
            if distance_squared <= radius_squared and (
                best_key is None or key < best_key
            ):
                best_key = key
                boundary = (x, y)
    return boundary


def _raster_line(start: tuple[int, int], end: tuple[int, int]):
    x, y = start
    end_x, end_y = end
    delta_x = abs(end_x - x)
    step_x = 1 if x < end_x else -1
    delta_y = -abs(end_y - y)
    step_y = 1 if y < end_y else -1
    error = delta_x + delta_y
    while True:
        yield (x, y)
        if (x, y) == (end_x, end_y):
            return
        doubled_error = 2 * error
        if doubled_error >= delta_y:
            error += delta_y
            x += step_x
        if doubled_error <= delta_x:
            error += delta_x
            y += step_y


def detect_joint_gap(
    parent: Image.Image, child: Image.Image, effective_mask: Image.Image
) -> Image.Image:
    """Return vacant effective-mask pixels on the nearest local boundary corridor."""
    if not isinstance(effective_mask, Image.Image):
        _blocked()
    size = _require_mask_size(effective_mask.size)
    parent_alpha = _require_rgba_image(parent, size).getchannel("A")
    child_alpha = _require_rgba_image(child, size).getchannel("A")
    effective = _binary_ownership(effective_mask, size)
    gap = Image.new("L", size, 0)
    parent_boundary = _nearest_local_boundary(parent_alpha, effective)
    child_boundary = _nearest_local_boundary(child_alpha, effective)
    if parent_boundary is None or child_boundary is None:
        return gap
    for point in _raster_line(parent_boundary, child_boundary):
        if point in (parent_boundary, child_boundary):
            continue
        if (
            effective.getpixel(point) != 0
            and parent_alpha.getpixel(point) == 0
            and child_alpha.getpixel(point) == 0
        ):
            gap.putpixel(point, 255)
    return gap


def repair_out_of_bounds_count(repair_mask: Image.Image, effective_mask: Image.Image) -> int:
    """Count owned repair pixels that are not inside the effective mask."""
    if not isinstance(effective_mask, Image.Image):
        _blocked()
    size = _require_mask_size(effective_mask.size)
    repair = _binary_ownership(repair_mask, size)
    effective = _binary_ownership(effective_mask, size)
    return sum(
        1
        for repair_value, effective_value in zip(
            repair.get_flattened_data(), effective.get_flattened_data()
        )
        if repair_value != 0 and effective_value == 0
    )


def overlap_count(left: Image.Image, right: Image.Image) -> int:
    """Count pixels owned by both masks or alpha channels."""
    if not isinstance(left, Image.Image) or not isinstance(right, Image.Image):
        _blocked()
    if left.size != right.size:
        _blocked()
    size = _require_mask_size(left.size)
    left_ownership = _binary_ownership(left, size)
    right_ownership = _binary_ownership(right, size)
    return sum(
        1
        for left_value, right_value in zip(
            left_ownership.get_flattened_data(), right_ownership.get_flattened_data()
        )
        if left_value != 0 and right_value != 0
    )


def _repair_blocked() -> None:
    raise JointReconstructionBlocked("JOINT_REPAIR_CAPABILITY_BLOCKED")


def translate_repair_polygon(
    polygon,
    source_anchor: tuple[int, int],
    target_anchor: tuple[float, float],
    max_radius: int,
    size: tuple[int, int],
) -> tuple[tuple[int, int], ...]:
    points = _require_declared_polygon(polygon, size)
    target = (
        _round_half_away_from_zero(target_anchor[0]),
        _round_half_away_from_zero(target_anchor[1]),
    )
    delta = (target[0] - source_anchor[0], target[1] - source_anchor[1])
    moved = tuple((x + delta[0], y + delta[1]) for x, y in points)
    if any(not (0 <= x < size[0] and 0 <= y < size[1]) for x, y in moved):
        _repair_blocked()
    if any(
        (x - target[0]) ** 2 + (y - target[1]) ** 2 > max_radius**2
        for x, y in moved
    ):
        _repair_blocked()
    return moved


def _require_layer_order(layer_order, transformed: dict[str, Image.Image]) -> tuple[str, ...]:
    if (
        not isinstance(layer_order, (list, tuple))
        or not all(isinstance(part_name, str) for part_name in layer_order)
        or len(layer_order) != len(set(layer_order))
        or set(layer_order) != set(transformed)
    ):
        _blocked()
    return tuple(layer_order)


def _chain_depth(
    chain: JointChain, layer_positions: dict[str, int], layer_order: tuple[str, ...]
) -> str:
    if _CORE_PART not in layer_positions:
        return "rear"
    chain_parts = {
        definition.child_part
        for definition in chain.joints
        if definition.child_part != _CORE_PART
    }
    if not chain_parts or not chain_parts.issubset(layer_positions):
        _blocked()
    core_position = layer_positions[_CORE_PART]
    if all(layer_positions[part_name] < core_position for part_name in chain_parts):
        return "rear"
    if all(layer_positions[part_name] > core_position for part_name in chain_parts):
        return "front"
    _blocked()


def _axis_donors(
    parent: Image.Image,
    child: Image.Image,
    effective_mask: Image.Image,
    gap: Image.Image,
    max_radius: int,
    depth: str,
) -> dict[tuple[int, int], RGBA]:
    """Find exact source pixels on the declared parent-to-child raster axis."""
    if gap.getbbox() is None:
        return {}
    parent_boundary = _nearest_local_boundary(parent.getchannel("A"), effective_mask)
    child_boundary = _nearest_local_boundary(child.getchannel("A"), effective_mask)
    if parent_boundary is None or child_boundary is None:
        _repair_blocked()

    axis = tuple(_raster_line(parent_boundary, child_boundary))
    axis_positions = {point: index for index, point in enumerate(axis)}
    repair_values: dict[tuple[int, int], RGBA] = {}
    for y in range(gap.height):
        for x in range(gap.width):
            point = (x, y)
            if gap.getpixel(point) == 0:
                continue
            if point not in axis_positions:
                _repair_blocked()
            position = axis_positions[point]
            parent_candidate = next(
                (
                    (position - candidate_position, parent.getpixel(candidate))
                    for candidate_position in range(position - 1, -1, -1)
                    if parent.getpixel(axis[candidate_position])[3] != 0
                    for candidate in (axis[candidate_position],)
                ),
                None,
            )
            child_candidate = next(
                (
                    (candidate_position - position, child.getpixel(candidate))
                    for candidate_position in range(position + 1, len(axis))
                    if child.getpixel(axis[candidate_position])[3] != 0
                    for candidate in (axis[candidate_position],)
                ),
                None,
            )
            if parent_candidate is None or child_candidate is None:
                _repair_blocked()
            parent_distance, parent_rgba = parent_candidate
            child_distance, child_rgba = child_candidate
            if max(parent_distance, child_distance) > max_radius:
                _repair_blocked()
            if parent_distance < child_distance:
                repair_values[point] = parent_rgba
            elif child_distance < parent_distance:
                repair_values[point] = child_rgba
            elif depth == "rear":
                repair_values[point] = parent_rgba
            else:
                repair_values[point] = child_rgba
    return repair_values


def _composite_layer_order(
    transformed: dict[str, Image.Image], layer_order: tuple[str, ...]
) -> Image.Image:
    image = Image.new("RGBA", next(iter(transformed.values())).size, (0, 0, 0, 0))
    for part_name in layer_order:
        image.alpha_composite(transformed[part_name])
    return image


def occluded_joint_bridge_exists(
    transformed: dict[str, Image.Image],
    layer_order: tuple[str, ...],
    visible_parts: frozenset[str],
    definition: JointDefinition,
    target_anchor: tuple[float, float],
    occluder_parts: tuple[str, ...],
) -> bool:
    """Return whether declared front occluders visibly bridge one joint."""
    try:
        if (
            not isinstance(transformed, dict)
            or not transformed
            or not isinstance(layer_order, tuple)
            or not all(isinstance(name, str) for name in layer_order)
            or len(layer_order) != len(set(layer_order))
            or set(layer_order) != set(transformed)
            or not isinstance(visible_parts, frozenset)
            or not all(isinstance(name, str) for name in visible_parts)
            or not isinstance(definition, JointDefinition)
            or not isinstance(occluder_parts, tuple)
            or not occluder_parts
            or not all(isinstance(name, str) for name in occluder_parts)
            or len(occluder_parts) != len(set(occluder_parts))
            or definition.parent_part == definition.child_part
            or definition.parent_part in occluder_parts
            or definition.child_part in occluder_parts
        ):
            return False

        required_names = {
            definition.parent_part,
            definition.child_part,
            *occluder_parts,
        }
        if not required_names.issubset(transformed):
            return False

        first_image = next(iter(transformed.values()))
        if (
            not isinstance(first_image, Image.Image)
            or first_image.mode != "RGBA"
            or first_image.width <= 0
            or first_image.height <= 0
            or any(
                not isinstance(image, Image.Image)
                or image.mode != "RGBA"
                or image.size != first_image.size
                for image in transformed.values()
            )
        ):
            return False

        if (
            not isinstance(target_anchor, tuple)
            or len(target_anchor) != 2
            or not all(_is_finite_number(coordinate) for coordinate in target_anchor)
        ):
            return False
        anchor = tuple(
            _round_half_away_from_zero(coordinate) for coordinate in target_anchor
        )
        if not (
            0 <= anchor[0] < first_image.width
            and 0 <= anchor[1] < first_image.height
        ):
            return False

        positions = {name: index for index, name in enumerate(layer_order)}
        joint_front = max(
            positions[definition.parent_part], positions[definition.child_part]
        )
        if any(
            name not in visible_parts or positions[name] <= joint_front
            for name in occluder_parts
        ):
            return False

        def opaque_points(image: Image.Image) -> set[tuple[int, int]]:
            return {
                (index % image.width, index // image.width)
                for index, alpha in enumerate(
                    image.getchannel("A").get_flattened_data()
                )
                if alpha != 0
            }

        parent_pixels = opaque_points(transformed[definition.parent_part])
        child_pixels = opaque_points(transformed[definition.child_part])
        occluder_pixels = set().union(
            *(opaque_points(transformed[name]) for name in occluder_parts)
        )
        union_pixels = parent_pixels | child_pixels | occluder_pixels
        seeds = {
            (x, y)
            for y in range(
                max(0, anchor[1] - 1), min(first_image.height, anchor[1] + 2)
            )
            for x in range(
                max(0, anchor[0] - 1), min(first_image.width, anchor[0] + 2)
            )
            if (x, y) in occluder_pixels
        }
        if not seeds:
            return False

        visited: set[tuple[int, int]] = set()
        for seed in seeds:
            if seed in visited:
                continue
            component: set[tuple[int, int]] = set()
            pending = [seed]
            visited.add(seed)
            while pending:
                point = pending.pop()
                component.add(point)
                for delta_y in (-1, 0, 1):
                    for delta_x in (-1, 0, 1):
                        if delta_x == 0 and delta_y == 0:
                            continue
                        neighbor = (point[0] + delta_x, point[1] + delta_y)
                        if neighbor in union_pixels and neighbor not in visited:
                            visited.add(neighbor)
                            pending.append(neighbor)
            if (
                not component.isdisjoint(parent_pixels)
                and not component.isdisjoint(child_pixels)
                and not component.isdisjoint(occluder_pixels)
            ):
                return True
        return False
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def repair_joint_gaps(
    master: Image.Image,
    resolution: ChainResolution,
    chains: tuple[JointChain, ...],
    layer_order,
    protected_mask: Image.Image,
    forbidden_mask: Image.Image,
    joint_occluders=None,
    visible_parts=None,
) -> JointRepairResult:
    """Fill declared joint corridors from nearest legal source pixels, deterministically."""
    palette = master_rgba_palette(master)
    try:
        if (
            not isinstance(resolution, ChainResolution)
            or not isinstance(resolution.transformed, dict)
            or not isinstance(resolution.joint_masks, dict)
            or not isinstance(chains, tuple)
            or not chains
        ):
            _blocked()
        size = master.size
        transformed = {
            name: _require_rgba_image(image, size)
            for name, image in resolution.transformed.items()
        }
        if not transformed:
            _blocked()
        ordered_parts = _require_layer_order(layer_order, transformed)
        layer_positions = {
            part_name: index for index, part_name in enumerate(ordered_parts)
        }
        protected = _binary_ownership(protected_mask, size)
        forbidden = _binary_ownership(forbidden_mask, size)
        blocked = ImageChops.lighter(protected, forbidden)

        repairs_by_layer: dict[int, dict[tuple[int, int], RGBA]] = {}
        effective_union = Image.new("L", size, 0)
        repair_mask = Image.new("L", size, 0)
        used_rgba: set[RGBA] = set()
        seen_joint_names: set[str] = set()
        joint_statuses: dict[str, str] = {}
        joint_repair_pixel_counts: dict[str, int] = {}

        for chain in chains:
            if not isinstance(chain, JointChain) or not chain.joints:
                _blocked()
            depth = _chain_depth(chain, layer_positions, ordered_parts)
            chain_parts = [
                definition.child_part
                for definition in chain.joints
                if definition.child_part != _CORE_PART
            ]
            if any(part_name not in transformed for part_name in chain_parts):
                _blocked()
            repair_layer = max(layer_positions[part_name] for part_name in chain_parts)

            for definition in chain.joints:
                if (
                    not isinstance(definition, JointDefinition)
                    or definition.name in seen_joint_names
                    or definition.name not in resolution.joint_masks
                    or not 1 <= definition.max_radius <= _LOCAL_JOINT_SEARCH_RADIUS
                ):
                    _blocked()
                seen_joint_names.add(definition.name)
                declared_occluders = (
                    joint_occluders.get(definition.name)
                    if isinstance(joint_occluders, dict)
                    and definition.name in joint_occluders
                    else None
                )
                if declared_occluders is not None:
                    target_anchor = (
                        resolution.target_anchors.get(definition.name)
                        if isinstance(resolution.target_anchors, dict)
                        else None
                    )
                    if occluded_joint_bridge_exists(
                        transformed,
                        ordered_parts,
                        visible_parts
                        if isinstance(visible_parts, frozenset)
                        else frozenset(),
                        definition,
                        target_anchor,
                        declared_occluders,
                    ):
                        joint_statuses[definition.name] = "OCCLUDED_BRIDGE"
                        joint_repair_pixel_counts[definition.name] = 0
                        continue
                declared_effective = _binary_ownership(
                    resolution.joint_masks[definition.name], size
                )
                effective = ImageChops.subtract(declared_effective, blocked)
                if declared_effective.getbbox() is not None and effective.getbbox() is None:
                    _repair_blocked()
                effective_union = ImageChops.lighter(effective_union, effective)
                parent_boundary = _nearest_local_boundary(
                    transformed[definition.parent_part].getchannel("A"), effective
                )
                child_boundary = _nearest_local_boundary(
                    transformed[definition.child_part].getchannel("A"), effective
                )
                if parent_boundary is None or child_boundary is None:
                    _repair_blocked()
                gap = detect_joint_gap(
                    transformed[definition.parent_part],
                    transformed[definition.child_part],
                    effective,
                )
                repair_values = _axis_donors(
                    transformed[definition.parent_part],
                    transformed[definition.child_part],
                    effective,
                    gap,
                    definition.max_radius,
                    depth,
                )
                joint_repair_pixel_count = 0
                for coordinate, rgba in repair_values.items():
                    if (
                        rgba not in palette
                        or repair_mask.getpixel(coordinate) != 0
                        or effective.getpixel(coordinate) == 0
                        or protected.getpixel(coordinate) != 0
                        or forbidden.getpixel(coordinate) != 0
                    ):
                        _repair_blocked()
                    repairs_by_layer.setdefault(repair_layer, {})[coordinate] = rgba
                    repair_mask.putpixel(coordinate, 255)
                    used_rgba.add(rgba)
                    joint_repair_pixel_count += 1
                joint_statuses[definition.name] = (
                    "REPAIRED" if joint_repair_pixel_count else "NO_GAP"
                )
                joint_repair_pixel_counts[definition.name] = joint_repair_pixel_count

        if (
            repair_out_of_bounds_count(repair_mask, effective_union) != 0
            or overlap_count(repair_mask, protected) != 0
            or overlap_count(repair_mask, forbidden) != 0
            or not used_rgba.issubset(palette)
        ):
            _repair_blocked()

        protected_baseline = _composite_layer_order(transformed, ordered_parts)
        result = Image.new("RGBA", size, (0, 0, 0, 0))
        for layer_index, part_name in enumerate(ordered_parts):
            result.alpha_composite(transformed[part_name])
            for coordinate, rgba in repairs_by_layer.get(layer_index, {}).items():
                result.putpixel(coordinate, rgba)
        for y in range(size[1]):
            for x in range(size[0]):
                if protected.getpixel((x, y)) != 0:
                    result.putpixel((x, y), protected_baseline.getpixel((x, y)))
        if any(
            result.getpixel((x, y)) != protected_baseline.getpixel((x, y))
            for y in range(size[1])
            for x in range(size[0])
            if protected.getpixel((x, y)) != 0
        ):
            _repair_blocked()
        return JointRepairResult(
            image=result,
            repair_mask=repair_mask,
            repaired_pixel_count=sum(value != 0 for value in repair_mask.get_flattened_data()),
            used_rgba=frozenset(used_rgba),
            joint_statuses=joint_statuses,
            joint_repair_pixel_counts=joint_repair_pixel_counts,
        )
    except JointReconstructionBlocked:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        _blocked()


def joint_palette_mismatch_count(
    result: JointRepairResult, palette: frozenset[RGBA]
) -> int:
    """Count used repair colors absent from the exact technical-master palette."""
    if (
        not isinstance(result, JointRepairResult)
        or not isinstance(palette, frozenset)
        or not all(
            isinstance(rgba, tuple)
            and len(rgba) == 4
            and all(_is_json_integer(channel) and 0 <= channel <= 255 for channel in rgba)
            for rgba in palette
        )
    ):
        _blocked()
    return sum(rgba not in palette for rgba in result.used_rgba)


def component_count(image: Image.Image) -> int:
    """Return the four-connected component count of nontransparent pixels."""
    if not isinstance(image, Image.Image) or image.mode != "RGBA":
        _blocked()
    alpha = image.getchannel("A")
    visited: set[tuple[int, int]] = set()
    components = 0
    for y in range(alpha.height):
        for x in range(alpha.width):
            start = (x, y)
            if start in visited or alpha.getpixel(start) == 0:
                continue
            components += 1
            stack = [start]
            visited.add(start)
            while stack:
                current_x, current_y = stack.pop()
                for neighbor in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    neighbor_x, neighbor_y = neighbor
                    if (
                        0 <= neighbor_x < alpha.width
                        and 0 <= neighbor_y < alpha.height
                        and neighbor not in visited
                        and alpha.getpixel(neighbor) != 0
                    ):
                        visited.add(neighbor)
                        stack.append(neighbor)
    return components
