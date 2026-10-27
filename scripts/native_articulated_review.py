"""Pure validation of articulated source and visual-review evidence."""
import math

from PIL import Image

from generic_sprite_workflow import _validate_visual_report
from native_components import apply_component_review
from sheet_workflow_state import WorkflowError


def _fail(code):
    raise WorkflowError(code)


def _notes(value):
    return isinstance(value, str) and 10 <= len(value.strip()) <= 4000


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point(value, width, height, *, visible_image=None, nullable=False, raw_bounds=False):
    if nullable and value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(_number(item) for item in value)):
        _fail("SOURCE_GEOMETRY_INVALID")
    point = [value[0], value[1]]
    if raw_bounds and not (0 <= point[0] < width and 0 <= point[1] < height):
        _fail("SOURCE_GEOMETRY_INVALID")
    x, y = round(point[0]), round(point[1])
    if not (0 <= x < width and 0 <= y < height):
        _fail("SOURCE_GEOMETRY_INVALID")
    if visible_image is not None and visible_image.getpixel((x, y))[3] < 16:
        _fail("SOURCE_GEOMETRY_INVISIBLE")
    return point


def _distance(points):
    return math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1])


def validate_source_geometry(source_image, source_sha256, geometry):
    """Validate explicit source landmarks and return normalized measurements."""
    if not isinstance(source_image, Image.Image) or source_image.mode != "RGBA":
        _fail("SOURCE_GEOMETRY_INVALID")
    width, height = source_image.size
    if (not isinstance(source_sha256, str) or len(source_sha256) != 64
            or any(c not in "0123456789abcdef" for c in source_sha256)):
        _fail("SOURCE_GEOMETRY_INVALID")
    if not isinstance(geometry, dict):
        _fail("SOURCE_GEOMETRY_INVALID")
    if geometry.get("source_sha256") != source_sha256:
        _fail("SOURCE_GEOMETRY_STALE")
    if not _notes(geometry.get("notes")):
        _fail("SOURCE_GEOMETRY_INVALID")
    uncertainty = geometry.get("uncertainty_pixels")
    if not _number(uncertainty) or uncertainty < 0:
        _fail("SOURCE_GEOMETRY_INVALID")
    anchors = geometry.get("anchors")
    if not isinstance(anchors, list) or len(anchors) != 2:
        _fail("SOURCE_GEOMETRY_INVALID")
    names, lengths = [], {}
    for anchor in anchors:
        if not isinstance(anchor, dict) or not _notes(anchor.get("notes")):
            _fail("SOURCE_GEOMETRY_INVALID")
        name = anchor.get("name")
        if not isinstance(name, str) or not name.strip() or name.strip().casefold() in {n.casefold() for n in names}:
            _fail("SOURCE_GEOMETRY_INVALID")
        points = anchor.get("points")
        if not isinstance(points, list) or len(points) != 2:
            _fail("SOURCE_GEOMETRY_INVALID")
        normalized = [_point(point, width, height, visible_image=source_image) for point in points]
        length = _distance(normalized)
        if length < 4:
            _fail("SOURCE_GEOMETRY_INVALID")
        clean_name = name.strip()
        names.append(clean_name)
        lengths[clean_name] = length
    body_anchor = _point(geometry.get("body_anchor"), width, height, visible_image=source_image)
    pivot = _point(geometry.get("pivot"), width, height, raw_bounds=True)
    held_items = geometry.get("held_items")
    if not isinstance(held_items, list):
        _fail("SOURCE_GEOMETRY_INVALID")
    held_ids = []
    for item in held_items:
        if not isinstance(item, dict) or not _notes(item.get("notes")):
            _fail("SOURCE_GEOMETRY_INVALID")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier.strip() in held_ids:
            _fail("SOURCE_GEOMETRY_INVALID")
        held_ids.append(identifier.strip())
    return {
        "source_size": [width, height], "source_sha256": source_sha256,
        "anchor_names": names, "anchor_lengths": lengths,
        "body_anchor": body_anchor, "pivot": pivot,
        "uncertainty_pixels": uncertainty, "held_item_ids": held_ids,
    }


def _review_point(value, image, *, nullable=False):
    try:
        return _point(value, image.width, image.height, visible_image=image, nullable=nullable)
    except WorkflowError:
        _fail("ARTICULATED_GEOMETRY_INVALID")


def _ordered(records, key="frame"):
    return isinstance(records, list) and len(records) == 8 and [r.get(key) if isinstance(r, dict) else None for r in records] == list(range(1, 9))


