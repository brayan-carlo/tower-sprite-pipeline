"""Immutable generic sprite manifest contract (offline, provider-neutral)."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import copy
import hashlib
import json
from pathlib import Path
import re


GENERIC_MODE = "generic_sequential"
GENERIC_MODEL = "fal-ai/nano-banana-2/edit"
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "source",
        "request",
        "output",
        "movement_plan",
        "provider",
        "authorization",
        "qa",
    }
)
PRICING_INPUT_KEYS = frozenset(
    {
        "model",
        "source_url",
        "verified_at",
        "currency",
        "unit",
        "base_unit_price_usd",
        "resolution",
        "resolution_multiplier",
    }
)
PRICING_KEYS = frozenset(
    {
        "source_url",
        "verified_at",
        "currency",
        "unit",
        "base_unit_price_usd",
        "resolution",
        "resolution_multiplier",
        "effective_unit_price_usd",
    }
)
SOURCE_KEYS = frozenset(
    {
        "original_path",
        "normalized_path",
        "original_filename",
        "byte_length",
        "sha256",
        "normalized_sha256",
        "original_mode",
        "original_size",
        "normalized_size",
        "background_action",
        "alpha_bbox",
        "resample_mode",
    }
)
PROVIDER_KEYS_V1 = frozenset({"name", "model", "pricing"})
PROVIDER_KEYS_V2 = frozenset({*PROVIDER_KEYS_V1, "prompt_template_version"})


def _err(message: str) -> None:
    raise ValueError(message)


def _money(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        _err("INVALID_PRICE")
    if not result.is_finite() or result <= 0:
        _err("PRICE_NONPOSITIVE")
    return result


def _fixed(value: object) -> str:
    return format(Decimal(str(value)), "f")


def _multiply_decimal_exact(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(1, len(left.as_tuple().digits) + len(right.as_tuple().digits))
        return left * right


def _sum_decimals_exact(values: object) -> Decimal:
    items = tuple(values)
    with localcontext() as context:
        context.prec = max(1, sum(len(value.as_tuple().digits) for value in items) + 1)
        return sum(items, Decimal("0"))


def validate_movement_plan(value: object, frame_count: int) -> tuple[dict, ...]:
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
        _err("FRAME_COUNT_INVALID")
    if not isinstance(value, (list, tuple)) or len(value) != frame_count:
        _err("MOVEMENT_PLAN_LENGTH")
    result = []
    for expected, item in enumerate(value, 1):
        if not isinstance(item, dict):
            _err("MOVEMENT_PLAN_KEYS")
        if set(item) != {"frame_index", "phase", "body_motion"}:
            _err("MOVEMENT_PLAN_KEYS")
        if type(item["frame_index"]) is not int or item["frame_index"] != expected:
            _err("MOVEMENT_PLAN_INDEX")
        for key in ("phase", "body_motion"):
            if not isinstance(item[key], str) or not item[key].strip():
                _err("MOVEMENT_PLAN_TEXT")
        result.append(
            {
                "frame_index": expected,
                "phase": item["phase"],
                "body_motion": item["body_motion"],
            }
        )
    return tuple(result)


def validate_pricing_evidence(value: object, model: str, now: datetime) -> dict:
    if not isinstance(value, dict):
        _err("PRICING_INVALID")
    if set(value) != PRICING_INPUT_KEYS:
        _err("PRICING_KEYS")
    if value["model"] != model:
        _err("PRICING_MODEL_MISMATCH")
    if value["unit"] != "image" or value["resolution"] != "0.5K":
        _err("PRICING_INCOMPATIBLE")
    if value["currency"] != "USD":
        _err("PRICING_INVALID")
    if value["source_url"] != "https://api.fal.ai/v1/models/pricing":
        _err("PRICING_INVALID")
    if not isinstance(value["verified_at"], str):
        _err("PRICING_TIMESTAMP")
    timestamp_pattern = (
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]+)?(?:Z|[+-]([0-9]{2}):([0-9]{2}))"
    )
    match = re.fullmatch(timestamp_pattern, value["verified_at"], flags=re.ASCII)
    if match is None:
        _err("PRICING_TIMESTAMP")
    if match.group(1) is not None and (
        int(match.group(1)) > 23 or int(match.group(2)) > 59
    ):
        _err("PRICING_TIMESTAMP")
    try:
        verified = datetime.fromisoformat(value["verified_at"].replace("Z", "+00:00"))
    except ValueError:
        _err("PRICING_TIMESTAMP")
    if verified.tzinfo is None:
        _err("PRICING_TIMESTAMP")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    verified_ms = verified.replace(microsecond=(verified.microsecond // 1000) * 1000)
    now_ms = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    if (now_ms - verified_ms).total_seconds() > 900:
        _err("PRICING_STALE")
    if (verified_ms - now_ms).total_seconds() > 60:
        _err("PRICING_STALE")
    decimal_pattern = r"[0-9]+(?:\.[0-9]+)?"
    base_text = str(value["base_unit_price_usd"])
    base = _money(base_text)
    if re.fullmatch(decimal_pattern, base_text, flags=re.ASCII) is None:
        _err("PRICING_INVALID")
    multiplier_text = str(value["resolution_multiplier"])
    try:
        multiplier = Decimal(multiplier_text)
    except (InvalidOperation, ValueError, TypeError):
        _err("PRICING_INVALID")
    if not multiplier.is_finite() or multiplier <= 0:
        _err("PRICE_NONPOSITIVE")
    if re.fullmatch(decimal_pattern, multiplier_text, flags=re.ASCII) is None:
        _err("PRICING_INVALID")
    result = {key: value[key] for key in PRICING_INPUT_KEYS if key != "model"}
    result["base_unit_price_usd"] = _fixed(base)
    result["resolution_multiplier"] = _fixed(multiplier)
    result["effective_unit_price_usd"] = _fixed(
        _multiply_decimal_exact(base, multiplier)
    )
    return result


def _validate_frame_size(frame_size: tuple[int, int]) -> None:
    if not isinstance(frame_size, tuple) or len(frame_size) != 2:
        _err("FRAME_SIZE_INVALID")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in frame_size):
        _err("FRAME_SIZE_INVALID")


def _validate_pricing(pricing: dict, schema_version: int = 1) -> Decimal:
    if not isinstance(pricing, dict) or set(pricing) != PRICING_KEYS:
        _err("PRICING_UNVALIDATED")
    if pricing["currency"] != "USD":
        _err("PRICING_INCOMPATIBLE")
    if pricing["unit"] != "image" or pricing["resolution"] != "0.5K":
        _err("PRICING_INCOMPATIBLE")
    try:
        base = Decimal(str(pricing["base_unit_price_usd"]))
        multiplier = Decimal(str(pricing["resolution_multiplier"]))
        effective = Decimal(str(pricing["effective_unit_price_usd"]))
    except (InvalidOperation, ValueError, TypeError):
        _err("PRICING_UNVALIDATED")
    values = (base, multiplier, effective)
    if any(not value.is_finite() or value <= 0 for value in values):
        _err("PRICING_UNVALIDATED")
    calculated = (
        _multiply_decimal_exact(base, multiplier)
        if schema_version >= 2
        else base * multiplier
    )
    if effective != calculated:
        _err("PRICING_UNVALIDATED")
    return effective


def _validate_source(source: dict, frame_size: tuple[int, int]) -> None:
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        _err("SOURCE_RECORD_INVALID")
    expected_paths = {
        "original_path": "input/source/original.png",
        "normalized_path": "input/source/master.png",
    }
    for key, expected in expected_paths.items():
        if source[key] != expected or Path(source[key]).is_absolute():
            _err("SOURCE_RECORD_INVALID")
    filename = source["original_filename"]
    if not isinstance(filename, str) or not filename or filename != Path(filename).name:
        _err("SOURCE_RECORD_INVALID")
    if type(source["byte_length"]) is not int or source["byte_length"] <= 0:
        _err("SOURCE_RECORD_INVALID")
    for key in ("sha256", "normalized_sha256"):
        if not isinstance(source[key], str):
            _err("SOURCE_RECORD_INVALID")
        if re.fullmatch(r"[0-9a-f]{64}", source[key]) is None:
            _err("SOURCE_RECORD_INVALID")
    if not isinstance(source["original_mode"], str) or not source["original_mode"]:
        _err("SOURCE_RECORD_INVALID")
    for key in ("original_size", "normalized_size"):
        value = source[key]
        if not isinstance(value, list) or len(value) != 2:
            _err("SOURCE_RECORD_INVALID")
        if any(type(number) is not int or number <= 0 for number in value):
            _err("SOURCE_RECORD_INVALID")
    if source["normalized_size"] != list(frame_size):
        _err("SOURCE_RECORD_INVALID")
    if source["resample_mode"] != "nearest":
        _err("SOURCE_RECORD_INVALID")
    if source["background_action"] not in (
        "preserved-alpha",
        "removed-exact-uniform-border",
    ):
        _err("SOURCE_RECORD_INVALID")
    bbox = source["alpha_bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        _err("SOURCE_RECORD_INVALID")
    if any(type(number) is not int for number in bbox):
        _err("SOURCE_RECORD_INVALID")
    if not (
        0 <= bbox[0] < bbox[2] <= frame_size[0]
        and 0 <= bbox[1] < bbox[3] <= frame_size[1]
    ):
        _err("SOURCE_RECORD_INVALID")


def build_manifest(
    *,
    source_record: dict,
    animation: str,
    direction: str,
    frame_count: int,
    movement_plan: tuple[dict, ...],
    frame_size: tuple[int, int],
    model: str,
    pricing: dict,
    max_budget_usd: Decimal,
    schema_version: int = 2,
) -> dict:
    if type(schema_version) is not int or schema_version not in (1, 2):
        _err("MANIFEST_INVALID")
    if not isinstance(animation, str) or not animation.strip():
        _err("ANIMATION_INVALID")
    if not isinstance(direction, str) or not direction.strip():
        _err("DIRECTION_INVALID")
    if model != GENERIC_MODEL:
        _err("MODEL_MISMATCH")
    _validate_frame_size(frame_size)
    effective_price = _validate_pricing(pricing, schema_version)
    _validate_source(source_record, frame_size)
    validated_plan = validate_movement_plan(movement_plan, frame_count)
    budget = _money(max_budget_usd)
    worst_case = (
        _multiply_decimal_exact(effective_price, Decimal(frame_count))
        if schema_version >= 2
        else effective_price * frame_count
    )
    if worst_case > budget:
        _err("BUDGET_EXCEEDED")
    manifest = {
        "schema_version": schema_version,
        "mode": GENERIC_MODE,
        "source": dict(source_record),
        "request": {
            "animation": animation,
            "direction": direction,
            "frame_count": frame_count,
        },
        "output": {
            "width": frame_size[0],
            "height": frame_size[1],
            "background": "transparent",
            "anchor": "bottom-center",
            "resample_mode": "nearest",
        },
        "movement_plan": list(validated_plan),
        "provider": {
            "name": "fal.ai",
            "model": model,
            "pricing": pricing,
        },
        "authorization": {
            "status": "PENDING",
            "planned_requests": frame_count,
            "worst_case_usd": _fixed(worst_case),
            "max_budget_usd": _fixed(budget),
        },
        "qa": {
            "technical_required": True,
            "human_review_required": True,
            "machine_ceiling": "READY_FOR_VISUAL_REVIEW",
        },
    }
    if schema_version == 2:
        manifest["provider"]["prompt_template_version"] = 2
    return manifest


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


AUTHORIZATION_MUTABLE_KEYS = frozenset(
    {
        "status",
        "authorized_at",
        "authorized_budget_usd",
        "contract_sha256",
    }
)


def authorization_contract_sha256(manifest: dict) -> str:
    """Hash the prepared contract excluding only authorization-time fields.

    The canonical JSON algorithm is UTF-8, sorted keys, no insignificant
    whitespace, Unicode preserved and non-finite numbers rejected. Planning
    fields such as request count, prices and budgets remain covered.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("authorization"), dict):
        _err("MANIFEST_INVALID")
    contract = copy.deepcopy(manifest)
    for key in AUTHORIZATION_MUTABLE_KEYS:
        contract["authorization"].pop(key, None)
    return canonical_sha256(contract)


