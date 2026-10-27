import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";


const PRIMARY_MODEL = "fal-ai/qwen-image-2/edit";
const FALLBACK_MODEL = "fal-ai/nano-banana-2/edit";
const ALLOWED_MODELS = new Set([PRIMARY_MODEL, FALLBACK_MODEL]);
const MAX_PRIMARY_SUBMITS = 10;
const MAX_FALLBACK_SUBMITS = 2;
const MAX_BUDGET_USD = 0.50;
const MAX_PRICE_AGE_MS = 15 * 60 * 1000;
const AUTHENTICATED_PRICING_URL = "https://api.fal.ai/v1/models/pricing";
const PREPARED_MARKER = "prepared-run.json";
const TERMINAL_RUN_STATES = new Set([
  "BLOCKED",
  "TECHNICAL_REJECTED",
  "READY_FOR_VISUAL_REVIEW",
]);
const ACTIVE_REQUEST_STATUSES = new Set(["IN_QUEUE", "IN_PROGRESS"]);
const REQUIRED_RUN_DIRECTORIES = [
  "input/master",
  "input/pose-guides",
  "input/references",
  "raw",
  "normalized/frames",
  "previews",
  "reports",
];
const TERMINAL_STATUSES = new Set(["FAILED", "CANCELLED"]);
const KNOWN_STATUSES = new Set([
  "IN_QUEUE",
  "IN_PROGRESS",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "SUBMISSION_STATUS_UNKNOWN",
]);
const SAFE_ERROR_TYPES = new Set([
  "AbortError",
  "InvalidSubmissionResponse",
  "NetworkError",
  "RESULT_VALIDATION_ERROR",
  "SubmitError",
  "TimeoutError",
  "image_too_small",
]);
const SAFE_REQUEST_FIELDS = new Set([
  "frame_index",
  "attempt",
  "model",
  "request_id",
  "estimated_cost_usd",
  "status",
  "submitted_at",
  "result_path",
  "result_sha256",
  "request_fingerprint",
  "safe_error_type",
]);

const LITERAL_SEQUENTIAL_MANIFEST = {
  mode: "sequential_reconstruction",
  project_root: "C:/UnityProjects/tower1",
  source_path: "Assets/Art/GeneratedWork/Enemies/Ogro/Walk/source/Ogre_Walk_S_Base.png",
  entity: "ogre",
  action: "walk",
  direction: "S",
  frame_count: 8,
  frame_width: 256,
  frame_height: 416,
  technical_scale: 2,
  anchor: "bottom-center",
  root_motion: false,
  max_budget_usd: 0.50,
  primary_model: PRIMARY_MODEL,
  fallback_model: FALLBACK_MODEL,
  max_primary_submits: 10,
  max_fallback_submits: 2,
  pose_names: [
    "left_contact", "left_down", "left_pass", "left_up",
    "right_contact", "right_down", "right_pass", "right_up",
  ],
  poses: [
    { name: "left_contact", support_foot: "left", stride: -1.0, counter_swing: 1.0, body_y: 0.0 },
    { name: "left_down", support_foot: "left", stride: -0.6, counter_swing: 0.6, body_y: 1.0 },
    { name: "left_pass", support_foot: "left", stride: 0.0, counter_swing: 0.0, body_y: 0.0 },
    { name: "left_up", support_foot: "left", stride: 0.6, counter_swing: -0.6, body_y: -1.0 },
    { name: "right_contact", support_foot: "right", stride: 1.0, counter_swing: -1.0, body_y: 0.0 },
    { name: "right_down", support_foot: "right", stride: 0.6, counter_swing: -0.6, body_y: 1.0 },
    { name: "right_pass", support_foot: "right", stride: 0.0, counter_swing: 0.0, body_y: 0.0 },
    { name: "right_up", support_foot: "right", stride: -0.6, counter_swing: 0.6, body_y: -1.0 },
  ],
  identity_regions: {
    head_face: { bbox_ratio: [0.42, 0.00, 0.88, 0.25], master_hamming_max: 10, previous_hamming_max: 8 },
    chest: { bbox_ratio: [0.22, 0.18, 0.78, 0.48], master_hamming_max: 14, previous_hamming_max: 12 },
    belt: { bbox_ratio: [0.16, 0.42, 0.82, 0.59], master_hamming_max: 14, previous_hamming_max: 12 },
    screen_left_bracer: { bbox_ratio: [0.00, 0.30, 0.34, 0.62], master_hamming_max: 14, previous_hamming_max: 12 },
    screen_right_bracer: { bbox_ratio: [0.68, 0.32, 1.00, 0.64], master_hamming_max: 14, previous_hamming_max: 12 },
    main_loincloth: { bbox_ratio: [0.34, 0.49, 0.72, 0.82], master_hamming_max: 14, previous_hamming_max: 12 },
  },
  foot_regions: {
    screen_left: { bbox_ratio: [0.05, 0.78, 0.50, 1.00], leg_corridor_ratio: [0.18, 0.50, 0.49, 0.96] },
    screen_right: { bbox_ratio: [0.50, 0.78, 0.98, 1.00], leg_corridor_ratio: [0.51, 0.50, 0.86, 0.96] },
  },
  qa: {
    center_drift_px_max: 4,
    opaque_area_ratio_min: 0.90,
    opaque_area_ratio_max: 1.10,
    bbox_ratio_delta_max: 0.05,
    palette_colors: 64,
    palette_rgb_distance_max: 48,
    chroma_rgb_distance_max: 24,
    out_of_palette_ratio_max: 0.02,
    new_alpha_components_max: 2,
    orphan_area_ratio_max: 0.005,
    new_foot_island_area_px_max: 3,
    planted_foot_drift_px_max: 2,
  },
};


