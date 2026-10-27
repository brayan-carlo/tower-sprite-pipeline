from __future__ import annotations

import hashlib
import io
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, GifImagePlugin


MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_PIXELS = 64_000_000
MEANINGFUL_ALPHA = 16
PADDING = 2


class SheetImageError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _Cell:
    index: int
    bounds: tuple[int, int, int, int]
    size: tuple[int, int]
    meaningful_bbox: tuple[int, int, int, int]
    crop_bbox: tuple[int, int, int, int]
    crop: Image.Image
    ignored_low_alpha_pixels: int


def _fail(code: str, message: str) -> None:
    raise SheetImageError(code, message)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_arguments(
    raw_path: str | Path,
    output_dir: str | Path,
    *,
    frame_count: int,
    columns: int,
    rows: int,
    canvas: tuple[int, int],
    frame_ms: int,
    loop: bool,
    pixel_art: bool,
    grounded: bool,
) -> tuple[Path, Path, int, int]:
    if not isinstance(raw_path, (str, Path)) or not isinstance(output_dir, (str, Path)):
        _fail("INPUT_INVALID", "input and output paths must be strings or Path values")
    if not all(_is_int(value) for value in (frame_count, columns, rows, frame_ms)):
        _fail("INPUT_INVALID", "frame and grid values must be integers")
    if not isinstance(canvas, tuple) or len(canvas) != 2 or not all(_is_int(value) for value in canvas):
        _fail("INPUT_INVALID", "canvas must be a pair of integers")
    if not all(isinstance(value, bool) for value in (loop, pixel_art, grounded)):
        _fail("INPUT_INVALID", "loop, pixel_art and grounded must be boolean")
    if not 2 <= frame_count <= 16:
        _fail("INPUT_INVALID", "frame_count must be between 2 and 16")
    if columns < 1 or rows < 1:
        _fail("INPUT_INVALID", "columns and rows must be positive")
    if columns * rows < frame_count:
        _fail("GRID_INVALID", "the grid does not contain enough slots")
    if not 20 <= frame_ms <= 1000 or frame_ms % 10:
        _fail("INPUT_INVALID", "frame_ms must be 20..1000 in 10 ms steps")
    canvas_width, canvas_height = canvas
    if not 16 <= canvas_width <= 4096 or not 16 <= canvas_height <= 4096:
        _fail("INPUT_INVALID", "canvas dimensions must be between 16 and 4096")
    if canvas_width * canvas_height * frame_count > MAX_PIXELS:
        _fail("INPUT_INVALID", "requested output exceeds the pixel limit")

    try:
        source = Path(raw_path).expanduser().resolve()
        destination = Path(output_dir).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail("INPUT_INVALID", f"invalid path: {exc.__class__.__name__}")
    if not source.is_file():
        _fail("INPUT_INVALID", "source PNG does not exist")
    try:
        source_size = source.stat().st_size
    except OSError as exc:
        _fail("INPUT_INVALID", f"source PNG is not readable: {exc.__class__.__name__}")
    if source_size <= 0 or source_size > MAX_INPUT_BYTES:
        _fail("INPUT_INVALID", "source PNG must be nonempty and at most 50 MiB")
    if destination.exists() and not destination.is_dir():
        _fail("OUTPUT_CONFLICT", "output destination exists and is not a directory")
    return source, destination, canvas_width, canvas_height


def _load_source(source: Path) -> tuple[Image.Image, bytes]:
    try:
        source_bytes = source.read_bytes()
        with Image.open(io.BytesIO(source_bytes)) as opened:
            original_mode = opened.mode
            has_alpha = "A" in opened.getbands() or "transparency" in opened.info
            if opened.format != "PNG":
                _fail("INPUT_INVALID", "source must be a PNG image")
            width, height = opened.size
            if width < 1 or height < 1 or width * height > MAX_PIXELS:
                _fail("INPUT_INVALID", "source dimensions exceed the pixel limit")
            if not has_alpha or original_mode == "RGB":
                _fail("ALPHA_REQUIRED", "source must contain real transparency")
            image = opened.convert("RGBA")
            image.load()
    except SheetImageError:
        raise
    except (OSError, ValueError) as exc:
        _fail("INPUT_INVALID", f"source PNG could not be decoded: {exc.__class__.__name__}")

    alpha_histogram = image.getchannel("A").histogram()
    transparent_pixels = alpha_histogram[0]
    meaningful_pixels = sum(alpha_histogram[MEANINGFUL_ALPHA:])
    total_pixels = image.width * image.height
    if transparent_pixels == 0 or meaningful_pixels == 0 or transparent_pixels * 100 < total_pixels:
        _fail("ALPHA_REQUIRED", "source must contain at least one percent fully transparent pixels")
    return image, source_bytes


