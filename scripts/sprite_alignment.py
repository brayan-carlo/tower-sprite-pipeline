"""Pure integer body-X alignment, preserving visible pixels and natural Y.

This module verifies geometry only. Callers must review whether the supplied
points identify the same anatomical reference in every frame.
"""

from collections.abc import Mapping, Set
import math
from numbers import Integral, Real

from PIL import Image


MAX_DIMENSION = 4096
MAX_OUTPUT_PIXELS = 64_000_000
POLICY = "body_x_preserve_y_v1"


class AlignmentError(ValueError):
    """Invalid input or unsafe output geometry, with a stable machine code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _items(value, code, label):
    if isinstance(value, (str, bytes, bytearray, Mapping, Set)):
        raise AlignmentError(code, f"{label} must be an ordered iterable.")
    try:
        return list(value)
    except TypeError as error:
        raise AlignmentError(code, f"{label} must be an ordered iterable.") from error


def align_body_x(frames, anchors, *, canvas_width=None):
    """Return new RGBA frames and a JSON-serializable transformation report.

    Coordinates use pixel centers: 0 <= x <= width-1, 0 <= y <= height-1.
    The output width is at least the input width and ``canvas_width`` (when
    provided), expanded only as necessary to keep every nonzero-alpha pixel.
    Half-pixel ties round toward positive X. Transparent pixels outside a
    translated input canvas may be discarded; visible RGBA bytes never change.

    Raises AlignmentError with INVALID_FRAMES, INVALID_FRAME, MIXED_CANVAS,
    INVALID_ANCHORS, INVALID_ANCHOR, INVALID_CANVAS_WIDTH, DIMENSION_LIMIT,
    or PIXEL_LIMIT. No output image is allocated until all geometry is safe.
    """
    sources = _items(frames, "INVALID_FRAMES", "frames")
    if not sources:
        raise AlignmentError("INVALID_FRAMES", "At least one frame is required.")
    size = None
    for index, frame in enumerate(sources):
        if not isinstance(frame, Image.Image) or frame.mode != "RGBA":
            raise AlignmentError("INVALID_FRAME", f"Frame {index} must be a PIL RGBA image.")
        width, height = frame.size
        if width < 1 or height < 1:
            raise AlignmentError("INVALID_FRAME", f"Frame {index} has an empty canvas.")
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise AlignmentError("DIMENSION_LIMIT", f"Frame {index} exceeds 4096 pixels per dimension.")
        if size is not None and frame.size != size:
            raise AlignmentError("MIXED_CANVAS", "All frames must use the same canvas.")
        size = frame.size
    width, height = size

    if canvas_width is not None:
        if (isinstance(canvas_width, bool) or not isinstance(canvas_width, Integral)
                or not 1 <= canvas_width <= MAX_DIMENSION):
            raise AlignmentError("INVALID_CANVAS_WIDTH", "canvas_width must be an integer from 1 to 4096.")
        canvas_width = int(canvas_width)
    minimum_width = max(width, canvas_width or width)
    if len(sources) * minimum_width * height > MAX_OUTPUT_PIXELS:
        raise AlignmentError("PIXEL_LIMIT", "Output would exceed 64000000 total pixels.")

    points = _items(anchors, "INVALID_ANCHORS", "anchors")
    if len(points) != len(sources):
        raise AlignmentError("INVALID_ANCHORS", "Exactly one reference point is required per frame.")
    normalized = []
    x_ratios = []
    for index, point in enumerate(points):
        coordinates = _items(point, "INVALID_ANCHOR", f"Anchor {index}")
        if len(coordinates) != 2:
            raise AlignmentError("INVALID_ANCHOR", f"Anchor {index} must contain exactly x and y.")
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in coordinates):
            raise AlignmentError("INVALID_ANCHOR", f"Anchor {index} coordinates must be real numbers, excluding bool.")
        # Compare the original Real values: conversion can round an outside
        # Fraction onto an edge or underflow a small negative value to -0.0.
        if not (0 <= coordinates[0] <= width - 1 and 0 <= coordinates[1] <= height - 1):
            raise AlignmentError("INVALID_ANCHOR", f"Anchor {index} lies outside the input canvas.")
        try:
            x, y = (float(value) for value in coordinates)
        except (OverflowError, TypeError, ValueError) as error:
            raise AlignmentError("INVALID_ANCHOR", f"Anchor {index} coordinates must be finite.") from error
        if not math.isfinite(x) or not math.isfinite(y):
            raise AlignmentError("INVALID_ANCHOR", f"Anchor {index} coordinates must be finite.")
        if not (0 <= x <= width - 1 and 0 <= y <= height - 1):
            raise AlignmentError("INVALID_ANCHOR", f"Anchor {index} lies outside the input canvas.")
        normalized.append([x, y])
        x_ratios.append(x.as_integer_ratio())

    bounds = []
    for index, frame in enumerate(sources):
        try:
            bounds.append(frame.getchannel("A").getbbox())
        except (OSError, ValueError) as error:
            raise AlignmentError("INVALID_FRAME", f"Frame {index} pixels cannot be read.") from error

    # Exact integer arithmetic implements floor(output_width / 2 - anchor_x).
    # It avoids cancellation around ties for fractional binary float anchors.
    output_width = minimum_width
    while output_width <= MAX_DIMENSION:
        shifts = [
            (output_width * denominator - 2 * numerator) // (2 * denominator)
            for numerator, denominator in x_ratios
        ]
        if all(box is None or (box[0] + dx >= 0 and box[2] + dx <= output_width)
               for box, dx in zip(bounds, shifts)):
            break
        output_width += 1
    else:
        raise AlignmentError("DIMENSION_LIMIT", "Preserving visible pixels requires width above 4096.")
    if len(sources) * output_width * height > MAX_OUTPUT_PIXELS:
        raise AlignmentError("PIXEL_LIMIT", "Expanded output would exceed 64000000 total pixels.")

    output = []
    for frame, dx in zip(sources, shifts):
        aligned = Image.new("RGBA", (output_width, height), (0, 0, 0, 0))
        # No alpha mask or compositing: copy RGBA bytes, including partial alpha.
        aligned.paste(frame, (dx, 0))
        output.append(aligned)
    report = {
        "policy": POLICY,
        "input_canvas": {"width": width, "height": height},
        "output_canvas": {"width": output_width, "height": height},
        "target_x": (output_width - 1) / 2,
        "anchors_before": normalized,
        "anchors_after": [[x + dx, y] for (x, y), dx in zip(normalized, shifts)],
        "transforms": [
            {"frame_index": index, "dx": dx, "dy": 0, "scale": 1}
            for index, dx in enumerate(shifts)
        ],
    }
    return output, report
