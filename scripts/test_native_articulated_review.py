import copy
import hashlib
import unittest

from PIL import Image, ImageDraw

from generic_sprite_workflow import _review_target
from native_components import analyze_components
from native_articulated_review import validate_review, validate_source_geometry
from sheet_workflow_state import WorkflowError


NOTES = "Explicit reviewer evidence for this required field."


def pixel_digest(image):
    return hashlib.sha256(
        image.width.to_bytes(4, "big") + image.height.to_bytes(4, "big") + image.tobytes()
    ).hexdigest()


def source_fixture():
    image = Image.new("RGBA", (32, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 4, 23, 27), fill=(80, 150, 90, 255))
    digest = pixel_digest(image)
    geometry = {
        "source_sha256": digest,
        "anchors": [
            {"name": "torso", "points": [[10, 8], [21, 8]], "notes": NOTES},
            {"name": "hips", "points": [[11, 20], [20, 20]], "notes": NOTES},
        ],
        "body_anchor": [16, 12],
        "pivot": [16, 28],
        "uncertainty_pixels": 1.5,
        "held_items": [{"id": "shield", "notes": NOTES}],
        "notes": NOTES,
    }
    return image, digest, geometry


def frame_fixture():
    image = Image.new("RGBA", (32, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 4, 23, 27), fill=(80, 150, 90, 255))
    draw.rectangle((27, 10, 29, 12), fill=(180, 120, 40, 255))
    return image


def review_fixture():
    source, source_sha, geometry = source_fixture()
    frames = [frame_fixture() for _ in range(8)]
    component_reports = [analyze_components(frame) for frame in frames]
    context = {
        "run_id": "run-123",
        "attempts": [{"index": 1}],
        "request": {
            "frames": 8, "action": "walk", "orientation": "left",
            "frame_ms": 100, "canvas": [32, 32], "loop": True,
        },
        "artifact_hashes": {"frame.png": "a" * 64},
        "consistency": {"flagged_frames": []},
        "source_sha256": source_sha,
    }
    checks = {name: {"status": "pass", "notes": NOTES} for name in
              ("identity", "scale", "orientation", "framing", "background", "motion", "loop")}
    phases = ["contact", "down", "passing", "up"] * 2
    first = "near"
    geometry_frames = []
    motion_frames = []
    component_evidence = []
    for number in range(1, 9):
        if number in (1, 5):
            supports = {"near": {"role": "contact", "point": [12, 27]},
                        "far": {"role": "contact", "point": [20, 27]}}
        elif number <= 4:
            supports = {"near": {"role": "stance", "point": [12, 27]},
                        "far": {"role": "swing", "point": None}}
        else:
            supports = {"near": {"role": "swing", "point": None},
                        "far": {"role": "stance", "point": [20, 27]}}
        geometry_frames.append({
            "frame": number,
            "anchors": copy.deepcopy(geometry["anchors"]),
            "body_anchor": [16, 12],
            "supports": supports,
            "notes": NOTES,
        })
        motion_frames.append({
            "frame": number, "phase": phases[number - 1], "notes": NOTES,
            "equipment": [{"id": "shield", "attachment": "visible", "rigidity": "pass", "notes": NOTES}],
        })
        report = component_reports[number - 1]
        decisions = [{"id": item["id"], "decision": "preserve_as_art", "notes": NOTES}
                     for item in report["components"] if item["requires_review"]]
        component_evidence.append({
            "frame": number, "image_pixel_sha256": report["image_pixel_sha256"],
            "component_decisions": decisions,
        })
    transitions = []
    for number in range(1, 9):
        transitions.append({
            "from": number, "to": number + 1 if number < 8 else 1,
            "status": "pass", "notes": NOTES,
            "equipment": [{"id": "shield", "status": "pass", "notes": NOTES}],
        })
    report = {
        "artifact_set_sha256": _review_target(context),
        "decision": "pass", "checks": checks,
        "scale_anchors": [{"name": "torso", "notes": NOTES}, {"name": "hips", "notes": NOTES}],
        "flagged_frame_findings": [],
        "geometry": {"pivot": [16, 28], "first_support_limb": first, "frames": geometry_frames},
        "components": component_evidence,
        "motion": {
            "frames": motion_frames,
            "transitions": transitions,
            "playback": [
                {"frame_ms": 100, "status": "pass", "method": "agent_frame_and_playback_review", "notes": NOTES},
                {"frame_ms": 200, "status": "pass", "method": "human_continuous_review", "notes": NOTES},
            ],
        },
    }
    return context, report, source, geometry, frames, component_reports


