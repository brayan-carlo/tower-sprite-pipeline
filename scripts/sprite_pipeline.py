import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, __version__ as PILLOW_VERSION


REQUIRED_FIELDS = (
    "project_root",
    "source_path",
    "entity",
    "action",
    "direction",
    "frame_count",
    "frame_width",
    "frame_height",
    "anchor",
    "max_budget_usd",
    "primary_model",
    "fallback_model",
)
PILOT_VALUES = {
    "entity": "ogre",
    "action": "walk",
    "direction": "S",
    "frame_count": 6,
    "frame_width": 256,
    "frame_height": 416,
    "anchor": "bottom-center",
    "primary_model": "fal-ai/kling-image/o1",
    "fallback_model": "fal-ai/nano-banana-2/edit",
}
STRUCTURAL_GUIDE_FIELDS = (
    "six_complete_bodies",
    "distinct_phases",
    "south_orientation",
    "no_duplicated_limbs",
    "single_row",
)
RUN_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-ogre-walk-s$")
FRAME_SIZE = (256, 416)
BOTTOM_MARGIN = 1
POSES = (
    {"bounce": 0, "leftArm": (0, 0, 0), "rightArm": (0, 0, 0), "leftLeg": (0, 0, 0), "rightLeg": (0, 0, 0)},
    {"bounce": 3, "leftArm": (-8, -4, -1), "rightArm": (8, 4, 1), "leftLeg": (8, 5, -2), "rightLeg": (-8, -5, 2)},
    {"bounce": 6, "leftArm": (-4, -2, 0), "rightArm": (4, 2, 0), "leftLeg": (4, 3, -1), "rightLeg": (-4, -3, 1)},
    {"bounce": 0, "leftArm": (8, 4, 2), "rightArm": (-8, -4, -2), "leftLeg": (-8, -5, 2), "rightLeg": (8, 5, -2)},
    {"bounce": 3, "leftArm": (8, 4, 1), "rightArm": (-8, -4, -1), "leftLeg": (-8, -6, 3), "rightLeg": (8, 6, -3)},
    {"bounce": 6, "leftArm": (4, 2, 0), "rightArm": (-4, -2, 0), "leftLeg": (-4, -3, 1), "rightLeg": (4, 3, -1)},
)


class PipelineBlocked(Exception):
    pass


def emit(payload, exit_code=0):
    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineBlocked(f"cannot read JSON {Path(path)}: {error}") from error