def _equipment(records, held_ids, *, transition=False):
    if not isinstance(records, list):
        _fail("EQUIPMENT_EVIDENCE_INVALID")
    identifiers = [r.get("id") if isinstance(r, dict) else None for r in records]
    if (any(not isinstance(identifier, str) for identifier in identifiers)
            or len(identifiers) != len(set(identifiers)) or set(identifiers) != set(held_ids)):
        _fail("EQUIPMENT_EVIDENCE_INVALID")
    for record in records:
        allowed = {"pass", "fail"} if transition else {"pass", "fail"}
        status = record.get("status") if transition else record.get("rigidity")
        if not isinstance(status, str) or status not in allowed or not _notes(record.get("notes")):
            _fail("EQUIPMENT_EVIDENCE_INVALID")
        attachment = record.get("attachment")
        if not transition and (not isinstance(attachment, str) or attachment not in {"visible", "occluded"}):
            _fail("EQUIPMENT_EVIDENCE_INVALID")
        if status != "pass":
            _fail("EQUIPMENT_EVIDENCE_INVALID")


def validate_review(context, report, source_image, source_geometry, frames, component_reports):
    """Validate a review against current buffers and return a derived summary."""
    if not isinstance(context, dict):
        _fail("VISUAL_REVIEW_INVALID")
    source_summary = validate_source_geometry(source_image, context.get("source_sha256"), source_geometry)
    if (not isinstance(report, dict) or not isinstance(report.get("decision"), str)
            or not isinstance(report.get("checks"), dict)
            or any(not isinstance(check, dict) or not isinstance(check.get("status"), str)
                   for check in report["checks"].values())):
        _fail("VISUAL_REVIEW_INVALID")
    _validate_visual_report(context, report)
    if report["decision"] == "reject":
        return {"decision": "reject", "source_geometry": source_summary}
    request = context.get("request")
    if (not isinstance(request, dict) or request.get("frames") != 8
            or request.get("action") not in ("walk", "attack") or not isinstance(request.get("orientation"), str)
            or request.get("orientation") not in {"left", "right"}
            or type(request.get("frame_ms")) is not int or request["frame_ms"] <= 0
            or not isinstance(request.get("canvas"), list) or len(request["canvas"]) != 2
            or any(type(value) is not int or value <= 0 for value in request["canvas"])
            or request.get("loop") is not (request.get("action") == "walk")):
        _fail("ARTICULATED_REVIEW_SCOPE_INVALID")
    if (not isinstance(frames, list) or len(frames) != 8
            or any(not isinstance(frame, Image.Image) or frame.mode != "RGBA" or list(frame.size) != request["canvas"] for frame in frames)):
        _fail("ARTICULATED_GEOMETRY_INVALID")

    attack = request["action"] == "attack"
    geometry = report.get("geometry")
    if (not isinstance(geometry, dict) or (not attack and
            (not isinstance(geometry.get("first_support_limb"), str)
             or geometry.get("first_support_limb") not in {"near", "far"}))):
        _fail("ARTICULATED_GEOMETRY_INVALID")
    try:
        pivot = _point(geometry.get("pivot"), request["canvas"][0], request["canvas"][1], raw_bounds=True)
    except WorkflowError:
        _fail("ARTICULATED_GEOMETRY_INVALID")
    records = geometry.get("frames")
    if not _ordered(records):
        _fail("ARTICULATED_GEOMETRY_INVALID")
    first = None if attack else geometry["first_support_limb"]
    other = "far" if first == "near" else "near"
    lane_y = {"near": [], "far": []}
    support_x = {"near": [], "far": []}
    for index, (record, image) in enumerate(zip(records, frames), 1):
        if not _notes(record.get("notes")):
            _fail("ARTICULATED_GEOMETRY_INVALID")
        anchors = record.get("anchors")
        if not isinstance(anchors, list) or len(anchors) != 2:
            _fail("ARTICULATED_GEOMETRY_INVALID")
        anchor_names = [a.get("name", "").strip() if isinstance(a, dict) and isinstance(a.get("name"), str) else None for a in anchors]
        if len(anchor_names) != len(set(anchor_names)) or set(anchor_names) != set(source_summary["anchor_names"]):
            _fail("ARTICULATED_GEOMETRY_INVALID")
        for anchor in anchors:
            if not _notes(anchor.get("notes")) or not isinstance(anchor.get("points"), list) or len(anchor["points"]) != 2:
                _fail("ARTICULATED_GEOMETRY_INVALID")
            points = [_review_point(point, image) for point in anchor["points"]]
            source_length = source_summary["anchor_lengths"][anchor["name"].strip()]
            if abs(_distance(points) - source_length) > max(4, 0.2 * source_length):
                _fail("ARTICULATED_GEOMETRY_INVALID")
        _review_point(record.get("body_anchor"), image)
        supports = record.get("supports")
        if not isinstance(supports, dict) or set(supports) != {"near", "far"} or any(not isinstance(value, dict) for value in supports.values()):
            _fail("ARTICULATED_GEOMETRY_INVALID")
        expected = {"near": "contact", "far": "contact"} if index in (1, 5) else ({first: "stance", other: "swing"} if index <= 4 else {first: "swing", other: "stance"})
        if attack:
            expected = {"near": "stance", "far": "stance"}
        for limb, support in supports.items():
            role = support.get("role")
            if role != expected[limb]:
                _fail("ARTICULATED_GEOMETRY_INVALID")
            point = support.get("point")
            if role == "swing":
                if point is not None: _fail("ARTICULATED_GEOMETRY_INVALID")
            else:
                visible = _review_point(point, image)
                lane_y[limb].append(visible[1])
                support_x[limb].append(visible[0])
    if any(max(values) - min(values) > 4 for values in lane_y.values()):
        _fail("SUPPORT_LANE_INCONSISTENT")
    if attack and any(max(values) - min(values) > 4 for values in support_x.values()):
        _fail("PLANTED_SUPPORT_INCONSISTENT")

    evidence = report.get("components")
    if not _ordered(evidence) or not isinstance(component_reports, list) or len(component_reports) != 8:
        _fail("COMPONENT_EVIDENCE_INVALID")
    for record, image, component_report in zip(evidence, frames, component_reports):
        if record.get("image_pixel_sha256") != component_report.get("image_pixel_sha256"):
            _fail("COMPONENT_REPORT_STALE")
        try:
            reviewed = apply_component_review(image, component_report, record.get("component_decisions"))
        except WorkflowError:
            raise
        if reviewed.tobytes() != image.tobytes():
            _fail("COMPONENT_REVIEW_DESTRUCTIVE")

    motion = report.get("motion")
    motion_frames = motion.get("frames") if isinstance(motion, dict) else None
    phases = ["contact", "down", "passing", "up"] * 2
    if attack:
        phases = ["ready", "anticipation", "load", "strike", "follow_through", "recovery", "recovery", "settled"]
    if not _ordered(motion_frames):
        _fail("MOTION_EVIDENCE_INVALID")
    for record, phase in zip(motion_frames, phases):
        if record.get("phase") != phase or not _notes(record.get("notes")):
            _fail("MOTION_EVIDENCE_INVALID")
        _equipment(record.get("equipment"), source_summary["held_item_ids"])
    transitions = motion.get("transitions")
    expected_pairs = [(i, i + 1 if i < 8 else 1) for i in range(1, 9)]
    if not isinstance(transitions, list) or len(transitions) != 8:
        _fail("TRANSITION_EVIDENCE_INVALID")
    for record, pair in zip(transitions, expected_pairs):
        if (not isinstance(record, dict) or (record.get("from"), record.get("to")) != pair
                or record.get("status") != "pass" or not _notes(record.get("notes"))):
            _fail("TRANSITION_EVIDENCE_INVALID")
        _equipment(record.get("equipment"), source_summary["held_item_ids"], transition=True)
    playback = motion.get("playback")
    if not isinstance(playback, list) or len(playback) != 2:
        _fail("PLAYBACK_EVIDENCE_INVALID")
    methods = {"agent_frame_and_playback_review", "human_continuous_review"}
    for record in playback:
        if (not isinstance(record, dict) or type(record.get("frame_ms")) is not int
                or record.get("status") != "pass" or not isinstance(record.get("method"), str)
                or record.get("method") not in methods
                or not _notes(record.get("notes"))):
            _fail("PLAYBACK_EVIDENCE_INVALID")
    if playback[0]["frame_ms"] != request["frame_ms"] or playback[1]["frame_ms"] <= request["frame_ms"]:
        _fail("PLAYBACK_EVIDENCE_INVALID")
    return {
        "decision": "pass", "source_geometry": source_summary,
        "output_pivot": pivot, "first_support_limb": first, "validated_frames": 8,
        "validated_transitions": 8,
        "playback_frame_ms": [item["frame_ms"] for item in playback],
        "held_item_ids": source_summary["held_item_ids"],
    }
