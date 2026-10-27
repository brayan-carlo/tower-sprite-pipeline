import argparse
import base64
import io
import json
import hashlib
import math
import os
import shutil
import sys
from uuid import uuid4
from collections import deque
from collections.abc import Mapping
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


class PipelineBlocked(RuntimeError):
    pass


REQUIRED_FIELDS = frozenset(
    {
        "mode",
        "project_root",
        "source_path",
        "entity",
        "action",
        "direction",
        "frame_count",
        "frame_width",
        "frame_height",
        "technical_scale",
        "anchor",
        "root_motion",
        "max_budget_usd",
        "primary_model",
        "fallback_model",
        "max_primary_submits",
        "max_fallback_submits",
        "pose_names",
        "poses",
        "identity_regions",
        "foot_regions",
        "qa",
    }
)

EXPECTED_FIELDS = {
    "mode": "sequential_reconstruction",
    "project_root": "C:/UnityProjects/tower1",
    "source_path": "Assets/Art/GeneratedWork/Enemies/Ogro/Walk/source/Ogre_Walk_S_Base.png",
    "entity": "ogre",
    "action": "walk",
    "direction": "S",
    "frame_count": 8,
    "frame_width": 256,
    "frame_height": 416,
    "technical_scale": 2,
    "anchor": "bottom-center",
    "root_motion": False,
    "max_budget_usd": 0.50,
    "primary_model": "fal-ai/qwen-image-2/edit",
    "fallback_model": "fal-ai/nano-banana-2/edit",
    "max_primary_submits": 10,
    "max_fallback_submits": 2,
    "pose_names": [
        "left_contact",
        "left_down",
        "left_pass",
        "left_up",
        "right_contact",
        "right_down",
        "right_pass",
        "right_up",
    ],
    "poses": [
        {"name": "left_contact", "support_foot": "left", "stride": -1.0, "counter_swing": 1.0, "body_y": 0.0},
        {"name": "left_down", "support_foot": "left", "stride": -0.6, "counter_swing": 0.6, "body_y": 1.0},
        {"name": "left_pass", "support_foot": "left", "stride": 0.0, "counter_swing": 0.0, "body_y": 0.0},
        {"name": "left_up", "support_foot": "left", "stride": 0.6, "counter_swing": -0.6, "body_y": -1.0},
        {"name": "right_contact", "support_foot": "right", "stride": 1.0, "counter_swing": -1.0, "body_y": 0.0},
        {"name": "right_down", "support_foot": "right", "stride": 0.6, "counter_swing": -0.6, "body_y": 1.0},
        {"name": "right_pass", "support_foot": "right", "stride": 0.0, "counter_swing": 0.0, "body_y": 0.0},
        {"name": "right_up", "support_foot": "right", "stride": -0.6, "counter_swing": 0.6, "body_y": -1.0},
    ],
    "identity_regions": {
        "head_face": {"bbox_ratio": [0.42, 0.00, 0.88, 0.25], "master_hamming_max": 10, "previous_hamming_max": 8},
        "chest": {"bbox_ratio": [0.22, 0.18, 0.78, 0.48], "master_hamming_max": 14, "previous_hamming_max": 12},
        "belt": {"bbox_ratio": [0.16, 0.42, 0.82, 0.59], "master_hamming_max": 14, "previous_hamming_max": 12},
        "screen_left_bracer": {"bbox_ratio": [0.00, 0.30, 0.34, 0.62], "master_hamming_max": 14, "previous_hamming_max": 12},
        "screen_right_bracer": {"bbox_ratio": [0.68, 0.32, 1.00, 0.64], "master_hamming_max": 14, "previous_hamming_max": 12},
        "main_loincloth": {"bbox_ratio": [0.34, 0.49, 0.72, 0.82], "master_hamming_max": 14, "previous_hamming_max": 12},
    },
    "foot_regions": {
        "screen_left": {"bbox_ratio": [0.05, 0.78, 0.50, 1.00], "leg_corridor_ratio": [0.18, 0.50, 0.49, 0.96]},
        "screen_right": {"bbox_ratio": [0.50, 0.78, 0.98, 1.00], "leg_corridor_ratio": [0.51, 0.50, 0.86, 0.96]},
    },
    "qa": {
        "center_drift_px_max": 4,
        "opaque_area_ratio_min": 0.90,
        "opaque_area_ratio_max": 1.10,
        "bbox_ratio_delta_max": 0.05,
        "palette_colors": 64,
        "palette_rgb_distance_max": 48,
        "chroma_rgb_distance_max": 24,
        "out_of_palette_ratio_max": 0.02,
        "new_alpha_components_max": 2,
        "orphan_area_ratio_max": 0.005,
        "new_foot_island_area_px_max": 3,
        "planted_foot_drift_px_max": 2,
    },
}


RUN_DIRS = (
    "input/master",
    "input/pose-guides",
    "input/references",
    "raw",
    "normalized/frames",
    "previews",
    "reports",
)

CHROMA_CANDIDATES = ((255, 0, 255), (0, 255, 0), (0, 255, 255), (255, 255, 0))

PHASES = (
    ("left_contact", -1.0, 1.0, 0.0),
    ("left_down", -0.6, 0.6, 1.0),
    ("left_pass", 0.0, 0.0, 0.0),
    ("left_up", 0.6, -0.6, -1.0),
    ("right_contact", 1.0, -1.0, 0.0),
    ("right_down", 0.6, -0.6, 1.0),
    ("right_pass", 0.0, 0.0, 0.0),
    ("right_up", -0.6, 0.6, -1.0),
)

NORMALIZED_SIZE = (256, 416)
TECHNICAL_SIZE = (512, 832)
PREPARED_RUN_MARKER = "reports/prepared-run.json"
CHROMA_RGB_DISTANCE_MAX = 24
ANATOMICAL_TO_SCREEN_REGION = {
    "left": "screen_right",
    "right": "screen_left",
}