def manifest_schema_version(manifest: object) -> int:
    if not isinstance(manifest, dict):
        _err("MANIFEST_INVALID")
    version = manifest.get("schema_version")
    provider = manifest.get("provider")
    if type(version) is not int or version not in (1, 2) or not isinstance(provider, dict):
        _err("MANIFEST_INVALID")
    expected_keys = PROVIDER_KEYS_V1 if version == 1 else PROVIDER_KEYS_V2
    if set(provider) != expected_keys:
        _err("MANIFEST_INVALID")
    if version == 2 and (
        type(provider.get("prompt_template_version")) is not int
        or provider["prompt_template_version"] != 2
    ):
        _err("MANIFEST_INVALID")
    return version


def effective_prompt(manifest: dict, frame_index: int) -> str:
    version = manifest_schema_version(manifest)
    movement_plan = manifest.get("movement_plan")
    if (
        type(frame_index) is not int
        or not isinstance(movement_plan, list)
        or not 1 <= frame_index <= len(movement_plan)
    ):
        _err("MANIFEST_INVALID")
    movement = movement_plan[frame_index - 1]
    if not isinstance(movement, dict):
        _err("MANIFEST_INVALID")
    try:
        animation = manifest["request"]["animation"]
        direction = manifest["request"]["direction"]
        phase = movement["phase"]
        body_motion = movement["body_motion"]
    except (KeyError, TypeError):
        _err("MANIFEST_INVALID")
    subject = (
        "image 1"
        if frame_index == 1
        else "image 1 (master) and image 2 (previous accepted frame)"
    )
    movement_clause = f"; body movement: {body_motion}" if version == 2 else ""
    return (
        f"Redraw {subject} as the same character {animation} {direction} at {phase}"
        f"{movement_clause}; preserve face, proportions, clothing, equipment, palette and art style; "
        "show one complete bottom-center character on the declared solid chroma background."
    )


