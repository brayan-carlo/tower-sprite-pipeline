import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { createHash, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readMp4Metadata } from "./sheet_video_metadata.mjs";

const API_PRICING_URL = "https://api.fal.ai/v1/models/pricing";
const NANO_PUBLIC_URL = "https://fal.ai/models/fal-ai/nano-banana-2/edit";
const NANO = "fal-ai/nano-banana-2/edit";
const CLEANUP = "fal-ai/ideogram/remove-background";
const WAN = "fal-ai/wan-motion";
const DREAM = "fal-ai/bytedance/dreamactor/v2";
const H3 = "minimax/h3-max/image-to-video";
const FLF = "fal-ai/wan-flf2v";
const SEEDREAM = "fal-ai/bytedance/seedream/v4.5/edit";
const SEEDREAM_VARIANT = "seedream45_a_identity_v1";
const VACE = "fal-ai/wan-vace-14b/inpainting";
const REGIONAL_ROUTE = "regional_video_inpaint_v1";
const REGIONAL_SCHEMA_CANONICAL_SHA256 = "c8a204e8242707c534f84f8f306b6085a1ca9d383c683c323533b8739e586f40";
const REGIONAL_PRICE_TTL = 600_000;
const REGIONAL_FIXED_INPUT = {
  seed:1, num_frames:81, frames_per_second:24, match_input_num_frames:true, match_input_frames_per_second:true,
  resolution:"720p", aspect_ratio:"1:1", preprocess:false, num_interpolated_frames:0, temporal_downsample_factor:0,
  enable_auto_downsample:false, auto_downsample_min_fps:15, sync_mode:false, enable_safety_checker:true,
  enable_prompt_expansion:false, num_inference_steps:30, guidance_scale:5, sampler:"unipc", shift:5,
  acceleration:"none", video_quality:"high", video_write_mode:"balanced", interpolator_model:"film",
  transparency_mode:"content_aware", return_frames_zip:false,
};
const ENDPOINT_ROLES = ["pose_edit", "video_ab", "video_ba", "cleanup"];
const ENDPOINT_MODELS = { pose_edit: NANO, video_ab: FLF, video_ba: FLF, cleanup: CLEANUP };
// Reviewed immutable public schemas. A changed schema requires a new review, never guessed defaults.
const ENDPOINT_SCHEMAS = {
  [SEEDREAM]: { name: "BytedanceSeedreamV45EditInput", sha256: "a04c03c635d50a9bef2040511898a50aed2d79a2fd5d9aaf9e18eee02db7389a" },
  [NANO]: { name: "NanoBanana2EditInput", sha256: "7f069b57442e0acd2b46bd3cc9131e0501132c07144c44ad45ece9c6e4054ce3" },
  [FLF]: { name: "WanFlf2vInput", sha256: "8c57ca8d3b9a29600d1d65f6d7fc9593461d443db9d64a8a8ccaa02f0732b4eb" },
  [CLEANUP]: { name: "IdeogramRemoveBackgroundInput", sha256: "20a65966f81b36ab7c815f7e65f0294393156ee142e541796d1690396a7377f3" },
};
const H3_PUBLIC_URL = `https://fal.ai/models/${H3}`;
const MODELS = new Set([NANO, CLEANUP, WAN, DREAM, H3]);
const GUIDED_MODELS = new Set([WAN, DREAM]);
const VIDEO_MODELS = new Set([WAN, DREAM, H3, FLF, VACE]);
const PRICE_MAX_AGE_MS = 900_000;
const PRICE_FUTURE_SKEW_MS = 30_000;
const MAX_PRICING_BYTES = 5 * 1024 * 1024;
const MAX_JSON_BYTES = 5 * 1024 * 1024;
const MAX_NO_MEDIA_BYTES = 32 * 1024 * 1024;
const MAX_PNG_BYTES = 50 * 1024 * 1024;
const MAX_MP4_BYTES = 100 * 1024 * 1024;
const OUTPUT_FIELDS = ["state", "request_id", "estimated_cost_usd", "raw_path", "raw_sha256", "block_reason", "retry_safe"];
const STATE_RANK = new Map([
  ["SUBMITTING", 0], ["REQUEST_ACCEPTED", 1], ["IN_QUEUE", 2], ["IN_PROGRESS", 3],
  ["COMPLETED", 4], ["RESULT_READY", 5], ["DOWNLOADED", 6], ["REMOTE_REJECTED", 6],
  ["FAILED", 6], ["CANCELLED", 6], ["RESULT_INVALID", 6],
  ["SEEDREAM_CAPTURED", 6],
]);

function failure(code, retrySafe = false) {
  const error = new Error(code);
  error.code = code;
  error.retrySafe = retrySafe;
  return error;
}

function output(values = {}) {
  const result = Object.fromEntries(OUTPUT_FIELDS.map((key) => [key, null]));
  Object.assign(result, values);
  if (result.retry_safe === null) result.retry_safe = false;
  return result;
}

function blocked(error) {
  const code = typeof error?.code === "string" ? error.code : "INTERNAL_ERROR";
  if (code === "KEY_SETUP_REQUIRED") {
    return output({ state: "KEY_SETUP_REQUIRED", block_reason: "FAL_KEY_MISSING", retry_safe: true });
  }
  return output({ state: "BLOCKED", block_reason: code, retry_safe: error?.retrySafe === true });
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, expected) {
  return isObject(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort());
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (isObject(value)) return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  if (typeof value === "number" && !Number.isFinite(value)) throw failure("CONFIG_INVALID");
  return value;
}

