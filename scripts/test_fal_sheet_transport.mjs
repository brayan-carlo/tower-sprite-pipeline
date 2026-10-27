import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";

import { runCommand } from "./fal_sheet_transport.mjs";
import { readMp4Metadata } from "./sheet_video_metadata.mjs";

const NANO = "fal-ai/nano-banana-2/edit";
const CLEANUP = "fal-ai/ideogram/remove-background";
const WAN = "fal-ai/wan-motion";
const DREAM = "fal-ai/bytedance/dreamactor/v2";
const PUBLIC_URL = "https://fal.ai/models/fal-ai/nano-banana-2/edit";

function sha(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function png(width = 1024, height = 1024) {
  const bytes = Buffer.alloc(33);
  Buffer.from("89504e470d0a1a0a", "hex").copy(bytes);
  bytes.writeUInt32BE(13, 8);
  bytes.write("IHDR", 12, "ascii");
  bytes.writeUInt32BE(width, 16);
  bytes.writeUInt32BE(height, 20);
  bytes[24] = 8;
  bytes[25] = 6;
  return bytes;
}

function box(type, payload, extended = false) {
  const header = extended ? 16 : 8;
  const result = Buffer.alloc(header + payload.length);
  result.writeUInt32BE(extended ? 1 : result.length, 0);
  result.write(type, 4, "ascii");
  if (extended) result.writeBigUInt64BE(BigInt(result.length), 8);
  payload.copy(result, header);
  return result;
}

function syntheticMp4({ duration = 2, width = 592, height = 800, version = 0, extended = false, tkhdTrailing = null, mvhdTrailing = 0 } = {}) {
  const ftyp = box("ftyp", Buffer.from("69736f6d0000020069736f6d69736f32", "hex"), extended);
  const mvhd = Buffer.alloc((version === 0 ? 100 : 112) + mvhdTrailing);
  mvhd[0] = version;
  mvhd.writeUInt32BE(1000, version === 0 ? 12 : 20);
  if (version === 0) mvhd.writeUInt32BE(Math.round(duration * 1000), 16);
  else mvhd.writeBigUInt64BE(BigInt(Math.round(duration * 1000)), 24);
  const tkhd = Buffer.alloc((version === 0 ? 84 : 96) + (tkhdTrailing?.length ?? 0));
  tkhd[0] = version;
  tkhd.writeUInt32BE(1, version === 0 ? 12 : 20);
  const dimensionOffset = version === 0 ? 76 : 88;
  tkhd.writeUInt32BE(Math.round(width * 65536), dimensionOffset);
  tkhd.writeUInt32BE(Math.round(height * 65536), dimensionOffset + 4);
  if (tkhdTrailing) tkhdTrailing.copy(tkhd, dimensionOffset + 8);
  const hdlr = Buffer.alloc(20);
  hdlr.write("vide", 8, "ascii");
  const trak = box("trak", Buffer.concat([box("tkhd", tkhd, extended), box("mdia", box("hdlr", hdlr, extended), extended)]), extended);
  return Buffer.concat([ftyp, box("moov", Buffer.concat([box("mvhd", mvhd, extended), trak]), extended)]);
}

function jsonResponse(value, init = {}) {
  return new Response(JSON.stringify(value), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
}

function publicHtml(base = "0.08", multiplier = "1.5", extra = "") {
  return `<!doctype html><html><head><link rel="canonical" href="${PUBLIC_URL}"></head><body>${extra}<p>Your request will cost $${base} per image. 2K and 4K outputs will be charged at ${multiplier} times and 2 times the standard rate, respectively.</p></body></html>`;
}

function inputFor(model) {
  if (model === NANO) return {
    prompt: "walk left as one horizontal sheet", resolution: "2K", aspect_ratio: "16:9",
    num_images: 1, output_format: "png", limit_generations: true, enable_web_search: false,
  };
  if (model === CLEANUP) return { sync_mode: false };
  if (model === WAN) return {
    prompt: "follow the guide", adapt_motion: true, enhance_identity: false,
    acceleration: "regular", enable_safety_checker: true,
  };
  return { trim_first_second: true };
}

function unitPrice(model) {
  if (model === WAN) return 0.06;
  if (model === DREAM) return 0.05;
  if (model === CLEANUP) return 0.01;
  return 0.08;
}

async function fixture(model = NANO, { maxCost = 0.5, sourceDimensions, duration = 2 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fal-sheet-"));
  const dims = sourceDimensions ?? (model === DREAM ? [592, 800] : [1024, 1024]);
  const source = png(...dims);
  const sourcePath = path.join(dir, "source.png");
  fs.writeFileSync(sourcePath, source);
  const config = {
    schema_version: 3,
    model,
    source: { path: sourcePath, sha256: sha(source) },
    input: inputFor(model),
    authorization: { request_authorized: true, scope_sha256: "a".repeat(64) },
    max_cost_usd: maxCost,
  };
  let guide = null;
  if (model === WAN || model === DREAM) {
    guide = syntheticMp4({ duration });
    const guidePath = path.join(dir, "guide.mp4");
    fs.writeFileSync(guidePath, guide);
    config.driving_video = { path: guidePath, sha256: sha(guide) };
  }
  fs.writeFileSync(path.join(dir, "config.json"), `${JSON.stringify(config, null, 2)}\n`);
  return { dir, config, source, guide };
}

function apiPricing(model, overrides = {}) {
  return { prices: [{ endpoint_id: model, currency: "USD", unit: model === NANO || model === CLEANUP ? "image" : "seconds", unit_price: unitPrice(model), ...overrides }] };
}

async function price(context, { now = 1_800_000_000_000, api = apiPricing(context.config.model), html = publicHtml() } = {}) {
  const calls = [];
  const result = await runCommand("pricing", context.dir, {
    key: "test-key", now: () => now,
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      if (String(url).startsWith("https://api.fal.ai/v1/models/pricing?")) return jsonResponse(api);
      if (String(url) === PUBLIC_URL) return new Response(html, { status: 200 });
      throw new Error("unexpected URL");
    },
  });
  return { result, calls };
}

function state(dir) {
  return JSON.parse(fs.readFileSync(path.join(dir, "job-state.json"), "utf8"));
}

function receiptNames(dir) {
  return fs.readdirSync(path.join(dir, "pricing-receipts")).filter((name) => name.endsWith(".receipt.json"));
}

test("MP4 metadata parser reads duration and video dimensions and rejects ambiguity", () => {
  assert.deepEqual(readMp4Metadata(syntheticMp4()), { duration_seconds: 2, width: 592, height: 800 });
  assert.deepEqual(readMp4Metadata(syntheticMp4({ version: 1 })), { duration_seconds: 2, width: 592, height: 800 });
  assert.deepEqual(readMp4Metadata(syntheticMp4({ extended: true })), { duration_seconds: 2, width: 592, height: 800 });
  const appendedDimensions = Buffer.alloc(8);
  appendedDimensions.writeUInt32BE(592 * 65536, 0);
  appendedDimensions.writeUInt32BE(800 * 65536, 4);
  assert.throws(() => readMp4Metadata(syntheticMp4({ width: 400, height: 400, tkhdTrailing: appendedDimensions })), /MP4_METADATA_INVALID/);
  assert.throws(() => readMp4Metadata(syntheticMp4({ mvhdTrailing: 8 })), /MP4_METADATA_INVALID/);
  const twoTracks = syntheticMp4();
  const moov = twoTracks.subarray(twoTracks.indexOf(Buffer.from("moov")) + 4);
  assert.throws(() => readMp4Metadata(Buffer.concat([twoTracks, box("moov", moov)])), /MP4_METADATA_INVALID/);
  assert.throws(() => readMp4Metadata(Buffer.from("not an mp4")), /MP4_METADATA_INVALID/);
});

test("pricing archives a fresh Nano 2K receipt with public evidence and credentials only on API", async () => {
  const context = await fixture();
  const { result, calls } = await price(context);
  assert.equal(result.state, "PRICED");
  assert.equal(result.estimated_cost_usd, 0.12);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.headers.Authorization, "Key test-key");
  assert.equal(calls[1].init.headers?.Authorization, undefined);
  assert.ok(calls.every((call) => call.init.redirect === "error"));
  const receipts = receiptNames(context.dir);
  assert.equal(receipts.length, 1);
  const receipt = JSON.parse(fs.readFileSync(path.join(context.dir, "pricing-receipts", receipts[0]), "utf8"));
  assert.equal(receipt.resolution_multiplier, 1.5);
  assert.equal(receipt.effective_unit_price_usd, 0.12);
  assert.equal(receipt.sources.length, 2);
  assert.equal(fs.readFileSync(path.join(context.dir,"pricing-receipts",receipt.sources[0].archive_file),"utf8"),JSON.stringify(apiPricing(NANO)));
  assert.equal(fs.readFileSync(path.join(context.dir,"pricing-receipts",receipt.sources[1].archive_file),"utf8"),publicHtml());
});

test("pricing rejects conflicting public base, multiplier, hidden fake, duplicate API selection and oversized body", async (t) => {
  for (const [name, options] of [
    ["base", { html: publicHtml("0.09") }],
    ["multiplier", { html: publicHtml("0.08", "2") }],
    ["hidden", { html: `<!doctype html><html><head><link rel="canonical" href="${PUBLIC_URL}"></head><body><script>${publicHtml()}</script><p hidden>Your request will cost $0.08 per image. 2K and 4K outputs will be charged at 1.5 times and 2 times the standard rate, respectively.</p></body></html>` }],
    ["duplicate", { api: { prices: [apiPricing(NANO).prices[0], apiPricing(NANO).prices[0]] } }],
    ["api multiplier conflict", { api: apiPricing(NANO, { resolution_multipliers: { "2K": 2 } }) }],
  ]) {
    await t.test(name, async () => {
      const context = await fixture();
      const { result } = await price(context, options);
      assert.equal(result.state, "BLOCKED");
      assert.equal(result.block_reason, "PRICE_NOT_VERIFIABLE");
    });
  }
  const context = await fixture();
  let calls = 0;
  const result = await runCommand("pricing", context.dir, { key: "test-key", fetchImpl: async () => {
    calls += 1;
    return new Response("{}", { headers: { "content-length": String(6 * 1024 * 1024) } });
  }});
  assert.equal(calls, 1);
  assert.equal(result.block_reason, "PRICE_FETCH_FAILED");
});

test("missing key blocks before every HTTP request", async () => {
  const context = await fixture();
  let calls = 0;
  const result = await runCommand("pricing", context.dir, { key: "", fetchImpl: async () => { calls += 1; } });
  assert.equal(result.state, "KEY_SETUP_REQUIRED");
  assert.equal(calls, 0);
  const execute = await fixture();
  await price(execute);
  const executeResult = await runCommand("execute", execute.dir, { key: "", fetchImpl: async () => { calls += 1; } });
  assert.equal(executeResult.state, "KEY_SETUP_REQUIRED");
  assert.equal(calls, 0);
  assert.equal(fs.existsSync(path.join(execute.dir, "submission.claim")), false);
});

test("cleanup pricing uses the image unit without a public multiplier", async () => {
  const context = await fixture(CLEANUP);
  const priced = await price(context);
  assert.equal(priced.result.estimated_cost_usd, 0.01);
  assert.equal(priced.calls.length, 1);
  const receipt = JSON.parse(fs.readFileSync(path.join(context.dir, "pricing-receipts", receiptNames(context.dir)[0])));
  assert.equal(receipt.resolution_multiplier, null);
});

test("timeout without id never posts again", async () => {
  const context = await fixture();
  await price(context);
  let posts = 0;
  const fake = async (_url, init) => {
    if (init?.method === "POST") { posts += 1; throw new Error("secret body"); }
    throw new Error("unexpected GET");
  };
  await runCommand("execute", context.dir, { fetchImpl: fake, key: "test-key", now: () => 1_800_000_000_001 });
  await runCommand("execute", context.dir, { fetchImpl: fake, key: "test-key", now: () => 1_800_000_000_002 });
  assert.equal(posts, 1);
  assert.equal(state(context.dir).state, "SUBMISSION_STATUS_UNKNOWN");
});

test("two concurrent first executes create one durable claim and one POST", async () => {
  const context = await fixture();
  await price(context);
  let posts = 0;
  const fake = async (_url, init) => {
    if (init?.method === "POST") { posts += 1; await new Promise((resolve) => setTimeout(resolve, 10)); return jsonResponse({ request_id: "req_concurrent" }); }
    return jsonResponse({ status: "IN_QUEUE" });
  };
  const results = await Promise.all([
    runCommand("execute", context.dir, { fetchImpl: fake, key: "test-key", now: () => 1_800_000_000_003 }),
    runCommand("execute", context.dir, { fetchImpl: fake, key: "test-key", now: () => 1_800_000_000_003 }),
  ]);
  assert.equal(posts, 1);
  assert.ok(results.some((item) => item.state === "IN_QUEUE"));
  assert.ok(results.some((item) => item.state === "SUBMISSION_STATUS_UNKNOWN" || item.state === "IN_QUEUE"));
  const receiptBytes=fs.readFileSync(path.join(context.dir,"pricing-receipts",receiptNames(context.dir)[0]));
  assert.equal(JSON.parse(fs.readFileSync(path.join(context.dir,"submission.claim"))).pricing_receipt_sha256,sha(receiptBytes));
  assert.equal(state(context.dir).pricing_receipt_sha256,sha(receiptBytes));
  for (const file of ["submission.claim", "job-state.json"]) {
    assert.equal(fs.readFileSync(path.join(context.dir, file), "utf8").includes("data:"), false);
    assert.equal(fs.readFileSync(path.join(context.dir, file), "utf8").includes("test-key"), false);
  }
});

test("two OS processes competing on one job still permit exactly one POST", async () => {
  const context=await fixture();await price(context);const counter=path.join(context.dir,"post-count.txt");
  const moduleUrl=new URL("./fal_sheet_transport.mjs",import.meta.url).href;
  const code=`import fs from "node:fs";import {runCommand} from ${JSON.stringify(moduleUrl)};const [dir,counter]=process.argv.slice(1);const fake=async(_url,init={})=>{if(init.method==="POST"){fs.appendFileSync(counter,"P");await new Promise(r=>setTimeout(r,100));return new Response(JSON.stringify({request_id:"req_process"}),{headers:{"content-type":"application/json"}});}return new Response(JSON.stringify({status:"IN_QUEUE"}),{headers:{"content-type":"application/json"}});};console.log(JSON.stringify(await runCommand("execute",dir,{key:"test-key",now:()=>1800000000001,fetchImpl:fake})));`;
  const run=()=>new Promise((resolve,reject)=>{const child=spawn(process.execPath,["--input-type=module","-e",code,context.dir,counter],{stdio:["ignore","pipe","pipe"]});let stdout="";let stderr="";child.stdout.on("data",chunk=>stdout+=chunk);child.stderr.on("data",chunk=>stderr+=chunk);child.on("error",reject);child.on("close",status=>status===0?resolve(JSON.parse(stdout)):reject(new Error(stderr||`exit ${status}`)));});
  const results=await Promise.all([run(),run()]);
  assert.equal(fs.readFileSync(counter,"utf8"),"P");
  assert.ok(results.some(item=>item.state==="IN_QUEUE"));
  assert.ok(results.some(item=>item.state==="SUBMISSION_STATUS_UNKNOWN"||item.state==="IN_QUEUE"));
});

test("known request resumes with GET only and downloads an authenticated-free PNG", async () => {
  const context = await fixture();
  await price(context);
  let posts = 0;
  let statusCount = 0;
  const fake = async (url, init = {}) => {
    const value = String(url);
    assert.equal(init.redirect, "error");
    if (init.method === "POST") { posts += 1; return jsonResponse({ request_id: "req_resume" }); }
    if (value.endsWith("/status?logs=0")) { statusCount += 1; return jsonResponse({ status: statusCount === 1 ? "IN_QUEUE" : "COMPLETED" }); }
    if (value.endsWith("/requests/req_resume")) return jsonResponse({ images: [{ url: "https://cdn.fal.media/out.png" }] });
    if (value === "https://cdn.fal.media/out.png") {
      assert.equal(init.headers?.Authorization, undefined);
      return new Response(png(2048, 1024), { status: 200 });
    }
    throw new Error(`unexpected ${value}`);
  };
  assert.equal((await runCommand("execute", context.dir, { fetchImpl: fake, key: "test-key", now: () => 1_800_000_000_004 })).state, "IN_QUEUE");
  const complete = await runCommand("execute", context.dir, { fetchImpl: fake, key: "test-key", now: () => 1_900_000_000_000 });
  assert.equal(posts, 1);
  assert.equal(complete.state, "DOWNLOADED");
  assert.equal(path.basename(complete.raw_path), "raw.png");
  assert.equal(complete.raw_sha256, sha(png(2048, 1024)));
});

test("source, config, and downloaded raw tampering block without POST", async (t) => {
  await t.test("source", async () => {
    const context = await fixture(); await price(context); fs.appendFileSync(context.config.source.path, "x");
    let posts = 0; const result = await runCommand("execute", context.dir, { key: "test-key", fetchImpl: async (_u, i) => { if (i?.method === "POST") posts += 1; } });
    assert.equal(result.block_reason, "SOURCE_INVALID"); assert.equal(posts, 0);
  });
  await t.test("config", async () => {
    const context = await fixture(); await price(context); const config = { ...context.config, max_cost_usd: 0.49 }; fs.writeFileSync(path.join(context.dir, "config.json"), JSON.stringify(config));
    let posts = 0; const result = await runCommand("execute", context.dir, { key: "test-key", fetchImpl: async (_u, i) => { if (i?.method === "POST") posts += 1; } });
    assert.equal(result.block_reason, "PRICE_RECEIPT_INCOMPATIBLE"); assert.equal(posts, 0);
  });
  await t.test("raw", async () => {
    const context = await fixture(); await price(context);
    let status = 0; const fake = async (url, init = {}) => { const u=String(url); if(init.method==="POST") return jsonResponse({request_id:"req_raw"}); if(u.endsWith("status?logs=0")){status+=1;return jsonResponse({status:"COMPLETED"});} if(u.endsWith("req_raw"))return jsonResponse({images:[{url:"https://fal.media/raw.png"}]}); return new Response(png(),{status:200}); };
    await runCommand("execute", context.dir, { key:"test-key", fetchImpl:fake, now:()=>1_800_000_000_010 });
    fs.appendFileSync(path.join(context.dir,"raw.png"),"tamper");
    const result=await runCommand("execute", context.dir,{key:"test-key",fetchImpl:async()=>{throw new Error("offline");}});
    assert.equal(result.block_reason,"RAW_TAMPERED");
  });
});

test("expired receipt blocks first submit, refresh restores eligibility, and explicit cap is enforced", async () => {
  const expired = await fixture(); await price(expired, { now: 1_800_000_000_000 });
  let posts = 0;
  let result = await runCommand("execute", expired.dir, { key:"test-key", now:()=>1_800_000_901_000, fetchImpl:async(_u,i)=>{if(i?.method==="POST")posts+=1;} });
  assert.equal(result.block_reason,"PRICE_STALE"); assert.equal(posts,0);
  await price(expired,{now:1_800_000_901_001});
  assert.equal(receiptNames(expired.dir).length,2);
  assert.equal(fs.readdirSync(path.join(expired.dir,"pricing-receipts")).length,6);
  result = await runCommand("execute", expired.dir,{key:"test-key",now:()=>1_800_000_901_002,fetchImpl:async(_u,i)=>i?.method==="POST"?jsonResponse({request_id:"req_refreshed"}):jsonResponse({status:"IN_QUEUE"})});
  assert.equal(result.state,"IN_QUEUE");
  const capped=await fixture(NANO,{maxCost:0.11}); await price(capped);
  result=await runCommand("execute",capped.dir,{key:"test-key",now:()=>1_800_000_000_001,fetchImpl:async()=>{posts+=1;}});
  assert.equal(result.block_reason,"COST_CAP_EXCEEDED");
});

test("coherently tampered receipt economics cannot bypass the configured cost cap", async () => {
  const context=await fixture(NANO,{maxCost:0.05});await price(context);
  const receiptPath=path.join(context.dir,"pricing-receipts",receiptNames(context.dir)[0]);
  const receipt=JSON.parse(fs.readFileSync(receiptPath));
  receipt.base_unit_price_usd=0.001;receipt.effective_unit_price_usd=0.0015;receipt.estimated_cost_usd=0.0015;
  receipt.sources[0].selected.unit_price_usd=0.001;receipt.sources[1].selected.base_unit_price_usd=0.001;
  fs.writeFileSync(receiptPath,JSON.stringify(receipt));
  let posts=0;const result=await runCommand("execute",context.dir,{key:"test-key",now:()=>1_800_000_000_001,fetchImpl:async(_url,init={})=>{if(init.method==="POST")posts+=1;return jsonResponse({request_id:"req_tamper"});}});
  assert.equal(result.block_reason,"PRICE_RECEIPT_INVALID");assert.equal(posts,0);
});

test("direct API and public pricing archive-body tampering blocks with zero POST", async (t) => {
  for (const [name,sourceIndex] of [["api",0],["public",1]]) await t.test(name,async()=>{
    const context=await fixture(NANO,{maxCost:0.5});await price(context);
    const receipt=JSON.parse(fs.readFileSync(path.join(context.dir,"pricing-receipts",receiptNames(context.dir)[0])));
    fs.appendFileSync(path.join(context.dir,"pricing-receipts",receipt.sources[sourceIndex].archive_file),"x");
    let posts=0;
    const result=await runCommand("execute",context.dir,{key:"test-key",now:()=>1_800_000_000_001,fetchImpl:async(_url,init={})=>{if(init.method==="POST")posts+=1;throw new Error("HTTP must not run");}});
    assert.equal(result.block_reason,"PRICE_RECEIPT_INVALID");
    assert.equal(posts,0);
    assert.equal(fs.existsSync(path.join(context.dir,"submission.claim")),false);
  });
});

test("submit validates ids and returned queue URLs, and definite 4xx never resubmits", async (t) => {
  for (const payload of [{ request_id: "bad/id" }]) await t.test(JSON.stringify(payload), async () => {
    const context=await fixture(); await price(context); let posts=0;
    const first=await runCommand("execute",context.dir,{key:"test-key",now:()=>1_800_000_000_001,fetchImpl:async(_u,i)=>{if(i?.method==="POST"){posts+=1;return jsonResponse(payload);}throw new Error("no get");}});
    const second=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:async()=>{posts+=100;}});
    assert.equal(first.state,"SUBMISSION_STATUS_UNKNOWN"); assert.equal(second.state,"SUBMISSION_STATUS_UNKNOWN"); assert.equal(posts,1);
  });
  const rejected=await fixture(); await price(rejected); let posts=0;
  const fake=async(_u,i)=>{if(i?.method==="POST"){posts+=1;return jsonResponse({error:"secret"},{status:400});}throw new Error("no get");};
  assert.equal((await runCommand("execute",rejected.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001})).state,"REMOTE_REJECTED");
  assert.equal((await runCommand("execute",rejected.dir,{key:"test-key",fetchImpl:fake})).state,"REMOTE_REJECTED");
  assert.equal(posts,1);
});