def authorization_summary(manifest: dict) -> dict:
    """Return the complete portable, provider-neutral spending summary."""
    source = manifest["source"]
    request = manifest["request"]
    output = manifest["output"]
    provider = manifest["provider"]
    authorization = manifest["authorization"]
    schema_version = manifest_schema_version(manifest)
    summary = {
        "source": {
            "original_filename": source["original_filename"],
            "original_path": source["original_path"],
            "original_sha256": source["sha256"],
            "transmitted_path": source["normalized_path"],
            "transmitted_sha256": source["normalized_sha256"],
        },
        "animation": request["animation"],
        "direction": request["direction"],
        "frame_count": request["frame_count"],
        "output_dimensions": {
            "width": output["width"],
            "height": output["height"],
        },
        "movement_plan": copy.deepcopy(manifest["movement_plan"]),
        "provider": provider["name"],
        "model": provider["model"],
        "effective_unit_price_usd": provider["pricing"]["effective_unit_price_usd"],
        "planned_requests": authorization["planned_requests"],
        "worst_case_usd": authorization["worst_case_usd"],
        "max_budget_usd": authorization["max_budget_usd"],
    }
    if schema_version == 2:
        summary["schema_version"] = 2
        summary["prompt_template_version"] = 2
        summary["effective_prompts"] = [
            {
                "frame_index": movement["frame_index"],
                "prompt": effective_prompt(manifest, movement["frame_index"]),
            }
            for movement in manifest["movement_plan"]
        ]
    return summary