function blocked(reason) {
  const error = new Error(reason);
  error.reason = reason;
  return error;
}


function parseArgs(argv) {
  const allowed = new Set([
    "manifest",
    "run-manifest",
    "pricing",
    "request-input",
    "client-module",
  ]);
  const required = ["manifest", "run-manifest", "pricing", "request-input"];
  const args = {};
  if (argv.length % 2 !== 0) throw blocked("INVALID_ARGUMENTS");
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    const name = typeof flag === "string" && flag.startsWith("--")
      ? flag.slice(2)
      : "";
    if (
      !allowed.has(name)
      || Object.hasOwn(args, name)
      || typeof value !== "string"
      || value.trim() === ""
    ) {
      throw blocked("INVALID_ARGUMENTS");
    }
    args[name] = value;
  }
  if (required.some((name) => !Object.hasOwn(args, name))) {
    throw blocked("INVALID_ARGUMENTS");
  }
  return args;
}


function readJson(filePath, reason) {
  try {
    return JSON.parse(fs.readFileSync(path.resolve(filePath), "utf8"));
  } catch {
    throw blocked(reason);
  }
}


function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}


function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}


function isWithin(candidate, parent, allowEqual = false) {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  if (relative === "") return allowEqual;
  return !relative.startsWith("..") && !path.isAbsolute(relative);
}


function samePath(left, right) {
  return path.relative(path.resolve(left), path.resolve(right)) === "";
}


function lstatExisting(candidate, reason) {
  try {
    const stats = fs.lstatSync(candidate, { throwIfNoEntry: false });
    if (!stats) throw blocked(reason);
    return stats;
  } catch (error) {
    if (error?.reason) throw error;
    throw blocked(reason);
  }
}


function canonicalExistingDirectory(candidate, reason, rejectLinked = false) {
  const resolved = path.resolve(candidate);
  const stats = lstatExisting(resolved, reason);
  if ((rejectLinked && stats.isSymbolicLink()) || !stats.isDirectory()) {
    throw blocked(reason);
  }
  try {
    return { resolved, canonical: fs.realpathSync.native(resolved) };
  } catch {
    throw blocked(reason);
  }
}


function validateExistingDirectoryChain(base, target, reason) {
  const baseDirectory = canonicalExistingDirectory(base, reason, true);
  const resolvedTarget = path.resolve(target);
  if (!isWithin(resolvedTarget, baseDirectory.resolved, true)) throw blocked(reason);
  const relative = path.relative(baseDirectory.resolved, resolvedTarget);
  let current = baseDirectory.resolved;
  for (const component of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    let stats;
    try {
      stats = fs.lstatSync(current, { throwIfNoEntry: false });
    } catch {
      throw blocked(reason);
    }
    if (!stats) break;
    if (stats.isSymbolicLink() || !stats.isDirectory()) throw blocked(reason);
    let canonical;
    try {
      canonical = fs.realpathSync.native(current);
    } catch {
      throw blocked(reason);
    }
    if (!isWithin(canonical, baseDirectory.canonical, true)) throw blocked(reason);
  }
  return baseDirectory;
}


function validateExistingFile(base, candidate, reason) {
  const baseDirectory = validateExistingDirectoryChain(base, path.dirname(candidate), reason);
  const resolved = path.resolve(candidate);
  if (!isWithin(resolved, baseDirectory.resolved)) throw blocked(reason);
  const stats = lstatExisting(resolved, reason);
  if (stats.isSymbolicLink() || !stats.isFile()) throw blocked(reason);
  let canonical;
  try {
    canonical = fs.realpathSync.native(resolved);
  } catch {
    throw blocked(reason);
  }
  if (!isWithin(canonical, baseDirectory.canonical)) throw blocked(reason);
  return canonical;
}


function sameJsonValue(left, right) {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => sameJsonValue(item, right[index]));
  }
  if (!isObject(left) || !isObject(right)) return false;
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => (
      key === rightKeys[index] && sameJsonValue(left[key], right[key])
    ));
}


function validateManifest(manifest) {
  if (!isObject(manifest) || !sameJsonValue(manifest, LITERAL_SEQUENTIAL_MANIFEST)) {
    throw blocked("INVALID_MANIFEST");
  }
  return manifest;
}