test("valid request ID is durable for every unsafe optional queue URL", async (t) => {
  const cases=[
    ["status_url host",{status_url:"https://evil.example/req_recoverable/status"}],
    ["response_url wrong request",{response_url:"https://queue.fal.run/fal-ai/nano-banana-2/requests/wrong"}],
    ["result_url wrong path",{result_url:"https://queue.fal.run/fal-ai/nano-banana-2/other/req_recoverable"}],
  ];
  for(const [name,optional] of cases) await t.test(name,async()=>{
    const context=await fixture();await price(context);let posts=0;let firstGets=0;let secondGets=0;
    const first=await runCommand("execute",context.dir,{key:"test-key",now:()=>1_800_000_000_001,fetchImpl:async(_url,init={})=>{if(init.method==="POST"){posts+=1;return jsonResponse({request_id:"req_recoverable",...optional});}firstGets+=1;throw new Error("first call must stop before GET");}});
    assert.equal(first.state,"BLOCKED");assert.equal(first.request_id,"req_recoverable");assert.equal(first.retry_safe,true);
    assert.equal(first.block_reason,"AUXILIARY_QUEUE_URL_INVALID");assert.equal(firstGets,0);
    assert.equal(state(context.dir).request_id,"req_recoverable");
    const second=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:async(url,init={})=>{assert.notEqual(init.method,"POST");secondGets+=1;assert.equal(String(url),"https://queue.fal.run/fal-ai/nano-banana-2/requests/req_recoverable/status?logs=0");return jsonResponse({status:"IN_QUEUE"});}});
    assert.equal(second.state,"IN_QUEUE");assert.equal(posts,1);assert.equal(secondGets,1);
  });
});

