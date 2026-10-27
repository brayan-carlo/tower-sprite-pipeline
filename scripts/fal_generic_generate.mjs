import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";


const MODEL = "fal-ai/nano-banana-2/edit";
const SUBMIT_URL = `https://queue.fal.run/${MODEL}`;
const PRICING_URL = "https://api.fal.ai/v1/models/pricing";
const PUBLIC_PRICING_URL = "https://fal.ai/models/fal-ai/nano-banana-2/edit";
const PRICING_FETCH_TIMEOUT_MS = 15 * 1000;
const MAX_PRICING_RESPONSE_BYTES = 5 * 1024 * 1024;
const MAX_PRICE_AGE_MS = 15 * 60 * 1000;
const MAX_FUTURE_SKEW_MS = 60 * 1000;
const MAX_RESULT_BYTES = 50 * 1024 * 1024;
const MAX_REQUEST_ID_LENGTH = 128;
const ATOMIC_BRIDGE_PROTOCOL = "tower-sprite-atomic-v1";
const SCRIPT_ROOT = path.dirname(fileURLToPath(import.meta.url));
const PYTHON_ATOMIC_BRIDGE = String.raw`
import base64
import ctypes
import hashlib
import inspect
import json
import msvcrt
import os
import sys
import time
import uuid
from pathlib import Path

def fail():
    raise RuntimeError("ATOMIC_BRIDGE_FAILED")

def digest_json(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def identity_values(identity):
    return [identity.volume_serial, identity.file_index, identity.attributes]

def signal_hook(operation, temporary, identity):
    signal = os.environ.get("TOWER_SPRITE_ATOMIC_TEST_SIGNAL")
    selected = os.environ.get("TOWER_SPRITE_ATOMIC_TEST_OPERATION")
    if not signal or (selected and selected != operation):
        return
    signal_path = Path(signal)
    signal_path.write_text(json.dumps({
        "operation": operation,
        "temporary": str(temporary),
        "identity": identity_values(identity),
        "fal_key_present": "FAL_KEY" in os.environ,
        "argv": sys.argv,
    }), encoding="utf-8")
    acknowledgement = Path(str(signal_path) + ".ack")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if acknowledgement.is_file():
            return
        time.sleep(0.01)
    fail()

def signal_published(operation, target, identity):
    signal = os.environ.get("TOWER_SPRITE_ATOMIC_TEST_SIGNAL")
    selected = os.environ.get("TOWER_SPRITE_ATOMIC_TEST_OPERATION")
    if not signal or (selected and selected != operation):
        return
    done = Path(str(signal) + ".done")
    done.write_text(json.dumps({
        "operation": operation,
        "target": str(target),
        "identity": identity_values(identity),
    }), encoding="utf-8")
    acknowledgement = Path(str(done) + ".ack")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if acknowledgement.is_file():
            return
        time.sleep(0.01)
    fail()

def create_windows_file(path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000 | 0x40000000 | 0x00010000,
        0x00000001,
        None,
        1,
        0x00000080,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        fail()
    return handle

def publish_new(gp, target, payload):
    if os.name != "nt":
        fail()
    parent = gp._safe_chain(target.parent)
    with gp._PathGuard(parent, directory=True) as parent_guard:
        if os.path.lexists(target):
            fail()
        temporary = parent / ("." + target.name + "." + str(uuid.uuid4()) + ".partial")
        raw_handle = create_windows_file(temporary)
        descriptor = None
        try:
            descriptor = msvcrt.open_osfhandle(raw_handle, os.O_RDWR)
            raw_handle = None
            with os.fdopen(descriptor, "w+b", buffering=0) as stream:
                descriptor = None
                stream.write(payload)
                os.fsync(stream.fileno())
                handle = msvcrt.get_osfhandle(stream.fileno())
                opened_identity = gp._windows_handle_identity(handle)
                if gp._read_windows_handle_bytes(handle) != payload:
                    fail()
                signal_hook("new", temporary, opened_identity)
                parent_guard.verify()
                if os.path.lexists(target):
                    fail()
                if gp._windows_handle_identity(handle) != opened_identity:
                    fail()
                if gp._read_windows_handle_bytes(handle) != payload:
                    fail()
                gp._rename_windows_handle(handle, target, replace_if_exists=False)
                parent_guard.verify()
                if gp._windows_handle_identity(handle) != opened_identity:
                    fail()
                if gp._read_windows_handle_bytes(handle) != payload:
                    fail()
                snapshot = gp._file_snapshot(target, payload)
                if snapshot.size != len(payload):
                    fail()
                if gp._windows_path_identity(target, False) != opened_identity:
                    fail()
                signal_published("new", target, opened_identity)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if raw_handle is not None:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                    ctypes.c_void_p(raw_handle)
                )
            if os.path.lexists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

def main():
    sys.path.insert(0, os.getcwd())
    import generic_sprite_pipeline as gp
    if Path(gp.__file__).resolve().parent != Path.cwd().resolve():
        fail()
    signatures = {
        "_rewrite_existing_json": ("path", "value", "expected"),
        "_PathGuard": (
            "path", "directory", "writable_rename", "allow_delete_share",
            "expected_snapshot",
        ),
        "_file_snapshot": ("path", "data"),
        "_rename_windows_handle": ("handle", "destination", "replace_if_exists"),
        "_safe_chain": ("path",),
        "_windows_handle_identity": ("handle",),
        "_read_windows_handle_bytes": ("handle",),
        "_windows_path_identity": ("path", "directory"),
    }
    for name, parameters in signatures.items():
        candidate = getattr(gp, name, None)
        if not callable(candidate):
            fail()
        if tuple(inspect.signature(candidate).parameters) != parameters:
            fail()
    request = json.loads(sys.stdin.buffer.read())
    protocol = "tower-sprite-atomic-v1"
    if request.get("protocol") != protocol:
        fail()
    operation = request.get("operation")
    envelope = {
        "protocol": request.get("protocol"),
        "operation": operation,
        "target": request.get("target"),
        "payload_sha256": request.get("payload_sha256"),
        "expected_sha256": request.get("expected_sha256"),
    }
    if request.get("operation_sha256") != digest_json(envelope):
        fail()
    if operation == "probe":
        if set(request) != {"protocol", "operation", "operation_sha256"}:
            fail()
        return protocol
    if set(request) != {
        "protocol", "operation", "operation_sha256", "target", "payload_b64",
        "payload_sha256", "expected_b64", "expected_sha256", "value"
    }:
        fail()
    target = Path(request["target"])
    if not target.is_absolute():
        fail()
    payload = base64.b64decode(request["payload_b64"], validate=True)
    if hashlib.sha256(payload).hexdigest() != request["payload_sha256"]:
        fail()
    if operation == "replace":
        expected = base64.b64decode(request["expected_b64"], validate=True)
        if hashlib.sha256(expected).hexdigest() != request["expected_sha256"]:
            fail()
        serialized = (
            json.dumps(request["value"], ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        if serialized != payload:
            fail()
        original = gp._replace_existing_file
        if os.environ.get("TOWER_SPRITE_ATOMIC_TEST_SIGNAL"):
            def hooked(replacement_guard, destination):
                target_identity = gp._windows_path_identity(destination, False)
                signal_hook("replace", replacement_guard.path, replacement_guard.handle_identity)
                if gp._windows_path_identity(destination, False) != target_identity:
                    fail()
                result = original(replacement_guard, destination)
                if gp._windows_path_identity(destination, False) != replacement_guard.handle_identity:
                    fail()
                signal_published("replace", destination, replacement_guard.handle_identity)
                return result
            gp._replace_existing_file = hooked
        gp._rewrite_existing_json(target, request["value"], expected)
    elif operation == "new":
        if (
            request["expected_b64"] is not None
            or request["expected_sha256"] is not None
            or request["value"] is not None
        ):
            fail()
        publish_new(gp, target, payload)
    else:
        fail()
    return protocol

try:
    result = main()
except BaseException:
    sys.exit(2)
sys.stdout.write("OK:" + result)
`;
const PYTHON_EVALUATION_VALIDATOR = String.raw`
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import generic_sprite_pipeline as gp

if Path(gp.__file__).resolve().parent != Path.cwd().resolve():
    sys.exit(2)
try:
    gp._validate_transport_evaluation_state(Path(sys.argv[1]))
except BaseException:
    sys.exit(2)
sys.stdout.write("OK")
`;
const TERMINAL_RUN_STATES = new Set([
  "BLOCKED",
  "TECHNICAL_REJECTED",
  "READY_FOR_VISUAL_REVIEW",
  "APPROVED",
  "APPROVED_WITH_CORRECTIONS",
  "REJECTED",
]);
const ACTIVE_REQUEST_STATUSES = new Set(["IN_QUEUE", "IN_PROGRESS"]);
const REMOTE_TERMINAL_STATUSES = new Set(["FAILED", "CANCELLED"]);
const KNOWN_REQUEST_STATUSES = new Set([
  "SUBMITTING",
  "SUBMISSION_STATUS_UNKNOWN",
  "IN_QUEUE",
  "IN_PROGRESS",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "REJECTED",
]);
const DETERMINISTIC_RESULT_FAILURES = new Set([
  "RAW_PROVENANCE_MISMATCH",
  "RESULT_DESTINATION_EXISTS",
  "RESULT_NOT_PNG",
  "RESULT_SCHEMA_INVALID",
  "RESULT_TOO_LARGE",
  "RESULT_URL_NOT_ALLOWED",
  "WORKSPACE_CONFINEMENT_FAILED",
]);
const SAFE_ERROR_TYPES = new Set([
  "AbortError",
  "FalError",
  "InvalidSubmissionResponse",
  "NetworkError",
  "RESULT_VALIDATION_ERROR",
  "RAW_PROVENANCE_MISMATCH",
  "RESULT_DESTINATION_EXISTS",
  "RESULT_NOT_PNG",
  "RESULT_SCHEMA_INVALID",
  "RESULT_TOO_LARGE",
  "RESULT_URL_NOT_ALLOWED",
  "SubmitError",
  "TimeoutError",
  "WORKSPACE_CONFINEMENT_FAILED",
  "image_too_small",
]);
const PUBLIC_REQUEST_KEYS = [
  "frame_index",
  "instructions",
  "model",
  "references",
  "request_fingerprint",
];
const INSTRUCTION_KEYS = [
  "animation",
  "background",
  "body_motion",
  "direction",
  "framing",
  "identity",
  "phase",
];
const RUN_KEYS = [
  "artifacts",
  "frames",
  "manifest_path",
  "manifest_sha256",
  "next_request",
  "requests",
  "schema_version",
  "state",
  "terminal_reason",
];
const EVALUATION_FRAME_KEYS = [
  "frame_index",
  "normalized_path",
  "normalized_sha256",
  "raw_path",
  "raw_sha256",
  "report_path",
  "report_sha256",
  "status",
];
const EVALUATION_CLAIM_KEYS = [
  "failure",
  "frame_index",
  "normalized_path",
  "normalized_sha256",
  "raw_path",
  "raw_sha256",
  "report_path",
  "report_sha256",
  "status",
  "technical_result",
];
const EVALUATION_REPORT_KEYS = [
  "advisory",
  "failures",
  "frame_index",
  "normalization",
  "provenance",
  "schema_version",
  "technical_result",
];
const EVALUATION_PROVENANCE_KEYS = [
  "normalized_path",
  "normalized_sha256",
  "raw_path",
  "raw_sha256",
];
const EVALUATION_CONTENT_FAILURES = new Set([
  "FRAME_EMPTY",
  "FRAME_CLIPPED",
  "FRAME_DUPLICATE",
  "FRAME_NOT_PNG",
]);
const MANIFEST_KEYS = [
  "authorization",
  "mode",
  "movement_plan",
  "output",
  "provider",
  "qa",
  "request",
  "schema_version",
  "source",
];
const PRICING_EVIDENCE_KEYS = [
  "base_unit_price_usd",
  "currency",
  "model",
  "resolution",
  "resolution_multiplier",
  "source_url",
  "unit",
  "verified_at",
];
let reportingContext = null;


