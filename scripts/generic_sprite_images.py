"""PNG intake and conservative normalization for generic sprite sources."""

from collections import Counter, deque
import hashlib
import io
from pathlib import Path
from typing import Optional
import warnings

from PIL import Image


class GenericImageBlocked(Exception):
    """Raised when a source cannot be normalized without an unsafe guess."""

    def __init__(self, reason: str, evidence: dict | None = None):
        super().__init__(reason)
        self.evidence = evidence or {}


def _blocked(reason: str, evidence: dict | None = None) -> GenericImageBlocked:
    return GenericImageBlocked(reason, evidence)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rgba_rgb(pixel):
    return tuple(pixel[:3])


def recover_exact_uniform_border(image: Image.Image) -> tuple[Image.Image, dict]:
    """Remove an exactly uniform, edge-connected opaque RGB border."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    alpha = rgba.getchannel("A")
    if alpha.getbbox() is None:
        raise _blocked("INPUT_EMPTY_ALPHA")

    # Existing transparency is authoritative; never infer a replacement key.
    if alpha.getextrema() != (255, 255):
        return rgba, {"background_action": "preserved-alpha"}

    border = set()
    border.update(_rgba_rgb(pixels[x, 0]) for x in range(width))
    border.update(_rgba_rgb(pixels[x, height - 1]) for x in range(width))
    border.update(_rgba_rgb(pixels[0, y]) for y in range(height))
    border.update(_rgba_rgb(pixels[width - 1, y]) for y in range(height))
    if len(border) != 1:
        raise _blocked("INPUT_BACKGROUND_COMPLEX")
    key = next(iter(border))

    visited = set()
    queue = deque()
    for x in range(width):
        queue.extend(((x, 0), (x, height - 1)))
    for y in range(height):
        queue.extend(((0, y), (width - 1, y)))
    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or _rgba_rgb(pixels[x, y]) != key:
            continue
        visited.add((x, y))
        pixels[x, y] = (*key, 0)
        if x:
            queue.append((x - 1, y))
        if x + 1 < width:
            queue.append((x + 1, y))
        if y:
            queue.append((x, y - 1))
        if y + 1 < height:
            queue.append((x, y + 1))
    return rgba, {"background_action": "removed-exact-uniform-border"}


def place_bottom_center(image: Image.Image, size: tuple[int, int], resample_mode: str) -> Image.Image:
    if resample_mode != "nearest":
        raise _blocked("RESAMPLE_MODE_INVALID")
    width, height = size
    if width <= 0 or height <= 0:
        raise _blocked("OUTPUT_SIZE_INVALID")
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    source = image.convert("RGBA")
    x = (width - source.width) // 2
    y = height - source.height
    canvas.alpha_composite(source, (x, y))
    return canvas


def _ground_same_canvas(image: Image.Image) -> Image.Image:
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise _blocked("INPUT_EMPTY_ALPHA")
    x = (image.width - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = image.height - (bbox[3] - bbox[1]) - bbox[1]
    grounded = Image.new("RGBA", image.size, (0, 0, 0, 0))
    grounded.alpha_composite(image, (x, y))
    return grounded


def normalize_source(
    path: Path,
    output_size: Optional[tuple[int, int]],
    resample_mode: str = "nearest",
) -> tuple[Image.Image, dict]:
    if resample_mode != "nearest":
        raise _blocked("RESAMPLE_MODE_INVALID")
    path = Path(path)
    try:
        source_hash = sha256_file(path)
        with Image.open(path) as probe:
            probe.verify()
        with Image.open(path) as opened:
            if opened.format != "PNG":
                raise _blocked("INPUT_NOT_PNG")
            source_mode = opened.mode
            source_size = opened.size
            image = opened.copy()
    except GenericImageBlocked:
        raise
    except Exception as exc:
        raise _blocked("INPUT_NOT_PNG") from exc

    normalized, bg_report = recover_exact_uniform_border(image)
    if output_size is not None:
        if len(output_size) != 2 or output_size[0] <= 0 or output_size[1] <= 0:
            raise _blocked("OUTPUT_SIZE_INVALID")
        if output_size == normalized.size:
            normalized = _ground_same_canvas(normalized)
            bbox = normalized.getchannel("A").getbbox()
            if bbox is None:
                raise _blocked("INPUT_EMPTY_ALPHA")
            resized = None
        else:
            bbox = normalized.getchannel("A").getbbox()
            if bbox is None:
                raise _blocked("INPUT_EMPTY_ALPHA")
            normalized = normalized.crop(bbox)
            target_width, target_height = output_size
            scale = min(target_width / normalized.width, target_height / normalized.height)
            resized_size = (
                max(1, int(normalized.width * scale)),
                max(1, int(normalized.height * scale)),
            )
            resized = normalized.resize(resized_size, Image.Resampling.NEAREST)
            if resized.getchannel("A").getbbox() is None:
                raise _blocked("INPUT_EMPTY_ALPHA")
        if resized is not None:
            normalized = place_bottom_center(resized, output_size, resample_mode)
    else:
        normalized = _ground_same_canvas(normalized)

    bbox = normalized.getchannel("A").getbbox()
    report = {
        "source_sha256": source_hash,
        "source_mode": source_mode,
        "source_size": [source_size[0], source_size[1]],
        "background_action": bg_report["background_action"],
        "normalized_size": [normalized.width, normalized.height],
        "alpha_bbox": list(bbox) if bbox is not None else None,
        "resample_mode": resample_mode,
    }
    return normalized, report


def _generated_output(manifest: dict) -> tuple[int, int, str]:
    try:
        output = manifest["output"]
        width = output["width"]
        height = output["height"]
        background = output["background"]
        anchor = output["anchor"]
        resample_mode = output["resample_mode"]
    except (KeyError, TypeError):
        raise _blocked("MANIFEST_INVALID") from None
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or background != "transparent"
        or anchor != "bottom-center"
        or resample_mode != "nearest"
    ):
        raise _blocked("MANIFEST_INVALID")
    return width, height, resample_mode


def _recover_generated_uniform_border(image: Image.Image) -> tuple[Image.Image, dict]:
    """Recover alpha only from an exact opaque top/left/right border.

    The lower border is deliberately excluded because a grounded character may
    touch it. Existing transparency is authoritative and is never recolored.
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    alpha = rgba.getchannel("A")
    if alpha.getbbox() is None:
        raise _blocked(
            "FRAME_EMPTY",
            {
                "advisory": {
                    "alpha_bbox": None,
                    "opaque_pixels": 0,
                    "visible_pixels": 0,
                    "measurement": {"status": "AVAILABLE", "space": "raw_alpha"},
                }
            },
        )
    if alpha.getextrema() != (255, 255):
        return rgba, {
            "background_action": "preserved-rgba-alpha",
            "detected_background_rgb": None,
        }

    pixels = rgba.load()
    prohibited_border = {
        _rgba_rgb(pixels[x, 0]) for x in range(width)
    }
    prohibited_border.update(_rgba_rgb(pixels[0, y]) for y in range(height))
    prohibited_border.update(_rgba_rgb(pixels[width - 1, y]) for y in range(height))
    if len(prohibited_border) != 1:
        raise _blocked(
            "FRAME_CLIPPED",
            {
                "normalization": {
                    "background_action": "unavailable-nonuniform-prohibited-border",
                    "detected_background_rgb": None,
                },
                "advisory": {
                    "alpha_bbox": None,
                    "opaque_pixels": None,
                    "visible_pixels": None,
                    "measurement": {
                        "status": "UNAVAILABLE",
                        "reason": "OPAQUE_PROHIBITED_BORDER_NONUNIFORM",
                    },
                },
            },
        )
    key = next(iter(prohibited_border))
    queue = deque((x, 0) for x in range(width))
    queue.extend((0, y) for y in range(height))
    queue.extend((width - 1, y) for y in range(height))
    visited = set()
    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or _rgba_rgb(pixels[x, y]) != key:
            continue
        visited.add((x, y))
        pixels[x, y] = (*key, 0)
        if x:
            queue.append((x - 1, y))
        if x + 1 < width:
            queue.append((x + 1, y))
        if y:
            queue.append((x, y - 1))
        if y + 1 < height:
            queue.append((x, y + 1))
    if rgba.getchannel("A").getbbox() is None:
        raise _blocked("FRAME_EMPTY")
    return rgba, {
        "background_action": "removed-detected-exact-uniform-border",
        "detected_background_rgb": list(key),
    }