def _axis_bounds(index: int, count: int, dimension: int) -> tuple[int, int]:
    return index * dimension // count, (index + 1) * dimension // count


def _meaningful_mask(alpha: Image.Image) -> Image.Image:
    return alpha.point(lambda value: 255 if value >= MEANINGFUL_ALPHA else 0, mode="L")


def _low_alpha_count(alpha: Image.Image) -> int:
    histogram = alpha.histogram()
    return sum(histogram[1:MEANINGFUL_ALPHA])


def _partition_cells(image: Image.Image, frame_count: int, columns: int, rows: int) -> tuple[list[_Cell], list[list[int]]]:
    if columns > image.width or rows > image.height:
        _fail("GRID_INVALID", "grid cells must have positive dimensions")

    used: list[_Cell] = []
    all_bounds: list[list[int]] = []
    for index in range(columns * rows):
        column = index % columns
        row = index // columns
        left, right = _axis_bounds(column, columns, image.width)
        top, bottom = _axis_bounds(row, rows, image.height)
        bounds = (left, top, right, bottom)
        all_bounds.append(list(bounds))
        cell = image.crop(bounds)
        alpha = cell.getchannel("A")

        if index >= frame_count:
            if alpha.getbbox() is not None:
                _fail("GRID_INVALID", f"unused grid slot {index + 1} contains visible content")
            continue
        bbox = _meaningful_mask(alpha).getbbox()
        if bbox is None:
            _fail("CELL_EMPTY", f"requested grid cell {index + 1} is empty")

        cell_width, cell_height = cell.size
        bbox_left, bbox_top, bbox_right, bbox_bottom = bbox
        if bbox_left == 0 or bbox_top == 0 or bbox_right == cell_width or bbox_bottom == cell_height:
            _fail("CELL_CLIPPED", f"visible content touches an edge in cell {index + 1}")

        crop_bbox = (
            max(0, bbox_left - PADDING),
            max(0, bbox_top - PADDING),
            min(cell_width, bbox_right + PADDING),
            min(cell_height, bbox_bottom + PADDING),
        )
        total_low_alpha = _low_alpha_count(alpha)
        included_low_alpha = _low_alpha_count(alpha.crop(crop_bbox))
        used.append(
            _Cell(
                index=index,
                bounds=bounds,
                size=cell.size,
                meaningful_bbox=bbox,
                crop_bbox=crop_bbox,
                crop=cell.crop(crop_bbox),
                ignored_low_alpha_pixels=total_low_alpha - included_low_alpha,
            )
        )
    return used, all_bounds


def _scaled_length(length: int, scale: float) -> int:
    return max(1, int(math.floor(length * scale + 0.5)))


def _scaled_coordinate(value: int, scale: float) -> int:
    return int(math.floor(value * scale + 0.5))


def _paste_clipped(canvas: Image.Image, source: Image.Image, left: int, top: int) -> None:
    destination_box = (
        max(0, left),
        max(0, top),
        min(canvas.width, left + source.width),
        min(canvas.height, top + source.height),
    )
    if destination_box[0] >= destination_box[2] or destination_box[1] >= destination_box[3]:
        return
    source_box = (
        destination_box[0] - left,
        destination_box[1] - top,
        destination_box[2] - left,
        destination_box[3] - top,
    )
    piece = source.crop(source_box)
    canvas.alpha_composite(piece, (destination_box[0], destination_box[1]))