function blocked(reason) {
  const error = new Error(reason);
  error.reason = reason;
  return error;
}


function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}


function exactKeys(value, expected) {
  return isObject(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort());
}


function isWithin(candidate, root, allowEqual = false) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  if (relative === "") return allowEqual;
  return !relative.startsWith("..") && !path.isAbsolute(relative);
}


function samePath(left, right) {
  return path.relative(path.resolve(left), path.resolve(right)) === "";
}


function hasAlternateDataStream(candidate) {
  if (typeof candidate !== "string" || candidate.includes("\0")) return true;
  const resolved = path.resolve(candidate);
  return resolved.slice(path.parse(resolved).root.length).includes(":");
}


function rejectAlternateDataStream(candidate) {
  if (hasAlternateDataStream(candidate)) throw blocked("INVALID_ARGUMENTS");
}


function requireDirectory(candidate, reason, root = null) {
  const resolved = path.resolve(candidate);
  let stats;
  let canonical;
  try {
    stats = fs.lstatSync(resolved, { bigint: true, throwIfNoEntry: false });
    if (!stats || stats.isSymbolicLink() || !stats.isDirectory()) throw new Error();
    canonical = fs.realpathSync.native(resolved);
  } catch {
    throw blocked(reason);
  }
  if (!samePath(canonical, resolved)) throw blocked(reason);
  if (root !== null && !isWithin(resolved, root, true)) throw blocked(reason);
  return resolved;
}


function validateDirectoryChain(root, target, reason) {
  const rootPath = requireDirectory(root, reason);
  const targetPath = path.resolve(target);
  if (!isWithin(targetPath, rootPath, true)) throw blocked(reason);
  const relative = path.relative(rootPath, targetPath);
  let current = rootPath;
  for (const component of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    const stats = fs.lstatSync(current, { bigint: true, throwIfNoEntry: false });
    if (!stats) break;
    requireDirectory(current, reason, rootPath);
  }
  return rootPath;
}


function sameIdentity(left, right) {
  return left.dev === right.dev
    && left.ino === right.ino
    && left.mode === right.mode
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs;
}


function stableBytes(candidate, reason, root = null, allowMultipleLinks = false) {
  const resolved = path.resolve(candidate);
  if (root !== null) {
    validateDirectoryChain(root, path.dirname(resolved), reason);
    if (!isWithin(resolved, root)) throw blocked(reason);
  }
  let before;
  let opened;
  let after;
  let bytes;
  let descriptor;
  try {
    before = fs.lstatSync(resolved, { bigint: true });
    if (
      before.isSymbolicLink()
      || !before.isFile()
      || (!allowMultipleLinks && before.nlink !== 1n)
      || !samePath(fs.realpathSync.native(resolved), resolved)
    ) {
      throw new Error();
    }
    descriptor = fs.openSync(resolved, "r");
    opened = fs.fstatSync(descriptor, { bigint: true });
    bytes = fs.readFileSync(descriptor);
    after = fs.lstatSync(resolved, { bigint: true });
  } catch {
    throw blocked(reason);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
  if (
    !sameIdentity(before, opened)
    || !sameIdentity(opened, after)
    || BigInt(bytes.length) !== after.size
  ) {
    throw blocked(reason);
  }
  return { resolved, bytes, stats: after };
}


function readJsonStable(candidate, reason, root = null) {
  const record = stableBytes(candidate, reason, root);
  let value;
  try {
    value = JSON.parse(record.bytes.toString("utf8"));
  } catch {
    throw blocked(reason);
  }
  return { ...record, value };
}


function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (isObject(value)) {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalValue(value[key])]),
    );
  }
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw blocked("INVALID_JSON_NUMBER");
  }
  return value;
}


function canonicalSha256(value) {
  return createHash("sha256")
    .update(JSON.stringify(canonicalValue(value)), "utf8")
    .digest("hex");
}


function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}


function jsonBytes(value) {
  return Buffer.from(`${JSON.stringify(canonicalValue(value), null, 2)}\n`, "utf8");
}


function atomicProtocol() {
  if (
    process.env.NODE_ENV === "test"
    && typeof process.env.FAKE_ROOT === "string"
    && typeof process.env.TOWER_SPRITE_TEST_ATOMIC_PROTOCOL === "string"
  ) {
    return process.env.TOWER_SPRITE_TEST_ATOMIC_PROTOCOL;
  }
  return ATOMIC_BRIDGE_PROTOCOL;
}


function atomicRuntime() {
  const value = process.env.TOWER_SPRITE_PYTHON_EXE;
  if (
    typeof value !== "string"
    || value.trim() === ""
    || !path.isAbsolute(value)
    || path.basename(value).toLowerCase() !== "python.exe"
  ) {
    throw blocked("ATOMIC_RUNTIME_INVALID");
  }
  rejectAlternateDataStream(value);
  return stableBytes(value, "ATOMIC_RUNTIME_INVALID").resolved;
}


let atomicHookConsumed = false;


function atomicChildEnvironment(operation) {
  const environment = {
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
  };
  for (const name of ["SystemRoot", "WINDIR"]) {
    if (typeof process.env[name] === "string") environment[name] = process.env[name];
  }
  const signalValue = process.env.TOWER_SPRITE_ATOMIC_TEST_SIGNAL;
  if (signalValue !== undefined && !atomicHookConsumed) {
    if (
      process.env.NODE_ENV !== "test"
      || typeof process.env.FAKE_ROOT !== "string"
    ) {
      throw blocked("ATOMIC_RUNTIME_INVALID");
    }
    const fakeRoot = requireDirectory(process.env.FAKE_ROOT, "ATOMIC_RUNTIME_INVALID");
    rejectAlternateDataStream(signalValue);
    const signal = path.resolve(signalValue);
    if (!isWithin(signal, fakeRoot)) throw blocked("ATOMIC_RUNTIME_INVALID");
    validateDirectoryChain(fakeRoot, path.dirname(signal), "ATOMIC_RUNTIME_INVALID");
    environment.TOWER_SPRITE_ATOMIC_TEST_SIGNAL = signal;
    const selected = process.env.TOWER_SPRITE_ATOMIC_TEST_OPERATION;
    if (!new Set(["new", "replace"]).has(selected)) {
      throw blocked("ATOMIC_RUNTIME_INVALID");
    }
    if (selected === operation) {
      environment.TOWER_SPRITE_ATOMIC_TEST_OPERATION = selected;
    } else {
      delete environment.TOWER_SPRITE_ATOMIC_TEST_SIGNAL;
    }
  }
  return environment;
}


function invokeAtomicBridge(request, reason) {
  const runtime = atomicRuntime();
  const protocol = atomicProtocol();
  const payloadSha256 = typeof request.payload_b64 === "string"
    ? sha256(Buffer.from(request.payload_b64, "base64"))
    : null;
  const expectedSha256 = typeof request.expected_b64 === "string"
    ? sha256(Buffer.from(request.expected_b64, "base64"))
    : null;
  const envelope = {
    protocol,
    operation: request.operation,
    target: request.target ?? null,
    payload_sha256: payloadSha256,
    expected_sha256: expectedSha256,
  };
  const bridgeRequest = {
    ...request,
    protocol,
    operation_sha256: canonicalSha256(envelope),
  };
  if (request.operation !== "probe") {
    bridgeRequest.payload_sha256 = payloadSha256;
    bridgeRequest.expected_sha256 = expectedSha256;
  }
  const completed = spawnSync(
    runtime,
    ["-I", "-B", "-c", PYTHON_ATOMIC_BRIDGE],
    {
      cwd: SCRIPT_ROOT,
      env: atomicChildEnvironment(request.operation),
      input: JSON.stringify(bridgeRequest),
      encoding: "utf8",
      windowsHide: true,
      maxBuffer: 1024 * 1024,
      shell: false,
    },
  );
  const expectedOutput = `OK:${ATOMIC_BRIDGE_PROTOCOL}`;
  if (
    completed.error
    || completed.status !== 0
    || completed.signal !== null
    || completed.stdout !== expectedOutput
  ) {
    throw blocked(reason);
  }
  if (
    typeof process.env.TOWER_SPRITE_ATOMIC_TEST_SIGNAL === "string"
    && process.env.TOWER_SPRITE_ATOMIC_TEST_OPERATION === request.operation
  ) {
    atomicHookConsumed = true;
  }
}


let atomicBridgeReady = false;


function probeAtomicBridge() {
  if (atomicBridgeReady) return;
  invokeAtomicBridge({ operation: "probe" }, "ATOMIC_RUNTIME_INVALID");
  atomicBridgeReady = true;
}