function canonicalBytes(value) {
  return Buffer.from(`${JSON.stringify(canonical(value), null, 2)}\n`, "utf8");
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function samePath(left, right) {
  return path.relative(path.resolve(left), path.resolve(right)) === "";
}

function regularBytes(candidate, code, maximum) {
  if (typeof candidate !== "string" || !path.isAbsolute(candidate) || candidate.includes("\0")) throw failure(code);
  let before;
  let opened;
  let after;
  let descriptor;
  let bytes;
  try {
    before = fs.lstatSync(candidate, { bigint: true });
    if (before.isSymbolicLink() || !before.isFile() || !samePath(fs.realpathSync.native(candidate), candidate)) throw failure(code);
    if (before.size > BigInt(maximum)) throw failure(code);
    descriptor = fs.openSync(candidate, "r");
    opened = fs.fstatSync(descriptor, { bigint: true });
    bytes = fs.readFileSync(descriptor);
    after = fs.lstatSync(candidate, { bigint: true });
  } catch (error) {
    if (error?.code === code) throw error;
    throw failure(code);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
  const identity = (a, b) => a.dev === b.dev && a.ino === b.ino && a.size === b.size && a.mtimeNs === b.mtimeNs;
  if (!identity(before, opened) || !identity(opened, after) || BigInt(bytes.length) !== after.size) throw failure(code);
  return bytes;
}

function readJsonFile(candidate, code, maximum = MAX_JSON_BYTES) {
  const bytes = regularBytes(candidate, code, maximum);
  try {
    return { value: JSON.parse(bytes.toString("utf8")), bytes };
  } catch {
    throw failure(code);
  }
}

function ensureJobDir(jobDir) {
  if (typeof jobDir !== "string" || jobDir.trim() === "") throw failure("JOB_DIR_INVALID");
  const resolved = path.resolve(jobDir);
  let stat;
  try { stat = fs.lstatSync(resolved); } catch { throw failure("JOB_DIR_INVALID"); }
  if (stat.isSymbolicLink() || !stat.isDirectory() || !samePath(fs.realpathSync.native(resolved), resolved)) throw failure("JOB_DIR_INVALID");
  return resolved;
}

function pngMetadata(bytes) {
  if (bytes.length < 29 || !bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex")) || bytes.toString("ascii", 12, 16) !== "IHDR" || bytes.readUInt32BE(8) !== 13) {
    throw failure("SOURCE_INVALID");
  }
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  if (width < 16 || height < 16 || width > 4096 || height > 4096 || width * height > 64_000_000) throw failure("SOURCE_INVALID");
  return { width, height };
}

function validHash(value) {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

function validateInput(model, input) {
  if (model === NANO) {
    if (!exactKeys(input, ["prompt", "resolution", "aspect_ratio", "num_images", "output_format", "limit_generations", "enable_web_search"])
      || typeof input.prompt !== "string" || input.prompt.trim() === "" || input.prompt.length > 10_000
      || input.resolution !== "2K" || input.aspect_ratio !== "16:9" || input.num_images !== 1
      || input.output_format !== "png" || input.limit_generations !== true || input.enable_web_search !== false) throw failure("CONFIG_INVALID");
  } else if (model === CLEANUP) {
    if (!exactKeys(input, ["sync_mode"]) || input.sync_mode !== false) throw failure("CONFIG_INVALID");
  } else if (model === WAN) {
    if (!exactKeys(input, ["prompt", "adapt_motion", "enhance_identity", "acceleration", "enable_safety_checker"])
      || typeof input.prompt !== "string" || input.prompt.trim() === "" || input.prompt.length > 10_000
      || input.adapt_motion !== true || input.enhance_identity !== false || input.acceleration !== "regular" || input.enable_safety_checker !== true) throw failure("CONFIG_INVALID");
  } else if (model === H3) {
    if (!exactKeys(input, ["prompt", "duration", "resolution", "prompt_expansion_mode", "enable_safety_checker", "sync_mode", "end_image_same_as_start"])
      || typeof input.prompt !== "string" || !input.prompt.trim() || input.prompt.length > 10_000
      || input.duration !== 5 || input.resolution !== "768P" || input.prompt_expansion_mode !== "balanced"
      || input.enable_safety_checker !== true || input.sync_mode !== false || typeof input.end_image_same_as_start !== "boolean") throw failure("CONFIG_INVALID");
  } else if (model === DREAM) {
    if (!exactKeys(input, ["trim_first_second"]) || input.trim_first_second !== true) throw failure("CONFIG_INVALID");
  }
}

function loadContext(jobDir) {
  const root = ensureJobDir(jobDir);
  const configRecord = readJsonFile(path.join(root, "config.json"), "CONFIG_INVALID");
  const config = configRecord.value;
  rejectRegionalDowngrade(root, config.schema_version === 6);
  if (config?.schema_version === 6) return loadRegionalContext(root, configRecord);
  if ([4,5].includes(config?.schema_version)) return loadEndpointContext(root, configRecord);
  const rootKeys = ["schema_version", "model", "source", "input", "authorization", "max_cost_usd"];
  if (GUIDED_MODELS.has(config?.model)) rootKeys.push("driving_video");
  if (!exactKeys(config, rootKeys) || config.schema_version !== 3 || !MODELS.has(config.model)) throw failure("CONFIG_INVALID");
  if (!exactKeys(config.source, ["path", "sha256"]) || !validHash(config.source.sha256)) throw failure("CONFIG_INVALID");
  const managed = isObject(config.authorization) && Object.hasOwn(config.authorization, "task_context");
  const authorizationKeys = ["request_authorized", "scope_sha256", ...(managed ? ["task_context"] : [])];
  if (!exactKeys(config.authorization, authorizationKeys)
    || config.authorization.request_authorized !== true || !validHash(config.authorization.scope_sha256)) throw failure("CONFIG_INVALID");
  if (managed) validateTaskContext(config.authorization.task_context);
  else rejectMissingManagedContext(root);
  if (typeof config.max_cost_usd !== "number" || !Number.isFinite(config.max_cost_usd) || config.max_cost_usd < 0) throw failure("CONFIG_INVALID");
  validateInput(config.model, config.input);
  const sourceMaximum = config.model === CLEANUP ? 10 * 1024 * 1024 : config.model === DREAM ? Math.floor(4.7 * 1024 * 1024) : 20 * 1024 * 1024;
  const sourceBytes = regularBytes(config.source.path, "SOURCE_INVALID", sourceMaximum);
  if (sha256(sourceBytes) !== config.source.sha256) throw failure("SOURCE_INVALID");
  const sourceMetadata = pngMetadata(sourceBytes);
  if (config.model === DREAM && (sourceMetadata.width < 480 || sourceMetadata.width > 1920 || sourceMetadata.height < 480 || sourceMetadata.height > 1080)) throw failure("SOURCE_INVALID");
  let guideBytes = null;
  let guideMetadata = null;
  if (GUIDED_MODELS.has(config.model)) {
    if (!exactKeys(config.driving_video, ["path", "sha256"]) || !validHash(config.driving_video.sha256)) throw failure("CONFIG_INVALID");
    guideBytes = regularBytes(config.driving_video.path, "DRIVING_VIDEO_INVALID", 20 * 1024 * 1024);
    if (sha256(guideBytes) !== config.driving_video.sha256) throw failure("DRIVING_VIDEO_INVALID");
    try { guideMetadata = readMp4Metadata(guideBytes); } catch { throw failure("DRIVING_VIDEO_INVALID"); }
    if (guideMetadata.width < 200 || guideMetadata.width > 2048 || guideMetadata.height < 200 || guideMetadata.height > 1440
      || guideMetadata.duration_seconds <= 0 || guideMetadata.duration_seconds > 10) throw failure("DRIVING_VIDEO_INVALID");
  }
  return {
    root, config, managed, configSha256: sha256(canonicalBytes(config)), configFileSha256: sha256(configRecord.bytes), sourceBytes, sourceMetadata, guideBytes, guideMetadata,
  };
}

function validateTaskContext(value, role = null) {
  if (!exactKeys(value, ["task_ref", "run_manifest", "run_id", "attempt_id", "operation_id"])
    || !exactKeys(value.task_ref, ["schema", "task_id", "task_manifest", "authorization_sha256", "target_id"])
    || value.task_ref.schema !== "sprite_task_ref_v1" || !validHash(value.task_ref.authorization_sha256)
    || !path.isAbsolute(value.task_ref.task_manifest ?? "") || !path.isAbsolute(value.run_manifest ?? "")
    || ![value.run_id, value.attempt_id, value.operation_id, value.task_ref.task_id, value.task_ref.target_id].every(item => typeof item === "string" && item.length > 0 && item.length <= 256)
    || !/^.+:attempt-\d{2}$/.test(value.attempt_id) || value.attempt_id.slice(0, -11) !== value.run_id
    || !(role ? value.attempt_id === `${value.run_id}:attempt-01` && value.operation_id === `${value.attempt_id}:${role}`
      : [`${value.attempt_id}:primary`, `${value.attempt_id}:cleanup`].includes(value.operation_id))) throw failure("TASK_CONTEXT_INVALID");
}

function rejectMissingManagedContext(root) {
  // A removed config marker must not downgrade a job within the existing sealed v3 layout.
  const attempt = path.dirname(root);
  if (!/^attempt-\d{2}$/.test(path.basename(attempt)) || !["primary", ...ENDPOINT_ROLES].includes(path.basename(root))) return;
  const runRoot = path.dirname(attempt);
  if (["pose_edit", "video_ab", "video_ba"].includes(path.basename(root)) || fs.existsSync(path.join(runRoot,"endpoint-plan.json"))) throw failure("TASK_CONTEXT_REQUIRED");
  for (const name of ["scope.json", "run-manifest.json"]) {
    const candidate = path.join(runRoot, name);
    if (fs.existsSync(candidate)) {
      const request = readJsonFile(candidate, "TASK_CONTEXT_INVALID").value?.request;
      if (request?.task_ref !== undefined || request?.route === "endpoint_cycle_v1") throw failure("TASK_CONTEXT_REQUIRED");
    }
  }
}

function endpointPlan(runRoot) {
  const record = readJsonFile(path.join(runRoot, "endpoint-plan.json"), "ENDPOINT_PLAN_INVALID");
  const p = record.value;
  const equal = (a, b) => canonicalBytes(a).equals(canonicalBytes(b));
  const seedream = p.schema === "sprite_endpoint_plan_v2";
  if (!exactKeys(p, ["schema", "attempt_id", "source_sha256", "pose_config_sha256", "pose_inputs", "endpoint_a_sha256", "preparation_policy", "video_policy", "sampling_policy", "cleanup_policy", "output_policy",...(seedream?["variant","pose_output_policy","pose_decoder"]:[])])
    || (!seedream && p.schema !== "sprite_endpoint_plan_v1") || typeof p.attempt_id !== "string" || !/^.+:attempt-01$/.test(p.attempt_id)
    || ![p.source_sha256, p.pose_config_sha256, p.endpoint_a_sha256].every(validHash)
    || !Array.isArray(p.pose_inputs) || p.pose_inputs.length !== 2
    || p.pose_inputs.some((v, i) => !exactKeys(v, ["role", "sha256"]) || v.role !== ["edit", "identity"][i] || !validHash(v.sha256))
    || p.preparation_policy !== "pose_canvas_2048_v1" || p.sampling_policy !== "endpoint_full_range_81_four_phases_v1"
    || !equal(p.video_policy, { frames:81, fps:24, resolution:"720p", aspect_ratio:"1:1", steps:30, acceleration:"none", prompt_expansion:false })
    || !equal(p.cleanup_policy, { model:CLEANUP, sync_mode:false, max_bytes:10485760, max_width:4096, max_height:4096 })
    || !equal(p.output_policy, { frames:8, frame_ms:100, loop:true, canvas:[2048,2048], geometry_policy:"common_nearest_2048_v1", alignment_policy:"body_x_preserve_y_v1" })
    || !canonicalBytes(p).equals(record.bytes)
    || (seedream && (p.variant !== SEEDREAM_VARIANT || p.pose_output_policy !== "seedream_rgb8_png_jpeg_to_png_v1"
      || !exactKeys(p.pose_decoder,["pillow_version","jpeg_codec_version"]) || !Object.values(p.pose_decoder).every(v=>typeof v === "string" && v.trim() !== "")
      || p.pose_inputs[0].sha256 !== p.endpoint_a_sha256))) throw failure("ENDPOINT_PLAN_INVALID");
  // These objects contain only integers, booleans and strings: canonicalBytes is Python encoded exactly.
  return { plan: p, planSha256: sha256(record.bytes) };
}

function endpointLayout(root, role) {
  if (!ENDPOINT_ROLES.includes(role) || path.basename(root) !== role || path.basename(path.dirname(root)) !== "attempt-01") throw failure("CONFIG_INVALID");
  return path.dirname(path.dirname(root));
}

function validateEndpointInput(role, input, seedream = false) {
  if (role === "cleanup") return validateInput(CLEANUP, input);
  const prompt = typeof input?.prompt === "string" && input.prompt.trim().length >= 3 && input.prompt.length <= 10_000;
  if (role === "pose_edit") {
    if (seedream) {
      if (!exactKeys(input,["prompt","image_size","num_images","max_images","sync_mode","enable_safety_checker"]) || !prompt
        || !exactKeys(input.image_size,["width","height"]) || input.image_size.width !== 2048 || input.image_size.height !== 2048
        || input.num_images !== 1 || input.max_images !== 1 || input.sync_mode !== false || input.enable_safety_checker !== true) throw failure("CONFIG_INVALID");
      return;
    }
    if (!exactKeys(input, ["prompt", "resolution", "aspect_ratio", "num_images", "output_format", "limit_generations", "enable_web_search", "sync_mode", "safety_tolerance"])
      || !prompt || input.resolution !== "2K" || input.aspect_ratio !== "1:1" || input.num_images !== 1 || input.output_format !== "png"
      || input.limit_generations !== true || input.enable_web_search !== false || input.sync_mode !== false || input.safety_tolerance !== "4") throw failure("CONFIG_INVALID");
  } else if (!exactKeys(input, ["prompt", "frames", "fps", "resolution", "aspect_ratio", "steps", "acceleration", "prompt_expansion", "enable_safety_checker"])
    || !prompt || input.frames !== 81 || input.fps !== 24 || input.resolution !== "720p" || input.aspect_ratio !== "1:1"
    || input.steps !== 30 || input.acceleration !== "none" || input.prompt_expansion !== false || input.enable_safety_checker !== true) throw failure("CONFIG_INVALID");
}

function validateEndpointProvenance(record, config, inputsSha, plan, planSha) {
  const p = record.value;
  const role = config.role;
  const kind = role === "pose_edit" ? "pose_edit_inputs" : role === "cleanup" ? "two_video_full_range_grid" : "ordered_endpoint_pair";
  if (!exactKeys(p, ["schema", "kind", "original_source_sha256", "input_set_sha256", "endpoint_plan_sha256", "evidence"])
    || p.schema !== (config.schema_version === 5 ? "sprite_endpoint_provenance_v2" : "sprite_endpoint_provenance_v1") || p.kind !== kind || p.original_source_sha256 !== plan.source_sha256
    || p.input_set_sha256 !== inputsSha || p.endpoint_plan_sha256 !== planSha || !canonicalBytes(p).equals(record.bytes)) throw failure("PROVENANCE_INVALID");
  const e = p.evidence;
  const seedream = config.schema_version === 5;
  if (role === "pose_edit") {
    const candidateKey = seedream ? "a_candidate_ref" : "b_candidate_ref";
    if (!exactKeys(e, ["preparation_proof_sha256", "original_ref", candidateKey]) || !validHash(e.preparation_proof_sha256)
      || [e.original_ref, e[candidateKey]].some(r => !exactKeys(r, ["path", "sha256"]) || !path.isAbsolute(r.path ?? "") || !validHash(r.sha256))
      || e.original_ref.sha256 !== plan.source_sha256) throw failure("PROVENANCE_INVALID");
  } else if (role !== "cleanup") {
    if (!exactKeys(e, ["pose_operation_id", "pose_request_id", "pose_raw_sha256", "pair_gate_sha256", "direction",...(seedream?["pose_output_proof_sha256","endpoint_b_sha256"]:[])])
      || e.pose_operation_id !== `${plan.attempt_id}:pose_edit` || !validRequestId(e.pose_request_id)
      || !validHash(e.pair_gate_sha256) || e.direction !== (role === "video_ab" ? "ab" : "ba")
      || (seedream && ![e.pose_raw_sha256,e.pose_output_proof_sha256].every(validHash))
      || e[seedream ? "endpoint_b_sha256" : "pose_raw_sha256"] !== config.input_files[role === "video_ab" ? 1 : 0].sha256
      || config.input_files[role === "video_ab" ? 0 : 1].sha256 !== plan.endpoint_a_sha256) throw failure("PROVENANCE_INVALID");
  } else if (!exactKeys(e, ["ab_operation_id", "ab_request_id", "ab_raw_sha256", "ba_operation_id", "ba_request_id", "ba_raw_sha256", "cycle_gate_sha256", "selection_sha256", "grid_sha256"])
    || e.ab_operation_id !== `${plan.attempt_id}:video_ab` || e.ba_operation_id !== `${plan.attempt_id}:video_ba`
    || !validRequestId(e.ab_request_id) || !validRequestId(e.ba_request_id)
    || ![e.ab_raw_sha256, e.ba_raw_sha256, e.cycle_gate_sha256, e.selection_sha256].every(validHash)
    || e.grid_sha256 !== config.input_files[0].sha256) throw failure("PROVENANCE_INVALID");
}

function loadEndpointContext(root, configRecord) {
  const config = configRecord.value;
  const seedream = config.schema_version === 5;
  if (!exactKeys(config, ["schema_version", "route", "role", "model", "input_files", "input", "authorization", "max_cost_usd",...(seedream?["variant"]:[])])
    || config.route !== "endpoint_cycle_v1" || !ENDPOINT_ROLES.includes(config.role)
    || (seedream && config.variant !== SEEDREAM_VARIANT)
    || config.model !== (seedream && config.role === "pose_edit" ? SEEDREAM : ENDPOINT_MODELS[config.role])) throw failure("CONFIG_INVALID");
  if (!isObject(config.authorization) || !Object.hasOwn(config.authorization, "task_context")) throw failure("TASK_CONTEXT_REQUIRED");
  if (!exactKeys(config.authorization, ["request_authorized", "task_context"]) || config.authorization.request_authorized !== true) throw failure("CONFIG_INVALID");
  validateTaskContext(config.authorization.task_context, config.role);
  const runRoot = endpointLayout(root, config.role);
  if (!samePath(config.authorization.task_context.run_manifest, path.join(runRoot, "run-manifest.json"))) throw failure("TASK_CONTEXT_INVALID");
  if (typeof config.max_cost_usd !== "number" || !Number.isFinite(config.max_cost_usd) || config.max_cost_usd < 0) throw failure("CONFIG_INVALID");
  validateEndpointInput(config.role, config.input, seedream);
  const roles = config.role === "pose_edit" ? ["edit", "identity"] : config.role === "cleanup" ? ["grid"] : ["start", "end"];
  const snapshot = name => path.join(runRoot, "snapshots", name);
  const a = snapshot("endpoint-a.png");
  const b = path.join(runRoot, "attempt-01", "pose_edit", seedream ? "endpoint-b.png" : "raw.png");
  const paths = config.role === "pose_edit" ? [snapshot("edit.png"), snapshot("identity.png")] : config.role === "cleanup" ? [path.join(runRoot, "attempt-01", "cycle-grid.png")] : config.role === "video_ab" ? [a, b] : [b, a];
  if (!Array.isArray(config.input_files) || config.input_files.length !== roles.length
    || config.input_files.some((v, i) => !exactKeys(v, ["role", "path", "sha256"]) || v.role !== roles[i] || !validHash(v.sha256)
      || typeof v.path !== "string" || !path.isAbsolute(v.path) || !samePath(v.path, paths[i]))) throw failure("CONFIG_INVALID");
  const inputBuffers = config.input_files.map(v => {
    const bytes = regularBytes(v.path, "SOURCE_INVALID", config.role === "cleanup" ? 10485760 : 20 * 1024 * 1024);
    if (sha256(bytes) !== v.sha256) throw failure("SOURCE_INVALID");
    const meta = pngMetadata(bytes);
    if (config.role !== "cleanup" && (meta.width !== 2048 || meta.height !== 2048)) throw failure("SOURCE_INVALID");
    return bytes;
  });
  if (config.input_files.length === 2 && config.input_files[0].sha256 === config.input_files[1].sha256) throw failure("SOURCE_INVALID");
  const inputs = config.input_files.map(({role, sha256}) => ({role, sha256}));
  const inputRecord = readJsonFile(path.join(root, "inputs.json"), "INPUT_SET_INVALID");
  if (!canonicalBytes(inputs).equals(inputRecord.bytes)) throw failure("INPUT_SET_INVALID");
  const inputSetSha256 = sha256(inputRecord.bytes);
  const { plan, planSha256 } = endpointPlan(runRoot);
  if (seedream !== (plan.schema === "sprite_endpoint_plan_v2")) throw failure("ENDPOINT_PLAN_INVALID");
  if (seedream && config.role === "pose_edit" && !regularBytes(a,"SOURCE_INVALID",20*1024*1024).equals(inputBuffers[0])) throw failure("SOURCE_INVALID");
  const configSha256 = sha256(canonicalBytes(config));
  if (plan.attempt_id !== config.authorization.task_context.attempt_id
    || (config.role === "pose_edit" && (plan.pose_config_sha256 !== configSha256 || !canonicalBytes(plan.pose_inputs).equals(inputRecord.bytes)))) throw failure("ENDPOINT_PLAN_INVALID");
  const provenance = readJsonFile(path.join(root, "source-provenance.json"), "PROVENANCE_INVALID");
  validateEndpointProvenance(provenance, config, inputSetSha256, plan, planSha256);
  return { root, config, configBytes:configRecord.bytes, managed:true, endpoint:true, runRoot, plan, planSha256, configSha256, configFileSha256:sha256(configRecord.bytes),
    inputBuffers, inputSetSha256, sourceBytes:inputBuffers[0], sourceMetadata:pngMetadata(inputBuffers[0]) };
}

function rejectRegionalDowngrade(root, regional) {
  const runRoot = path.dirname(path.dirname(root));
  if (!/^attempt-\d{2}$/.test(path.basename(path.dirname(root)))) return;
  let marker = fs.existsSync(path.join(runRoot,"regional-plan.json"));
  for (const name of ["scope.json","run-manifest.json"]) {
    if (!fs.existsSync(path.join(runRoot,name))) continue;
    const request = readJsonFile(path.join(runRoot,name),"TASK_CONTEXT_INVALID").value?.request;
    marker ||= request?.route === REGIONAL_ROUTE || Object.hasOwn(request ?? {},"regional_plan_sha256");
  }
  if (marker && !regional) throw failure("REGIONAL_CONTEXT_REQUIRED");
}

function regionalLayout(root, role) {
  if (!["primary","cleanup"].includes(role) || path.basename(root) !== role || path.basename(path.dirname(root)) !== "attempt-01") throw failure("CONFIG_INVALID");
  return path.dirname(path.dirname(root));
}

function regionalPlan(runRoot) {
  const record = readJsonFile(path.join(runRoot,"regional-plan.json"),"REGIONAL_PLAN_INVALID");
  const p = record.value;
  const equal = (a,b) => canonicalBytes(a).equals(canonicalBytes(b));
  const ref = r => exactKeys(r,["path","sha256"]) && typeof r.path === "string" && path.isAbsolute(r.path) && validHash(r.sha256);
  if (!exactKeys(p,["schema","attempt_id","source_sha256","provenance","media","preflight","primary_config_sha256","primary_inputs","profile","historical_source","cleanup_policy","output_policy"])
    || p.schema !== "sprite_regional_plan_v1" || !/^.+:attempt-01$/.test(p.attempt_id ?? "")
    || !validHash(p.source_sha256) || !validHash(p.primary_config_sha256) || ![p.provenance,p.media,p.preflight].every(ref)
    || !exactKeys(p.historical_source,["history_bundle_sha256","history_id"]) || !validHash(p.historical_source.history_bundle_sha256)
    || typeof p.historical_source.history_id !== "string" || !p.historical_source.history_id || p.historical_source.history_id.length > 256
    || p.profile !== "vace81_24_720_v1" || !Array.isArray(p.primary_inputs) || p.primary_inputs.length !== 3
    || p.primary_inputs.some((v,i)=>!exactKeys(v,["role","sha256"]) || v.role !== ["video","mask","identity"][i] || !validHash(v.sha256))
    || p.primary_inputs[2].sha256 !== p.source_sha256
    || !equal(p.cleanup_policy,{model:CLEANUP,sync_mode:false,max_bytes:10485760,max_width:4096,max_height:4096})
    || !exactKeys(p.output_policy,["frames","frame_ms","loop","grounded","canvas","selection","geometry","alignment_policy"])
    || p.output_policy.frames !== 8 || p.output_policy.frame_ms !== 100 || p.output_policy.loop !== true || p.output_policy.grounded !== false
    || p.output_policy.alignment_policy !== "body_x_preserve_y_v1" || !canonicalBytes(p).equals(record.bytes)) throw failure("REGIONAL_PLAN_INVALID");
  const {canvas,selection,geometry:g} = p.output_policy;
  if (!Array.isArray(canvas) || canvas.length !== 2 || !canvas.every(x=>Number.isSafeInteger(x) && x > 0)
    || !Array.isArray(selection) || selection.length !== 8 || selection.some((x,i)=>!Number.isInteger(x) || x < 0 || x >= 81 || (i > 0 && x <= selection[i-1]))
    || !exactKeys(g,["policy","resize_canvas","crop","output_canvas","scale_numerator","scale_denominator"])
    || g.policy !== "inverse_parent_preparation_v1" || !equal(g.output_canvas,canvas) || !equal(g.resize_canvas,[Math.max(...canvas),Math.max(...canvas)])
    || g.scale_numerator !== Math.max(...canvas) || !Number.isSafeInteger(g.scale_denominator) || g.scale_denominator <= 0
    || !Array.isArray(g.crop) || g.crop.length !== 4 || !g.crop.every(x=>Number.isSafeInteger(x) && x >= 0)
    || !equal(g.crop.slice(2),canvas) || g.crop[0]+canvas[0] > g.resize_canvas[0] || g.crop[1]+canvas[1] > g.resize_canvas[1]) throw failure("REGIONAL_PLAN_INVALID");
  const scope = readJsonFile(path.join(runRoot,"scope.json"),"TASK_CONTEXT_INVALID");
  const manifestRecord = readJsonFile(path.join(runRoot,"run-manifest.json"),"TASK_CONTEXT_INVALID");
  const manifest = manifestRecord.value;
  const scopeHash = sha256(scope.bytes), planSha256 = sha256(record.bytes);
  const sealBytes = regularBytes(path.join(runRoot,"scope.sha256"),"TASK_CONTEXT_INVALID",128);
  const seal = sealBytes.toString("utf8");
  if (!canonicalBytes(scope.value).equals(scope.bytes) || seal !== `${scopeHash}\n`
    || manifest.schema_version !== 3 || manifest.mode !== "whole_sheet" || manifest.authorization_scope_sha256 !== scopeHash
    || scope.value.request?.route !== REGIONAL_ROUTE || scope.value.request?.regional_plan_sha256 !== planSha256
    || !["request","source","authorization","run_id"].every(k=>Object.hasOwn(scope.value,k) && Object.hasOwn(manifest,k) && equal(scope.value[k],manifest[k]))
    || p.attempt_id !== `${scope.value.run_id}:attempt-01`) throw failure("TASK_CONTEXT_INVALID");
  return {plan:p,planSha256,scope:scope.value,runRoot,scopeFileSha256:scopeHash,
    scopeSealFileSha256:sha256(sealBytes),runManifestFileSha256:sha256(manifestRecord.bytes)};
}

function loadRegionalContext(root, configRecord) {
  const c = configRecord.value;
  if (!exactKeys(c,["schema_version","route","role","model","input_files","input","authorization","max_cost_usd"])
    || c.schema_version !== 6 || c.route !== REGIONAL_ROUTE || !["primary","cleanup"].includes(c.role)
    || c.model !== (c.role === "primary" ? VACE : CLEANUP)
    || !exactKeys(c.authorization,["request_authorized","task_context"]) || c.authorization.request_authorized !== true
    || typeof c.max_cost_usd !== "number" || !Number.isFinite(c.max_cost_usd) || c.max_cost_usd < 0) throw failure("CONFIG_INVALID");
  validateTaskContext(c.authorization.task_context,c.role);
  const runRoot = regionalLayout(root,c.role), regional = regionalPlan(runRoot);
  if (!samePath(c.authorization.task_context.run_manifest,path.join(runRoot,"run-manifest.json"))
    || c.authorization.task_context.run_id !== regional.scope.run_id) throw failure("TASK_CONTEXT_INVALID");
  if (c.role === "cleanup") validateInput(CLEANUP,c.input);
  else if (!exactKeys(c.input,["prompt","negative_prompt",...Object.keys(REGIONAL_FIXED_INPUT)])
    || ["prompt","negative_prompt"].some(k=>typeof c.input[k] !== "string" || !c.input[k].trim() || c.input[k].length > 10000)
    || Object.entries(REGIONAL_FIXED_INPUT).some(([k,v])=>c.input[k] !== v)) throw failure("CONFIG_INVALID");
  const roles = c.role === "primary" ? ["video","mask","identity"] : ["grid"];
  if (!Array.isArray(c.input_files) || c.input_files.length !== roles.length
    || c.input_files.some((v,i)=>!exactKeys(v,["role","path","sha256"]) || v.role !== roles[i] || !validHash(v.sha256))) throw failure("CONFIG_INVALID");
  const inputBuffers = c.input_files.map((v,i)=>{
    const video = c.role === "primary" && i < 2;
    const bytes = regularBytes(v.path,"SOURCE_INVALID",video ? 20*1024*1024 : c.role === "cleanup" ? 10485760 : 8*1024*1024);
    if (sha256(bytes) !== v.sha256) throw failure("SOURCE_INVALID");
    if (video) {
      let meta; try { meta = readMp4Metadata(bytes); } catch { throw failure("SOURCE_INVALID"); }
      const declared = regional.plan.output_policy.geometry.scale_denominator;
      if (declared > 4096 || meta.width !== declared || meta.height !== declared || Math.abs(meta.duration_seconds-81/24) > 1e-6) throw failure("SOURCE_INVALID");
    } else pngMetadata(bytes);
    return bytes;
  });
  if (inputBuffers.reduce((sum,b)=>sum+b.length,0) > 48*1024*1024) throw failure("SOURCE_INVALID");
  const inputs = c.input_files.map(({role,sha256})=>({role,sha256}));
  const inputRecord = readJsonFile(path.join(root,"inputs.json"),"INPUT_SET_INVALID");
  if (!canonicalBytes(inputs).equals(inputRecord.bytes)) throw failure("INPUT_SET_INVALID");
  const configSha256 = sha256(canonicalBytes(c));
  if (c.role === "primary" && (regional.plan.primary_config_sha256 !== configSha256 || !canonicalBytes(regional.plan.primary_inputs).equals(inputRecord.bytes))) throw failure("REGIONAL_PLAN_INVALID");
  return {root,config:c,configBytes:configRecord.bytes,managed:true,regional:true,...regional,configSha256,configFileSha256:sha256(configRecord.bytes),
    inputSetSha256:sha256(inputRecord.bytes),inputBuffers,sourceBytes:inputBuffers[0],sourceMetadata:c.role === "cleanup" ? pngMetadata(inputBuffers[0]) : null};
}

function normalizedDecimal(value, code = "PRICE_NOT_VERIFIABLE") {
  const text = String(value);
  if (text.length > 256 || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(text)) throw failure(code);
  const [whole, fraction = ""] = text.split(".");
  const tail = fraction.replace(/0+$/, "");
  return tail ? `${whole}.${tail}` : whole;
}

function decimalProduct(...values) {
  let integer = 1n;
  let scale = 0;
  for (const value of values) {
    const text = normalizedDecimal(value);
    const parts = text.split(".");
    integer *= BigInt(parts.join(""));
    scale += parts[1]?.length ?? 0;
  }
  const digits = integer.toString().padStart(scale + 1, "0");
  return normalizedDecimal(scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits);
}

function exactNumericDecimal(text) {
  const number = Number(text);
  if (!Number.isFinite(number) || number <= 0) throw failure("PRICE_NOT_VERIFIABLE");
  // Keep the legacy numeric receipt field, but never silently round the managed decimal.
  let printed = String(number);
  const exponential = /^(\d+)(?:\.(\d+))?e([+-]?\d+)$/.exec(printed);
  if (exponential) {
    const digits = exponential[1] + (exponential[2] ?? "");
    const point = exponential[1].length + Number(exponential[3]);
    printed = point <= 0 ? `0.${"0".repeat(-point)}${digits}` : point >= digits.length ? digits + "0".repeat(point - digits.length) : `${digits.slice(0, point)}.${digits.slice(point)}`;
  }
  if (normalizedDecimal(printed) !== normalizedDecimal(text)) throw failure("PRICE_NOT_VERIFIABLE");
  return number;
}

function money(value) {
  return Number(value.toFixed(12));
}

async function boundedBytes(response, maximum, code, requireCompleteLength = false) {
  const length = response.headers?.get?.("content-length");
  if (length !== null && length !== undefined) {
    if (!/^\d+$/.test(length) || BigInt(length) > BigInt(maximum)) {
      try { await response.body?.cancel(); } catch {}
      throw failure(code);
    }
  }
  const reader = response.body?.getReader?.();
  if (!reader) throw failure(code);
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw failure(code);
      size += value.byteLength;
      if (size > maximum) { try { await reader.cancel(); } catch {} throw failure(code); }
      chunks.push(Buffer.from(value));
    }
  } catch (error) {
    try { await reader.cancel(); } catch {}
    if (error?.code === code) throw error;
    throw failure(code, true);
  } finally {
    try { reader.releaseLock(); } catch {}
  }
  const encoding=response.headers?.get?.("content-encoding");
  if (requireCompleteLength && length != null && (!encoding || encoding.trim().toLowerCase() === "identity") && BigInt(length) !== BigInt(size)) throw failure(code,true);
  return Buffer.concat(chunks, size);
}

async function fetchResponse(fetchImpl, url, init, timeoutMs, code) {
  let response;
  try { response = await fetchImpl(url, { ...init, redirect: "error", signal: AbortSignal.timeout(timeoutMs) }); }
  catch { throw failure(code, true); }
  if (!response || typeof response.status !== "number") throw failure(code, true);
  if (typeof response.url === "string" && response.url !== "" && response.url !== url) throw failure(code === "DOWNLOAD_FAILED" ? "DOWNLOAD_REDIRECTED" : code);
  return response;
}

async function jsonBody(response, maximum, code, requireCompleteLength = false) {
  const bytes = await boundedBytes(response, maximum, code, requireCompleteLength);
  try { return { value: JSON.parse(bytes.toString("utf8")), bytes }; } catch { throw failure(code); }
}

function parseAttributes(source) {
  const attributes = new Map();
  let index = 0;
  while (index < source.length) {
    while (/\s/.test(source[index] ?? "")) index += 1;
    if (index >= source.length) break;
    const match = /^[^\s"'<>\/=]+/.exec(source.slice(index));
    if (!match) throw failure("PRICE_NOT_VERIFIABLE");
    const name = match[0].toLowerCase();
    if (attributes.has(name)) throw failure("PRICE_NOT_VERIFIABLE");
    index += match[0].length;
    while (/\s/.test(source[index] ?? "")) index += 1;
    let value = null;
    if (source[index] === "=") {
      index += 1;
      while (/\s/.test(source[index] ?? "")) index += 1;
      const quote = source[index];
      if (quote === "\"" || quote === "'") {
        const end = source.indexOf(quote, ++index);
        if (end < 0) throw failure("PRICE_NOT_VERIFIABLE");
        value = source.slice(index, end); index = end + 1;
      } else {
        const unquoted = /^[^\s"'=<>`]+/.exec(source.slice(index));
        if (!unquoted) throw failure("PRICE_NOT_VERIFIABLE");
        value = unquoted[0]; index += value.length;
      }
    }
    attributes.set(name, value);
  }
  return attributes;
}

function visiblePricingParagraphs(source, canonicalUrl) {
  const markup = source.replace(/<!--[\s\S]*?-->/g, "").replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
  const voids = new Set(["area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"]);
  const inert = new Set(["template","textarea","title","xmp","iframe","noembed","noframes"]);
  const stack = [];
  const paragraphs = [];
  const canonicals = [];
  let paragraph = null;
  for (const token of markup.match(/<[^>]*>|[^<]+/g) ?? []) {
    if (!token.startsWith("<")) { if (paragraph !== null && !stack.at(-1)?.hidden) paragraph.text += token; continue; }
    if (/^<!doctype\b[^>]*>$/i.test(token)) continue;
    const closing = /^<\s*\/\s*([A-Za-z][A-Za-z0-9:-]*)\s*>$/.exec(token);
    if (closing) {
      const frame = stack.at(-1);
      if (!frame || frame.tag !== closing[1].toLowerCase()) throw failure("PRICE_NOT_VERIFIABLE");
      stack.pop();
      if (frame.startsParagraph) { if (!frame.hidden) paragraphs.push(paragraph.text); paragraph = null; }
      else if (paragraph !== null && !frame.hidden) paragraph.text += " ";
      continue;
    }
    const opening = /^<\s*([A-Za-z][A-Za-z0-9:-]*)\b([^>]*)>$/.exec(token);
    if (!opening) throw failure("PRICE_NOT_VERIFIABLE");
    const tag = opening[1].toLowerCase();
    let attributeSource = opening[2];
    const selfClosing = /\/\s*$/.test(attributeSource);
    if (selfClosing) attributeSource = attributeSource.replace(/\/\s*$/, "");
    if (selfClosing && !voids.has(tag)) throw failure("PRICE_NOT_VERIFIABLE");
    const attributes = parseAttributes(attributeSource);
    const style = attributes.get("style");
    const hidden = Boolean(stack.at(-1)?.hidden) || inert.has(tag) || attributes.has("hidden")
      || attributes.get("aria-hidden")?.toLowerCase() === "true"
      || (style !== null && /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)/i.test(style));
    if (tag === "link" && !hidden && attributes.get("rel")?.toLowerCase().split(/\s+/).includes("canonical")) canonicals.push(attributes.get("href"));
    const startsParagraph = tag === "p";
    if (startsParagraph) { if (paragraph !== null || selfClosing) throw failure("PRICE_NOT_VERIFIABLE"); paragraph = { text: "" }; }
    else if (paragraph !== null && !hidden) paragraph.text += " ";
    if (!selfClosing && !voids.has(tag)) stack.push({ tag, hidden, startsParagraph });
  }
  if (stack.length || paragraph !== null || canonicals.length !== 1 || canonicals[0] !== canonicalUrl) throw failure("PRICE_NOT_VERIFIABLE");
  return paragraphs;
}

function visibleNanoPricing(source) {
  const candidates = [];
  for (const raw of visiblePricingParagraphs(source, NANO_PUBLIC_URL)) {
    const text = raw.replace(/&nbsp;|&#160;|&#xA0;/gi, " ").replace(/&#36;|&#x24;|&dollar;/gi, "$").replace(/\s+/g, " ").trim();
    const bases = [...text.matchAll(/Your request will cost\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*per image\b/gi)];
    const multipliers = [...text.matchAll(/2K and 4K outputs will be charged at\s*([0-9]+(?:\.[0-9]+)?)\s*times and\s*[0-9]+(?:\.[0-9]+)?\s*times the standard rate, respectively\b/gi)];
    if (bases.length === 1 && multipliers.length === 1) candidates.push({
      base: exactNumericDecimal(bases[0][1]), baseDecimal: normalizedDecimal(bases[0][1]),
      multiplier: exactNumericDecimal(multipliers[0][1]), multiplierDecimal: normalizedDecimal(multipliers[0][1]),
    });
    else if (bases.length || multipliers.length) throw failure("PRICE_NOT_VERIFIABLE");
  }
  if (candidates.length !== 1) throw failure("PRICE_NOT_VERIFIABLE");
  return candidates[0];
}

function visibleH3Pricing(source) {
  const candidates = [];
  for (const raw of visiblePricingParagraphs(source, H3_PUBLIC_URL)) {
    const text = raw.replace(/&nbsp;|&#160;|&#xA0;/gi, " ").replace(/&#36;|&#x24;|&dollar;/gi, "$").replace(/\s+/g, " ").trim();
    const matches = [...text.matchAll(/Video costs\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*per second at\s*480p\s*,\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*per second at\s*768p\b/gi)];
    if (matches.length === 1) candidates.push({base: exactNumericDecimal(matches[0][1]), baseDecimal: normalizedDecimal(matches[0][1]), high: exactNumericDecimal(matches[0][2]), highDecimal: normalizedDecimal(matches[0][2])});
    else if (matches.length || /Video costs/i.test(text)) throw failure("PRICE_NOT_VERIFIABLE");
  }
  if (candidates.length !== 1) throw failure("PRICE_NOT_VERIFIABLE");
  return candidates[0];
}
function reservedUnitsFor(context) {
  return context.config.model === H3 ? context.config.input.duration + 1 : VIDEO_MODELS.has(context.config.model) ? Math.ceil(context.guideMetadata.duration_seconds) + 1 : context.config.input.num_images ?? 1;
}
function planningBasis(context) {
  return context.config.model === H3 ? "configured_duration+1_reserved_output_second" : VIDEO_MODELS.has(context.config.model) ? "ceil(input_video_duration_seconds)+1_reserved_output_second" : "configured_output_count";
}

function selectedPrice(raw, model) {
  if (!isObject(raw) || !Array.isArray(raw.prices)) throw failure("PRICE_NOT_VERIFIABLE");
  const matches = raw.prices.filter((item) => isObject(item) && item.endpoint_id === model);
  if (matches.length !== 1) throw failure("PRICE_NOT_VERIFIABLE");
  const selected = matches[0];
  const expectedUnits = VIDEO_MODELS.has(model) ? new Set(["seconds"]) : new Set(["image", "images"]);
  if (selected.currency !== "USD" || !expectedUnits.has(selected.unit)) throw failure("PRICE_NOT_VERIFIABLE");
  let apiMultiplierDecimal = null;
  if (Object.hasOwn(selected, "resolution_multipliers")) {
    if (!isObject(selected.resolution_multipliers)) throw failure("PRICE_NOT_VERIFIABLE");
    if (Object.hasOwn(selected.resolution_multipliers, "2K")) {
      apiMultiplierDecimal = normalizedDecimal(selected.resolution_multipliers["2K"]);
      exactNumericDecimal(apiMultiplierDecimal);
    }
  }
  const unitPriceDecimal = normalizedDecimal(selected.unit_price);
  return { unitPrice: exactNumericDecimal(unitPriceDecimal), unitPriceDecimal, unit: selected.unit, apiMultiplierDecimal };
}

function parsedApiPricing(bytes) {
  try {
    return JSON.parse(bytes.toString("utf8"), (key, value, context) => {
      if (!["unit_price", "2K"].includes(key) || typeof value !== "number") return value;
      // JSON.parse otherwise rounds long numeric API lexemes before we can reserve them.
      if (typeof context?.source !== "string") throw failure("PRICE_NOT_VERIFIABLE");
      return context.source;
    });
  } catch (error) {
    if (error?.code === "PRICE_NOT_VERIFIABLE") throw error;
    throw failure("PRICE_NOT_VERIFIABLE");
  }
}

function fsyncDirectory(directory) {
  let descriptor;
  try { descriptor = fs.openSync(directory, "r"); fs.fsyncSync(descriptor); } catch {} finally { if (descriptor !== undefined) fs.closeSync(descriptor); }
}

function writeNewDurable(candidate, bytes) {
  let descriptor;
  try {
    descriptor = fs.openSync(candidate, "wx");
    fs.writeFileSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
  } finally { if (descriptor !== undefined) fs.closeSync(descriptor); }
  fsyncDirectory(path.dirname(candidate));
}

function readState(root) {
  const candidate = path.join(root, "job-state.json");
  if (!fs.existsSync(candidate)) return null;
  return readJsonFile(candidate, "STATE_INVALID").value;
}

function writeState(root, next) {
  const lock = path.join(root, ".job-state.lock");
  let lockDescriptor;
  const deadline = Date.now() + 2_000;
  while (lockDescriptor === undefined) {
    try {
      lockDescriptor = fs.openSync(lock, "wx");
      fs.writeFileSync(lockDescriptor, `${process.pid}\n`);
      fs.fsyncSync(lockDescriptor);
      fsyncDirectory(root);
    } catch (error) {
      if (error?.code !== "EEXIST" || Date.now() >= deadline) throw failure("STATE_LOCKED", true);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
    }
  }
  try {
    const current = readState(root);
    if (next.state === "SEEDREAM_CAPTURED" && current && (
      (current.config_sha256 !== undefined && current.config_sha256 !== next.config_sha256)
      || (current.request_id != null && current.request_id !== next.request_id)
      || ["DOWNLOADED","RESULT_INVALID","FAILED","CANCELLED","REMOTE_REJECTED"].includes(current.state)
      || (current.seedream_capture != null && !canonicalBytes(current.seedream_capture).equals(canonicalBytes(next.seedream_capture)))
      || (current.raw_path != null && current.raw_path !== next.raw_path) || (current.raw_sha256 != null && current.raw_sha256 !== next.raw_sha256)
      || (current.pricing_receipt_sha256 !== undefined && next.pricing_receipt_sha256 !== undefined && current.pricing_receipt_sha256 !== next.pricing_receipt_sha256)
      || ["status_url","result_url"].some(k=>current[k] !== undefined && next[k] !== undefined && current[k] !== next[k]))) throw failure("SEEDREAM_CAPTURE_CONFLICT");
    if (current?.state === "DOWNLOADED" || (current?.state === "RESULT_INVALID" && validHash(current.result_invalid_sha256))) return current;
    if (current?.request_id && current.request_id === next.request_id
      && (STATE_RANK.get(current.state) ?? -1) > (STATE_RANK.get(next.state) ?? -1)) return current;
    const target = path.join(root, "job-state.json");
    const temporary = path.join(root, `.job-state.${randomUUID()}.partial`);
    const bytes = canonicalBytes(next);
    writeNewDurable(temporary, bytes);
    fs.renameSync(temporary, target);
    fsyncDirectory(root);
    return next;
  } finally {
    fs.closeSync(lockDescriptor);
    try { fs.unlinkSync(lock); fsyncDirectory(root); } catch {}
  }
}

function stateOutput(state, overrides = {}) {
  return output({
    state: state.state,
    request_id: state.request_id ?? null,
    estimated_cost_usd: state.estimated_cost_usd ?? null,
    raw_path: state.raw_path ?? null,
    raw_sha256: state.raw_sha256 ?? null,
    retry_safe: ["IN_QUEUE", "IN_PROGRESS", "REQUEST_ACCEPTED"].includes(state.state),
    ...overrides,
  });
}

function receiptDirectory(root, planning = false) {
  const dir = path.join(root, planning ? "planning-pricing-receipts" : "pricing-receipts");
  if (!fs.existsSync(dir)) { fs.mkdirSync(dir); fsyncDirectory(root); }
  const stat = fs.lstatSync(dir);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw failure("PRICE_RECEIPT_INVALID");
  return dir;
}

function loadCleanupPlan(jobDir) {
  const root = ensureJobDir(jobDir);
  const record = readJsonFile(path.join(root, "plan.json"), "CLEANUP_PLAN_INVALID");
  const plan = record.value;
  rejectRegionalDowngrade(root,plan?.schema === "sprite_regional_pricing_request_v1");
  if (plan?.schema === "sprite_regional_pricing_request_v1") {
    if (!exactKeys(plan,["schema","regional_plan_sha256","role","model"]) || plan.role !== "cleanup" || plan.model !== CLEANUP || !validHash(plan.regional_plan_sha256)) throw failure("REGIONAL_PLAN_INVALID");
    const regional = regionalPlan(regionalLayout(root,"cleanup"));
    if (regional.planSha256 !== plan.regional_plan_sha256) throw failure("REGIONAL_PLAN_INVALID");
    return {root,config:{role:"cleanup",model:CLEANUP,input:{sync_mode:false}},regional:true,planning:true,...regional};
  }
  if (plan?.schema === "sprite_endpoint_pricing_request_v1") {
    if (!exactKeys(plan, ["schema", "endpoint_plan_sha256", "role", "model"]) || !["video_ab", "video_ba", "cleanup"].includes(plan.role)
      || plan.model !== ENDPOINT_MODELS[plan.role] || !validHash(plan.endpoint_plan_sha256)) throw failure("ENDPOINT_PLAN_INVALID");
    const runRoot = endpointLayout(root, plan.role);
    const endpoint = endpointPlan(runRoot);
    if (endpoint.planSha256 !== plan.endpoint_plan_sha256) throw failure("ENDPOINT_PLAN_INVALID");
    return { root, config:{model:plan.model,role:plan.role,input:plan.role === "cleanup" ? {sync_mode:false} : endpoint.plan.video_policy},
      ...endpoint, endpoint:true, planning:true, runRoot };
  }
  if (!exactKeys(plan, ["schema", "primary_operation_id", "primary_config_sha256", "model", "input", "reserved_units", "source_from", "source_limits"])
    || plan.schema !== "sprite_deferred_cleanup_v1" || plan.model !== CLEANUP || !validHash(plan.primary_config_sha256)
    || typeof plan.primary_operation_id !== "string" || !/^.+:attempt-\d{2}:primary$/.test(plan.primary_operation_id)
    || !exactKeys(plan.input, ["sync_mode"]) || plan.input.sync_mode !== false || plan.reserved_units !== 1
    || !["primary_raw_png", "video_sample_grid_v1"].includes(plan.source_from)
    || !exactKeys(plan.source_limits, ["mime", "max_bytes", "max_width", "max_height"])
    || plan.source_limits.mime !== "image/png" || plan.source_limits.max_bytes !== 10485760
    || plan.source_limits.max_width !== 4096 || plan.source_limits.max_height !== 4096) throw failure("CLEANUP_PLAN_INVALID");
  return { root, config: { model: CLEANUP, input: plan.input }, planSha256: sha256(record.bytes), planning: true };
}

function quoteOutput(context, receiptPath, bytes, amount) {
  return {
    receipt_path: receiptPath, receipt_sha256: sha256(bytes), estimated_cost_usd_decimal: amount,
    ...(context.planning ? context.regional ? {regional_plan_sha256:context.planSha256,role:context.config.role} : context.endpoint ? { endpoint_plan_sha256:context.planSha256, role:context.config.role } : { plan_sha256: context.planSha256 } : { config_sha256: context.configSha256, config_file_sha256: context.configFileSha256 }),
  };
}

function endpointPricingBasis(context) {
  return context.config.role === "cleanup" ? "joined_grid_one_image_v1" : context.config.role === "pose_edit" ? "configured_output_count" : "wan_720p_81_video_v1";
}

function endpointApiPrice(bytes, model) {
  const value = parsedApiPricing(bytes);
  if (!isObject(value) || !Array.isArray(value.prices)) throw failure("PRICE_NOT_VERIFIABLE");
  if (model === SEEDREAM && (!exactKeys(value,["prices","has_more","next_cursor"]) || value.has_more !== false || value.next_cursor !== null
    || value.prices.length !== 1 || !exactKeys(value.prices[0],["endpoint_id","currency","unit","unit_price"]))) throw failure("PRICE_NOT_VERIFIABLE");
  const matching = value.prices.filter(p => isObject(p) && p.endpoint_id === model);
  if (matching.length !== 1 || matching[0].currency !== "USD" || matching[0].unit !== (model === FLF ? "videos" : "images")) throw failure("PRICE_NOT_VERIFIABLE");
  const selected = matching[0];
  if (selected.resolution_multipliers !== undefined && (model !== NANO || !isObject(selected.resolution_multipliers)
    || normalizedDecimal(selected.resolution_multipliers["2K"]) !== "1.5")) throw failure("PRICE_NOT_VERIFIABLE");
  const decimal = normalizedDecimal(selected.unit_price);
  return { unit:selected.unit, unitPriceDecimal:decimal, unitPrice:exactNumericDecimal(decimal) };
}

function endpointSchemaUrl(model) {
  const url = new URL("https://fal.ai/api/openapi/queue/openapi.json");
  url.searchParams.set("endpoint_id", model);
  return url.toString();
}

function verifiedEndpointSchema(bytes, model) {
  const expected = ENDPOINT_SCHEMAS[model];
  if (!expected || sha256(bytes) !== expected.sha256) throw failure("PRICE_NOT_VERIFIABLE");
  let schema;
  try { schema = JSON.parse(bytes.toString("utf8")); } catch { throw failure("PRICE_NOT_VERIFIABLE"); }
  const input = schema?.components?.schemas?.[expected.name];
  const properties = input?.properties;
  const post = schema?.paths?.[`/${model}`]?.post?.requestBody?.content?.["application/json"]?.schema;
  if (!isObject(properties) || post?.$ref !== `#/components/schemas/${expected.name}`) throw failure("PRICE_NOT_VERIFIABLE");
  if (model === FLF && (properties.num_frames?.default !== 81 || properties.resolution?.default !== "720p"
    || properties.num_inference_steps?.default !== 30 || properties.enable_prompt_expansion?.default !== false
    || !Array.isArray(properties.loras?.default) || properties.loras.default.length !== 0
    || !properties.resolution.description.includes("720p is 1 billing unit")
    || !properties.num_frames.description.includes("1.25x more billing units"))) throw failure("PRICE_NOT_VERIFIABLE");
  if (model === NANO && (properties.safety_tolerance?.default !== "4" || properties.enable_web_search?.default !== false
    || properties.enable_image_search?.default !== false || properties.return_history?.default !== false
    || properties.image_passthrough?.default !== false || properties.system_prompt?.default !== ""
    || !properties.thinking_level?.description?.includes("provider default ('minimal')"))) throw failure("PRICE_NOT_VERIFIABLE");
  if (model === CLEANUP && properties.sync_mode?.default !== false) throw failure("PRICE_NOT_VERIFIABLE");
  if (model === SEEDREAM && (properties.num_images?.default !== 1 || properties.max_images?.default !== 1
    || properties.sync_mode?.default !== false || properties.enable_safety_checker?.default !== true
    || properties.image_size?.default?.width !== 2048 || properties.image_size?.default?.height !== 2048)) throw failure("PRICE_NOT_VERIFIABLE");
  return { input_schema:expected.name, policy:model === SEEDREAM ? "seedream45_one_image_v1" : "endpoint_cycle_v1" };
}

function endpointStreamingLink(source) {
  const stack = [];
  let previous = null;
  for (let index = 0; index < source.length; index++) {
    const char = source[index];
    if (/\s/.test(char)) continue;
    if (char === "/" && source[index + 1] === "/") {
      const end = source.indexOf("\n", index + 2);
      if (end < 0) return null;
      index = end; continue;
    }
    if (char === "/" && source[index + 1] === "*") {
      const end = source.indexOf("*/", index + 2);
      if (end < 0) return null;
      index = end + 1; continue;
    }
    // Templates and regexp/division contexts are outside the reviewed bootstrap grammar.
    if (char === "`" || char === "/") return null;
    if (char === "\"" || char === "'") {
      const quote = char;
      let closed = false;
      for (index++; index < source.length; index++) {
        if (source[index] === "\\") { index++; continue; }
        if (source[index] === quote) { closed = true; break; }
        if (source[index] === "\n" || source[index] === "\r") return null;
      }
      if (!closed) return null;
      previous = "literal"; continue;
    }
    if (!stack.length && (previous === null || previous === ";") && /^\$(?:RC|RR)\(/.test(source.slice(index))) {
      const call = /^\$(RC|RR)\(([\s\S]*)\)\s*;?\s*$/.exec(source.slice(index));
      if (!call) return null;
      let args;
      try { args = JSON.parse(`[${call[2]}]`); } catch { return null; }
      if (!Array.isArray(args) || args.length !== (call[1] === "RC" ? 2 : 3)
        || typeof args[0] !== "string" || !/^B:\d{1,12}$/.test(args[0]) || args[1] !== `S:${args[0].slice(2)}`) return null;
      if (call[1] === "RR" && (!Array.isArray(args[2]) || !args[2].length || args[2].some(css => !Array.isArray(css) || css.length !== 2
        || typeof css[0] !== "string" || !css[0].startsWith("/_next/static/") || css[1] !== "next"))) return null;
      return {slot:args[1],boundary:args[0]};
    }
    if ("({[".includes(char)) stack.push(char);
    else if (")}]".includes(char) && stack.pop() !== { ")":"(", "}":"{", "]":"[" }[char]) return null;
    previous = char;
  }
  return null;
}

function endpointCssVisibility(styles) {
  const selectors = [];
  let uncertain = false;
  for (const original of styles) {
    const css = original.replace(/\/\*[\s\S]*?\*\//g, "");
    // Escaped CSS is outside the supported grammar; it can disguise a visibility property.
    if (css.includes("\\")) { uncertain = true; continue; }
    const visibilityProperty = /(?:^|[;{])\s*(?:display|visibility|content-visibility|opacity)\s*:/i;
    if (!visibilityProperty.test(css)) continue;
    if (css.includes("@")) uncertain = true;
    let remaining = css;
    for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      remaining = remaining.replace(match[0], "");
      const declarations = match[2];
      if (!/(?:^|;)\s*(?:display|visibility|content-visibility|opacity)\s*:/i.test(declarations)) continue;
      let hidden = false;
      const known = {display:["none","block","inline","inline-block","flex","inline-flex","grid","inline-grid","contents","table","list-item"],visibility:["visible","hidden","collapse"],"content-visibility":["visible","auto","hidden"]};
      for (const declaration of declarations.split(";")) {
        if (!/^\s*(?:display|visibility|content-visibility|opacity)\s*:/i.test(declaration)) continue;
        const property = /^\s*(display|visibility|content-visibility|opacity)\s*:\s*([^!]+?)\s*(?:!important\s*)?$/i.exec(declaration);
        if (!property) { uncertain = true; continue; }
        const name = property[1].toLowerCase();
        const value = property[2].trim().toLowerCase();
        if (name === "opacity") {
          if (!/^(?:0|1|0?\.\d+|1\.0+)$/.test(value) || Number(value) > 1) uncertain = true;
          else if (Number(value) === 0) hidden = true;
        } else if (!known[name].includes(value)) uncertain = true;
        else if (["none","hidden","collapse"].includes(value)) hidden = true;
      }
      // Only ordinary, flat selectors are supported. Conditional/complex visibility fails closed.
      for (const selector of match[1].split(",").map(s => s.trim())) {
        if (!/^(?:[A-Za-z][\w-]*)?(?:\.[A-Za-z_][\w-]*|#[A-Za-z_][\w-]*)*$/.test(selector) || !selector) uncertain = true;
        else if (hidden) selectors.push(selector);
      }
    }
    if (visibilityProperty.test(remaining)) uncertain = true;
  }
  return {uncertain, hidden(tag, attrs) {
    const classes = new Set((attrs.get("class") ?? "").split(/\s+/));
    return selectors.some(selector => {
      const type = /^[A-Za-z][\w-]*/.exec(selector)?.[0];
      if (type && type.toLowerCase() !== tag) return false;
      return [...selector.matchAll(/([.#])([\w-]+)/g)].every(([,kind,name]) => kind === "." ? classes.has(name) : attrs.get("id") === name);
    });
  }};
}

function endpointPricingParagraphs(bytes, model) {
  // FAL's SSR resolves Suspense templates using literal $RC/$RR slot references.
  // Read that bounded linkage as data; never execute scripts or trust arbitrary hidden text.
  let text;
  try { text = new TextDecoder("utf-8", {fatal:true}).decode(bytes); } catch { throw failure("PRICE_NOT_VERIFIABLE"); }
  const links = new Map();
  const styles = [];
  const withoutScripts = text.replace(/<(script|style)\b([^>]*)>([\s\S]*?)<\/\1\s*>/gi, (_all, tag, attributes, content) => {
    const attrs = parseAttributes(attributes);
    if (tag.toLowerCase() === "style") styles.push(content);
    if (tag.toLowerCase() === "script" && (!attrs.has("type") || ["text/javascript","application/javascript"].includes(attrs.get("type")))) {
      const call = endpointStreamingLink(content);
      if (call) {
        if (links.has(call.slot)) throw failure("PRICE_NOT_VERIFIABLE");
        links.set(call.slot,call.boundary);
      }
    }
    return "";
  });
  const css = endpointCssVisibility(styles);
  const boundaries = new Set();
  for (const match of withoutScripts.matchAll(/<!--\$\?-->\s*<template\b([^>]*)>/gi)) {
    const id = parseAttributes(match[1]).get("id");
    if (/^B:\d{1,12}$/.test(id ?? "")) {
      if (boundaries.has(id)) throw failure("PRICE_NOT_VERIFIABLE");
      boundaries.add(id);
    }
  }
  const markup = withoutScripts.replace(/<!--[\s\S]*?-->/g, "");
  const voids = new Set(["area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"]);
  const inert = new Set(["template","textarea","title","xmp","iframe","noembed","noframes","noscript"]);
  const stack = [];
  const placeholders = new Map();
  const slots = new Set();
  const paragraphs = [];
  const canonicals = [];
  let paragraph = null;
  for (const token of markup.match(/<[^>]*>|[^<]+/g) ?? []) {
    if (!token.startsWith("<")) {
      if (paragraph !== null) {
        paragraph.text += token;
        if (token.trim() && stack.at(-1)?.hidden) paragraph.hidden = true;
      } else if (/Your request will cost|For a video generation, your request will cost/i.test(token)) throw failure("PRICE_NOT_VERIFIABLE");
      continue;
    }
    if (/^<!doctype\b[^>]*>$/i.test(token)) continue;
    const closing = /^<\s*\/\s*([A-Za-z][A-Za-z0-9:-]*)\s*>$/.exec(token);
    if (closing) {
      const frame = stack.pop();
      if (!frame || frame.tag !== closing[1].toLowerCase()) throw failure("PRICE_NOT_VERIFIABLE");
      if (frame.startsParagraph) { paragraphs.push(paragraph); paragraph = null; }
      else if (paragraph !== null) paragraph.text += " ";
      continue;
    }
    const opening = /^<\s*([A-Za-z][A-Za-z0-9:-]*)\b([^>]*)>$/.exec(token);
    if (!opening) throw failure("PRICE_NOT_VERIFIABLE");
    const tag = opening[1].toLowerCase();
    const selfClosing = /\/\s*$/.test(opening[2]);
    const attrs = parseAttributes(selfClosing ? opening[2].replace(/\/\s*$/, "") : opening[2]);
    const parent = stack.at(-1) ?? {hidden:false,streams:[]};
    const id = attrs.get("id");
    const streamed = tag === "div" && attrs.has("hidden") && links.has(id) && attrs.size === 2;
    if (streamed) {
      if (slots.has(id)) throw failure("PRICE_NOT_VERIFIABLE");
      slots.add(id);
    }
    if (tag === "template" && boundaries.has(id)) {
      if (placeholders.has(id)) throw failure("PRICE_NOT_VERIFIABLE");
      placeholders.set(id,{hidden:parent.hidden,streams:[...parent.streams]});
    }
    const style = attrs.get("style");
    const hidden = parent.hidden || inert.has(tag) || attrs.has("inert") || (attrs.has("hidden") && !streamed) || css.hidden(tag,attrs)
      || attrs.get("aria-hidden")?.toLowerCase() === "true"
      || (typeof style === "string" && /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)/i.test(style));
    const streams = streamed ? [...parent.streams,id] : parent.streams;
    if (tag === "link" && !hidden && attrs.get("rel")?.toLowerCase().split(/\s+/).includes("canonical")) canonicals.push(attrs.get("href"));
    const startsParagraph = tag === "p";
    if (startsParagraph) {
      if (paragraph !== null || selfClosing) throw failure("PRICE_NOT_VERIFIABLE");
      paragraph = {text:"",hidden,streams};
    } else if (paragraph !== null) paragraph.text += " ";
    if (!selfClosing && !voids.has(tag)) stack.push({tag,hidden,streams,startsParagraph});
  }
  if (stack.length || paragraph !== null || canonicals.length !== 1 || canonicals[0] !== `https://fal.ai/models/${model}`) throw failure("PRICE_NOT_VERIFIABLE");
  const visibleSlot = (slot, ancestry = new Set()) => {
    const placeholder = placeholders.get(links.get(slot));
    if (!slots.has(slot) || !placeholder || placeholder.hidden || ancestry.has(slot)) return false;
    const next = new Set([...ancestry,slot]);
    return placeholder.streams.every(parent => visibleSlot(parent,next));
  };
  const candidates = paragraphs.filter(p=>/Your request will cost|For a video generation, your request will cost/i.test(p.text));
  if (!candidates.length || css.uncertain || candidates.some(p=>p.hidden || p.streams.some(s=>!visibleSlot(s)))) throw failure("PRICE_NOT_VERIFIABLE");
  return candidates.map(p=>p.text);
}

function visibleEndpointWanPricing(bytes) {
  const values = [];
  for (const raw of endpointPricingParagraphs(bytes, FLF)) {
    const value = raw.replace(/&nbsp;|&#160;|&#xA0;/gi, " ").replace(/&#36;|&#x24;|&dollar;/gi, "$").replace(/\s+/g, " ").trim();
    const matches = [...value.matchAll(/For a video generation, your request will cost\s*\$\s*[0-9]+(?:\.[0-9]+)?\s*at 480p resolution and\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*at 720p resolution\./gi)];
    if (matches.length === 1 && /with the default frames\./i.test(value) && /More than the default frames will cost 1\.25x more\./i.test(value)) values.push(normalizedDecimal(matches[0][1]));
    else throw failure("PRICE_NOT_VERIFIABLE");
  }
  if (!values.length || new Set(values).size !== 1) throw failure("PRICE_NOT_VERIFIABLE");
  return values[0];
}

function endpointPublicSelection(bytes, model, apiPrice) {
  if (model === SEEDREAM) {
    const url = `https://fal.ai/models/${SEEDREAM}`;
    // Corroboration checks only canonical identity. Price authority is API + frozen schema.
    const markup = bytes.toString("utf8").replace(/<!--[\s\S]*?-->/g, "").replace(/<(script|style|template)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
    const canonicals = [];
    for (const tag of markup.match(/<link\b[^>]*>/gi) ?? []) {
      const attributes = parseAttributes(tag.replace(/^<link\b/i,"").replace(/>$/, "").replace(/\/\s*$/, ""));
      if (attributes.get("rel")?.toLowerCase().split(/\s+/).includes("canonical")) canonicals.push(attributes.get("href"));
    }
    if (canonicals.length !== 1 || canonicals[0] !== url) throw failure("PRICE_NOT_VERIFIABLE");
    return {corroboration_only:true,canonical_url:url};
  }
  if (model === NANO) {
    for (const paragraph of endpointPricingParagraphs(bytes,model)) {
      const normalized = paragraph.replace(/&nbsp;|&#160;|&#xA0;/gi," ").replace(/&#36;|&#x24;|&dollar;/gi,"$").replace(/\s+/g," ").trim();
      const bases = [...normalized.matchAll(/Your request will cost\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*per image\b/gi)];
      const multipliers = [...normalized.matchAll(/2K and 4K outputs will be charged at\s*([0-9]+(?:\.[0-9]+)?)\s*times and\s*[0-9]+(?:\.[0-9]+)?\s*times the standard rate, respectively\b/gi)];
      if (bases.length !== 1 || multipliers.length !== 1 || normalizedDecimal(bases[0][1]) !== apiPrice.unitPriceDecimal || normalizedDecimal(multipliers[0][1]) !== "1.5") throw failure("PRICE_NOT_VERIFIABLE");
    }
    return { base_unit_price_usd:apiPrice.unitPrice, resolution:"2K", resolution_multiplier:1.5 };
  }
  if (visibleEndpointWanPricing(bytes) !== apiPrice.unitPriceDecimal) throw failure("PRICE_NOT_VERIFIABLE");
  return { resolution:"720p", frames:81, billing_units:1, base_unit_price_usd:apiPrice.unitPrice };
}

export function validateEndpointPricingEvidence(model, apiBytes, publicBytesOrNull, schemaBytes) {
  if (!Object.hasOwn(ENDPOINT_SCHEMAS,model) || !(apiBytes instanceof Uint8Array) || !(schemaBytes instanceof Uint8Array)
    || (model === CLEANUP ? publicBytesOrNull !== null : !(publicBytesOrNull instanceof Uint8Array))) throw failure("PRICE_NOT_VERIFIABLE");
  const api = endpointApiPrice(Buffer.from(apiBytes),model);
  return {api:{endpoint_id:model,currency:"USD",unit:api.unit,unit_price_usd:api.unitPrice},
    public:publicBytesOrNull === null ? null : endpointPublicSelection(Buffer.from(publicBytesOrNull),model,api),
    schema:verifiedEndpointSchema(Buffer.from(schemaBytes),model)};
}

async function endpointPricing(context, fetchImpl, key, now) {
  if (typeof key !== "string" || key.trim() === "") throw failure("KEY_SETUP_REQUIRED", true);
  const model = context.config.model;
  const apiUrl = new URL(API_PRICING_URL);
  apiUrl.searchParams.set("endpoint_id", model);
  const get = async (url, privateRequest = false) => {
    const response = await fetchResponse(fetchImpl, url, {method:"GET", headers:privateRequest ? {Authorization:`Key ${key}`} : {}}, 15_000, "PRICE_FETCH_FAILED");
    if (!response.ok) throw failure("PRICE_FETCH_FAILED", true);
    return boundedBytes(response, MAX_PRICING_BYTES, "PRICE_FETCH_FAILED");
  };
  const apiBytes = await get(apiUrl.toString(), true);
  const api = endpointApiPrice(apiBytes, model);
  const publicUrl = `https://fal.ai/models/${model}`;
  const publicBytes = model === CLEANUP ? null : await get(publicUrl);
  const selectedPublic = publicBytes === null ? null : endpointPublicSelection(publicBytes, model, api);
  const schemaBytes = await get(endpointSchemaUrl(model));
  const selectedSchema = verifiedEndpointSchema(schemaBytes, model);
  const retrieved = now();
  if (!Number.isFinite(retrieved)) throw failure("CLOCK_INVALID");
  const receiptId = randomUUID();
  const retrievedAt = new Date(retrieved).toISOString();
  const source = (url, suffix, bytes, selected) => ({source_url:url,retrieved_at:retrievedAt,body_sha256:sha256(bytes),archive_file:`${receiptId}.${suffix}.body`,selected});
  const sources = [source(apiUrl.toString(), "api", apiBytes, {endpoint_id:model,currency:"USD",unit:api.unit,unit_price_usd:api.unitPrice})];
  if (publicBytes !== null) sources.push(source(publicUrl,"public",publicBytes,selectedPublic));
  sources.push(source(endpointSchemaUrl(model),"schema",schemaBytes,selectedSchema));
  const multiplier = model === NANO ? "1.5" : "1";
  const amount = decimalProduct(api.unitPriceDecimal, multiplier);
  const estimate = exactNumericDecimal(amount);
  const receipt = context.planning ? {
    schema:"sprite_endpoint_planning_quote_v1", receipt_id:receiptId, endpoint_plan_sha256:context.planSha256, role:context.config.role, model,
    retrieved_at:retrievedAt,expires_at:new Date(retrieved+PRICE_MAX_AGE_MS).toISOString(),currency:"USD",api_unit:api.unit,unit_price_usd:api.unitPriceDecimal,
    estimated_cost_usd:amount,reserved_units:1,multiplier_decimal:multiplier,planning_basis:endpointPricingBasis(context),sources,
  } : {
    schema_version:3,receipt_id:receiptId,config_sha256:context.configSha256,model,retrieved_at:retrievedAt,expires_at:new Date(retrieved+PRICE_MAX_AGE_MS).toISOString(),
    currency:"USD",api_unit:api.unit,base_unit_price_usd:api.unitPrice,resolution_multiplier:model===NANO?1.5:null,effective_unit_price_usd:estimate,
    estimated_cost_usd:estimate,estimated_cost_usd_decimal:amount,planning_basis:endpointPricingBasis(context),input_duration_seconds:null,reserved_units:1,driving_video_sha256:null,sources,
  };
  const directory = receiptDirectory(context.root, context.planning);
  const bodyList = [apiBytes,...(publicBytes===null?[]:[publicBytes]),schemaBytes];
  sources.forEach((entry,i)=>writeNewDurable(path.join(directory,entry.archive_file),bodyList[i]));
  const bytes = canonicalBytes(receipt);
  const candidate = path.join(directory,`${String(retrieved).padStart(16,"0")}-${receiptId}.receipt.json`);
  writeNewDurable(candidate,bytes);
  return output({state:"PRICED",estimated_cost_usd:estimate,retry_safe:true,quote:quoteOutput(context,candidate,bytes,amount)});
}

function regionalApiPrice(bytes, model) {
  const value = parsedApiPricing(bytes);
  if (!exactKeys(value,["prices","has_more","next_cursor"]) && !exactKeys(value,["prices"])) throw failure("PRICE_NOT_VERIFIABLE");
  if ((Object.hasOwn(value,"has_more") && (value.has_more !== false || value.next_cursor !== null))
    || !Array.isArray(value.prices) || value.prices.length !== 1
    || !exactKeys(value.prices[0],["endpoint_id","currency","unit","unit_price"])) throw failure("PRICE_NOT_VERIFIABLE");
  const p = value.prices[0];
  if (p.endpoint_id !== model || p.currency !== "USD" || p.unit !== (model === VACE ? "seconds" : "images")) throw failure("PRICE_NOT_VERIFIABLE");
  const decimal = normalizedDecimal(p.unit_price);
  return {unit:p.unit,unitPriceDecimal:decimal,unitPrice:exactNumericDecimal(decimal)};
}

function regionalPublicPrice(bytes, api) {
  // Reuse the visibility-aware parser; hidden and conflicting price paragraphs fail closed.
  const paragraphs = endpointPricingParagraphs(bytes,VACE);
  const prices = [];
  for (const raw of paragraphs) {
    const text = raw.replace(/&nbsp;|&#160;|&#xA0;/gi," ").replace(/&#36;|&#x24;|&dollar;/gi,"$").replace(/\s+/g," ").trim();
    const matches = [...text.matchAll(/\$\s*([0-9]+(?:\.[0-9]+)?)\s*per video second for\s*720p\b/gi)];
    if (matches.length !== 1 || !/Video seconds are calculated at 16 frames per second\./i.test(text)) throw failure("PRICE_NOT_VERIFIABLE");
    prices.push(normalizedDecimal(matches[0][1]));
  }
  if (!prices.length || prices.some(p=>p !== api.unitPriceDecimal)) throw failure("PRICE_NOT_VERIFIABLE");
  return {resolution:"720p",billing_frames_per_second:16,base_unit_price_usd:api.unitPrice};
}

function schemaDecimalValue(lexeme) {
  const parts = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(lexeme);
  if (!parts) throw failure("PRICE_NOT_VERIFIABLE");
  const [,sign,integer,fraction="",exponent="0"] = parts;
  const digits = (integer+fraction).replace(/^0+/,"");
  if (!digits) return "0";
  const coefficient = digits.replace(/0+$/,"");
  const decimalExponent = Number(exponent);
  if (!Number.isSafeInteger(decimalExponent)) throw failure("PRICE_NOT_VERIFIABLE");
  const power = decimalExponent-fraction.length+digits.length-coefficient.length;
  if (!Number.isSafeInteger(power)) throw failure("PRICE_NOT_VERIFIABLE");
  return `${sign}${coefficient}e${power}`;
}

function regionalSchemaDocument(bytes) {
  // JSON.parse owns grammar validation; this bounded walk only detects keys
  // it would otherwise discard. It never treats text inside strings as JSON.
  if (bytes.length > MAX_PRICING_BYTES) throw failure("PRICE_NOT_VERIFIABLE");
  try {
    const source = new TextDecoder("utf-8",{fatal:true,ignoreBOM:true}).decode(bytes);
    const document = JSON.parse(source,(_key,value,context)=>{
      if (typeof value === "number" && (!Number.isFinite(value) || typeof context?.source !== "string"
        || schemaDecimalValue(context.source) !== schemaDecimalValue(String(value)))) throw failure("PRICE_NOT_VERIFIABLE");
      return value;
    });
    const scopes = [];
    for (let i=0;i<source.length;i++) {
      const token = source[i];
      if (token === '"') {
        const start = i;
        for (i++;source[i] !== '"';i++) if (source[i] === "\\") i++;
        let next = i+1;
        while (/[\t\n\r ]/.test(source[next] ?? "")) next++;
        if (source[next] === ":") {
          const keys = scopes.at(-1), key = JSON.parse(source.slice(start,i+1));
          if (!(keys instanceof Set) || keys.has(key)) throw failure("PRICE_NOT_VERIFIABLE");
          keys.add(key);
        }
      } else if (token === "{") scopes.push(new Set());
      else if (token === "[") scopes.push(null);
      else if (token === "}" || token === "]") scopes.pop();
    }
    return document;
  } catch { throw failure("PRICE_NOT_VERIFIABLE"); }
}

export function validateRegionalPricingEvidence(model, apiBytes, publicBytesOrNull, schemaBytes) {
  if (![VACE,CLEANUP].includes(model) || !(apiBytes instanceof Uint8Array) || !(schemaBytes instanceof Uint8Array)
    || (model === CLEANUP ? publicBytesOrNull !== null : !(publicBytesOrNull instanceof Uint8Array))) throw failure("PRICE_NOT_VERIFIABLE");
  const api = regionalApiPrice(Buffer.from(apiBytes),model);
  let schema;
  if (model === CLEANUP) schema = verifiedEndpointSchema(Buffer.from(schemaBytes),model);
  else {
    const document = regionalSchemaDocument(Buffer.from(schemaBytes));
    let commitment;
    try { commitment = sha256(canonicalBytes(document)); } catch { throw failure("PRICE_NOT_VERIFIABLE"); }
    if (commitment !== REGIONAL_SCHEMA_CANONICAL_SHA256) throw failure("PRICE_NOT_VERIFIABLE");
    const name = "WanVace14bInpaintingInput";
    if (document.paths?.[`/${VACE}`]?.post?.requestBody?.content?.["application/json"]?.schema?.$ref !== `#/components/schemas/${name}`
      || !isObject(document.components?.schemas?.[name]?.properties)) throw failure("PRICE_NOT_VERIFIABLE");
    schema = {input_schema:name,policy:"vace81_24_720_v1"};
  }
  return {api:{endpoint_id:model,currency:"USD",unit:api.unit,unit_price_usd:api.unitPrice},
    public:model === CLEANUP ? null : regionalPublicPrice(Buffer.from(publicBytesOrNull),api),schema};
}

function regionalQuote(context, receiptId, retrievedAt, sources, api) {
  const primary = context.config.role === "primary";
  const reserved = primary ? 7 : 1;
  const units = primary ? "5.0625" : "1";
  return {schema:"sprite_regional_quote_v1",receipt_id:receiptId,kind:context.planning?"planning":"execution",regional_plan_sha256:context.planSha256,
    role:context.config.role,model:context.config.model,config_sha256:context.planning?null:context.configSha256,input_set_sha256:context.planning?null:context.inputSetSha256,
    retrieved_at:retrievedAt,expires_at:new Date(Date.parse(retrievedAt)+REGIONAL_PRICE_TTL).toISOString(),currency:"USD",api_unit:api.unit,
    unit_price_usd:api.unitPriceDecimal,billing_frames:primary?81:null,billing_fps:primary?16:null,estimated_units:units,reserved_units:reserved,
    estimated_amount_usd:decimalProduct(api.unitPriceDecimal,units),reserved_amount_usd:decimalProduct(api.unitPriceDecimal,String(reserved)),sources};
}

function regionalSourceLayout(model) {
  const api = new URL(API_PRICING_URL); api.searchParams.set("endpoint_id",model);
  return model === CLEANUP ? [["api",api.toString()],["schema",endpointSchemaUrl(model)]]
    : [["api",api.toString()],["public",`https://fal.ai/models/${model}`],["schema",endpointSchemaUrl(model)]];
}

async function regionalPricing(context, fetchImpl, key, now) {
  if (typeof key !== "string" || !key.trim()) throw failure("KEY_SETUP_REQUIRED",true);
  const model = context.config.model, layout = regionalSourceLayout(model), bodies = [];
  for (const [kind,url] of layout) {
    const response = await fetchResponse(fetchImpl,url,{method:"GET",headers:kind === "api"?{Authorization:`Key ${key}`} : {}},15000,"PRICE_FETCH_FAILED");
    if (!response.ok) throw failure("PRICE_FETCH_FAILED",true);
    bodies.push(await boundedBytes(response,MAX_PRICING_BYTES,"PRICE_FETCH_FAILED"));
  }
  const selection = validateRegionalPricingEvidence(model,bodies[0],model === CLEANUP?null:bodies[1],bodies.at(-1));
  const api = regionalApiPrice(bodies[0],model), retrieved = now();
  if (!Number.isFinite(retrieved)) throw failure("CLOCK_INVALID");
  const retrievedAt = new Date(retrieved).toISOString(), id = randomUUID();
  const sources = layout.map(([kind,url],i)=>({source_url:url,retrieved_at:retrievedAt,body_sha256:sha256(bodies[i]),archive_file:`${id}.${kind}.body`,selected:selection[kind]}));
  const receipt = regionalQuote(context,id,retrievedAt,sources,api), directory = receiptDirectory(context.root,context.planning);
  sources.forEach((s,i)=>writeNewDurable(path.join(directory,s.archive_file),bodies[i]));
  const bytes = canonicalBytes(receipt), file = path.join(directory,`${String(retrieved).padStart(16,"0")}-${id}.receipt.json`);
  writeNewDurable(file,bytes);
  return output({state:"PRICED",estimated_cost_usd:exactNumericDecimal(receipt.reserved_amount_usd),retry_safe:true,quote:quoteOutput(context,file,bytes,receipt.reserved_amount_usd)});
}

function regionalReceiptForExecute(context, now) {
  const directory = receiptDirectory(context.root);
  const records = fs.readdirSync(directory).filter(n=>n.endsWith(".receipt.json")).map(name=>({name,...readJsonFile(path.join(directory,name),"PRICE_RECEIPT_INVALID")}));
  const compatible = records.filter(r=>r.value.schema === "sprite_regional_quote_v1" && r.value.kind === "execution" && r.value.config_sha256 === context.configSha256 && r.value.input_set_sha256 === context.inputSetSha256);
  if (!compatible.length) throw failure("PRICE_RECEIPT_INCOMPATIBLE");
  compatible.sort((a,b)=>Date.parse(b.value.retrieved_at)-Date.parse(a.value.retrieved_at));
  for (const record of compatible) {
    const q = record.value;
    try {
      const retrieved = Date.parse(q.retrieved_at), layout = regionalSourceLayout(context.config.model);
      if (!Number.isFinite(retrieved) || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(q.receipt_id ?? "")
        || record.name !== `${String(retrieved).padStart(16,"0")}-${q.receipt_id}.receipt.json`
        || !Array.isArray(q.sources) || q.sources.length !== layout.length) throw failure("PRICE_RECEIPT_INVALID");
      const bodies = q.sources.map((s,i)=>{
        if (!exactKeys(s,["source_url","retrieved_at","body_sha256","archive_file","selected"]) || s.source_url !== layout[i][1] || s.retrieved_at !== q.retrieved_at
          || s.archive_file !== `${q.receipt_id}.${layout[i][0]}.body` || !validHash(s.body_sha256)) throw failure("PRICE_RECEIPT_INVALID");
        const bytes = regularBytes(path.join(directory,s.archive_file),"PRICE_RECEIPT_INVALID",MAX_PRICING_BYTES);
        if (sha256(bytes) !== s.body_sha256) throw failure("PRICE_RECEIPT_INVALID"); return bytes;
      });
      const selection = validateRegionalPricingEvidence(context.config.model,bodies[0],context.config.model === CLEANUP?null:bodies[1],bodies.at(-1));
      if (q.sources.some((s,i)=>!canonicalBytes(s.selected).equals(canonicalBytes(selection[layout[i][0]])))) throw failure("PRICE_RECEIPT_INVALID");
      const expected = regionalQuote(context,q.receipt_id,q.retrieved_at,q.sources,regionalApiPrice(bodies[0],context.config.model));
      if (!canonicalBytes(expected).equals(record.bytes)) throw failure("PRICE_RECEIPT_INVALID");
      if (Number.isFinite(now) && now-retrieved >= -PRICE_FUTURE_SKEW_MS && now-retrieved <= REGIONAL_PRICE_TTL)
        return {...q,estimated_cost_usd:exactNumericDecimal(q.reserved_amount_usd),estimated_cost_usd_decimal:q.reserved_amount_usd,receipt_sha256:sha256(record.bytes),receipt_path:path.join(directory,record.name)};
    } catch { throw failure("PRICE_RECEIPT_INVALID"); }
  }
  throw failure("PRICE_STALE");
}

async function pricing(context, fetchImpl, key, now) {
  if (context.regional) return regionalPricing(context,fetchImpl,key,now);
  if (context.endpoint) return endpointPricing(context, fetchImpl, key, now);
  if (typeof key !== "string" || key.trim() === "") throw failure("KEY_SETUP_REQUIRED", true);
  const requestUrl = new URL(API_PRICING_URL);
  requestUrl.searchParams.set("endpoint_id", context.config.model);
  const apiResponse = await fetchResponse(fetchImpl, requestUrl.toString(), {
    method: "GET", headers: { Authorization: `Key ${key}` },
  }, 15_000, "PRICE_FETCH_FAILED");
  if (!apiResponse.ok) throw failure("PRICE_FETCH_FAILED", true);
  const apiBody = await jsonBody(apiResponse, MAX_PRICING_BYTES, "PRICE_FETCH_FAILED");
  const selected = selectedPrice(parsedApiPricing(apiBody.bytes), context.config.model);
  const retrievedAt = now();
  if (!Number.isFinite(retrievedAt)) throw failure("CLOCK_INVALID");
  const receiptId = randomUUID();
  const apiArchiveFile = `${receiptId}.api.body`;
  const publicArchiveFile = `${receiptId}.public.body`;
  const sources = [{
    source_url: requestUrl.toString(), retrieved_at: new Date(retrievedAt).toISOString(), body_sha256: sha256(apiBody.bytes), archive_file: apiArchiveFile,
    selected: { endpoint_id: context.config.model, currency: "USD", unit: selected.unit, unit_price_usd: selected.unitPrice },
  }];
  let multiplier = 1;
  let publicBody = null;
  let effectiveDecimal = selected.unitPriceDecimal;
  if (context.config.model === NANO) {
    const publicResponse = await fetchResponse(fetchImpl, NANO_PUBLIC_URL, { method: "GET", headers: {} }, 15_000, "PRICE_FETCH_FAILED");
    if (!publicResponse.ok) throw failure("PRICE_FETCH_FAILED", true);
    publicBody = await boundedBytes(publicResponse, MAX_PRICING_BYTES, "PRICE_FETCH_FAILED");
    let html;
    try { html = new TextDecoder("utf-8", { fatal: true }).decode(publicBody); } catch { throw failure("PRICE_NOT_VERIFIABLE"); }
    const parsed = visibleNanoPricing(html);
    if (parsed.baseDecimal !== selected.unitPriceDecimal || parsed.multiplierDecimal !== "1.5"
      || (selected.apiMultiplierDecimal !== null && selected.apiMultiplierDecimal !== parsed.multiplierDecimal)) throw failure("PRICE_NOT_VERIFIABLE");
    multiplier = parsed.multiplier;
    effectiveDecimal = decimalProduct(parsed.baseDecimal, parsed.multiplierDecimal);
    sources.push({
      source_url: NANO_PUBLIC_URL, retrieved_at: new Date(retrievedAt).toISOString(), body_sha256: sha256(publicBody), archive_file: publicArchiveFile,
      selected: { base_unit_price_usd: parsed.base, resolution: "2K", resolution_multiplier: parsed.multiplier },
    });
  }
  if (context.config.model === H3) {
    const response = await fetchResponse(fetchImpl, H3_PUBLIC_URL, {method:"GET", headers:{}}, 15_000, "PRICE_FETCH_FAILED");
    if (!response.ok) throw failure("PRICE_FETCH_FAILED", true);
    publicBody = await boundedBytes(response, MAX_PRICING_BYTES, "PRICE_FETCH_FAILED");
    let html;
    try { html = new TextDecoder("utf-8", {fatal:true}).decode(publicBody); } catch { throw failure("PRICE_NOT_VERIFIABLE"); }
    const parsed = visibleH3Pricing(html);
    if (parsed.baseDecimal !== selected.unitPriceDecimal) throw failure("PRICE_NOT_VERIFIABLE");
    multiplier = parsed.high / parsed.base;
    effectiveDecimal = parsed.highDecimal;
    sources.push({source_url:H3_PUBLIC_URL, retrieved_at:new Date(retrievedAt).toISOString(), body_sha256:sha256(publicBody), archive_file:publicArchiveFile,
      selected:{base_unit_price_usd:parsed.base, resolution:"768P", effective_unit_price_usd:parsed.high}});
  }
  const reservedUnits = reservedUnitsFor(context);
  const effective = money(selected.unitPrice * multiplier);
  const amountDecimal = decimalProduct(effectiveDecimal, reservedUnits);
  const estimate = context.managed || context.planning ? exactNumericDecimal(amountDecimal) : money(effective * reservedUnits);
  const receipt = context.planning ? {
    schema: "sprite_cleanup_planning_quote_v1", receipt_id: receiptId, plan_sha256: context.planSha256,
    model: CLEANUP, retrieved_at: new Date(retrievedAt).toISOString(), expires_at: new Date(retrievedAt + PRICE_MAX_AGE_MS).toISOString(),
    currency: "USD", api_unit: selected.unit, unit_price_usd: selected.unitPriceDecimal, estimated_cost_usd: amountDecimal,
    reserved_units: 1, planning_basis: "configured_output_count", sources,
  } : {
    schema_version: 3,
    receipt_id: receiptId,
    config_sha256: context.configSha256,
    model: context.config.model,
    retrieved_at: new Date(retrievedAt).toISOString(),
    expires_at: new Date(retrievedAt + PRICE_MAX_AGE_MS).toISOString(),
    currency: "USD",
    api_unit: selected.unit,
    base_unit_price_usd: selected.unitPrice,
    resolution_multiplier: context.config.model === NANO ? multiplier : null,
    effective_unit_price_usd: effective,
    estimated_cost_usd: estimate,
    ...(context.managed ? { estimated_cost_usd_decimal: amountDecimal } : {}),
    planning_basis: planningBasis(context),
    input_duration_seconds: context.config.model === H3 ? context.config.input.duration : context.guideMetadata?.duration_seconds ?? null,
    reserved_units: reservedUnits,
    driving_video_sha256: context.config.driving_video?.sha256 ?? null,
    sources,
  };
  const bytes = canonicalBytes(receipt);
  const directory = receiptDirectory(context.root, context.planning);
  writeNewDurable(path.join(directory, apiArchiveFile), apiBody.bytes);
  if (publicBody !== null) writeNewDurable(path.join(directory, publicArchiveFile), publicBody);
  const name = `${String(retrievedAt).padStart(16, "0")}-${receipt.receipt_id}.receipt.json`;
  const receiptPath = path.join(directory, name);
  writeNewDurable(receiptPath, bytes);
  return output({ state: "PRICED", estimated_cost_usd: estimate, retry_safe: true,
    ...(context.managed || context.planning ? { quote: quoteOutput(context, receiptPath, bytes, amountDecimal) } : {}),
  });
}

function endpointReceiptForExecute(context, now) {
  const directory = receiptDirectory(context.root);
  const records = fs.readdirSync(directory).filter(n => n.endsWith(".json")).map(name => ({name,...readJsonFile(path.join(directory,name),"PRICE_RECEIPT_INVALID")}));
  const compatible = records.filter(r => r.value?.schema_version === 3 && r.value.config_sha256 === context.configSha256 && r.value.model === context.config.model);
  if (!compatible.length) throw failure("PRICE_RECEIPT_INCOMPATIBLE");
  compatible.sort((a,b)=>Date.parse(b.value.retrieved_at)-Date.parse(a.value.retrieved_at));
  for (const record of compatible) {
    const receipt = record.value;
    try {
      const retrieved = Date.parse(receipt.retrieved_at);
      if (!exactKeys(receipt,["schema_version","receipt_id","config_sha256","model","retrieved_at","expires_at","currency","api_unit","base_unit_price_usd","resolution_multiplier","effective_unit_price_usd","estimated_cost_usd","estimated_cost_usd_decimal","planning_basis","input_duration_seconds","reserved_units","driving_video_sha256","sources"])
        || !Number.isFinite(retrieved) || Date.parse(receipt.expires_at)-retrieved !== PRICE_MAX_AGE_MS
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(receipt.receipt_id ?? "")
        || record.name !== `${String(retrieved).padStart(16,"0")}-${receipt.receipt_id}.receipt.json`
        || receipt.currency !== "USD" || receipt.api_unit !== (context.config.model === FLF ? "videos" : "images")
        || receipt.reserved_units !== 1 || receipt.driving_video_sha256 !== null || receipt.input_duration_seconds !== null
        || receipt.planning_basis !== endpointPricingBasis(context) || receipt.resolution_multiplier !== (context.config.model === NANO ? 1.5 : null)
        || !Array.isArray(receipt.sources) || receipt.sources.length !== (context.config.model === CLEANUP ? 2 : 3)) throw failure("PRICE_RECEIPT_INVALID");
      const model = context.config.model;
      const apiUrl = new URL(API_PRICING_URL);
      apiUrl.searchParams.set("endpoint_id",model);
      const suffixes = model === CLEANUP ? ["api","schema"] : ["api","public","schema"];
      const urls = model === CLEANUP ? [apiUrl.toString(),endpointSchemaUrl(model)] : [apiUrl.toString(),`https://fal.ai/models/${model}`,endpointSchemaUrl(model)];
      const bodies = receipt.sources.map((s,i)=>{
        if (!exactKeys(s,["source_url","retrieved_at","body_sha256","archive_file","selected"])
          || s.source_url !== urls[i] || s.retrieved_at !== receipt.retrieved_at || !validHash(s.body_sha256)
          || s.archive_file !== `${receipt.receipt_id}.${suffixes[i]}.body`) throw failure("PRICE_RECEIPT_INVALID");
        const bytes = regularBytes(path.join(directory,s.archive_file),"PRICE_RECEIPT_INVALID",MAX_PRICING_BYTES);
        if (sha256(bytes) !== s.body_sha256) throw failure("PRICE_RECEIPT_INVALID");
        return bytes;
      });
      const api = endpointApiPrice(bodies[0],model);
      const selected = [{endpoint_id:model,currency:"USD",unit:api.unit,unit_price_usd:api.unitPrice}];
      if (model !== CLEANUP) selected.push(endpointPublicSelection(bodies[1],model,api));
      selected.push(verifiedEndpointSchema(bodies.at(-1),model));
      if (receipt.sources.some((s,i)=>!canonicalBytes(s.selected).equals(canonicalBytes(selected[i])))) throw failure("PRICE_RECEIPT_INVALID");
      const amount = decimalProduct(api.unitPriceDecimal,model === NANO ? "1.5" : "1");
      if (receipt.base_unit_price_usd !== api.unitPrice || receipt.effective_unit_price_usd !== exactNumericDecimal(amount)
        || receipt.estimated_cost_usd !== exactNumericDecimal(amount) || receipt.estimated_cost_usd_decimal !== amount) throw failure("PRICE_RECEIPT_INVALID");
      const age = now-retrieved;
      if (Number.isFinite(now) && age >= -PRICE_FUTURE_SKEW_MS && age <= PRICE_MAX_AGE_MS) return {...receipt,receipt_sha256:sha256(record.bytes),receipt_path:path.join(directory,record.name)};
    } catch { throw failure("PRICE_RECEIPT_INVALID"); }
  }
  throw failure("PRICE_STALE");
}

function receiptForExecute(context, now) {
  if (context.regional) return regionalReceiptForExecute(context,now);
  if (context.endpoint) return endpointReceiptForExecute(context, now);
  const dir = receiptDirectory(context.root);
  const records = [];
  for (const name of fs.readdirSync(dir)) {
    if (!name.endsWith(".json")) continue;
    try {
      const record = readJsonFile(path.join(dir, name), "PRICE_RECEIPT_INVALID");
      records.push({ ...record, name });
    } catch { throw failure("PRICE_RECEIPT_INVALID"); }
  }
  const compatible = records.filter((record) => record.value?.schema_version === 3
    && record.value.config_sha256 === context.configSha256 && record.value.model === context.config.model);
  if (!compatible.length) throw failure("PRICE_RECEIPT_INCOMPATIBLE");
  compatible.sort((a, b) => Date.parse(b.value.retrieved_at) - Date.parse(a.value.retrieved_at));
  for (const record of compatible) {
    const receipt = record.value;
    const receiptKeys = [
      "schema_version", "receipt_id", "config_sha256", "model", "retrieved_at", "expires_at",
      "currency", "api_unit", "base_unit_price_usd", "resolution_multiplier",
      "effective_unit_price_usd", "estimated_cost_usd", "planning_basis",
      "input_duration_seconds", "reserved_units", "driving_video_sha256", "sources",
      ...(context.managed ? ["estimated_cost_usd_decimal"] : []),
    ];
    const retrieved = Date.parse(receipt.retrieved_at);
    const expires = Date.parse(receipt.expires_at);
    const video = VIDEO_MODELS.has(context.config.model);
    const expectedUnits = reservedUnitsFor(context);
    const expectedMultiplier = context.config.model === NANO ? 1.5 : 1;
    const expectedApiUnits = video ? new Set(["seconds"]) : new Set(["image", "images"]);
    if (!exactKeys(receipt, receiptKeys)
      || !Number.isFinite(retrieved) || !Number.isFinite(expires) || expires - retrieved !== PRICE_MAX_AGE_MS
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(receipt.receipt_id ?? "")
      || record.name !== `${String(retrieved).padStart(16, "0")}-${receipt.receipt_id}.receipt.json`
      || receipt.currency !== "USD" || !expectedApiUnits.has(receipt.api_unit)
      || receipt.resolution_multiplier !== (context.config.model === NANO ? 1.5 : null)
      || receipt.reserved_units !== expectedUnits
      || receipt.input_duration_seconds !== (context.config.model === H3 ? context.config.input.duration : context.guideMetadata?.duration_seconds ?? null)
      || receipt.driving_video_sha256 !== (context.config.driving_video?.sha256 ?? null)
      || receipt.planning_basis !== planningBasis(context)
      || !Array.isArray(receipt.sources) || receipt.sources.length !== ([NANO, H3].includes(context.config.model) ? 2 : 1)) throw failure("PRICE_RECEIPT_INVALID");
    const expectedApiUrl = new URL(API_PRICING_URL); expectedApiUrl.searchParams.set("endpoint_id", context.config.model);
    const apiSource = receipt.sources[0];
    if (!exactKeys(apiSource, ["source_url", "retrieved_at", "body_sha256", "archive_file", "selected"])
      || !exactKeys(apiSource.selected, ["endpoint_id", "currency", "unit", "unit_price_usd"])
      || apiSource.source_url !== expectedApiUrl.toString() || apiSource.retrieved_at !== receipt.retrieved_at
      || apiSource.archive_file !== `${receipt.receipt_id}.api.body` || !validHash(apiSource.body_sha256)
      || path.basename(apiSource.archive_file) !== apiSource.archive_file) throw failure("PRICE_RECEIPT_INVALID");
    const apiArchive = regularBytes(path.join(dir, apiSource.archive_file), "PRICE_RECEIPT_INVALID", MAX_PRICING_BYTES);
    if (sha256(apiArchive) !== apiSource.body_sha256) throw failure("PRICE_RECEIPT_INVALID");
    let archivedApi;
    try { archivedApi = parsedApiPricing(apiArchive); } catch { throw failure("PRICE_RECEIPT_INVALID"); }
    let reparsed;
    try { reparsed = selectedPrice(archivedApi, context.config.model); } catch { throw failure("PRICE_RECEIPT_INVALID"); }
    if (apiSource.selected.endpoint_id !== context.config.model || apiSource.selected.currency !== "USD"
      || apiSource.selected.unit !== reparsed.unit || apiSource.selected.unit_price_usd !== reparsed.unitPrice
      || receipt.api_unit !== reparsed.unit || !expectedApiUnits.has(reparsed.unit)) throw failure("PRICE_RECEIPT_INVALID");
    let publicBase = reparsed.unitPrice;
    let effectiveDecimal = reparsed.unitPriceDecimal;
    if (context.config.model === NANO) {
      const publicSource = receipt.sources[1];
      if (!exactKeys(publicSource, ["source_url", "retrieved_at", "body_sha256", "archive_file", "selected"])
        || !exactKeys(publicSource.selected, ["base_unit_price_usd", "resolution", "resolution_multiplier"])
        || publicSource.source_url !== NANO_PUBLIC_URL || publicSource.retrieved_at !== receipt.retrieved_at
        || publicSource.archive_file !== `${receipt.receipt_id}.public.body` || !validHash(publicSource.body_sha256)
        || path.basename(publicSource.archive_file) !== publicSource.archive_file) throw failure("PRICE_RECEIPT_INVALID");
      const publicArchive = regularBytes(path.join(dir, publicSource.archive_file), "PRICE_RECEIPT_INVALID", MAX_PRICING_BYTES);
      if (sha256(publicArchive) !== publicSource.body_sha256) throw failure("PRICE_RECEIPT_INVALID");
      let publicText;
      try { publicText = new TextDecoder("utf-8", { fatal: true }).decode(publicArchive); } catch { throw failure("PRICE_RECEIPT_INVALID"); }
      let publicPricing;
      try { publicPricing = visibleNanoPricing(publicText); } catch { throw failure("PRICE_RECEIPT_INVALID"); }
      if (publicSource.selected.base_unit_price_usd !== publicPricing.base || publicSource.selected.resolution !== "2K"
        || publicSource.selected.resolution_multiplier !== publicPricing.multiplier
        || publicPricing.baseDecimal !== reparsed.unitPriceDecimal || publicPricing.multiplierDecimal !== "1.5"
        || (reparsed.apiMultiplierDecimal !== null && reparsed.apiMultiplierDecimal !== publicPricing.multiplierDecimal)) throw failure("PRICE_RECEIPT_INVALID");
      publicBase = publicPricing.base;
      effectiveDecimal = decimalProduct(publicPricing.baseDecimal, publicPricing.multiplierDecimal);
    }
    if (context.config.model === H3) {
      const pub = receipt.sources[1];
      if (!exactKeys(pub,["source_url","retrieved_at","body_sha256","archive_file","selected"])
        || !exactKeys(pub.selected,["base_unit_price_usd","resolution","effective_unit_price_usd"])
        || pub.source_url !== H3_PUBLIC_URL || pub.retrieved_at !== receipt.retrieved_at
        || pub.archive_file !== `${receipt.receipt_id}.public.body` || !validHash(pub.body_sha256)) throw failure("PRICE_RECEIPT_INVALID");
      const bytes = regularBytes(path.join(dir,pub.archive_file),"PRICE_RECEIPT_INVALID",MAX_PRICING_BYTES);
      if (sha256(bytes) !== pub.body_sha256) throw failure("PRICE_RECEIPT_INVALID");
      let parsed;
      try { parsed=visibleH3Pricing(new TextDecoder("utf-8",{fatal:true}).decode(bytes)); } catch { throw failure("PRICE_RECEIPT_INVALID"); }
      if (parsed.baseDecimal !== reparsed.unitPriceDecimal || pub.selected.base_unit_price_usd !== parsed.base
        || pub.selected.resolution !== "768P" || pub.selected.effective_unit_price_usd !== parsed.high) throw failure("PRICE_RECEIPT_INVALID");
      publicBase = parsed.high;
      effectiveDecimal = parsed.highDecimal;
    }
    const expectedEffective = money(publicBase * expectedMultiplier);
    const amountDecimal = decimalProduct(effectiveDecimal, expectedUnits);
    const expectedEstimate = context.managed ? exactNumericDecimal(amountDecimal) : money(expectedEffective * expectedUnits);
    if (receipt.base_unit_price_usd !== reparsed.unitPrice
      || receipt.effective_unit_price_usd !== expectedEffective || receipt.estimated_cost_usd !== expectedEstimate) throw failure("PRICE_RECEIPT_INVALID");
    if (context.managed && receipt.estimated_cost_usd_decimal !== amountDecimal) throw failure("PRICE_RECEIPT_INVALID");
    const age = now - retrieved;
    if (age >= -PRICE_FUTURE_SKEW_MS && age <= PRICE_MAX_AGE_MS) {
      return { ...receipt, receipt_sha256: sha256(record.bytes), receipt_path: path.join(dir, record.name) };
    }
  }
  throw failure("PRICE_STALE");
}

function queueBase(model) {
  const parts = model.split("/");
  return `${parts[0]}/${parts[1]}`;
}

function canonicalRequestUrls(model, id) {
  const prefix = `https://queue.fal.run/${queueBase(model)}/requests/${id}`;
  return { statusUrl: `${prefix}/status?logs=0`, resultUrl: prefix };
}

function validRequestId(id) {
  return typeof id === "string" && id.length >= 1 && id.length <= 128 && /^[A-Za-z0-9_-]+$/.test(id);
}

function validateQueueUrl(value, model, id, kind) {
  if (typeof value !== "string") throw failure("SUBMISSION_RESPONSE_INVALID");
  let url;
  try { url = new URL(value); } catch { throw failure("SUBMISSION_RESPONSE_INVALID"); }
  if (url.protocol !== "https:" || url.hostname !== "queue.fal.run" || url.port || url.username || url.password || url.hash) throw failure("SUBMISSION_RESPONSE_INVALID");
  const suffix = `/requests/${id}${kind === "status" ? "/status" : ""}`;
  const paths = [`/${queueBase(model)}${suffix}`, `/${model}${suffix}`];
  if (!paths.includes(url.pathname) || url.pathname.includes("..")) throw failure("SUBMISSION_RESPONSE_INVALID");
  if (kind === "status") {
    if (url.search !== "" && url.search !== "?logs=0") throw failure("SUBMISSION_RESPONSE_INVALID");
  } else if (url.search !== "") throw failure("SUBMISSION_RESPONSE_INVALID");
  return kind === "status" ? `${url.origin}${url.pathname}?logs=0` : `${url.origin}${url.pathname}`;
}

function submissionInput(context) {
  if (context.regional) {
    if (context.config.role === "cleanup") return {...context.config.input,image_url:`data:image/png;base64,${context.inputBuffers[0].toString("base64")}`};
    return {...context.config.input,video_url:`data:video/mp4;base64,${context.inputBuffers[0].toString("base64")}`,
      mask_video_url:`data:video/mp4;base64,${context.inputBuffers[1].toString("base64")}`,ref_image_urls:[`data:image/png;base64,${context.inputBuffers[2].toString("base64")}`]};
  }
  if (context.endpoint) {
    const images = context.inputBuffers.map(bytes=>`data:image/png;base64,${bytes.toString("base64")}`);
    if (context.config.role === "pose_edit") return {...context.config.input,image_urls:images};
    if (context.config.role === "cleanup") return {...context.config.input,image_url:images[0]};
    const p = context.config.input;
    return {prompt:p.prompt,start_image_url:images[0],end_image_url:images[1],num_frames:p.frames,frames_per_second:p.fps,resolution:p.resolution,
      aspect_ratio:p.aspect_ratio,num_inference_steps:p.steps,acceleration:p.acceleration,enable_prompt_expansion:p.prompt_expansion,enable_safety_checker:true,loras:[]};
  }
  const imageData = `data:image/png;base64,${context.sourceBytes.toString("base64")}`;
  if (context.config.model === NANO) return { ...context.config.input, image_urls: [imageData] };
  if (context.config.model === CLEANUP) return { ...context.config.input, image_url: imageData };
  if (context.config.model === H3) {
    const {end_image_same_as_start, ...input} = context.config.input;
    return {...input, image_url:imageData, ...(end_image_same_as_start ? {end_image_url:imageData} : {})};
  }
  const videoData = `data:video/mp4;base64,${context.guideBytes.toString("base64")}`;
  return { ...context.config.input, image_url: imageData, video_url: videoData };
}

function createClaim(context, receiptSha256) {
  const candidate = path.join(context.root, "submission.claim");
  try {
    writeNewDurable(candidate, canonicalBytes({
      schema_version: 3,
      config_sha256: context.configSha256,
      pricing_receipt_sha256: receiptSha256,
      created_at: new Date().toISOString(),
    }));
    return true;
  } catch (error) {
    if (error?.code === "EEXIST") return false;
    throw failure("CLAIM_CREATE_FAILED");
  }
}

function fixedTaskBridge(command, payload) {
  const runtime = path.join(os.homedir(), ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "python", "python.exe");
  const script = fileURLToPath(new URL("./sprite_task_bridge.py", import.meta.url));
  try {
    regularBytes(runtime, "RUNTIME_REQUIRED", 100 * 1024 * 1024);
    regularBytes(script, "RUNTIME_REQUIRED", MAX_JSON_BYTES);
  } catch { throw failure("RUNTIME_REQUIRED"); }
  const env = {};
  for (const name of ["SystemRoot", "WINDIR", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP"]) {
    if (typeof process.env[name] === "string") env[name] = process.env[name];
  }
  env.PYTHONDONTWRITEBYTECODE = "1";
  const input = JSON.stringify(payload);
  if (Buffer.byteLength(input) > 65536) throw failure("TASK_BRIDGE_INVALID");
  let result;
  try {
    result = spawnSync(runtime, ["-B", script, command], {
      input, encoding: "utf8", env, shell: false, windowsHide: true, timeout: 15000, maxBuffer: 65536,
    });
  } catch { throw failure("TASK_BRIDGE_FAILED"); }
  if (result.error || result.signal || ![0, 2].includes(result.status)
    || typeof result.stdout !== "string" || Buffer.byteLength(result.stdout) > 65536
    || typeof result.stderr !== "string" || Buffer.byteLength(result.stderr) > 65536) throw failure("TASK_BRIDGE_FAILED");
  let value;
  try { value = JSON.parse(result.stdout); } catch { throw failure("TASK_BRIDGE_INVALID"); }
  if ((result.status === 0) === (value?.state === "BLOCKED")) throw failure("TASK_BRIDGE_INVALID");
  return value;
}

async function verifiedRegionalBridge(context, command, taskBridge, receipt, token) {
  const before = command === "before-submit";
  const payload = {schema:before?"sprite_regional_before_submit_v1":"sprite_regional_transport_context_v1",job_dir:context.root,
    config_sha256:context.configSha256,config_file_sha256:context.configFileSha256,input_set_sha256:context.inputSetSha256,
    ...(before?{quote_sha256:receipt.receipt_sha256,amount_usd:receipt.estimated_cost_usd_decimal,regional_state_sha256:token}:{})};
  let v; try { v = await taskBridge(command,payload); } catch (error) { if (error?.code === "RUNTIME_REQUIRED") throw error; throw failure("TASK_BRIDGE_FAILED"); }
  if (v?.state === "BLOCKED") {
    if (!exactKeys(v,["state","block_reason"]) || !/^[A-Z][A-Z0-9_]{0,127}$/.test(v.block_reason ?? "")) throw failure("TASK_BRIDGE_INVALID");
    throw failure(v.block_reason);
  }
  const keys = ["schema","state","task_id","target_id","run_id","attempt_id","operation_id","config_sha256","config_file_sha256","input_set_sha256","binding_sha256","provider_request_id","submission_started","status","outcome_status","regional_state_sha256",...(before?["submit_allowed"]:[])];
  const ref = context.config.authorization.task_context;
  if (!exactKeys(v,keys) || v.schema !== "sprite_regional_transport_context_result_v1" || v.state !== (before?"SUBMISSION_AUTHORIZED":"CONTEXT_VERIFIED")
    || v.task_id !== ref.task_ref.task_id || v.target_id !== ref.task_ref.target_id || v.run_id !== ref.run_id || v.attempt_id !== ref.attempt_id || v.operation_id !== ref.operation_id
    || v.config_sha256 !== context.configSha256 || v.config_file_sha256 !== context.configFileSha256 || v.input_set_sha256 !== context.inputSetSha256
    || !validHash(v.binding_sha256) || !validHash(v.regional_state_sha256) || !(v.provider_request_id === null || validRequestId(v.provider_request_id))
    || typeof v.submission_started !== "boolean" || typeof v.status !== "string" || !v.status || v.status.length > 64
    || ![null,"accepted","pending","completed","failed","cancelled","rejected","uncertain"].includes(v.outcome_status)
    || (before && (typeof v.submit_allowed !== "boolean" || (v.submit_allowed && (v.regional_state_sha256 === token || v.provider_request_id !== null || !v.submission_started))))) throw failure("TASK_BRIDGE_INVALID");
  if (v.provider_request_id !== null && !v.submission_started) throw failure("TASK_CONTEXT_CONFLICT");
  return v;
}

async function verifiedTaskBridge(context, command, taskBridge, receipt = null, experimentToken = null) {
  if (context.regional) return verifiedRegionalBridge(context,command,taskBridge,receipt,experimentToken);
  const payload = {
    schema: context.endpoint ? command === "check-context" ? "sprite_transport_context_v2" : "sprite_before_submit_v2" : command === "check-context" ? "sprite_transport_context_v1" : "sprite_before_submit_v1",
    job_dir: context.root, config_sha256: context.configSha256, config_file_sha256: context.configFileSha256,
    ...(context.endpoint ? { input_set_sha256:context.inputSetSha256 } : { source_sha256: sha256(context.sourceBytes) }),
    ...(receipt ? { quote_sha256: receipt.receipt_sha256, amount_usd: receipt.estimated_cost_usd_decimal } : {}),
    ...(context.endpoint && command === "before-submit" ? { experiment_state_sha256:experimentToken } : {}),
  };
  let value;
  try { value = await taskBridge(command, payload); } catch (error) {
    if (error?.code === "RUNTIME_REQUIRED") throw error;
    throw failure("TASK_BRIDGE_FAILED");
  }
  if (value?.state === "BLOCKED") {
    if ((context.endpoint && !exactKeys(value,["state","block_reason"])) || typeof value.block_reason !== "string" || !/^[A-Z][A-Z0-9_]{0,127}$/.test(value.block_reason)) throw failure("TASK_BRIDGE_INVALID");
    throw failure(value.block_reason);
  }
  const reference = context.config.authorization.task_context;
  const keys = ["state", "task_id", "target_id", "run_id", "attempt_id", "operation_id", "config_sha256", "config_file_sha256", "binding_sha256", "provider_request_id", "submission_started", "status", "outcome_status", ...(command === "before-submit" ? ["submit_allowed"] : [])];
  if (context.endpoint) keys.push("schema","input_set_sha256","experiment_state_sha256");
  if (!exactKeys(value, keys) || value.state !== (command === "check-context" ? "CONTEXT_VERIFIED" : "SUBMISSION_AUTHORIZED")
    || (context.endpoint && (value.schema !== "sprite_transport_context_result_v2" || value.input_set_sha256 !== context.inputSetSha256 || !validHash(value.experiment_state_sha256)))
    || value.task_id !== reference.task_ref.task_id || value.target_id !== reference.task_ref.target_id || value.run_id !== reference.run_id || value.attempt_id !== reference.attempt_id
    || value.operation_id !== reference.operation_id || value.config_sha256 !== context.configSha256
    || value.config_file_sha256 !== context.configFileSha256 || !validHash(value.binding_sha256)
    || !(value.provider_request_id === null || validRequestId(value.provider_request_id))
    || typeof value.submission_started !== "boolean" || typeof value.status !== "string" || value.status.length > 64 || !value.status
    || ![null, "accepted", "pending", "completed", "failed", "cancelled", "rejected", "uncertain"].includes(value.outcome_status)
    || (command === "before-submit" && typeof value.submit_allowed !== "boolean")) throw failure("TASK_BRIDGE_INVALID");
  if (value.provider_request_id !== null && !value.submission_started) throw failure("TASK_CONTEXT_CONFLICT");
  if (context.endpoint && command === "before-submit" && value.submit_allowed && value.experiment_state_sha256 === experimentToken) throw failure("TASK_BRIDGE_INVALID");
  return value;
}

function reconcileManagedState(context, saved, ledger) {
  const nodeId = saved?.request_id ?? null;
  const terminal = { completed: "COMPLETED", failed: "FAILED", cancelled: "CANCELLED", rejected: "REMOTE_REJECTED" }[ledger.outcome_status];
  let regionalReceipt = saved?.pricing_receipt_sha256 ?? null;
  if (context.regional && ledger.provider_request_id) {
    // Only a durable known ID can recover absent mirrors. Missing local facts
    // remain explicit; no claim or receipt is manufactured for a GET recovery.
    if (saved && (saved.config_sha256 !== context.configSha256 || (!nodeId && !["SUBMITTING","SUBMISSION_STATUS_UNKNOWN","REQUEST_ACCEPTED"].includes(saved.state)))) throw failure("TASK_CONTEXT_CONFLICT");
    if (regionalReceipt !== null && !validHash(regionalReceipt)) throw failure("TASK_CONTEXT_CONFLICT");
    const claimPath = path.join(context.root,"submission.claim");
    if (fs.existsSync(claimPath)) {
      const claim = readJsonFile(claimPath,"TASK_CONTEXT_CONFLICT").value;
      if (!exactKeys(claim,["schema_version","config_sha256","pricing_receipt_sha256","created_at"]) || claim.schema_version !== 3
        || claim.config_sha256 !== context.configSha256 || !validHash(claim.pricing_receipt_sha256) || typeof claim.created_at !== "string" || !Number.isFinite(Date.parse(claim.created_at))
        || (regionalReceipt !== null && regionalReceipt !== claim.pricing_receipt_sha256)) throw failure("TASK_CONTEXT_CONFLICT");
      regionalReceipt = claim.pricing_receipt_sha256;
    }
  }
  if (nodeId !== null && (!validRequestId(nodeId) || saved.config_sha256 !== context.configSha256)) throw failure("TASK_CONTEXT_CONFLICT");
  if (nodeId && ledger.provider_request_id && nodeId !== ledger.provider_request_id) throw failure("TASK_CONTEXT_CONFLICT");
  if (nodeId && !ledger.submission_started) throw failure("TASK_CONTEXT_CONFLICT");
  if (nodeId && !ledger.provider_request_id) {
    const claim = readJsonFile(path.join(context.root, "submission.claim"), "TASK_CONTEXT_CONFLICT").value;
    if (!exactKeys(claim, ["schema_version", "config_sha256", "pricing_receipt_sha256", "created_at"])
      || claim.schema_version !== 3 || claim.config_sha256 !== context.configSha256
      || !validHash(claim.pricing_receipt_sha256) || claim.pricing_receipt_sha256 !== saved.pricing_receipt_sha256
      || !Number.isFinite(Date.parse(claim.created_at))) throw failure("TASK_CONTEXT_CONFLICT");
  }
  if (!nodeId && ledger.provider_request_id) {
    const urls = canonicalRequestUrls(context.config.model, ledger.provider_request_id);
    return writeState(context.root, {
      schema_version: 3, state: terminal ?? "REQUEST_ACCEPTED", config_sha256: context.configSha256,
      request_id: ledger.provider_request_id, status_url: urls.statusUrl, result_url: urls.resultUrl,
      estimated_cost_usd: saved?.estimated_cost_usd ?? null, raw_path: null, raw_sha256: null,
      ...(context.regional ? {pricing_receipt_sha256:regionalReceipt} : {}),
      ...(context.config.schema_version === 5 ? {seedream_capture:null} : {}),
    });
  }
  if (nodeId && terminal) return writeState(context.root, { ...saved, state: terminal });
  return saved;
}

function outputUrlAllowed(value) {
  if (typeof value !== "string") throw failure("RESULT_URL_NOT_ALLOWED");
  let url;
  try { url = new URL(value); } catch { throw failure("RESULT_URL_NOT_ALLOWED"); }
  if (url.protocol !== "https:" || (url.hostname !== "fal.media" && !url.hostname.endsWith(".fal.media"))
    || url.port || url.username || url.password || url.hash) throw failure("RESULT_URL_NOT_ALLOWED");
  for (const key of url.searchParams.keys()) {
    if (/(?:token|key|auth|signature|credential|password|secret)/i.test(key)) throw failure("RESULT_URL_NOT_ALLOWED");
  }
  return url.toString();
}

function resultUrlFromBody(body, model, endpoint = false) {
  let candidate;
  if (endpoint && model === NANO && (!Array.isArray(body?.images) || body.images.length !== 1)) throw failure("RESULT_IMAGE_COUNT_INVALID");
  if (VIDEO_MODELS.has(model)) candidate = body?.video?.url;
  else if (model === NANO) candidate = body?.images?.[0]?.url;
  else candidate = body?.image?.url ?? body?.images?.[0]?.url;
  return outputUrlAllowed(candidate);
}

function endpointCountFailure(model, result) {
  if (model !== NANO || !Array.isArray(result?.images) || result.images.length === 1) return null;
  // A count mismatch never turns an unsafe/malformed URL into terminal provider evidence.
  for (const image of result.images) outputUrlAllowed(image?.url);
  return "RESULT_IMAGE_COUNT_INVALID";
}

function endpointByteFailure(config, bytes, sourceMetadata) {
  if (VIDEO_MODELS.has(config.model)) {
    try {
      const metadata = readMp4Metadata(bytes);
      if (metadata.width !== metadata.height || metadata.width > 960 || Math.abs(metadata.duration_seconds-81/24) > 1/24) return "RESULT_VIDEO_INVALID";
    } catch { return "RESULT_VIDEO_INVALID"; }
  } else {
    if (bytes.length < 8 || !bytes.subarray(0,8).equals(Buffer.from("89504e470d0a1a0a","hex"))) return "RESULT_NOT_PNG";
    try {
      const metadata = pngMetadata(bytes);
      const expected = config.role === "pose_edit" ? {width:2048,height:2048} : sourceMetadata;
      if (metadata.width !== expected.width || metadata.height !== expected.height) return "RESULT_IMAGE_INVALID";
    } catch { return "RESULT_IMAGE_INVALID"; }
  }
  return null;
}

function providerNoMediaBody(value) {
  if (!exactKeys(value,["detail"]) || !Array.isArray(value.detail) || value.detail.length !== 1) return false;
  const entry = value.detail[0];
  const required = ["loc","msg","type","url"];
  const allowed = [...required,"input","ctx","categories"];
  return isObject(entry) && required.every(key=>Object.hasOwn(entry,key)) && Object.keys(entry).every(key=>allowed.includes(key))
    && Array.isArray(entry.loc) && entry.loc.length === 1 && entry.loc[0] === "body" && entry.type === "no_media_generated"
    && typeof entry.msg === "string" && entry.msg.trim() !== "" && typeof entry.url === "string" && entry.url.trim() !== ""
    && (!Object.hasOwn(entry,"ctx") || isObject(entry.ctx))
    && (!Object.hasOwn(entry,"categories") || (Array.isArray(entry.categories) && entry.categories.every(item=>typeof item === "string")));
}

function noMediaRetryHeader(response) {
  if (typeof response.headers?.get !== "function") throw failure("REMOTE_RESULT_UNAVAILABLE");
  const value = response.headers.get("x-fal-needs-retry");
  if (value === null) return null;
  if (typeof value === "string" && value.trim().toLowerCase() === "false") return "false";
  throw failure("REMOTE_RESULT_UNAVAILABLE");
}

function seedreamResultClass(result, httpStatus, retryHeader) {
  if (httpStatus === 422) {
    if (![null,"false"].includes(retryHeader) || !providerNoMediaBody(result)) throw failure("SEEDREAM_CAPTURE_INVALID");
    return "no_media";
  }
  if (httpStatus !== 200 || !exactKeys(result,["images"]) || !Array.isArray(result.images)) throw failure("SEEDREAM_CAPTURE_INVALID");
  for (const image of result.images) {
    const allowed = ["url","content_type","file_name","file_size","width","height"];
    if (!isObject(image) || !Object.hasOwn(image,"url") || Object.keys(image).some(k=>!allowed.includes(k))) throw failure("SEEDREAM_CAPTURE_INVALID");
    outputUrlAllowed(image.url);
    // With a single RAW, C can classify metadata type conflicts after validating the raster.
    // Count failures have no raster and require the official metadata types as protocol evidence.
    if (result.images.length !== 1 && Object.entries(image).some(([k,v])=>v !== null && ( ["content_type","file_name"].includes(k) ? typeof v !== "string" : ["file_size","width","height"].includes(k) ? !Number.isSafeInteger(v) : false))) throw failure("SEEDREAM_CAPTURE_INVALID");
  }
  return result.images.length === 1 ? "one_image" : "count";
}

// Pure buffer validation: deliberately has no filesystem, network, decoder or bridge dependency.
export function validateSeedreamCaptureEvidence(configBytes, claimBytes, captureBytes, statusBytes, resultBytes, rawBytesOrNull, expectedIdentity) {
  try {
    const bounded = (bytes,max) => { if (!(bytes instanceof Uint8Array) || bytes.byteLength > max) throw failure("SEEDREAM_CAPTURE_INVALID");return Buffer.from(bytes); };
    const configBuffer=bounded(configBytes,MAX_JSON_BYTES),claimBuffer=bounded(claimBytes,MAX_JSON_BYTES),captureBuffer=bounded(captureBytes,MAX_JSON_BYTES),statusBuffer=bounded(statusBytes,MAX_JSON_BYTES);
    const config=JSON.parse(configBuffer.toString("utf8")),claim=JSON.parse(claimBuffer.toString("utf8")),capture=JSON.parse(captureBuffer.toString("utf8")),status=JSON.parse(statusBuffer.toString("utf8"));
    const identityKeys=["variant","attempt_id","operation_id","request_id","config_sha256","config_file_sha256","input_set_sha256","binding_sha256","claim_sha256"];
    if (!exactKeys(expectedIdentity,identityKeys) || expectedIdentity.variant !== SEEDREAM_VARIANT || !validRequestId(expectedIdentity.request_id)
      || !identityKeys.filter(k=>k.endsWith("sha256")).every(k=>validHash(expectedIdentity[k]))
      || !exactKeys(config,["schema_version","variant","route","role","model","input_files","input","authorization","max_cost_usd"])
      || config.schema_version !== 5 || config.variant !== SEEDREAM_VARIANT || config.route !== "endpoint_cycle_v1" || config.role !== "pose_edit" || config.model !== SEEDREAM
      || !exactKeys(config.authorization,["request_authorized","task_context"]) || config.authorization.request_authorized !== true
      || typeof config.max_cost_usd !== "number" || !Number.isFinite(config.max_cost_usd) || config.max_cost_usd < 0) throw failure("SEEDREAM_CAPTURE_INVALID");
    validateTaskContext(config.authorization.task_context,"pose_edit");validateEndpointInput("pose_edit",config.input,true);
    const task=config.authorization.task_context;
    const root=path.join(path.dirname(task.run_manifest),"attempt-01","pose_edit");
    if (path.basename(task.run_manifest) !== "run-manifest.json" || task.attempt_id !== expectedIdentity.attempt_id || task.operation_id !== expectedIdentity.operation_id
      || !Array.isArray(config.input_files) || config.input_files.length !== 2 || config.input_files.some((r,i)=>!exactKeys(r,["role","path","sha256"]) || r.role !== ["edit","identity"][i] || !validHash(r.sha256) || r.path !== path.join(path.dirname(task.run_manifest),"snapshots",`${r.role}.png`))
      || config.input_files[0].sha256 === config.input_files[1].sha256
      || sha256(canonicalBytes(config)) !== expectedIdentity.config_sha256 || sha256(configBuffer) !== expectedIdentity.config_file_sha256
      || sha256(canonicalBytes(config.input_files.map(({role,sha256})=>({role,sha256})))) !== expectedIdentity.input_set_sha256
      || sha256(claimBuffer) !== expectedIdentity.claim_sha256
      || !exactKeys(claim,["schema_version","config_sha256","pricing_receipt_sha256","created_at"]) || claim.schema_version !== 3 || claim.config_sha256 !== expectedIdentity.config_sha256
      || !validHash(claim.pricing_receipt_sha256) || typeof claim.created_at !== "string" || !Number.isFinite(Date.parse(claim.created_at))
      || !canonicalBytes(claim).equals(claimBuffer) || !canonicalBytes(capture).equals(captureBuffer)
      || !exactKeys(capture,["schema",...identityKeys,"status","result","download"]) || capture.schema !== "sprite_seedream_capture_v1"
      || identityKeys.some(k=>capture[k] !== expectedIdentity[k])) throw failure("SEEDREAM_CAPTURE_INVALID");
    const urls=canonicalRequestUrls(SEEDREAM,expectedIdentity.request_id);
    const ref=(r,p,b)=>exactKeys(r,["path","sha256"]) && r.path === path.join(root,p) && validHash(r.sha256) && r.sha256 === sha256(b);
    if (!exactKeys(capture.status,["url","http_status","body_ref"]) || capture.status.url !== urls.statusUrl || capture.status.http_status !== 200
      || !ref(capture.status.body_ref,path.join("capture","status.body"),statusBuffer) || !isObject(status) || status.status !== "COMPLETED"
      || (Object.hasOwn(status,"request_id") && status.request_id !== expectedIdentity.request_id)
      || !exactKeys(capture.result,["url","http_status","headers","body_ref"]) || capture.result.url !== urls.resultUrl
      || ![200,422].includes(capture.result.http_status) || !exactKeys(capture.result.headers,["x_fal_needs_retry"])) throw failure("SEEDREAM_CAPTURE_INVALID");
    const header=capture.result.headers.x_fal_needs_retry;
    if (!(header === null || (typeof header === "string" && header === header.trim().toLowerCase()))) throw failure("SEEDREAM_CAPTURE_INVALID");
    const resultBuffer=bounded(resultBytes,capture.result.http_status === 422?MAX_NO_MEDIA_BYTES:MAX_JSON_BYTES);
    if (!ref(capture.result.body_ref,path.join("capture","result.body"),resultBuffer)) throw failure("SEEDREAM_CAPTURE_INVALID");
    const result=JSON.parse(resultBuffer.toString("utf8"));const resultClass=seedreamResultClass(result,capture.result.http_status,header);
    let rawHash=null;
    if (resultClass !== "one_image") { if (capture.download !== null || rawBytesOrNull !== null) throw failure("SEEDREAM_CAPTURE_INVALID"); }
    else {
      const raw=bounded(rawBytesOrNull,MAX_PNG_BYTES),d=capture.download;
      if (!exactKeys(d,["url","http_status","content_type","raw_ref","bytes"]) || d.url !== result.images[0].url || outputUrlAllowed(d.url) !== d.url || d.http_status !== 200
        || !(d.content_type === null || typeof d.content_type === "string") || !Number.isSafeInteger(d.bytes) || d.bytes < 0 || d.bytes !== raw.length
        || !ref(d.raw_ref,"provider-raw.bin",raw)) throw failure("SEEDREAM_CAPTURE_INVALID");
      rawHash=sha256(raw);
    }
    return {capture_sha256:sha256(captureBuffer),request_id:expectedIdentity.request_id,raw_sha256:rawHash,result_class:resultClass};
  } catch { throw failure("SEEDREAM_CAPTURE_INVALID"); }
}

function isSeedreamPose(context) { return context.config.schema_version === 5 && context.config.role === "pose_edit"; }

function seedreamIdentity(context,requestId) {
  const task=context.config.authorization.task_context;
  const claimBytes=regularBytes(path.join(context.root,"submission.claim"),"SEEDREAM_CAPTURE_INVALID",MAX_JSON_BYTES);
  if (!validHash(context.bindingSha256)) throw failure("SEEDREAM_CAPTURE_INVALID");
  return {variant:SEEDREAM_VARIANT,attempt_id:task.attempt_id,operation_id:task.operation_id,request_id:requestId,
    config_sha256:context.configSha256,config_file_sha256:context.configFileSha256,input_set_sha256:context.inputSetSha256,binding_sha256:context.bindingSha256,claim_sha256:sha256(claimBytes)};
}

function writeNewOrIdentical(candidate,bytes) {
  const directory=path.dirname(candidate);
  const temporary=path.join(directory,`.${path.basename(candidate)}.${randomUUID()}.partial`);
  let descriptor;
  let ownedTemporary=false;
  try {
    descriptor=fs.openSync(temporary,"wx");
    ownedTemporary=true;
    fs.writeFileSync(descriptor,bytes);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor=undefined;
    try {
      // A hard link publishes the fully written inode atomically without replacing a winner.
      fs.linkSync(temporary,candidate);
    } catch(error) {
      if (error?.code !== "EEXIST" || !regularBytes(candidate,"SEEDREAM_CAPTURE_CONFLICT",MAX_PNG_BYTES).equals(bytes)) throw failure("SEEDREAM_CAPTURE_CONFLICT");
    }
    fsyncDirectory(directory);
  } catch { throw failure("SEEDREAM_CAPTURE_CONFLICT"); }
  finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    // Only this invocation's successfully created temp is removable; published finals are immutable.
    if (ownedTemporary) { try { fs.unlinkSync(temporary);fsyncDirectory(directory); } catch {} }
  }
}

function restoreSeedreamCapture(context,saved) {
  const capturePath=path.join(context.root,"capture","complete.json");
  if (!fs.existsSync(capturePath)) {
    if (saved.seedream_capture != null || saved.state === "SEEDREAM_CAPTURED") throw failure("SEEDREAM_CAPTURE_INVALID");
    return null;
  }
  if (["DOWNLOADED","RESULT_INVALID","FAILED","CANCELLED","REMOTE_REJECTED","SUBMITTING","SUBMISSION_STATUS_UNKNOWN"].includes(saved.state)) throw failure("SEEDREAM_CAPTURE_CONFLICT");
  const record=readJsonFile(capturePath,"SEEDREAM_CAPTURE_INVALID");const capture=record.value;
  const claim=regularBytes(path.join(context.root,"submission.claim"),"SEEDREAM_CAPTURE_INVALID",MAX_JSON_BYTES);
  const urls=canonicalRequestUrls(SEEDREAM,saved.request_id);
  if ((saved.status_url !== undefined && saved.status_url !== urls.statusUrl) || (saved.result_url !== undefined && saved.result_url !== urls.resultUrl)
    || (saved.pricing_receipt_sha256 !== undefined && saved.pricing_receipt_sha256 !== JSON.parse(claim.toString("utf8")).pricing_receipt_sha256)) throw failure("SEEDREAM_CAPTURE_CONFLICT");
  const status=regularBytes(path.join(context.root,"capture","status.body"),"SEEDREAM_CAPTURE_INVALID",MAX_JSON_BYTES);
  const result=regularBytes(path.join(context.root,"capture","result.body"),"SEEDREAM_CAPTURE_INVALID",capture.result?.http_status === 422?MAX_NO_MEDIA_BYTES:MAX_JSON_BYTES);
  const raw=capture.download === null ? null : regularBytes(path.join(context.root,"provider-raw.bin"),"SEEDREAM_CAPTURE_INVALID",MAX_PNG_BYTES);
  const verified=validateSeedreamCaptureEvidence(context.configBytes,claim,record.bytes,status,result,raw,seedreamIdentity(context,saved.request_id));
  const captureRef={path:capturePath,sha256:verified.capture_sha256};const rawRef=capture.download?.raw_ref ?? null;
  const block={schema:"sprite_seedream_capture_state_v1",capture_ref:captureRef,raw_ref:rawRef};
  if ((saved.seedream_capture != null && !canonicalBytes(saved.seedream_capture).equals(canonicalBytes(block)))
    || (saved.raw_path != null && saved.raw_path !== rawRef?.path) || (saved.raw_sha256 != null && saved.raw_sha256 !== rawRef?.sha256)
    || (saved.state === "SEEDREAM_CAPTURED" && (!canonicalBytes(saved.seedream_capture).equals(canonicalBytes(block)) || saved.raw_path !== (rawRef?.path??null) || saved.raw_sha256 !== (rawRef?.sha256??null)))) throw failure("SEEDREAM_CAPTURE_CONFLICT");
  return writeState(context.root,{...saved,state:"SEEDREAM_CAPTURED",seedream_capture:block,raw_path:rawRef?.path??null,raw_sha256:rawRef?.sha256??null});
}

async function captureSeedream(context,fetchImpl,state,statusResponse,statusBody,resultResponse,resultBody) {
  const header=resultResponse.headers.get("x-fal-needs-retry");const retry=header === null ? null : header.trim().toLowerCase();
  const resultClass=seedreamResultClass(resultBody.value,resultResponse.status,retry);
  const urls=canonicalRequestUrls(SEEDREAM,state.request_id);
  const ref=(p,b)=>({path:path.join(context.root,p),sha256:sha256(b)});
  let download=null,raw=null;
  if (resultClass === "one_image") {
    const url=resultBody.value.images[0].url;outputUrlAllowed(url);
    const response=await fetchResponse(fetchImpl,url,{method:"GET",headers:{}},30_000,"DOWNLOAD_FAILED");
    if (response.status !== 200) throw failure("DOWNLOAD_FAILED",true);
    raw=await boundedBytes(response,MAX_PNG_BYTES,"DOWNLOAD_FAILED",true);
    download={url,http_status:200,content_type:response.headers.get("content-type"),raw_ref:ref("provider-raw.bin",raw),bytes:raw.length};
  }
  const capture={schema:"sprite_seedream_capture_v1",...seedreamIdentity(context,state.request_id),
    status:{url:urls.statusUrl,http_status:statusResponse.status,body_ref:ref(path.join("capture","status.body"),statusBody.bytes)},
    result:{url:urls.resultUrl,http_status:resultResponse.status,headers:{x_fal_needs_retry:retry},body_ref:ref(path.join("capture","result.body"),resultBody.bytes)},download};
  const bytes=canonicalBytes(capture);
  validateSeedreamCaptureEvidence(context.configBytes,regularBytes(path.join(context.root,"submission.claim"),"SEEDREAM_CAPTURE_INVALID",MAX_JSON_BYTES),bytes,statusBody.bytes,resultBody.bytes,raw,seedreamIdentity(context,state.request_id));
  const directory=path.join(context.root,"capture");
  if (!fs.existsSync(directory)) { fs.mkdirSync(directory);fsyncDirectory(context.root); }
  ensureJobDir(directory);
  writeNewOrIdentical(capture.status.body_ref.path,statusBody.bytes);writeNewOrIdentical(capture.result.body_ref.path,resultBody.bytes);
  if (raw !== null) writeNewOrIdentical(download.raw_ref.path,raw);
  writeNewOrIdentical(path.join(directory,"complete.json"),bytes);
  return stateOutput(restoreSeedreamCapture(context,state));
}

export function validateEndpointInvalidResultEvidence(configBytes, claimBytes, evidenceBytes, statusBytes, resultBytes, outputBytesOrNull, sourceBytesOrNull, expectedRequestId) {
  try {
    const required = [configBytes,claimBytes,evidenceBytes,statusBytes];
    if (required.some(b => !(b instanceof Uint8Array) || b.length > MAX_JSON_BYTES) || !validRequestId(expectedRequestId)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    const parse = b => JSON.parse(Buffer.from(b).toString("utf8"));
    const evidence = parse(evidenceBytes);
    const noMedia = evidence?.schema === "sprite_endpoint_invalid_result_v2";
    if (!(resultBytes instanceof Uint8Array) || resultBytes.length > (noMedia ? MAX_NO_MEDIA_BYTES : MAX_JSON_BYTES)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    const config = parse(configBytes);
    if (!exactKeys(config,["schema_version","route","role","model","input_files","input","authorization","max_cost_usd"])
      || config.schema_version !== 4 || config.route !== "endpoint_cycle_v1" || !ENDPOINT_ROLES.includes(config.role) || config.model !== ENDPOINT_MODELS[config.role]
      || !exactKeys(config.authorization,["request_authorized","task_context"]) || config.authorization.request_authorized !== true
      || typeof config.max_cost_usd !== "number" || !Number.isFinite(config.max_cost_usd) || config.max_cost_usd < 0) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    validateTaskContext(config.authorization.task_context,config.role);
    validateEndpointInput(config.role,config.input);
    const roles = config.role === "pose_edit" ? ["edit","identity"] : config.role === "cleanup" ? ["grid"] : ["start","end"];
    if (!Array.isArray(config.input_files) || config.input_files.length !== roles.length || config.input_files.some((v,i) => !exactKeys(v,["role","path","sha256"])
      || v.role !== roles[i] || !validHash(v.sha256) || typeof v.path !== "string" || !path.isAbsolute(v.path))) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    const configSha = sha256(canonicalBytes(config));
    const inputSha = sha256(canonicalBytes(config.input_files.map(({role,sha256})=>({role,sha256}))));
    const claim = parse(claimBytes);
    if (!exactKeys(claim,["schema_version","config_sha256","pricing_receipt_sha256","created_at"]) || claim.schema_version !== 3
      || claim.config_sha256 !== configSha || !validHash(claim.pricing_receipt_sha256) || typeof claim.created_at !== "string" || !Number.isFinite(Date.parse(claim.created_at))) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    if (!exactKeys(evidence,["schema","role","model","operation_id","request_id","config_sha256","config_file_sha256","input_set_sha256","claim_sha256","reason","status","result","download"])
      || !["sprite_endpoint_invalid_result_v1","sprite_endpoint_invalid_result_v2"].includes(evidence.schema) || evidence.role !== config.role || evidence.model !== config.model
      || evidence.operation_id !== config.authorization.task_context.operation_id || evidence.request_id !== expectedRequestId
      || evidence.config_sha256 !== configSha || evidence.config_file_sha256 !== sha256(configBytes) || evidence.input_set_sha256 !== inputSha
      || evidence.claim_sha256 !== sha256(claimBytes) || !canonicalBytes(evidence).equals(Buffer.from(evidenceBytes))) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    const urls = canonicalRequestUrls(config.model,expectedRequestId);
    for (const [ref,bytes,url] of [[evidence.status,statusBytes,urls.statusUrl],[evidence.result,resultBytes,urls.resultUrl]]) {
      const errorResult = noMedia && ref === evidence.result;
      if (!exactKeys(ref,["url","http_status","body_sha256",...(errorResult?["headers"]:[])]) || ref.url !== url || ref.http_status !== (errorResult?422:200)
        || ref.body_sha256 !== sha256(bytes) || (errorResult && (!exactKeys(ref.headers,["x_fal_needs_retry"]) || ![null,"false"].includes(ref.headers.x_fal_needs_retry)))) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    }
    const status = parse(statusBytes);
    const result = parse(resultBytes);
    if (!isObject(status) || status.status !== "COMPLETED" || (status.request_id !== undefined && status.request_id !== expectedRequestId)
      || !isObject(result) || (result.request_id !== undefined && result.request_id !== expectedRequestId)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    let sourceMetadata = null;
    if (config.role === "cleanup") {
      if (!(sourceBytesOrNull instanceof Uint8Array) || sourceBytesOrNull.length > 10485760 || sha256(sourceBytesOrNull) !== config.input_files[0].sha256) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
      sourceMetadata = pngMetadata(Buffer.from(sourceBytesOrNull));
    } else if (sourceBytesOrNull !== null) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    let reason;
    if (noMedia) {
      if (!providerNoMediaBody(result)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
      reason = "PROVIDER_NO_MEDIA_GENERATED";
    } else reason = endpointCountFailure(config.model,result);
    if (reason) {
      if (evidence.download !== null || outputBytesOrNull !== null) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    } else {
      const url = resultUrlFromBody(result,config.model,true);
      const ref = evidence.download;
      if (!(outputBytesOrNull instanceof Uint8Array) || outputBytesOrNull.length > (VIDEO_MODELS.has(config.model) ? MAX_MP4_BYTES : MAX_PNG_BYTES)
        || !exactKeys(ref,["url","http_status","body_sha256","bytes"]) || ref.url !== url || ref.http_status !== 200
        || !Number.isSafeInteger(ref.bytes) || ref.bytes !== outputBytesOrNull.length || ref.body_sha256 !== sha256(outputBytesOrNull)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
      reason = endpointByteFailure(config,Buffer.from(outputBytesOrNull),sourceMetadata);
    }
    if (!reason || evidence.reason !== reason) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
    return {state:"RESULT_INVALID",request_id:expectedRequestId,operation_id:evidence.operation_id,role:config.role,reason,evidence_sha256:sha256(evidenceBytes),
      config_sha256:configSha,config_file_sha256:evidence.config_file_sha256,input_set_sha256:inputSha,claim_sha256:evidence.claim_sha256};
  } catch { throw failure("INVALID_RESULT_EVIDENCE_INVALID"); }
}

function invalidResultFiles(context, proof) {
  const dir = path.join(context.root,"result-evidence");
  return {status:path.join(dir,`status-${proof.status.body_sha256}.body.json`),result:path.join(dir,`result-${proof.result.body_sha256}.body.json`),
    output:proof.download === null ? null : path.join(dir,`output-${proof.download.body_sha256}.bin`)};
}

function verifiedInvalidResult(context, state) {
  if (fs.existsSync(path.join(context.root,VIDEO_MODELS.has(context.config.model)?"raw.mp4":"raw.png"))) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
  const record = readJsonFile(path.join(context.root,"result-invalid.json"),"INVALID_RESULT_EVIDENCE_INVALID");
  const proof = record.value;
  // Only derive fixed CAS filenames after structural/hash checks, before opening any bytes.
  if (!validHash(proof?.status?.body_sha256) || !validHash(proof?.result?.body_sha256)
    || !(proof.download === null || validHash(proof.download?.body_sha256))) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
  const files = invalidResultFiles(context,proof);
  const claimRecord = readJsonFile(path.join(context.root,"submission.claim"),"INVALID_RESULT_EVIDENCE_INVALID");
  const claim = claimRecord.bytes;
  if (state.pricing_receipt_sha256 !== undefined && claimRecord.value.pricing_receipt_sha256 !== state.pricing_receipt_sha256) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
  const summary = validateEndpointInvalidResultEvidence(context.configBytes,claim,record.bytes,
    regularBytes(files.status,"INVALID_RESULT_EVIDENCE_INVALID",MAX_JSON_BYTES),regularBytes(files.result,"INVALID_RESULT_EVIDENCE_INVALID",proof.schema === "sprite_endpoint_invalid_result_v2" ? MAX_NO_MEDIA_BYTES : MAX_JSON_BYTES),
    files.output === null ? null : regularBytes(files.output,"INVALID_RESULT_EVIDENCE_INVALID",VIDEO_MODELS.has(context.config.model)?MAX_MP4_BYTES:MAX_PNG_BYTES),
    context.config.role === "cleanup" ? context.sourceBytes : null,state.request_id);
  if (state.result_invalid_sha256 !== undefined && (state.result_invalid_sha256 !== summary.evidence_sha256 || state.result_invalid_reason !== summary.reason)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
  const next = writeState(context.root,{...state,state:"RESULT_INVALID",raw_path:null,raw_sha256:null,result_invalid_sha256:summary.evidence_sha256,result_invalid_reason:summary.reason});
  return stateOutput(next,{block_reason:summary.reason,retry_safe:false});
}

function sealInvalidResult(context, state, completed, bytes, reason, downloadStatus = null) {
  const noMedia = reason === "PROVIDER_NO_MEDIA_GENERATED";
  if (completed.statusCode !== 200 || completed.resultCode !== (noMedia?422:200) || (bytes !== null && downloadStatus !== 200)) throw failure(reason);
  if (fs.existsSync(path.join(context.root,VIDEO_MODELS.has(context.config.model)?"raw.mp4":"raw.png"))) throw failure("RESULT_DESTINATION_EXISTS");
  const claim = regularBytes(path.join(context.root,"submission.claim"),"INVALID_RESULT_EVIDENCE_INVALID",MAX_JSON_BYTES);
  const urls = canonicalRequestUrls(context.config.model,state.request_id);
  const proof = {schema:noMedia?"sprite_endpoint_invalid_result_v2":"sprite_endpoint_invalid_result_v1",role:context.config.role,model:context.config.model,operation_id:context.config.authorization.task_context.operation_id,
    request_id:state.request_id,config_sha256:context.configSha256,config_file_sha256:context.configFileSha256,input_set_sha256:context.inputSetSha256,claim_sha256:sha256(claim),reason,
    status:{url:urls.statusUrl,http_status:completed.statusCode,body_sha256:sha256(completed.statusBytes)},
    result:{url:urls.resultUrl,http_status:completed.resultCode,body_sha256:sha256(completed.resultBytes),...(noMedia?{headers:{x_fal_needs_retry:completed.retryHeader}}:{})},
    download:bytes === null ? null : {url:state.output_url,http_status:downloadStatus,body_sha256:sha256(bytes),bytes:bytes.length}};
  const proofBytes = canonicalBytes(proof);
  validateEndpointInvalidResultEvidence(context.configBytes,claim,proofBytes,completed.statusBytes,completed.resultBytes,bytes,
    context.config.role === "cleanup" ? context.sourceBytes : null,state.request_id);
  const directory = path.join(context.root,"result-evidence");
  if (!fs.existsSync(directory)) { fs.mkdirSync(directory); fsyncDirectory(context.root); }
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || !samePath(fs.realpathSync.native(directory),directory)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
  const put = (file,buffer) => {
    const temporary = path.join(path.dirname(file),`.${path.basename(file)}.${randomUUID()}.partial`);
    try {
      writeNewDurable(temporary,buffer);
      try { fs.linkSync(temporary,file); } catch (error) {
        if (error?.code !== "EEXIST" || !regularBytes(file,"INVALID_RESULT_EVIDENCE_INVALID",Math.max(MAX_JSON_BYTES,buffer.length)).equals(buffer)) throw failure("INVALID_RESULT_EVIDENCE_INVALID");
      }
      fsyncDirectory(path.dirname(file));
    } finally {
      try { fs.unlinkSync(temporary); fsyncDirectory(path.dirname(file)); } catch {}
    }
  };
  const files = invalidResultFiles(context,proof);
  put(files.status,completed.statusBytes); put(files.result,completed.resultBytes);
  if (bytes !== null) put(files.output,bytes);
  put(path.join(context.root,"result-invalid.json"),proofBytes);
  return verifiedInvalidResult(context,state);
}

async function download(context, fetchImpl, url, state, completed = null) {
  const video = VIDEO_MODELS.has(context.config.model);
  const finalPath = path.join(context.root, video ? "raw.mp4" : "raw.png");
  const partialPath = path.join(context.root, video ? ".raw.mp4.partial" : ".raw.png.partial");
  const downloadLock = path.join(context.root, ".download.lock");
  let lockDescriptor;
  try {
    lockDescriptor = fs.openSync(downloadLock, "wx");
    fs.writeFileSync(lockDescriptor, `${process.pid}\n`); fs.fsyncSync(lockDescriptor); fsyncDirectory(context.root);
  } catch { if (lockDescriptor !== undefined) fs.closeSync(lockDescriptor); throw failure("DOWNLOAD_IN_PROGRESS", true); }
  try {
    const response = await fetchResponse(fetchImpl, url, { method: "GET", headers: {} }, 30_000, "DOWNLOAD_FAILED");
    if (!response.ok) throw failure("DOWNLOAD_FAILED", true);
    const bytes = await boundedBytes(response, video ? MAX_MP4_BYTES : MAX_PNG_BYTES, "DOWNLOAD_FAILED");
    if (context.endpoint && completed) {
      const reason = endpointByteFailure(context.config,bytes,context.sourceMetadata);
      if (reason) return sealInvalidResult(context,state,completed,bytes,reason,response.status);
    }
    let rawMetadata = null;
    if (video) {
      let metadata;
      try { metadata = readMp4Metadata(bytes); } catch { throw failure("RESULT_VIDEO_INVALID"); }
      if (metadata.duration_seconds > 30) throw failure("RESULT_VIDEO_INVALID");
      if (context.endpoint && (metadata.width !== metadata.height || metadata.width > 960 || Math.abs(metadata.duration_seconds-81/24) > 1/24)) throw failure("RESULT_VIDEO_INVALID");
      rawMetadata = metadata;
    } else if (bytes.length < 8 || !bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))) {
      throw failure("RESULT_NOT_PNG");
    }
    if (context.endpoint && !video) {
      let metadata;
      try { metadata = pngMetadata(bytes); } catch { throw failure("RESULT_IMAGE_INVALID"); }
      const expected = context.config.role === "pose_edit" ? {width:2048,height:2048} : context.sourceMetadata;
      if (metadata.width !== expected.width || metadata.height !== expected.height) throw failure("RESULT_IMAGE_INVALID");
      rawMetadata = metadata;
    }
    let descriptor;
    try {
      descriptor = fs.openSync(partialPath, "w");
      fs.writeFileSync(descriptor, bytes);
      fs.fsyncSync(descriptor);
    } finally { if (descriptor !== undefined) fs.closeSync(descriptor); }
    const digest = sha256(bytes);
    if (fs.existsSync(finalPath)) {
      const existing = regularBytes(finalPath, "RESULT_DESTINATION_EXISTS", video ? MAX_MP4_BYTES : MAX_PNG_BYTES);
      if (sha256(existing) !== digest || !existing.equals(bytes)) throw failure("RESULT_DESTINATION_EXISTS");
      fs.unlinkSync(partialPath);
    } else {
      fs.renameSync(partialPath, finalPath);
      fsyncDirectory(context.root);
    }
    return writeState(context.root, { ...state, state: "DOWNLOADED", raw_path: finalPath, raw_sha256: digest, downloaded_at: new Date().toISOString(), ...(context.endpoint?{raw_metadata:rawMetadata}:{}) });
  } finally {
    fs.closeSync(lockDescriptor);
    try { fs.unlinkSync(downloadLock); fsyncDirectory(context.root); } catch {}
  }
}

async function resumeKnown(context, fetchImpl, key, saved) {
  if (typeof key !== "string" || key.trim() === "") throw failure("KEY_SETUP_REQUIRED", true);
  if (!validRequestId(saved.request_id)) throw failure("STATE_INVALID");
  const statusUrl = validateQueueUrl(saved.status_url, context.config.model, saved.request_id, "status");
  const resultUrl = validateQueueUrl(saved.result_url, context.config.model, saved.request_id, "result");
  const statusResponse = await fetchResponse(fetchImpl, statusUrl, { method: "GET", headers: { Authorization: `Key ${key}` } }, 15_000, "REMOTE_STATUS_UNAVAILABLE");
  if (!statusResponse.ok) throw failure("REMOTE_STATUS_UNAVAILABLE", true);
  const statusBody = await jsonBody(statusResponse, MAX_JSON_BYTES, "REMOTE_STATUS_INVALID",isSeedreamPose(context));
  if (isSeedreamPose(context) && (statusResponse.status !== 200 || (Object.hasOwn(statusBody.value??{},"request_id") && statusBody.value.request_id !== saved.request_id))) throw failure("REMOTE_STATUS_INVALID");
  const status = statusBody.value?.status;
  if (status === "IN_QUEUE" || status === "IN_PROGRESS") {
    const next = writeState(context.root, { ...saved, state: status, status_url: statusUrl, result_url: resultUrl });
    return stateOutput(next);
  }
  if (status === "FAILED" || status === "CANCELLED") {
    const next = writeState(context.root, { ...saved, state: status, status_url: statusUrl, result_url: resultUrl });
    return stateOutput(next);
  }
  if (status !== "COMPLETED") throw failure("REMOTE_STATUS_INVALID");
  let next = writeState(context.root, { ...saved, state: "COMPLETED", status_url: statusUrl, result_url: resultUrl });
  if (next.state === "DOWNLOADED") return stateOutput(next);
  const resultResponse = await fetchResponse(fetchImpl, resultUrl, { method: "GET", headers: { Authorization: `Key ${key}` } }, 15_000, "REMOTE_RESULT_UNAVAILABLE");
  if (isSeedreamPose(context)) {
    if (![200,422].includes(resultResponse.status)) throw failure("REMOTE_RESULT_UNAVAILABLE",true);
    const result=await jsonBody(resultResponse,resultResponse.status === 422?MAX_NO_MEDIA_BYTES:MAX_JSON_BYTES,"REMOTE_RESULT_INVALID",true);
    return captureSeedream(context,fetchImpl,next,statusResponse,statusBody,resultResponse,result);
  }
  if (context.endpoint && statusResponse.status === 200 && resultResponse.status === 422) {
    const retryHeader = noMediaRetryHeader(resultResponse);
    const errorBody = await jsonBody(resultResponse,MAX_NO_MEDIA_BYTES,"REMOTE_RESULT_INVALID");
    if (!providerNoMediaBody(errorBody.value)) throw failure("REMOTE_RESULT_UNAVAILABLE",true);
    const completed = {statusBytes:statusBody.bytes,statusCode:statusResponse.status,resultBytes:errorBody.bytes,resultCode:422,retryHeader};
    return sealInvalidResult(context,next,completed,null,"PROVIDER_NO_MEDIA_GENERATED");
  }
  if (!resultResponse.ok) throw failure("REMOTE_RESULT_UNAVAILABLE", true);
  const resultBody = await jsonBody(resultResponse, MAX_JSON_BYTES, "REMOTE_RESULT_INVALID");
  const completed = context.endpoint ? {statusBytes:statusBody.bytes,statusCode:statusResponse.status,resultBytes:resultBody.bytes,resultCode:resultResponse.status} : null;
  if (context.endpoint) {
    const reason = endpointCountFailure(context.config.model,resultBody.value);
    if (reason) return sealInvalidResult(context,next,completed,null,reason);
  }
  const remoteOutputUrl = resultUrlFromBody(resultBody.value, context.config.model, context.endpoint);
  next = writeState(context.root, { ...next, state: "RESULT_READY", output_url: remoteOutputUrl });
  if (next.state === "DOWNLOADED") return stateOutput(next);
  const downloaded = await download(context, fetchImpl, remoteOutputUrl, next, completed);
  if (downloaded.state === "RESULT_INVALID") return downloaded;
  return stateOutput(downloaded);
}

async function dispatchKnown(context, fetchImpl, key, saved) {
  if (isSeedreamPose(context)) { const restored=restoreSeedreamCapture(context,saved);if(restored)return stateOutput(restored); }
  if (context.endpoint && (saved.state === "RESULT_INVALID" || fs.existsSync(path.join(context.root,"result-invalid.json")))) return verifiedInvalidResult(context,saved);
  if (saved.state === "DOWNLOADED") {
    const expectedRawPath = path.join(context.root, VIDEO_MODELS.has(context.config.model) ? "raw.mp4" : "raw.png");
    if (saved.raw_path !== expectedRawPath || !validHash(saved.raw_sha256)) throw failure("RAW_TAMPERED");
    const bytes = regularBytes(saved.raw_path, "RAW_TAMPERED", saved.raw_path.endsWith(".mp4") ? MAX_MP4_BYTES : MAX_PNG_BYTES);
    if (sha256(bytes) !== saved.raw_sha256) throw failure("RAW_TAMPERED");
    if (VIDEO_MODELS.has(context.config.model)) { try { readMp4Metadata(bytes); } catch { throw failure("RAW_TAMPERED"); } }
    else if (!bytes.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))) throw failure("RAW_TAMPERED");
    return stateOutput(saved);
  }
  if (["REMOTE_REJECTED", "FAILED", "CANCELLED"].includes(saved.state)) return stateOutput(saved);
  return resumeKnown(context, fetchImpl, key, saved);
}

async function execute(context, fetchImpl, key, now, taskBridge) {
  let saved = readState(context.root);
  const ledger = context.managed ? await verifiedTaskBridge(context, "check-context", taskBridge) : null;
  if (saved?.config_sha256 && saved.config_sha256 !== context.configSha256) throw failure("CONFIG_CHANGED_AFTER_SUBMISSION");
  if (isSeedreamPose(context)) {
    context.bindingSha256=ledger.binding_sha256;
    const known=saved?.request_id??ledger.provider_request_id;
    if (known && fs.existsSync(path.join(context.root,"capture","complete.json"))) {
      if ((saved?.request_id && ledger.provider_request_id && saved.request_id !== ledger.provider_request_id) || !ledger.submission_started || ["cancelled","rejected"].includes(ledger.outcome_status)) throw failure("SEEDREAM_CAPTURE_CONFLICT");
      const urls=canonicalRequestUrls(SEEDREAM,known);
      const recoveryState={...(saved??{schema_version:3,config_sha256:context.configSha256,raw_path:null,raw_sha256:null}),request_id:known,
        ...(!saved?.request_id && (!saved || ["SUBMITTING","SUBMISSION_STATUS_UNKNOWN","REQUEST_ACCEPTED"].includes(saved.state)) ? {state:"REQUEST_ACCEPTED",status_url:urls.statusUrl,result_url:urls.resultUrl} : {})};
      const restored=restoreSeedreamCapture(context,recoveryState);
      return stateOutput(restored);
    }
  }
  if (ledger) saved = reconcileManagedState(context, saved, ledger);
  if (saved?.request_id || ["DOWNLOADED", "REMOTE_REJECTED", "FAILED", "CANCELLED", ...(context.endpoint?["RESULT_INVALID"]:[])].includes(saved?.state)) return dispatchKnown(context, fetchImpl, key, saved);
  const claimPath = path.join(context.root, "submission.claim");
  if (ledger?.submission_started || fs.existsSync(claimPath) || saved?.state === "SUBMITTING" || saved?.state === "SUBMISSION_STATUS_UNKNOWN") {
    return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: saved?.estimated_cost_usd ?? null, block_reason: "SUBMISSION_CLAIM_WITHOUT_REQUEST_ID", retry_safe: false });
  }
  if (typeof key !== "string" || key.trim() === "") throw failure("KEY_SETUP_REQUIRED", true);
  const receipt = receiptForExecute(context, now());
  if (receipt.estimated_cost_usd > context.config.max_cost_usd) throw failure("COST_CAP_EXCEEDED");
  if (context.managed) {
    const permission = await verifiedTaskBridge(context, "before-submit", taskBridge, receipt, context.regional ? ledger?.regional_state_sha256 : ledger?.experiment_state_sha256);
    if (context.regional && permission.binding_sha256 !== ledger.binding_sha256) throw failure("TASK_CONTEXT_CONFLICT");
    if (isSeedreamPose(context) && permission.binding_sha256 !== context.bindingSha256) throw failure("TASK_CONTEXT_CONFLICT");
    if (!permission.submit_allowed) {
      saved = reconcileManagedState(context, saved, permission);
      if (saved?.request_id) return dispatchKnown(context, fetchImpl, key, saved);
      return output({ state: "SUBMISSION_STATUS_UNKNOWN", block_reason: "SUBMISSION_CLAIM_WITHOUT_REQUEST_ID" });
    }
    if (!permission.submission_started || permission.provider_request_id !== null) throw failure("TASK_BRIDGE_INVALID");
    // The same archived receipt may expire while the marker callback runs. Never erase that marker.
    const instant = now();
    const age = instant - Date.parse(receipt.retrieved_at);
    if (!Number.isFinite(instant) || age < -PRICE_FUTURE_SKEW_MS || age > (context.regional ? REGIONAL_PRICE_TTL : PRICE_MAX_AGE_MS)) throw failure("PRICE_STALE");
    if (context.regional) {
      // Callback is an async boundary: quote/config/inputs and the physical authorization snapshot must be unchanged.
      const refreshed = loadContext(context.root), currentReceipt = regionalReceiptForExecute(context,instant);
      if (refreshed.configFileSha256 !== context.configFileSha256 || refreshed.configSha256 !== context.configSha256
        || refreshed.inputSetSha256 !== context.inputSetSha256 || refreshed.planSha256 !== context.planSha256
        || refreshed.scopeFileSha256 !== context.scopeFileSha256 || refreshed.scopeSealFileSha256 !== context.scopeSealFileSha256
        || refreshed.runManifestFileSha256 !== context.runManifestFileSha256
        || currentReceipt.receipt_sha256 !== receipt.receipt_sha256) throw failure("TASK_CONTEXT_CONFLICT");
    }
  }
  if (!createClaim(context, receipt.receipt_sha256)) return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: receipt.estimated_cost_usd, block_reason: "SUBMISSION_CLAIM_WITHOUT_REQUEST_ID" });
  saved = writeState(context.root, {
    schema_version: 3, state: "SUBMITTING", config_sha256: context.configSha256, request_id: null,
    pricing_receipt_sha256: receipt.receipt_sha256,
    estimated_cost_usd: receipt.estimated_cost_usd, raw_path: null, raw_sha256: null,
    ...(context.config.schema_version === 5 ? {seedream_capture:null} : {}),
  });
  const submitUrl = `https://queue.fal.run/${context.config.model}`;
  let response;
  try {
    response = await fetchImpl(submitUrl, {
      method: "POST", headers: { Authorization: `Key ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify(submissionInput(context)), redirect: "error", signal: AbortSignal.timeout(30_000),
    });
  } catch {
    writeState(context.root, { ...saved, state: "SUBMISSION_STATUS_UNKNOWN" });
    return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: receipt.estimated_cost_usd, block_reason: "SUBMIT_OUTCOME_UNKNOWN", retry_safe: false });
  }
  if (!response || typeof response.status !== "number" || (typeof response.url === "string" && response.url !== "" && response.url !== submitUrl)) {
    writeState(context.root, { ...saved, state: "SUBMISSION_STATUS_UNKNOWN" });
    return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: receipt.estimated_cost_usd, block_reason: "SUBMIT_OUTCOME_UNKNOWN" });
  }
  if (response.status >= 400 && response.status <= 499) {
    const rejected = writeState(context.root, { ...saved, state: "REMOTE_REJECTED" });
    try { await response.body?.cancel(); } catch {}
    return stateOutput(rejected, { block_reason: "REMOTE_REJECTED" });
  }
  if (!response.ok) {
    writeState(context.root, { ...saved, state: "SUBMISSION_STATUS_UNKNOWN" });
    try { await response.body?.cancel(); } catch {}
    return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: receipt.estimated_cost_usd, block_reason: "SUBMIT_OUTCOME_UNKNOWN" });
  }
  let body;
  try { body = (await jsonBody(response, MAX_JSON_BYTES, "SUBMISSION_RESPONSE_INVALID")).value; }
  catch {
    writeState(context.root, { ...saved, state: "SUBMISSION_STATUS_UNKNOWN" });
    return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: receipt.estimated_cost_usd, block_reason: "SUBMISSION_RESPONSE_INVALID" });
  }
  const id = body?.request_id;
  if (!validRequestId(id)) {
    writeState(context.root, { ...saved, state: "SUBMISSION_STATUS_UNKNOWN" });
    return output({ state: "SUBMISSION_STATUS_UNKNOWN", estimated_cost_usd: receipt.estimated_cost_usd, block_reason: "SUBMISSION_RESPONSE_INVALID" });
  }
  const defaults = canonicalRequestUrls(context.config.model, id);
  saved = writeState(context.root, {
    ...saved,
    state: "REQUEST_ACCEPTED",
    request_id: id,
    status_url: defaults.statusUrl,
    result_url: defaults.resultUrl,
    accepted_at: new Date().toISOString(),
  });
  try {
    if (body.status_url !== undefined) validateQueueUrl(body.status_url, context.config.model, id, "status");
    if (body.response_url !== undefined) validateQueueUrl(body.response_url, context.config.model, id, "result");
    if (body.result_url !== undefined) validateQueueUrl(body.result_url, context.config.model, id, "result");
  } catch {
    return output({
      state: "BLOCKED",
      request_id: id,
      estimated_cost_usd: receipt.estimated_cost_usd,
      block_reason: "AUXILIARY_QUEUE_URL_INVALID",
      retry_safe: true,
    });
  }
  return resumeKnown(context, fetchImpl, key, saved);
}

export async function runCommand(command, jobDir, { fetchImpl = fetch, now = () => Date.now(), key = process.env.FAL_KEY, taskBridge = fixedTaskBridge } = {}) {
  try {
    if (!["pricing", "pricing-plan", "execute"].includes(command)) throw failure("INVALID_COMMAND");
    if (typeof fetchImpl !== "function" || typeof now !== "function" || typeof taskBridge !== "function") throw failure("INVALID_ARGUMENTS");
    const context = command === "pricing-plan" ? loadCleanupPlan(jobDir) : loadContext(jobDir);
    return command !== "execute" ? await pricing(context, fetchImpl, key, now) : await execute(context, fetchImpl, key, now, taskBridge);
  } catch (error) {
    const result = blocked(error);
    // Keep a durable known ID visible even when later context or auxiliary recovery checks fail.
    if (command === "execute") {
      try { const saved = readState(ensureJobDir(jobDir)); if (validRequestId(saved?.request_id)) result.request_id = saved.request_id; } catch {}
    }
    return result;
  }
}

function userEnvironmentKey() {
  const direct = process.env.FAL_KEY;
  if (typeof direct === "string" && direct.trim() !== "") return direct;
  if (process.platform !== "win32") return undefined;
  const childEnv = {};
  for (const name of ["SystemRoot", "WINDIR"]) if (typeof process.env[name] === "string") childEnv[name] = process.env[name];
  try {
    const result = spawnSync("reg.exe", ["query", "HKCU\\Environment", "/v", "FAL_KEY"], { encoding: "utf8", windowsHide: true, env: childEnv, timeout: 5_000 });
    if (result.status !== 0 || typeof result.stdout !== "string") return undefined;
    const match = /^\s*FAL_KEY\s+REG_(?:SZ|EXPAND_SZ)\s+(.+)$/mi.exec(result.stdout);
    return match?.[1]?.trim() || undefined;
  } catch { return undefined; }
}

async function main(argv) {
  if (argv.length !== 3 || !new Set(["pricing", "pricing-plan", "execute"]).has(argv[0]) || argv[1] !== "--job-dir" || typeof argv[2] !== "string" || argv[2] === "") {
    process.stdout.write(`${JSON.stringify(output({ state: "BLOCKED", block_reason: "INVALID_ARGUMENTS" }))}\n`);
    return 2;
  }
  const result = await runCommand(argv[0], argv[2], { key: userEnvironmentKey() });
  process.stdout.write(`${JSON.stringify(result)}\n`);
  return result.state === "BLOCKED" || result.state === "KEY_SETUP_REQUIRED" || result.state === "SUBMISSION_STATUS_UNKNOWN" ? 2 : 0;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = await main(process.argv.slice(2));
}
