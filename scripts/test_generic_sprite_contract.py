import copy
import hashlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import generic_sprite_contract as contract


PRICING_CORPUS = (
    Path(__file__).resolve().parent.parent
    / "assets/fixtures/generic-pricing-conformance.json"
)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.raw_pricing = {
            "model": contract.GENERIC_MODEL,
            "source_url": "https://api.fal.ai/v1/models/pricing",
            "verified_at": self.now.isoformat(),
            "currency": "USD",
            "unit": "image",
            "base_unit_price_usd": "0.10",
            "resolution": "0.5K",
            "resolution_multiplier": "1",
        }
        self.pricing = contract.validate_pricing_evidence(
            self.raw_pricing,
            contract.GENERIC_MODEL,
            self.now,
        )
        self.plan = (
            {
                "frame_index": 1,
                "phase": "left-foot contact",
                "body_motion": "left foot forward",
            },
            {
                "frame_index": 2,
                "phase": "right-foot contact",
                "body_motion": "right foot forward",
            },
        )
        self.source_record = {
            "original_path": "input/source/original.png",
            "normalized_path": "input/source/master.png",
            "original_filename": "hero.png",
            "byte_length": 91,
            "sha256": "a" * 64,
            "normalized_sha256": "b" * 64,
            "original_mode": "RGBA",
            "original_size": [8, 8],
            "normalized_size": [8, 8],
            "background_action": "preserved-alpha",
            "alpha_bbox": [2, 1, 6, 8],
            "resample_mode": "nearest",
        }

    def build(self, **changes: object) -> dict:
        arguments = {
            "source_record": copy.deepcopy(self.source_record),
            "animation": "walk",
            "direction": "south",
            "frame_count": 2,
            "movement_plan": self.plan,
            "frame_size": (8, 8),
            "model": contract.GENERIC_MODEL,
            "pricing": copy.deepcopy(self.pricing),
            "max_budget_usd": Decimal("0.20"),
        }
        arguments.update(changes)
        return contract.build_manifest(**arguments)

    def assert_reason(self, expected: str, callback) -> None:
        with self.assertRaises(ValueError) as caught:
            callback()
        self.assertEqual(expected, str(caught.exception))

    def test_movement_plan_requires_exact_ordered_entries(self) -> None:
        self.assertEqual(self.plan, contract.validate_movement_plan(list(self.plan), 2))

    def test_movement_plan_rejects_duplicate_and_out_of_order_indices(self) -> None:
        duplicate = (self.plan[0], {**self.plan[1], "frame_index": 1})
        out_of_order = ({**self.plan[0], "frame_index": 2}, self.plan[1])
        for label, value in (("duplicate", duplicate), ("out-of-order", out_of_order)):
            with self.subTest(case=label):
                self.assert_reason(
                    "MOVEMENT_PLAN_INDEX",
                    lambda value=value: contract.validate_movement_plan(value, 2),
                )

    def test_movement_plan_rejects_invalid_shape_and_empty_text(self) -> None:
        cases = (
            ("MOVEMENT_PLAN_LENGTH", self.plan[:1]),
            (
                "MOVEMENT_PLAN_KEYS",
                ({**self.plan[0], "unexpected": True}, self.plan[1]),
            ),
            (
                "MOVEMENT_PLAN_TEXT",
                ({**self.plan[0], "phase": " "}, self.plan[1]),
            ),
        )
        for expected, value in cases:
            with self.subTest(reason=expected):
                self.assert_reason(
                    expected,
                    lambda value=value: contract.validate_movement_plan(value, 2),
                )

    def test_movement_plan_rejects_boolean_float_and_nonpositive_frame_count(self) -> None:
        self.assert_reason(
            "MOVEMENT_PLAN_INDEX",
            lambda: contract.validate_movement_plan(
                ({**self.plan[0], "frame_index": True},),
                1,
            ),
        )
        self.assert_reason(
            "MOVEMENT_PLAN_INDEX",
            lambda: contract.validate_movement_plan(
                ({**self.plan[0], "frame_index": 1.0},),
                1,
            ),
        )
        for value in (0, -1, True):
            with self.subTest(frame_count=value):
                self.assert_reason(
                    "FRAME_COUNT_INVALID",
                    lambda value=value: contract.validate_movement_plan((), value),
                )

    def test_pricing_rejects_model_mismatch(self) -> None:
        value = {**self.raw_pricing, "model": "other-model"}
        self.assert_reason(
            "PRICING_MODEL_MISMATCH",
            lambda: contract.validate_pricing_evidence(
                value,
                contract.GENERIC_MODEL,
                self.now,
            ),
        )

    def test_pricing_rejects_nonpositive_and_nonfinite_values(self) -> None:
        for price in ("0", "-0.01", "NaN"):
            value = {**self.raw_pricing, "base_unit_price_usd": price}
            with self.subTest(price=price):
                self.assert_reason(
                    "PRICE_NONPOSITIVE",
                    lambda value=value: contract.validate_pricing_evidence(
                        value,
                        contract.GENERIC_MODEL,
                        self.now,
                    ),
                )

    def test_pricing_rejects_stale_naive_future_and_extra_fields(self) -> None:
        stale = {
            **self.raw_pricing,
            "verified_at": (self.now - timedelta(minutes=16)).isoformat(),
        }
        naive = {**self.raw_pricing, "verified_at": "2026-09-02T12:00:00"}
        future = {
            **self.raw_pricing,
            "verified_at": (self.now + timedelta(seconds=61)).isoformat(),
        }
        extra = {**self.raw_pricing, "secret": "must-not-pass"}
        cases = (
            ("PRICING_STALE", stale),
            ("PRICING_TIMESTAMP", naive),
            ("PRICING_STALE", future),
            ("PRICING_KEYS", extra),
        )
        for expected, value in cases:
            with self.subTest(reason=expected):
                self.assert_reason(
                    expected,
                    lambda value=value: contract.validate_pricing_evidence(
                        value,
                        contract.GENERIC_MODEL,
                        self.now,
                    ),
                )

    def test_fresh_pricing_rejects_noncanonical_source_and_timestamp(self) -> None:
        canonical = {
            **self.raw_pricing,
            "source_url": "https://api.fal.ai/v1/models/pricing",
        }
        cases = (
            (
                "PRICING_INVALID",
                {**canonical, "source_url": "https://example.invalid/pricing"},
            ),
            (
                "PRICING_TIMESTAMP",
                {**canonical, "verified_at": self.now.isoformat().replace("T", " ")},
            ),
        )
        for reason, value in cases:
            with self.subTest(reason=reason):
                self.assert_reason(
                    reason,
                    lambda value=value: contract.validate_pricing_evidence(
                        value,
                        contract.GENERIC_MODEL,
                        self.now,
                    ),
                )

    def test_shared_pricing_corpus_matches_python_acceptance_and_normalization(self) -> None:
        corpus = json.loads(PRICING_CORPUS.read_text(encoding="utf-8"))
        self.assertEqual("OFFLINE_FIXTURE_NOT_FOR_LIVE_USE", corpus["evidence_mode"])
        for case in corpus["cases"]:
            with self.subTest(case=case["id"]):
                value = {**corpus["base"], **case["changes"]}
                now = datetime.fromisoformat(case["now"].replace("Z", "+00:00"))
                if case["accepted"]:
                    self.assertEqual(
                        case["normalized"],
                        contract.validate_pricing_evidence(
                            value,
                            contract.GENERIC_MODEL,
                            now,
                        ),
                    )
                else:
                    with self.assertRaises(ValueError):
                        contract.validate_pricing_evidence(
                            value,
                            contract.GENERIC_MODEL,
                            now,
                        )

    def test_budget_exceeded_reaches_budget_gate_with_otherwise_valid_inputs(self) -> None:
        self.assert_reason(
            "BUDGET_EXCEEDED",
            lambda: self.build(max_budget_usd=Decimal("0.19")),
        )

    def test_manifest_rejects_empty_animation_and_direction_independently(self) -> None:
        for field, expected in (
            ("animation", "ANIMATION_INVALID"),
            ("direction", "DIRECTION_INVALID"),
        ):
            with self.subTest(field=field):
                self.assert_reason(expected, lambda field=field: self.build(**{field: " "}))

    def test_manifest_rejects_effective_price_mismatch_and_negative(self) -> None:
        mismatch = {**self.pricing, "effective_unit_price_usd": "0.11"}
        negative = {**self.pricing, "effective_unit_price_usd": "-0.10"}
        for label, value in (("mismatch", mismatch), ("negative", negative)):
            with self.subTest(case=label):
                self.assert_reason(
                    "PRICING_UNVALIDATED",
                    lambda value=value: self.build(pricing=value),
                )

    def test_manifest_rejects_malformed_source_provenance(self) -> None:
        cases = (
            (
                "absolute path",
                {**self.source_record, "original_path": "C:/outside/original.png"},
            ),
            (
                "three-component original size",
                {**self.source_record, "original_size": [8, 8, 4]},
            ),
            (
                "empty original mode",
                {**self.source_record, "original_mode": ""},
            ),
            (
                "invalid background action",
                {**self.source_record, "background_action": "semantic-removal"},
            ),
            (
                "out-of-bounds alpha bbox",
                {**self.source_record, "alpha_bbox": [2, 1, 9, 8]},
            ),
            (
                "string alpha bbox",
                {**self.source_record, "alpha_bbox": "2,1,6,8"},
            ),
            (
                "invalid sha",
                {**self.source_record, "sha256": "not-a-sha256"},
            ),
            (
                "extra provenance key",
                {**self.source_record, "unexpected": "must-not-pass"},
            ),
        )
        for label, malformed in cases:
            with self.subTest(case=label):
                self.assert_reason(
                    "SOURCE_RECORD_INVALID",
                    lambda malformed=malformed: self.build(source_record=malformed),
                )

    def test_manifest_has_fixed_shape_and_decimal_budget(self) -> None:
        manifest = self.build()
        self.assertEqual(contract.TOP_LEVEL_KEYS, frozenset(manifest))
        self.assertEqual("0.20", manifest["authorization"]["max_budget_usd"])
        self.assertEqual("0.20", manifest["authorization"]["worst_case_usd"])
        self.assertEqual(2, manifest["authorization"]["planned_requests"])
        self.assertEqual(contract.GENERIC_MODEL, manifest["provider"]["model"])

    def test_new_manifests_select_prompt_v2_and_preserve_explicit_v1(self) -> None:
        current = self.build()
        self.assertEqual(2, current["schema_version"])
        self.assertEqual(2, current["provider"]["prompt_template_version"])
        historic = self.build(schema_version=1)
        self.assertEqual(1, historic["schema_version"])
        self.assertNotIn("prompt_template_version", historic["provider"])
        for invalid in (True, 0, 3, "2"):
            with self.subTest(version=invalid):
                self.assert_reason(
                    "MANIFEST_INVALID",
                    lambda invalid=invalid: self.build(schema_version=invalid),
                )

    def test_effective_prompt_preserves_v1_and_adds_exact_v2_body_motion(self) -> None:
        historic = self.build(schema_version=1)
        current = self.build()
        self.assertEqual(
            "Redraw image 1 as the same character walk south at left-foot contact; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.",
            contract.effective_prompt(historic, 1),
        )
        self.assertEqual(
            "Redraw image 1 (master) and image 2 (previous accepted frame) as the same character walk south at right-foot contact; body movement: right foot forward; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.",
            contract.effective_prompt(current, 2),
        )

    def test_manifest_requires_the_normalized_master_sha256(self) -> None:
        missing = copy.deepcopy(self.source_record)
        del missing["normalized_sha256"]
        malformed = {**self.source_record, "normalized_sha256": "not-a-sha256"}
        for value in (missing, malformed):
            with self.subTest(value=value):
                self.assert_reason(
                    "SOURCE_RECORD_INVALID",
                    lambda value=value: self.build(source_record=value),
                )

    def test_authorization_contract_hash_excludes_only_mutable_authorization_fields(self) -> None:
        manifest = self.build()
        authorized = copy.deepcopy(manifest)
        authorized["authorization"].update(
            {
                "status": "AUTHORIZED",
                "authorized_at": "2026-09-02T15:04:05Z",
                "authorized_budget_usd": "0.20",
                "contract_sha256": "f" * 64,
            }
        )
        pruned = copy.deepcopy(manifest)
        del pruned["authorization"]["status"]
        expected = hashlib.sha256(
            json.dumps(
                pruned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        self.assertEqual(expected, contract.authorization_contract_sha256(manifest))
        self.assertEqual(expected, contract.authorization_contract_sha256(authorized))

        changed = copy.deepcopy(authorized)
        changed["movement_plan"][0]["phase"] = "changed phase"
        self.assertNotEqual(expected, contract.authorization_contract_sha256(changed))

    def test_authorization_summary_is_complete_portable_and_secret_free(self) -> None:
        manifest = self.build()
        summary = contract.authorization_summary(manifest)
        self.assertEqual(
            {
                "source": {
                    "original_filename": "hero.png",
                    "original_path": "input/source/original.png",
                    "original_sha256": "a" * 64,
                    "transmitted_path": "input/source/master.png",
                    "transmitted_sha256": "b" * 64,
                },
                "schema_version": 2,
                "prompt_template_version": 2,
                "animation": "walk",
                "direction": "south",
                "frame_count": 2,
                "output_dimensions": {"width": 8, "height": 8},
                "movement_plan": list(self.plan),
                "provider": "fal.ai",
                "model": contract.GENERIC_MODEL,
                "effective_unit_price_usd": "0.10",
                "planned_requests": 2,
                "worst_case_usd": "0.20",
                "max_budget_usd": "0.20",
                "effective_prompts": [
                    {
                        "frame_index": 1,
                        "prompt": "Redraw image 1 as the same character walk south at left-foot contact; body movement: left foot forward; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.",
                    },
                    {
                        "frame_index": 2,
                        "prompt": "Redraw image 1 (master) and image 2 (previous accepted frame) as the same character walk south at right-foot contact; body movement: right foot forward; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.",
                    },
                ],
            },
            summary,
        )
        serialized = json.dumps(summary)
        self.assertNotIn("FAL_KEY", serialized)
        self.assertNotIn("C:\\", serialized)

    def test_historic_v1_authorization_summary_preserves_c1_shape_exactly(self) -> None:
        historic = self.build(schema_version=1)
        self.assertEqual(
            {
                "source": {
                    "original_filename": "hero.png",
                    "original_path": "input/source/original.png",
                    "original_sha256": "a" * 64,
                    "transmitted_path": "input/source/master.png",
                    "transmitted_sha256": "b" * 64,
                },
                "animation": "walk",
                "direction": "south",
                "frame_count": 2,
                "output_dimensions": {"width": 8, "height": 8},
                "movement_plan": list(self.plan),
                "provider": "fal.ai",
                "model": contract.GENERIC_MODEL,
                "effective_unit_price_usd": "0.10",
                "planned_requests": 2,
                "worst_case_usd": "0.20",
                "max_budget_usd": "0.20",
            },
            contract.authorization_summary(historic),
        )


if __name__ == "__main__":
    unittest.main()