function validateEvaluationSemantics(context) {
  const currentFrame = context.run.frames.find(
    (frame) => frame.frame_index === context.run.next_request,
  );
  const currentRequest = context.run.requests.find(
    (record) => record.frame_index === context.run.next_request,
  );
  const hasAccepted = context.run.frames.some((frame) => frame.status === "ACCEPTED");
  const hasEvaluatorRejection = currentFrame?.status === "REJECTED"
    && currentRequest?.status === "COMPLETED";
  if (!hasAccepted && !hasEvaluatorRejection) return;
  const completed = spawnSync(
    atomicRuntime(),
    ["-I", "-B", "-c", PYTHON_EVALUATION_VALIDATOR, context.runManifestPath],
    {
      cwd: SCRIPT_ROOT,
      env: atomicChildEnvironment("probe"),
      encoding: "utf8",
      windowsHide: true,
      maxBuffer: 1024 * 1024,
      shell: false,
    },
  );
  if (
    completed.error
    || completed.status !== 0
    || completed.signal !== null
    || completed.stdout !== "OK"
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
}


function writeNewAtomic(destination, bytes, reason) {
  const resolved = path.resolve(destination);
  requireDirectory(path.dirname(resolved), reason);
  if (fs.lstatSync(resolved, { throwIfNoEntry: false })) throw blocked(reason);
  probeAtomicBridge();
  invokeAtomicBridge(
    {
      operation: "new",
      target: resolved,
      payload_b64: bytes.toString("base64"),
      expected_b64: null,
      value: null,
    },
    reason,
  );
  const final = stableBytes(resolved, reason, path.dirname(resolved));
  if (!final.bytes.equals(bytes)) throw blocked(reason);
}


function replaceJsonAtomic(destination, value, expectedBytes) {
  const resolved = path.resolve(destination);
  const current = stableBytes(resolved, "RUN_MANIFEST_CHANGED", path.dirname(resolved));
  if (!current.bytes.equals(expectedBytes)) throw blocked("RUN_MANIFEST_CHANGED");
  const payload = jsonBytes(value);
  probeAtomicBridge();
  invokeAtomicBridge(
    {
      operation: "replace",
      target: resolved,
      payload_b64: payload.toString("base64"),
      expected_b64: expectedBytes.toString("base64"),
      value: canonicalValue(value),
    },
    "RUN_MANIFEST_CHANGED",
  );
  const published = stableBytes(
    resolved,
    "RUN_MANIFEST_CHANGED",
    path.dirname(resolved),
  );
  if (!published.bytes.equals(payload)) throw blocked("RUN_MANIFEST_CHANGED");
  return payload;
}


function decimal(value, reason = "PRICE_NOT_VERIFIABLE") {
  if (typeof value !== "string" && typeof value !== "number") throw blocked(reason);
  const text = String(value);
  const match = /^([0-9]+)(?:\.([0-9]+))?$/.exec(text);
  if (!match) throw blocked(reason);
  let integer = match[1].replace(/^0+(?=\d)/, "");
  let fraction = match[2] || "";
  fraction = fraction.replace(/0+$/, "");
  const digits = `${integer}${fraction}`.replace(/^0+(?=\d)/, "");
  const coefficient = BigInt(digits || "0");
  if (coefficient <= 0n) throw blocked(reason);
  return { coefficient, scale: fraction.length, text };
}


function alignDecimals(left, right) {
  const scale = Math.max(left.scale, right.scale);
  return [
    left.coefficient * 10n ** BigInt(scale - left.scale),
    right.coefficient * 10n ** BigInt(scale - right.scale),
  ];
}


function decimalEqual(left, right) {
  const [a, b] = alignDecimals(left, right);
  return a === b;
}


function decimalGreater(left, right) {
  const [a, b] = alignDecimals(left, right);
  return a > b;
}


function multiplyDecimal(left, right) {
  return {
    coefficient: left.coefficient * right.coefficient,
    scale: left.scale + right.scale,
    text: "",
  };
}


function multiplyInteger(value, count) {
  return { coefficient: value.coefficient * BigInt(count), scale: value.scale, text: "" };
}


function validateTimestamp(
  value,
  now = Date.now(),
  reason = "PRICE_STALE",
  enforceFreshness = true,
) {
  const match = typeof value === "string"
    ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(value)
    : null;
  if (match === null) {
    throw blocked(reason);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const monthDays = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    year === 0
    || month < 1
    || month > 12
    || day < 1
    || day > monthDays[month - 1]
    || hour > 23
    || minute > 59
    || second > 59
    || offsetHour > 23
    || offsetMinute > 59
  ) {
    throw blocked(reason);
  }
  const verified = Date.parse(value);
  if (!Number.isFinite(verified)) throw blocked(reason);
  if (!enforceFreshness) return verified;
  const age = now - verified;
  if (age > MAX_PRICE_AGE_MS || age < -MAX_FUTURE_SKEW_MS) {
    throw blocked(reason);
  }
  return verified;
}


function sanitizeOfficialPricing(raw, evidenceMode = null, now = Date.now()) {
  if (
    !isObject(raw)
    || raw.source_url !== PRICING_URL
    || typeof raw.verified_at !== "string"
    || !Array.isArray(raw.prices)
  ) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  validateTimestamp(raw.verified_at, now);
  const seen = new Set();
  let selected = null;
  for (const item of raw.prices) {
    if (!isObject(item) || typeof item.endpoint_id !== "string") continue;
    if (seen.has(item.endpoint_id)) throw blocked("PRICE_NOT_VERIFIABLE");
    seen.add(item.endpoint_id);
    if (item.endpoint_id === MODEL) selected = item;
  }
  if (
    selected === null
    || selected.currency !== "USD"
    || selected.unit !== "image"
    || !isObject(selected.resolution_multipliers)
  ) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  const base = decimal(selected.unit_price);
  const multiplier = decimal(selected.resolution_multipliers["0.5K"]);
  const evidence = {
    model: MODEL,
    source_url: PRICING_URL,
    verified_at: raw.verified_at,
    currency: "USD",
    unit: "image",
    base_unit_price_usd: base.text,
    resolution: "0.5K",
    resolution_multiplier: multiplier.text,
  };
  if (evidenceMode !== null) evidence.evidence_mode = evidenceMode;
  return evidence;
}


function selectLivePricing(raw) {
  if (!isObject(raw) || !Array.isArray(raw.prices)) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  const seen = new Set();
  let selected = null;
  for (const item of raw.prices) {
    if (!isObject(item) || typeof item.endpoint_id !== "string") continue;
    if (seen.has(item.endpoint_id)) throw blocked("PRICE_NOT_VERIFIABLE");
    seen.add(item.endpoint_id);
    if (item.endpoint_id === MODEL) selected = item;
  }
  if (
    selected === null
    || selected.currency !== "USD"
    || !new Set(["image", "images"]).has(selected.unit)
  ) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  const base = decimal(selected.unit_price);
  let multiplier = null;
  if (Object.hasOwn(selected, "resolution_multipliers")) {
    if (!isObject(selected.resolution_multipliers)) {
      throw blocked("PRICE_NOT_VERIFIABLE");
    }
    if (Object.hasOwn(selected.resolution_multipliers, "0.5K")) {
      multiplier = decimal(selected.resolution_multipliers["0.5K"]);
    }
  }
  return { base, multiplier, selected };
}


function parseHtmlAttributes(source) {
  const attributes = new Map();
  let index = 0;
  while (index < source.length) {
    while (/\s/.test(source[index] || "")) index += 1;
    if (index >= source.length) break;
    const nameMatch = /^[^\s"'<>\/=]+/.exec(source.slice(index));
    if (nameMatch === null) throw blocked("PRICE_NOT_VERIFIABLE");
    const name = nameMatch[0].toLowerCase();
    if (attributes.has(name)) throw blocked("PRICE_NOT_VERIFIABLE");
    index += nameMatch[0].length;
    while (/\s/.test(source[index] || "")) index += 1;
    let value = null;
    if (source[index] === "=") {
      index += 1;
      while (/\s/.test(source[index] || "")) index += 1;
      const quote = source[index];
      if (quote === "\"" || quote === "'") {
        index += 1;
        const end = source.indexOf(quote, index);
        if (end < 0) throw blocked("PRICE_NOT_VERIFIABLE");
        value = source.slice(index, end);
        index = end + 1;
      } else {
        const valueMatch = /^[^\s"'=<>`]+/.exec(source.slice(index));
        if (valueMatch === null) throw blocked("PRICE_NOT_VERIFIABLE");
        value = valueMatch[0];
        index += value.length;
      }
    }
    attributes.set(name, value);
  }
  return attributes;
}


function explicitlyHidden(attributes) {
  const style = attributes.get("style");
  return (
    attributes.has("hidden")
    || attributes.get("aria-hidden")?.toLowerCase() === "true"
    || (
      style !== null
      && /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)/i.test(style)
    )
  );
}


function visiblePricingDocument(source) {
  const markup = source
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
  const voidElements = new Set([
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
  ]);
  const inertElements = new Set([
    "template", "textarea", "title", "xmp", "iframe", "noembed", "noframes",
  ]);
  const stack = [];
  const paragraphs = [];
  const canonicalUrls = [];
  let paragraph = null;
  const tokens = markup.match(/<[^>]*>|[^<]+/g) || [];
  for (const token of tokens) {
    if (!token.startsWith("<")) {
      if (paragraph !== null && !stack.at(-1)?.hidden) paragraph.text += token;
      continue;
    }
    if (/^<!doctype\b[^>]*>$/i.test(token) || /^<\?[^>]*>$/i.test(token)) continue;
    const closing = /^<\s*\/\s*([A-Za-z][A-Za-z0-9:-]*)\s*>$/.exec(token);
    if (closing !== null) {
      const tag = closing[1].toLowerCase();
      const frame = stack.at(-1);
      if (frame?.tag !== tag) throw blocked("PRICE_NOT_VERIFIABLE");
      if (paragraph !== null && !frame.hidden) paragraph.text += " ";
      stack.pop();
      if (frame.startsParagraph) {
        if (!frame.hidden) paragraphs.push(paragraph.text);
        paragraph = null;
      }
      continue;
    }
    const opening = /^<\s*([A-Za-z][A-Za-z0-9:-]*)\b([^>]*)>$/.exec(token);
    if (opening === null) throw blocked("PRICE_NOT_VERIFIABLE");
    const tag = opening[1].toLowerCase();
    let attributes = opening[2];
    const selfClosing = /\/\s*$/.test(attributes);
    if (selfClosing) attributes = attributes.replace(/\/\s*$/, "");
    if (selfClosing && !voidElements.has(tag)) throw blocked("PRICE_NOT_VERIFIABLE");
    const parsedAttributes = parseHtmlAttributes(attributes);
    const hidden = (
      Boolean(stack.at(-1)?.hidden)
      || inertElements.has(tag)
      || explicitlyHidden(parsedAttributes)
    );
    if (tag === "link" && !hidden) {
      const rel = parsedAttributes.get("rel");
      if (rel?.toLowerCase().split(/\s+/).includes("canonical")) {
        canonicalUrls.push(parsedAttributes.get("href") ?? null);
      }
    }
    const startsParagraph = tag === "p";
    if (startsParagraph) {
      if (paragraph !== null || selfClosing) throw blocked("PRICE_NOT_VERIFIABLE");
      paragraph = { text: "" };
    } else if (paragraph !== null && !hidden) {
      paragraph.text += " ";
    }
    if (!selfClosing && !voidElements.has(tag)) {
      stack.push({ tag, hidden, startsParagraph });
    }
  }
  if (stack.length !== 0 || paragraph !== null) throw blocked("PRICE_NOT_VERIFIABLE");
  if (canonicalUrls.length !== 1 || canonicalUrls[0] !== PUBLIC_PRICING_URL) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  const candidates = [];
  for (const paragraphText of paragraphs) {
    const text = paragraphText
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;|&#160;|&#xA0;/gi, " ")
      .replace(/&#36;|&#x24;|&dollar;/gi, "$")
      .replace(/\s+/g, " ")
      .trim();
    const baseMatches = [...text.matchAll(
      /Your request will cost\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*per image\b/gi,
    )];
    const multiplierMatches = [...text.matchAll(
      /0\.5K\s*\(512px\)\s*resolution outputs will be charged at\s*([0-9]+(?:\.[0-9]+)?)\s*times the standard rate\b/gi,
    )];
    if (baseMatches.length === 1 && multiplierMatches.length === 1) {
      candidates.push({
        base: decimal(baseMatches[0][1]),
        multiplier: decimal(multiplierMatches[0][1]),
      });
    } else if (baseMatches.length > 0 || multiplierMatches.length > 0) {
      throw blocked("PRICE_NOT_VERIFIABLE");
    }
  }
  if (candidates.length !== 1) throw blocked("PRICE_NOT_VERIFIABLE");
  return candidates[0];
}


async function readBoundedPricingBody(response) {
  const contentLength = response.headers?.get?.("content-length");
  if (contentLength !== null && contentLength !== undefined) {
    if (!/^[0-9]+$/.test(contentLength)) throw blocked("PRICE_FETCH_FAILED");
    if (BigInt(contentLength) > BigInt(MAX_PRICING_RESPONSE_BYTES)) {
      try { await response.body?.cancel(); } catch {}
      throw blocked("PRICE_FETCH_FAILED");
    }
  }
  const reader = response.body?.getReader?.();
  if (!reader) throw blocked("PRICE_FETCH_FAILED");
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw blocked("PRICE_FETCH_FAILED");
      size += value.byteLength;
      if (size > MAX_PRICING_RESPONSE_BYTES) {
        try { await reader.cancel(); } catch {}
        throw blocked("PRICE_FETCH_FAILED");
      }
      chunks.push(Buffer.from(value));
    }
  } catch (error) {
    try { await reader.cancel(); } catch {}
    if (error?.reason === "PRICE_FETCH_FAILED") throw error;
    throw blocked("PRICE_FETCH_FAILED");
  } finally {
    try { reader.releaseLock(); } catch {}
  }
  return Buffer.concat(chunks, size);
}


async function fetchPricingText(url, headers = {}) {
  let response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers,
      redirect: "error",
      signal: AbortSignal.timeout(PRICING_FETCH_TIMEOUT_MS),
    });
  } catch {
    throw blocked("PRICE_FETCH_FAILED");
  }
  if (
    !response.ok
    || (typeof response.url === "string" && response.url !== "" && response.url !== url)
  ) {
    throw blocked("PRICE_FETCH_FAILED");
  }
  let bytes;
  try {
    bytes = await readBoundedPricingBody(response);
  } catch {
    throw blocked("PRICE_FETCH_FAILED");
  }
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw blocked("PRICE_FETCH_FAILED");
  }
  return {
    text,
    retrievedAt: new Date().toISOString(),
    responseSha256: sha256(bytes),
  };
}