def _normalize_generated_frame_bytes(
    raw_bytes: bytes,
    manifest: dict,
) -> tuple[Image.Image, dict]:
    """Normalize the provider bytes captured by the caller without reopening a path."""
    width, height, resample_mode = _generated_output(manifest)
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes)) as probe:
                probe.verify()
            with Image.open(io.BytesIO(raw_bytes)) as opened:
                if opened.format != "PNG":
                    raise _blocked("FRAME_NOT_PNG")
                raw_mode = opened.mode
                raw_size = opened.size
                decoded = opened.copy()
    except GenericImageBlocked:
        raise
    except (
        OSError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise _blocked(
            "FRAME_NOT_PNG",
            {
                "normalization": {
                    "status": "FAILED",
                    "failure": "FRAME_NOT_PNG",
                    "raw_sha256": raw_hash,
                },
                "advisory": {
                    "alpha_bbox": None,
                    "opaque_pixels": None,
                    "visible_pixels": None,
                    "measurement": {"status": "UNAVAILABLE", "reason": "PNG_DECODE_FAILED"},
                },
            },
        ) from exc

    try:
        recovered, background = _recover_generated_uniform_border(decoded)
    except GenericImageBlocked as error:
        evidence = dict(error.evidence)
        normalization = {
            "status": "FAILED",
            "failure": str(error),
            "raw_sha256": raw_hash,
            "raw_mode": raw_mode,
            "raw_size": [raw_size[0], raw_size[1]],
        }
        normalization.update(evidence.get("normalization", {}))
        evidence["normalization"] = normalization
        raise _blocked(str(error), evidence) from None
    raw_bbox = recovered.getchannel("A").getbbox()
    if raw_bbox is None:
        raise _blocked("FRAME_EMPTY")
    if raw_bbox[0] == 0 or raw_bbox[2] == recovered.width or raw_bbox[1] == 0:
        pixels = tuple(recovered.get_flattened_data())
        raise _blocked(
            "FRAME_CLIPPED",
            {
                "normalization": {
                    "status": "FAILED",
                    "failure": "FRAME_CLIPPED",
                    "raw_sha256": raw_hash,
                    "raw_mode": raw_mode,
                    "raw_size": [raw_size[0], raw_size[1]],
                    "background_action": background["background_action"],
                    "detected_background_rgb": background["detected_background_rgb"],
                    "raw_alpha_bbox": list(raw_bbox),
                },
                "advisory": {
                    "alpha_bbox": list(raw_bbox),
                    "opaque_pixels": sum(pixel[3] == 255 for pixel in pixels),
                    "visible_pixels": sum(pixel[3] > 0 for pixel in pixels),
                    "measurement": {
                        "status": "AVAILABLE",
                        "space": "raw_after_background_recovery",
                    },
                },
            },
        )

    if recovered.size == (width, height):
        normalized = _ground_same_canvas(recovered)
    else:
        available_width = width - 2
        available_height = height - 1
        if available_width < 1 or available_height < 1:
            pixels = tuple(recovered.get_flattened_data())
            raise _blocked(
                "FRAME_CLIPPED",
                {
                    "normalization": {
                        "status": "FAILED",
                        "failure": "FRAME_CLIPPED",
                        "raw_sha256": raw_hash,
                        "raw_mode": raw_mode,
                        "raw_size": [raw_size[0], raw_size[1]],
                        "background_action": background["background_action"],
                        "detected_background_rgb": background["detected_background_rgb"],
                        "raw_alpha_bbox": list(raw_bbox),
                        "normalized_size": [width, height],
                        "alpha_bbox": None,
                        "resample_mode": resample_mode,
                    },
                    "advisory": {
                        "alpha_bbox": list(raw_bbox),
                        "opaque_pixels": sum(pixel[3] == 255 for pixel in pixels),
                        "visible_pixels": sum(pixel[3] > 0 for pixel in pixels),
                        "measurement": {
                            "status": "AVAILABLE",
                            "space": "raw_after_background_recovery",
                        },
                    },
                },
            )
        subject = recovered.crop(raw_bbox)
        scale = min(
            available_width / subject.width,
            available_height / subject.height,
        )
        resized_size = (
            max(1, int(subject.width * scale)),
            max(1, int(subject.height * scale)),
        )
        resized = subject.resize(resized_size, Image.Resampling.NEAREST)
        if resized.getchannel("A").getbbox() is None:
            raise _blocked("FRAME_EMPTY")
        normalized = place_bottom_center(resized, (width, height), resample_mode)
    normalized_bbox = normalized.getchannel("A").getbbox()
    if normalized_bbox is None:
        raise _blocked("FRAME_EMPTY")
    if (
        normalized_bbox[0] == 0
        or normalized_bbox[2] == normalized.width
        or normalized_bbox[1] == 0
    ):
        pixels = tuple(normalized.get_flattened_data())
        raise _blocked(
            "FRAME_CLIPPED",
            {
                "normalization": {
                    "status": "FAILED",
                    "failure": "FRAME_CLIPPED",
                    "raw_sha256": raw_hash,
                    "raw_mode": raw_mode,
                    "raw_size": [raw_size[0], raw_size[1]],
                    "background_action": background["background_action"],
                    "detected_background_rgb": background["detected_background_rgb"],
                    "raw_alpha_bbox": list(raw_bbox),
                    "normalized_size": [normalized.width, normalized.height],
                    "alpha_bbox": list(normalized_bbox),
                    "resample_mode": resample_mode,
                },
                "advisory": {
                    "alpha_bbox": list(normalized_bbox),
                    "opaque_pixels": sum(pixel[3] == 255 for pixel in pixels),
                    "visible_pixels": sum(pixel[3] > 0 for pixel in pixels),
                    "measurement": {
                        "status": "AVAILABLE",
                        "space": "normalized_alpha",
                    },
                },
            },
        )
    return normalized, {
        "raw_sha256": raw_hash,
        "raw_mode": raw_mode,
        "raw_size": [raw_size[0], raw_size[1]],
        "background_action": background["background_action"],
        "detected_background_rgb": background["detected_background_rgb"],
        "raw_alpha_bbox": list(raw_bbox),
        "normalized_size": [normalized.width, normalized.height],
        "alpha_bbox": list(normalized_bbox),
        "resample_mode": resample_mode,
    }


def normalize_generated_frame(raw: Path, manifest: dict) -> tuple[Image.Image, dict]:
    """Normalize one provider PNG without guessing a chroma from the master."""
    _generated_output(manifest)
    try:
        raw_bytes = Path(raw).read_bytes()
    except OSError as exc:
        raise _blocked(
            "FRAME_NOT_PNG",
            {
                "normalization": {"status": "FAILED", "failure": "FRAME_NOT_PNG"},
                "advisory": {
                    "alpha_bbox": None,
                    "opaque_pixels": None,
                    "visible_pixels": None,
                    "measurement": {
                        "status": "UNAVAILABLE",
                        "reason": "PNG_DECODE_FAILED",
                    },
                },
            },
        ) from exc
    return _normalize_generated_frame_bytes(raw_bytes, manifest)


def _assembly_frames(frames: tuple[Image.Image, ...]) -> tuple[Image.Image, ...]:
    if not isinstance(frames, tuple) or not frames:
        raise ValueError("FRAMES_REQUIRED")
    if any(not isinstance(frame, Image.Image) for frame in frames):
        raise ValueError("FRAMES_INVALID")
    size = frames[0].size
    if size[0] <= 0 or size[1] <= 0:
        raise ValueError("FRAME_DIMENSIONS_MISMATCH")
    if any(frame.size != size for frame in frames):
        raise ValueError("FRAME_DIMENSIONS_MISMATCH")
    return tuple(frame.convert("RGBA").copy() for frame in frames)


def assemble_sheet(frames: tuple[Image.Image, ...]) -> Image.Image:
    """Assemble equal-sized frames horizontally without changing source pixels."""
    ordered = _assembly_frames(frames)
    width, height = ordered[0].size
    sheet = Image.new("RGBA", (width * len(ordered), height), (0, 0, 0, 0))
    for index, frame in enumerate(ordered):
        sheet.paste(frame, (index * width, 0))
    return sheet


def save_loop_gif(
    frames: tuple[Image.Image, ...],
    destination: Path,
    duration_ms: int,
) -> None:
    """Write a metadata-minimal deterministic sequence preview."""
    Path(destination).write_bytes(_loop_gif_bytes(frames, duration_ms))


def _loop_gif_bytes(frames: tuple[Image.Image, ...], duration_ms: int) -> bytes:
    """Build the complete GIF before its caller publishes any final path."""
    ordered = _assembly_frames(frames)
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("GIF_DURATION_INVALID")
    buffer = io.BytesIO()
    ordered[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=list(ordered[1:]),
        duration=duration_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )
    return buffer.getvalue()


def save_contact_sheet(
    frames: tuple[Image.Image, ...],
    destination: Path,
) -> None:
    """Write the ordered frames as a lossless horizontal review preview."""
    buffer = io.BytesIO()
    assemble_sheet(frames).save(buffer, format="PNG")
    Path(destination).write_bytes(buffer.getvalue())


_METRIC_DEFINITIONS = {
    "bbox_movement": "candidate_bbox_minus_reference_bbox_[left,top,right,bottom]",
    "normalized_pixel_difference_count": "exact_rgba_pixel_mismatches_on_equal_canvas",
    "silhouette": "alpha_gt_zero_intersection_and_union_pixel_counts",
    "dominant_color_histogram_distance": "integer_l1_distance_over_4bit_rgb_histograms_of_visible_pixels",
}


def _visible_histogram(image: Image.Image) -> Counter:
    return Counter(
        (red >> 4, green >> 4, blue >> 4)
        for red, green, blue, alpha in image.convert("RGBA").get_flattened_data()
        if alpha > 0
    )


def _advisory_comparison(candidate: Image.Image, reference: Image.Image) -> dict:
    candidate_rgba = candidate.convert("RGBA")
    reference_rgba = reference.convert("RGBA")
    if candidate_rgba.size != reference_rgba.size:
        return {
            "comparison_status": "UNAVAILABLE",
            "unavailable_reason": "DIMENSIONS_MISMATCH",
            "candidate_size": [candidate_rgba.width, candidate_rgba.height],
            "reference_size": [reference_rgba.width, reference_rgba.height],
            "bbox_movement": None,
            "normalized_pixel_difference_count": None,
            "silhouette_intersection": None,
            "silhouette_union": None,
            "dominant_color_histogram_distance": None,
        }
    candidate_bbox = candidate_rgba.getchannel("A").getbbox()
    reference_bbox = reference_rgba.getchannel("A").getbbox()
    bbox_movement = None
    if candidate_bbox is not None and reference_bbox is not None:
        bbox_movement = [
            candidate_bbox[index] - reference_bbox[index]
            for index in range(4)
        ]
    candidate_pixels = tuple(candidate_rgba.get_flattened_data())
    reference_pixels = tuple(reference_rgba.get_flattened_data())
    candidate_mask = tuple(pixel[3] > 0 for pixel in candidate_pixels)
    reference_mask = tuple(pixel[3] > 0 for pixel in reference_pixels)
    candidate_histogram = _visible_histogram(candidate_rgba)
    reference_histogram = _visible_histogram(reference_rgba)
    histogram_keys = candidate_histogram.keys() | reference_histogram.keys()
    return {
        "bbox_movement": bbox_movement,
        "normalized_pixel_difference_count": sum(
            left != right for left, right in zip(candidate_pixels, reference_pixels)
        ),
        "silhouette_intersection": sum(
            left and right for left, right in zip(candidate_mask, reference_mask)
        ),
        "silhouette_union": sum(
            left or right for left, right in zip(candidate_mask, reference_mask)
        ),
        "dominant_color_histogram_distance": sum(
            abs(candidate_histogram[key] - reference_histogram[key])
            for key in histogram_keys
        ),
    }


def technical_frame_report(
    frame: Image.Image,
    master: Image.Image,
    previous: Image.Image | None,
    manifest: dict,
    frame_index: int,
) -> dict:
    """Return deterministic gates and explicitly advisory comparison evidence."""
    width, height, _resample_mode = _generated_output(manifest)
    candidate = frame.convert("RGBA")
    alpha = candidate.getchannel("A")
    bbox = alpha.getbbox()
    failures = []
    if candidate.size != (width, height):
        failures.append("FRAME_DIMENSIONS_MISMATCH")
    if bbox is None:
        failures.append("FRAME_EMPTY")
    else:
        if bbox[0] == 0 or bbox[2] == candidate.width or bbox[1] == 0:
            failures.append("FRAME_CLIPPED")
        if bbox[3] != height:
            failures.append("FRAME_NOT_GROUNDED")
        expected_left = (width - (bbox[2] - bbox[0])) // 2
        if bbox[0] != expected_left:
            failures.append("FRAME_NOT_BOTTOM_CENTERED")
    pixels = tuple(candidate.get_flattened_data())
    return {
        "schema_version": 1,
        "frame_index": frame_index,
        "technical_result": "PASS" if not failures else "FAIL",
        "failures": failures,
        "normalization": None,
        "advisory": {
            "alpha_bbox": list(bbox) if bbox is not None else None,
            "opaque_pixels": sum(pixel[3] == 255 for pixel in pixels),
            "visible_pixels": sum(pixel[3] > 0 for pixel in pixels),
            "measurement": {"status": "AVAILABLE", "space": "normalized_canvas"},
            "metric_definitions": dict(_METRIC_DEFINITIONS),
            "master": _advisory_comparison(candidate, master),
            "previous": (
                _advisory_comparison(candidate, previous)
                if previous is not None
                else None
            ),
        },
        "provenance": None,
    }