def _normalize_cells(
    cells: list[_Cell],
    canvas_width: int,
    canvas_height: int,
    *,
    pixel_art: bool,
    grounded: bool,
) -> tuple[list[Image.Image], float]:
    max_bbox_width = max(cell.meaningful_bbox[2] - cell.meaningful_bbox[0] for cell in cells)
    max_bbox_height = max(cell.meaningful_bbox[3] - cell.meaningful_bbox[1] for cell in cells)
    if grounded:
        scale = min(0.90 * canvas_width / max_bbox_width, 0.80 * canvas_height / max_bbox_height)
    else:
        retained_left = min(cell.crop_bbox[0] for cell in cells)
        retained_top = min(cell.crop_bbox[1] for cell in cells)
        retained_right = max(cell.crop_bbox[2] for cell in cells)
        retained_bottom = max(cell.crop_bbox[3] for cell in cells)
        scale = min(
            0.90 * canvas_width / (retained_right - retained_left),
            0.80 * canvas_height / (retained_bottom - retained_top),
        )
    resampling = Image.Resampling.NEAREST if pixel_art else Image.Resampling.LANCZOS
    if grounded:
        shared_origin_x = 0
        shared_origin_y = 0
    else:
        mapped_left = min(_scaled_coordinate(cell.crop_bbox[0], scale) for cell in cells)
        mapped_top = min(_scaled_coordinate(cell.crop_bbox[1], scale) for cell in cells)
        mapped_right = max(
            _scaled_coordinate(cell.crop_bbox[0], scale) + _scaled_length(cell.crop.width, scale)
            for cell in cells
        )
        mapped_bottom = max(
            _scaled_coordinate(cell.crop_bbox[1], scale) + _scaled_length(cell.crop.height, scale)
            for cell in cells
        )
        shared_origin_x = (canvas_width - (mapped_right - mapped_left)) // 2 - mapped_left
        shared_origin_y = (canvas_height - (mapped_bottom - mapped_top)) // 2 - mapped_top
    baseline = math.floor(0.90 * canvas_height)

    frames: list[Image.Image] = []
    for cell in cells:
        resized = cell.crop.resize(
            (_scaled_length(cell.crop.width, scale), _scaled_length(cell.crop.height, scale)),
            resample=resampling,
        )
        normalized = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        if grounded:
            visible = _meaningful_mask(resized.getchannel("A")).getbbox()
            if visible is None:
                _fail("CELL_EMPTY", f"cell {cell.index + 1} became empty during normalization")
            visible_width = visible[2] - visible[0]
            left = (canvas_width - visible_width) // 2 - visible[0]
            top = baseline - visible[3]
        else:
            left = shared_origin_x + _scaled_coordinate(cell.crop_bbox[0], scale)
            top = shared_origin_y + _scaled_coordinate(cell.crop_bbox[1], scale)
            if left < 0 or top < 0 or left + resized.width > canvas_width or top + resized.height > canvas_height:
                _fail("CELL_CLIPPED", f"retained content does not fit canvas for cell {cell.index + 1}")
        _paste_clipped(normalized, resized, left, top)
        frames.append(normalized)
    return frames, scale


def _canonical_rgba_bytes(image: Image.Image) -> bytes:
    raw = bytearray(image.convert("RGBA").tobytes())
    for offset in range(0, len(raw), 4):
        if raw[offset + 3] == 0:
            raw[offset] = 0
            raw[offset + 1] = 0
            raw[offset + 2] = 0
    return bytes(raw)


def _reject_duplicates(frames: list[Image.Image]) -> None:
    seen: dict[str, int] = {}
    for index, frame in enumerate(frames):
        digest = hashlib.sha256(_canonical_rgba_bytes(frame)).hexdigest()
        if digest in seen:
            _fail("DUPLICATE_FRAME", f"frames {seen[digest] + 1} and {index + 1} are exact duplicates")
        seen[digest] = index


