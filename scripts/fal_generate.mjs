import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";


const PRIMARY_LIMIT = 3;
const FALLBACK_LIMIT = 1;
const PRICING_MAX_AGE_MS = 15 * 60 * 1000;
const TERMINAL_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELLED"]);


function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`invalid argument sequence near ${key ?? "end"}`);
    }
    result[key.slice(2)] = value;
  }
  return result;
}


function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}


function isWithin(candidate, parent, allowEqual = false) {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  if (relative === "") return allowEqual;
  return !relative.startsWith("..") && !path.isAbsolute(relative);
}


function writeJsonAtomic(filePath, payload) {
  const destination = path.resolve(filePath);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const temporary = `${destination}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  fs.renameSync(temporary, destination);
}


function writeOutput(outputPath, projectRoot, payload) {
  if (isWithin(outputPath, projectRoot, true)) {
    throw new Error(`output must remain outside project_root: ${outputPath}`);
  }
  writeJsonAtomic(outputPath, payload);
}


function blocked(reason, details = {}) {
  return { state: "BLOCKED", reason, attempts: 0, request_ids: [], ...details };
}


function requestSummary(run) {
  const requests = Array.isArray(run?.fal_requests) ? run.fal_requests : [];
  return {
    attempts: requests.length,
    request_ids: requests.map((item) => item.request_id).filter(Boolean),
    estimated_cost_usd: requests.reduce(
      (total, item) => total + Number(item.estimated_cost_usd || 0),
      0,
    ),
  };
}


function simulate(manifest, fixture) {
  const primaryPrice = Number(fixture?.pricing?.primary);
  const fallbackPrice = Number(fixture?.pricing?.fallback);
  if (!Number.isFinite(primaryPrice) || !Number.isFinite(fallbackPrice)) {
    return blocked("PRICE_NOT_VERIFIABLE");
  }
  const worstCase = primaryPrice * PRIMARY_LIMIT + fallbackPrice;
  if (worstCase > Number(manifest.max_budget_usd)) {
    return blocked("WORST_CASE_EXCEEDS_BUDGET", {
      worst_case_usd: worstCase,
      max_budget_usd: Number(manifest.max_budget_usd),
    });
  }
  const requests = Array.isArray(fixture.requests) ? fixture.requests : [];
  if (requests.length > 0 && requests[0].status === "in_progress") {
    const request = requests[0];
    return {
      state: "DRAFT",
      attempts: 1,
      request_ids: [String(request.request_id)],
      estimated_cost_usd: Number(request.estimated_cost_usd),
      actions: [`query:${request.request_id}`],
    };
  }
  const requestIds = [];
  const actions = [];
  let attempts = 0;
  let primaryAttempts = 0;
  let fallbackAttempts = 0;
  let estimatedCost = 0;
  for (const request of requests) {
    const model = String(request.model);
    const isPrimary = model === manifest.primary_model;
    const isFallback = model === manifest.fallback_model;
    if (!isPrimary && !isFallback) continue;
    if (isPrimary && primaryAttempts >= PRIMARY_LIMIT) continue;
    if (isFallback && fallbackAttempts >= FALLBACK_LIMIT) continue;
    if (estimatedCost + Number(request.estimated_cost_usd) > manifest.max_budget_usd) break;
    attempts += 1;
    primaryAttempts += isPrimary ? 1 : 0;
    fallbackAttempts += isFallback ? 1 : 0;
    requestIds.push(String(request.request_id));
    actions.push(`consume-fixture:${request.request_id}`);
    estimatedCost += Number(request.estimated_cost_usd);
    if (request.status === "accepted") {
      return {
        state: "GUIDE_ACCEPTED",
        attempts,
        request_ids: requestIds,
        estimated_cost_usd: estimatedCost,
        actions,
      };
    }
  }
  return {
    state: "GUIDE_REJECTED",
    attempts,
    request_ids: requestIds,
    estimated_cost_usd: estimatedCost,
    actions,
  };
}


function verifyRunManifestPath(manifest, runManifestPath, run) {
  const workspace = path.resolve(String(run.workspace || ""));
  const expected = path.join(workspace, "reports", "run_manifest.json");
  const projectRoot = path.resolve(manifest.project_root);
  if (
    path.resolve(runManifestPath) !== path.resolve(expected)
    || !fs.existsSync(workspace)
    || isWithin(workspace, projectRoot, true)
  ) {
    return null;
  }
  return workspace;
}


function verifiedPrices(pricing, manifest) {
  const verifiedAt = Date.parse(pricing?.verified_at);
  const age = Date.now() - verifiedAt;
  if (!Number.isFinite(verifiedAt) || age < -60_000 || age > PRICING_MAX_AGE_MS) return null;
  const prices = new Map();
  for (const entry of Array.isArray(pricing?.prices) ? pricing.prices : []) {
    if (!["image", "images"].includes(entry?.unit) || entry?.currency !== "USD") continue;
    const value = Number(entry.unit_price);
    if (Number.isFinite(value) && value >= 0) prices.set(entry.endpoint_id, value);
  }
  if (!prices.has(manifest.primary_model) || !prices.has(manifest.fallback_model)) return null;
  return {
    primary: prices.get(manifest.primary_model),
    fallback: prices.get(manifest.fallback_model),
  };
}


function modelCounts(run, manifest) {
  const requests = Array.isArray(run.fal_requests) ? run.fal_requests : [];
  return {
    primary: requests.filter((item) => item.model === manifest.primary_model).length,
    fallback: requests.filter((item) => item.model === manifest.fallback_model).length,
  };
}


function remainingWorstCase(run, manifest, prices) {
  const counts = modelCounts(run, manifest);
  const summary = requestSummary(run);
  return summary.estimated_cost_usd
    + Math.max(0, PRIMARY_LIMIT - counts.primary) * prices.primary
    + Math.max(0, FALLBACK_LIMIT - counts.fallback) * prices.fallback;
}


function persistRun(runManifestPath, run) {
  const summary = requestSummary(run);
  run.attempts = summary.attempts;
  run.request_ids = summary.request_ids;
  run.estimated_cost_usd = summary.estimated_cost_usd;
  writeJsonAtomic(runManifestPath, run);
}


function unresolvedRequest(run) {
  const requests = Array.isArray(run.fal_requests) ? run.fal_requests : [];
  return requests.find((item) => {
    const status = String(item.status || "").toUpperCase();
    return !TERMINAL_STATUSES.has(status) || (status === "COMPLETED" && !item.result_path);
  }) || null;
}


function normalizeStatus(status) {
  return String(status?.status || status || "UNKNOWN").toUpperCase();
}


function modelSlug(model) {
  return String(model).replace(/^fal-ai\//, "").replaceAll("/", "-").replace(/[^a-zA-Z0-9-]/g, "-");
}


async function readImageBytes(url) {
  if (url.startsWith("data:")) {
    const match = /^data:image\/[a-zA-Z0-9.+-]+;base64,(.+)$/.exec(url);
    if (!match) throw new Error("RESULT_SCHEMA_INVALID");
    return Buffer.from(match[1], "base64");
  }
  if (!url.startsWith("https://")) throw new Error("RESULT_SCHEMA_INVALID");
  const response = await fetch(url);
  if (!response.ok) throw new Error(`RESULT_DOWNLOAD_FAILED:${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}


async function saveCompletedResult(client, request, workspace, run, runManifestPath) {
  let result;
  try {
    result = await client.queue.result(request.model, { requestId: request.request_id });
  } catch (error) {
    if (Number(error?.status) === 422) {
      const detail = Array.isArray(error?.body?.detail) ? error.body.detail[0] : null;
      request.status = "FAILED";
      request.failure_reason = typeof detail?.type === "string" ? detail.type : "RESULT_VALIDATION_ERROR";
      persistRun(runManifestPath, run);
      return {
        state: "DRAFT",
        reason: "REQUEST_FAILED",
        failure_reason: request.failure_reason,
        ...requestSummary(run),
      };
    }
    request.status = "RESULT_PENDING";
    persistRun(runManifestPath, run);
    return blocked("RESULT_RETRIEVAL_FAILED", requestSummary(run));
  }
  const url = result?.data?.images?.[0]?.url;
  if (typeof url !== "string" || url.length === 0) {
    request.status = "RESULT_SCHEMA_INVALID";
    persistRun(runManifestPath, run);
    return blocked("RESULT_SCHEMA_INVALID", requestSummary(run));
  }
  const bytes = await readImageBytes(url);
  const relative = `raw/attempt-${String(request.attempt).padStart(2, "0")}-${modelSlug(request.model)}.png`;
  const destination = path.resolve(workspace, relative);
  if (!isWithin(destination, workspace)) return blocked("WORKSPACE_CONFINEMENT_FAILED", requestSummary(run));
  fs.writeFileSync(destination, bytes);
  request.status = "COMPLETED";
  request.result_path = relative;
  const receipt = {
    attempt: request.attempt,
    model: request.model,
    request_id: request.request_id,
    estimated_cost_usd: request.estimated_cost_usd,
    status: request.status,
    result_path: relative,
  };
  writeJsonAtomic(
    path.join(workspace, "reports", `fal-attempt-${String(request.attempt).padStart(2, "0")}.json`),
    receipt,
  );
  persistRun(runManifestPath, run);
  return {
    state: "DRAFT",
    reason: "GUIDE_READY_FOR_ASSESSMENT",
    ...requestSummary(run),
    result_path: relative,
  };
}


async function queryRequest(client, request, workspace, run, runManifestPath) {
  const status = await client.queue.status(request.model, {
    requestId: request.request_id,
    logs: false,
  });
  request.status = normalizeStatus(status);
  persistRun(runManifestPath, run);
  if (request.status === "COMPLETED") {
    return saveCompletedResult(client, request, workspace, run, runManifestPath);
  }
  if (request.status === "FAILED" || request.status === "CANCELLED") {
    return { state: "DRAFT", reason: `REQUEST_${request.status}`, ...requestSummary(run) };
  }
  return { state: "DRAFT", reason: "REQUEST_PENDING", ...requestSummary(run) };
}


async function importClient(clientModule) {
  if (clientModule) return import(pathToFileURL(path.resolve(clientModule)).href);
  return import("@fal-ai/client");
}


async function runReal(args, manifest) {
  const runManifestPath = path.resolve(args["run-manifest"] || "");
  let run;
  try {
    run = readJson(runManifestPath);
  } catch {
    return blocked("WORKSPACE_CONFINEMENT_FAILED");
  }
  const workspace = verifyRunManifestPath(manifest, runManifestPath, run);
  if (!workspace) return blocked("WORKSPACE_CONFINEMENT_FAILED");
  if (!Array.isArray(run.fal_requests)) run.fal_requests = [];
  if (run.guide_accepted === true) {
    return { state: "GUIDE_ACCEPTED", reason: "GUIDE_ALREADY_ACCEPTED", ...requestSummary(run) };
  }
  if (!process.env.FAL_KEY) return blocked("FAL_KEY_MISSING", requestSummary(run));
  const pricing = readJson(args.pricing);
  const prices = verifiedPrices(pricing, manifest);
  if (!prices) return blocked("PRICE_NOT_VERIFIABLE", requestSummary(run));
  const requestInput = readJson(args["request-input"]);
  const model = requestInput?.model;
  if (model !== manifest.primary_model && model !== manifest.fallback_model) {
    return blocked("MODEL_NOT_AUTHORIZED", requestSummary(run));
  }
  const unknown = run.fal_requests.find((item) => item.request_id == null && item.status === "SUBMISSION_STATUS_UNKNOWN");
  if (unknown) return blocked("SUBMISSION_STATUS_UNKNOWN", requestSummary(run));
  const pending = unresolvedRequest(run);
  if (pending) {
    const module = await importClient(args["client-module"]);
    return queryRequest(module.fal, pending, workspace, run, runManifestPath);
  }
  const counts = modelCounts(run, manifest);
  if (
    (model === manifest.primary_model && counts.primary >= PRIMARY_LIMIT)
    || (model === manifest.fallback_model && counts.fallback >= FALLBACK_LIMIT)
  ) {
    return blocked("ATTEMPT_LIMIT_REACHED", requestSummary(run));
  }
  const worstCase = remainingWorstCase(run, manifest, prices);
  if (worstCase > Number(manifest.max_budget_usd) + 1e-12) {
    const reason = model === manifest.fallback_model
      ? "FALLBACK_EXCEEDS_REMAINING_BUDGET"
      : "WORST_CASE_EXCEEDS_BUDGET";
    return blocked(reason, {
      ...requestSummary(run),
      worst_case_usd: worstCase,
      max_budget_usd: Number(manifest.max_budget_usd),
    });
  }
  const module = await importClient(args["client-module"]);
  const client = module.fal;

  const attempt = run.fal_requests.length + 1;
  const estimate = model === manifest.primary_model ? prices.primary : prices.fallback;
  let submission;
  try {
    submission = await client.queue.submit(model, { input: requestInput.input });
  } catch {
    const request = {
      attempt,
      model,
      estimated_cost_usd: estimate,
      request_id: null,
      submitted_at: new Date().toISOString(),
      status: "SUBMISSION_STATUS_UNKNOWN",
      result_path: null,
    };
    run.fal_requests.push(request);
    persistRun(runManifestPath, run);
    return blocked("SUBMISSION_STATUS_UNKNOWN", requestSummary(run));
  }
  const requestId = submission?.request_id ?? submission?.requestId;
  if (typeof requestId !== "string" || requestId.length === 0) {
    const request = {
      attempt,
      model,
      estimated_cost_usd: estimate,
      request_id: null,
      submitted_at: new Date().toISOString(),
      status: "SUBMISSION_STATUS_UNKNOWN",
      result_path: null,
    };
    run.fal_requests.push(request);
    persistRun(runManifestPath, run);
    return blocked("SUBMISSION_STATUS_UNKNOWN", requestSummary(run));
  }
  const request = {
    attempt,
    model,
    estimated_cost_usd: estimate,
    request_id: requestId,
    submitted_at: new Date().toISOString(),
    status: "SUBMITTED",
    result_path: null,
  };
  run.fal_requests.push(request);
  persistRun(runManifestPath, run);
  return queryRequest(client, request, workspace, run, runManifestPath);
}


async function main() {
  try {
    const args = parseArgs(process.argv.slice(2));
    const manifest = readJson(args.manifest);
    if (args.mode === "real") {
      const payload = await runReal(args, manifest);
      process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
      return payload.state === "BLOCKED" ? 2 : 0;
    }
    if (args.mode !== "fixture") throw new Error("mode must be fixture or real");
    const fixture = readJson(args.fixture);
    const payload = simulate(manifest, fixture);
    writeOutput(args.output, manifest.project_root, payload);
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return payload.state === "BLOCKED" ? 2 : 0;
  } catch (error) {
    const reason = String(error?.message || error);
    const payload = reason.startsWith("RESULT_DOWNLOAD_FAILED")
      ? blocked("RESULT_DOWNLOAD_FAILED")
      : blocked("INVALID_INPUT", { error: reason });
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
    return 2;
  }
}


process.exitCode = await main();