test("result rejects malformed output URLs, download redirects, query credentials and non-PNG", async (t) => {
  for (const [name, outputUrl, download] of [
    ["host", "https://example.com/out.png", null],
    ["credentials", "https://fal.media/out.png?token=secret", null],
    ["redirect", "https://fal.media/out.png", new Response(png(), { status: 200 })],
    ["signature", "https://fal.media/out.png", new Response("not png", { status: 200 })],
  ]) await t.test(name, async () => {
    const context=await fixture(); await price(context);
    const fake=async(url,init={})=>{const u=String(url);if(init.method==="POST")return jsonResponse({request_id:"req_output"});if(u.endsWith("status?logs=0"))return jsonResponse({status:"COMPLETED"});if(u.endsWith("req_output"))return jsonResponse({images:[{url:outputUrl}]});if(download){Object.defineProperty(download,"url",{value:"https://redirected.fal.media/out.png"});return download;}throw new Error("download must not happen");};
    const result=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001});
    assert.equal(result.state,"BLOCKED");
    assert.ok(["RESULT_URL_NOT_ALLOWED","DOWNLOAD_REDIRECTED","RESULT_NOT_PNG"].includes(result.block_reason));
  });
});

test("status, result, and download response limits are enforced", async (t) => {
  for (const stage of ["status","result","download"]) await t.test(stage,async()=>{
    const context=await fixture();await price(context);
    const fake=async(url,init={})=>{const u=String(url);if(init.method==="POST")return jsonResponse({request_id:`req_big_${stage}`});if(u.endsWith("status?logs=0")){if(stage==="status")return new Response("{}",{headers:{"content-length":String(6*1024*1024)}});return jsonResponse({status:"COMPLETED"});}if(u.includes(`/requests/req_big_${stage}`)){if(stage==="result")return new Response("{}",{headers:{"content-length":String(6*1024*1024)}});return jsonResponse({images:[{url:"https://fal.media/out.png"}]});}return new Response("",{headers:{"content-length":String(51*1024*1024)}});};
    const result=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001});
    assert.equal(result.state,"BLOCKED");
    assert.ok(["REMOTE_STATUS_INVALID","REMOTE_RESULT_INVALID","DOWNLOAD_FAILED"].includes(result.block_reason));
  });
});