class ArticulatedReviewTests(unittest.TestCase):
    def reject(self, code, function, *args):
        with self.assertRaises(WorkflowError) as caught:
            function(*args)
        self.assertEqual(code, caught.exception.code)

    def test_source_geometry_returns_normalized_measurements(self):
        source, digest, geometry = source_fixture()
        result = validate_source_geometry(source, digest, geometry)
        self.assertEqual([32, 32], result["source_size"])
        self.assertEqual(["torso", "hips"], result["anchor_names"])
        self.assertEqual({"torso": 11.0, "hips": 9.0}, result["anchor_lengths"])
        self.assertEqual(["shield"], result["held_item_ids"])

    def test_source_geometry_rejects_stale_invisible_and_invalid_geometry(self):
        source, digest, geometry = source_fixture()
        cases = []
        stale = copy.deepcopy(geometry); stale["source_sha256"] = "0" * 64
        cases.append(("SOURCE_GEOMETRY_STALE", stale))
        invisible = copy.deepcopy(geometry); invisible["body_anchor"] = [1, 1]
        cases.append(("SOURCE_GEOMETRY_INVISIBLE", invisible))
        short = copy.deepcopy(geometry); short["anchors"][0]["points"] = [[10, 8], [12, 8]]
        cases.append(("SOURCE_GEOMETRY_INVALID", short))
        for code, candidate in cases:
            with self.subTest(code=code):
                self.reject(code, validate_source_geometry, source, digest, candidate)

    def test_source_pivot_uses_unrounded_canvas_bounds(self):
        source, digest, geometry = source_fixture()
        for pivot in ([-0.49, 28], [32, 28]):
            candidate = copy.deepcopy(geometry); candidate["pivot"] = pivot
            with self.subTest(pivot=pivot):
                self.reject("SOURCE_GEOMETRY_INVALID", validate_source_geometry, source, digest, candidate)

    def test_complete_pass_returns_derived_geometry_and_motion_summary(self):
        args = review_fixture()
        result = validate_review(*args)
        self.assertEqual("pass", result["decision"])
        self.assertEqual("near", result["first_support_limb"])
        self.assertEqual(8, result["validated_frames"])
        self.assertEqual(8, result["validated_transitions"])
        self.assertEqual([100, 200], result["playback_frame_ms"])
        self.assertEqual(["shield"], result["held_item_ids"])

    def test_review_accepts_output_canvas_distinct_from_source_canvas(self):
        args = list(review_fixture())
        args[0]["request"]["canvas"] = [40, 40]
        args[4] = [Image.new("RGBA", (40, 40)) for _ in range(8)]
        for image in args[4]:
            draw = ImageDraw.Draw(image)
            draw.rectangle((8, 4, 23, 27), fill=(80, 150, 90, 255))
            draw.rectangle((27, 10, 29, 12), fill=(180, 120, 40, 255))
        args[5] = [analyze_components(frame) for frame in args[4]]
        for evidence, component_report in zip(args[1]["components"], args[5]):
            evidence["image_pixel_sha256"] = component_report["image_pixel_sha256"]
            evidence["component_decisions"] = [
                {"id": item["id"], "decision": "preserve_as_art", "notes": NOTES}
                for item in component_report["components"] if item["requires_review"]
            ]
        self.assertEqual("pass", validate_review(*args)["decision"])

    def test_review_accepts_named_support_records_and_distinct_output_pivot(self):
        args = list(review_fixture())
        args[1]["geometry"]["pivot"] = [18, 29]
        self.assertEqual("pass", validate_review(*args)["decision"])

    def test_named_anchor_and_equipment_coverage_is_order_independent(self):
        args = list(review_fixture())
        args[3]["held_items"].append({"id": "sword", "notes": NOTES})
        for frame in args[1]["geometry"]["frames"]:
            frame["anchors"].reverse()
        for frame in args[1]["motion"]["frames"]:
            frame["equipment"].append({"id": "sword", "attachment": "occluded", "rigidity": "pass", "notes": NOTES})
            frame["equipment"].reverse()
        for transition in args[1]["motion"]["transitions"]:
            transition["equipment"].append({"id": "sword", "status": "pass", "notes": NOTES})
            transition["equipment"].reverse()
        result = validate_review(*args)
        self.assertEqual(["shield", "sword"], result["held_item_ids"])

    def test_named_anchor_and_equipment_coverage_rejects_duplicates_or_missing_ids(self):
        args = list(review_fixture())
        args[1]["geometry"]["frames"][0]["anchors"][1]["name"] = "torso"
        self.reject("ARTICULATED_GEOMETRY_INVALID", validate_review, *args)
        args = list(review_fixture())
        args[1]["motion"]["frames"][0]["equipment"].append(copy.deepcopy(args[1]["motion"]["frames"][0]["equipment"][0]))
        self.reject("EQUIPMENT_EVIDENCE_INVALID", validate_review, *args)
        args = list(review_fixture())
        args[3]["held_items"].append({"id": "sword", "notes": NOTES})
        self.reject("EQUIPMENT_EVIDENCE_INVALID", validate_review, *args)

    def test_output_pivot_uses_unrounded_canvas_bounds(self):
        for pivot in ([-0.49, 28], [32, 28]):
            args = list(review_fixture()); args[1]["geometry"]["pivot"] = pivot
            with self.subTest(pivot=pivot):
                self.reject("ARTICULATED_GEOMETRY_INVALID", validate_review, *args)

    def test_malformed_json_scalar_types_fail_with_workflow_errors(self):
        cases = [
            ("EQUIPMENT_EVIDENCE_INVALID", lambda r: r["motion"]["frames"][0]["equipment"][0].update(id=[])),
            ("EQUIPMENT_EVIDENCE_INVALID", lambda r: r["motion"]["frames"][0]["equipment"][0].update(attachment=[])),
            ("EQUIPMENT_EVIDENCE_INVALID", lambda r: r["motion"]["frames"][0]["equipment"][0].update(rigidity={})),
            ("EQUIPMENT_EVIDENCE_INVALID", lambda r: r["motion"]["transitions"][0]["equipment"][0].update(status=[])),
            ("TRANSITION_EVIDENCE_INVALID", lambda r: r["motion"]["transitions"][0].update(status=[])),
            ("ARTICULATED_GEOMETRY_INVALID", lambda r: r["geometry"].update(first_support_limb=[])),
            ("PLAYBACK_EVIDENCE_INVALID", lambda r: r["motion"]["playback"][0].update(method={})),
        ]
        for code, mutate in cases:
            args = list(review_fixture()); mutate(args[1])
            with self.subTest(code=code):
                self.reject(code, validate_review, *args)

    def test_review_uses_context_source_digest_and_rejects_stale_target(self):
        args = list(review_fixture())
        args[0]["source_sha256"] = "f" * 64
        self.reject("SOURCE_GEOMETRY_STALE", validate_review, *args)
        args = list(review_fixture())
        args[1]["artifact_set_sha256"] = "f" * 64
        self.reject("VISUAL_REVIEW_STALE", validate_review, *args)

    def test_review_rejects_bad_support_order_and_lane_spread(self):
        args = list(review_fixture())
        args[1]["geometry"]["frames"][1]["supports"]["near"]["role"] = "swing"
        args[1]["geometry"]["frames"][1]["supports"]["near"]["point"] = None
        self.reject("ARTICULATED_GEOMETRY_INVALID", validate_review, *args)
        args = list(review_fixture())
        args[1]["geometry"]["frames"][2]["supports"]["near"]["point"][1] = 21
        self.reject("SUPPORT_LANE_INCONSISTENT", validate_review, *args)

    def test_review_rejects_malformed_timing_and_playback(self):
        args = list(review_fixture())
        args[1]["motion"]["frames"][2]["phase"] = "down"
        self.reject("MOTION_EVIDENCE_INVALID", validate_review, *args)
        args = list(review_fixture())
        args[1]["motion"]["playback"][1]["frame_ms"] = 100
        self.reject("PLAYBACK_EVIDENCE_INVALID", validate_review, *args)

    def test_review_rejects_omitted_or_failed_equipment_transition_and_playback(self):
        mutations = [
            ("EQUIPMENT_EVIDENCE_INVALID", lambda r: r["motion"]["frames"][0].update(equipment=[])),
            ("TRANSITION_EVIDENCE_INVALID", lambda r: r["motion"]["transitions"][7].update(status="fail")),
            ("PLAYBACK_EVIDENCE_INVALID", lambda r: r["motion"]["playback"][0].update(status="fail")),
        ]
        for code, mutate in mutations:
            args = list(review_fixture()); mutate(args[1])
            with self.subTest(code=code): self.reject(code, validate_review, *args)

    def test_review_rejects_incomplete_stale_and_destructive_component_evidence(self):
        args = list(review_fixture())
        args[1]["components"].pop()
        self.reject("COMPONENT_EVIDENCE_INVALID", validate_review, *args)
        args = list(review_fixture())
        args[1]["components"][0]["image_pixel_sha256"] = "0" * 64
        self.reject("COMPONENT_REPORT_STALE", validate_review, *args)
        args = list(review_fixture())
        args[1]["components"][0]["component_decisions"][0]["decision"] = "remove_as_background"
        self.reject("COMPONENT_REVIEW_DESTRUCTIVE", validate_review, *args)

    def test_legitimate_rejection_needs_no_extended_motion_evidence(self):
        context, report, source, geometry, frames, component_reports = review_fixture()
        report["decision"] = "reject"
        report["checks"]["motion"]["status"] = "fail"
        report.pop("geometry"); report.pop("components"); report.pop("motion")
        result = validate_review(context, report, source, geometry, frames, component_reports)
        self.assertEqual({"decision": "reject", "source_geometry": validate_source_geometry(source, context["source_sha256"], geometry)}, result)


if __name__ == "__main__":
    unittest.main()