ACTIVE_REQUEST_STATUSES = frozenset({"IN_QUEUE", "IN_PROGRESS"})
TERMINAL_RUN_STATES = frozenset({"BLOCKED", "TECHNICAL_REJECTED", "READY_FOR_VISUAL_REVIEW"})
QA_CHECK_GROUPS = frozenset(
    {"geometry", "identity", "palette", "alpha_components", "feet"}
)
CONTENT_NORMALIZATION_FAILURES = frozenset(
    {"EMPTY_ALPHA", "NORMALIZED_SUBJECT_OUT_OF_BOUNDS"}
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _valid_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _image_png_sha256(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def chroma_distance_mask(
    rgba: Image.Image,
    chroma: tuple[int, int, int],
) -> Image.Image:
    threshold_squared = CHROMA_RGB_DISTANCE_MAX * CHROMA_RGB_DISTANCE_MAX
    mask = Image.new("L", rgba.size, 0)
    mask.putdata(
        [
            255
            if sum((pixel[channel] - chroma[channel]) ** 2 for channel in range(3))
            <= threshold_squared
            else 0
            for pixel in rgba.get_flattened_data()
        ]
    )
    return mask


def flood_from_edges(candidates: Image.Image) -> Image.Image:
    if candidates.mode != "L":
        candidates = candidates.convert("L")
    width, height = candidates.size
    candidate_values = bytes(candidates.get_flattened_data())
    visited = bytearray(width * height)
    pending = deque()

    def enqueue(x: int, y: int) -> None:
        index = y * width + x
        if candidate_values[index] and not visited[index]:
            visited[index] = 255
            pending.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while pending:
        x, y = pending.popleft()
        if x:
            enqueue(x - 1, y)
        if x + 1 < width:
            enqueue(x + 1, y)
        if y:
            enqueue(x, y - 1)
        if y + 1 < height:
            enqueue(x, y + 1)

    result = Image.new("L", candidates.size, 0)
    result.frombytes(bytes(visited))
    return result


def recover_alpha(
    raw: Image.Image,
    chroma: tuple[int, int, int],
) -> Image.Image:
    rgba = raw.convert("RGBA")
    candidates = chroma_distance_mask(rgba, chroma)
    background = flood_from_edges(candidates)
    result = rgba.copy()
    alpha = result.getchannel("A")
    alpha.paste(0, mask=background)
    result.putalpha(alpha)
    if alpha.getbbox() is None:
        raise PipelineBlocked("EMPTY_ALPHA")
    return result


def place_bottom_center(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox is None:
        raise PipelineBlocked("EMPTY_ALPHA")
    subject = rgba.crop(alpha_bbox)
    if subject.width > size[0] or subject.height > size[1] - 1:
        raise PipelineBlocked("NORMALIZED_SUBJECT_OUT_OF_BOUNDS")
    destination = Image.new("RGBA", size, (0, 0, 0, 0))
    offset = ((size[0] - subject.width) // 2, size[1] - 1 - subject.height)
    destination.alpha_composite(subject, offset)
    return destination


def _validated_raw_dimensions(
    raw_path: Path, run_manifest: dict, model: str | None = None
) -> tuple[int, int]:
    try:
        with Image.open(raw_path) as loaded:
            if loaded.format != "PNG":
                raise PipelineBlocked("RAW_FRAME_UNREADABLE")
            dimensions = loaded.size
            loaded.verify()
    except OSError as error:
        raise PipelineBlocked("RAW_FRAME_UNREADABLE") from error
    runtime_config = _resolve_runtime_config(run_manifest)
    if isinstance(model, str) and model == runtime_config.get("fallback_model"):
        width, height = dimensions
        if (
            width < NORMALIZED_SIZE[0]
            or height < NORMALIZED_SIZE[1]
            or width * NORMALIZED_SIZE[1] != height * NORMALIZED_SIZE[0]
        ):
            raise PipelineBlocked("RAW_SIZE_MISMATCH")
    elif dimensions != TECHNICAL_SIZE:
        raise PipelineBlocked("RAW_SIZE_MISMATCH")
    return dimensions


def normalize_frame(
    raw_path: Path, run_manifest: dict, model: str | None = None
) -> Image.Image:
    try:
        chroma = tuple(run_manifest["chroma_rgb"])
    except (KeyError, TypeError) as error:
        raise PipelineBlocked("MISSING_CHROMA_RGB") from error
    if len(chroma) != 3 or any(type(channel) is not int or not 0 <= channel <= 255 for channel in chroma):
        raise PipelineBlocked("INVALID_CHROMA_RGB")
    _validated_raw_dimensions(raw_path, run_manifest, model)
    try:
        with Image.open(raw_path) as loaded:
            recovered = recover_alpha(loaded, chroma)
    except OSError as error:
        raise PipelineBlocked("RAW_FRAME_UNREADABLE") from error
    reduced = recovered.resize(NORMALIZED_SIZE, Image.Resampling.BOX)
    return place_bottom_center(reduced, NORMALIZED_SIZE)


def _resolve_runtime_config(config: dict) -> dict:
    normalized_input = config.get("normalized_input")
    if not isinstance(normalized_input, Mapping):
        return config
    resolved = dict(normalized_input)
    resolved.update(
        (key, value)
        for key, value in config.items()
        if key != "normalized_input"
    )
    return resolved


def _opaque_count(image: Image.Image) -> int:
    return sum(1 for value in image.getchannel("A").get_flattened_data() if value)


def _alpha_bbox(image: Image.Image):
    return image.getchannel("A").getbbox()


def geometry_checks(frame: Image.Image, master: Image.Image, manifest: dict) -> dict:
    failures = []
    if frame.size != NORMALIZED_SIZE:
        failures.append("FRAME_SIZE_MISMATCH")
    if frame.mode != "RGBA":
        failures.append("FRAME_MODE_MISMATCH")

    rgba = frame.convert("RGBA")
    master_rgba = master.convert("RGBA")
    bbox = _alpha_bbox(rgba)
    master_bbox = _alpha_bbox(master_rgba)
    measurements = {
        "size": list(frame.size),
        "mode": frame.mode,
        "alpha_bbox": list(bbox) if bbox else None,
        "center_drift_px": None,
        "opaque_area_ratio": None,
        "bbox_width_ratio_delta": None,
        "bbox_height_ratio_delta": None,
    }
    if bbox is None:
        failures.append("EMPTY_ALPHA")
        return {"failures": failures, "measurements": measurements}
    if master_bbox is None:
        failures.append("MASTER_EMPTY_ALPHA")
        return {"failures": failures, "measurements": measurements}

    alpha_extrema = rgba.getchannel("A").getextrema()
    measurements["alpha_extrema"] = list(alpha_extrema)
    if alpha_extrema != (0, 255):
        failures.append("NO_REAL_TRANSPARENCY")
    if bbox[3] != 415:
        failures.append("GROUND_LINE_MISMATCH")

    center = (bbox[0] + bbox[2]) / 2
    master_center = (master_bbox[0] + master_bbox[2]) / 2
    center_drift = abs(center - master_center)
    measurements["center_drift_px"] = center_drift
    if center_drift > manifest["qa"]["center_drift_px_max"]:
        failures.append("CENTER_DRIFT")

    master_area = _opaque_count(master_rgba)
    area_ratio = _opaque_count(rgba) / master_area if master_area else math.inf
    measurements["opaque_area_ratio"] = area_ratio
    if not (
        manifest["qa"]["opaque_area_ratio_min"]
        <= area_ratio
        <= manifest["qa"]["opaque_area_ratio_max"]
    ):
        failures.append("OPAQUE_AREA_RATIO")

    bbox_width = bbox[2] - bbox[0]
    bbox_height = bbox[3] - bbox[1]
    master_width = master_bbox[2] - master_bbox[0]
    master_height = master_bbox[3] - master_bbox[1]
    width_delta = abs(bbox_width / master_width - 1)
    height_delta = abs(bbox_height / master_height - 1)
    measurements["bbox_width_ratio_delta"] = width_delta
    measurements["bbox_height_ratio_delta"] = height_delta
    bbox_limit = manifest["qa"]["bbox_ratio_delta_max"]
    if (
        abs(bbox_width - master_width) > bbox_limit * master_width
        or abs(bbox_height - master_height) > bbox_limit * master_height
    ):
        failures.append("BBOX_RATIO_DELTA")
    return {"failures": failures, "measurements": measurements}


def _aligned_identity_roi(image: Image.Image, ratios: list) -> Image.Image | None:
    rgba = image.convert("RGBA")
    bbox = _alpha_bbox(rgba)
    if bbox is None:
        return None
    roi_box = _resolve_ratio_rectangle(bbox, ratios)
    if roi_box[0] >= roi_box[2] or roi_box[1] >= roi_box[3]:
        return None
    crop = rgba.crop(roi_box)
    background = Image.new("RGBA", crop.size, (0, 0, 0, 255))
    background.alpha_composite(crop)
    return background.convert("L").resize((8, 8), Image.Resampling.BOX)


def perceptual_hash64(image: Image.Image) -> int:
    normalized = image.convert("L").resize((8, 8), Image.Resampling.BOX)
    values = list(normalized.get_flattened_data())
    average = sum(values) / 64
    result = 0
    for value in values:
        result = (result << 1) | int(value >= average)
    return result


def _hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def identity_checks(
    frame: Image.Image,
    master: Image.Image,
    previous: Image.Image | None,
    manifest: dict,
) -> dict:
    failures = []
    regions = {}
    for name, specification in manifest["identity_regions"].items():
        frame_roi = _aligned_identity_roi(frame, specification["bbox_ratio"])
        master_roi = _aligned_identity_roi(master, specification["bbox_ratio"])
        previous_roi = (
            _aligned_identity_roi(previous, specification["bbox_ratio"])
            if previous is not None
            else None
        )
        if frame_roi is None or master_roi is None:
            failures.append("IDENTITY_ROI_EMPTY")
            regions[name] = {
                "master_hamming": None,
                "previous_hamming": None,
                "passed": False,
            }
            continue
        frame_hash = perceptual_hash64(frame_roi)
        master_hamming = _hamming_distance(frame_hash, perceptual_hash64(master_roi))
        previous_hamming = (
            _hamming_distance(frame_hash, perceptual_hash64(previous_roi))
            if previous_roi is not None
            else None
        )
        region_failures = []
        if master_hamming > specification["master_hamming_max"]:
            failures.append("IDENTITY_MASTER_HAMMING")
            region_failures.append("IDENTITY_MASTER_HAMMING")
        if (
            previous_hamming is not None
            and previous_hamming > specification["previous_hamming_max"]
        ):
            failures.append("IDENTITY_PREVIOUS_HAMMING")
            region_failures.append("IDENTITY_PREVIOUS_HAMMING")
        regions[name] = {
            "master_hamming": master_hamming,
            "master_hamming_max": specification["master_hamming_max"],
            "previous_hamming": previous_hamming,
            "previous_hamming_max": specification["previous_hamming_max"],
            "failures": region_failures,
            "passed": not region_failures,
        }
    return {"failures": failures, "regions": regions}


def _reference_palette(master: Image.Image, colors: int) -> list[tuple[int, int, int]]:
    rgba = master.convert("RGBA")
    opaque_colors = [
        pixel[:3] for pixel in rgba.get_flattened_data() if pixel[3]
    ]
    if not opaque_colors:
        return []
    samples = Image.new("RGB", (len(opaque_colors), 1))
    samples.putdata(opaque_colors)
    quantized = samples.quantize(colors=colors).convert("RGB")
    return [color for _, color in quantized.getcolors(maxcolors=colors) or []]


def _rgb_distance_squared(left, right) -> int:
    return sum((left[channel] - right[channel]) ** 2 for channel in range(3))


def palette_checks(frame: Image.Image, master: Image.Image, manifest: dict) -> dict:
    manifest = _resolve_runtime_config(manifest)
    qa = manifest["qa"]
    palette = _reference_palette(master, qa["palette_colors"])
    opaque_pixels = [
        pixel for pixel in frame.convert("RGBA").get_flattened_data() if pixel[3]
    ]
    failures = []
    chroma = manifest.get("chroma_rgb")
    if (
        not isinstance(chroma, (list, tuple))
        or len(chroma) != 3
        or any(type(channel) is not int or not 0 <= channel <= 255 for channel in chroma)
    ):
        chroma = None
        failures.append("CHROMA_NOT_CONFIGURED")
    if not palette or not opaque_pixels:
        failures.append("PALETTE_UNAVAILABLE")
        return {
            "failures": failures,
            "measurements": {"out_of_palette_ratio": None},
        }
    distance_limit_squared = qa["palette_rgb_distance_max"] ** 2
    out_of_palette = sum(
        1
        for pixel in opaque_pixels
        if min(_rgb_distance_squared(pixel, color) for color in palette)
        > distance_limit_squared
    )
    ratio = out_of_palette / len(opaque_pixels)
    if ratio > qa["out_of_palette_ratio_max"]:
        failures.append("OUT_OF_PALETTE")

    halo_pixels = None
    if chroma is not None:
        halo_pixels = 0
        chroma_limit_squared = qa["chroma_rgb_distance_max"] ** 2
        halo_pixels = sum(
            1
            for pixel in opaque_pixels
            if _rgb_distance_squared(pixel, chroma) <= chroma_limit_squared
        )
        if halo_pixels:
            failures.append("CHROMA_HALO")
    partial_alpha = sum(1 for pixel in opaque_pixels if pixel[3] != 255)
    return {
        "failures": failures,
        "measurements": {
            "reference_palette_colors": len(palette),
            "opaque_pixels": len(opaque_pixels),
            "out_of_palette_pixels": out_of_palette,
            "out_of_palette_ratio": ratio,
            "chroma_halo_pixels": halo_pixels,
            "partial_alpha_ratio": partial_alpha / len(opaque_pixels),
        },
    }


def _label_components(image: Image.Image):
    alpha = bytes(image.convert("RGBA").getchannel("A").get_flattened_data())
    width, height = image.size
    labels = [-1] * (width * height)
    components = []
    for start, value in enumerate(alpha):
        if not value or labels[start] >= 0:
            continue
        label = len(components)
        labels[start] = label
        pending = deque([start])
        points = []
        while pending:
            index = pending.popleft()
            points.append(index)
            x = index % width
            y = index // width
            neighbors = []
            if x:
                neighbors.append(index - 1)
            if x + 1 < width:
                neighbors.append(index + 1)
            if y:
                neighbors.append(index - width)
            if y + 1 < height:
                neighbors.append(index + width)
            for neighbor in neighbors:
                if alpha[neighbor] and labels[neighbor] < 0:
                    labels[neighbor] = label
                    pending.append(neighbor)
        components.append(points)
    return labels, components


def component_checks(
    frame: Image.Image,
    master: Image.Image,
    manifest: dict,
    previous: Image.Image | None = None,
) -> dict:
    manifest = _resolve_runtime_config(manifest)
    _, frame_components = _label_components(frame)
    _, master_components = _label_components(master)
    baseline_count = len(master_components)
    master_alpha = bytes(master.convert("RGBA").getchannel("A").get_flattened_data())
    previous_alpha = (
        bytes(previous.convert("RGBA").getchannel("A").get_flattened_data())
        if previous is not None and previous.size == frame.size
        else None
    )
    matched_components = []
    unmatched_components = []
    for component in frame_components:
        overlaps_reference = any(
            master_alpha[index]
            or (previous_alpha is not None and previous_alpha[index])
            for index in component
        )
        destination = matched_components if overlaps_reference else unmatched_components
        destination.append(component)
    new_components = len(unmatched_components)
    subject_area = sum(len(component) for component in matched_components)
    orphan_area = sum(len(component) for component in unmatched_components)
    orphan_ratio = orphan_area / subject_area if subject_area else math.inf
    failures = []
    qa = manifest["qa"]
    component_count_limit = baseline_count + qa["new_alpha_components_max"]
    if (
        len(frame_components) > component_count_limit
        or new_components > qa["new_alpha_components_max"]
    ):
        failures.append("NEW_ALPHA_COMPONENTS")
    if orphan_ratio > qa["orphan_area_ratio_max"]:
        failures.append("ORPHAN_ALPHA_AREA")
    return {
        "failures": failures,
        "measurements": {
            "master_components": baseline_count,
            "frame_components": len(frame_components),
            "component_count_limit": component_count_limit,
            "new_components": new_components,
            "orphan_area_px": orphan_area,
            "orphan_area_ratio": orphan_ratio,
        },
    }


def _indices_in_rectangle(rectangle, width, height):
    left = max(0, rectangle[0])
    top = max(0, rectangle[1])
    right = min(width, rectangle[2])
    bottom = min(height, rectangle[3])
    for y in range(top, bottom):
        for x in range(left, right):
            yield y * width + x


def _component_labels_in_rectangle(labels, rectangle, width, height):
    return {
        labels[index]
        for index in _indices_in_rectangle(rectangle, width, height)
        if labels[index] >= 0
    }


def _foot_anchor(image, labels, connected_labels, foot_rectangle, alpha_bbox):
    width, height = image.size
    ground_y = alpha_bbox[3] - 1
    points = []
    for index in _indices_in_rectangle(foot_rectangle, width, height):
        if labels[index] in connected_labels and index // width == ground_y:
            points.append((index % width, ground_y))
    if not points:
        return None
    return (sum(point[0] for point in points) / len(points), float(ground_y))


def foot_checks(
    frame: Image.Image,
    previous: Image.Image | None,
    manifest: dict,
    frame_index: int,
) -> dict:
    failures = []
    measurements = {"planted_foot_drift_px": None}
    frame_rgba = frame.convert("RGBA")
    frame_bbox = _alpha_bbox(frame_rgba)
    previous_rgba = previous.convert("RGBA") if previous is not None else None
    previous_bbox = _alpha_bbox(previous_rgba) if previous_rgba is not None else None
    if frame_bbox is None:
        failures.append("EMPTY_ALPHA")
        return {
            "failures": failures,
            "measurements": measurements,
            "anatomical_to_screen_region": dict(ANATOMICAL_TO_SCREEN_REGION),
            "support_region": None,
        }
    if not 1 <= frame_index <= len(manifest["poses"]):
        failures.append("INVALID_FRAME_INDEX")
        support_foot = None
    else:
        support_foot = manifest["poses"][frame_index - 1]["support_foot"]
    support_region = ANATOMICAL_TO_SCREEN_REGION.get(support_foot)
    previous_support_foot = (
        manifest["poses"][frame_index - 2]["support_foot"]
        if previous is not None and frame_index > 1
        else None
    )
    same_support_interval = previous_support_foot == support_foot

    frame_labels, frame_components = _label_components(frame_rgba)
    previous_labels = previous_components = None
    if previous_rgba is not None and previous_bbox is not None:
        previous_labels, previous_components = _label_components(previous_rgba)
    region_reports = {}
    support_anchor = None
    previous_support_anchor = None
    for name, specification in manifest["foot_regions"].items():
        foot_rectangle = _resolve_ratio_rectangle(frame_bbox, specification["bbox_ratio"])
        corridor_rectangle = _resolve_ratio_rectangle(
            frame_bbox, specification["leg_corridor_ratio"]
        )
        foot_labels = _component_labels_in_rectangle(
            frame_labels, foot_rectangle, frame.width, frame.height
        )
        corridor_labels = _component_labels_in_rectangle(
            frame_labels, corridor_rectangle, frame.width, frame.height
        )
        grounded_labels = {
            frame_labels[index]
            for index in _indices_in_rectangle(
                [foot_rectangle[0], frame_bbox[3] - 1, foot_rectangle[2], frame_bbox[3]],
                frame.width,
                frame.height,
            )
            if frame_labels[index] >= 0
        }
        connected_labels = foot_labels & corridor_labels
        grounded_connected_labels = connected_labels & grounded_labels
        region_failures = []
        if not connected_labels or (
            name == support_region and not grounded_connected_labels
        ):
            failures.append("FOOT_DISCONNECTED")
            region_failures.append("FOOT_DISCONNECTED")

        disconnected_labels = foot_labels - corridor_labels
        new_island_sizes = []
        if previous_rgba is not None:
            previous_alpha = bytes(previous_rgba.getchannel("A").get_flattened_data())
            for label in disconnected_labels:
                points = frame_components[label]
                if not any(previous_alpha[index] for index in points):
                    new_island_sizes.append(len(points))
        largest_new_island = max(new_island_sizes, default=0)
        if largest_new_island > manifest["qa"]["new_foot_island_area_px_max"]:
            failures.append("NEW_FOOT_ISLAND")
            region_failures.append("NEW_FOOT_ISLAND")

        anchor = (
            _foot_anchor(
                frame_rgba,
                frame_labels,
                grounded_connected_labels,
                foot_rectangle,
                frame_bbox,
            )
            if name == support_region
            else None
        )
        if name == support_region:
            support_anchor = anchor
        previous_anchor = None
        if (
            same_support_interval
            and name == support_region
            and previous_labels is not None
            and previous_bbox is not None
        ):
            previous_foot_rectangle = _resolve_ratio_rectangle(
                previous_bbox, specification["bbox_ratio"]
            )
            previous_corridor_rectangle = _resolve_ratio_rectangle(
                previous_bbox, specification["leg_corridor_ratio"]
            )
            previous_corridor_labels = _component_labels_in_rectangle(
                previous_labels,
                previous_corridor_rectangle,
                previous.width,
                previous.height,
            )
            previous_foot_labels = _component_labels_in_rectangle(
                previous_labels,
                previous_foot_rectangle,
                previous.width,
                previous.height,
            )
            previous_grounded_labels = {
                previous_labels[index]
                for index in _indices_in_rectangle(
                    [
                        previous_foot_rectangle[0],
                        previous_bbox[3] - 1,
                        previous_foot_rectangle[2],
                        previous_bbox[3],
                    ],
                    previous.width,
                    previous.height,
                )
                if previous_labels[index] >= 0
            }
            previous_connected = (
                previous_foot_labels
                & previous_corridor_labels
                & previous_grounded_labels
            )
            previous_anchor = _foot_anchor(
                previous_rgba,
                previous_labels,
                previous_connected,
                previous_foot_rectangle,
                previous_bbox,
            )
            if name == support_region:
                previous_support_anchor = previous_anchor
        region_reports[name] = {
            "foot_rectangle": foot_rectangle,
            "leg_corridor_rectangle": corridor_rectangle,
            "anchor": list(anchor) if anchor else None,
            "previous_anchor": list(previous_anchor) if previous_anchor else None,
            "largest_new_island_px": largest_new_island,
            "failures": region_failures,
            "passed": not region_failures,
        }

    if previous is not None and same_support_interval:
        if support_anchor is None or previous_support_anchor is None:
            if "FOOT_DISCONNECTED" not in failures:
                failures.append("FOOT_DISCONNECTED")
        else:
            drift = math.dist(support_anchor, previous_support_anchor)
            measurements["planted_foot_drift_px"] = drift
            if drift > manifest["qa"]["planted_foot_drift_px_max"]:
                failures.append("PLANTED_FOOT_DRIFT")
    return {
        "failures": failures,
        "measurements": measurements,
        "regions": region_reports,
        "anatomical_to_screen_region": dict(ANATOMICAL_TO_SCREEN_REGION),
        "support_foot": support_foot,
        "previous_support_foot": previous_support_foot,
        "support_region": support_region,
    }


def accept_frame(report: dict) -> bool:
    return report.get("state") == "ACCEPTED" and report.get("passed") is True


def evaluate_frame(
    frame: Image.Image,
    master: Image.Image,
    previous: Image.Image | None,
    manifest: dict,
    frame_index: int,
) -> dict:
    manifest = _resolve_runtime_config(manifest)
    checks = {
        "geometry": geometry_checks(frame, master, manifest),
        "identity": identity_checks(frame, master, previous, manifest),
        "palette": palette_checks(frame, master, manifest),
        "alpha_components": component_checks(frame, master, manifest, previous),
        "feet": foot_checks(frame, previous, manifest, frame_index),
    }
    if frame_index > 1 and previous is None:
        checks["identity"]["failures"].append("PREVIOUS_ACCEPTED_REQUIRED")
    failures = []
    for group in checks.values():
        for code in group["failures"]:
            if code not in failures:
                failures.append(code)
    passed = not failures
    return {
        "frame": frame_index,
        "checks": checks,
        "failures": failures,
        "passed": passed,
        "state": "ACCEPTED" if passed else "REJECTED",
    }


def _normalization_failure_evaluation(frame_index: int, reason: str) -> dict:
    checks = {
        group: {"failures": [reason]}
        for group in ("geometry", "identity", "palette", "alpha_components", "feet")
    }
    return {
        "frame": frame_index,
        "checks": checks,
        "failures": [reason],
        "passed": False,
        "state": "REJECTED",
    }


def _difference_score(left: Image.Image, right: Image.Image) -> int:
    difference = ImageChops.difference(left.convert("RGBA"), right.convert("RGBA"))
    return sum(
        1
        for pixel in difference.get_flattened_data()
        if pixel != (0, 0, 0, 0)
    )


def evaluate_sequence(frames: list[Image.Image]) -> dict:
    failures = []
    if len(frames) != 8:
        failures.append("FRAME_COUNT_MISMATCH")
    nonempty = [frame.convert("RGBA").getchannel("A").getbbox() is not None for frame in frames]
    if not all(nonempty):
        failures.append("EMPTY_SEQUENCE_FRAME")
    hashes = [hashlib.sha256(frame.convert("RGBA").tobytes()).hexdigest() for frame in frames]
    if len(frames) != 8 or len(set(hashes)) != 8:
        failures.append("DUPLICATE_FRAME_HASH")

    compatible = bool(frames) and all(
        frame.size == frames[0].size for frame in frames
    )
    consecutive = []
    closing = None
    if not compatible:
        failures.append("SEQUENCE_FRAME_MISMATCH")
    elif len(frames) >= 2:
        consecutive = [
            _difference_score(left, right)
            for left, right in zip(frames, frames[1:])
        ]
        closing = _difference_score(frames[-1], frames[0])
        if closing > max(consecutive):
            failures.append("LOOP_CLOSURE")
    passed = not failures
    return {
        "checks": {
            "frame_count": len(frames),
            "nonempty": nonempty,
            "hashes": hashes,
            "distinct_hashes": len(set(hashes)),
            "consecutive_differences": consecutive,
            "closing_difference": closing,
        },
        "failures": failures,
        "passed": passed,
        "state": "DRAFT" if passed else "TECHNICAL_REJECTED",
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _run_destination(run_dir: Path, relative: str) -> Path:
    destination = (run_dir / relative).resolve()
    if not _is_relative_to(destination, run_dir):
        raise PipelineBlocked("OUTPUT_OUTSIDE_RUN")
    return destination


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_path.replace(path)


def _frame_record(run_manifest: dict, frame_index: int) -> dict | None:
    frames = run_manifest.get("frames", [])
    if not isinstance(frames, list):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    matches = [
        frame
        for frame in frames
        if isinstance(frame, Mapping) and frame.get("frame_index") == frame_index
    ]
    if len(matches) > 1:
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    return matches[0] if matches else None


def _first_unaccepted_frame(run_manifest: dict, frame_count: int) -> int | None:
    found_gap = False
    first_gap = None
    for frame_index in range(1, frame_count + 1):
        frame = _frame_record(run_manifest, frame_index)
        accepted = frame is not None and frame.get("state") == "ACCEPTED"
        if not accepted and first_gap is None:
            found_gap = True
            first_gap = frame_index
        elif accepted and found_gap:
            raise PipelineBlocked("INVALID_FRAME_CHAIN")
    return first_gap


def _last_accepted_before(run_manifest: dict, frame_index: int) -> dict | None:
    if frame_index <= 1:
        return None
    previous = _frame_record(run_manifest, frame_index - 1)
    if previous is None or previous.get("state") != "ACCEPTED":
        return None
    return previous


def _run_dir_from_manifest(run_manifest: dict, manifest: dict) -> Path:
    value = run_manifest.get("run_dir")
    if not isinstance(value, str) or not value:
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    run_dir = Path(value).resolve()
    return _require_prepared_run(manifest, run_dir)


def _confined_reference(
    run_dir: Path,
    value: str,
    required_parent: str | None = None,
) -> tuple[Path, str]:
    if not isinstance(value, str) or not value:
        raise PipelineBlocked("INVALID_REFERENCE_PATH")
    candidate = Path(value)
    destination = (
        candidate.resolve()
        if candidate.is_absolute()
        else (run_dir / candidate).resolve()
    )
    if not _is_relative_to(destination, run_dir):
        raise PipelineBlocked("REFERENCE_OUTSIDE_RUN")
    if required_parent is not None:
        parent = (run_dir / required_parent).resolve()
        if not _is_relative_to(destination, parent):
            raise PipelineBlocked("REFERENCE_OUTSIDE_RUN")
    if not destination.is_file():
        raise PipelineBlocked("REFERENCE_NOT_FOUND")
    relative = destination.relative_to(run_dir).as_posix()
    return destination, relative


def _png_data_uri(path: Path) -> str:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise PipelineBlocked("REFERENCE_NOT_PNG")
            image.verify()
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as error:
        raise PipelineBlocked("REFERENCE_NOT_PNG") from error
    return f"data:image/png;base64,{encoded}"


def _evaluated_raw_paths(frame: dict | None) -> set[str]:
    if frame is None:
        return set()
    attempts = frame.get("attempts", [])
    if not isinstance(attempts, list):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    return {
        attempt["raw_path"]
        for attempt in attempts
        if isinstance(attempt, Mapping) and isinstance(attempt.get("raw_path"), str)
    }


def _request_model(run_manifest: dict, manifest: dict, frame_index: int) -> str | None:
    requests = run_manifest.get("requests", [])
    if not isinstance(requests, list):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    frame_requests = [
        request
        for request in requests
        if isinstance(request, Mapping) and request.get("frame_index") == frame_index
    ]
    frame = _frame_record(run_manifest, frame_index)
    evaluated = _evaluated_raw_paths(frame)
    for request in reversed(frame_requests):
        status = request.get("status")
        result_path = request.get("result_path")
        if status in ACTIVE_REQUEST_STATUSES or (
            status == "COMPLETED" and result_path is None
        ):
            return request.get("model")
        if status == "COMPLETED" and isinstance(result_path, str):
            normalized_result = Path(result_path).as_posix()
            if normalized_result not in evaluated:
                return None

    primary = manifest["primary_model"]
    fallback = manifest["fallback_model"]
    primary_by_frame = {}
    for request in requests:
        if not isinstance(request, Mapping):
            raise PipelineBlocked("INVALID_RUN_MANIFEST")
        if request.get("model") == primary:
            index = request.get("frame_index")
            primary_by_frame[index] = primary_by_frame.get(index, 0) + 1
    extra_primary = sum(max(0, count - 1) for count in primary_by_frame.values())
    fallback_count = sum(1 for request in requests if request.get("model") == fallback)
    frame_primary = sum(1 for request in frame_requests if request.get("model") == primary)
    frame_fallback = sum(1 for request in frame_requests if request.get("model") == fallback)

    if frame_primary == 0:
        return primary
    if (
        frame_primary == 1
        and extra_primary < 2
        and sum(1 for request in requests if request.get("model") == primary)
        < manifest["max_primary_submits"]
    ):
        return primary
    if frame_fallback == 0 and fallback_count < manifest["max_fallback_submits"]:
        return fallback
    return ""


def _request_prompt(
    reference_roles: list[str], frame_index: int, chroma_rgb: list[int]
) -> str:
    if (
        not isinstance(chroma_rgb, list)
        or len(chroma_rgb) != 3
        or any(type(channel) is not int or not 0 <= channel <= 255 for channel in chroma_rgb)
    ):
        raise PipelineBlocked("INVALID_CHROMA_RGB")
    red, green, blue = chroma_rgb
    chroma_hex = f"#{red:02X}{green:02X}{blue:02X}"
    role_text = "; ".join(
        f"reference {index} ({role})"
        for index, role in enumerate(reference_roles, start=1)
    )
    prompt = (
        f"Create ogre South walk frame {frame_index}. Ordered roles: {role_text}. "
        "Preserve the master's identity, proportions, palette, lighting, face, chains, "
        "bracers, belt and loincloth. Follow only the target pose; when present, use "
        "previous_accepted for local continuity. Keep one complete body, fixed bottom-center "
        f"root, stable South orientation and solid uniform chroma background "
        f"RGB({red}, {green}, {blue})/{chroma_hex}, no gradient."
    )
    if len(prompt) >= 800:
        raise PipelineBlocked("PROMPT_TOO_LONG")
    return prompt


def _build_request(
    run_manifest: dict,
    manifest: dict,
    frame_index: int,
    model: str,
    reference_paths: list[str],
    reference_roles: list[str],
) -> dict:
    run_dir = _run_dir_from_manifest(run_manifest, manifest)
    resolved = [_confined_reference(run_dir, value) for value in reference_paths]
    normalized_paths = [relative for _, relative in resolved]
    image_urls = [_png_data_uri(path) for path, _ in resolved]
    seeds = run_manifest.get("frame_seeds")
    if (
        not isinstance(seeds, list)
        or len(seeds) != manifest["frame_count"]
        or any(type(seed) is not int or seed < 0 for seed in seeds)
    ):
        raise PipelineBlocked("INVALID_FRAME_SEEDS")
    request_input = {
        "image_urls": image_urls,
        "image_size": {"width": 512, "height": 832},
        "num_images": 1,
        "output_format": "png",
        "enable_prompt_expansion": False,
        "seed": seeds[frame_index - 1],
        "prompt": _request_prompt(
            reference_roles, frame_index, run_manifest.get("chroma_rgb")
        ),
        "negative_prompt": (
            "duplicate or missing limbs, identity drift, changed clothing, changed face, "
            "orientation drift, complex background, text, watermark, cropped body"
        ),
    }
    if model == manifest["fallback_model"]:
        request_input["resolution"] = "0.5K"
    return {
        "frame_index": frame_index,
        "model": model,
        "input": request_input,
        "reference_paths": normalized_paths,
        "reference_roles": list(reference_roles),
    }


def next_request(run_manifest: dict, manifest: dict) -> dict | None:
    if not isinstance(run_manifest, dict) or not isinstance(manifest, dict):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    if run_manifest.get("state") in TERMINAL_RUN_STATES:
        return None
    frame_index = _first_unaccepted_frame(run_manifest, manifest["frame_count"])
    if frame_index is None:
        return None
    previous = _last_accepted_before(run_manifest, frame_index)
    if frame_index > 1 and previous is None:
        raise PipelineBlocked("NO_ACCEPTED_PREDECESSOR")

    model = _request_model(run_manifest, manifest, frame_index)
    if model is None:
        return None
    if model == "":
        run_manifest["state"] = "TECHNICAL_REJECTED"
        run_manifest["terminal_reason"] = "ATTEMPT_POLICY_EXHAUSTED"
        return None

    technical_master = run_manifest.get("technical_master")
    pose_guides = run_manifest.get("pose_guides")
    if not isinstance(technical_master, str) or not isinstance(pose_guides, list):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    if len(pose_guides) != manifest["frame_count"]:
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    references = [technical_master]
    roles = ["master"]
    if previous is not None:
        technical_path = previous.get("technical_path")
        if not isinstance(technical_path, str):
            raise PipelineBlocked("INVALID_ACCEPTED_FRAME")
        references.append(technical_path)
        roles.append("previous_accepted")
    references.append(pose_guides[frame_index - 1])
    roles.append("pose")
    return _build_request(
        run_manifest, manifest, frame_index, model, references, roles
    )


def _save_image_atomic(image: Image.Image, destination: Path, format_name: str) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    image.save(temporary, format=format_name)
    temporary.replace(destination)


def _flatten_on_chroma(image: Image.Image, chroma: tuple[int, int, int]) -> Image.Image:
    rgba = image.convert("RGBA")
    flattened = Image.new("RGB", rgba.size, chroma)
    flattened.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))
    return flattened


def _frame_summary(report: dict) -> dict:
    summary = {
        "frame_index": report["frame"],
        "state": report["state"],
        "passed": report["passed"],
        "qa_report_path": f"reports/frame-{report['frame']:02d}-qa.json",
        "attempts": [
            {
                "request_id": attempt["request_id"],
                "attempt": attempt["attempt"],
                "raw_path": attempt["raw_path"],
                "raw_sha256": attempt["raw_sha256"],
                "model": attempt.get("model"),
                "state": attempt["evaluation"]["state"],
                "failures": attempt["evaluation"]["failures"],
            }
            for attempt in report["attempts"]
        ],
        "provenance": report["provenance"],
    }
    if report["state"] == "ACCEPTED":
        summary["normalized_path"] = f"normalized/frames/{report['frame']:02d}.png"
        summary["technical_path"] = (
            f"normalized/frames/{report['frame']:02d}-technical.png"
        )
    return summary


def _sync_frame_report(run_manifest: dict, report: dict) -> None:
    summary = _frame_summary(report)
    frames = [
        frame
        for frame in run_manifest.get("frames", [])
        if frame.get("frame_index") != report["frame"]
    ]
    frames.append(summary)
    frames.sort(key=lambda frame: frame["frame_index"])
    run_manifest["frames"] = frames
    accepted = [
        frame["frame_index"]
        for frame in frames
        if frame.get("state") == "ACCEPTED"
    ]
    run_manifest["last_accepted_frame"] = max(accepted, default=0)
    run_manifest["state"] = "DRAFT"
    run_manifest.pop("terminal_reason", None)


def _frame_report_is_recorded(run_manifest: dict, report: dict) -> bool:
    return _frame_record(run_manifest, report["frame"]) == _frame_summary(report)


def _completed_evaluation_request(
    run_manifest: dict,
    manifest: dict,
    frame_index: int,
    raw_relative: str,
    raw_sha256: str,
) -> dict:
    requests = run_manifest.get("requests")
    if not isinstance(requests, list):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    matching = [
        request
        for request in requests
        if isinstance(request, Mapping)
        and request.get("frame_index") == frame_index
        and isinstance(request.get("result_path"), str)
        and Path(request["result_path"]).as_posix() == raw_relative
    ]
    if not matching:
        raise PipelineBlocked("EVALUATION_REQUEST_NOT_FOUND")
    if len(matching) != 1:
        raise PipelineBlocked("EVALUATION_REQUEST_INVALID")
    request = matching[0]
    if request.get("model") not in {
        manifest["primary_model"],
        manifest["fallback_model"],
    }:
        raise PipelineBlocked("EVALUATION_REQUEST_INVALID")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise PipelineBlocked("EVALUATION_REQUEST_INVALID")
    if request.get("status") != "COMPLETED":
        raise PipelineBlocked("EVALUATION_REQUEST_NOT_COMPLETED")
    request_fingerprint = request.get("request_fingerprint")
    current_handoff = run_manifest.get("next_request")
    if (
        not _valid_sha256(request.get("result_sha256"))
        or request["result_sha256"] != raw_sha256
        or not _valid_sha256(request_fingerprint)
        or not isinstance(current_handoff, Mapping)
        or dict(current_handoff)
        != {
            "fingerprint": request_fingerprint,
            "frame_index": frame_index,
            "model": request["model"],
        }
        or type(request.get("attempt")) is not int
        or request["attempt"] < 1
    ):
        raise PipelineBlocked("EVALUATION_REQUEST_INVALID")
    return dict(request)


def record_evaluation(run_dir: Path, frame_index: int, raw_path: Path) -> dict:
    run_dir = Path(run_dir).resolve()
    run_manifest_path = _run_destination(run_dir, "reports/run_manifest.json")
    try:
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("INVALID_RUN_MANIFEST") from error
    manifest = run_manifest.get("normalized_input")
    if not isinstance(manifest, dict):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    if run_manifest.get("run_dir") != str(run_dir):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    _require_prepared_run(manifest, run_dir)
    if type(frame_index) is not int or not 1 <= frame_index <= manifest["frame_count"]:
        raise PipelineBlocked("INVALID_FRAME_INDEX")
    raw, raw_relative = _confined_reference(
        run_dir, str(raw_path), f"raw/frame-{frame_index:02d}"
    )
    raw_sha256 = _sha256_file(raw)
    request = _completed_evaluation_request(
        run_manifest, manifest, frame_index, raw_relative, raw_sha256
    )
    raw_dimensions = _validated_raw_dimensions(
        raw, run_manifest, request["model"]
    )
    report_path = _run_destination(run_dir, f"reports/frame-{frame_index:02d}-qa.json")
    existing = None
    if report_path.is_file():
        try:
            existing = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PipelineBlocked("INVALID_FRAME_REPORT") from error
        for attempt in existing.get("attempts", []):
            if attempt.get("raw_path") == raw_relative:
                if _frame_report_is_recorded(run_manifest, existing):
                    return existing
                if run_manifest.get("state") in TERMINAL_RUN_STATES:
                    raise PipelineBlocked("TERMINAL_RUN_REPORT_MISMATCH")
                _sync_frame_report(run_manifest, existing)
                _write_json_atomic(run_manifest_path, run_manifest)
                return existing
        existing_frame = _frame_record(run_manifest, frame_index)
        if (
            run_manifest.get("state") in TERMINAL_RUN_STATES
            or (existing_frame is not None and existing_frame.get("state") == "ACCEPTED")
        ):
            raise PipelineBlocked("FRAME_EVALUATION_CONFLICT")
    expected_frame = _first_unaccepted_frame(run_manifest, manifest["frame_count"])
    if expected_frame != frame_index:
        raise PipelineBlocked("FRAME_OUT_OF_ORDER")

    try:
        normalized = normalize_frame(raw, run_manifest, request["model"])
    except PipelineBlocked as error:
        reason = str(error)
        if reason not in CONTENT_NORMALIZATION_FAILURES:
            raise
        evaluation = _normalization_failure_evaluation(frame_index, reason)
        provenance = {
            "request_id": request["request_id"],
            "request_fingerprint": request["request_fingerprint"],
            "attempt": request["attempt"],
            "model": request["model"],
            "raw": {
                "path": raw_relative,
                "sha256": raw_sha256,
                "dimensions": list(raw_dimensions),
            },
            "normalized": {
                "path": None,
                "sha256": None,
                "dimensions": list(NORMALIZED_SIZE),
            },
            "technical": {
                "path": None,
                "sha256": None,
                "dimensions": list(TECHNICAL_SIZE),
            },
        }
        attempts = list(existing.get("attempts", [])) if existing else []
        attempts.append(
            {
                "request_id": request["request_id"],
                "request_fingerprint": request["request_fingerprint"],
                "attempt": request["attempt"],
                "raw_path": raw_relative,
                "raw_sha256": raw_sha256,
                "result_sha256": request["result_sha256"],
                "model": request["model"],
                "evaluation": evaluation,
                "provenance": provenance,
            }
        )
        report = {
            **evaluation,
            "raw_path": raw_relative,
            "model": request["model"],
            "attempts": attempts,
            "provenance": provenance,
        }
        _write_json_atomic(report_path, report)
        _sync_frame_report(run_manifest, report)
        _write_json_atomic(run_manifest_path, run_manifest)
        return report
    master_path, _ = _confined_reference(
        run_dir, run_manifest["technical_master"], "input/master"
    )
    master = normalize_frame(master_path, run_manifest)
    previous_record = _last_accepted_before(run_manifest, frame_index)
    previous = None
    if previous_record is not None:
        previous_path, _ = _confined_reference(
            run_dir, previous_record["normalized_path"], "normalized/frames"
        )
        with Image.open(previous_path) as loaded:
            previous = loaded.convert("RGBA")
    evaluation = evaluate_frame(
        normalized, master, previous, run_manifest, frame_index
    )
    technical = _flatten_on_chroma(
        normalized.resize(TECHNICAL_SIZE, Image.Resampling.NEAREST),
        tuple(run_manifest["chroma_rgb"]),
    )
    normalized_path = _run_destination(
        run_dir, f"normalized/frames/{frame_index:02d}.png"
    )
    technical_path = _run_destination(
        run_dir, f"normalized/frames/{frame_index:02d}-technical.png"
    )
    normalized_relative = normalized_path.relative_to(run_dir).as_posix()
    technical_relative = technical_path.relative_to(run_dir).as_posix()
    if accept_frame(evaluation):
        _save_image_atomic(normalized, normalized_path, "PNG")
        _save_image_atomic(technical, technical_path, "PNG")
        normalized_sha256 = _sha256_file(normalized_path)
        technical_sha256 = _sha256_file(technical_path)
    else:
        normalized_sha256 = _image_png_sha256(normalized)
        technical_sha256 = _image_png_sha256(technical)
    provenance = {
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "attempt": request["attempt"],
        "model": request["model"],
        "raw": {
            "path": raw_relative,
            "sha256": raw_sha256,
            "dimensions": list(raw_dimensions),
        },
        "normalized": {
            "path": normalized_relative if accept_frame(evaluation) else None,
            "sha256": normalized_sha256,
            "dimensions": list(NORMALIZED_SIZE),
        },
        "technical": {
            "path": technical_relative if accept_frame(evaluation) else None,
            "sha256": technical_sha256,
            "dimensions": list(TECHNICAL_SIZE),
        },
    }
    attempts = list(existing.get("attempts", [])) if existing else []
    attempts.append(
        {
            "request_id": request["request_id"],
            "request_fingerprint": request["request_fingerprint"],
            "attempt": request["attempt"],
            "raw_path": raw_relative,
            "raw_sha256": raw_sha256,
            "result_sha256": request["result_sha256"],
            "model": request["model"],
            "evaluation": evaluation,
            "provenance": provenance,
        }
    )
    report = {
        **evaluation,
        "raw_path": raw_relative,
        "model": request["model"],
        "attempts": attempts,
        "provenance": provenance,
    }
    _write_json_atomic(report_path, report)
    _sync_frame_report(run_manifest, report)
    _write_json_atomic(run_manifest_path, run_manifest)
    return report


def _write_global_reports(
    run_dir: Path,
    run_manifest: dict,
    sequence: dict,
    state: str,
    failures: list[str],
    artifacts: dict | None = None,
) -> dict:
    consistency = {
        **sequence,
        "state": "DRAFT" if sequence.get("passed") else "TECHNICAL_REJECTED",
    }
    _write_json_atomic(
        _run_destination(run_dir, "reports/consistency_report.json"), consistency
    )
    report = {
        "state": state,
        "passed": state == "READY_FOR_VISUAL_REVIEW",
        "failures": failures,
        "sequence": sequence,
        "artifacts": artifacts or {},
    }
    _write_json_atomic(_run_destination(run_dir, "reports/qa_report.json"), report)
    run_manifest["state"] = state
    if state == "TECHNICAL_REJECTED":
        run_manifest["terminal_reason"] = failures[0] if failures else "SEQUENCE_QA_FAILED"
    else:
        run_manifest.pop("terminal_reason", None)
        run_manifest["artifacts"] = artifacts or {}
    _write_json_atomic(
        _run_destination(run_dir, "reports/run_manifest.json"), run_manifest
    )
    return report


def _save_contact_sheet(frames: list[Image.Image], destination: Path) -> None:
    contact = Image.new("RGBA", (1024, 880), (24, 24, 24, 255))
    draw = ImageDraw.Draw(contact)
    for offset, frame in enumerate(frames):
        column = offset % 4
        row = offset // 4
        x = column * 256
        y = row * 440
        draw.text((x + 8, y + 5), f"Frame {offset + 1:02d}", fill=(255, 255, 255, 255))
        contact.alpha_composite(frame.convert("RGBA"), (x, y + 24))
    _save_image_atomic(contact, destination, "PNG")


def _save_loop_gif(frames: list[Image.Image], destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    converted = [frame.convert("RGBA") for frame in frames]
    converted[0].save(
        temporary,
        format="GIF",
        save_all=True,
        append_images=converted[1:],
        duration=125,
        loop=0,
        disposal=2,
        optimize=False,
    )
    temporary.replace(destination)


def _provenance_request(
    run_manifest: dict,
    provenance: dict,
    frame_index: int,
) -> dict:
    requests = run_manifest.get("requests")
    if not isinstance(requests, list):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    request_fingerprint = provenance.get("request_fingerprint")
    if not _valid_sha256(request_fingerprint):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    matching = [
        request
        for request in requests
        if isinstance(request, Mapping)
        and request.get("frame_index") == frame_index
        and request.get("request_id") == provenance.get("request_id")
        and request.get("attempt") == provenance.get("attempt")
        and request.get("model") == provenance.get("model")
        and request.get("request_fingerprint") == request_fingerprint
        and request.get("result_path") == provenance.get("raw", {}).get("path")
        and request.get("result_sha256") == provenance.get("raw", {}).get("sha256")
        and request.get("status") == "COMPLETED"
    ]
    if len(matching) != 1:
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    return matching[0]


def _checked_provenance_image(
    run_dir: Path,
    artifact: dict,
    expected_relative: str,
    required_parent: str,
    expected_size: tuple[int, int],
    mode: str,
) -> Image.Image:
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("path") != expected_relative
        or artifact.get("dimensions") != list(expected_size)
        or not _valid_sha256(artifact.get("sha256"))
    ):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    path, _ = _confined_reference(run_dir, expected_relative, required_parent)
    if _sha256_file(path) != artifact["sha256"]:
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    try:
        with Image.open(path) as loaded:
            if loaded.size != expected_size:
                raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
            return loaded.convert(mode)
    except OSError as error:
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID") from error


def _validate_accepted_frame_report(
    run_dir: Path,
    run_manifest: dict,
    frame_record: dict,
    frame_report: dict,
    master: Image.Image,
    previous: Image.Image | None,
) -> Image.Image:
    frame_index = frame_record.get("frame_index")
    checks = frame_report.get("checks")
    attempts = frame_report.get("attempts")
    provenance = frame_report.get("provenance")
    if (
        frame_report.get("frame") != frame_index
        or frame_report.get("state") != "ACCEPTED"
        or frame_report.get("passed") is not True
        or frame_report.get("failures") != []
        or not isinstance(checks, Mapping)
        or set(checks) != QA_CHECK_GROUPS
        or not all(
            isinstance(group, Mapping) and isinstance(group.get("failures"), list)
            for group in checks.values()
        )
        or not isinstance(attempts, list)
        or not attempts
        or not isinstance(provenance, Mapping)
        or attempts[-1].get("provenance") != provenance
        or frame_record != _frame_summary(frame_report)
    ):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    if any(group["failures"] for group in checks.values()):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")

    for attempt in attempts:
        attempt_provenance = attempt.get("provenance")
        if (
            not isinstance(attempt_provenance, Mapping)
            or not _valid_sha256(attempt.get("request_fingerprint"))
            or attempt.get("request_fingerprint")
            != attempt_provenance.get("request_fingerprint")
            or attempt.get("request_id") != attempt_provenance.get("request_id")
            or attempt.get("attempt") != attempt_provenance.get("attempt")
            or attempt.get("model") != attempt_provenance.get("model")
            or attempt.get("raw_path") != attempt_provenance.get("raw", {}).get("path")
            or attempt.get("raw_sha256") != attempt_provenance.get("raw", {}).get("sha256")
            or attempt.get("result_sha256") != attempt.get("raw_sha256")
            or not isinstance(attempt.get("evaluation"), Mapping)
        ):
            raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
        _provenance_request(run_manifest, attempt_provenance, frame_index)

    provenance_request = _provenance_request(
        run_manifest, provenance, frame_index
    )
    raw_relative = f"raw/frame-{frame_index:02d}"
    raw_artifact = provenance.get("raw")
    if (
        not isinstance(raw_artifact, Mapping)
        or not isinstance(raw_artifact.get("dimensions"), list)
        or not _valid_sha256(raw_artifact.get("sha256"))
    ):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    raw_path, raw_path_relative = _confined_reference(
        run_dir, raw_artifact.get("path"), raw_relative
    )
    if raw_path_relative != raw_artifact["path"] or _sha256_file(raw_path) != raw_artifact["sha256"]:
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    raw_dimensions = _validated_raw_dimensions(
        raw_path, run_manifest, provenance_request["model"]
    )
    if raw_artifact["dimensions"] != list(raw_dimensions):
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")

    recomputed = normalize_frame(
        raw_path, run_manifest, provenance_request["model"]
    )
    normalized_relative = f"normalized/frames/{frame_index:02d}.png"
    normalized = _checked_provenance_image(
        run_dir,
        provenance.get("normalized"),
        normalized_relative,
        "normalized/frames",
        NORMALIZED_SIZE,
        "RGBA",
    )
    if normalized.tobytes() != recomputed.tobytes():
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")

    technical_relative = f"normalized/frames/{frame_index:02d}-technical.png"
    technical = _checked_provenance_image(
        run_dir,
        provenance.get("technical"),
        technical_relative,
        "normalized/frames",
        TECHNICAL_SIZE,
        "RGB",
    )
    expected_technical = _flatten_on_chroma(
        recomputed.resize(TECHNICAL_SIZE, Image.Resampling.NEAREST),
        tuple(run_manifest["chroma_rgb"]),
    )
    if technical.tobytes() != expected_technical.tobytes():
        raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")

    reevaluated = evaluate_frame(
        recomputed, master, previous, run_manifest, frame_index
    )
    for key in ("checks", "failures", "passed", "state"):
        if frame_report.get(key) != reevaluated[key]:
            raise PipelineBlocked("ACCEPTED_FRAME_REPORT_INVALID")
    return recomputed


def _ready_report(run_dir: Path, run_manifest: dict, sequence: dict) -> dict:
    try:
        report = json.loads(
            _run_destination(run_dir, "reports/qa_report.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("READY_PROVENANCE_INVALID") from error
    expected = {
        "sheet": ("normalized/Ogre_Walk_S_8f_256x416.png", (2048, 416)),
        "contact_sheet": ("previews/contact_sheet.png", (1024, 880)),
        "loop_gif": ("previews/loop.gif", NORMALIZED_SIZE),
    }
    if (
        report.get("state") != "READY_FOR_VISUAL_REVIEW"
        or report.get("passed") is not True
        or report.get("failures") != []
        or report.get("sequence") != sequence
        or report.get("artifacts") != run_manifest.get("artifacts")
        or set(report.get("artifacts", {})) != set(expected)
    ):
        raise PipelineBlocked("READY_PROVENANCE_INVALID")
    for name, (relative, size) in expected.items():
        artifact = report["artifacts"][name]
        try:
            _checked_provenance_image(
                run_dir,
                artifact,
                relative,
                str(Path(relative).parent),
                size,
                "RGBA",
            )
        except PipelineBlocked as error:
            raise PipelineBlocked("READY_PROVENANCE_INVALID") from error
    return report


def assemble_outputs(run_dir: Path, manifest: dict) -> dict:
    run_dir = _require_prepared_run(manifest, Path(run_dir).resolve())
    run_manifest_path = _run_destination(run_dir, "reports/run_manifest.json")
    try:
        run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("INVALID_RUN_MANIFEST") from error
    was_ready = run_manifest.get("state") == "READY_FOR_VISUAL_REVIEW"

    accepted = [
        frame
        for frame in run_manifest.get("frames", [])
        if frame.get("state") == "ACCEPTED" and frame.get("passed") is True
    ]
    accepted.sort(key=lambda frame: frame["frame_index"])
    failures = []
    expected_indices = list(range(1, manifest["frame_count"] + 1))
    if [frame.get("frame_index") for frame in accepted] != expected_indices:
        failures.append("ACCEPTED_FRAME_COUNT_MISMATCH")
    frames = []
    previous = None
    try:
        master_path, _ = _confined_reference(
            run_dir, run_manifest["technical_master"], "input/master"
        )
        master = normalize_frame(master_path, run_manifest)
    except (KeyError, OSError, PipelineBlocked) as error:
        if was_ready:
            raise PipelineBlocked("READY_PROVENANCE_INVALID") from error
        master = None
        failures.append("ACCEPTED_FRAME_REPORT_INVALID")
    for frame in accepted:
        if master is None:
            break
        frame_index = frame.get("frame_index")
        expected_report = f"reports/frame-{frame_index:02d}-qa.json"
        if frame.get("qa_report_path") != expected_report:
            failures.append("ACCEPTED_FRAME_REPORT_MISSING")
            break
        try:
            report_path, _ = _confined_reference(run_dir, expected_report, "reports")
            frame_report = json.loads(report_path.read_text(encoding="utf-8"))
            normalized = _validate_accepted_frame_report(
                run_dir,
                run_manifest,
                frame,
                frame_report,
                master,
                previous,
            )
            frames.append(normalized)
            previous = normalized
        except (KeyError, OSError, json.JSONDecodeError, PipelineBlocked):
            if was_ready:
                raise PipelineBlocked("READY_PROVENANCE_INVALID")
            failure = (
                "ACCEPTED_FRAME_REPORT_MISSING"
                if not (run_dir / expected_report).is_file()
                else "ACCEPTED_FRAME_REPORT_INVALID"
            )
            failures.append(failure)
            break
    sequence = evaluate_sequence(frames)
    for failure in sequence["failures"]:
        if failure not in failures:
            failures.append(failure)
    if was_ready:
        if failures:
            raise PipelineBlocked("READY_PROVENANCE_INVALID")
        return _ready_report(run_dir, run_manifest, sequence)
    if failures:
        return _write_global_reports(
            run_dir,
            run_manifest,
            sequence,
            "TECHNICAL_REJECTED",
            failures,
        )

    sheet_path = _run_destination(
        run_dir, "normalized/Ogre_Walk_S_8f_256x416.png"
    )
    sheet = Image.new("RGBA", (2048, 416), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, (index * 256, 0))
    _save_image_atomic(sheet, sheet_path, "PNG")
    contact_path = _run_destination(run_dir, "previews/contact_sheet.png")
    gif_path = _run_destination(run_dir, "previews/loop.gif")
    _save_contact_sheet(frames, contact_path)
    _save_loop_gif(frames, gif_path)
    artifacts = {}
    for name, path in (
        ("sheet", sheet_path),
        ("contact_sheet", contact_path),
        ("loop_gif", gif_path),
    ):
        with Image.open(path) as artifact_image:
            dimensions = list(artifact_image.size)
        artifacts[name] = {
            "path": path.relative_to(run_dir).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dimensions": dimensions,
        }
    return _write_global_reports(
        run_dir,
        run_manifest,
        sequence,
        "READY_FOR_VISUAL_REVIEW",
        [],
        artifacts,
    )


def _prepared_marker_value(project: Path, workspace_root: Path) -> dict:
    return {
        "format": "tower-sprite-pipeline-prepared-run-v1",
        "project_root": str(project),
        "workspace_root": str(workspace_root),
        "state": "PREPARING",
    }


def _require_prepared_run(manifest: dict, run_dir: Path) -> Path:
    project = Path(manifest["project_root"]).resolve()
    prepared_run = Path(run_dir).resolve()
    if prepared_run == project or _is_relative_to(prepared_run, project):
        raise PipelineBlocked("WORKSPACE_INSIDE_PROJECT")

    marker_path = _run_destination(prepared_run, PREPARED_RUN_MARKER)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("RUN_NOT_PREPARED") from error
    if marker != _prepared_marker_value(project, prepared_run.parent.resolve()):
        raise PipelineBlocked("RUN_NOT_PREPARED")
    for relative in RUN_DIRS:
        if not _run_destination(prepared_run, relative).is_dir():
            raise PipelineBlocked("RUN_NOT_PREPARED")
    return prepared_run


def _is_temporary_run_directory(path: Path, root: Path, run_id: str) -> bool:
    resolved_path = path.resolve()
    return (
        resolved_path.parent == root.resolve()
        and resolved_path.name.startswith(f".{run_id}.preparing-")
        and _is_relative_to(resolved_path, root)
    )


def _resolve_source(manifest: dict) -> tuple[Path, Path]:
    project = Path(manifest["project_root"]).resolve()
    source = (project / manifest["source_path"]).resolve()
    if not _is_relative_to(source, project):
        raise PipelineBlocked("SOURCE_OUTSIDE_PROJECT")
    if not source.is_file():
        raise PipelineBlocked("SOURCE_NOT_FOUND")
    return project, source


def _normalize_master(source: Path) -> tuple[Image.Image, dict]:
    with Image.open(source) as loaded:
        source_rgba = loaded.convert("RGBA")

    alpha_bbox = source_rgba.getchannel("A").getbbox()
    if alpha_bbox is None:
        raise PipelineBlocked("SOURCE_WITHOUT_ALPHA")

    subject = source_rgba.crop(alpha_bbox)
    if subject.width > NORMALIZED_SIZE[0] or subject.height > NORMALIZED_SIZE[1] - 1:
        raise PipelineBlocked("SOURCE_DOES_NOT_FIT_NORMALIZED_MASTER")

    master = Image.new("RGBA", NORMALIZED_SIZE, (0, 0, 0, 0))
    offset = (
        (NORMALIZED_SIZE[0] - subject.width) // 2,
        NORMALIZED_SIZE[1] - 1 - subject.height,
    )
    master.alpha_composite(subject, offset)
    normalized_bbox = master.getchannel("A").getbbox()
    return master, {
        "source_size": list(source_rgba.size),
        "source_alpha_bbox": list(alpha_bbox),
        "normalized_size": list(NORMALIZED_SIZE),
        "normalized_alpha_bbox": list(normalized_bbox),
        "subject_offset": list(offset),
        "anchor": "bottom-center",
    }


def choose_chroma(master: Image.Image) -> tuple[int, int, int]:
    quantized = master.convert("RGB").quantize(colors=64).convert("RGB")
    palette = {rgb for _, rgb in quantized.getcolors(maxcolors=256 * 416) or []}
    for candidate in CHROMA_CANDIDATES:
        if candidate not in palette:
            return candidate
    raise PipelineBlocked("NO_SAFE_CHROMA")


def _resolve_ratio_rectangle(alpha_bbox: tuple[int, int, int, int], ratios: list) -> list[int]:
    left, top, right, bottom = alpha_bbox
    width = right - left
    height = bottom - top
    return [
        left + math.floor(width * ratios[0]),
        top + math.floor(height * ratios[1]),
        left + math.ceil(width * ratios[2]),
        top + math.ceil(height * ratios[3]),
    ]


def _resolve_regions(manifest: dict, alpha_bbox: tuple[int, int, int, int]) -> dict:
    identity = {
        name: _resolve_ratio_rectangle(alpha_bbox, specification["bbox_ratio"])
        for name, specification in manifest["identity_regions"].items()
    }
    feet = {
        name: {
            "foot": _resolve_ratio_rectangle(alpha_bbox, specification["bbox_ratio"]),
            "leg_corridor": _resolve_ratio_rectangle(
                alpha_bbox, specification["leg_corridor_ratio"]
            ),
        }
        for name, specification in manifest["foot_regions"].items()
    }
    return {"identity": identity, "feet": feet}


def _point(x: int, y: int) -> list[int]:
    return [int(x), int(y)]


def _derive_skeleton(alpha_bbox: tuple[int, int, int, int]) -> dict:
    left, top, right, bottom = alpha_bbox
    width = right - left
    height = bottom - top
    center_x = (left + right - 1) // 2
    root_y = bottom - 1
    hip_y = top + round(height * 0.58)
    shoulder_y = top + round(height * 0.34)
    elbow_y = top + round(height * 0.51)
    knee_y = top + round(height * 0.78)
    shoulder_offset = round(width * 0.20)
    elbow_offset = round(width * 0.31)
    hip_offset = round(width * 0.13)
    knee_offset = round(width * 0.18)
    foot_offset = round(width * 0.21)
    return {
        "root": _point(center_x, root_y),
        "head": _point(center_x, top + round(height * 0.14)),
        "chest": _point(center_x, top + round(height * 0.36)),
        "hip": _point(center_x, hip_y),
        "screen_left_shoulder": _point(center_x - shoulder_offset, shoulder_y),
        "screen_right_shoulder": _point(center_x + shoulder_offset, shoulder_y),
        "screen_left_elbow": _point(center_x - elbow_offset, elbow_y),
        "screen_right_elbow": _point(center_x + elbow_offset, elbow_y),
        "screen_left_hand": _point(center_x - elbow_offset, top + round(height * 0.66)),
        "screen_right_hand": _point(center_x + elbow_offset, top + round(height * 0.66)),
        "screen_left_hip": _point(center_x - hip_offset, hip_y),
        "screen_right_hip": _point(center_x + hip_offset, hip_y),
        "screen_left_knee": _point(center_x - knee_offset, knee_y),
        "screen_right_knee": _point(center_x + knee_offset, knee_y),
        "screen_left_foot": _point(center_x - foot_offset, root_y),
        "screen_right_foot": _point(center_x + foot_offset, root_y),
    }


def _technical_point(point: list[int]) -> tuple[int, int]:
    return point[0] * 2, point[1] * 2


def _shift(point: list[int], horizontal: int = 0, vertical: int = 0) -> list[int]:
    return [point[0] + horizontal, point[1] + vertical]


def _draw_joint(draw: ImageDraw.ImageDraw, point: list[int], color: tuple[int, int, int]) -> None:
    x, y = _technical_point(point)
    radius = min(5, x - 1, y - 1, TECHNICAL_SIZE[0] - 2 - x, TECHNICAL_SIZE[1] - 2 - y)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)


def _draw_segment(
    draw: ImageDraw.ImageDraw,
    start: list[int],
    end: list[int],
    color: tuple[int, int, int],
) -> None:
    draw.line((_technical_point(start), _technical_point(end)), fill=color, width=4)


def _pose_guide_coordinates(
    skeleton: dict,
    stride: float,
    counter_swing: float,
    body_y: float,
    support_foot: str,
) -> dict[str, list[int]]:
    body_shift = round(body_y * 3)
    arm_shift = round(counter_swing * 14)
    leg_shift = round(stride * 18)

    chest = _shift(skeleton["chest"], vertical=body_shift)
    hip = _shift(skeleton["hip"], vertical=body_shift)
    left_shoulder = _shift(skeleton["screen_left_shoulder"], vertical=body_shift)
    right_shoulder = _shift(skeleton["screen_right_shoulder"], vertical=body_shift)
    left_elbow = _shift(skeleton["screen_left_elbow"], -arm_shift, body_shift)
    right_elbow = _shift(skeleton["screen_right_elbow"], arm_shift, body_shift)
    left_hand = _shift(skeleton["screen_left_hand"], -arm_shift * 2, body_shift)
    right_hand = _shift(skeleton["screen_right_hand"], arm_shift * 2, body_shift)
    left_hip = _shift(skeleton["screen_left_hip"], vertical=body_shift)
    right_hip = _shift(skeleton["screen_right_hip"], vertical=body_shift)
    left_knee = _shift(skeleton["screen_left_knee"], -leg_shift, body_shift)
    right_knee = _shift(skeleton["screen_right_knee"], leg_shift, body_shift)
    support_region = ANATOMICAL_TO_SCREEN_REGION[support_foot]
    left_foot = list(skeleton["screen_left_foot"])
    right_foot = list(skeleton["screen_right_foot"])
    if support_region == "screen_right":
        left_foot = _shift(left_foot, -leg_shift * 2)
    else:
        right_foot = _shift(right_foot, leg_shift * 2)

    return {
        "root": list(skeleton["root"]),
        "head": list(skeleton["head"]),
        "chest": chest,
        "hip": hip,
        "screen_left_shoulder": left_shoulder,
        "screen_right_shoulder": right_shoulder,
        "screen_left_elbow": left_elbow,
        "screen_right_elbow": right_elbow,
        "screen_left_hand": left_hand,
        "screen_right_hand": right_hand,
        "screen_left_hip": left_hip,
        "screen_right_hip": right_hip,
        "screen_left_knee": left_knee,
        "screen_right_knee": right_knee,
        "screen_left_foot": left_foot,
        "screen_right_foot": right_foot,
    }


def _draw_pose_guide(
    path: Path,
    coordinates: dict[str, list[int]],
    chroma: tuple[int, int, int],
    name: str,
) -> None:
    guide = Image.new("RGB", TECHNICAL_SIZE, chroma)
    draw = ImageDraw.Draw(guide)
    segment_color = (25, 25, 25)
    joint_color = (245, 245, 245)
    label_color = (255, 255, 255)

    segments = (
        (coordinates["head"], coordinates["chest"]),
        (coordinates["chest"], coordinates["hip"]),
        (coordinates["screen_left_shoulder"], coordinates["screen_left_elbow"]),
        (coordinates["screen_left_elbow"], coordinates["screen_left_hand"]),
        (coordinates["screen_right_shoulder"], coordinates["screen_right_elbow"]),
        (coordinates["screen_right_elbow"], coordinates["screen_right_hand"]),
        (coordinates["screen_left_hip"], coordinates["screen_left_knee"]),
        (coordinates["screen_left_knee"], coordinates["screen_left_foot"]),
        (coordinates["screen_right_hip"], coordinates["screen_right_knee"]),
        (coordinates["screen_right_knee"], coordinates["screen_right_foot"]),
    )
    joints = tuple(coordinates.values())
    for start, end in segments:
        _draw_segment(draw, start, end, segment_color)
    for point in joints:
        _draw_joint(draw, point, joint_color)
    draw.text((12, 12), name, fill=label_color)
    guide.save(path)


def build_pose_guides(
    manifest: dict, normalized_master: Image.Image, run_dir: Path
) -> list[Path]:
    run_dir = _require_prepared_run(manifest, run_dir)
    if normalized_master.size != NORMALIZED_SIZE:
        raise PipelineBlocked("INVALID_NORMALIZED_MASTER_SIZE")

    alpha_bbox = normalized_master.getchannel("A").getbbox()
    if alpha_bbox is None:
        raise PipelineBlocked("NORMALIZED_MASTER_WITHOUT_ALPHA")

    chroma = choose_chroma(normalized_master)
    skeleton = _derive_skeleton(alpha_bbox)
    regions = _resolve_regions(manifest, alpha_bbox)
    guide_dir = _run_destination(run_dir, "input/pose-guides")
    reports_dir = _run_destination(run_dir, "reports")
    guide_records = []
    for index, (name, stride, counter_swing, body_y) in enumerate(PHASES, start=1):
        path = _run_destination(guide_dir, f"{index:02d}-{name}.png")
        support_foot = manifest["poses"][index - 1]["support_foot"]
        support_region = ANATOMICAL_TO_SCREEN_REGION[support_foot]
        coordinates = _pose_guide_coordinates(
            skeleton, stride, counter_swing, body_y, support_foot
        )
        guide_records.append(
            {
                "name": name,
                "multipliers": [stride, counter_swing, body_y],
                "path": str(path.relative_to(run_dir)),
                "root": coordinates["root"],
                "support_foot": support_foot,
                "support_region": support_region,
                "support_anchor": coordinates[f"{support_region}_foot"],
                "coordinates": coordinates,
            }
        )

    geometry = {
        "chroma_rgb": list(chroma),
        "normalized_master": {
            "size": list(normalized_master.size),
            "alpha_bbox": list(alpha_bbox),
            "technical_size": list(TECHNICAL_SIZE),
        },
        "resolved_regions": regions,
        "skeleton": skeleton,
        "guides": guide_records,
    }
    _write_json_atomic(reports_dir / "pose_geometry.json", geometry)
    for guide in guide_records:
        _draw_pose_guide(
            run_dir / guide["path"], guide["coordinates"], chroma, guide["name"]
        )
    return [run_dir / guide["path"] for guide in guide_records]


def _frame_seed(run_id: str, frame_index: int) -> int:
    digest = hashlib.sha256(f"{run_id}:frame:{frame_index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def prepare_workspace(manifest: dict, workspace_root: Path, run_id: str) -> Path:
    project, source = _resolve_source(manifest)
    normalized_master, normalized_facts = _normalize_master(source)
    root = Path(workspace_root).resolve()
    run_dir = (root / run_id).resolve()
    if run_dir == root or not _is_relative_to(run_dir, root):
        raise PipelineBlocked("RUN_OUTSIDE_WORKSPACE")
    if run_dir == project or _is_relative_to(run_dir, project):
        raise PipelineBlocked("WORKSPACE_INSIDE_PROJECT")
    if run_dir.exists():
        raise PipelineBlocked("RUN_ALREADY_EXISTS")

    root.mkdir(parents=True, exist_ok=True)
    temporary_run = root / f".{run_id}.preparing-{uuid4().hex}"
    try:
        temporary_run.mkdir()
        for relative in RUN_DIRS:
            _run_destination(temporary_run, relative).mkdir(parents=True)
        _write_json_atomic(
            _run_destination(temporary_run, PREPARED_RUN_MARKER),
            _prepared_marker_value(project, root),
        )

        chroma = choose_chroma(normalized_master)
        technical_master = _flatten_on_chroma(
            normalized_master.resize(TECHNICAL_SIZE, Image.Resampling.NEAREST),
            chroma,
        )
        technical_master.save(
            _run_destination(temporary_run, "input/master/master-technical.png")
        )
        build_pose_guides(manifest, normalized_master, temporary_run)
        geometry_path = _run_destination(temporary_run, "reports/pose_geometry.json")
        geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
        run_manifest = {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state": "DRAFT",
            "project_root": str(project),
            "source_path": str(source),
            "chroma_rgb": geometry["chroma_rgb"],
            "normalized_input": json.loads(json.dumps(manifest)),
            "normalized_master": {**normalized_facts, **geometry["normalized_master"]},
            "resolved_regions": geometry["resolved_regions"],
            "skeleton": geometry["skeleton"],
            "guides": geometry["guides"],
            "technical_master": "input/master/master-technical.png",
            "pose_guides": [guide["path"] for guide in geometry["guides"]],
            "frame_seeds": [
                _frame_seed(run_id, frame_index)
                for frame_index in range(1, manifest["frame_count"] + 1)
            ],
            "frames": [],
            "requests": [],
            "last_accepted_frame": 0,
        }
        _write_json_atomic(
            _run_destination(temporary_run, "reports/run_manifest.json"), run_manifest
        )
        temporary_run.rename(run_dir)
        return run_dir
    except Exception:
        if temporary_run.exists() and _is_temporary_run_directory(
            temporary_run, root, run_id
        ):
            shutil.rmtree(temporary_run)
        raise


def _matches_expected(value, expected):
    if type(value) is not type(expected):
        return False
    if isinstance(expected, Mapping):
        return value.keys() == expected.keys() and all(
            _matches_expected(value[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _matches_expected(actual_item, expected_item)
            for actual_item, expected_item in zip(value, expected)
        )
    return value == expected


def validate_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("INVALID_MANIFEST") from error

    if not isinstance(manifest, dict):
        raise PipelineBlocked("INVALID_MANIFEST")

    for field in sorted(REQUIRED_FIELDS):
        if field not in manifest:
            raise PipelineBlocked(f"MISSING_FIELD:{field}")

    for field, expected in EXPECTED_FIELDS.items():
        if not _matches_expected(manifest[field], expected):
            raise PipelineBlocked(f"INCOMPATIBLE_FIELD:{field}")

    return manifest


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline orchestration for the eight-frame sequential sprite pilot."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--manifest", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--workspace-root", required=True)
    prepare.add_argument("--run-id", required=True)

    for name in ("next-request", "resume"):
        request = commands.add_parser(name)
        request.add_argument("--manifest", required=True)
        request.add_argument("--run-manifest", required=True)
        request.add_argument("--request-output", required=True)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--run-manifest", required=True)
    evaluate.add_argument("--frame-index", required=True, type=int)
    evaluate.add_argument("--raw", required=True)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--manifest", required=True)
    assemble.add_argument("--run-manifest", required=True)
    return parser


def _read_run_manifest(path: Path) -> tuple[dict, Path]:
    run_manifest_path = Path(path).resolve()
    try:
        run = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked("INVALID_RUN_MANIFEST") from error
    run_dir = run_manifest_path.parent.parent.resolve()
    if run_manifest_path != _run_destination(run_dir, "reports/run_manifest.json"):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    if run.get("run_dir") != str(run_dir):
        raise PipelineBlocked("INVALID_RUN_MANIFEST")
    return run, run_dir


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError as error:
        raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED") from error


def _validate_request_output_path(destination: Path, references: Path) -> None:
    try:
        relative = destination.relative_to(references)
    except ValueError as error:
        raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED") from error
    if not relative.parts:
        raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED")
    if _is_link_or_junction(references) or not references.is_dir():
        raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED")
    canonical_references = references.resolve(strict=True)
    current = references
    for index, component in enumerate(relative.parts):
        current = current / component
        if _is_link_or_junction(current):
            raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED")
        if not current.exists():
            break
        canonical = current.resolve(strict=True)
        if not _is_relative_to(canonical, canonical_references):
            raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED")
        is_destination = index == len(relative.parts) - 1
        if (is_destination and not current.is_file()) or (
            not is_destination and not current.is_dir()
        ):
            raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED")


def _write_request_output(path: Path, request: dict, run_dir: Path) -> Path:
    references = Path(os.path.abspath(run_dir / "input/references"))
    destination = Path(os.path.abspath(path))
    _validate_request_output_path(destination, references)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists() or _is_link_or_junction(temporary):
        raise PipelineBlocked("REQUEST_OUTPUT_NOT_CONFINED")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _validate_request_output_path(destination, references)
    transport_request = {
        "frame_index": request["frame_index"],
        "model": request["model"],
        "input": request["input"],
    }
    canonical = json.dumps(
        transport_request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    transport_request["request_fingerprint"] = hashlib.sha256(canonical).hexdigest()
    _write_json_atomic(destination, transport_request)
    return destination


def _request_cli_payload(request: dict | None, run: dict, output: Path) -> dict:
    if request is None:
        return {
            "state": run.get("state", "DRAFT"),
            "request_ready": False,
            "reason": run.get("terminal_reason", "NO_NEW_REQUEST"),
        }
    return {
        "state": run.get("state", "DRAFT"),
        "request_ready": True,
        "frame_index": request["frame_index"],
        "model": request["model"],
        "reference_roles": request["reference_roles"],
        "reference_paths": request["reference_paths"],
        "request_output": str(output),
    }


def _execute_cli(arguments: argparse.Namespace) -> dict:
    if arguments.command == "validate":
        return {"state": "VALID", "manifest": validate_manifest(Path(arguments.manifest))}
    if arguments.command == "prepare":
        manifest = validate_manifest(Path(arguments.manifest))
        run_dir = prepare_workspace(
            manifest, Path(arguments.workspace_root), arguments.run_id
        )
        return {
            "state": "DRAFT",
            "run_dir": str(run_dir),
            "run_manifest": str(run_dir / "reports/run_manifest.json"),
        }
    if arguments.command in {"next-request", "resume"}:
        manifest = validate_manifest(Path(arguments.manifest))
        run, run_dir = _read_run_manifest(Path(arguments.run_manifest))
        request = next_request(run, manifest)
        if run.get("state") == "TECHNICAL_REJECTED":
            _write_json_atomic(Path(arguments.run_manifest).resolve(), run)
        output = Path(arguments.request_output)
        if request is not None:
            output = _write_request_output(output, request, run_dir)
            written = json.loads(output.read_text(encoding="utf-8"))
            run["next_request"] = {
                "fingerprint": written["request_fingerprint"],
                "frame_index": written["frame_index"],
                "model": written["model"],
            }
            _write_json_atomic(Path(arguments.run_manifest).resolve(), run)
        else:
            output = Path(os.path.abspath(output))
            if "next_request" in run:
                run.pop("next_request")
                _write_json_atomic(Path(arguments.run_manifest).resolve(), run)
        return _request_cli_payload(request, run, output)
    if arguments.command == "evaluate":
        _, run_dir = _read_run_manifest(Path(arguments.run_manifest))
        return record_evaluation(run_dir, arguments.frame_index, Path(arguments.raw))
    if arguments.command == "assemble":
        manifest = validate_manifest(Path(arguments.manifest))
        _, run_dir = _read_run_manifest(Path(arguments.run_manifest))
        return assemble_outputs(run_dir, manifest)
    raise PipelineBlocked("INVALID_COMMAND")


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    try:
        arguments = parser.parse_args(argv)
        payload = _execute_cli(arguments)
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 2 if payload.get("state") in {"BLOCKED", "TECHNICAL_REJECTED"} else 0
    except PipelineBlocked as error:
        sys.stdout.write(
            json.dumps(
                {"state": "BLOCKED", "reason": str(error)},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