test("interrupted download leaves resumable state and deterministic retry completes", async () => {
  const context=await fixture(); await price(context); let downloads=0;
  const fake=async(url,init={})=>{const u=String(url);if(init.method==="POST")return jsonResponse({request_id:"req_interrupt"});if(u.endsWith("status?logs=0"))return jsonResponse({status:"COMPLETED"});if(u.endsWith("req_interrupt"))return jsonResponse({images:[{url:"https://fal.media/out.png"}]});downloads+=1;if(downloads===1){return new Response(new ReadableStream({start(c){c.enqueue(png().subarray(0,10));c.error(new Error("interrupted"));}}));}return new Response(png());};
  const first=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001});
  assert.equal(first.state,"BLOCKED"); assert.equal(first.retry_safe,true);
  const second=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_900_000_000_000});
  assert.equal(second.state,"DOWNLOADED"); assert.equal(downloads,2);
});

test("guided models price conservative seconds and resume MP4 without another POST", async (t) => {
  for (const [model, expected] of [[WAN,0.18],[DREAM,0.15]]) await t.test(model, async()=>{
    const context=await fixture(model); const priced=await price(context); assert.equal(priced.result.estimated_cost_usd,expected);
    const receiptName=receiptNames(context.dir)[0]; const receipt=JSON.parse(fs.readFileSync(path.join(context.dir,"pricing-receipts",receiptName)));
    assert.equal(receipt.input_duration_seconds,2); assert.equal(receipt.reserved_units,3); assert.equal(receipt.driving_video_sha256,sha(context.guide));
    let posts=0;let statuses=0;const fake=async(url,init={})=>{const u=String(url);if(init.method==="POST"){posts+=1;return jsonResponse({request_id:`req_${model===WAN?"wan":"dream"}`});}if(u.endsWith("status?logs=0")){statuses+=1;return jsonResponse({status:statuses===1?"IN_QUEUE":"COMPLETED"});}if(u.includes("/requests/req_"))return jsonResponse({video:{url:"https://fal.media/out.mp4"}});return new Response(syntheticMp4());};
    assert.equal((await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001})).state,"IN_QUEUE");
    const done=await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_900_000_000_000});assert.equal(done.state,"DOWNLOADED");assert.equal(path.basename(done.raw_path),"raw.mp4");assert.equal(posts,1);
  });
});