function preparedProjectRoot(args, manifest) {
  const testProjectRoot = process.env.TOWER_SPRITE_TEST_PROJECT_ROOT;
  if (testProjectRoot === undefined) return manifest.project_root;
  const fakeRootValue = process.env.FAKE_ROOT;
  if (
    testProjectRoot.trim() === ""
    || process.env.NODE_ENV !== "test"
    || !Object.hasOwn(args, "client-module")
    || typeof fakeRootValue !== "string"
    || fakeRootValue.trim() === ""
  ) {
    throw blocked("INVALID_ARGUMENTS");
  }
  const fakeRoot = canonicalExistingDirectory(
    fakeRootValue,
    "INVALID_ARGUMENTS",
    true,
  );
  if (!samePath(fakeRoot.resolved, fakeRoot.canonical)) {
    throw blocked("INVALID_ARGUMENTS");
  }

  const projectRootValue = path.resolve(testProjectRoot);
  validateExistingDirectoryChain(fakeRoot.resolved, projectRootValue, "INVALID_ARGUMENTS");
  const projectRoot = canonicalExistingDirectory(
    projectRootValue,
    "INVALID_ARGUMENTS",
    true,
  );
  if (
    !isWithin(projectRoot.resolved, fakeRoot.resolved)
    || !isWithin(projectRoot.canonical, fakeRoot.canonical)
    || !samePath(projectRoot.resolved, projectRoot.canonical)
  ) {
    throw blocked("INVALID_ARGUMENTS");
  }

  const clientModuleValue = path.resolve(args["client-module"]);
  const clientModule = validateExistingFile(
    fakeRoot.resolved,
    clientModuleValue,
    "INVALID_ARGUMENTS",
  );
  if (
    !isWithin(clientModule, fakeRoot.canonical)
    || !samePath(clientModuleValue, clientModule)
  ) {
    throw blocked("INVALID_ARGUMENTS");
  }
  return projectRoot.canonical;
}


function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!isObject(value)) return value;
  return Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, canonicalValue(value[key])]),
  );
}


function requestFingerprint(request) {
  const payload = canonicalValue({
    frame_index: request.frame_index,
    model: request.model,
    input: request.input,
  });
  return createHash("sha256").update(JSON.stringify(payload), "utf8").digest("hex");
}


function validateRequestInput(request, manifest) {
  if (!isObject(request)) throw blocked("INVALID_REQUEST_INPUT");
  const keys = Object.keys(request).sort();
  if (!sameJsonValue(keys, ["frame_index", "input", "model", "request_fingerprint"])) {
    throw blocked("INVALID_REQUEST_INPUT");
  }
  if (
    !Number.isInteger(request.frame_index)
    || request.frame_index < 1
    || request.frame_index > manifest.frame_count
    || !ALLOWED_MODELS.has(request.model)
    || (request.model !== manifest.primary_model && request.model !== manifest.fallback_model)
    || !isObject(request.input)
  ) {
    throw blocked("INVALID_REQUEST_INPUT");
  }
  const baseInputKeys = [
    "enable_prompt_expansion",
    "image_size",
    "image_urls",
    "negative_prompt",
    "num_images",
    "output_format",
    "prompt",
    "seed",
  ];
  const expectedInputKeys = request.model === FALLBACK_MODEL
    ? [...baseInputKeys, "resolution"].sort()
    : baseInputKeys;
  const input = request.input;
  if (
    !sameJsonValue(Object.keys(input).sort(), expectedInputKeys)
    || input.enable_prompt_expansion !== false
    || !sameJsonValue(input.image_size, { width: 512, height: 832 })
    || !Array.isArray(input.image_urls)
    || ![2, 3].includes(input.image_urls.length)
    || !input.image_urls.every((url) => (
      typeof url === "string" && /^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(url)
    ))
    || typeof input.negative_prompt !== "string"
    || input.negative_prompt.trim() === ""
    || input.num_images !== 1
    || input.output_format !== "png"
    || typeof input.prompt !== "string"
    || input.prompt.trim() === ""
    || input.prompt.length >= 800
    || !Number.isInteger(input.seed)
    || input.seed < 0
    || (request.model === FALLBACK_MODEL && input.resolution !== "0.5K")
    || typeof request.request_fingerprint !== "string"
    || !/^[0-9a-f]{64}$/.test(request.request_fingerprint)
  ) {
    throw blocked("INVALID_REQUEST_INPUT");
  }
  return request;
}


function verifyPricing(pricing, manifest, now = Date.now()) {
  if (
    !isObject(pricing)
    || pricing.source_url !== AUTHENTICATED_PRICING_URL
    || !Array.isArray(pricing.prices)
  ) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  const verifiedAt = Date.parse(pricing.verified_at);
  const age = now - verifiedAt;
  if (!Number.isFinite(verifiedAt) || age < 0 || age > MAX_PRICE_AGE_MS) {
    throw blocked("PRICE_STALE");
  }
  const byId = new Map();
  for (const item of pricing.prices) {
    if (!isObject(item) || typeof item.endpoint_id !== "string") continue;
    if (byId.has(item.endpoint_id)) throw blocked("PRICE_NOT_VERIFIABLE");
    byId.set(item.endpoint_id, item);
  }
  for (const endpoint of ALLOWED_MODELS) {
    const item = byId.get(endpoint);
    if (
      !item
      || item.currency !== "USD"
      || !["image", "images"].includes(item.unit)
      || !isFiniteNumber(item.unit_price)
      || item.unit_price <= 0
    ) {
      throw blocked("PRICE_NOT_VERIFIABLE");
    }
  }
  const primary = byId.get(manifest.primary_model).unit_price;
  const fallbackItem = byId.get(manifest.fallback_model);
  const multiplier = fallbackItem.resolution_multipliers?.["0.5K"];
  if (!isFiniteNumber(multiplier) || multiplier <= 0) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  const fallback05k = fallbackItem.unit_price * multiplier;
  if (!Number.isFinite(fallback05k) || fallback05k <= 0) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  return { primary, fallback05k };
}