function parsePricingJson(source) {
  try {
    return JSON.parse(source, (_key, value, context) => (
      typeof value === "number" ? context.source : value
    ));
  } catch {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
}


function fixturePath(value) {
  rejectAlternateDataStream(value);
  if (process.env.NODE_ENV !== "test") throw blocked("INVALID_ARGUMENTS");
  const fakeRootValue = process.env.FAKE_ROOT;
  if (typeof fakeRootValue !== "string" || fakeRootValue.trim() === "") {
    throw blocked("INVALID_ARGUMENTS");
  }
  const fakeRoot = requireDirectory(fakeRootValue, "INVALID_ARGUMENTS");
  const candidate = path.resolve(value);
  if (!isWithin(candidate, fakeRoot)) throw blocked("INVALID_ARGUMENTS");
  return stableBytes(candidate, "INVALID_ARGUMENTS", fakeRoot);
}


async function pricingCommand(args) {
  let raw;
  let evidenceMode = null;
  if (args.source === PRICING_URL) {
    rejectAlternateDataStream(args.output);
    const output = path.resolve(args.output);
    const receiptPath = path.resolve(`${args.output}.provenance.json`);
    requireDirectory(path.dirname(output), "PRICING_OUTPUT_INVALID");
    requireDirectory(path.dirname(receiptPath), "PRICING_PROVENANCE_INVALID");
    if (fs.lstatSync(output, { throwIfNoEntry: false })) {
      throw blocked("PRICING_OUTPUT_INVALID");
    }
    if (fs.lstatSync(receiptPath, { throwIfNoEntry: false })) {
      throw blocked("PRICING_PROVENANCE_INVALID");
    }
    const credential = process.env.FAL_KEY;
    if (typeof credential !== "string" || credential.trim() === "") {
      throw blocked("FAL_KEY_MISSING");
    }
    const pricingRequestUrl = new URL(PRICING_URL);
    pricingRequestUrl.searchParams.set("endpoint_id", MODEL);
    const apiSource = await fetchPricingText(
      pricingRequestUrl.toString(),
      { Authorization: `Key ${credential}` },
    );
    const body = parsePricingJson(apiSource.text);
    const selected = selectLivePricing(body);
    let multiplier = selected.multiplier;
    const sources = [{
      source_url: pricingRequestUrl.toString(),
      retrieved_at: apiSource.retrievedAt,
      response_sha256: apiSource.responseSha256,
      selected: {
        model: MODEL,
        currency: "USD",
        unit: selected.selected.unit,
        normalized_unit: "image",
        base_unit_price_usd: selected.base.text,
        ...(multiplier === null ? {} : { resolution_multiplier: multiplier.text }),
      },
    }];
    if (multiplier === null) {
      const publicSource = await fetchPricingText(PUBLIC_PRICING_URL);
      const publicPricing = visiblePricingDocument(publicSource.text);
      if (!decimalEqual(selected.base, publicPricing.base)) {
        throw blocked("PRICE_NOT_VERIFIABLE");
      }
      multiplier = publicPricing.multiplier;
      sources.push({
        source_url: PUBLIC_PRICING_URL,
        retrieved_at: publicSource.retrievedAt,
        response_sha256: publicSource.responseSha256,
        selected: {
          base_unit_price_usd: publicPricing.base.text,
          resolution: "0.5K",
          resolution_multiplier: multiplier.text,
        },
      });
    }
    const evidence = {
      model: MODEL,
      source_url: PRICING_URL,
      verified_at: apiSource.retrievedAt,
      currency: "USD",
      unit: "image",
      base_unit_price_usd: selected.base.text,
      resolution: "0.5K",
      resolution_multiplier: multiplier.text,
    };
    const pricingBytes = jsonBytes(evidence);
    const receipt = {
      schema_version: 1,
      pricing_sha256: sha256(pricingBytes),
      sources,
    };
    const receiptBytes = jsonBytes(receipt);
    writeNewAtomic(receiptPath, receiptBytes, "PRICING_PROVENANCE_INVALID");
    writeNewAtomic(output, pricingBytes, "PRICING_OUTPUT_INVALID");
    return {
      state: "PRICING_VERIFIED",
      evidence,
      provenance: {
        path: receiptPath,
        sha256: sha256(receiptBytes),
      },
    };
  } else {
    const source = fixturePath(args.source);
    raw = parsePricingJson(source.bytes.toString("utf8"));
    evidenceMode = "OFFLINE_FIXTURE_NOT_FOR_LIVE_USE";
  }
  const evidence = sanitizeOfficialPricing(raw, evidenceMode);
  rejectAlternateDataStream(args.output);
  const output = path.resolve(args.output);
  if (evidenceMode !== null) {
    const fakeRoot = requireDirectory(process.env.FAKE_ROOT, "INVALID_ARGUMENTS");
    if (!isWithin(output, fakeRoot)) throw blocked("INVALID_ARGUMENTS");
  }
  writeNewAtomic(output, jsonBytes(evidence), "PRICING_OUTPUT_INVALID");
  return {
    state: evidenceMode === null ? "PRICING_VERIFIED" : "PRICING_FIXTURE_VALIDATED",
    evidence,
  };
}


function parseArgs(argv) {
  if (argv.length < 1) throw blocked("INVALID_ARGUMENTS");
  const command = argv[0];
  const definitions = {
    pricing: {
      allowed: new Set(["source", "output"]),
      required: ["source", "output"],
    },
    execute: {
      allowed: new Set(["run-manifest", "request-plan", "pricing", "client-module"]),
      required: ["run-manifest", "request-plan", "pricing"],
    },
  };
  const definition = definitions[command];
  if (!definition || (argv.length - 1) % 2 !== 0) throw blocked("INVALID_ARGUMENTS");
  const args = {};
  for (let index = 1; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    const name = typeof flag === "string" && flag.startsWith("--")
      ? flag.slice(2)
      : "";
    if (
      !definition.allowed.has(name)
      || Object.hasOwn(args, name)
      || typeof value !== "string"
      || value.trim() === ""
    ) {
      throw blocked("INVALID_ARGUMENTS");
    }
    args[name] = value;
  }
  if (definition.required.some((name) => !Object.hasOwn(args, name))) {
    throw blocked("INVALID_ARGUMENTS");
  }
  return { command, args };
}


function validateClientModule(args) {
  if (!Object.hasOwn(args, "client-module")) return null;
  rejectAlternateDataStream(args["client-module"]);
  if (process.env.NODE_ENV !== "test") throw blocked("INVALID_ARGUMENTS");
  const fakeRootValue = process.env.FAKE_ROOT;
  if (typeof fakeRootValue !== "string" || fakeRootValue.trim() === "") {
    throw blocked("INVALID_ARGUMENTS");
  }
  const fakeRoot = requireDirectory(fakeRootValue, "INVALID_ARGUMENTS");
  const modulePath = path.resolve(args["client-module"]);
  if (!isWithin(modulePath, fakeRoot)) throw blocked("INVALID_ARGUMENTS");
  return stableBytes(modulePath, "INVALID_ARGUMENTS", fakeRoot);
}


function validateManifest(manifest) {
  const schemaVersion = manifest?.schema_version;
  const providerKeys = schemaVersion === 1
    ? ["model", "name", "pricing"]
    : ["model", "name", "pricing", "prompt_template_version"];
  if (
    !exactKeys(manifest, MANIFEST_KEYS)
    || !Number.isInteger(schemaVersion)
    || !new Set([1, 2]).has(schemaVersion)
    || manifest.mode !== "generic_sequential"
    || !isObject(manifest.source)
    || !isObject(manifest.request)
    || !isObject(manifest.output)
    || !Array.isArray(manifest.movement_plan)
    || !isObject(manifest.provider)
    || !exactKeys(manifest.provider, providerKeys)
    || manifest.provider.name !== "fal.ai"
    || manifest.provider.model !== MODEL
    || (schemaVersion === 2 && manifest.provider.prompt_template_version !== 2)
    || !isObject(manifest.authorization)
    || !isObject(manifest.qa)
  ) {
    throw blocked("MANIFEST_INVALID");
  }
  const frameCount = manifest.request.frame_count;
  if (
    !Number.isInteger(frameCount)
    || frameCount < 1
    || typeof manifest.request.animation !== "string"
    || manifest.request.animation.trim() === ""
    || typeof manifest.request.direction !== "string"
    || manifest.request.direction.trim() === ""
    || manifest.movement_plan.length !== frameCount
  ) {
    throw blocked("MANIFEST_INVALID");
  }
  for (let index = 0; index < frameCount; index += 1) {
    const movement = manifest.movement_plan[index];
    if (
      !exactKeys(movement, ["frame_index", "phase", "body_motion"])
      || movement.frame_index !== index + 1
      || typeof movement.phase !== "string"
      || movement.phase.trim() === ""
      || typeof movement.body_motion !== "string"
      || movement.body_motion.trim() === ""
    ) {
      throw blocked("MANIFEST_INVALID");
    }
  }
  const authorization = manifest.authorization;
  if (
    authorization.status !== "AUTHORIZED"
    || typeof authorization.authorized_at !== "string"
    || typeof authorization.contract_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(authorization.contract_sha256)
  ) {
    throw blocked("RUN_NOT_AUTHORIZED");
  }
  validateTimestamp(
    authorization.authorized_at,
    Date.now(),
    "RUN_NOT_AUTHORIZED",
    false,
  );
  const contractValue = structuredClone(manifest);
  for (const key of [
    "status",
    "authorized_at",
    "authorized_budget_usd",
    "contract_sha256",
  ]) {
    delete contractValue.authorization[key];
  }
  if (canonicalSha256(contractValue) !== authorization.contract_sha256) {
    throw blocked("AUTHORIZATION_RECORD_INVALID");
  }
  return manifest;
}


function validateBudget(manifest) {
  const authorization = manifest.authorization;
  const pricing = manifest.provider.pricing;
  if (
    !isObject(pricing)
    || authorization.planned_requests !== manifest.request.frame_count
    || authorization.authorized_budget_usd === undefined
    || authorization.max_budget_usd === undefined
    || authorization.worst_case_usd === undefined
  ) {
    throw blocked("AUTHORIZATION_RECORD_INVALID");
  }
  const maximum = decimal(authorization.max_budget_usd, "AUTHORIZATION_RECORD_INVALID");
  const authorized = decimal(
    authorization.authorized_budget_usd,
    "AUTHORIZATION_RECORD_INVALID",
  );
  const declaredWorst = decimal(
    authorization.worst_case_usd,
    "AUTHORIZATION_RECORD_INVALID",
  );
  if (!decimalEqual(maximum, authorized)) throw blocked("AUTHORIZED_BUDGET_MISMATCH");
  if (decimalGreater(declaredWorst, maximum)) {
    throw blocked("BUDGET_WORST_CASE_EXCEEDED");
  }
  const effective = decimal(
    pricing.effective_unit_price_usd,
    "AUTHORIZATION_RECORD_INVALID",
  );
  const calculatedWorst = multiplyInteger(effective, manifest.request.frame_count);
  if (!decimalEqual(calculatedWorst, declaredWorst)) {
    throw blocked("AUTHORIZATION_RECORD_INVALID");
  }
  return { effective, maximum };
}


function validatePricingEvidence(evidence, manifest) {
  if (!exactKeys(evidence, PRICING_EVIDENCE_KEYS)) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  if (
    evidence.model !== MODEL
    || evidence.source_url !== PRICING_URL
    || evidence.currency !== "USD"
    || evidence.unit !== "image"
    || evidence.resolution !== "0.5K"
  ) {
    throw blocked("PRICE_NOT_VERIFIABLE");
  }
  validateTimestamp(evidence.verified_at);
  const base = decimal(evidence.base_unit_price_usd);
  const multiplier = decimal(evidence.resolution_multiplier);
  const effective = multiplyDecimal(base, multiplier);
  const manifestPricing = manifest.provider.pricing;
  if (
    !isObject(manifestPricing)
    || manifestPricing.source_url !== evidence.source_url
    || manifestPricing.verified_at !== evidence.verified_at
    || manifestPricing.currency !== evidence.currency
    || manifestPricing.unit !== evidence.unit
    || manifestPricing.resolution !== evidence.resolution
    || !decimalEqual(decimal(manifestPricing.base_unit_price_usd), base)
    || !decimalEqual(decimal(manifestPricing.resolution_multiplier), multiplier)
    || !decimalEqual(decimal(manifestPricing.effective_unit_price_usd), effective)
  ) {
    throw blocked("PRICE_EVIDENCE_MISMATCH");
  }
  return effective;
}


function validateRequestRecord(record, index, manifest) {
  const allowed = new Set([
    "frame_index",
    "attempt",
    "model",
    "request_fingerprint",
    "request_id",
    "status",
    "estimated_cost_usd",
    "submitted_at",
    "result_path",
    "raw_sha256",
    "safe_error_type",
  ]);
  if (
    !isObject(record)
    || Object.keys(record).some((key) => !allowed.has(key))
    || !Number.isInteger(record.frame_index)
    || record.frame_index < 1
    || record.frame_index > manifest.request.frame_count
    || record.attempt !== 1
    || record.model !== MODEL
    || typeof record.request_fingerprint !== "string"
    || !/^[0-9a-f]{64}$/.test(record.request_fingerprint)
    || !KNOWN_REQUEST_STATUSES.has(record.status)
    || typeof record.submitted_at !== "string"
    || record.result_path === undefined
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  validateTimestamp(
    record.submitted_at,
    Date.now(),
    "RUN_MANIFEST_INVALID",
    false,
  );
  const estimatedCost = decimal(record.estimated_cost_usd, "RUN_MANIFEST_INVALID");
  const authorizedUnitCost = decimal(
    manifest.provider?.pricing?.effective_unit_price_usd,
    "RUN_MANIFEST_INVALID",
  );
  if (!decimalEqual(estimatedCost, authorizedUnitCost)) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (
    record.request_id !== null
    && !safeRequestId(record.request_id)
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (
    record.request_id === null
    && !new Set(["SUBMITTING", "SUBMISSION_STATUS_UNKNOWN"]).has(record.status)
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (record.result_path === null) {
    if (
      Object.hasOwn(record, "raw_sha256")
      && (
        !new Set(["COMPLETED", "REJECTED"]).has(record.status)
        || typeof record.raw_sha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(record.raw_sha256)
      )
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
  } else if (
    typeof record.result_path !== "string"
    || record.result_path !== `raw/frame-${String(record.frame_index).padStart(2, "0")}/attempt-01.png`
    || typeof record.raw_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(record.raw_sha256)
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (
    Object.hasOwn(record, "safe_error_type")
    && !safeErrorTypeAllowed(record.safe_error_type)
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const hasRawHash = Object.hasOwn(record, "raw_sha256");
  const hasSafeError = Object.hasOwn(record, "safe_error_type");
  if (
    (record.status === "SUBMITTING"
      && (record.request_id !== null || record.result_path !== null || hasRawHash || hasSafeError))
    || (record.status === "SUBMISSION_STATUS_UNKNOWN"
      && (record.request_id !== null || record.result_path !== null || hasRawHash))
    || (ACTIVE_REQUEST_STATUSES.has(record.status)
      && (record.request_id === null || record.result_path !== null || hasRawHash || hasSafeError))
    || (record.status === "COMPLETED"
      && (record.request_id === null || hasSafeError))
    || (REMOTE_TERMINAL_STATUSES.has(record.status)
      && (record.request_id === null || record.result_path !== null || hasRawHash))
    || (record.status === "REJECTED"
      && (
        record.request_id === null
        || record.result_path !== null
        || !hasSafeError
        || !DETERMINISTIC_RESULT_FAILURES.has(record.safe_error_type)
      ))
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  return index;
}


function validateFrames(run, manifest, runDir) {
  if (!Array.isArray(run.frames)) throw blocked("RUN_MANIFEST_INVALID");
  const byIndex = new Map();
  for (const frame of run.frames) {
    if (
      !isObject(frame)
      || !Number.isInteger(frame.frame_index)
      || byIndex.has(frame.frame_index)
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
    byIndex.set(frame.frame_index, frame);
  }
  const current = run.next_request;
  if (!Number.isInteger(current) || current < 1 || current > manifest.request.frame_count + 1) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const rejected = [];
  for (let index = 1; index < current; index += 1) {
    const frame = byIndex.get(index);
    const expectedPath = `normalized/frames/frame-${String(index).padStart(2, "0")}.png`;
    if (
      !frame
      || !exactKeys(frame, EVALUATION_FRAME_KEYS)
      || frame.status !== "ACCEPTED"
      || frame.normalized_path !== expectedPath
      || typeof frame.normalized_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(frame.normalized_sha256)
    ) {
      throw blocked("PREVIOUS_FRAME_UNAVAILABLE");
    }
    const bytes = stableBytes(path.join(runDir, expectedPath), "WORKSPACE_CONFINEMENT_FAILED", runDir).bytes;
    if (sha256(bytes) !== frame.normalized_sha256) throw blocked("REFERENCE_HASH_MISMATCH");
  }
  for (const frame of run.frames) {
    if (frame.frame_index < current) continue;
    if (
      frame.frame_index === current
      && current <= manifest.request.frame_count
      && frame.status === "REJECTED"
      && run.state === "TECHNICAL_REJECTED"
      && (
        EVALUATION_CONTENT_FAILURES.has(run.terminal_reason)
        || DETERMINISTIC_RESULT_FAILURES.has(run.terminal_reason)
      )
    ) {
      rejected.push(frame);
      continue;
    }
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (rejected.length > 1 || run.frames.length !== current - 1 + rejected.length) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (run.state === "TECHNICAL_REJECTED" && rejected.length !== 1) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  return byIndex;
}


function validateRunContext(args) {
  rejectAlternateDataStream(args["run-manifest"]);
  rejectAlternateDataStream(args["request-plan"]);
  rejectAlternateDataStream(args.pricing);
  const clientModule = validateClientModule(args);
  const runManifestPath = path.resolve(args["run-manifest"]);
  if (
    path.basename(runManifestPath) !== "run_manifest.json"
    || path.basename(path.dirname(runManifestPath)) !== "reports"
  ) {
    throw blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  const reports = requireDirectory(
    path.dirname(runManifestPath),
    "WORKSPACE_CONFINEMENT_FAILED",
  );
  const runDir = requireDirectory(path.dirname(reports), "WORKSPACE_CONFINEMENT_FAILED");
  for (const relative of [
    "input",
    "input/source",
    "input/requests",
    "raw",
    "normalized",
    "normalized/frames",
    "previews",
    "reports",
    "reports/frames",
  ]) {
    requireDirectory(path.join(runDir, relative), "WORKSPACE_CONFINEMENT_FAILED", runDir);
  }
  const runRecord = readJsonStable(
    runManifestPath,
    "RUN_MANIFEST_INVALID",
    runDir,
  );
  const run = runRecord.value;
  if (
    !exactKeys(run, RUN_KEYS)
    || !Number.isInteger(run.schema_version)
    || !new Set([1, 2]).has(run.schema_version)
    || run.manifest_path !== "reports/manifest.json"
    || !Array.isArray(run.requests)
    || !isObject(run.artifacts)
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const manifestRecord = readJsonStable(
    path.join(runDir, "reports/manifest.json"),
    "MANIFEST_INVALID",
    runDir,
  );
  const manifest = validateManifest(manifestRecord.value);
  if (run.schema_version !== manifest.schema_version) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (
    !exactKeys(
      run.artifacts,
      Object.hasOwn(run.artifacts, "evaluation")
        ? ["run_id", "evaluation"]
        : ["run_id"],
    )
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (run.artifacts.run_id !== path.basename(runDir)) {
    throw blocked("RUN_IDENTITY_MISMATCH");
  }
  if (run.manifest_sha256 !== canonicalSha256(manifest)) {
    throw blocked("MANIFEST_HASH_MISMATCH");
  }
  validateBudget(manifest);
  const source = manifest.source;
  if (
    source.original_path !== "input/source/original.png"
    || source.normalized_path !== "input/source/master.png"
    || typeof source.sha256 !== "string"
    || typeof source.normalized_sha256 !== "string"
  ) {
    throw blocked("MANIFEST_INVALID");
  }
  const original = stableBytes(
    path.join(runDir, source.original_path),
    "WORKSPACE_CONFINEMENT_FAILED",
    runDir,
  ).bytes;
  const master = stableBytes(
    path.join(runDir, source.normalized_path),
    "WORKSPACE_CONFINEMENT_FAILED",
    runDir,
  ).bytes;
  if (sha256(original) !== source.sha256 || sha256(master) !== source.normalized_sha256) {
    throw blocked("REFERENCE_HASH_MISMATCH");
  }
  const movementRecord = readJsonStable(
    path.join(runDir, "input/movement-plan.json"),
    "MOVEMENT_PLAN_MISMATCH",
    runDir,
  );
  const movement = movementRecord.value;
  if (
    canonicalSha256(movement) !== canonicalSha256(manifest.movement_plan)
    || !movementRecord.bytes.equals(jsonBytes(manifest.movement_plan))
  ) {
    throw blocked("MOVEMENT_PLAN_MISMATCH");
  }
  validateFrames(run, manifest, runDir);
  const context = {
    clientModule,
    runManifestPath,
    runDir,
    run,
    runBytes: runRecord.bytes,
    manifest,
  };
  validateLedger(context);
  validateEvaluationSemantics(context);
  probeAtomicBridge();
  return context;
}


function expectedReferenceRecords(context, request) {
  const frame = request.frame_index;
  const expected = [{ role: "master", path: "input/source/master.png" }];
  if (frame > 1) {
    expected.push({
      role: "previous_accepted",
      path: `normalized/frames/frame-${String(frame - 1).padStart(2, "0")}.png`,
    });
  }
  if (canonicalSha256(request.references) !== canonicalSha256(expected)) {
    throw blocked("REQUEST_PLAN_INVALID");
  }
  return expected.map((reference) => {
    const bytes = stableBytes(
      path.join(context.runDir, reference.path),
      "WORKSPACE_CONFINEMENT_FAILED",
      context.runDir,
    ).bytes;
    let expectedHash;
    if (reference.role === "master") {
      expectedHash = context.manifest.source.normalized_sha256;
    } else {
      const frameRecord = context.run.frames.find(
        (item) => item.frame_index === frame - 1,
      );
      expectedHash = frameRecord?.normalized_sha256;
    }
    const digest = sha256(bytes);
    if (digest !== expectedHash || !isPng(bytes)) throw blocked("REFERENCE_HASH_MISMATCH");
    return { ...reference, sha256: digest, bytes };
  });
}


function validateRequestPlanAt(context, frame, suppliedPath = null) {
  const expectedPath = path.join(
    context.runDir,
    "input/requests",
    `frame-${String(frame).padStart(2, "0")}.json`,
  );
  const supplied = suppliedPath === null ? expectedPath : path.resolve(suppliedPath);
  if (!samePath(supplied, expectedPath)) throw blocked("REQUEST_PLAN_INVALID");
  const requestRecord = readJsonStable(
    supplied,
    "REQUEST_PLAN_INVALID",
    context.runDir,
  );
  const request = requestRecord.value;
  if (!requestRecord.bytes.equals(jsonBytes(request))) {
    throw blocked("REQUEST_PLAN_INVALID");
  }
  const movement = context.manifest.movement_plan[frame - 1];
  if (
    !exactKeys(request, PUBLIC_REQUEST_KEYS)
    || request.frame_index !== frame
    || request.model !== MODEL
    || !exactKeys(request.instructions, INSTRUCTION_KEYS)
    || request.instructions.animation !== context.manifest.request.animation
    || request.instructions.direction !== context.manifest.request.direction
    || request.instructions.phase !== movement?.phase
    || request.instructions.body_motion !== movement?.body_motion
    || request.instructions.identity !== "preserve the same character, face, proportions, clothing, equipment, palette and art style"
    || request.instructions.framing !== "one complete character, bottom-center grounded, no crop"
    || request.instructions.background !== "solid uniform run chroma, no shadow, no gradient"
    || typeof request.request_fingerprint !== "string"
    || !/^[0-9a-f]{64}$/.test(request.request_fingerprint)
  ) {
    throw blocked("REQUEST_PLAN_INVALID");
  }
  const references = expectedReferenceRecords(context, request);
  const unsigned = structuredClone(request);
  delete unsigned.request_fingerprint;
  const fingerprint = canonicalSha256({
    schema_version: 1,
    request: unsigned,
    run_id: context.run.artifacts.run_id,
    manifest_sha256: context.run.manifest_sha256,
    references: references.map(({ role, path: relative, sha256: digest }) => ({
      role,
      path: relative,
      sha256: digest,
    })),
  });
  if (fingerprint !== request.request_fingerprint) {
    throw blocked("REQUEST_FINGERPRINT_MISMATCH");
  }
  return { request, references };
}


function validateRequestPlan(args, context) {
  return validateRequestPlanAt(
    context,
    context.run.next_request,
    args["request-plan"],
  );
}


function validateEvaluationFrame(frame, request, runDir) {
  if (!exactKeys(frame, EVALUATION_FRAME_KEYS)) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const index = frame.frame_index;
  const rawPath = `raw/frame-${String(index).padStart(2, "0")}/attempt-01.png`;
  const reportPath = `reports/frames/frame-${String(index).padStart(2, "0")}.json`;
  if (
    request?.status !== "COMPLETED"
    || request.result_path !== rawPath
    || frame.raw_path !== rawPath
    || frame.raw_sha256 !== request.raw_sha256
    || frame.report_path !== reportPath
    || typeof frame.report_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(frame.report_sha256)
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const reportRecord = readJsonStable(
    path.join(runDir, ...reportPath.split("/")),
    "RUN_MANIFEST_INVALID",
    runDir,
  );
  const report = reportRecord.value;
  if (
    sha256(reportRecord.bytes) !== frame.report_sha256
    || !reportRecord.bytes.equals(jsonBytes(report))
    || !exactKeys(report, EVALUATION_REPORT_KEYS)
    || report.schema_version !== 1
    || report.frame_index !== index
    || !isObject(report.normalization)
    || !isObject(report.advisory)
    || !exactKeys(report.provenance, EVALUATION_PROVENANCE_KEYS)
    || report.provenance.raw_path !== frame.raw_path
    || report.provenance.raw_sha256 !== frame.raw_sha256
    || report.provenance.normalized_path !== frame.normalized_path
    || report.provenance.normalized_sha256 !== frame.normalized_sha256
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  if (frame.status === "ACCEPTED") {
    const normalizedPath = `normalized/frames/frame-${String(index).padStart(2, "0")}.png`;
    if (
      frame.normalized_path !== normalizedPath
      || typeof frame.normalized_sha256 !== "string"
      || !/^[0-9a-f]{64}$/.test(frame.normalized_sha256)
      || report.technical_result !== "PASS"
      || !Array.isArray(report.failures)
      || report.failures.length !== 0
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
    const normalized = stableBytes(
      path.join(runDir, ...normalizedPath.split("/")),
      "RUN_MANIFEST_INVALID",
      runDir,
    ).bytes;
    if (sha256(normalized) !== frame.normalized_sha256) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
  } else if (
    frame.status !== "REJECTED"
    || frame.normalized_path !== null
    || frame.normalized_sha256 !== null
    || report.technical_result !== "FAIL"
    || !Array.isArray(report.failures)
    || report.failures.length !== 1
    || !EVALUATION_CONTENT_FAILURES.has(report.failures[0])
  ) {
    throw blocked("RUN_MANIFEST_INVALID");
  } else {
    const normalizedPath = path.join(
      runDir,
      "normalized/frames",
      `frame-${String(index).padStart(2, "0")}.png`,
    );
    if (fs.lstatSync(normalizedPath, { throwIfNoEntry: false })) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
  }
  return report;
}


function validateEvaluationClaim(run, frame, report) {
  const claim = run.artifacts.evaluation;
  if (!exactKeys(claim, EVALUATION_CLAIM_KEYS)) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const expected = {
    frame_index: frame.frame_index,
    raw_path: frame.raw_path,
    raw_sha256: frame.raw_sha256,
    normalized_path: frame.normalized_path,
    normalized_sha256: frame.normalized_sha256,
    report_path: frame.report_path,
    report_sha256: frame.report_sha256,
    technical_result: report.technical_result,
    failure: report.failures.length === 0 ? null : report.failures[0],
    status: "COMPLETED",
  };
  if (canonicalSha256(claim) !== canonicalSha256(expected)) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
}


function validateLedger(context) {
  const { run, manifest, runDir } = context;
  if (run.requests.length > manifest.authorization.planned_requests) {
    throw blocked("RUN_MANIFEST_INVALID");
  }
  const requestByFrame = new Map();
  run.requests.forEach((record, index) => {
    validateRequestRecord(record, index, manifest);
    if (
      record.frame_index !== index + 1
      || requestByFrame.has(record.frame_index)
      || record.frame_index > run.next_request
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
    const plan = validateRequestPlanAt(context, record.frame_index);
    if (plan.request.request_fingerprint !== record.request_fingerprint) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
    if (record.result_path !== null) {
      if (record.status !== "COMPLETED") throw blocked("RUN_MANIFEST_INVALID");
      const expected = `raw/frame-${String(record.frame_index).padStart(2, "0")}/attempt-01.png`;
      if (record.result_path !== expected) throw blocked("RUN_MANIFEST_INVALID");
      const raw = stableBytes(
        path.join(runDir, ...expected.split("/")),
        "WORKSPACE_CONFINEMENT_FAILED",
        runDir,
      ).bytes;
      if (!isPng(raw) || sha256(raw) !== record.raw_sha256) {
        throw blocked("RAW_PROVENANCE_MISMATCH");
      }
    }
    requestByFrame.set(record.frame_index, record);
    if (
      record.status === "SUBMISSION_STATUS_UNKNOWN"
      && (
        run.state !== "BLOCKED"
        || run.terminal_reason !== "SUBMISSION_STATUS_UNKNOWN"
      )
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
    if (
      REMOTE_TERMINAL_STATUSES.has(record.status)
      && (
        run.state !== "BLOCKED"
        || run.terminal_reason !== `REQUEST_${record.status}`
      )
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
  });

  for (let frameIndex = 1; frameIndex < run.next_request; frameIndex += 1) {
    const record = requestByFrame.get(frameIndex);
    if (
      !record
      || record.status !== "COMPLETED"
      || record.result_path === null
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
  }

  const evaluated = [];
  for (let frameIndex = 1; frameIndex < run.next_request; frameIndex += 1) {
    const frame = run.frames.find((candidate) => candidate.frame_index === frameIndex);
    const report = validateEvaluationFrame(
      frame,
      requestByFrame.get(frameIndex),
      runDir,
    );
    evaluated.push({ frame, report });
  }

  const currentFrame = run.frames.find(
    (frame) => frame.frame_index === run.next_request,
  );
  const currentRequestRecord = requestByFrame.get(run.next_request);
  if (currentFrame?.status === "REJECTED") {
    const providerResultRejected = currentRequestRecord?.status === "REJECTED"
      && currentRequestRecord.result_path === null;
    const evaluatedFrameRejected = currentRequestRecord?.status === "COMPLETED"
      && currentRequestRecord.result_path !== null;
    if (
      run.state !== "TECHNICAL_REJECTED"
      || !currentRequestRecord
      || (!providerResultRejected && !evaluatedFrameRejected)
    ) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
    if (providerResultRejected) {
      if (
        !exactKeys(currentFrame, ["frame_index", "status"])
        || run.terminal_reason !== currentRequestRecord.safe_error_type
        || !DETERMINISTIC_RESULT_FAILURES.has(run.terminal_reason)
      ) {
        throw blocked("RUN_MANIFEST_INVALID");
      }
    } else {
      const report = validateEvaluationFrame(
        currentFrame,
        currentRequestRecord,
        runDir,
      );
      if (run.terminal_reason !== report.failures[0]) {
        throw blocked("RUN_MANIFEST_INVALID");
      }
      evaluated.push({ frame: currentFrame, report });
    }
  } else if (currentRequestRecord?.status === "REJECTED") {
    throw blocked("RUN_MANIFEST_INVALID");
  }

  if (evaluated.length === 0) {
    if (Object.hasOwn(run.artifacts, "evaluation")) {
      throw blocked("RUN_MANIFEST_INVALID");
    }
  } else {
    const latest = evaluated[evaluated.length - 1];
    validateEvaluationClaim(run, latest.frame, latest.report);
  }
}


function safeErrorTypeAllowed(value) {
  return typeof value === "string" && SAFE_ERROR_TYPES.has(value);
}


function providerErrorTypeAllowed(value) {
  if (!safeErrorTypeAllowed(value)) return false;
  const credential = typeof process.env.FAL_KEY === "string"
    ? process.env.FAL_KEY.trim()
    : "";
  return credential === ""
    || (!value.includes(credential) && !credential.includes(value));
}


function safeRequestId(value) {
  if (
    typeof value !== "string"
    || value.length < 1
    || value.length > MAX_REQUEST_ID_LENGTH
    || value.trim() !== value
    || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)
  ) {
    return false;
  }
  const credential = typeof process.env.FAL_KEY === "string"
    ? process.env.FAL_KEY.trim()
    : "";
  return credential === ""
    || (!value.includes(credential) && !credential.includes(value));
}


function safeErrorType(error, fallback) {
  const detail = Array.isArray(error?.body?.detail) ? error.body.detail[0] : null;
  for (const candidate of [detail?.type, error?.name, fallback]) {
    if (providerErrorTypeAllowed(candidate)) return candidate;
  }
  return null;
}


function setSafeErrorType(record, error, fallback) {
  const value = safeErrorType(error, fallback);
  if (value === null) {
    delete record.safe_error_type;
  } else {
    record.safe_error_type = value;
  }
}


function claimPath(context, frameIndex) {
  return path.join(
    context.runDir,
    "reports",
    `transport-claim-frame-${String(frameIndex).padStart(2, "0")}.json`,
  );
}


function existingClaimState(context, request) {
  const candidate = claimPath(context, request.frame_index);
  const exists = fs.lstatSync(candidate, { throwIfNoEntry: false });
  if (!exists) return null;
  let claim;
  try {
    claim = readJsonStable(candidate, "SUBMISSION_STATUS_UNKNOWN", context.runDir).value;
  } catch {
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  const matching = context.run.requests.find(
    (record) => record.frame_index === request.frame_index
      && record.request_fingerprint === request.request_fingerprint,
  );
  if (
    !isObject(claim)
    || claim.frame_index !== request.frame_index
    || claim.request_fingerprint !== request.request_fingerprint
    || !matching
    || matching.request_id === null
  ) {
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  return { candidate, matching };
}


function createClaim(context, request) {
  const candidate = claimPath(context, request.frame_index);
  const claim = {
    schema_version: 1,
    frame_index: request.frame_index,
    attempt: 1,
    model: MODEL,
    request_fingerprint: request.request_fingerprint,
    status: "SUBMITTING",
    claimed_at: new Date().toISOString(),
  };
  try {
    writeNewAtomic(candidate, jsonBytes(claim), "SUBMISSION_STATUS_UNKNOWN");
  } catch {
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  return candidate;
}


function persistContext(context) {
  context.runBytes = replaceJsonAtomic(
    context.runManifestPath,
    context.run,
    context.runBytes,
  );
}


function requestSummary(context, reason) {
  const record = context.run.requests.find(
    (item) => item.frame_index === context.run.next_request,
  );
  return {
    state: context.run.state,
    reason,
    frame_index: record?.frame_index ?? context.run.next_request,
    request_id: safeRequestId(record?.request_id) ? record.request_id : null,
    status: record?.status ?? null,
    submitted_requests: context.run.requests.length,
  };
}


function blockedPayload(reason, context = null) {
  if (context === null) {
    return { state: "BLOCKED", reason, frame_index: null, request_id: null, status: null, submitted_requests: 0 };
  }
  return { ...requestSummary(context, reason), state: "BLOCKED" };
}


function buildFalInput(requestPlan, manifest) {
  const instructions = requestPlan.request.instructions;
  const later = requestPlan.request.frame_index > 1;
  const subject = later
    ? "image 1 (master) and image 2 (previous accepted frame)"
    : "image 1";
  const movementClause = manifest.schema_version === 2
    ? "; body movement: " + instructions.body_motion
    : "";
  return {
    prompt: `Redraw ${subject} as the same character ${instructions.animation} ${instructions.direction} at ${instructions.phase}${movementClause}; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.`,
    num_images: 1,
    aspect_ratio: "auto",
    output_format: "png",
    image_urls: requestPlan.references.map(
      (reference) => `data:image/png;base64,${reference.bytes.toString("base64")}`,
    ),
    resolution: "0.5K",
    limit_generations: true,
    enable_web_search: false,
  };
}


function createSinglePostFetch(delegate) {
  let postConsumed = false;
  return async (input, init = {}) => {
    const isRequest = typeof Request !== "undefined" && input instanceof Request;
    const method = String(init.method || (isRequest ? input.method : "GET")).toUpperCase();
    const url = String(isRequest ? input.url : input);
    if (method === "POST") {
      if (postConsumed || url !== SUBMIT_URL) {
        throw new DOMException("SECOND_POST_BLOCKED", "AbortError");
      }
      postConsumed = true;
      return delegate(input, { ...init, redirect: "error" });
    }
    if (method !== "GET") {
      throw new DOMException("UNEXPECTED_HTTP_METHOD", "AbortError");
    }
    return delegate(input, init);
  };
}


async function importClient(clientModule) {
  try {
    const imported = clientModule === null
      ? await import("@fal-ai/client")
      : await import(
        `data:text/javascript;base64,${clientModule.bytes.toString("base64")}`
      );
    const client = clientModule === null
      ? imported.createFalClient({
        credentials: process.env.FAL_KEY,
        retry: { maxRetries: 0 },
        fetch: createSinglePostFetch(globalThis.fetch.bind(globalThis)),
      })
      : imported.fal;
    if (
      !client?.queue
      || typeof client.queue.submit !== "function"
      || typeof client.queue.status !== "function"
      || typeof client.queue.result !== "function"
    ) {
      throw new Error();
    }
    return client;
  } catch {
    throw blocked("CLIENT_IMPORT_FAILED");
  }
}


function normalizeStatus(response) {
  const status = String(response?.status || "").toUpperCase();
  if (
    !ACTIVE_REQUEST_STATUSES.has(status)
    && status !== "COMPLETED"
    && !REMOTE_TERMINAL_STATUSES.has(status)
  ) {
    throw blocked("REQUEST_STATUS_INVALID");
  }
  return status;
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
  if (
    !Buffer.isBuffer(bytes)
    || bytes.length < signature.length
    || bytes.length > MAX_RESULT_BYTES
    || !bytes.subarray(0, 8).equals(signature)
  ) {
    return false;
  }
  let offset = 8;
  let chunk = 0;
  let sawHeader = false;
  let sawData = false;
  while (offset < bytes.length) {
    if (bytes.length - offset < 12) return false;
    const length = bytes.readUInt32BE(offset);
    const typeStart = offset + 4;
    const dataStart = typeStart + 4;
    if (length > bytes.length - dataStart - 4) return false;
    const dataEnd = dataStart + length;
    const end = dataEnd + 4;
    const typeBytes = bytes.subarray(typeStart, dataStart);
    if (![...typeBytes].every((value) => (
      (value >= 65 && value <= 90) || (value >= 97 && value <= 122)
    ))) return false;
    if (crc32(bytes.subarray(typeStart, dataEnd)) !== bytes.readUInt32BE(dataEnd)) {
      return false;
    }
    const type = typeBytes.toString("ascii");
    if (chunk === 0 && type !== "IHDR") return false;
    if (type === "IHDR") {
      if (sawHeader || chunk !== 0 || length !== 13) return false;
      const width = bytes.readUInt32BE(dataStart);
      const height = bytes.readUInt32BE(dataStart + 4);
      if (width < 1 || height < 1) return false;
      sawHeader = true;
    } else if (!sawHeader) return false;
    if (type === "IDAT") sawData = true;
    if (type === "IEND") return length === 0 && sawData && end === bytes.length;
    offset = end;
    chunk += 1;
  }
  return false;
}


async function readResultBytes(url) {
  if (typeof url !== "string") throw blocked("RESULT_SCHEMA_INVALID");
  let bytes;
  if (url.startsWith("data:image/png;base64,")) {
    const match = /^data:image\/png;base64,([A-Za-z0-9+/]+={0,2})$/.exec(url);
    if (!match) throw blocked("RESULT_SCHEMA_INVALID");
    const maximumEncodedLength = Math.ceil(MAX_RESULT_BYTES / 3) * 4;
    if (match[1].length > maximumEncodedLength) throw blocked("RESULT_TOO_LARGE");
    bytes = Buffer.from(match[1], "base64");
    if (bytes.length > MAX_RESULT_BYTES) throw blocked("RESULT_TOO_LARGE");
  } else if (url.startsWith("https://")) {
    let response;
    try {
      response = await fetch(url, { redirect: "follow" });
    } catch {
      throw blocked("RESULT_DOWNLOAD_FAILED");
    }
    if (!response.ok || !String(response.url).startsWith("https://")) {
      throw blocked("RESULT_DOWNLOAD_FAILED");
    }
    const declaredLength = response.headers?.get?.("content-length");
    if (
      declaredLength !== null
      && declaredLength !== undefined
      && (!/^[0-9]+$/.test(declaredLength)
        || BigInt(declaredLength) > BigInt(MAX_RESULT_BYTES))
    ) {
      throw blocked("RESULT_TOO_LARGE");
    }
    const reader = response.body?.getReader?.();
    if (!reader || typeof reader.read !== "function") {
      throw blocked("RESULT_DOWNLOAD_FAILED");
    }
    const chunks = [];
    let total = 0;
    try {
      while (true) {
        const item = await reader.read();
        if (!isObject(item) || typeof item.done !== "boolean") {
          throw blocked("RESULT_DOWNLOAD_FAILED");
        }
        if (item.done) break;
        if (!(item.value instanceof Uint8Array)) {
          throw blocked("RESULT_DOWNLOAD_FAILED");
        }
        total += item.value.byteLength;
        if (total > MAX_RESULT_BYTES) {
          try { await reader.cancel?.(); } catch {}
          throw blocked("RESULT_TOO_LARGE");
        }
        chunks.push(Buffer.from(item.value));
      }
    } catch (error) {
      if (error?.reason) throw error;
      throw blocked("RESULT_DOWNLOAD_FAILED");
    }
    bytes = Buffer.concat(chunks, total);
  } else {
    throw blocked("RESULT_URL_NOT_ALLOWED");
  }
  if (!isPng(bytes)) throw blocked("RESULT_NOT_PNG");
  return bytes;
}


function exactRawPath(context, record) {
  const relative = `raw/frame-${String(record.frame_index).padStart(2, "0")}/attempt-01.png`;
  return { relative, destination: path.join(context.runDir, ...relative.split("/")) };
}


function preflightRawDestination(context, record) {
  const { destination } = exactRawPath(context, record);
  const rawRoot = requireDirectory(
    path.join(context.runDir, "raw"),
    "WORKSPACE_CONFINEMENT_FAILED",
    context.runDir,
  );
  const frameDirectory = path.dirname(destination);
  validateDirectoryChain(rawRoot, frameDirectory, "WORKSPACE_CONFINEMENT_FAILED");
  const existingFrame = fs.lstatSync(frameDirectory, { throwIfNoEntry: false });
  if (existingFrame) {
    requireDirectory(frameDirectory, "WORKSPACE_CONFINEMENT_FAILED", rawRoot);
  }
  if (fs.lstatSync(destination, { throwIfNoEntry: false })) {
    throw blocked("RESULT_DESTINATION_EXISTS");
  }
}


function recoverPublishedRaw(context, record) {
  if (record.status !== "COMPLETED" || record.result_path !== null) return false;
  if (
    typeof record.raw_sha256 !== "string"
    || !/^[0-9a-f]{64}$/.test(record.raw_sha256)
  ) {
    return false;
  }
  const { relative, destination } = exactRawPath(context, record);
  if (!fs.lstatSync(destination, { throwIfNoEntry: false })) return false;
  const bytes = stableBytes(
    destination,
    "WORKSPACE_CONFINEMENT_FAILED",
    context.runDir,
  ).bytes;
  if (!isPng(bytes) || sha256(bytes) !== record.raw_sha256) {
    throw blocked("RAW_PROVENANCE_MISMATCH");
  }
  record.result_path = relative;
  persistContext(context);
  return true;
}


function publishRaw(context, record, bytes) {
  const { relative, destination } = exactRawPath(context, record);
  const rawRoot = requireDirectory(
    path.join(context.runDir, "raw"),
    "WORKSPACE_CONFINEMENT_FAILED",
    context.runDir,
  );
  const frameDirectory = path.dirname(destination);
  const existingFrame = fs.lstatSync(frameDirectory, { throwIfNoEntry: false });
  if (!existingFrame) {
    try {
      fs.mkdirSync(frameDirectory);
    } catch {
      throw blocked("WORKSPACE_CONFINEMENT_FAILED");
    }
  }
  requireDirectory(frameDirectory, "WORKSPACE_CONFINEMENT_FAILED", rawRoot);
  if (fs.lstatSync(destination, { throwIfNoEntry: false })) {
    throw blocked("RESULT_DESTINATION_EXISTS");
  }
  writeNewAtomic(destination, bytes, "RESULT_DESTINATION_EXISTS");
  const hook = process.env.TOWER_SPRITE_TEST_EXIT_AFTER_RAW_PUBLISH;
  if (
    context.clientModule !== null
    && process.env.NODE_ENV === "test"
    && typeof process.env.FAKE_ROOT === "string"
    && typeof hook === "string"
    && /^[1-9][0-9]{0,2}$/.test(hook)
  ) {
    process.exit(Number(hook));
  }
  record.result_path = relative;
  persistContext(context);
}


async function retrieveResult(client, context, record) {
  let result;
  try {
    result = await client.queue.result(MODEL, { requestId: record.request_id });
  } catch (error) {
    if (Number(error?.status) === 422) {
      record.status = "FAILED";
      setSafeErrorType(record, error, "RESULT_VALIDATION_ERROR");
      context.run.state = "BLOCKED";
      context.run.terminal_reason = "REQUEST_FAILED";
      persistContext(context);
      throw blocked("REQUEST_FAILED");
    }
    throw blocked("RESULT_RETRIEVAL_FAILED");
  }
  const images = result?.data?.images;
  if (!Array.isArray(images) || images.length !== 1 || !isObject(images[0])) {
    throw blocked("RESULT_SCHEMA_INVALID");
  }
  const bytes = await readResultBytes(images[0].url);
  const digest = sha256(bytes);
  if (Object.hasOwn(record, "raw_sha256") && record.raw_sha256 !== digest) {
    throw blocked("RAW_PROVENANCE_MISMATCH");
  }
  record.raw_sha256 = digest;
  persistContext(context);
  publishRaw(context, record, bytes);
  return requestSummary(context, "EVALUATION_REQUIRED");
}


function persistTechnicalRejection(context, record, reason) {
  record.status = "REJECTED";
  record.safe_error_type = reason;
  if (!context.run.frames.some((frame) => frame.frame_index === record.frame_index)) {
    context.run.frames.push({ frame_index: record.frame_index, status: "REJECTED" });
  }
  context.run.state = "TECHNICAL_REJECTED";
  context.run.terminal_reason = reason;
  persistContext(context);
}


async function retrieveOrReject(client, context, record) {
  try {
    return await retrieveResult(client, context, record);
  } catch (error) {
    if (DETERMINISTIC_RESULT_FAILURES.has(error?.reason)) {
      persistTechnicalRejection(context, record, error.reason);
    }
    throw error;
  }
}


async function resumeRequest(client, context, record) {
  if (recoverPublishedRaw(context, record)) {
    return requestSummary(context, "EVALUATION_REQUIRED");
  }
  if (record.status === "COMPLETED") return retrieveOrReject(client, context, record);
  let response;
  try {
    response = await client.queue.status(MODEL, {
      requestId: record.request_id,
      logs: false,
    });
  } catch {
    throw blocked("REQUEST_STATUS_UNKNOWN");
  }
  record.status = normalizeStatus(response);
  persistContext(context);
  if (record.status === "COMPLETED") return retrieveOrReject(client, context, record);
  if (REMOTE_TERMINAL_STATUSES.has(record.status)) {
    context.run.state = "BLOCKED";
    context.run.terminal_reason = `REQUEST_${record.status}`;
    persistContext(context);
    throw blocked(`REQUEST_${record.status}`);
  }
  return requestSummary(context, "REQUEST_PENDING");
}


function currentRequest(context, request) {
  const matches = context.run.requests.filter(
    (record) => record.frame_index === request.frame_index,
  );
  if (matches.length > 1) throw blocked("RUN_MANIFEST_INVALID");
  return matches[0] || null;
}


async function executeCommand(args) {
  const context = validateRunContext(args);
  reportingContext = context;
  const requestPlan = validateRequestPlan(args, context);
  const request = requestPlan.request;
  const existing = currentRequest(context, request);
  const claim = existingClaimState(context, request);

  if (
    existing?.status === "SUBMITTING"
    || existing?.status === "SUBMISSION_STATUS_UNKNOWN"
    || (claim !== null && existing?.request_id === null)
  ) {
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  if (TERMINAL_RUN_STATES.has(context.run.state) || context.run.terminal_reason !== null) {
    throw blocked("RUN_TERMINAL");
  }
  if (existing?.status === "COMPLETED" && existing.result_path !== null) {
    const { relative, destination } = exactRawPath(context, existing);
    if (existing.result_path !== relative) throw blocked("RUN_MANIFEST_INVALID");
    const bytes = stableBytes(
      destination,
      "WORKSPACE_CONFINEMENT_FAILED",
      context.runDir,
    ).bytes;
    if (!isPng(bytes) || sha256(bytes) !== existing.raw_sha256) {
      throw blocked("RAW_PROVENANCE_MISMATCH");
    }
    throw blocked("EVALUATION_REQUIRED");
  }
  if (context.run.state !== "AUTHORIZED") throw blocked("RUN_NOT_AUTHORIZED");
  if (typeof process.env.FAL_KEY !== "string" || process.env.FAL_KEY.trim() === "") {
    throw blocked("FAL_KEY_MISSING");
  }

  if (existing !== null) {
    if (
      existing.request_fingerprint !== request.request_fingerprint
      || existing.model !== MODEL
      || existing.request_id === null
      || (!ACTIVE_REQUEST_STATUSES.has(existing.status) && existing.status !== "COMPLETED")
    ) {
      throw blocked("STALE_REQUEST_INPUT");
    }
    if (recoverPublishedRaw(context, existing)) {
      return requestSummary(context, "EVALUATION_REQUIRED");
    }
    preflightRawDestination(context, existing);
    const client = await importClient(context.clientModule);
    return resumeRequest(client, context, existing);
  }

  const pricing = readJsonStable(path.resolve(args.pricing), "PRICE_NOT_VERIFIABLE").value;
  const effective = validatePricingEvidence(pricing, context.manifest);
  const { maximum } = validateBudget(context.manifest);
  const spentAfter = multiplyInteger(effective, context.run.requests.length + 1);
  if (
    context.run.requests.length >= context.manifest.authorization.planned_requests
    || decimalGreater(spentAfter, maximum)
  ) {
    throw blocked("BUDGET_WORST_CASE_EXCEEDED");
  }

  preflightRawDestination(context, request);

  const claimFile = createClaim(context, request);
  const record = {
    frame_index: request.frame_index,
    attempt: 1,
    model: MODEL,
    request_fingerprint: request.request_fingerprint,
    request_id: null,
    status: "SUBMITTING",
    estimated_cost_usd: context.manifest.provider.pricing.effective_unit_price_usd,
    submitted_at: new Date().toISOString(),
    result_path: null,
  };
  context.run.requests.push(record);
  persistContext(context);

  let client;
  try {
    client = await importClient(context.clientModule);
  } catch (error) {
    record.status = "SUBMISSION_STATUS_UNKNOWN";
    setSafeErrorType(record, error, "SubmitError");
    context.run.state = "BLOCKED";
    context.run.terminal_reason = "SUBMISSION_STATUS_UNKNOWN";
    persistContext(context);
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  let submission;
  try {
    submission = await client.queue.submit(MODEL, { input: buildFalInput(requestPlan, context.manifest) });
  } catch (error) {
    record.status = "SUBMISSION_STATUS_UNKNOWN";
    setSafeErrorType(record, error, "SubmitError");
    context.run.state = "BLOCKED";
    context.run.terminal_reason = "SUBMISSION_STATUS_UNKNOWN";
    persistContext(context);
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  const requestId = submission?.request_id;
  if (!safeRequestId(requestId)) {
    record.status = "SUBMISSION_STATUS_UNKNOWN";
    setSafeErrorType(record, null, "InvalidSubmissionResponse");
    context.run.state = "BLOCKED";
    context.run.terminal_reason = "SUBMISSION_STATUS_UNKNOWN";
    persistContext(context);
    throw blocked("SUBMISSION_STATUS_UNKNOWN");
  }
  record.request_id = requestId;
  record.status = "IN_QUEUE";
  delete record.safe_error_type;
  persistContext(context);
  try {
    fs.unlinkSync(claimFile);
  } catch {}
  return resumeRequest(client, context, record);
}


async function main() {
  reportingContext = null;
  try {
    const parsed = parseArgs(process.argv.slice(2));
    const payload = parsed.command === "pricing"
      ? await pricingCommand(parsed.args)
      : await executeCommand(parsed.args);
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return 0;
  } catch (error) {
    const reason = typeof error?.reason === "string" ? error.reason : "INTERNAL_ERROR";
    const payload = blockedPayload(reason, reportingContext);
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return 2;
  }
}


process.exitCode = await main();