def _lower_half_iou(frames: list[Image.Image]) -> list[dict[str, int | float]]:
    masks: list[bytes] = []
    for frame in frames:
        alpha = frame.getchannel("A")
        lower = alpha.crop((0, frame.height // 2, frame.width, frame.height))
        binary = _meaningful_mask(lower).resize((64, 64), resample=Image.Resampling.NEAREST)
        masks.append(binary.tobytes())

    diagnostics: list[dict[str, int | float]] = []
    for first in range(len(masks)):
        for second in range(first + 1, len(masks)):
            intersection = 0
            union = 0
            for left, right in zip(masks[first], masks[second]):
                left_set = left != 0
                right_set = right != 0
                intersection += int(left_set and right_set)
                union += int(left_set or right_set)
            diagnostics.append(
                {
                    "frame_a": first + 1,
                    "frame_b": second + 1,
                    "iou": round(intersection / union, 6) if union else 1.0,
                }
            )
    return diagnostics


def consistency_diagnostics(frames: list[Image.Image]) -> dict:
    """Flag silhouette-mass outliers for review, never infer anatomy from height."""
    measurements = []
    for index, frame in enumerate(frames, 1):
        alpha = frame.convert("RGBA").getchannel("A")
        bbox = _meaningful_mask(alpha).getbbox()
        if bbox is None:
            _fail("CELL_EMPTY", f"review frame {index} is empty")
        measurements.append({"frame": index, "visible_area": sum(alpha.histogram()[MEANINGFUL_ALPHA:]),
                             "bbox": list(bbox), "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]})
    flagged = []
    for item in measurements:
        peers = [other["visible_area"] for other in measurements if other is not item]
        baseline = statistics.median(peers) if peers else item["visible_area"]
        ratio = item["visible_area"] / baseline
        item["area_ratio_to_peer_median"] = round(ratio, 6)
        # Four samples are the minimum for a peer comparison. Occlusion can also
        # change area, so this signal requires anatomical review, not rescaling.
        if len(measurements) >= 4 and (ratio > 1.25 or ratio < 0.80):
            flagged.append(item["frame"])
    return {"visual_review_required": True, "proves_anatomical_scale": False,
            "signal": "silhouette_area_outlier", "flagged_frames": flagged,
            "frames": measurements,
            "limitation": "Pose and occlusion affect area; compare stable anatomical anchors and adjacent poses. No flags is not visual approval."}


def _png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=9)
    return output.getvalue()


def _gif_bytes(frames: list[Image.Image], frame_ms: int, loop: bool, *, binary_alpha: bool = False, alpha_threshold: int = 128, palette_strategy: str = "median_cut") -> bytes:
    # Keep the legacy default for native manifests whose verifier compares
    # previously encoded bytes. New v3 outputs explicitly use binary opacity.
    if palette_strategy not in ("median_cut", "max_coverage"):
        raise ValueError("palette_strategy must be median_cut or max_coverage")
    if palette_strategy != "median_cut" and not binary_alpha:
        raise ValueError("max_coverage requires binary_alpha=True")
    if binary_alpha:
        if not _is_int(alpha_threshold) or not 1 <= alpha_threshold <= 255:
            raise ValueError("alpha_threshold must be an integer from 1 to 255")
        rgb_frames = []
        transparent_masks = []
        for frame in frames:
            mask = frame.getchannel("A").point(lambda alpha: 255 if alpha < alpha_threshold else 0)
            rgb = frame.convert("RGB")
            rgb.paste((0, 0, 0), mask=mask)
            rgb_frames.append(rgb)
            transparent_masks.append(mask)
        atlas = Image.new("RGB", (sum(frame.width for frame in frames), max(frame.height for frame in frames)))
        offset = 0
        for rgb in rgb_frames:
            atlas.paste(rgb, (offset, 0))
            offset += rgb.width
        # Opt in only after a demonstrated loss of rare colors: coverage can
        # increase average error in common tones. Keep historical bytes by default.
        method = Image.Quantize.MAXCOVERAGE if palette_strategy == "max_coverage" else Image.Quantize.MEDIANCUT
        palette = atlas.quantize(colors=255, method=method, dither=Image.Dither.NONE)
        colors = (palette.getpalette() + [0] * 768)[:765]
        # Index 255 duplicates 0 during quantization; remap it to 0 before
        # using it for transparency, so no opaque RGB color is ever erased.
        palette.putpalette(colors + colors[:3])
        indexed = []
        for rgb, mask in zip(rgb_frames, transparent_masks):
            frame = rgb.quantize(palette=palette, dither=Image.Dither.NONE)
            frame = frame.point(list(range(255)) + [0])
            frame.paste(255, mask=mask)
            indexed.append(frame)
        frames = indexed
    output = io.BytesIO()
    save_options: dict[str, Any] = {
        "format": "GIF",
        "save_all": True,
        "append_images": frames[1:],
        "duration": [frame_ms] * len(frames),
        "disposal": 2,
        "optimize": False,
    }
    if binary_alpha:
        save_options.update(transparency=255, background=255)
    if loop:
        save_options["loop"] = 0
    frames[0].save(output, **save_options)
    return output.getvalue()


def _translated_gif_bytes(parent_gif: bytes, transforms: list[dict], *,
                          input_canvas: tuple[int, int], output_canvas: tuple[int, int],
                          frame_ms: int, loop: bool) -> bytes:
    """Translate decoded parent GIF colors exactly, without palette quantization.

    A local palette reserves index 255 for transparency in every frame. Sources
    needing more than 255 visible RGB colors in a composed frame are rejected.
    Explicit descriptors preserve identical adjacent frames and their timing.
    """
    if (not isinstance(parent_gif,bytes) or not parent_gif or len(parent_gif)>MAX_INPUT_BYTES):
        _fail('GIF_SOURCE_INVALID','a bounded source GIF payload is required')
    if (not isinstance(transforms,list) or not 1<=len(transforms)<=16
            or type(loop) is not bool or not _is_int(frame_ms) or not 20<=frame_ms<=1000 or frame_ms%10):
        _fail('GIF_TRANSFORM_INVALID','invalid frame/timing contract')
    for size in (input_canvas,output_canvas):
        if (not isinstance(size,(tuple,list)) or len(size)!=2
                or any(not _is_int(v) or not 1<=v<=4096 for v in size)
                or size[0]*size[1]*len(transforms)>MAX_PIXELS):
            _fail('GIF_TRANSFORM_INVALID','invalid or excessive GIF canvas')
    if input_canvas[1]!=output_canvas[1]:_fail('GIF_TRANSFORM_INVALID','Y must be preserved')
    for i,t in enumerate(transforms):
        if (not isinstance(t,dict) or type(t.get('frame_index')) is not int or t['frame_index']!=i
                or not _is_int(t.get('dx')) or type(t.get('dy')) is not int or t['dy']!=0
                or type(t.get('scale')) not in (int,float) or t['scale']!=1):
            _fail('GIF_TRANSFORM_INVALID','only ordered integer X translations are supported')
    output=io.BytesIO();expected_hashes=[]
    try:
        with Image.open(io.BytesIO(parent_gif)) as source:
            if source.format!='GIF':_fail('GIF_SOURCE_INVALID','source must be GIF')
            # Pillow's initial L path can silently ignore later local palettes.
            # Reject before scanning frames or trusting any decoded source pixel.
            if source.mode=='L':
                _fail('GIF_SOURCE_INVALID','initial L-mode GIF decoding is unsupported for exact palette transport')
            if (source.size!=tuple(input_canvas) or source.n_frames!=len(transforms)
                    or (source.info.get('loop')!=0 if loop else 'loop' in source.info)):
                _fail('GIF_SOURCE_CONTRACT_MISMATCH','source GIF dimensions/count/loop differ')
            for i,transform in enumerate(transforms):
                source.seek(i)
                if source.info.get('duration')!=frame_ms:
                    _fail('GIF_SOURCE_CONTRACT_MISMATCH','source GIF frame duration differs')
                rgba=source.convert('RGBA');alpha=rgba.getchannel('A')
                if any(alpha.histogram()[1:255]):_fail('GIF_SOURCE_CONTRACT_MISMATCH','GIF alpha must be binary')
                bounds=alpha.getbbox();dx=transform['dx']
                if bounds and (bounds[0]+dx<0 or bounds[2]+dx>output_canvas[0]):
                    _fail('GIF_VISIBLE_CLIPPED','translated parent GIF pixels would be clipped')
                # Canonical hidden RGB never consumes palette capacity.
                rgba.paste((0,0,0,0),mask=alpha.point(lambda v:255 if v==0 else 0))
                colors=rgba.getcolors(maxcolors=257)
                visible=sorted(c for _,c in colors if c[3]) if colors is not None else []
                if colors is None or len(visible)>255:
                    _fail('GIF_EXACT_PALETTE_LIMIT','composed frame needs over 255 opaque RGB colors')
                palette=[channel for color in visible for channel in color[:3]]
                palette.extend([0]*(768-len(palette)))
                indices=bytearray(b'\xff')*(output_canvas[0]*output_canvas[1])
                mapping={bytes(c):j for j,c in enumerate(visible)};pixels=rgba.tobytes()
                if bounds:
                    for y in range(bounds[1],bounds[3]):
                        for x in range(bounds[0],bounds[2]):
                            offset=(y*rgba.width+x)*4
                            if pixels[offset+3]:indices[y*output_canvas[0]+x+dx]=mapping[pixels[offset:offset+4]]
                indexed=Image.frombytes('P',tuple(output_canvas),bytes(indices));indexed.putpalette(palette)
                if i==0:
                    info={'background':255,'transparency':255,'duration':frame_ms}
                    if loop:info['loop']=0
                    header,_=GifImagePlugin.getheader(indexed.copy(),info=info)
                    for block in header:output.write(block)
                for block in GifImagePlugin.getdata(indexed,duration=frame_ms,disposal=2,
                        transparency=255,include_color_table=True):output.write(block)
                expected=Image.new('RGBA',tuple(output_canvas));expected.paste(rgba,(dx,0))
                expected_hashes.append(hashlib.sha256(expected.tobytes()).digest())
        output.write(b';');payload=output.getvalue()
        with Image.open(io.BytesIO(payload)) as check:
            if (check.size!=tuple(output_canvas) or check.n_frames!=len(transforms)
                    or (check.info.get('loop')!=0 if loop else 'loop' in check.info)):
                _fail('GIF_ROUNDTRIP_MISMATCH','encoded GIF dimensions/count/loop changed')
            for i,digest in enumerate(expected_hashes):
                check.seek(i);decoded=check.convert('RGBA');alpha=decoded.getchannel('A')
                decoded.paste((0,0,0,0),mask=alpha.point(lambda v:255 if v==0 else 0))
                if check.info.get('duration')!=frame_ms or hashlib.sha256(decoded.tobytes()).digest()!=digest:
                    _fail('GIF_ROUNDTRIP_MISMATCH','encoded GIF changed visible RGB, alpha or duration')
        return payload
    except SheetImageError:raise
    except (OSError,ValueError,EOFError,IndexError,KeyError,Image.DecompressionBombError):
        _fail('GIF_SOURCE_INVALID','GIF could not be decoded or encoded safely')


def _checkerboard(size: tuple[int, int], tile: int = 8) -> Image.Image:
    image = Image.new("RGBA", size, (200, 200, 200, 255))
    draw = ImageDraw.Draw(image)
    for top in range(0, size[1], tile):
        for left in range(0, size[0], tile):
            if (left // tile + top // tile) % 2:
                draw.rectangle(
                    (left, top, min(size[0] - 1, left + tile - 1), min(size[1] - 1, top + tile - 1)),
                    fill=(235, 235, 235, 255),
                )
    return image


def _contact_image(frames: list[Image.Image], *, pixel_art: bool) -> Image.Image:
    preview_width = min(frames[0].width, 256)
    preview_height = min(frames[0].height, 256)
    preview_scale = min(preview_width / frames[0].width, preview_height / frames[0].height)
    thumbnail_size = (
        _scaled_length(frames[0].width, preview_scale),
        _scaled_length(frames[0].height, preview_scale),
    )
    thumbnail_offset = (
        (preview_width - thumbnail_size[0]) // 2,
        (preview_height - thumbnail_size[1]) // 2,
    )
    resampling = Image.Resampling.NEAREST if pixel_art else Image.Resampling.LANCZOS
    label_height = 20
    columns = min(4, len(frames))
    rows = math.ceil(len(frames) / columns)
    contact = Image.new(
        "RGBA",
        (columns * preview_width, rows * (preview_height + label_height)),
        (64, 64, 64, 255),
    )
    font = ImageFont.load_default()
    draw = ImageDraw.Draw(contact)
    for index, frame in enumerate(frames):
        column = index % columns
        row = index // columns
        left = column * preview_width
        top = row * (preview_height + label_height)
        background = _checkerboard((preview_width, preview_height))
        thumbnail = frame.resize(thumbnail_size, resample=resampling)
        background.alpha_composite(thumbnail, thumbnail_offset)
        contact.alpha_composite(background, (left, top))
        label = f"{index + 1:02d}"
        draw.text((left + 5, top + preview_height + 3), label, fill=(255, 255, 255, 255), font=font)
    return contact


def _validate_encoded_artifacts(
    frame_bytes: list[bytes],
    sheet_bytes: bytes,
    gif_bytes: bytes,
    contact_bytes: bytes,
    *,
    frame_count: int,
    canvas: tuple[int, int],
    frame_ms: int,
    loop: bool,
) -> tuple[list[int], list[int], list[int]]:
    for encoded in frame_bytes:
        with Image.open(io.BytesIO(encoded)) as frame:
            if frame.mode != "RGBA" or frame.size != canvas:
                _fail("OUTPUT_CONFLICT", "encoded frame failed validation")
    with Image.open(io.BytesIO(sheet_bytes)) as sheet:
        sheet_dimensions = list(sheet.size)
        if sheet.mode != "RGBA" or sheet.size != (canvas[0] * frame_count, canvas[1]):
            _fail("OUTPUT_CONFLICT", "encoded sprite sheet failed validation")
    durations: list[int] = []
    with Image.open(io.BytesIO(gif_bytes)) as preview:
        if preview.n_frames != frame_count:
            _fail("OUTPUT_CONFLICT", "encoded preview frame count is incorrect")
        if loop and preview.info.get("loop") != 0:
            _fail("OUTPUT_CONFLICT", "encoded preview loop setting is incorrect")
        if not loop and "loop" in preview.info:
            _fail("OUTPUT_CONFLICT", "nonloop preview contains a loop extension")
        for index in range(frame_count):
            preview.seek(index)
            duration = preview.info.get("duration")
            if duration != frame_ms:
                _fail("OUTPUT_CONFLICT", "encoded preview duration is incorrect")
            durations.append(duration)
    with Image.open(io.BytesIO(contact_bytes)) as contact:
        contact_dimensions = list(contact.size)
    return sheet_dimensions, durations, contact_dimensions


def _write_new_or_identical(payloads: dict[Path, bytes]) -> None:
    for path, payload in payloads.items():
        if path.exists():
            if not path.is_file():
                _fail("OUTPUT_CONFLICT", f"artifact target {path.name} is not a file")
            try:
                existing = path.read_bytes()
            except OSError as exc:
                _fail("OUTPUT_CONFLICT", f"artifact target {path.name} is unreadable: {exc.__class__.__name__}")
            if existing != payload:
                _fail("OUTPUT_CONFLICT", f"artifact target {path.name} has different content")

    for path, payload in payloads.items():
        if path.exists():
            continue
        try:
            with path.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            if path.read_bytes() != payload:
                _fail("OUTPUT_CONFLICT", f"artifact target {path.name} changed during write")
        except OSError as exc:
            _fail("OUTPUT_CONFLICT", f"artifact target {path.name} could not be written: {exc.__class__.__name__}")


def assemble_sheet(
    raw_path: str | Path,
    output_dir: str | Path,
    *,
    frame_count: int,
    columns: int,
    rows: int,
    canvas: tuple[int, int],
    frame_ms: int = 100,
    loop: bool = True,
    pixel_art: bool = True,
    grounded: bool = True,
) -> dict:
    source, destination, canvas_width, canvas_height = _validate_arguments(
        raw_path,
        output_dir,
        frame_count=frame_count,
        columns=columns,
        rows=rows,
        canvas=canvas,
        frame_ms=frame_ms,
        loop=loop,
        pixel_art=pixel_art,
        grounded=grounded,
    )
    source_image, source_bytes = _load_source(source)
    cells, cell_bounds = _partition_cells(source_image, frame_count, columns, rows)
    frames, scale = _normalize_cells(
        cells,
        canvas_width,
        canvas_height,
        pixel_art=pixel_art,
        grounded=grounded,
    )
    _reject_duplicates(frames)

    sheet = Image.new("RGBA", (canvas_width * frame_count, canvas_height), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, (index * canvas_width, 0))
    contact = _contact_image(frames, pixel_art=pixel_art)

    encoded_frames = [_png_bytes(frame) for frame in frames]
    encoded_sheet = _png_bytes(sheet)
    encoded_gif = _gif_bytes(frames, frame_ms, loop, binary_alpha=True)
    encoded_contact = _png_bytes(contact)
    sheet_dimensions, durations, contact_dimensions = _validate_encoded_artifacts(
        encoded_frames,
        encoded_sheet,
        encoded_gif,
        encoded_contact,
        frame_count=frame_count,
        canvas=(canvas_width, canvas_height),
        frame_ms=frame_ms,
        loop=loop,
    )

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _fail("OUTPUT_CONFLICT", f"output destination could not be created: {exc.__class__.__name__}")
    frame_paths = [destination / f"frame-{index + 1:02d}.png" for index in range(frame_count)]
    sheet_path = destination / "sprite-sheet.png"
    gif_path = destination / "preview.gif"
    contact_path = destination / "contact.png"
    qa_path = destination / "qa.json"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    hashes = {
        "sheet": hashlib.sha256(encoded_sheet).hexdigest(),
        "frames": [hashlib.sha256(payload).hexdigest() for payload in encoded_frames],
        "preview_gif": hashlib.sha256(encoded_gif).hexdigest(),
        "contact": hashlib.sha256(encoded_contact).hexdigest(),
    }
    qa: dict[str, Any] = {
        "state": "READY_FOR_VISUAL_REVIEW",
        "semantic_review_required": True,
        "delivery_allowed": False,
        "consistency": consistency_diagnostics(frames),
        "source_sha256": source_sha256,
        "source_dimensions": [source_image.width, source_image.height],
        "frame_count": frame_count,
        "frame_ms": frame_ms,
        "loop": loop,
        "pixel_art": pixel_art,
        "grounded": grounded,
        "canvas": [canvas_width, canvas_height],
        "sheet_dimensions": sheet_dimensions,
        "contact_dimensions": contact_dimensions,
        "preview": {"frame_count": frame_count, "durations_ms": durations, "loop": loop,
                    "alpha_threshold": 128, "palette": "shared_255_colors"},
        "grid": {"columns": columns, "rows": rows},
        "cell_bounds": cell_bounds,
        "normalization": {
            "scale": scale,
            "max_meaningful_bbox": [
                max(cell.meaningful_bbox[2] - cell.meaningful_bbox[0] for cell in cells),
                max(cell.meaningful_bbox[3] - cell.meaningful_bbox[1] for cell in cells),
            ],
            "ground_baseline": math.floor(0.90 * canvas_height) if grounded else None,
        },
        "frames": [
            {
                "ordinal": cell.index + 1,
                "dimensions": [canvas_width, canvas_height],
                "source_cell_bounds": list(cell.bounds),
                "meaningful_bbox": list(cell.meaningful_bbox),
                "crop_bbox": list(cell.crop_bbox),
                "ignored_low_alpha_outliers": cell.ignored_low_alpha_pixels,
            }
            for cell in cells
        ],
        "lower_half_mask_iou": _lower_half_iou(frames),
        "sha256": hashes,
    }
    encoded_qa = (json.dumps(qa, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    payloads = {
        **{path: payload for path, payload in zip(frame_paths, encoded_frames)},
        sheet_path: encoded_sheet,
        gif_path: encoded_gif,
        contact_path: encoded_contact,
        qa_path: encoded_qa,
    }
    _write_new_or_identical(payloads)
    try:
        _validate_encoded_artifacts(
            [path.read_bytes() for path in frame_paths],
            sheet_path.read_bytes(),
            gif_path.read_bytes(),
            contact_path.read_bytes(),
            frame_count=frame_count,
            canvas=(canvas_width, canvas_height),
            frame_ms=frame_ms,
            loop=loop,
        )
        json.loads(qa_path.read_text("utf-8"))
    except SheetImageError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("OUTPUT_CONFLICT", f"written artifact failed reopening: {exc.__class__.__name__}")

    artifact_paths = {
        "sheet": str(sheet_path),
        "frames": [str(path) for path in frame_paths],
        "preview_gif": str(gif_path),
        "contact": str(contact_path),
        "qa_report": str(qa_path),
    }
    return {
        "state": "READY_FOR_VISUAL_REVIEW",
        "artifact_paths": artifact_paths,
        "qa": qa,
        "source_sha256": source_sha256,
    }