function validatePreparedRun(manifest, runManifestPath, run, projectRootValue) {
  if (!isObject(run)) throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  const resolvedRunManifest = path.resolve(runManifestPath);
  const reports = path.dirname(resolvedRunManifest);
  const workspace = path.dirname(reports);
  const projectRoot = canonicalExistingDirectory(
    projectRootValue,
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  const workspaceRoot = canonicalExistingDirectory(
    workspace,
    "WORKSPACE_CONFINEMENT_FAILED",
    true,
  );
  if (
    path.basename(reports) !== "reports"
    || resolvedRunManifest !== path.join(reports, "run_manifest.json")
    || !sameJsonValue(run.normalized_input, manifest)
    || !Array.isArray(run.chroma_rgb)
    || run.chroma_rgb.length !== 3
    || !run.chroma_rgb.every((value) => Number.isInteger(value) && value >= 0 && value <= 255)
  ) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  validateExistingDirectoryChain(workspace, reports, "WORKSPACE_CONFINEMENT_FAILED");
  validateExistingFile(reports, resolvedRunManifest, "WORKSPACE_CONFINEMENT_FAILED");
  if (
    samePath(workspaceRoot.canonical, projectRoot.canonical)
    || isWithin(workspaceRoot.canonical, projectRoot.canonical)
  ) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  const runProjectRoot = canonicalExistingDirectory(
    run.project_root || "",
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  if (!samePath(runProjectRoot.canonical, projectRoot.canonical)) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  const expectedSource = path.resolve(projectRoot.resolved, manifest.source_path);
  const canonicalExpectedSource = validateExistingFile(
    projectRoot.resolved,
    expectedSource,
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  let canonicalRunSource;
  try {
    canonicalRunSource = fs.realpathSync.native(path.resolve(run.source_path || ""));
  } catch {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  if (
    !isWithin(canonicalExpectedSource, projectRoot.canonical)
    || !samePath(canonicalRunSource, canonicalExpectedSource)
  ) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  for (const relative of REQUIRED_RUN_DIRECTORIES) {
    const directory = path.join(workspace, relative);
    validateExistingDirectoryChain(workspace, directory, "WORKSPACE_CONFINEMENT_FAILED");
    const stats = lstatExisting(directory, "WORKSPACE_CONFINEMENT_FAILED");
    if (stats.isSymbolicLink() || !stats.isDirectory()) {
      throw blocked("WORKSPACE_CONFINEMENT_FAILED");
    }
  }
  const markerPath = path.join(reports, PREPARED_MARKER);
  validateExistingFile(reports, markerPath, "WORKSPACE_CONFINEMENT_FAILED");
  const marker = readJson(markerPath, "WORKSPACE_CONFINEMENT_FAILED");
  const markerProjectRoot = canonicalExistingDirectory(
    marker.project_root || "",
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  const markerWorkspaceRoot = canonicalExistingDirectory(
    marker.workspace_root || "",
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  const parentWorkspaceRoot = canonicalExistingDirectory(
    path.dirname(workspace),
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  if (
    marker.format !== "tower-sprite-pipeline-prepared-run-v1"
    || !samePath(markerProjectRoot.canonical, projectRoot.canonical)
    || !samePath(markerWorkspaceRoot.canonical, parentWorkspaceRoot.canonical)
    || marker.state !== "PREPARING"
  ) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  validatePersistedRequests(run, manifest, workspace);
  return workspace;
}


function validatePersistedRequests(run, manifest, workspace) {
  if (run.requests === undefined) run.requests = [];
  if (!Array.isArray(run.requests)) throw blocked("INVALID_RUN_MANIFEST");
  for (const [index, request] of run.requests.entries()) {
    if (!isObject(request)) throw blocked("INVALID_RUN_MANIFEST");
    const keys = Object.keys(request);
    if (keys.some((key) => !SAFE_REQUEST_FIELDS.has(key))) {
      throw blocked("INVALID_RUN_MANIFEST");
    }
    if (
      !Number.isInteger(request.frame_index)
      || request.frame_index < 1
      || request.frame_index > manifest.frame_count
      || !Number.isInteger(request.attempt)
      || request.attempt !== index + 1
      || !ALLOWED_MODELS.has(request.model)
      || !isFiniteNumber(request.estimated_cost_usd)
      || request.estimated_cost_usd < 0
      || !KNOWN_STATUSES.has(request.status)
      || !Number.isFinite(Date.parse(request.submitted_at))
      || !Object.hasOwn(request, "result_path")
      || typeof request.request_fingerprint !== "string"
      || !/^[0-9a-f]{64}$/.test(request.request_fingerprint)
    ) {
      throw blocked("INVALID_RUN_MANIFEST");
    }
    if (
      request.request_id !== null
      && (typeof request.request_id !== "string" || request.request_id.trim() === "")
    ) {
      throw blocked("INVALID_RUN_MANIFEST");
    }
    if (request.request_id === null && request.status !== "SUBMISSION_STATUS_UNKNOWN") {
      throw blocked("INVALID_RUN_MANIFEST");
    }
    if (request.result_path !== null) {
      if (typeof request.result_path !== "string") throw blocked("INVALID_RUN_MANIFEST");
      const resultPath = path.resolve(workspace, request.result_path);
      if (!isWithin(resultPath, path.join(workspace, "raw"))) {
        throw blocked("WORKSPACE_CONFINEMENT_FAILED");
      }
      if (
        typeof request.result_sha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(request.result_sha256)
      ) {
        throw blocked("INVALID_RUN_MANIFEST");
      }
    } else if (Object.hasOwn(request, "result_sha256")) {
      throw blocked("INVALID_RUN_MANIFEST");
    }
    if (
      Object.hasOwn(request, "safe_error_type")
      && !isSafeErrorType(request.safe_error_type)
    ) {
      throw blocked("INVALID_RUN_MANIFEST");
    }
  }
}


function frameRecord(run, frameIndex) {
  if (!Array.isArray(run.frames)) throw blocked("INVALID_RUN_MANIFEST");
  const matches = run.frames.filter((frame) => (
    isObject(frame) && frame.frame_index === frameIndex
  ));
  if (matches.length > 1) throw blocked("INVALID_RUN_MANIFEST");
  return matches[0] || null;
}


function firstUnacceptedFrame(run, frameCount) {
  let first = null;
  for (let frameIndex = 1; frameIndex <= frameCount; frameIndex += 1) {
    const frame = frameRecord(run, frameIndex);
    const accepted = frame !== null && frame.state === "ACCEPTED";
    if (!accepted && first === null) first = frameIndex;
    if (accepted && first !== null) throw blocked("INVALID_FRAME_CHAIN");
  }
  return first;
}


function evaluatedRawPaths(frame) {
  if (frame === null) return new Set();
  if (!Array.isArray(frame.attempts)) throw blocked("INVALID_RUN_MANIFEST");
  return new Set(
    frame.attempts
      .filter((attempt) => isObject(attempt) && typeof attempt.raw_path === "string")
      .map((attempt) => attempt.raw_path.replaceAll("\\", "/")),
  );
}


function completedResultPendingEvaluation(run) {
  return run.requests.some((request) => (
    request.status === "COMPLETED"
    && typeof request.result_path === "string"
    && !evaluatedRawPaths(frameRecord(run, request.frame_index)).has(
      request.result_path.replaceAll("\\", "/"),
    )
  ));
}


function recoveryModel(run, manifest, frameIndex) {
  const frameRequests = run.requests.filter((request) => request.frame_index === frameIndex);
  const primaryByFrame = new Map();
  for (const request of run.requests) {
    if (request.model !== manifest.primary_model) continue;
    primaryByFrame.set(
      request.frame_index,
      (primaryByFrame.get(request.frame_index) || 0) + 1,
    );
  }
  const extraPrimary = [...primaryByFrame.values()]
    .reduce((total, count) => total + Math.max(0, count - 1), 0);
  const primaryCount = run.requests.filter(
    (request) => request.model === manifest.primary_model,
  ).length;
  const fallbackCount = run.requests.filter(
    (request) => request.model === manifest.fallback_model,
  ).length;
  const framePrimary = frameRequests.filter(
    (request) => request.model === manifest.primary_model,
  ).length;
  const frameFallback = frameRequests.filter(
    (request) => request.model === manifest.fallback_model,
  ).length;
  if (framePrimary === 0) return manifest.primary_model;
  if (
    framePrimary === 1
    && extraPrimary < 2
    && primaryCount < manifest.max_primary_submits
  ) {
    return manifest.primary_model;
  }
  if (frameFallback === 0 && fallbackCount < manifest.max_fallback_submits) {
    return manifest.fallback_model;
  }
  return null;
}


function validateCurrentRequest(request, run, manifest) {
  if (
    !isObject(run.next_request)
    || !sameJsonValue(
      Object.keys(run.next_request).sort(),
      ["fingerprint", "frame_index", "model"],
    )
    || typeof run.next_request.fingerprint !== "string"
    || !/^[0-9a-f]{64}$/.test(run.next_request.fingerprint)
  ) {
    throw blocked("REQUEST_FINGERPRINT_MISMATCH");
  }
  const calculated = requestFingerprint(request);
  if (
    request.request_fingerprint !== calculated
    || run.next_request.fingerprint !== calculated
  ) {
    throw blocked("REQUEST_FINGERPRINT_MISMATCH");
  }
  const frameIndex = firstUnacceptedFrame(run, manifest.frame_count);
  const model = frameIndex === null ? null : recoveryModel(run, manifest, frameIndex);
  if (
    frameIndex === null
    || model === null
    || request.frame_index !== frameIndex
    || request.model !== model
    || run.next_request.frame_index !== frameIndex
    || run.next_request.model !== model
  ) {
    throw blocked("STALE_REQUEST_INPUT");
  }
}


function isSafeErrorType(value) {
  if (typeof value !== "string" || !SAFE_ERROR_TYPES.has(value)) return false;
  const credential = typeof process.env.FAL_KEY === "string"
    ? process.env.FAL_KEY.trim()
    : "";
  return credential === ""
    || (!value.includes(credential) && !credential.includes(value));
}


function safeErrorType(error, fallback = "Error") {
  const detail = Array.isArray(error?.body?.detail) ? error.body.detail[0] : null;
  for (const candidate of [detail?.type, error?.name, fallback]) {
    if (isSafeErrorType(candidate)) return candidate;
  }
  return null;
}


function modelCounts(run, manifest) {
  return {
    primary: run.requests.filter((item) => item.model === manifest.primary_model).length,
    fallback: run.requests.filter((item) => item.model === manifest.fallback_model).length,
  };
}


function spent(run) {
  return run.requests.reduce((total, item) => total + item.estimated_cost_usd, 0);
}


function remainingWorstCase(run, manifest, prices) {
  const counts = modelCounts(run, manifest);
  return spent(run)
    + Math.max(0, manifest.max_primary_submits - counts.primary) * prices.primary
    + Math.max(0, manifest.max_fallback_submits - counts.fallback) * prices.fallback05k;
}


function summary(run) {
  return {
    attempts: run.requests.length,
    request_ids: run.requests.map((item) => item.request_id).filter(Boolean),
    estimated_cost_usd: spent(run),
  };
}


function writeJsonAtomic(filePath, value) {
  const destination = path.resolve(filePath);
  const temporary = `${destination}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  fs.renameSync(temporary, destination);
}


function persistRun(runManifestPath, run) {
  writeJsonAtomic(runManifestPath, run);
}


function blockedPayload(reason, run = null, details = {}) {
  return {
    state: "BLOCKED",
    reason,
    ...(run ? summary(run) : { attempts: 0, request_ids: [], estimated_cost_usd: 0 }),
    ...details,
  };
}


function unresolvedRequest(run) {
  return run.requests.find((request) => (
    request.request_id !== null
    && (
      !TERMINAL_STATUSES.has(request.status)
      && !(request.status === "COMPLETED" && request.result_path)
    )
  )) || null;
}


async function importClient(clientModule) {
  try {
    const imported = clientModule
      ? await import(pathToFileURL(path.resolve(clientModule)).href)
      : await import("@fal-ai/client");
    const client = imported.fal;
    if (
      !client?.queue
      || typeof client.queue.submit !== "function"
      || typeof client.queue.status !== "function"
      || typeof client.queue.result !== "function"
    ) {
      throw new Error("client shape invalid");
    }
    return client;
  } catch {
    throw blocked("CLIENT_IMPORT_FAILED");
  }
}


function normalizeStatus(value) {
  const status = String(value?.status || "").toUpperCase();
  if (!KNOWN_STATUSES.has(status) || status === "SUBMISSION_STATUS_UNKNOWN") {
    throw blocked("REQUEST_STATUS_INVALID");
  }
  return status;
}


function modelSlug(model) {
  return model.replace(/^fal-ai\//, "").replaceAll("/", "-").replace(/[^A-Za-z0-9-]/g, "-");
}


function validateOutputDestination(workspace, destination) {
  const rawDirectory = path.join(workspace, "raw");
  const resolvedDestination = path.resolve(destination);
  if (!isWithin(resolvedDestination, rawDirectory)) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  const workspaceRoot = validateExistingDirectoryChain(
    workspace,
    path.dirname(resolvedDestination),
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  const existing = fs.lstatSync(resolvedDestination, { throwIfNoEntry: false });
  if (existing) {
    if (existing.isSymbolicLink() || !existing.isFile()) {
      throw blocked("WORKSPACE_CONFINEMENT_FAILED");
    }
    let canonical;
    try {
      canonical = fs.realpathSync.native(resolvedDestination);
    } catch {
      throw blocked("WORKSPACE_CONFINEMENT_FAILED");
    }
    if (!isWithin(canonical, workspaceRoot.canonical)) {
      throw blocked("WORKSPACE_CONFINEMENT_FAILED");
    }
  }
}


function buildCrc32Table() {
  const table = new Uint32Array(256);
  for (let value = 0; value < 256; value += 1) {
    let crc = value;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc & 1) !== 0 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
    }
    table[value] = crc >>> 0;
  }
  return table;
}


const CRC32_TABLE = buildCrc32Table();


function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc = CRC32_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}


function isPng(bytes) {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (bytes.length < signature.length || !bytes.subarray(0, 8).equals(signature)) {
    return false;
  }
  let offset = signature.length;
  let chunkIndex = 0;
  let sawIhdr = false;
  let sawIdat = false;
  while (offset < bytes.length) {
    if (bytes.length - offset < 12) return false;
    const length = bytes.readUInt32BE(offset);
    const typeStart = offset + 4;
    const dataStart = typeStart + 4;
    if (length > bytes.length - dataStart - 4) return false;
    const dataEnd = dataStart + length;
    const chunkEnd = dataEnd + 4;
    const typeBytes = bytes.subarray(typeStart, dataStart);
    if (![...typeBytes].every((value) => (
      (value >= 65 && value <= 90) || (value >= 97 && value <= 122)
    ))) {
      return false;
    }
    const expectedCrc = bytes.readUInt32BE(dataEnd);
    if (crc32(bytes.subarray(typeStart, dataEnd)) !== expectedCrc) return false;
    const type = typeBytes.toString("ascii");
    if (chunkIndex === 0 && type !== "IHDR") return false;
    if (type === "IHDR") {
      if (sawIhdr || chunkIndex !== 0 || length !== 13) return false;
      const width = bytes.readUInt32BE(dataStart);
      const height = bytes.readUInt32BE(dataStart + 4);
      if (width < 1 || height < 1 || width > 0x7fffffff || height > 0x7fffffff) {
        return false;
      }
      sawIhdr = true;
    } else if (!sawIhdr) {
      return false;
    }
    if (type === "IDAT") sawIdat = true;
    if (type === "IEND") {
      return length === 0 && sawIdat && chunkEnd === bytes.length;
    }
    offset = chunkEnd;
    chunkIndex += 1;
  }
  return false;
}


async function readImageBytes(url) {
  let bytes;
  if (typeof url !== "string") throw blocked("RESULT_SCHEMA_INVALID");
  if (url.startsWith("data:")) {
    const match = /^data:image\/png;base64,([A-Za-z0-9+/]+={0,2})$/.exec(url);
    if (!match) throw blocked("RESULT_SCHEMA_INVALID");
    bytes = Buffer.from(match[1], "base64");
  } else if (url.startsWith("https://")) {
    let response;
    try {
      response = await fetch(url, { redirect: "follow" });
    } catch {
      throw blocked("RESULT_DOWNLOAD_FAILED");
    }
    if (!response.ok) throw blocked("RESULT_DOWNLOAD_FAILED");
    bytes = Buffer.from(await response.arrayBuffer());
  } else {
    throw blocked("RESULT_URL_NOT_ALLOWED");
  }
  if (!isPng(bytes)) throw blocked("RESULT_NOT_PNG");
  return bytes;
}


async function retrieveResult(client, request, workspace, run, runManifestPath) {
  let result;
  try {
    result = await client.queue.result(request.model, { requestId: request.request_id });
  } catch (error) {
    if (Number(error?.status) === 422) {
      request.status = "FAILED";
      const category = safeErrorType(error, "RESULT_VALIDATION_ERROR");
      if (category !== null) request.safe_error_type = category;
      else delete request.safe_error_type;
      run.state = "DRAFT";
      delete run.blocked_reason;
      persistRun(runManifestPath, run);
      return {
        state: "DRAFT",
        reason: "REQUEST_FAILED",
        ...summary(run),
      };
    }
    throw blocked("RESULT_RETRIEVAL_FAILED");
  }
  const url = result?.data?.images?.[0]?.url;
  const bytes = await readImageBytes(url);
  const relative = [
    "raw",
    `frame-${String(request.frame_index).padStart(2, "0")}`,
    `attempt-${String(request.attempt).padStart(2, "0")}-${modelSlug(request.model)}.png`,
  ].join("/");
  const destination = path.resolve(workspace, relative);
  const frameDirectory = path.join(workspace, "raw", `frame-${String(request.frame_index).padStart(2, "0")}`);
  validateOutputDestination(workspace, destination);
  fs.mkdirSync(frameDirectory, { recursive: true });
  validateOutputDestination(workspace, destination);
  const temporary = `${destination}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(temporary, bytes);
  fs.renameSync(temporary, destination);
  request.status = "COMPLETED";
  request.result_path = relative;
  request.result_sha256 = createHash("sha256").update(bytes).digest("hex");
  delete request.safe_error_type;
  run.state = "DRAFT";
  delete run.blocked_reason;
  persistRun(runManifestPath, run);
  return {
    state: "DRAFT",
    reason: "RESULT_SAVED",
    ...summary(run),
    result_path: relative,
  };
}


async function queryExisting(client, request, workspace, run, runManifestPath) {
  if (request.status === "COMPLETED" && !request.result_path) {
    return retrieveResult(client, request, workspace, run, runManifestPath);
  }
  let response;
  try {
    response = await client.queue.status(request.model, {
      requestId: request.request_id,
      logs: false,
    });
  } catch {
    throw blocked("REQUEST_STATUS_UNKNOWN");
  }
  request.status = normalizeStatus(response);
  run.state = "DRAFT";
  delete run.blocked_reason;
  persistRun(runManifestPath, run);
  if (request.status === "COMPLETED") {
    return retrieveResult(client, request, workspace, run, runManifestPath);
  }
  if (TERMINAL_STATUSES.has(request.status)) {
    return {
      state: "DRAFT",
      reason: `REQUEST_${request.status}`,
      ...summary(run),
    };
  }
  return {
    state: "DRAFT",
    reason: "REQUEST_PENDING",
    ...summary(run),
  };
}


async function queryWithSafeBlock(client, request, workspace, run, runManifestPath) {
  try {
    return await queryExisting(client, request, workspace, run, runManifestPath);
  } catch (error) {
    if (typeof error?.reason === "string") {
      return blockedPayload(error.reason, run);
    }
    throw error;
  }
}


function unknownSubmissionRecord(run) {
  return run.requests.find((request) => request.request_id === null) || null;
}


async function execute(args) {
  const manifest = validateManifest(readJson(args.manifest, "INVALID_MANIFEST"));
  const projectRoot = preparedProjectRoot(args, manifest);
  const runManifestPath = path.resolve(args["run-manifest"]);
  const run = readJson(runManifestPath, "WORKSPACE_CONFINEMENT_FAILED");
  const workspace = validatePreparedRun(manifest, runManifestPath, run, projectRoot);
  const unknown = unknownSubmissionRecord(run);
  if (unknown) throw blocked("SUBMISSION_STATUS_UNKNOWN");
  if (TERMINAL_RUN_STATES.has(run.state)) {
    throw blocked("RUN_TERMINAL");
  }
  const currentFrame = firstUnacceptedFrame(run, manifest.frame_count);
  if (completedResultPendingEvaluation(run)) {
    throw blocked("EVALUATION_REQUIRED");
  }
  if (typeof process.env.FAL_KEY !== "string" || process.env.FAL_KEY.trim() === "") {
    throw blocked("FAL_KEY_MISSING");
  }

  const pending = unresolvedRequest(run);
  if (pending) {
    if (
      currentFrame === null
      || pending.frame_index !== currentFrame
      || !isObject(run.next_request)
      || pending.request_fingerprint !== run.next_request.fingerprint
      || pending.frame_index !== run.next_request.frame_index
      || pending.model !== run.next_request.model
    ) {
      throw blocked("STALE_REQUEST_INPUT");
    }
    const client = await importClient(args["client-module"]);
    return queryWithSafeBlock(client, pending, workspace, run, runManifestPath);
  }

  const pricing = readJson(args.pricing, "PRICE_NOT_VERIFIABLE");
  const prices = verifyPricing(pricing, manifest);
  const requestInput = validateRequestInput(
    readJson(args["request-input"], "INVALID_REQUEST_INPUT"),
    manifest,
  );
  const counts = modelCounts(run, manifest);
  const worstCase = remainingWorstCase(run, manifest, prices);
  const budget = Math.min(MAX_BUDGET_USD, manifest.max_budget_usd);
  if (worstCase > budget + 1e-12) {
    throw blocked("BUDGET_WORST_CASE_EXCEEDED");
  }

  if (
    counts.primary > Math.min(MAX_PRIMARY_SUBMITS, manifest.max_primary_submits)
    || counts.fallback > Math.min(MAX_FALLBACK_SUBMITS, manifest.max_fallback_submits)
    || (requestInput.model === manifest.primary_model && counts.primary >= manifest.max_primary_submits)
    || (requestInput.model === manifest.fallback_model && counts.fallback >= manifest.max_fallback_submits)
  ) {
    throw blocked("ATTEMPT_LIMIT_REACHED");
  }
  validateCurrentRequest(requestInput, run, manifest);

  const client = await importClient(args["client-module"]);
  const estimate = requestInput.model === manifest.primary_model
    ? prices.primary
    : prices.fallback05k;
  const attempt = run.requests.length + 1;
  const baseRecord = {
    frame_index: requestInput.frame_index,
    attempt,
    model: requestInput.model,
    request_id: null,
    estimated_cost_usd: estimate,
    status: "SUBMISSION_STATUS_UNKNOWN",
    submitted_at: new Date().toISOString(),
    result_path: null,
    request_fingerprint: requestInput.request_fingerprint,
  };
  let submission;
  try {
    submission = await client.queue.submit(requestInput.model, {
      input: requestInput.input,
    });
  } catch (error) {
    const category = safeErrorType(error, "SubmitError");
    if (category !== null) baseRecord.safe_error_type = category;
    run.requests.push(baseRecord);
    run.state = "BLOCKED";
    run.blocked_reason = "SUBMISSION_STATUS_UNKNOWN";
    persistRun(runManifestPath, run);
    return blockedPayload("SUBMISSION_STATUS_UNKNOWN", run);
  }
  const requestId = submission?.request_id;
  if (typeof requestId !== "string" || requestId.trim() === "") {
    const category = safeErrorType(null, "InvalidSubmissionResponse");
    if (category !== null) baseRecord.safe_error_type = category;
    run.requests.push(baseRecord);
    run.state = "BLOCKED";
    run.blocked_reason = "SUBMISSION_STATUS_UNKNOWN";
    persistRun(runManifestPath, run);
    return blockedPayload("SUBMISSION_STATUS_UNKNOWN", run);
  }
  const record = {
    ...baseRecord,
    request_id: requestId,
    status: "IN_QUEUE",
  };
  run.requests.push(record);
  run.state = "DRAFT";
  delete run.blocked_reason;
  persistRun(runManifestPath, run);
  return queryWithSafeBlock(client, record, workspace, run, runManifestPath);
}


async function main() {
  let run = null;
  try {
    const args = parseArgs(process.argv.slice(2));
    try {
      run = readJson(args["run-manifest"], "WORKSPACE_CONFINEMENT_FAILED");
    } catch {
      run = null;
    }
    const payload = await execute(args);
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return payload.state === "BLOCKED" ? 2 : 0;
  } catch (error) {
    const reason = typeof error?.reason === "string" ? error.reason : "INTERNAL_ERROR";
    const payload = blockedPayload(reason, isObject(run) && Array.isArray(run.requests) ? run : null);
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return 2;
  }
}


process.exitCode = await main();