test("guided pricing rejects non-seconds units and no-id submissions remain permanently unknown", async (t) => {
  for (const model of [WAN,DREAM]) await t.test(model,async()=>{
    const bad=await fixture(model);assert.equal((await price(bad,{api:apiPricing(model,{unit:"image"})})).result.block_reason,"PRICE_NOT_VERIFIABLE");
    const context=await fixture(model);await price(context);let posts=0;
    const fake=async(_url,init={})=>{if(init.method==="POST"){posts+=1;throw new Error("unknown");}throw new Error("no GET");};
    assert.equal((await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001})).state,"SUBMISSION_STATUS_UNKNOWN");
    assert.equal((await runCommand("execute",context.dir,{key:"test-key",fetchImpl:fake})).state,"SUBMISSION_STATUS_UNKNOWN");
    assert.equal(posts,1);
  });
});

test("guided validation catches guide hash, DreamActor dimensions, duration and output duration", async (t) => {
  const hash=await fixture(WAN); fs.appendFileSync(hash.config.driving_video.path,"x"); assert.equal((await price(hash)).result.block_reason,"DRIVING_VIDEO_INVALID");
  const dims=await fixture(DREAM,{sourceDimensions:[400,400]}); assert.equal((await price(dims)).result.block_reason,"SOURCE_INVALID");
  const duration=await fixture(WAN,{duration:11}); assert.equal((await price(duration)).result.block_reason,"DRIVING_VIDEO_INVALID");
  const output=await fixture(WAN);await price(output);const fake=async(url,init={})=>{const u=String(url);if(init.method==="POST")return jsonResponse({request_id:"req_long"});if(u.endsWith("status?logs=0"))return jsonResponse({status:"COMPLETED"});if(u.endsWith("req_long"))return jsonResponse({video:{url:"https://fal.media/out.mp4"}});return new Response(syntheticMp4({duration:31}));};
  const result=await runCommand("execute",output.dir,{key:"test-key",fetchImpl:fake,now:()=>1_800_000_000_001});assert.equal(result.block_reason,"RESULT_VIDEO_INVALID");
});