def _is_within(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_manifest(path):
    manifest = read_json(path)
    errors = []
    if not isinstance(manifest, dict):
        return {}, ["manifest must be a JSON object"]
    for field in REQUIRED_FIELDS:
        if field not in manifest:
            errors.append(f"missing required field: {field}")
    if errors:
        return manifest, errors
    for field, expected in PILOT_VALUES.items():
        if manifest[field] != expected:
            errors.append(f"incompatible {field}: expected {expected!r}, got {manifest[field]!r}")
    budget = manifest["max_budget_usd"]
    if not isinstance(budget, (int, float)) or isinstance(budget, bool):
        errors.append("max_budget_usd must be numeric")
    elif budget <= 0 or budget > 0.17:
        errors.append("max_budget_usd must be greater than 0 and no more than 0.17")
    project_root = Path(manifest["project_root"]).expanduser()
    source_relative = Path(manifest["source_path"])
    if source_relative.is_absolute():
        errors.append("source_path must be relative to project_root")
    else:
        source = (project_root / source_relative).resolve()
        if not _is_within(source, project_root):
            errors.append("source_path escapes project_root")
        elif not source.is_file():
            errors.append(f"source does not exist: {source}")
    normalized = dict(manifest)
    normalized["project_root"] = project_root.resolve().as_posix()
    normalized["source_path"] = source_relative.as_posix()
    return normalized, errors


def runtime_versions():
    node_executable = os.environ.get("TOWER_SPRITE_NODE") or shutil.which("node")
    node_version = "not-found"
    if node_executable:
        process = subprocess.run(
            [node_executable, "--version"], capture_output=True, text=True, check=False
        )
        if process.returncode == 0:
            node_version = process.stdout.strip()
    return {
        "python": sys.version.split()[0],
        "pillow": PILLOW_VERSION,
        "node": node_version,
    }


def _new_run_manifest(manifest, run_id, run_dir):
    return {
        "run_id": run_id,
        "workspace": run_dir.resolve().as_posix(),
        "normalized_input": manifest,
        "runtime_versions": runtime_versions(),
        "model": manifest["primary_model"],
        "request_ids": [],
        "attempts": 0,
        "fal_requests": [],
        "estimated_cost_usd": 0.0,
        "guide_accepted": False,
        "state": "DRAFT",
        "automatic_actions": [],
    }


def prepare_workspace(manifest, workspace_root, run_id):
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise PipelineBlocked("run-id must use YYYYMMDD-HHMMSS-ogre-walk-s")
    project_root = Path(manifest["project_root"]).resolve()
    root = Path(workspace_root).expanduser().resolve()
    if _is_within(root, project_root):
        raise PipelineBlocked(f"workspace must be outside project_root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / run_id
    if run_dir.exists():
        raise PipelineBlocked(f"run directory already exists and cannot be reused: {run_dir}")
    run_dir.mkdir()
    for name in ("input", "raw", "normalized", "previews", "reports"):
        (run_dir / name).mkdir()
    source = (project_root / manifest["source_path"]).resolve()
    shutil.copy2(source, run_dir / "input" / source.name)
    run_manifest = _new_run_manifest(manifest, run_id, run_dir)
    write_json(run_dir / "reports" / "run_manifest.json", run_manifest, run_dir)
    return run_dir, run_manifest


def write_json(path, payload, run_dir=None):
    destination = Path(path).resolve()
    if run_dir is not None and not _is_within(destination, Path(run_dir)):
        raise PipelineBlocked(f"destination escapes workspace: {destination}")
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def polygon_mask(size, points):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon([tuple(point) for point in points], fill=255)
    return mask


def extract_layers(source, rig):
    alpha = source.getchannel("A")
    masks = {name: polygon_mask(source.size, points) for name, points in rig["layers"].items()}
    union = Image.new("L", source.size, 0)
    for mask in masks.values():
        union = ImageChops.lighter(union, mask)
    core = source.copy()
    core.putalpha(ImageChops.multiply(alpha, ImageChops.invert(union)))
    layers = {"core": core}
    for name, mask in masks.items():
        layer = source.copy()
        layer.putalpha(ImageChops.multiply(alpha, mask))
        layers[name] = layer
    return layers


def translate(image, dx, dy):
    return image.transform(
        image.size,
        Image.Transform.AFFINE,
        (1, 0, -dx, 0, 1, -dy),
        resample=Image.Resampling.NEAREST,
    )


def transform_layer(layer, pivot, angle, dx, dy):
    rotated = layer.rotate(
        angle,
        resample=Image.Resampling.NEAREST,
        center=tuple(pivot),
        expand=False,
    )
    return translate(rotated, dx, dy)


def overlay_exact(base, top):
    mask = top.getchannel("A").point(lambda alpha: 255 if alpha else 0)
    base.paste(top, (0, 0), mask)


def source_offset(source):
    bbox = source.getbbox()
    if bbox is None:
        raise PipelineBlocked("source sprite is empty")
    return (FRAME_SIZE[0] - source.width) // 2, FRAME_SIZE[1] - BOTTOM_MARGIN - bbox[3]


def normalize_source(source):
    frame = Image.new("RGBA", FRAME_SIZE, (0, 0, 0, 0))
    frame.alpha_composite(source, source_offset(source))
    return frame


def build_frames(source, rig):
    if source.size != tuple(rig["sourceSize"]):
        raise PipelineBlocked(
            f"source size {source.size} does not match rig sourceSize {tuple(rig['sourceSize'])}"
        )
    if tuple(rig["frameSize"]) != FRAME_SIZE or rig["anchor"] != "bottom-center":
        raise PipelineBlocked("rig frameSize/anchor is incompatible with the pilot")
    layers = extract_layers(source, rig)
    base = normalize_source(source)
    base_x, base_y = source_offset(source)
    frames = [base]
    for pose in POSES[1:]:
        source_frame = Image.new("RGBA", source.size, (0, 0, 0, 0))
        for name in rig["layerOrder"]:
            layer = layers[name]
            if name == "core":
                transformed = translate(layer, 0, pose["bounce"])
            else:
                angle, dx, dy = pose[name]
                leg_bounce_compensation = -pose["bounce"] if name.endswith("Leg") else 0
                transformed = transform_layer(
                    layer,
                    rig["pivots"][name],
                    angle,
                    dx,
                    dy + pose["bounce"] + leg_bounce_compensation,
                )
            overlay_exact(source_frame, transformed)
        frame = Image.new("RGBA", FRAME_SIZE, (0, 0, 0, 0))
        frame.alpha_composite(source_frame, (base_x, base_y))
        base_alpha = base.getchannel("A")
        for y in range(FRAME_SIZE[1] - BOTTOM_MARGIN, FRAME_SIZE[1]):
            for x in range(FRAME_SIZE[0]):
                frame.putpixel((x, y), (0, 0, 0, 0))
        for x in range(FRAME_SIZE[0]):
            if base_alpha.getpixel((x, FRAME_SIZE[1] - BOTTOM_MARGIN - 1)):
                frame.putpixel((x, FRAME_SIZE[1] - BOTTOM_MARGIN - 1), base.getpixel((x, FRAME_SIZE[1] - BOTTOM_MARGIN - 1)))
        if frame.getchannel("A").getextrema()[1] == 0:
            raise PipelineBlocked("deterministic composition produced an empty frame")
        frames.append(frame)
    return frames


def compose_sheet(frames):
    sheet = Image.new("RGBA", (FRAME_SIZE[0] * len(frames), FRAME_SIZE[1]), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        sheet.alpha_composite(frame, (index * FRAME_SIZE[0], 0))
    return sheet


def create_contact_sheet(frames):
    label_height = 28
    contact = Image.new("RGBA", (FRAME_SIZE[0] * 6, FRAME_SIZE[1] + label_height), (30, 30, 30, 255))
    draw = ImageDraw.Draw(contact)
    for index, frame in enumerate(frames):
        x = index * FRAME_SIZE[0]
        contact.alpha_composite(frame, (x, label_height))
        draw.text((x + 8, 7), str(index + 1), fill=(255, 255, 255, 255))
    return contact


def difference_score(left, right):
    difference = ImageChops.difference(left, right)
    return sum(
        1
        for pixel in difference.get_flattened_data()
        if pixel != (0, 0, 0, 0)
    )


def required_layer_coverage(source, rig):
    results = []
    source_alpha = source.getchannel("A")
    layer_masks = {
        name: polygon_mask(source.size, points)
        for name, points in rig.get("layers", {}).items()
    }
    for requirement in rig.get("requiredLayerCoverage", []):
        layer_name = requirement.get("layer")
        region_points = requirement.get("region", [])
        maximum = requirement.get("maxUncoveredOpaquePixels", 0)
        if layer_name not in layer_masks or len(region_points) < 3 or not isinstance(maximum, int):
            results.append(
                {
                    "layer": layer_name,
                    "uncoveredOpaquePixels": None,
                    "maxUncoveredOpaquePixels": maximum,
                    "passed": False,
                }
            )
            continue
        region_mask = polygon_mask(source.size, region_points)
        uncovered = ImageChops.multiply(
            ImageChops.multiply(source_alpha, region_mask),
            ImageChops.invert(layer_masks[layer_name]),
        )
        uncovered_pixels = sum(1 for alpha in uncovered.get_flattened_data() if alpha)
        results.append(
            {
                "layer": layer_name,
                "uncoveredOpaquePixels": uncovered_pixels,
                "maxUncoveredOpaquePixels": maximum,
                "passed": uncovered_pixels <= maximum,
            }
        )
    return {
        "passed": all(result["passed"] for result in results),
        "results": results,
    }


def alpha_centroid(image):
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return None
    pixels = alpha.load()
    total = 0
    weighted_x = 0
    weighted_y = 0
    for y in range(bbox[1], bbox[3]):
        for x in range(bbox[0], bbox[2]):
            value = pixels[x, y]
            total += value
            weighted_x += x * value
            weighted_y += y * value
    return weighted_x / total, weighted_y / total


def minimum_limb_excursion(source, rig):
    layers = extract_layers(source, rig)
    results = []
    for layer_name, minimum in rig.get("minimumLimbExcursionPixels", {}).items():
        if (
            layer_name not in layers
            or layer_name == "core"
            or layer_name not in rig.get("pivots", {})
            or any(layer_name not in pose for pose in POSES)
            or not isinstance(minimum, (int, float))
            or minimum < 0
        ):
            results.append(
                {
                    "layer": layer_name,
                    "actualPixels": None,
                    "minimumPixels": minimum,
                    "passed": False,
                }
            )
            continue
        centroids = []
        for pose in POSES:
            angle, dx, dy = pose[layer_name]
            leg_bounce_compensation = -pose["bounce"] if layer_name.endswith("Leg") else 0
            transformed = transform_layer(
                layers[layer_name],
                rig["pivots"][layer_name],
                angle,
                dx,
                dy + pose["bounce"] + leg_bounce_compensation,
            )
            centroid = alpha_centroid(transformed)
            if centroid is not None:
                centroids.append(centroid)
        actual = max(
            (math.dist(left, right) for left in centroids for right in centroids),
            default=0.0,
        )
        results.append(
            {
                "layer": layer_name,
                "actualPixels": round(actual, 3),
                "minimumPixels": minimum,
                "passed": actual >= minimum,
            }
        )
    return {
        "passed": all(result["passed"] for result in results),
        "results": results,
    }


def technical_qa(frames, sheet, source, rig, manifest, run_dir, estimated_cost_usd=0.0):
    frame_hashes = [hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames]
    bboxes = [frame.getbbox() for frame in frames]
    consecutive = [difference_score(a, b) for a, b in zip(frames, frames[1:])]
    closing = difference_score(frames[-1], frames[0])
    source_colors = set(source.get_flattened_data())
    output_colors = {
        pixel
        for frame in frames
        for pixel in frame.get_flattened_data()
        if pixel[3]
    }
    checks = {
        "sheet_size": {"passed": sheet.size == (1536, 416), "actual": list(sheet.size)},
        "cell_sizes": {"passed": all(frame.size == FRAME_SIZE for frame in frames)},
        "six_nonempty_cells": {"passed": len(frames) == 6 and all(bboxes)},
        "real_alpha": {"passed": all(frame.getchannel("A").getextrema() == (0, 255) for frame in frames)},
        "bottom_center_ground_line": {"passed": all(bbox and bbox[3] == 415 for bbox in bboxes), "bboxes": [list(bbox) for bbox in bboxes]},
        "frame_one_exact": {"passed": frames[0].tobytes() == normalize_source(source).tobytes()},
        "six_unique_hashes": {"passed": len(set(frame_hashes)) == 6, "hashes": frame_hashes},
        "locked_identity_source_pixels": {"passed": output_colors.issubset(source_colors)},
        "required_layer_coverage": required_layer_coverage(source, rig),
        "minimum_limb_excursion": minimum_limb_excursion(source, rig),
        "consecutive_pose_differences": {"passed": all(score > 0 for score in consecutive), "scores": consecutive},
        "loop_closed": {"passed": closing <= max(consecutive), "closing_score": closing, "largest_consecutive_score": max(consecutive)},
        "workspace_confined": {"passed": not _is_within(Path(run_dir), Path(manifest["project_root"]))},
        "budget": {
            "passed": 0.0 <= estimated_cost_usd <= manifest["max_budget_usd"] <= 0.17,
            "estimated_cost_usd": estimated_cost_usd,
        },
    }
    result = "PASS" if all(check["passed"] for check in checks.values()) else "FAIL"
    return {"technical_result": result, "checks": checks}


def finalize_run(manifest, rig, guide, run_dir, run_manifest, estimated_cost_usd):
    guide_failures = [field for field in STRUCTURAL_GUIDE_FIELDS if guide.get(field) is not True]
    if guide_failures:
        run_manifest["state"] = "GUIDE_REJECTED"
        run_manifest["guide_accepted"] = False
        run_manifest["guide_failures"] = guide_failures
        qa = {
            "technical_result": "NOT_RUN",
            "guide_structural_result": "FAIL",
            "failed_checks": guide_failures,
        }
        write_json(run_dir / "reports" / "qa_report.json", qa, run_dir)
        write_json(run_dir / "reports" / "run_manifest.json", run_manifest, run_dir)
        return {"state": "GUIDE_REJECTED", "failed_checks": guide_failures}, 3
    if guide.get("pose_extraction_reliable") is not False:
        run_manifest["state"] = "TECHNICAL_REJECTED"
        run_manifest["guide_accepted"] = True
        run_manifest["reason"] = "offline fixture must explicitly select unreliable pose extraction"
        write_json(run_dir / "reports" / "run_manifest.json", run_manifest, run_dir)
        return {"state": "TECHNICAL_REJECTED", "reason": run_manifest["reason"]}, 3
    source = Image.open(Path(manifest["project_root"]) / manifest["source_path"]).convert("RGBA")
    frames = build_frames(source, rig)
    sheet = compose_sheet(frames)
    sheet_path = run_dir / "normalized" / "Ogre_Walk_S_6f_256x416.png"
    sheet.save(sheet_path)
    create_contact_sheet(frames).save(run_dir / "previews" / "contact_sheet.png")
    frames[0].save(
        run_dir / "previews" / "loop.gif",
        save_all=True,
        append_images=frames[1:],
        duration=140,
        loop=0,
        disposal=2,
    )
    qa = technical_qa(
        frames,
        sheet,
        source,
        rig,
        manifest,
        run_dir,
        estimated_cost_usd=estimated_cost_usd,
    )
    state = "READY_FOR_VISUAL_REVIEW" if qa["technical_result"] == "PASS" else "TECHNICAL_REJECTED"
    run_manifest["state"] = state
    run_manifest["guide_accepted"] = True
    run_manifest["composition_mode"] = "deterministic_fallback_after_structural_pass"
    write_json(run_dir / "reports" / "qa_report.json", qa, run_dir)
    write_json(run_dir / "reports" / "run_manifest.json", run_manifest, run_dir)
    return {"state": state, "run_dir": run_dir.resolve().as_posix()}, 0 if state == "READY_FOR_VISUAL_REVIEW" else 3


def run_offline(args, manifest):
    rig = read_json(args.rig_manifest)
    guide = read_json(args.guide_fixture)
    run_dir, run_manifest = prepare_workspace(manifest, args.workspace_root, args.run_id)
    return finalize_run(manifest, rig, guide, run_dir, run_manifest, estimated_cost_usd=0.0)


def run_prepared(args, manifest):
    run_manifest_path = Path(args.run_manifest).expanduser().resolve()
    run_manifest = read_json(run_manifest_path)
    run_dir = Path(run_manifest.get("workspace", "")).expanduser().resolve()
    expected_manifest = (run_dir / "reports" / "run_manifest.json").resolve()
    project_root = Path(manifest["project_root"]).resolve()
    if (
        run_manifest_path != expected_manifest
        or not run_dir.is_dir()
        or _is_within(run_dir, project_root)
        or run_manifest.get("run_id") != run_dir.name
    ):
        raise PipelineBlocked("prepared run manifest is outside its authorized workspace")
    normalized_input = run_manifest.get("normalized_input")
    if normalized_input != manifest:
        raise PipelineBlocked("prepared run manifest does not match the validated manifest")
    rig = read_json(args.rig_manifest)
    guide = read_json(args.guide_assessment)
    estimated_cost = float(run_manifest.get("estimated_cost_usd", 0.0))
    return finalize_run(
        manifest,
        rig,
        guide,
        run_dir,
        run_manifest,
        estimated_cost_usd=estimated_cost,
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Offline directional sprite pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--workspace-root", required=True)
    prepare.add_argument("--run-id", required=True)
    offline = subparsers.add_parser("run-offline")
    offline.add_argument("--manifest", required=True)
    offline.add_argument("--rig-manifest", required=True)
    offline.add_argument("--guide-fixture", required=True)
    offline.add_argument("--workspace-root", required=True)
    offline.add_argument("--run-id", required=True)
    prepared = subparsers.add_parser("run-prepared")
    prepared.add_argument("--manifest", required=True)
    prepared.add_argument("--run-manifest", required=True)
    prepared.add_argument("--rig-manifest", required=True)
    prepared.add_argument("--guide-assessment", required=True)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--run-manifest", required=True)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command == "resume":
            run_manifest = read_json(args.run_manifest)
            return emit(
                {
                    "state": run_manifest["state"],
                    "run_id": run_manifest["run_id"],
                    "automatic_actions": [],
                }
            )
        manifest, errors = validate_manifest(args.manifest)
        if errors:
            return emit({"state": "BLOCKED", "errors": errors}, 2)
        if args.command == "validate":
            return emit({"state": "VALID", "manifest": manifest})
        if args.command == "prepare":
            run_dir, run_manifest = prepare_workspace(manifest, args.workspace_root, args.run_id)
            return emit({"state": run_manifest["state"], "run_dir": run_dir.resolve().as_posix()})
        if args.command == "run-prepared":
            payload, exit_code = run_prepared(args, manifest)
        else:
            payload, exit_code = run_offline(args, manifest)
        return emit(payload, exit_code)
    except PipelineBlocked as error:
        return emit({"state": "BLOCKED", "errors": [str(error)]}, 2)
    except (KeyError, TypeError, ValueError, OSError) as error:
        return emit({"state": "BLOCKED", "errors": [f"invalid pipeline input: {error}"]}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