const H3 = "minimax/h3-max/image-to-video";
const H3_PUBLIC = `https://fal.ai/models/${H3}`;
function h3Html(base="0.0125", high="0.02", extra="") {
  return `<html><head><link rel="canonical" href="${H3_PUBLIC}"></head><body><p>Video costs $${base} per second at 480p, $${high} per second at 768p.</p>${extra}</body></html>`;
}
async function h3Fixture(end=true, cap=.5) {
  const c=await fixture(NANO,{maxCost:cap}); c.config.model=H3;
  c.config.input={prompt:"Walk left, alternate opposite leg contacts",duration:5,resolution:"768P",prompt_expansion_mode:"balanced",enable_safety_checker:true,sync_mode:false,end_image_same_as_start:end};
  fs.writeFileSync(path.join(c.dir,"config.json"),JSON.stringify(c.config)); return c;
}
async function h3Price(c,base=.0125,html=h3Html()) {
  return runCommand("pricing",c.dir,{key:"test-key",fetchImpl:async url=>String(url).startsWith("https://api.fal.ai/")?jsonResponse(apiPricing(H3,{unit_price:base})):new Response(html)});
}
test("H3 prices current and future paired visible rates and reserves six seconds",async()=>{
  for(const [base,high] of [[.0125,.02],[.05,.08]]) {
    const c=await h3Fixture(); const r=await h3Price(c,base,h3Html(String(base),String(high),"<p>Future regular prices: $0.05 at 480p and $0.08 at 768p.</p>"));
    assert.equal(r.state,"PRICED"); assert.equal(r.estimated_cost_usd,high*6);
  }
});
test("H3 submits native image only, strips internal flag and resumes canonical ID",async()=>{
  for(const end of [true,false]) {
    const c=await h3Fixture(end); assert.equal((await h3Price(c)).state,"PRICED"); let posts=0;
    const fetchImpl=async(url,init)=>{
      if(init.method==="POST") {posts++; const b=JSON.parse(init.body); assert.equal(b.end_image_same_as_start,undefined); assert.equal(b.video_url,undefined); assert.equal(b.image_url.startsWith("data:image/png;base64,"),true); assert.equal(b.end_image_url,end?b.image_url:undefined); assert.equal(Object.keys(b).length,end?8:7); return jsonResponse({request_id:"h3-real"});}
      assert.equal(String(url),"https://queue.fal.run/minimax/h3-max/requests/h3-real/status?logs=0"); return jsonResponse({status:"IN_PROGRESS"});
    };
    assert.equal((await runCommand("execute",c.dir,{key:"test-key",fetchImpl})).state,"IN_PROGRESS");
    assert.equal((await runCommand("execute",c.dir,{key:"test-key",fetchImpl})).state,"IN_PROGRESS"); assert.equal(posts,1);
  }
});
test("H3 hidden, conflicting, mismatched pricing fails before POST",async()=>{
  for(const html of [h3Html().replace("<p>","<p hidden>"),h3Html("0.02"),h3Html()+h3Html(),h3Html("0.0125","0.02","<p>Video costs $0.05 per second at 480p, $0.08 per second at 768p.</p>")]) {
    const c=await h3Fixture(); const r=await h3Price(c,.0125,html); assert.equal(r.state,"BLOCKED"); assert.equal(r.block_reason,"PRICE_NOT_VERIFIABLE"); assert.equal(fs.existsSync(path.join(c.dir,"submission.claim")),false);
  }
});
test("H3 archives are reparsed before cap gate and padded estimate is enforced",async()=>{
  for(const archive of ["api","public",null]) {
    const c=await h3Fixture(true,.10); assert.equal((await h3Price(c)).state,"PRICED");
    if(archive){const dir=path.join(c.dir,"pricing-receipts");const f=fs.readdirSync(dir).find(n=>n.endsWith(`.${archive}.body`));fs.appendFileSync(path.join(dir,f),"tamper");}
    let posts=0;const r=await runCommand("execute",c.dir,{key:"test-key",fetchImpl:async()=>{posts++;throw new Error("no");}});
    assert.equal(r.block_reason,archive?"PRICE_RECEIPT_INVALID":"COST_CAP_EXCEEDED");assert.equal(posts,0);
  }
});

test('new endpoint cannot be selected through legacy v3 single-source config',async()=>{
  const c=await fixture();c.config.model='fal-ai/wan-flf2v';fs.writeFileSync(path.join(c.dir,'config.json'),JSON.stringify(c.config));let requests=0;
  const result=await runCommand('pricing',c.dir,{key:'fake',fetchImpl:async()=>{requests++;}});assert.equal(result.block_reason,'CONFIG_INVALID');assert.equal(requests,0);
});
