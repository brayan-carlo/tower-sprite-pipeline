import base64
import copy
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

import generic_sprite_contract as contract
import generic_sprite_pipeline as pipeline


MODEL = "fal-ai/nano-banana-2/edit"
PRICING_URL = "https://api.fal.ai/v1/models/pricing"
PUBLIC_PRICING_URL = "https://fal.ai/models/fal-ai/nano-banana-2/edit"
PRICING_CORPUS = (
    Path(__file__).resolve().parent.parent
    / "assets/fixtures/generic-pricing-conformance.json"
)
C2_V1_BASELINE = (
    Path(__file__).resolve().parents[3]
    / "work/tower-sprite-pipeline/generic-png-sprite-generator-20260902/evidence/correction-c2-v1-baseline"
)
C4_REAL_SDK_RUNTIME = (
    Path(__file__).resolve().parents[3]
    / "work/tower-sprite-pipeline/generic-png-sprite-generator-20260902"
    / "runtimes/fal-1.10.1-20260904"
)
REQUEST_RECORD_BASE_KEYS = {
    "frame_index",
    "attempt",
    "model",
    "request_fingerprint",
    "request_id",
    "status",
    "estimated_cost_usd",
    "submitted_at",
    "result_path",
}


def required_external_file(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name}_REQUIRED")
    return Path(value).resolve(strict=True)


class FalGenericTransportTests(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parent / "fal_generic_generate.mjs"

    def setUp(self) -> None:
        self.node = required_external_file("TOWER_SPRITE_NODE_EXE")
        self.python = required_external_file("TOWER_SPRITE_PYTHON_EXE")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.workspace = self.root / "runs"
        self.source = self.inputs / "hero.png"
        self.write_character(self.source, (20, 40, 60, 255))
        self.movement = self.inputs / "movement.json"
        self.movement.write_text(
            json.dumps(
                [
                    {
                        "frame_index": 1,
                        "phase": "left-foot contact",
                        "body_motion": "left foot planted forward, right leg trailing",
                    },
                    {
                        "frame_index": 2,
                        "phase": "right-foot contact",
                        "body_motion": "right foot planted forward, left leg trailing",
                    },
                ]
            ),
            encoding="utf-8",
        )
        self.pricing = self.inputs / "pricing.json"
        self.write_json(self.pricing, self.valid_sanitized_pricing())
        self.events = self.root / "events.json"
        self.actions = self.root / "actions.log"
        self.client = self.root / "fake-client.mjs"
        self.client.write_text(
            textwrap.dedent(
                """
                import fs from "node:fs";
                import path from "node:path";

                const events = JSON.parse(fs.readFileSync(process.env.FAKE_EVENTS_PATH, "utf8"));
                const actionsPath = process.env.FAKE_ACTIONS_PATH;
                function canonical(value) {
                  if (Array.isArray(value)) return value.map(canonical);
                  if (value !== null && typeof value === "object") {
                    return Object.fromEntries(
                      Object.keys(value).sort().map((key) => [key, canonical(value[key])]),
                    );
                  }
                  return value;
                }
                function action(value) {
                  fs.appendFileSync(actionsPath, `${value}\n`, "utf8");
                }
                function next(operation, endpoint, options) {
                  const event = events.shift();
                  if (!event || event.operation !== operation) {
                    throw new Error(`expected ${operation}`);
                  }
                  if (event.endpoint !== undefined && event.endpoint !== endpoint) {
                    throw new Error("endpoint mismatch");
                  }
                  if (
                    event.input !== undefined
                    && JSON.stringify(canonical(event.input)) !== JSON.stringify(canonical(options?.input))
                  ) {
                    throw new Error("input mismatch");
                  }
                  if (event.delay_ms !== undefined) {
                    Atomics.wait(
                      new Int32Array(new SharedArrayBuffer(4)),
                      0,
                      0,
                      event.delay_ms,
                    );
                  }
                  if (event.throw) {
                    const error = new Error(event.message || "injected secret error");
                    error.name = event.error_name || "Error";
                    if (event.status !== undefined) error.status = event.status;
                    if (event.body !== undefined) error.body = event.body;
                    throw error;
                  }
                  return event.value;
                }
                if (process.env.FAKE_FETCH_MODE === "download-failure") {
                  globalThis.fetch = async () => {
                    throw new Error("temporary download failure");
                  };
                } else if (process.env.FAKE_FETCH_MODE === "declared-too-large") {
                  globalThis.fetch = async () => ({
                    ok: true,
                    url: "https://provider.example/result.png",
                    headers: { get: (name) => name.toLowerCase() === "content-length" ? "52428801" : null },
                    body: { getReader: () => ({ read: async () => ({ done: true }) }) },
                  });
                } else if (process.env.FAKE_FETCH_MODE === "stream-too-large") {
                  globalThis.fetch = async () => {
                    let sent = 0;
                    const chunk = new Uint8Array(1024 * 1024);
                    return {
                      ok: true,
                      url: "https://provider.example/result.png",
                      headers: { get: () => null },
                      body: {
                        getReader: () => ({
                          read: async () => sent++ < 51
                            ? { done: false, value: chunk }
                            : { done: true },
                        }),
                      },
                    };
                  };
                }
                action("import");
                export const fal = {
                  queue: {
                    async submit(endpoint, options) {
                      action("submit");
                      return next("submit", endpoint, options);
                    },
                    async status(endpoint, options) {
                      action(`status:${options.requestId}`);
                      return next("status", endpoint, options);
                    },
                    async result(endpoint, options) {
                      action(`result:${options.requestId}`);
                      const value = next("result", endpoint, options);
                      if (process.env.FAKE_PLANT_RAW_PATH) {
                        fs.mkdirSync(path.dirname(process.env.FAKE_PLANT_RAW_PATH), { recursive: true });
                        fs.writeFileSync(process.env.FAKE_PLANT_RAW_PATH, "LATE-SENTINEL");
                      }
                      if (
                        process.env.FAKE_PLANT_RAW_JUNCTION_PATH
                        && process.env.FAKE_PLANT_RAW_JUNCTION_TARGET
                      ) {
                        fs.symlinkSync(
                          process.env.FAKE_PLANT_RAW_JUNCTION_TARGET,
                          process.env.FAKE_PLANT_RAW_JUNCTION_PATH,
                          "junction",
                        );
                      }
                      if (process.env.FAKE_OVERSIZE_DATA_URI === "1") {
                        return {
                          data: {
                            images: [{
                              url: "data:image/png;base64," + "A".repeat(69905072),
                            }],
                          },
                        };
                      }
                      return value;
                    },
                  },
                };
                """
            ),
            encoding="utf-8",
        )
        self.png_bytes = self.png_payload((80, 100, 120, 255))
        self.png_uri = "data:image/png;base64," + base64.b64encode(
            self.png_bytes
        ).decode("ascii")
        self.run_dir, self.run_manifest, self.request_plan = self.prepare_authorized(
            "generic-run"
        )

    @staticmethod
    def write_character(path: Path, color: tuple[int, int, int, int]) -> None:
        image = Image.new("RGBA", (10, 12), (0, 0, 0, 0))
        image.paste(color, (2, 1, 8, 12))
        image.save(path)

    @staticmethod
    def png_payload(color: tuple[int, int, int, int]) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGBA", (5, 7), color).save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def generated_png_payload(
        color: tuple[int, int, int] = (31, 51, 71),
    ) -> bytes:
        buffer = io.BytesIO()
        image = Image.new("RGB", (10, 12), (231, 17, 199))
        image.paste(color, (2, 1, 8, 12))
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def generated_frame_payload(
        size: tuple[int, int],
        color: tuple[int, int, int],
        variant: str,
        *,
        transparent_background: bool = False,
    ) -> bytes:
        width, height = size
        if transparent_background:
            image = Image.new("RGBA", size, (0, 0, 0, 0))
            fill = (*color, 255)
        else:
            image = Image.new("RGB", size, (231, 17, 199))
            fill = color
        if variant == "narrow":
            image.paste(fill, (width // 4, 2, width - width // 4, height))
        elif variant == "wide":
            image.paste(fill, (width // 3, 3, width - width // 3, height))
            image.paste(fill, (width // 5, height // 3, width - width // 5, height // 2))
        else:
            raise ValueError("UNKNOWN_TEST_SILHOUETTE")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def structurally_valid_but_undecodable_png() -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
            b"IDAT", b"not-a-zlib-stream"
        ) + chunk(b"IEND", b"")

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def valid_sanitized_pricing(**changes: object) -> dict:
        value = {
            "model": MODEL,
            "source_url": PRICING_URL,
            "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "currency": "USD",
            "unit": "image",
            "base_unit_price_usd": "0.10",
            "resolution": "0.5K",
            "resolution_multiplier": "0.5",
        }
        value.update(changes)
        return value

    @staticmethod
    def valid_official_response(**changes: object) -> dict:
        value = {
            "source_url": PRICING_URL,
            "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "prices": [
                {
                    "endpoint_id": "fal-ai/other-model",
                    "unit_price": 1,
                    "unit": "image",
                    "currency": "USD",
                },
                {
                    "endpoint_id": MODEL,
                    "unit_price": "0.10",
                    "unit": "image",
                    "currency": "USD",
                    "resolution_multipliers": {"0.5K": 0.5, "1K": 1},
                },
            ],
        }
        value.update(changes)
        return value

    def prepare_authorized(
        self,
        run_id: str,
        *,
        animation: str = "running",
        direction: str = "east",
        pricing_path: Path | None = None,
        max_budget_usd: Decimal = Decimal("0.10"),
    ) -> tuple[Path, Path, Path]:
        run_dir = pipeline.prepare_run(
            source=self.source,
            movement_plan_path=self.movement,
            pricing_path=pricing_path or self.pricing,
            animation=animation,
            direction=direction,
            frame_count=2,
            frame_size=None,
            max_budget_usd=max_budget_usd,
            workspace_root=self.workspace,
            run_id=run_id,
        )
        run_manifest = run_dir / "reports/run_manifest.json"
        pipeline.authorize_run(run_manifest, max_budget_usd)
        request_plan = run_dir / "input/requests/frame-01.json"
        pipeline.next_request_plan(run_manifest, request_plan)
        return run_dir, run_manifest, request_plan

    def copy_historic_v1_partial(self, label: str) -> tuple[Path, Path, Path, Path]:
        fixture = C2_V1_BASELINE / "partial"
        destination_root = self.root / label
        destination_root.mkdir()
        run_dir = destination_root / "generic-run"
        shutil.copytree(fixture, run_dir)
        pricing = destination_root / "pricing.json"
        shutil.copy2(C2_V1_BASELINE / "pricing.json", pricing)
        return (
            run_dir,
            run_dir / "reports/run_manifest.json",
            run_dir / "input/requests/frame-02.json",
            pricing,
        )

    def fixed_clock_environment(self, now: str) -> dict[str, str]:
        launcher = self.root / "fixed-clock-launcher.mjs"
        launcher.write_text(
            textwrap.dedent(
                """
                import { pathToFileURL } from "node:url";
                const RealDate = globalThis.Date;
                const fixedNow = RealDate.parse(process.env.CONFORMANCE_NOW);
                if (!Number.isFinite(fixedNow)) throw new Error("TEST_CLOCK_INVALID");
                globalThis.Date = class extends RealDate {
                  constructor(...args) { super(...(args.length ? args : [fixedNow])); }
                  static now() { return fixedNow; }
                };
                const target = process.argv[2];
                process.argv = [process.execPath, target, ...process.argv.slice(3)];
                await import(pathToFileURL(target).href);
                """
            ),
            encoding="utf-8",
        )
        return {
            "CONFORMANCE_NOW": now,
            "CONFORMANCE_LAUNCHER": str(launcher),
        }

    def environment(self, key: str | None = "fake-key-never-persist") -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["NODE_ENV"] = "test"
        environment["FAKE_ROOT"] = str(self.root)
        environment["FAKE_EVENTS_PATH"] = str(self.events)
        environment["FAKE_ACTIONS_PATH"] = str(self.actions)
        environment["TOWER_SPRITE_PYTHON_EXE"] = str(self.python)
        if key is None:
            environment.pop("FAL_KEY", None)
        else:
            environment["FAL_KEY"] = key
        return environment

    @staticmethod
    def tree_hashes(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def real_sdk_adapter(self) -> tuple[Path, Path, dict[str, str]]:
        sdk = C4_REAL_SDK_RUNTIME / "node_modules/@fal-ai/client"
        package = self.read_json(sdk / "package.json")
        self.assertEqual("1.10.1", package["version"])
        before = self.tree_hashes(sdk)
        staging = Path(tempfile.mkdtemp(prefix="c4-adapter-", dir=C4_REAL_SDK_RUNTIME))
        self.addCleanup(shutil.rmtree, staging, True)
        adapter = staging / self.SCRIPT.name
        adapter.write_bytes(self.SCRIPT.read_bytes())
        for name in (
            "generic_sprite_pipeline.py",
            "generic_sprite_contract.py",
            "generic_sprite_images.py",
        ):
            source = self.SCRIPT.with_name(name)
            (staging / name).write_bytes(source.read_bytes())
        self.assertEqual(self.SCRIPT.read_bytes(), adapter.read_bytes())
        return adapter, sdk, before

    def write_real_sdk_preload(self, staging: Path, log: Path) -> Path:
        preload = staging / "offline-http-preload.cjs"
        preload.write_text(
            textwrap.dedent(
                r'''
                const fs = require("node:fs");
                const http = require("node:http");
                const https = require("node:https");
                const net = require("node:net");
                const tls = require("node:tls");
                const logPath = process.env.C4_HTTP_LOG;
                function record(value) {
                  fs.appendFileSync(logPath, JSON.stringify(value) + "\n", "utf8");
                }
                function metadata(input, init = {}) {
                  return {
                    kind: "fetch",
                    method: String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase(),
                    url: String(input instanceof Request ? input.url : input),
                    redirect: init.redirect ?? null,
                  };
                }
                const realSetTimeout = globalThis.setTimeout;
                globalThis.setTimeout = (callback, milliseconds, ...args) =>
                  realSetTimeout(callback, 0, ...args);
                globalThis.fetch = async (input, init = {}) => {
                  const request = metadata(input, init);
                  record(request);
                  if (process.env.C4_HTTP_MODE !== "resume") {
                    throw new TypeError("fetch failed");
                  }
                  const response = (body) => new Response(JSON.stringify(body), {
                    status: 200,
                    headers: {
                      "content-type": "application/json",
                      "x-fal-request-id": "real-sdk-resume-id",
                    },
                  });
                  if (request.method === "POST") {
                    return response({ request_id: "real-sdk-resume-id" });
                  }
                  if (request.url.endsWith("/status?logs=0")) {
                    const statusCalls = fs.readFileSync(logPath, "utf8")
                      .split("\n")
                      .filter((line) => line.includes("/status?logs=0")).length;
                    return response({
                      status: statusCalls === 1 ? "IN_QUEUE" : "COMPLETED",
                      request_id: "real-sdk-resume-id",
                    });
                  }
                  return response({ images: [{ url: process.env.C4_RESULT_URI }] });
                };
                const denyNetwork = () => {
                  record({ kind: "unexpected-network-entrypoint" });
                  throw new DOMException("NETWORK_DISABLED_BY_C4_TEST", "AbortError");
                };
                http.request = http.get = https.request = https.get = denyNetwork;
                net.connect = net.createConnection = tls.connect = denyNetwork;
                net.Socket.prototype.connect = denyNetwork;
                '''
            ),
            encoding="utf-8",
        )
        return preload

    def run_cli(
        self,
        arguments: list[str],
        *,
        expected_code: int,
        key: str | None = "fake-key-never-persist",
        extra_environment: dict[str, str] | None = None,
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        environment = self.environment(key)
        if extra_environment:
            environment.update(extra_environment)
        command = [str(self.node), str(self.SCRIPT), *arguments]
        if environment.get("CONFORMANCE_LAUNCHER"):
            command = [
                str(self.node),
                environment["CONFORMANCE_LAUNCHER"],
                str(self.SCRIPT),
                *arguments,
            ]
        completed = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(expected_code, completed.returncode, completed.stderr)
        return json.loads(completed.stdout), completed

    def run_live_pricing_http(
        self,
        output: Path,
        responses: list[dict],
        *,
        expected_code: int,
    ) -> tuple[dict, subprocess.CompletedProcess[str], list[dict]]:
        token = hashlib.sha256(str(output).encode("utf-8")).hexdigest()[:10]
        events = self.root / f"pricing-http-{token}.json"
        log = self.root / f"pricing-http-{token}.jsonl"
        preload = self.root / f"pricing-http-{token}.cjs"
        self.write_json(events, responses)
        preload.write_text(
            textwrap.dedent(
                r'''
                const fs = require("node:fs");
                const events = JSON.parse(fs.readFileSync(process.env.FAKE_PRICING_EVENTS, "utf8"));
                globalThis.fetch = async (input, options = {}) => {
                  const url = String(input);
                  fs.appendFileSync(process.env.FAKE_PRICING_LOG, JSON.stringify({
                    kind: "request",
                    url,
                    method: options.method ?? "GET",
                    redirect: options.redirect ?? null,
                    headers: options.headers ?? {},
                    has_signal: Boolean(options.signal),
                  }) + "\n", "utf8");
                  const event = events.shift();
                  if (!event) throw new Error("UNEXPECTED_NETWORK_REQUEST");
                  if (event.throw) throw new Error("NETWORK_DISABLED_BY_TEST");
                  const status = event.status ?? 200;
                  let body = event.body ?? "";
                  if (event.body_b64 !== undefined) body = Buffer.from(event.body_b64, "base64");
                  if (event.stream !== undefined) {
                    let chunks = 0;
                    body = new ReadableStream({
                      pull(controller) {
                        if (chunks >= event.stream.chunks) {
                          fs.appendFileSync(process.env.FAKE_PRICING_LOG, JSON.stringify({ kind: "eof", chunks }) + "\n", "utf8");
                          controller.close();
                          return;
                        }
                        chunks += 1;
                        fs.appendFileSync(process.env.FAKE_PRICING_LOG, JSON.stringify({ kind: "chunk", chunks }) + "\n", "utf8");
                        controller.enqueue(new Uint8Array(event.stream.chunk_size));
                      },
                      cancel() {
                        fs.appendFileSync(process.env.FAKE_PRICING_LOG, JSON.stringify({ kind: "cancel", chunks }) + "\n", "utf8");
                      },
                    });
                  }
                  const response = new Response(body, {
                    status,
                    headers: event.headers ?? {},
                  });
                  Object.defineProperty(response, "url", { value: event.response_url ?? url });
                  return response;
                };
                '''
            ),
            encoding="utf-8",
        )
        payload, completed = self.run_cli(
            ["pricing", "--source", PRICING_URL, "--output", str(output)],
            expected_code=expected_code,
            extra_environment={
                "NODE_OPTIONS": f"--require={preload}",
                "FAKE_PRICING_EVENTS": str(events),
                "FAKE_PRICING_LOG": str(log),
            },
        )
        trace = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
        ] if log.exists() else []
        self.pricing_http_trace = trace
        requests = [entry for entry in trace if entry.get("kind") == "request"]
        return payload, completed, requests

    @staticmethod
    def live_api_body(
        *,
        unit: str = "images",
        currency: str = "USD",
        base: object = 0.08,
        multipliers: object | None = None,
    ) -> str:
        selected = {
            "endpoint_id": MODEL,
            "unit_price": base,
            "unit": unit,
            "currency": currency,
        }
        if multipliers is not None:
            selected["resolution_multipliers"] = multipliers
        return json.dumps(
            {"prices": [selected], "next_cursor": None, "has_more": False},
            separators=(",", ":"),
        )

    @staticmethod
    def public_pricing_html(base: str = "0.08", multiplier: str = "0.75") -> str:
        return (
            "<html><head><link rel=\"canonical\" href=\""
            + PUBLIC_PRICING_URL
            + "\"></head><body><p>Your request will cost "
            f"<strong>${base}</strong> per image. 0.5K (512px) resolution outputs "
            f"will be charged at <strong>{multiplier}</strong> times the standard rate."
            "</p></body></html>"
        )

    def run_pipeline_cli(self, arguments, *, expected_code=0, plain=False):
        result = subprocess.run(
            [str(self.python), "-B", str(Path(pipeline.__file__)), *arguments],
            cwd=self.root,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(expected_code, result.returncode, result.stderr)
        return result.stdout.strip() if plain else json.loads(result.stdout)

    def execute(
        self,
        *,
        run_manifest: Path | None = None,
        request_plan: Path | None = None,
        pricing: Path | None = None,
        events: tuple[dict, ...] | list[dict] = (),
        expected_code: int = 0,
        key: str | None = "fake-key-never-persist",
        include_client: bool = True,
        client_module: Path | None = None,
        extra_environment: dict[str, str] | None = None,
    ) -> tuple[dict, list[str], subprocess.CompletedProcess[str]]:
        self.write_json(self.events, list(events))
        self.actions.unlink(missing_ok=True)
        arguments = [
            "execute",
            "--run-manifest",
            str(run_manifest or self.run_manifest),
            "--request-plan",
            str(request_plan or self.request_plan),
            "--pricing",
            str(pricing or self.pricing),
        ]
        if include_client:
            arguments.extend(("--client-module", str(client_module or self.client)))
        payload, completed = self.run_cli(
            arguments,
            expected_code=expected_code,
            key=key,
            extra_environment=extra_environment,
        )
        actions = (
            self.actions.read_text(encoding="utf-8").splitlines()
            if self.actions.exists()
            else []
        )
        return payload, actions, completed

    def assert_preimport_block(
        self,
        reason: str,
        *,
        run_manifest: Path | None = None,
        request_plan: Path | None = None,
        pricing: Path | None = None,
        key: str | None = "fake-key-never-persist",
    ) -> dict:
        target = run_manifest or self.run_manifest
        before = target.read_bytes()
        payload, actions, _ = self.execute(
            run_manifest=run_manifest,
            request_plan=request_plan,
            pricing=pricing,
            expected_code=2,
            key=key,
        )
        self.assertEqual(reason, payload["reason"])
        self.assertEqual([], actions)
        self.assertEqual(before, target.read_bytes())
        return payload

    def test_pricing_selects_one_model_and_writes_only_sanitized_exact_evidence(self) -> None:
        raw = self.inputs / "official-response.json"
        output = self.inputs / "sanitized.json"
        self.write_json(raw, self.valid_official_response(secret="must-not-survive"))

        payload, completed = self.run_cli(
            ["pricing", "--source", str(raw), "--output", str(output)],
            expected_code=0,
            key=None,
        )

        expected = {
            **self.valid_sanitized_pricing(
                verified_at=self.read_json(raw)["verified_at"]
            ),
            "evidence_mode": "OFFLINE_FIXTURE_NOT_FOR_LIVE_USE",
        }
        self.assertEqual(expected, self.read_json(output))
        self.assertEqual(
            {"state": "PRICING_FIXTURE_VALIDATED", "evidence": expected},
            payload,
        )
        audit = output.read_text(encoding="utf-8") + completed.stdout + completed.stderr
        self.assertNotIn("secret", audit)
        self.assertNotIn("prices", self.read_json(output))
        self.assertTrue(output.read_bytes().endswith(b"\n"))
        self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_live_pricing_composes_missing_multiplier_from_exact_public_page(self) -> None:
        output = self.inputs / "live-sanitized.json"
        receipt = Path(f"{output}.provenance.json")
        fetch_log = self.root / "pricing-fetch.jsonl"
        preload = self.root / "fake-pricing-fetch.cjs"
        api_response = json.dumps(
            {
                "prices": [
                    {
                        "endpoint_id": MODEL,
                        "unit_price": 0.08,
                        "unit": "images",
                        "currency": "USD",
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
            separators=(",", ":"),
        )
        public_response = (
            f"<html><head><link rel=\"canonical\" href=\"{PUBLIC_PRICING_URL}\">"
            "<style>.hidden{display:none}</style></head><body>"
            "<p>Your request will cost <strong>$0.08</strong> per image. "
            "For <strong>$1.00</strong>, you can run this model <strong>12</strong> times. "
            "2K and 4K outputs will be charged at <strong>1.5</strong> times and "
            "<strong>2</strong> times the standard rate, respectively. "
            "0.5K (512px) resolution outputs will be charged at "
            "<strong>0.75</strong> times the standard rate. "
            "<strong>Note: Pricing is subject to change.</strong></p>"
            "</body></html>"
        )
        preload.write_text(
            textwrap.dedent(
                f"""
                const fs = require("node:fs");
                globalThis.fetch = async (url, options) => {{
                  const requestUrl = String(url);
                  fs.appendFileSync(
                    process.env.FAKE_PRICING_FETCH_LOG,
                    JSON.stringify({{
                      url: requestUrl,
                      method: options?.method ?? "GET",
                      redirect: options?.redirect ?? null,
                      headers: options?.headers ?? {{}},
                      has_signal: Boolean(options?.signal),
                    }}) + "\\n",
                    "utf8",
                  );
                  if (requestUrl === {json.dumps(PRICING_URL + '?endpoint_id=fal-ai%2Fnano-banana-2%2Fedit')}) {{
                    const response = new Response({json.dumps(api_response)}, {{ status: 200 }});
                    Object.defineProperty(response, "url", {{ value: requestUrl }});
                    return response;
                  }}
                  if (requestUrl === {json.dumps(PUBLIC_PRICING_URL)}) {{
                    const response = new Response({json.dumps(public_response)}, {{ status: 200 }});
                    Object.defineProperty(response, "url", {{ value: requestUrl }});
                    return response;
                  }}
                  throw new Error("NETWORK_DISABLED_BY_TEST");
                }};
                """
            ),
            encoding="utf-8",
        )

        payload, _ = self.run_cli(
            ["pricing", "--source", PRICING_URL, "--output", str(output)],
            expected_code=0,
            extra_environment={
                "NODE_OPTIONS": f"--require={preload}",
                "FAKE_PRICING_FETCH_LOG": str(fetch_log),
            },
        )

        requests = [json.loads(line) for line in fetch_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(2, len(requests))
        api_request, public_request = requests
        self.assertEqual(f"{PRICING_URL}?endpoint_id=fal-ai%2Fnano-banana-2%2Fedit", api_request["url"])
        self.assertEqual(PUBLIC_PRICING_URL, public_request["url"])
        self.assertEqual("Key fake-key-never-persist", api_request["headers"]["Authorization"])
        self.assertNotIn("Authorization", public_request["headers"])
        self.assertTrue(api_request["has_signal"])
        self.assertTrue(public_request["has_signal"])
        self.assertEqual(["GET", "GET"], [request["method"] for request in requests])
        self.assertEqual(["error", "error"], [request["redirect"] for request in requests])
        expected_evidence = self.valid_sanitized_pricing(
            verified_at=self.read_json(receipt)["sources"][0]["retrieved_at"],
            base_unit_price_usd="0.08",
            resolution_multiplier="0.75",
        )
        self.assertEqual(expected_evidence, self.read_json(output))
        self.assertEqual(
            hashlib.sha256(output.read_bytes()).hexdigest(),
            self.read_json(receipt)["pricing_sha256"],
        )
        receipt_payload = self.read_json(receipt)
        self.assertEqual(1, receipt_payload["schema_version"])
        self.assertEqual(
            hashlib.sha256(api_response.encode("utf-8")).hexdigest(),
            receipt_payload["sources"][0]["response_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(public_response.encode("utf-8")).hexdigest(),
            receipt_payload["sources"][1]["response_sha256"],
        )
        self.assertEqual(
            {
                "model": MODEL,
                "currency": "USD",
                "unit": "images",
                "normalized_unit": "image",
                "base_unit_price_usd": "0.08",
            },
            receipt_payload["sources"][0]["selected"],
        )
        self.assertEqual(
            {
                "base_unit_price_usd": "0.08",
                "resolution": "0.5K",
                "resolution_multiplier": "0.75",
            },
            receipt_payload["sources"][1]["selected"],
        )
        self.assertEqual(
            hashlib.sha256(receipt.read_bytes()).hexdigest(),
            payload["provenance"]["sha256"],
        )
        self.assertEqual(str(receipt.resolve()), payload["provenance"]["path"])
        self.assertEqual("PRICING_VERIFIED", payload["state"])
        audit = output.read_text(encoding="utf-8") + receipt.read_text(encoding="utf-8")
        self.assertNotIn("fake-key-never-persist", audit)

    def test_live_api_multiplier_is_used_without_public_request(self) -> None:
        output = self.inputs / "live-api-multiplier.json"
        api_body = self.live_api_body(multipliers={"0.5K": 0.625, "1K": 1})
        payload, _, requests = self.run_live_pricing_http(
            output,
            [{"body": api_body}],
            expected_code=0,
        )
        self.assertEqual("0.625", payload["evidence"]["resolution_multiplier"])
        self.assertEqual("image", payload["evidence"]["unit"])
        self.assertEqual(1, len(requests))
        receipt = self.read_json(Path(f"{output}.provenance.json"))
        self.assertEqual(1, len(receipt["sources"]))
        self.assertEqual(
            "0.625",
            receipt["sources"][0]["selected"]["resolution_multiplier"],
        )

    def test_live_pricing_rejects_invalid_authenticated_entries_without_fallback(self) -> None:
        duplicate = json.loads(self.live_api_body())
        duplicate["prices"].append(copy.deepcopy(duplicate["prices"][0]))
        invalid_multiplier_object = json.loads(self.live_api_body())
        invalid_multiplier_object["prices"][0]["resolution_multipliers"] = []
        null_multiplier_object = json.loads(self.live_api_body())
        null_multiplier_object["prices"][0]["resolution_multipliers"] = None
        missing_selected = json.dumps(
            {"prices": [{"endpoint_id": "fal-ai/other", "unit_price": 1, "unit": "images", "currency": "USD"}]},
            separators=(",", ":"),
        )
        cases = (
            ("currency", self.live_api_body(currency="EUR")),
            ("unit", self.live_api_body(unit="megapixel")),
            ("missing", missing_selected),
            ("duplicate", json.dumps(duplicate, separators=(",", ":"))),
            ("multiplier-object", json.dumps(invalid_multiplier_object, separators=(",", ":"))),
            ("multiplier-null-object", json.dumps(null_multiplier_object, separators=(",", ":"))),
            ("multiplier-zero", self.live_api_body(multipliers={"0.5K": 0})),
            ("multiplier-null", self.live_api_body(multipliers={"0.5K": None})),
            ("multiplier-malformed", self.live_api_body(multipliers={"0.5K": "NaN"})),
        )
        for name, body in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-invalid-{name}.json"
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    [{"body": body}],
                    expected_code=2,
                )
                self.assertEqual("PRICE_NOT_VERIFIABLE", payload["reason"])
                self.assertEqual(1, len(requests))
                self.assertFalse(output.exists())
                self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_live_missing_target_multiplier_uses_public_fallback(self) -> None:
        cases = (
            ("absent-map", self.live_api_body()),
            ("empty-map", self.live_api_body(multipliers={})),
            ("other-resolution", self.live_api_body(multipliers={"1K": 1})),
        )
        for name, body in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-missing-factor-{name}.json"
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    [
                        {"body": body},
                        {"body": self.public_pricing_html()},
                    ],
                    expected_code=0,
                )
                self.assertEqual("0.75", payload["evidence"]["resolution_multiplier"])
                self.assertEqual(2, len(requests))

    def test_live_pricing_rejects_missing_conflicting_or_ambiguous_public_text(self) -> None:
        decoy = self.public_pricing_html()
        cases = (
            ("missing", "<html><body><p>Pricing unavailable.</p></body></html>"),
            ("conflicting", self.public_pricing_html(base="0.09")),
            ("ambiguous", self.public_pricing_html() + self.public_pricing_html()),
            (
                "ambiguous-one-paragraph",
                self.public_pricing_html().replace(
                    "</p>",
                    " Your request will cost $0.08 per image.</p>",
                ),
            ),
            (
                "malformed",
                "<p>Your request will cost $0.08 per image. 0.5K (512px) resolution outputs "
                "will be charged at three quarters times the standard rate.</p>",
            ),
            ("nonpositive", self.public_pricing_html(multiplier="0")),
            ("script-decoy", f"<html><body><script>{decoy}</script></body></html>"),
            ("style-decoy", f"<html><body><style>{decoy}</style></body></html>"),
            ("comment-decoy", f"<html><body><!--{decoy}--></body></html>"),
        )
        for name, html in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-public-{name}.json"
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    [{"body": self.live_api_body()}, {"body": html}],
                    expected_code=2,
                )
                self.assertEqual("PRICE_NOT_VERIFIABLE", payload["reason"])
                self.assertEqual(2, len(requests))
                self.assertFalse(output.exists())
                self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_live_public_page_requires_one_exact_canonical_identity(self) -> None:
        canonical = f'<link rel="canonical" href="{PUBLIC_PRICING_URL}">'
        valid = self.public_pricing_html()
        without_canonical = valid.replace(canonical, "")
        cases = (
            ("missing", without_canonical),
            ("wrong", valid.replace(PUBLIC_PRICING_URL, "https://fal.ai/models/other/model")),
            ("duplicate", valid.replace("</head>", canonical + "</head>")),
            ("script-decoy", without_canonical.replace("</head>", f"<script>{canonical}</script></head>")),
            ("comment-decoy", without_canonical.replace("</head>", f"<!--{canonical}--></head>")),
            (
                "quoted-attribute-decoy",
                without_canonical.replace(
                    "</head>",
                    f"<link data-description=' rel=\"canonical\" href=\"{PUBLIC_PRICING_URL}\"'></head>",
                ),
            ),
            ("template-decoy", without_canonical.replace("</head>", f"<template>{canonical}</template></head>")),
            ("textarea-decoy", without_canonical.replace("</head>", f"<textarea>{canonical}</textarea></head>")),
        )
        for name, html in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-canonical-{name}.json"
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    [{"body": self.live_api_body()}, {"body": html}],
                    expected_code=2,
                )
                self.assertEqual("PRICE_NOT_VERIFIABLE", payload["reason"])
                self.assertEqual(2, len(requests))
                self.assertFalse(output.exists())
                self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_hidden_public_pricing_paragraph_is_ignored(self) -> None:
        canonical = f'<link rel="canonical" href="{PUBLIC_PRICING_URL}">'
        valid_paragraph = (
            "<p>Your request will cost $0.08 per image. 0.5K (512px) resolution outputs "
            "will be charged at 0.75 times the standard rate.</p>"
        )
        decoy_paragraph = valid_paragraph.replace("0.08", "9.99").replace("0.75", "9.99")
        inline_decoy = (
            "<p>Your request will cost $0.08 per image. "
            "<span hidden>0.5K (512px) resolution outputs will be charged at 9.99 times the standard rate.</span> "
            "0.5K (512px) resolution outputs will be charged at 0.75 times the standard rate.</p>"
        )
        cases = (
            ("direct", decoy_paragraph.replace("<p>", "<p hidden>"), True),
            ("ancestor-hidden", f"<div hidden>{decoy_paragraph}</div>", True),
            ("ancestor-aria", f'<section aria-hidden="true">{decoy_paragraph}</section>', True),
            ("ancestor-display", f'<div style="display: none">{decoy_paragraph}</div>', True),
            ("ancestor-important", f'<div style="display:none !important">{decoy_paragraph}</div>', True),
            ("ancestor-visibility", f'<div style="visibility:hidden">{decoy_paragraph}</div>', True),
            ("template", f"<template>{decoy_paragraph}</template>", True),
            ("textarea", f"<textarea>{decoy_paragraph}</textarea>", True),
            ("inline-descendant", inline_decoy, False),
            ("inline-aria", inline_decoy.replace("hidden", 'aria-hidden="true"'), False),
            ("inline-display", inline_decoy.replace("hidden", 'style="display:none"'), False),
            ("inline-important", inline_decoy.replace("hidden", 'style="display:none !important"'), False),
            ("inline-visibility", inline_decoy.replace("hidden", 'style="visibility:hidden"'), False),
        )
        for name, hidden_markup, needs_visible in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-hidden-{name}.json"
                visible = valid_paragraph if needs_visible else ""
                html = f"<html><head>{canonical}</head><body>{hidden_markup}{visible}</body></html>"
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    [{"body": self.live_api_body()}, {"body": html}],
                    expected_code=0,
                )
                self.assertEqual("0.75", payload["evidence"]["resolution_multiplier"])
                self.assertEqual(2, len(requests))

    def test_hidden_only_or_ambiguous_public_structure_is_rejected(self) -> None:
        canonical = f'<link rel="canonical" href="{PUBLIC_PRICING_URL}">'
        paragraph = (
            "<p>Your request will cost $0.08 per image. 0.5K (512px) resolution outputs "
            "will be charged at 0.75 times the standard rate.</p>"
        )
        cases = (
            ("hidden-only", f"<div hidden>{paragraph}</div>"),
            ("hidden-important", f'<div style="display:none !important">{paragraph}</div>'),
            ("mismatched", f"<div>{paragraph[:-4]}</div></p>"),
            ("unclosed-hidden", f"<div hidden>{paragraph}"),
            ("self-closing-hidden-container", f"<div hidden/>{paragraph}"),
            ("template-only", f"<template>{paragraph}</template>"),
            ("textarea-only", f"<textarea>{paragraph}</textarea>"),
        )
        for name, markup in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-structure-{name}.json"
                html = f"<html><head>{canonical}</head><body>{markup}</body></html>"
                payload, _, _ = self.run_live_pricing_http(
                    output,
                    [{"body": self.live_api_body()}, {"body": html}],
                    expected_code=2,
                )
                self.assertEqual("PRICE_NOT_VERIFIABLE", payload["reason"])
                self.assertFalse(output.exists())

    def test_live_pricing_stops_oversized_stream_and_rejects_declared_oversize(self) -> None:
        streamed = self.inputs / "live-stream-oversize.json"
        payload, _, _ = self.run_live_pricing_http(
            streamed,
            [
                {"body": self.live_api_body()},
                {"stream": {"chunk_size": 1024 * 1024, "chunks": 8}},
            ],
            expected_code=2,
        )
        self.assertEqual("PRICE_FETCH_FAILED", payload["reason"])
        chunks = [entry for entry in self.pricing_http_trace if entry.get("kind") == "chunk"]
        self.assertLess(len(chunks), 8)
        self.assertTrue(any(entry.get("kind") == "cancel" for entry in self.pricing_http_trace))
        self.assertFalse(streamed.exists())

        declared = self.inputs / "live-declared-oversize.json"
        payload, _, _ = self.run_live_pricing_http(
            declared,
            [
                {"body": self.live_api_body()},
                {"body": "short", "headers": {"content-length": str(8 * 1024 * 1024)}},
            ],
            expected_code=2,
        )
        self.assertEqual("PRICE_FETCH_FAILED", payload["reason"])
        self.assertFalse(declared.exists())

    def test_live_pricing_rejects_invalid_utf8_before_publishing_hash_evidence(self) -> None:
        output = self.inputs / "live-invalid-utf8.json"
        raw = self.public_pricing_html().encode("utf-8") + b"\xff"
        payload, _, requests = self.run_live_pricing_http(
            output,
            [
                {"body": self.live_api_body()},
                {"body_b64": base64.b64encode(raw).decode("ascii")},
            ],
            expected_code=2,
        )
        self.assertEqual("PRICE_FETCH_FAILED", payload["reason"])
        self.assertEqual(2, len(requests))
        self.assertFalse(output.exists())
        self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_live_public_multiplier_is_extracted_dynamically(self) -> None:
        output = self.inputs / "live-dynamic-multiplier.json"
        payload, _, requests = self.run_live_pricing_http(
            output,
            [
                {"body": self.live_api_body()},
                {"body": self.public_pricing_html(multiplier="0.625")},
            ],
            expected_code=0,
        )
        self.assertEqual("0.625", payload["evidence"]["resolution_multiplier"])
        self.assertEqual(2, len(requests))

    def test_live_pricing_rejects_http_failures_and_response_url_changes(self) -> None:
        cases = (
            ("api-http", [{"status": 503}]),
            ("api-url", [{"body": self.live_api_body(), "response_url": "https://evil.example/pricing"}]),
            (
                "public-http",
                [{"body": self.live_api_body()}, {"status": 503}],
            ),
            (
                "public-url",
                [
                    {"body": self.live_api_body()},
                    {"body": self.public_pricing_html(), "response_url": "https://evil.example/model"},
                ],
            ),
        )
        for name, responses in cases:
            with self.subTest(case=name):
                output = self.inputs / f"live-http-{name}.json"
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    responses,
                    expected_code=2,
                )
                self.assertEqual("PRICE_FETCH_FAILED", payload["reason"])
                self.assertEqual(len(responses), len(requests))
                self.assertFalse(output.exists())
                self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_live_pricing_conflicts_block_before_network_and_preserve_sources(self) -> None:
        for name, conflict_receipt in (("pricing", False), ("receipt", True)):
            with self.subTest(case=name):
                output = self.inputs / f"live-conflict-{name}.json"
                conflict = Path(f"{output}.provenance.json") if conflict_receipt else output
                conflict.write_bytes(b"SENTINEL")
                payload, _, requests = self.run_live_pricing_http(
                    output,
                    [],
                    expected_code=2,
                )
                self.assertEqual(
                    "PRICING_PROVENANCE_INVALID" if conflict_receipt else "PRICING_OUTPUT_INVALID",
                    payload["reason"],
                )
                self.assertEqual([], requests)
                self.assertEqual(b"SENTINEL", conflict.read_bytes())
                other = output if conflict_receipt else Path(f"{output}.provenance.json")
                self.assertFalse(other.exists())

    def test_live_pricing_wrong_source_never_triggers_network(self) -> None:
        output = self.inputs / "wrong-source.json"
        payload, _ = self.run_cli(
            [
                "pricing",
                "--source",
                "https://api.fal.ai/v1/models/pricing/other",
                "--output",
                str(output),
            ],
            expected_code=2,
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
        self.assertFalse(output.exists())
        self.assertFalse(Path(f"{output}.provenance.json").exists())

    def test_live_pricing_drives_exact_eight_frame_budget_boundary(self) -> None:
        pricing = self.inputs / "budget-live-pricing.json"
        payload, _, _ = self.run_live_pricing_http(
            pricing,
            [
                {"body": self.live_api_body()},
                {"body": self.public_pricing_html()},
            ],
            expected_code=0,
        )
        self.assertEqual("0.08", payload["evidence"]["base_unit_price_usd"])
        self.assertEqual("0.75", payload["evidence"]["resolution_multiplier"])
        movement = self.inputs / "budget-movement.json"
        self.write_json(
            movement,
            [
                {"frame_index": index, "phase": f"phase-{index}", "body_motion": f"pose-{index}"}
                for index in range(1, 9)
            ],
        )
        arguments = [
            "prepare",
            "--source", str(self.source),
            "--movement-plan", str(movement),
            "--pricing", str(pricing),
            "--animation", "walking",
            "--direction", "west",
            "--frame-count", "8",
            "--max-budget-usd", "0.50",
            "--workspace-root", str(self.workspace),
            "--run-id", "live-budget-accepted",
        ]
        run_dir = Path(self.run_pipeline_cli(arguments, plain=True))
        manifest = self.read_json(run_dir / "reports/manifest.json")
        self.assertEqual(
            Decimal("0.48"),
            Decimal(manifest["authorization"]["worst_case_usd"]),
        )
        rejected = subprocess.run(
            [
                str(self.python), "-B", str(Path(pipeline.__file__)),
                *[
                    "0.47" if value == "0.50" else "live-budget-blocked" if value == "live-budget-accepted" else value
                    for value in arguments
                ],
            ],
            cwd=self.root,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("BUDGET_EXCEEDED", rejected.stdout + rejected.stderr)

    @unittest.skipUnless(os.name == "nt", "requires Win32 handle semantics")
    def test_pricing_failure_after_receipt_publication_retains_orphan_receipt(self) -> None:
        output = self.inputs / "late-pricing-conflict.json"
        receipt = Path(f"{output}.provenance.json")
        signal = self.root / "pricing-receipt.signal"
        preload = self.root / "late-pricing-fetch.cjs"
        api_body = self.live_api_body()
        public_html = self.public_pricing_html()
        preload.write_text(
            textwrap.dedent(
                f'''
                globalThis.fetch = async (input, options = {{}}) => {{
                  const url = String(input);
                  if (url === {json.dumps(PRICING_URL + '?endpoint_id=fal-ai%2Fnano-banana-2%2Fedit')}) {{
                    const response = new Response({json.dumps(api_body)}, {{ status: 200 }});
                    Object.defineProperty(response, "url", {{ value: url }});
                    return response;
                  }}
                  if (url === {json.dumps(PUBLIC_PRICING_URL)}) {{
                    const response = new Response({json.dumps(public_html)}, {{ status: 200 }});
                    Object.defineProperty(response, "url", {{ value: url }});
                    return response;
                  }}
                  throw new Error("UNEXPECTED_NETWORK_REQUEST");
                }};
                '''
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                str(self.node), str(self.SCRIPT),
                "pricing", "--source", PRICING_URL, "--output", str(output),
            ],
            cwd=self.root,
            env={
                **self.environment(),
                "NODE_OPTIONS": f"--require={preload}",
                "TOWER_SPRITE_ATOMIC_TEST_SIGNAL": str(signal),
                "TOWER_SPRITE_ATOMIC_TEST_OPERATION": "new",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for_file(signal)
        Path(f"{signal}.ack").write_text("continue", encoding="utf-8")
        done = Path(f"{signal}.done")
        self.wait_for_file(done)
        self.assertEqual(receipt.resolve(), Path(self.read_json(done)["target"]).resolve())
        output.write_bytes(b"LATE-SENTINEL")
        Path(f"{done}.ack").write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(2, process.returncode, stderr)
        self.assertEqual("PRICING_OUTPUT_INVALID", json.loads(stdout)["reason"])
        self.assertEqual(b"LATE-SENTINEL", output.read_bytes())
        self.assertTrue(receipt.is_file())
        self.assertEqual(1, self.read_json(receipt)["schema_version"])

    def test_fixed_clock_launcher_rejects_invalid_clock_before_cli_import(self) -> None:
        environment = self.environment()
        environment.update(self.fixed_clock_environment("not-a-clock"))
        completed = subprocess.run(
            [
                str(self.node),
                environment["CONFORMANCE_LAUNCHER"],
                str(self.SCRIPT),
                "pricing",
            ],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("TEST_CLOCK_INVALID", completed.stderr)
        self.assertFalse(self.actions.exists())

    def test_shared_pricing_corpus_matches_node_acceptance(self) -> None:
        corpus = json.loads(PRICING_CORPUS.read_text(encoding="utf-8"))
        self.assertEqual("OFFLINE_FIXTURE_NOT_FOR_LIVE_USE", corpus["evidence_mode"])
        for index, case in enumerate(corpus["cases"]):
            with self.subTest(case=case["id"]):
                evidence = {**corpus["base"], **case["changes"]}
                pricing = self.inputs / f"corpus-{index:02d}.json"
                self.write_json(pricing, evidence)
                clock = self.fixed_clock_environment(case["now"])
                if case["accepted"]:
                    frozen_now = datetime.fromisoformat(
                        case["now"].replace("Z", "+00:00")
                    )

                    class FrozenDate(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

                    with mock.patch.object(pipeline, "datetime", FrozenDate):
                        run_dir, run_manifest, request_plan = self.prepare_authorized(
                            f"corpus-{index:02d}",
                            pricing_path=pricing,
                            max_budget_usd=Decimal("1"),
                        )
                    self.assertEqual(
                        case["normalized"],
                        self.read_json(run_dir / "reports/manifest.json")["provider"]["pricing"],
                    )
                    payload, actions, _ = self.execute(
                        run_manifest=run_manifest,
                        request_plan=request_plan,
                        pricing=pricing,
                        events=(
                            {"operation": "submit", "value": {"request_id": f"corpus-{index:02d}"}},
                            {"operation": "status", "value": {"status": "IN_PROGRESS"}},
                        ),
                        extra_environment=clock,
                    )
                    self.assertEqual("REQUEST_PENDING", payload["reason"])
                    self.assertEqual(
                        ["import", "submit", f"status:corpus-{index:02d}"],
                        actions,
                    )
                else:
                    before = self.run_manifest.read_bytes()
                    payload, actions, _ = self.execute(
                        pricing=pricing,
                        expected_code=2,
                        extra_environment=clock,
                    )
                    self.assertIn(
                        payload["reason"],
                        {"PRICE_NOT_VERIFIABLE", "PRICE_STALE"},
                    )
                    self.assertEqual([], actions)
                    self.assertEqual(before, self.run_manifest.read_bytes())

    def test_pricing_rejects_missing_duplicate_and_mismatched_model_entries(self) -> None:
        missing = self.valid_official_response(prices=[])
        duplicate = self.valid_official_response()
        duplicate["prices"].append(copy.deepcopy(duplicate["prices"][-1]))
        mismatched = self.valid_official_response()
        mismatched["prices"][-1]["endpoint_id"] = "fal-ai/nano-banana/edit"
        for name, value in (
            ("missing", missing),
            ("duplicate", duplicate),
            ("mismatched", mismatched),
        ):
            with self.subTest(case=name):
                source = self.inputs / f"{name}.json"
                output = self.inputs / f"{name}-evidence.json"
                self.write_json(source, value)
                payload, _ = self.run_cli(
                    ["pricing", "--source", str(source), "--output", str(output)],
                    expected_code=2,
                    key=None,
                )
                self.assertEqual("PRICE_NOT_VERIFIABLE", payload["reason"])
                self.assertFalse(output.exists())

    def test_pricing_rejects_stale_future_non_usd_non_image_and_bad_multiplier(self) -> None:
        cases = []
        stale = self.valid_official_response(
            verified_at=(datetime.now(timezone.utc) - timedelta(minutes=16))
            .isoformat()
            .replace("+00:00", "Z")
        )
        cases.append(("stale", stale, "PRICE_STALE"))
        future = self.valid_official_response(
            verified_at=(datetime.now(timezone.utc) + timedelta(seconds=61))
            .isoformat()
            .replace("+00:00", "Z")
        )
        cases.append(("future", future, "PRICE_STALE"))
        for name, field, value in (
            ("currency", "currency", "EUR"),
            ("unit", "unit", "megapixel"),
        ):
            evidence = self.valid_official_response()
            evidence["prices"][-1][field] = value
            cases.append((name, evidence, "PRICE_NOT_VERIFIABLE"))
        multiplier = self.valid_official_response()
        multiplier["prices"][-1]["resolution_multipliers"]["0.5K"] = 0
        cases.append(("multiplier", multiplier, "PRICE_NOT_VERIFIABLE"))

        for name, value, reason in cases:
            with self.subTest(case=name):
                source = self.inputs / f"invalid-{name}.json"
                output = self.inputs / f"invalid-{name}-evidence.json"
                self.write_json(source, value)
                payload, _ = self.run_cli(
                    ["pricing", "--source", str(source), "--output", str(output)],
                    expected_code=2,
                    key=None,
                )
                self.assertEqual(reason, payload["reason"])
                self.assertFalse(output.exists())

    def test_pricing_preserves_decimal_lexemes_without_float_rounding(self) -> None:
        raw = self.inputs / "decimal-official-response.json"
        output = self.inputs / "decimal-sanitized.json"
        value = self.valid_official_response()
        value["prices"][-1]["unit_price"] = "0.123456789012345678"
        value["prices"][-1]["resolution_multipliers"]["0.5K"] = "0.333333333333333333"
        self.write_json(raw, value)
        exact_source = raw.read_text(encoding="utf-8").replace(
            '"unit_price": "0.123456789012345678"',
            '"unit_price": 0.123456789012345678',
        ).replace(
            '"0.5K": "0.333333333333333333"',
            '"0.5K": 0.333333333333333333',
        )
        raw.write_bytes(exact_source.encode("utf-8"))
        payload, _ = self.run_cli(
            ["pricing", "--source", str(raw), "--output", str(output)],
            expected_code=0,
            key=None,
        )
        evidence = payload["evidence"]
        self.assertEqual("0.123456789012345678", evidence["base_unit_price_usd"])
        self.assertEqual("0.333333333333333333", evidence["resolution_multiplier"])

    def test_submit_uses_exact_frame_one_payload_without_thinking_level(self) -> None:
        master = self.run_dir / "input/source/master.png"
        expected_input = {
            "prompt": "Redraw image 1 as the same character running east at left-foot contact; body movement: left foot planted forward, right leg trailing; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.",
            "num_images": 1,
            "aspect_ratio": "auto",
            "output_format": "png",
            "image_urls": [
                "data:image/png;base64,"
                + base64.b64encode(master.read_bytes()).decode("ascii")
            ],
            "resolution": "0.5K",
            "limit_generations": True,
            "enable_web_search": False,
        }
        self.assertEqual(
            expected_input["prompt"],
            pipeline.run_authorization_summary(self.run_manifest)["effective_prompts"][0]["prompt"],
        )
        payload, actions, _ = self.execute(
            events=(
                {
                    "operation": "submit",
                    "endpoint": MODEL,
                    "input": expected_input,
                    "value": {"request_id": "request-1"},
                },
                {"operation": "status", "value": {"status": "IN_QUEUE"}},
            )
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["import", "submit", "status:request-1"], actions)
        self.assertNotIn("thinking_level", expected_input)
        persisted = self.run_manifest.read_text(encoding="utf-8")
        self.assertNotIn("data:image", persisted)
        self.assertNotIn("fake-key-never-persist", persisted)

    def test_later_frame_names_and_encodes_master_then_previous_accepted(self) -> None:
        second_plan = self.seed_accepted_frame_one()
        previous = self.run_dir / "normalized/frames/frame-01.png"
        expected_input = {
            "prompt": "Redraw image 1 (master) and image 2 (previous accepted frame) as the same character running east at right-foot contact; body movement: right foot planted forward, left leg trailing; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background.",
            "num_images": 1,
            "aspect_ratio": "auto",
            "output_format": "png",
            "image_urls": [
                "data:image/png;base64,"
                + base64.b64encode(
                    (self.run_dir / "input/source/master.png").read_bytes()
                ).decode("ascii"),
                "data:image/png;base64,"
                + base64.b64encode(previous.read_bytes()).decode("ascii"),
            ],
            "resolution": "0.5K",
            "limit_generations": True,
            "enable_web_search": False,
        }
        self.assertEqual(
            expected_input["prompt"],
            pipeline.run_authorization_summary(self.run_manifest)["effective_prompts"][1]["prompt"],
        )
        payload, actions, _ = self.execute(
            request_plan=second_plan,
            events=(
                {
                    "operation": "submit",
                    "endpoint": MODEL,
                    "input": expected_input,
                    "value": {"request_id": "request-2"},
                },
                {"operation": "status", "value": {"status": "IN_PROGRESS"}},
            ),
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["import", "submit", "status:request-2"], actions)

    def test_historic_v1_partial_submit_preserves_exact_prompt_and_references(self) -> None:
        run_dir, run_manifest, request_plan, pricing = self.copy_historic_v1_partial(
            "historic-v1-submit"
        )
        expected_prompt = json.loads(
            (C2_V1_BASELINE / "expected-prompts.json").read_text(encoding="utf-8")
        )["partial"]["frame-02"]
        expected_input = {
            "prompt": expected_prompt,
            "num_images": 1,
            "aspect_ratio": "auto",
            "output_format": "png",
            "image_urls": [
                "data:image/png;base64,"
                + base64.b64encode((run_dir / "input/source/master.png").read_bytes()).decode("ascii"),
                "data:image/png;base64,"
                + base64.b64encode((run_dir / "normalized/frames/frame-01.png").read_bytes()).decode("ascii"),
            ],
            "resolution": "0.5K",
            "limit_generations": True,
            "enable_web_search": False,
        }
        self.assertNotIn("effective_prompts", pipeline.run_authorization_summary(run_manifest))
        payload, actions, _ = self.execute(
            run_manifest=run_manifest,
            request_plan=request_plan,
            pricing=pricing,
            events=(
                {
                    "operation": "submit",
                    "endpoint": MODEL,
                    "input": expected_input,
                    "value": {"request_id": "historic-v1-frame-2"},
                },
                {"operation": "status", "value": {"status": "IN_QUEUE"}},
            ),
            extra_environment=self.fixed_clock_environment(
                self.read_json(pricing)["verified_at"]
            ),
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["import", "submit", "status:historic-v1-frame-2"], actions)

    def test_historic_v1_existing_request_id_resumes_without_submit(self) -> None:
        run_dir, run_manifest, request_plan, pricing = self.copy_historic_v1_partial(
            "historic-v1-resume"
        )
        plan = self.read_json(request_plan)
        run = self.read_json(run_manifest)
        run["requests"].append(
            {
                "frame_index": 2,
                "attempt": 1,
                "model": MODEL,
                "request_fingerprint": plan["request_fingerprint"],
                "request_id": "historic-v1-existing-id",
                "status": "IN_PROGRESS",
                "estimated_cost_usd": "0.05",
                "submitted_at": "2026-09-04T12:00:00Z",
                "result_path": None,
            }
        )
        self.write_json(run_manifest, run)
        payload, actions, _ = self.execute(
            run_manifest=run_manifest,
            request_plan=request_plan,
            pricing=pricing,
            events=({"operation": "status", "value": {"status": "IN_QUEUE"}},),
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["import", "status:historic-v1-existing-id"], actions)

    def test_body_motion_changes_prompt_and_fingerprint_for_same_run_identity(self) -> None:
        def prepared(root: Path, body_motion: str) -> tuple[Path, Path, Path]:
            movement = root / "movement.json"
            self.write_json(
                movement,
                [
                    {"frame_index": 1, "phase": "contact", "body_motion": body_motion},
                    {"frame_index": 2, "phase": "passing", "body_motion": "unchanged frame two"},
                ],
            )
            workspace = root / "runs"
            run_dir = pipeline.prepare_run(
                source=self.source,
                movement_plan_path=movement,
                pricing_path=self.pricing,
                animation="running",
                direction="east",
                frame_count=2,
                frame_size=None,
                max_budget_usd=Decimal("0.10"),
                workspace_root=workspace,
                run_id="same-run-id",
            )
            run_manifest = run_dir / "reports/run_manifest.json"
            pipeline.authorize_run(run_manifest, Decimal("0.10"))
            request_plan = run_dir / "input/requests/frame-01.json"
            pipeline.next_request_plan(run_manifest, request_plan)
            return run_dir, run_manifest, request_plan

        first_root = self.root / "motion-a"
        second_root = self.root / "motion-b"
        first_root.mkdir()
        second_root.mkdir()
        _first_dir, first_manifest, first_plan = prepared(first_root, "left arm high")
        second_dir, second_manifest, second_plan = prepared(second_root, "left arm low")
        self.assertNotEqual(
            self.read_json(first_plan)["request_fingerprint"],
            self.read_json(second_plan)["request_fingerprint"],
        )
        expected_prompt = "Redraw image 1 as the same character running east at contact; body movement: left arm low; preserve face, proportions, clothing, equipment, palette and art style; show one complete bottom-center character on the declared solid chroma background."
        expected_input = {
            "prompt": expected_prompt,
            "num_images": 1,
            "aspect_ratio": "auto",
            "output_format": "png",
            "image_urls": [
                "data:image/png;base64,"
                + base64.b64encode((second_dir / "input/source/master.png").read_bytes()).decode("ascii")
            ],
            "resolution": "0.5K",
            "limit_generations": True,
            "enable_web_search": False,
        }
        payload, actions, _ = self.execute(
            run_manifest=second_manifest,
            request_plan=second_plan,
            events=(
                {"operation": "submit", "endpoint": MODEL, "input": expected_input, "value": {"request_id": "motion-id"}},
                {"operation": "status", "value": {"status": "IN_QUEUE"}},
            ),
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["import", "submit", "status:motion-id"], actions)

    def test_invalid_mixed_or_changed_versions_block_before_import_without_writes(self) -> None:
        cases = (
            ("manifest-bool", "MANIFEST_INVALID", lambda manifest, run: manifest.update(schema_version=True)),
            ("manifest-unknown", "MANIFEST_INVALID", lambda manifest, run: manifest.update(schema_version=3)),
            ("provider-bool", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].update(prompt_template_version=True)),
            ("provider-wrong", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].update(prompt_template_version=1)),
            ("provider-missing", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].pop("prompt_template_version", None)),
            ("provider-extra", "MANIFEST_INVALID", lambda manifest, run: manifest["provider"].update(unexpected="blocked")),
            ("run-bool", "RUN_MANIFEST_INVALID", lambda manifest, run: run.update(schema_version=True)),
            ("run-unknown", "RUN_MANIFEST_INVALID", lambda manifest, run: run.update(schema_version=3)),
            ("mixed", "RUN_MANIFEST_INVALID", lambda manifest, run: run.update(schema_version=1)),
            ("changed-after-authorization", "MANIFEST_INVALID", lambda manifest, run: manifest.update(schema_version=1)),
        )
        for name, reason, mutate in cases:
            with self.subTest(case=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(f"invalid-version-{name}")
                manifest_path = run_dir / "reports/manifest.json"
                manifest = self.read_json(manifest_path)
                run = self.read_json(run_manifest)
                mutate(manifest, run)
                self.write_json(manifest_path, manifest)
                self.write_json(run_manifest, run)
                before = (manifest_path.read_bytes(), run_manifest.read_bytes())
                payload, actions, _ = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    expected_code=2,
                )
                self.assertEqual(reason, payload["reason"])
                self.assertEqual([], actions)
                self.assertEqual(before, (manifest_path.read_bytes(), run_manifest.read_bytes()))

        run_dir, run_manifest, request_plan, pricing = self.copy_historic_v1_partial(
            "invalid-v1-provider-field"
        )
        manifest_path = run_dir / "reports/manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["provider"]["prompt_template_version"] = 2
        self.write_json(manifest_path, manifest)
        before = (manifest_path.read_bytes(), run_manifest.read_bytes())
        payload, actions, _ = self.execute(
            run_manifest=run_manifest,
            request_plan=request_plan,
            pricing=pricing,
            expected_code=2,
        )
        self.assertEqual("MANIFEST_INVALID", payload["reason"])
        self.assertEqual([], actions)
        self.assertEqual(before, (manifest_path.read_bytes(), run_manifest.read_bytes()))

    def test_missing_key_unauthorized_stale_price_and_fingerprint_block_before_import(self) -> None:
        self.assert_preimport_block("FAL_KEY_MISSING", key=None)

        draft = pipeline.prepare_run(
            source=self.source,
            movement_plan_path=self.movement,
            pricing_path=self.pricing,
            animation="running",
            direction="east",
            frame_count=2,
            frame_size=None,
            max_budget_usd=Decimal("0.10"),
            workspace_root=self.workspace,
            run_id="draft-run",
        )
        draft_plan = draft / "input/requests/frame-01.json"
        self.write_json(draft_plan, self.read_json(self.request_plan))
        self.assert_preimport_block(
            "RUN_NOT_AUTHORIZED",
            run_manifest=draft / "reports/run_manifest.json",
            request_plan=draft_plan,
        )

        stale = self.inputs / "stale-pricing.json"
        self.write_json(
            stale,
            self.valid_sanitized_pricing(
                verified_at=(datetime.now(timezone.utc) - timedelta(minutes=16))
                .isoformat()
                .replace("+00:00", "Z")
            ),
        )
        self.assert_preimport_block("PRICE_STALE", pricing=stale)

        changed = self.read_json(self.request_plan)
        changed["request_fingerprint"] = "0" * 64
        self.request_plan.write_bytes(
            (
                json.dumps(
                    changed,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )
        canonical_node = subprocess.run(
            [
                str(self.node),
                "-e",
                "const fs=require('node:fs');const c=v=>Array.isArray(v)?v.map(c):(v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,c(v[k])])):v);const v=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(c(v),null,2)+'\\n');",
            ],
            input=self.request_plan.read_text(encoding="utf-8"),
            capture_output=True,
            check=True,
            text=True,
        ).stdout.encode("utf-8")
        self.assertEqual(self.request_plan.read_bytes(), canonical_node)
        self.assert_preimport_block("REQUEST_FINGERPRINT_MISMATCH")

    def test_internally_over_budget_authorization_blocks_before_import(self) -> None:
        manifest_path = self.run_dir / "reports/manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["authorization"]["worst_case_usd"] = "0.11"
        manifest["authorization"]["contract_sha256"] = contract.authorization_contract_sha256(
            manifest
        )
        self.write_json(manifest_path, manifest)
        run = self.read_json(self.run_manifest)
        run["manifest_sha256"] = contract.canonical_sha256(manifest)
        self.write_json(self.run_manifest, run)
        self.assert_preimport_block("BUDGET_WORST_CASE_EXCEEDED")

    def test_submit_exception_and_invalid_response_are_irreversibly_unknown(self) -> None:
        cases = (
            (
                "throw",
                {
                    "operation": "submit",
                    "throw": True,
                    "error_name": "TimeoutError",
                    "message": "secret data:image/png;base64,AAAA",
                },
            ),
            ("invalid", {"operation": "submit", "value": {"request_id": " "}}),
        )
        for name, event in cases:
            with self.subTest(case=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"unknown-{name}"
                )
                payload, actions, completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    events=(event,),
                    expected_code=2,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", payload["reason"])
                self.assertEqual(1, payload["submitted_requests"])
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", payload["status"])
                self.assertIsNone(payload["request_id"])
                self.assertEqual(["import", "submit"], actions)
                run = self.read_json(run_manifest)
                self.assertEqual("BLOCKED", run["state"])
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", run["terminal_reason"])
                self.assertEqual(1, len(run["requests"]))
                self.assertIsNone(run["requests"][0]["request_id"])
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", run["requests"][0]["status"])
                audit = run_manifest.read_text(encoding="utf-8") + completed.stdout
                self.assertNotIn("secret", audit)
                self.assertNotIn("data:image", audit)

                repeated, repeated_actions, _ = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    expected_code=2,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", repeated["reason"])
                self.assertEqual([], repeated_actions)
                self.assertEqual(1, len(self.read_json(run_manifest)["requests"]))

    def test_real_sdk_submit_failure_delegates_one_post_and_blocks_resubmission(self) -> None:
        adapter, sdk, sdk_before = self.real_sdk_adapter()
        http_log = adapter.parent / "http.jsonl"
        preload = self.write_real_sdk_preload(adapter.parent, http_log)
        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "real-sdk-unknown"
        )
        arguments = [
            "execute",
            "--run-manifest",
            str(run_manifest),
            "--request-plan",
            str(request_plan),
            "--pricing",
            str(self.pricing),
        ]
        environment = self.environment()
        environment.update(
            {
                "NODE_OPTIONS": f"--require={preload}",
                "C4_HTTP_LOG": str(http_log),
            }
        )

        first = subprocess.run(
            [str(self.node), str(adapter), *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, first.returncode, first.stderr)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", json.loads(first.stdout)["reason"])
        calls_after_first = [
            json.loads(line) for line in http_log.read_text(encoding="utf-8").splitlines()
        ]
        posts = [item for item in calls_after_first if item.get("method") == "POST"]
        self.assertEqual(1, len(posts))
        self.assertEqual(f"https://queue.fal.run/{MODEL}", posts[0]["url"])
        self.assertEqual("error", posts[0]["redirect"])
        self.assertFalse(
            any(item["kind"] == "unexpected-network-entrypoint" for item in calls_after_first)
        )

        run = self.read_json(run_manifest)
        self.assertEqual("BLOCKED", run["state"])
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", run["terminal_reason"])
        self.assertEqual(1, len(run["requests"]))
        self.assertIsNone(run["requests"][0]["request_id"])
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", run["requests"][0]["status"])
        self.assertTrue((run_dir / "reports/transport-claim-frame-01.json").is_file())

        second = subprocess.run(
            [str(self.node), str(adapter), *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, second.returncode, second.stderr)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", json.loads(second.stdout)["reason"])
        calls_after_second = [
            json.loads(line) for line in http_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len([x for x in calls_after_second if x.get("method") == "POST"]))
        self.assertEqual(1, len(self.read_json(run_manifest)["requests"]))
        self.assertFalse((run_dir / "normalized/sprite-sheet.png").exists())
        self.assertFalse((run_dir / "previews/loop.gif").exists())
        self.assertEqual(sdk_before, self.tree_hashes(sdk))
        self.assertEqual(self.SCRIPT.read_bytes(), adapter.read_bytes())

    def test_real_sdk_known_id_resumes_with_get_and_never_submits_again(self) -> None:
        adapter, sdk, sdk_before = self.real_sdk_adapter()
        http_log = adapter.parent / "http.jsonl"
        preload = self.write_real_sdk_preload(adapter.parent, http_log)
        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "real-sdk-known-id"
        )
        arguments = [
            "execute",
            "--run-manifest",
            str(run_manifest),
            "--request-plan",
            str(request_plan),
            "--pricing",
            str(self.pricing),
        ]
        environment = self.environment()
        environment.update(
            {
                "NODE_OPTIONS": f"--require={preload}",
                "C4_HTTP_LOG": str(http_log),
                "C4_HTTP_MODE": "resume",
                "C4_RESULT_URI": self.png_uri,
            }
        )

        pending = subprocess.run(
            [str(self.node), str(adapter), *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, pending.returncode, pending.stderr)
        pending_payload = json.loads(pending.stdout)
        self.assertEqual("REQUEST_PENDING", pending_payload["reason"])
        self.assertEqual("real-sdk-resume-id", pending_payload["request_id"])

        completed = subprocess.run(
            [str(self.node), str(adapter), *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        completed_payload = json.loads(completed.stdout)
        self.assertEqual("EVALUATION_REQUIRED", completed_payload["reason"])
        self.assertEqual("real-sdk-resume-id", completed_payload["request_id"])

        calls = [
            json.loads(line) for line in http_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(1, len([item for item in calls if item.get("method") == "POST"]))
        self.assertEqual(
            2,
            len([item for item in calls if item.get("url", "").endswith("/status?logs=0")]),
        )
        self.assertEqual(
            1,
            len(
                [
                    item
                    for item in calls
                    if item.get("method") == "GET"
                    and item.get("url", "").endswith("/real-sdk-resume-id")
                ]
            ),
        )
        self.assertFalse(
            any(item["kind"] == "unexpected-network-entrypoint" for item in calls)
        )
        run = self.read_json(run_manifest)
        self.assertEqual(1, len(run["requests"]))
        self.assertEqual("real-sdk-resume-id", run["requests"][0]["request_id"])
        self.assertEqual("COMPLETED", run["requests"][0]["status"])
        self.assertEqual("raw/frame-01/attempt-01.png", run["requests"][0]["result_path"])
        self.assertEqual(self.png_bytes, (run_dir / run["requests"][0]["result_path"]).read_bytes())
        self.assertEqual(sdk_before, self.tree_hashes(sdk))
        self.assertEqual(self.SCRIPT.read_bytes(), adapter.read_bytes())

    def test_cli_adapter_e2e_resumes_and_assembles(self) -> None:
        cases = (
            ("narrow", (12, 18), (31, 61, 91)),
            ("wide", (20, 28), (101, 41, 71)),
        )
        for variant, size, source_color in cases:
            with self.subTest(variant=variant, size=size):
                source = self.inputs / f"source-{variant}.png"
                source.write_bytes(
                    self.generated_frame_payload(
                        size,
                        source_color,
                        variant,
                        transparent_background=True,
                    )
                )
                self.write_json(
                    self.movement,
                    [
                        {
                            "frame_index": index,
                            "phase": "phase-" + str(index),
                            "body_motion": "pose-" + str(index),
                        }
                        for index in range(1, 4)
                    ],
                )
                run_id = f"cli-three-frames-{variant}"
                run_dir = Path(
                    self.run_pipeline_cli(
                        [
                            "prepare",
                            "--source",
                            str(source),
                            "--movement-plan",
                            str(self.movement),
                            "--pricing",
                            str(self.pricing),
                            "--animation",
                            "running",
                            "--direction",
                            "east",
                            "--frame-count",
                            "3",
                            "--frame-width",
                            str(size[0]),
                            "--frame-height",
                            str(size[1]),
                            "--max-budget-usd",
                            "0.15",
                            "--workspace-root",
                            str(self.workspace),
                            "--run-id",
                            run_id,
                        ],
                        plain=True,
                    )
                )
                manifest = run_dir / "reports/run_manifest.json"
                summary = self.run_pipeline_cli(
                    ["summary", "--run-manifest", str(manifest)]
                )
                self.assertEqual(3, summary["frame_count"])
                self.assertEqual("0.150", summary["worst_case_usd"])
                self.assertEqual(
                    [
                        {
                            "frame_index": index,
                            "prompt": (
                                "Redraw image 1"
                                + (" (master) and image 2 (previous accepted frame)" if index > 1 else "")
                                + " as the same character running east at phase-"
                                + str(index)
                                + "; body movement: pose-"
                                + str(index)
                                + "; preserve face, proportions, clothing, equipment, palette and art style; "
                                "show one complete bottom-center character on the declared solid chroma background."
                            ),
                        }
                        for index in range(1, 4)
                    ],
                    summary["effective_prompts"],
                )
                self.run_pipeline_cli(
                    [
                        "authorize",
                        "--run-manifest",
                        str(manifest),
                        "--authorized-budget-usd",
                        "0.15",
                    ]
                )
                all_actions = []
                expected_colors = []
                for index in range(1, 4):
                    plan_path = run_dir / f"input/requests/frame-{index:02d}.json"
                    plan = self.run_pipeline_cli(
                        [
                            "next-request",
                            "--run-manifest",
                            str(manifest),
                            "--output",
                            str(plan_path),
                        ]
                    )
                    self.assertEqual(index, plan["frame_index"])
                    self.assertEqual(f"phase-{index}", plan["instructions"]["phase"])
                    self.assertEqual(f"pose-{index}", plan["instructions"]["body_motion"])
                    expected_references = [
                        {"role": "master", "path": "input/source/master.png"}
                    ]
                    if index > 1:
                        expected_references.append(
                            {
                                "role": "previous_accepted",
                                "path": f"normalized/frames/frame-{index - 1:02d}.png",
                            }
                        )
                    self.assertEqual(expected_references, plan["references"])
                    request_id = f"{run_id}-request-{index}"
                    pending, actions, _ = self.execute(
                        run_manifest=manifest,
                        request_plan=plan_path,
                        events=(
                            {
                                "operation": "submit",
                                "endpoint": MODEL,
                                "value": {"request_id": request_id},
                            },
                            {"operation": "status", "value": {"status": "IN_QUEUE"}},
                        ),
                    )
                    all_actions.extend(actions)
                    self.assertEqual("REQUEST_PENDING", pending["reason"])
                    self.assertEqual(request_id, pending["request_id"])
                    color = (
                        (source_color[0] + index * 23) % 220,
                        (source_color[1] + index * 31) % 220,
                        (source_color[2] + index * 37) % 220,
                    )
                    expected_colors.append((*color, 255))
                    png = self.generated_frame_payload(size, color, variant)
                    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
                    completed, actions, _ = self.execute(
                        run_manifest=manifest,
                        request_plan=plan_path,
                        events=(
                            {"operation": "status", "value": {"status": "COMPLETED"}},
                            {
                                "operation": "result",
                                "value": {"data": {"images": [{"url": uri}]}},
                            },
                        ),
                    )
                    all_actions.extend(actions)
                    self.assertNotIn("submit", actions)
                    self.assertEqual("EVALUATION_REQUIRED", completed["reason"])
                    record = self.read_json(manifest)["requests"][-1]
                    self.assertEqual(request_id, record["request_id"])
                    self.assertEqual("0.050", record["estimated_cost_usd"])
                    evaluated = self.run_pipeline_cli(
                        [
                            "evaluate",
                            "--run-manifest",
                            str(manifest),
                            "--frame-index",
                            str(index),
                            "--raw",
                            str(run_dir / record["result_path"]),
                        ]
                    )
                    self.assertEqual("PASS", evaluated["technical_result"])
                    normalized = run_dir / f"normalized/frames/frame-{index:02d}.png"
                    with Image.open(normalized) as frame:
                        self.assertEqual("RGBA", frame.mode)
                        self.assertEqual(size, frame.size)
                        self.assertEqual(expected_colors[-1], frame.getpixel((size[0] // 2, size[1] // 2)))

                self.assertEqual(3, all_actions.count("submit"))
                self.assertIsNone(
                    self.run_pipeline_cli(
                        [
                            "next-request",
                            "--run-manifest",
                            str(manifest),
                            "--output",
                            str(run_dir / "input/requests/frame-04.json"),
                        ]
                    )
                )
                assembled = self.run_pipeline_cli(
                    ["assemble", "--run-manifest", str(manifest)]
                )
                self.assertEqual("READY_FOR_VISUAL_REVIEW", assembled["state"])
                run = self.read_json(manifest)
                self.assertEqual("READY_FOR_VISUAL_REVIEW", run["state"])
                self.assertEqual(100, run["artifacts"]["assembly"]["duration_ms"])
                frame_paths = [
                    run_dir / f"normalized/frames/frame-{index:02d}.png"
                    for index in range(1, 4)
                ]
                with Image.open(run_dir / "normalized/sprite-sheet.png") as sheet:
                    self.assertEqual("RGBA", sheet.mode)
                    self.assertEqual((size[0] * 3, size[1]), sheet.size)
                    for offset, frame_path in enumerate(frame_paths):
                        with Image.open(frame_path) as frame:
                            self.assertEqual(
                                frame.tobytes(),
                                sheet.crop(
                                    (offset * size[0], 0, (offset + 1) * size[0], size[1])
                                ).tobytes(),
                            )
                with Image.open(run_dir / "previews/contact-sheet.png") as contact:
                    self.assertEqual("RGBA", contact.mode)
                    self.assertEqual((size[0] * 3, size[1]), contact.size)
                with Image.open(run_dir / "previews/loop.gif") as loop:
                    self.assertEqual(3, loop.n_frames)
                    self.assertEqual(0, loop.info["loop"])
                    for offset, expected_color in enumerate(expected_colors):
                        loop.seek(offset)
                        self.assertEqual(100, loop.info["duration"])
                        self.assertEqual(
                            expected_color,
                            loop.convert("RGBA").getpixel((size[0] // 2, size[1] // 2)),
                        )

    def test_cli_adapter_e2e_stops_on_rejection(self) -> None:
        source = self.inputs / "rejected-source.png"
        source.write_bytes(
            self.generated_frame_payload(
                (12, 18),
                (41, 71, 101),
                "narrow",
                transparent_background=True,
            )
        )
        self.write_json(
            self.movement,
            [
                {"frame_index": index, "phase": f"phase-{index}", "body_motion": f"pose-{index}"}
                for index in range(1, 4)
            ],
        )
        run_dir = Path(
            self.run_pipeline_cli(
                [
                    "prepare", "--source", str(source),
                    "--movement-plan", str(self.movement), "--pricing", str(self.pricing),
                    "--animation", "running", "--direction", "east", "--frame-count", "3",
                    "--frame-width", "12", "--frame-height", "18",
                    "--max-budget-usd", "0.15", "--workspace-root", str(self.workspace),
                    "--run-id", "cli-rejection",
                ],
                plain=True,
            )
        )
        manifest = run_dir / "reports/run_manifest.json"
        self.assertEqual(3, self.run_pipeline_cli(["summary", "--run-manifest", str(manifest)])["frame_count"])
        self.run_pipeline_cli(
            ["authorize", "--run-manifest", str(manifest), "--authorized-budget-usd", "0.15"]
        )
        plan_path = run_dir / "input/requests/frame-01.json"
        self.run_pipeline_cli(
            ["next-request", "--run-manifest", str(manifest), "--output", str(plan_path)]
        )
        empty = io.BytesIO()
        Image.new("RGB", (12, 18), (231, 17, 199)).save(empty, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(empty.getvalue()).decode("ascii")
        completed, actions, _ = self.execute(
            run_manifest=manifest,
            request_plan=plan_path,
            events=(
                {"operation": "submit", "value": {"request_id": "cli-rejected-request"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {"operation": "result", "value": {"data": {"images": [{"url": uri}]}}},
            ),
        )
        self.assertEqual("EVALUATION_REQUIRED", completed["reason"])
        self.assertEqual(1, actions.count("submit"))
        record = self.read_json(manifest)["requests"][0]
        evaluated = self.run_pipeline_cli(
            [
                "evaluate", "--run-manifest", str(manifest), "--frame-index", "1",
                "--raw", str(run_dir / record["result_path"]),
            ]
        )
        self.assertEqual("FAIL", evaluated["technical_result"])
        self.assertIn("FRAME_EMPTY", evaluated["failures"])
        self.assertEqual("TECHNICAL_REJECTED", self.read_json(manifest)["state"])
        self.assertIsNone(
            self.run_pipeline_cli(
                [
                    "next-request", "--run-manifest", str(manifest),
                    "--output", str(run_dir / "input/requests/frame-02.json"),
                ]
            )
        )
        self.assertFalse((run_dir / "input/requests/frame-02.json").exists())
        self.assertFalse((run_dir / "normalized/sprite-sheet.png").exists())
        self.assertFalse((run_dir / "previews/contact-sheet.png").exists())
        self.assertFalse((run_dir / "previews/loop.gif").exists())

    def test_cli_adapter_e2e_never_resubmits_unknown(self) -> None:
        source = self.inputs / "unknown-source.png"
        source.write_bytes(
            self.generated_frame_payload(
                (12, 18),
                (31, 71, 111),
                "narrow",
                transparent_background=True,
            )
        )
        self.write_json(
            self.movement,
            [
                {"frame_index": index, "phase": f"phase-{index}", "body_motion": f"pose-{index}"}
                for index in range(1, 4)
            ],
        )
        run_dir = Path(
            self.run_pipeline_cli(
                [
                    "prepare", "--source", str(source),
                    "--movement-plan", str(self.movement), "--pricing", str(self.pricing),
                    "--animation", "running", "--direction", "east", "--frame-count", "3",
                    "--frame-width", "12", "--frame-height", "18",
                    "--max-budget-usd", "0.15", "--workspace-root", str(self.workspace),
                    "--run-id", "cli-unknown",
                ],
                plain=True,
            )
        )
        manifest = run_dir / "reports/run_manifest.json"
        self.assertEqual(3, self.run_pipeline_cli(["summary", "--run-manifest", str(manifest)])["frame_count"])
        self.run_pipeline_cli(
            ["authorize", "--run-manifest", str(manifest), "--authorized-budget-usd", "0.15"]
        )
        plan_path = run_dir / "input/requests/frame-01.json"
        self.run_pipeline_cli(
            ["next-request", "--run-manifest", str(manifest), "--output", str(plan_path)]
        )
        uncertain, actions, _ = self.execute(
            run_manifest=manifest,
            request_plan=plan_path,
            events=(
                {
                    "operation": "submit",
                    "throw": True,
                    "error_name": "TimeoutError",
                    "message": "offline timeout without id",
                },
            ),
            expected_code=2,
        )
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", uncertain["reason"])
        self.assertEqual(["import", "submit"], actions)
        repeated, repeated_actions, _ = self.execute(
            run_manifest=manifest,
            request_plan=plan_path,
            expected_code=2,
        )
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", repeated["reason"])
        self.assertEqual([], repeated_actions)
        run = self.read_json(manifest)
        self.assertEqual("BLOCKED", run["state"])
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", run["terminal_reason"])
        self.assertEqual(1, len(run["requests"]))
        self.assertIsNone(run["requests"][0]["request_id"])
        self.assertTrue((run_dir / "reports/transport-claim-frame-01.json").is_file())
        self.assertFalse((run_dir / "normalized/sprite-sheet.png").exists())
        self.assertFalse((run_dir / "previews/contact-sheet.png").exists())
        self.assertFalse((run_dir / "previews/loop.gif").exists())

    def test_provider_request_id_is_a_bounded_sanitized_audit_token(self) -> None:
        unsafe_ids = (
            " surrounded ",
            "https://provider.example/request/secret",
            "data:image/png;base64,AAAA",
            "control\nvalue",
            "x" * 129,
            "prefix-fake-key-never-persist-suffix",
        )
        for index, unsafe_id in enumerate(unsafe_ids):
            with self.subTest(returned=index):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"unsafe-returned-id-{index}"
                )
                payload, actions, completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    events=(
                        {
                            "operation": "submit",
                            "value": {"request_id": unsafe_id},
                        },
                    ),
                    expected_code=2,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", payload["reason"])
                self.assertIsNone(payload["request_id"])
                self.assertEqual(["import", "submit"], actions)
                record = self.read_json(run_manifest)["requests"][0]
                self.assertIsNone(record["request_id"])
                self.assertNotIn(unsafe_id, completed.stdout)
                self.assertNotIn(unsafe_id, run_manifest.read_text(encoding="utf-8"))

        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "unsafe-preexisting-id"
        )
        self.run_dir, self.run_manifest, self.request_plan = (
            run_dir,
            run_manifest,
            request_plan,
        )
        self.seed_request("IN_QUEUE", "https://provider.example/unsafe")
        payload, actions, completed = self.execute(expected_code=2)
        self.assertEqual("RUN_MANIFEST_INVALID", payload["reason"])
        self.assertEqual([], actions)
        self.assertNotIn("https://provider.example/unsafe", completed.stdout)

    def test_concurrent_execute_processes_create_only_one_paid_submission(self) -> None:
        self.write_json(
            self.events,
            [
                {
                    "operation": "submit",
                    "delay_ms": 750,
                    "value": {"request_id": "concurrent-id"},
                },
                {"operation": "status", "value": {"status": "IN_PROGRESS"}},
            ],
        )
        self.actions.unlink(missing_ok=True)
        arguments = [
            str(self.node),
            str(self.SCRIPT),
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(self.client),
        ]
        first = subprocess.Popen(
            arguments,
            cwd=self.root,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.actions.exists() and "submit" in self.actions.read_text(
                encoding="utf-8"
            ).splitlines():
                break
            time.sleep(0.02)
        else:
            first.kill()
            first.communicate()
            self.fail("first process never reached submit")
        second = subprocess.run(
            arguments,
            cwd=self.root,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        first_stdout, first_stderr = first.communicate(timeout=10)
        self.assertEqual(0, first.returncode, first_stderr)
        self.assertEqual(2, second.returncode, second.stderr)
        self.assertEqual("REQUEST_PENDING", json.loads(first_stdout)["reason"])
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", json.loads(second.stdout)["reason"])
        actions = self.actions.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, actions.count("import"))
        self.assertEqual(1, actions.count("submit"))
        self.assertEqual(1, len(self.read_json(self.run_manifest)["requests"]))

    def test_atomic_bridge_rejects_missing_or_malicious_runtime_and_protocol(self) -> None:
        malicious_runtime = self.root / "malicious" / "python.exe"
        malicious_runtime.parent.mkdir()
        shutil.copy2(self.node, malicious_runtime)
        for name, extra_environment in (
            (
                "missing-runtime",
                {"TOWER_SPRITE_PYTHON_EXE": str(self.root / "missing" / "python.exe")},
            ),
            (
                "malicious-runtime",
                {"TOWER_SPRITE_PYTHON_EXE": str(malicious_runtime)},
            ),
            (
                "protocol",
                {"TOWER_SPRITE_TEST_ATOMIC_PROTOCOL": "malicious-protocol"},
            ),
        ):
            with self.subTest(case=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"bad-atomic-{name}"
                )
                payload, actions, completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    expected_code=2,
                    extra_environment=extra_environment,
                )
                self.assertEqual("ATOMIC_RUNTIME_INVALID", payload["reason"])
                self.assertEqual([], actions)
                self.assertFalse(
                    (run_dir / "reports/transport-claim-frame-01.json").exists()
                )
                self.assertEqual("", completed.stderr)

    def atomic_identity(self, candidate: Path) -> list[int]:
        identity = pipeline._windows_path_identity(candidate, False)
        return [identity.volume_serial, identity.file_index, identity.attributes]

    def wait_for_file(self, candidate: Path) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if candidate.is_file():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {candidate.name}")

    @unittest.skipUnless(os.name == "nt", "requires Win32 handle semantics")
    def test_atomic_new_publishes_the_verified_handle_with_isolated_child(self) -> None:
        self.write_json(
            self.events,
            [
                {"operation": "submit", "value": {"request_id": "atomic-new-id"}},
                {"operation": "status", "value": {"status": "IN_QUEUE"}},
            ],
        )
        signal = self.root / "atomic-new.signal"
        arguments = [
            str(self.node),
            str(self.SCRIPT),
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(self.client),
        ]
        process = subprocess.Popen(
            arguments,
            cwd=self.root,
            env={
                **self.environment(),
                "TOWER_SPRITE_ATOMIC_TEST_SIGNAL": str(signal),
                "TOWER_SPRITE_ATOMIC_TEST_OPERATION": "new",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for_file(signal)
        before = self.read_json(signal)
        self.assertEqual("new", before["operation"])
        self.assertFalse(before["fal_key_present"])
        self.assertEqual(["-c"], before["argv"])
        temporary = Path(before["temporary"])
        with self.assertRaises(OSError):
            temporary.write_bytes(b"HOSTILE")
        with self.assertRaises(OSError):
            temporary.replace(self.root / "stolen-new.partial")
        with self.assertRaises(OSError):
            (self.run_dir / "reports").replace(self.root / "stolen-reports")
        Path(f"{signal}.ack").write_text("continue", encoding="utf-8")

        done = Path(f"{signal}.done")
        self.wait_for_file(done)
        published = self.read_json(done)
        target = Path(published["target"])
        self.assertEqual(before["identity"], published["identity"])
        self.assertEqual(published["identity"], self.atomic_identity(target))
        with self.assertRaises(OSError):
            target.write_bytes(b"HOSTILE")
        Path(f"{done}.ack").write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("REQUEST_PENDING", json.loads(stdout)["reason"])
        self.assertNotIn("fake-key-never-persist", stdout + stderr)

    @unittest.skipUnless(os.name == "nt", "requires Win32 handle semantics")
    def test_atomic_replace_guards_target_parent_and_verified_temporary(self) -> None:
        self.write_json(
            self.events,
            [
                {"operation": "submit", "value": {"request_id": "atomic-replace-id"}},
                {"operation": "status", "value": {"status": "IN_PROGRESS"}},
            ],
        )
        signal = self.root / "atomic-replace.signal"
        arguments = [
            str(self.node),
            str(self.SCRIPT),
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(self.client),
        ]
        process = subprocess.Popen(
            arguments,
            cwd=self.root,
            env={
                **self.environment(),
                "TOWER_SPRITE_ATOMIC_TEST_SIGNAL": str(signal),
                "TOWER_SPRITE_ATOMIC_TEST_OPERATION": "replace",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for_file(signal)
        before = self.read_json(signal)
        self.assertEqual("replace", before["operation"])
        target = self.run_manifest
        target_before = target.read_bytes()
        temporary = Path(before["temporary"])
        with self.assertRaises(OSError):
            temporary.write_bytes(b"HOSTILE")
        with self.assertRaises(OSError):
            temporary.replace(self.root / "stolen-replace.partial")
        with self.assertRaises(OSError):
            target.write_bytes(b"HOSTILE")
        with self.assertRaises(OSError):
            target.parent.replace(self.root / "stolen-reports")
        self.assertEqual(target_before, target.read_bytes())
        Path(f"{signal}.ack").write_text("continue", encoding="utf-8")

        done = Path(f"{signal}.done")
        self.wait_for_file(done)
        published = self.read_json(done)
        self.assertEqual(before["identity"], published["identity"])
        self.assertEqual(published["identity"], self.atomic_identity(target))
        Path(f"{done}.ack").write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(0, process.returncode, stderr)
        self.assertEqual("REQUEST_PENDING", json.loads(stdout)["reason"])

    @unittest.skipUnless(os.name == "nt", "requires Win32 handle semantics")
    def test_atomic_replace_detects_target_swap_before_publishing_temporary(self) -> None:
        self.write_json(self.events, [])
        signal = self.root / "atomic-target-swap.signal"
        arguments = [
            str(self.node),
            str(self.SCRIPT),
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(self.client),
        ]
        process = subprocess.Popen(
            arguments,
            cwd=self.root,
            env={
                **self.environment(),
                "TOWER_SPRITE_ATOMIC_TEST_SIGNAL": str(signal),
                "TOWER_SPRITE_ATOMIC_TEST_OPERATION": "replace",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for_file(signal)
        original = self.run_manifest.read_bytes()
        stolen = self.root / "stolen-run.json"
        self.run_manifest.replace(stolen)
        Path(f"{signal}.ack").write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(2, process.returncode, stderr)
        self.assertEqual("RUN_MANIFEST_CHANGED", json.loads(stdout)["reason"])
        self.assertFalse(self.run_manifest.exists())
        self.assertEqual(original, stolen.read_bytes())
        self.assertFalse(self.actions.exists())

    @unittest.skipUnless(os.name == "nt", "requires Win32 handle semantics")
    def test_atomic_no_replace_loses_target_race_without_overwrite_or_import(self) -> None:
        self.write_json(self.events, [])
        signal = self.root / "atomic-target-race.signal"
        arguments = [
            str(self.node),
            str(self.SCRIPT),
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(self.client),
        ]
        process = subprocess.Popen(
            arguments,
            cwd=self.root,
            env={
                **self.environment(),
                "TOWER_SPRITE_ATOMIC_TEST_SIGNAL": str(signal),
                "TOWER_SPRITE_ATOMIC_TEST_OPERATION": "new",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.wait_for_file(signal)
        claim = self.run_dir / "reports/transport-claim-frame-01.json"
        claim.write_bytes(b"ATTACKER-CLAIM")
        Path(f"{signal}.ack").write_text("continue", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(2, process.returncode, stderr)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", json.loads(stdout)["reason"])
        self.assertEqual(b"ATTACKER-CLAIM", claim.read_bytes())
        self.assertFalse(self.actions.exists())

    def test_process_death_after_claim_blocks_all_future_submission(self) -> None:
        killer = self.root / "killer-client.mjs"
        killer.write_text("process.exit(73);\n", encoding="utf-8")
        self.write_json(self.events, [])
        arguments = [
            str(self.node),
            str(self.SCRIPT),
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(killer),
        ]
        died = subprocess.run(
            arguments,
            cwd=self.root,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(73, died.returncode, died.stderr)
        run = self.read_json(self.run_manifest)
        self.assertEqual("SUBMITTING", run["requests"][0]["status"])
        claim = self.run_dir / "reports/transport-claim-frame-01.json"
        self.assertTrue(claim.is_file())

        blocked, actions, _ = self.execute(expected_code=2)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", blocked["reason"])
        self.assertEqual([], actions)
        self.assertEqual(1, len(self.read_json(self.run_manifest)["requests"]))

    def test_preexisting_malformed_claim_blocks_without_import_or_run_rewrite(self) -> None:
        claim = self.run_dir / "reports/transport-claim-frame-01.json"
        claim.write_bytes(b"ATTACKER")
        before = self.run_manifest.read_bytes()
        payload, actions, _ = self.execute(expected_code=2)
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", payload["reason"])
        self.assertEqual([], actions)
        self.assertEqual(before, self.run_manifest.read_bytes())

    def seed_request(self, status: str, request_id: str = "existing-id") -> None:
        plan = self.read_json(self.request_plan)
        run = self.read_json(self.run_manifest)
        run["requests"] = [
            {
                "frame_index": 1,
                "attempt": 1,
                "model": MODEL,
                "request_fingerprint": plan["request_fingerprint"],
                "request_id": request_id,
                "status": status,
                "estimated_cost_usd": "0.05",
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": None,
            }
        ]
        self.write_json(self.run_manifest, run)

    def seed_accepted_frame_one(self) -> Path:
        raw_dir = self.run_dir / "raw/frame-01"
        raw_dir.mkdir(exist_ok=True)
        raw = raw_dir / "attempt-01.png"
        raw.write_bytes(self.generated_png_payload())
        first_plan = self.read_json(self.request_plan)
        run = self.read_json(self.run_manifest)
        run["requests"] = [
            {
                "frame_index": 1,
                "attempt": 1,
                "model": MODEL,
                "request_fingerprint": first_plan["request_fingerprint"],
                "request_id": "accepted-frame-1",
                "status": "COMPLETED",
                "estimated_cost_usd": "0.05",
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": "raw/frame-01/attempt-01.png",
                "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            }
        ]
        self.write_json(self.run_manifest, run)
        report = pipeline.record_evaluation(self.run_manifest, 1, raw)
        self.assertEqual("PASS", report["technical_result"])
        second_plan = self.run_dir / "input/requests/frame-02.json"
        pipeline.next_request_plan(self.run_manifest, second_plan)
        return second_plan

    def forge_accepted_frame_one(self) -> None:
        normalized = self.run_dir / "normalized/frames/frame-01.png"
        report_path = self.run_dir / "reports/frames/frame-01.json"
        self.write_character(normalized, (201, 31, 41, 255))
        normalized_sha256 = hashlib.sha256(normalized.read_bytes()).hexdigest()
        report = self.read_json(report_path)
        report["advisory"]["visible_pixels"] += 1
        report["provenance"]["normalized_sha256"] = normalized_sha256
        self.write_json(report_path, report)
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        run = self.read_json(self.run_manifest)
        run["frames"][0]["normalized_sha256"] = normalized_sha256
        run["frames"][0]["report_sha256"] = report_sha256
        run["artifacts"]["evaluation"]["normalized_sha256"] = normalized_sha256
        run["artifacts"]["evaluation"]["report_sha256"] = report_sha256
        second_plan_path = self.run_dir / "input/requests/frame-02.json"
        if second_plan_path.exists():
            second_plan = self.read_json(second_plan_path)
            unsigned = copy.deepcopy(second_plan)
            unsigned.pop("request_fingerprint")
            manifest = self.read_json(self.run_dir / "reports/manifest.json")
            second_plan["request_fingerprint"] = contract.canonical_sha256(
                {
                    "schema_version": 1,
                    "request": unsigned,
                    "run_id": run["artifacts"]["run_id"],
                    "manifest_sha256": run["manifest_sha256"],
                    "references": [
                        {
                            "role": "master",
                            "path": "input/source/master.png",
                            "sha256": manifest["source"]["normalized_sha256"],
                        },
                        {
                            "role": "previous_accepted",
                            "path": "normalized/frames/frame-01.png",
                            "sha256": normalized_sha256,
                        },
                    ],
                }
            )
            self.write_json(second_plan_path, second_plan)
            for request in run["requests"]:
                if request["frame_index"] == 2:
                    request["request_fingerprint"] = second_plan["request_fingerprint"]
        self.write_json(self.run_manifest, run)

    def test_existing_queue_and_progress_resume_same_id_without_submit(self) -> None:
        for initial, returned in (
            ("IN_QUEUE", "IN_PROGRESS"),
            ("IN_PROGRESS", "IN_QUEUE"),
        ):
            with self.subTest(initial=initial):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"resume-{initial.lower()}"
                )
                self.run_dir, self.run_manifest, self.request_plan = (
                    run_dir,
                    run_manifest,
                    request_plan,
                )
                self.seed_request(initial)
                payload, actions, _ = self.execute(
                    events=(
                        {"operation": "status", "value": {"status": returned}},
                    )
                )
                self.assertEqual("REQUEST_PENDING", payload["reason"])
                self.assertEqual(["import", "status:existing-id"], actions)
                self.assertEqual(returned, self.read_json(run_manifest)["requests"][0]["status"])

    def test_tampered_cost_or_future_request_blocks_before_client_import(self) -> None:
        self.seed_request("IN_QUEUE")
        run = self.read_json(self.run_manifest)
        run["requests"][0]["estimated_cost_usd"] = "0.00000000000000000001"
        self.write_json(self.run_manifest, run)
        self.assert_preimport_block("RUN_MANIFEST_INVALID")

        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "future-request-record"
        )
        self.run_dir, self.run_manifest, self.request_plan = (
            run_dir,
            run_manifest,
            request_plan,
        )
        run = self.read_json(run_manifest)
        run["requests"] = [
            {
                "frame_index": 2,
                "attempt": 1,
                "model": MODEL,
                "request_fingerprint": "f" * 64,
                "request_id": "future-id",
                "status": "IN_QUEUE",
                "estimated_cost_usd": "0.05",
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": None,
            }
        ]
        self.write_json(run_manifest, run)
        self.assert_preimport_block("RUN_MANIFEST_INVALID")

    def test_request_timestamp_requires_explicit_zone_and_accepts_rfc3339_offset(self) -> None:
        self.seed_request("IN_QUEUE")
        run = self.read_json(self.run_manifest)
        run["requests"][0]["submitted_at"] = "2026-09-02T12:00:00"
        self.write_json(self.run_manifest, run)
        self.assert_preimport_block("RUN_MANIFEST_INVALID")

        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "request-timestamp-offset"
        )
        self.run_dir, self.run_manifest, self.request_plan = (
            run_dir,
            run_manifest,
            request_plan,
        )
        self.seed_request("IN_QUEUE")
        run = self.read_json(run_manifest)
        run["requests"][0]["submitted_at"] = "2026-09-02T09:00:00-03:00"
        self.write_json(run_manifest, run)

        payload, actions, _ = self.execute(
            events=({"operation": "status", "value": {"status": "IN_PROGRESS"}},)
        )

        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["import", "status:existing-id"], actions)

    def test_request_timestamp_rejects_impossible_calendar_hour_and_offset_values(self) -> None:
        invalid_values = (
            "2026-02-30T12:00:00Z",
            "2025-02-29T12:00:00Z",
            "2026-13-01T12:00:00Z",
            "2026-09-02T24:00:00Z",
            "2026-09-02T12:60:00Z",
            "2026-09-02T12:00:60Z",
            "2026-09-02T12:00:00+24:00",
            "2026-09-02T12:00:00-03:60",
        )
        for index, submitted_at in enumerate(invalid_values):
            with self.subTest(submitted_at=submitted_at):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"invalid-request-timestamp-{index}"
                )
                self.run_dir, self.run_manifest, self.request_plan = (
                    run_dir,
                    run_manifest,
                    request_plan,
                )
                self.seed_request("IN_QUEUE")
                run = self.read_json(run_manifest)
                run["requests"][0]["submitted_at"] = submitted_at
                self.write_json(run_manifest, run)
                self.assert_preimport_block("RUN_MANIFEST_INVALID")

    def test_rehashed_authorized_budget_tamper_blocks_before_client_import(self) -> None:
        manifest_path = self.run_dir / "reports/manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["authorization"]["authorized_budget_usd"] = "999"
        self.write_json(manifest_path, manifest)
        run = self.read_json(self.run_manifest)
        run["manifest_sha256"] = contract.canonical_sha256(manifest)
        self.write_json(self.run_manifest, run)
        before = self.run_manifest.read_bytes()

        payload = self.assert_preimport_block("AUTHORIZED_BUDGET_MISMATCH")

        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual(before, self.run_manifest.read_bytes())

    def test_full_ledger_rejects_impossible_frames_requests_and_raw_binding(self) -> None:
        normalized = self.run_dir / "normalized/frames/frame-01.png"
        self.write_character(normalized, (5, 6, 7, 255))
        normalized_record = {
            "frame_index": 1,
            "status": "ACCEPTED",
            "normalized_path": "normalized/frames/frame-01.png",
            "normalized_sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
        }
        impossible = self.read_json(self.run_manifest)
        impossible["frames"] = [normalized_record]
        self.write_json(self.run_manifest, impossible)
        self.assert_preimport_block("RUN_MANIFEST_INVALID")

        for name, reason, mutation in (
            ("missing-request", "RUN_MANIFEST_INVALID", lambda run: run["requests"].clear()),
            (
                "fingerprint",
                "RUN_MANIFEST_INVALID",
                lambda run: run["requests"][0].update(
                    {"request_fingerprint": "0" * 64}
                ),
            ),
            (
                "raw-hash",
                "RAW_PROVENANCE_MISMATCH",
                lambda run: run["requests"][0].update({"raw_sha256": "0" * 64}),
            ),
            (
                "duplicate-frame",
                "RUN_MANIFEST_INVALID",
                lambda run: run["frames"].append(copy.deepcopy(run["frames"][0])),
            ),
            (
                "future-frame",
                "RUN_MANIFEST_INVALID",
                lambda run: run["frames"].append(
                    {"frame_index": 2, "status": "REJECTED"}
                ),
            ),
        ):
            with self.subTest(case=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"bad-ledger-{name}"
                )
                self.run_dir, self.run_manifest, self.request_plan = (
                    run_dir,
                    run_manifest,
                    request_plan,
                )
                second_plan = self.seed_accepted_frame_one()
                altered = self.read_json(run_manifest)
                mutation(altered)
                self.write_json(run_manifest, altered)
                self.assert_preimport_block(
                    reason,
                    run_manifest=run_manifest,
                    request_plan=second_plan,
                )

        for name, status, request_id, safe_error in (
            ("completed-safe-error", "COMPLETED", "coherence-id", "NetworkError"),
            ("failed-live-run", "FAILED", "coherence-id", "FalError"),
            (
                "unknown-live-run",
                "SUBMISSION_STATUS_UNKNOWN",
                None,
                "TimeoutError",
            ),
        ):
            with self.subTest(coherence=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"bad-coherence-{name}"
                )
                self.run_dir, self.run_manifest, self.request_plan = (
                    run_dir,
                    run_manifest,
                    request_plan,
                )
                self.seed_request(status, request_id or "temporary")
                altered = self.read_json(run_manifest)
                altered["requests"][0]["request_id"] = request_id
                altered["requests"][0]["safe_error_type"] = safe_error
                self.write_json(run_manifest, altered)
                self.assert_preimport_block("RUN_MANIFEST_INVALID")

        with self.subTest(case="request-ledger-order"):
            run_dir, run_manifest, request_plan = self.prepare_authorized(
                "bad-ledger-request-order"
            )
            self.run_dir, self.run_manifest, self.request_plan = (
                run_dir,
                run_manifest,
                request_plan,
            )
            second_plan = self.seed_accepted_frame_one()
            altered = self.read_json(run_manifest)
            plan = self.read_json(second_plan)
            altered["requests"].append(
                {
                    "frame_index": 2,
                    "attempt": 1,
                    "model": MODEL,
                    "request_fingerprint": plan["request_fingerprint"],
                    "request_id": "current-frame-2",
                    "status": "IN_QUEUE",
                    "estimated_cost_usd": "0.05",
                    "submitted_at": "2026-09-02T12:00:01Z",
                    "result_path": None,
                }
            )
            altered["requests"].reverse()
            self.write_json(run_manifest, altered)
            self.assert_preimport_block(
                "RUN_MANIFEST_INVALID",
                run_manifest=run_manifest,
                request_plan=second_plan,
            )

    def test_coordinated_prior_evaluation_tamper_blocks_before_client_import(self) -> None:
        second_plan = self.seed_accepted_frame_one()
        self.forge_accepted_frame_one()
        before = self.run_manifest.read_bytes()

        payload, actions, completed = self.execute(
            request_plan=second_plan,
            expected_code=2,
        )

        self.assertEqual("RUN_MANIFEST_INVALID", payload["reason"])
        self.assertEqual([], actions)
        self.assertEqual("", completed.stderr)
        self.assertEqual(before, self.run_manifest.read_bytes())

    def test_resume_existing_id_ignores_new_submit_pricing_gate(self) -> None:
        self.seed_request("IN_PROGRESS", "resume-despite-stale-price")
        stale = self.inputs / "resume-stale-pricing.json"
        self.write_json(
            stale,
            self.valid_sanitized_pricing(
                verified_at=(datetime.now(timezone.utc) - timedelta(days=2))
                .isoformat()
                .replace("+00:00", "Z")
            ),
        )
        payload, actions, _ = self.execute(
            pricing=stale,
            events=(
                {"operation": "status", "value": {"status": "IN_PROGRESS"}},
            ),
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(
            ["import", "status:resume-despite-stale-price"],
            actions,
        )

    def test_status_failure_keeps_durable_id_for_later_resume(self) -> None:
        self.seed_request("IN_QUEUE", "durable-id")
        payload, actions, _ = self.execute(
            events=(
                {
                    "operation": "status",
                    "throw": True,
                    "error_name": "NetworkError",
                },
            ),
            expected_code=2,
        )
        self.assertEqual("REQUEST_STATUS_UNKNOWN", payload["reason"])
        self.assertEqual(1, payload["submitted_requests"])
        self.assertEqual("IN_QUEUE", payload["status"])
        self.assertEqual("durable-id", payload["request_id"])
        self.assertEqual(["import", "status:durable-id"], actions)
        record = self.read_json(self.run_manifest)["requests"][0]
        self.assertEqual("durable-id", record["request_id"])
        self.assertEqual("IN_QUEUE", record["status"])

    def test_completed_result_awaiting_evaluation_and_terminal_run_never_import(self) -> None:
        raw = self.run_dir / "raw/frame-01"
        raw.mkdir()
        result_path = raw / "attempt-01.png"
        result_path.write_bytes(self.png_bytes)
        plan = self.read_json(self.request_plan)
        run = self.read_json(self.run_manifest)
        run["requests"] = [
            {
                "frame_index": 1,
                "attempt": 1,
                "model": MODEL,
                "request_fingerprint": plan["request_fingerprint"],
                "request_id": "completed-id",
                "status": "COMPLETED",
                "estimated_cost_usd": "0.05",
                "submitted_at": "2026-09-02T12:00:00Z",
                "result_path": "raw/frame-01/attempt-01.png",
                "raw_sha256": hashlib.sha256(self.png_bytes).hexdigest(),
            }
        ]
        self.write_json(self.run_manifest, run)
        self.assert_preimport_block("EVALUATION_REQUIRED")

        terminal_dir, terminal_run, terminal_plan = self.prepare_authorized(
            "terminal-run"
        )
        terminal = self.read_json(terminal_run)
        terminal["state"] = "TECHNICAL_REJECTED"
        terminal["terminal_reason"] = "FRAME_EMPTY"
        self.write_json(terminal_run, terminal)
        self.assert_preimport_block(
            "RUN_MANIFEST_INVALID",
            run_manifest=terminal_run,
            request_plan=terminal_plan,
        )

    def test_impossible_technical_rejections_block_before_client_import(self) -> None:
        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "minimal-evaluator-rejection"
        )
        self.run_dir, self.run_manifest, self.request_plan = (
            run_dir,
            run_manifest,
            request_plan,
        )
        transported, _actions, _completed = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "minimal-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            )
        )
        self.assertEqual("EVALUATION_REQUIRED", transported["reason"])
        run = self.read_json(run_manifest)
        run["frames"] = [{"frame_index": 1, "status": "REJECTED"}]
        run["state"] = "TECHNICAL_REJECTED"
        run["terminal_reason"] = "FRAME_EMPTY"
        self.write_json(run_manifest, run)
        self.assert_preimport_block("RUN_MANIFEST_INVALID")

        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "invented-evaluator-rejection"
        )
        self.run_dir, self.run_manifest, self.request_plan = (
            run_dir,
            run_manifest,
            request_plan,
        )
        self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "invented-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            )
        )
        raw = run_dir / "raw/frame-01/attempt-01.png"
        report = pipeline.record_evaluation(run_manifest, 1, raw)
        self.assertEqual(["FRAME_EMPTY"], report["failures"])
        report_path = run_dir / "reports/frames/frame-01.json"
        altered_report = self.read_json(report_path)
        altered_report["failures"] = ["FRAME_INVENTED"]
        altered_report["normalization"]["failure"] = "FRAME_INVENTED"
        self.write_json(report_path, altered_report)
        altered_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        run = self.read_json(run_manifest)
        run["terminal_reason"] = "FRAME_INVENTED"
        run["frames"][0]["report_sha256"] = altered_hash
        run["artifacts"]["evaluation"]["report_sha256"] = altered_hash
        run["artifacts"]["evaluation"]["failure"] = "FRAME_INVENTED"
        self.write_json(run_manifest, run)
        self.assert_preimport_block("RUN_MANIFEST_INVALID")

    def test_completed_status_fetches_one_png_and_binds_hash_before_evaluation(self) -> None:
        payload, actions, completed = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "completed-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            )
        )
        self.assertEqual("EVALUATION_REQUIRED", payload["reason"])
        self.assertEqual(
            ["import", "submit", "status:completed-id", "result:completed-id"],
            actions,
        )
        expected_path = self.run_dir / "raw/frame-01/attempt-01.png"
        self.assertEqual(self.png_bytes, expected_path.read_bytes())
        request = self.read_json(self.run_manifest)["requests"][0]
        self.assertEqual(REQUEST_RECORD_BASE_KEYS | {"raw_sha256"}, set(request))
        self.assertEqual("COMPLETED", request["status"])
        self.assertEqual("raw/frame-01/attempt-01.png", request["result_path"])
        self.assertEqual(hashlib.sha256(self.png_bytes).hexdigest(), request["raw_sha256"])
        audit = self.run_manifest.read_text(encoding="utf-8") + completed.stdout
        self.assertNotIn("data:image", audit)
        self.assertNotIn("fake-key-never-persist", audit)

    def test_python_frame_rejection_keeps_completed_transport_terminal_for_node(self) -> None:
        transported, actions, _ = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "evaluated-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            )
        )
        self.assertEqual("EVALUATION_REQUIRED", transported["reason"])
        self.assertEqual(
            ["import", "submit", "status:evaluated-id", "result:evaluated-id"],
            actions,
        )
        raw = self.run_dir / "raw/frame-01/attempt-01.png"
        raw_before = raw.read_bytes()
        request_before = copy.deepcopy(self.read_json(self.run_manifest)["requests"][0])

        report = pipeline.record_evaluation(self.run_manifest, 1, raw)

        self.assertEqual("FAIL", report["technical_result"])
        self.assertEqual(["FRAME_EMPTY"], report["failures"])
        rejected = self.read_json(self.run_manifest)
        self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
        self.assertEqual("FRAME_EMPTY", rejected["terminal_reason"])
        self.assertEqual("REJECTED", rejected["frames"][0]["status"])
        self.assertEqual(request_before, rejected["requests"][0])

        rerun, rerun_actions, _ = self.execute(expected_code=2)

        self.assertEqual(
            {
                "state": "BLOCKED",
                "reason": "RUN_TERMINAL",
                "frame_index": 1,
                "request_id": "evaluated-id",
                "status": "COMPLETED",
                "submitted_requests": 1,
            },
            rerun,
        )
        self.assertEqual([], rerun_actions)
        after = self.read_json(self.run_manifest)
        self.assertEqual(request_before, after["requests"][0])
        self.assertEqual(raw_before, raw.read_bytes())
        self.assertEqual(rejected, after)

    def test_structural_png_accepted_by_transport_is_durably_rejected_by_python(self) -> None:
        valid = self.generated_png_payload()
        cases = [("idat", self.structurally_valid_but_undecodable_png())]
        for kind, payload in ((b"pHYs", b"\0"), (b"sRGB", b"")):
            chunk = (
                struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )
            cases.append((kind.decode("ascii").lower(), valid[:33] + chunk + valid[33:]))
        for label, malformed in cases:
            with self.subTest(chunk=label):
                self.run_dir, self.run_manifest, self.request_plan = self.prepare_authorized(
                    f"malformed-transport-{label}"
                )
                self.assertTrue(pipeline._task5_structural_png(malformed))
                self.png_uri = "data:image/png;base64," + base64.b64encode(malformed).decode("ascii")
                transported, actions, _ = self.execute(
                    events=(
                        {"operation": "submit", "value": {"request_id": "malformed-id"}},
                        {"operation": "status", "value": {"status": "COMPLETED"}},
                        {
                            "operation": "result",
                            "value": {"data": {"images": [{"url": self.png_uri}]}},
                        },
                    )
                )
                self.assertEqual("EVALUATION_REQUIRED", transported["reason"])
                self.assertEqual(
                    ["import", "submit", "status:malformed-id", "result:malformed-id"],
                    actions,
                )
                raw = self.run_dir / "raw/frame-01/attempt-01.png"
                self.assertEqual(malformed, raw.read_bytes())

                report = pipeline.record_evaluation(self.run_manifest, 1, raw)

                self.assertEqual(["FRAME_NOT_PNG"], report["failures"])
                rejected = self.read_json(self.run_manifest)
                self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
                self.assertEqual("REJECTED", rejected["frames"][0]["status"])
                self.assertEqual("COMPLETED", rejected["requests"][0]["status"])
                before = self.run_manifest.read_bytes()
                terminal, terminal_actions, _ = self.execute(expected_code=2)
                self.assertEqual("RUN_TERMINAL", terminal["reason"])
                self.assertEqual([], terminal_actions)
                self.assertEqual(before, self.run_manifest.read_bytes())

    def test_raw_publish_before_json_update_is_recovered_without_client_import(self) -> None:
        self.write_json(
            self.events,
            [
                {"operation": "submit", "value": {"request_id": "raw-crash-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            ],
        )
        arguments = [
            "execute",
            "--run-manifest",
            str(self.run_manifest),
            "--request-plan",
            str(self.request_plan),
            "--pricing",
            str(self.pricing),
            "--client-module",
            str(self.client),
        ]
        died = subprocess.run(
            [str(self.node), str(self.SCRIPT), *arguments],
            cwd=self.root,
            env={
                **self.environment(),
                "TOWER_SPRITE_TEST_EXIT_AFTER_RAW_PUBLISH": "73",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(73, died.returncode, died.stderr)
        raw = self.run_dir / "raw/frame-01/attempt-01.png"
        self.assertEqual(self.png_bytes, raw.read_bytes())
        interrupted = self.read_json(self.run_manifest)["requests"][0]
        self.assertEqual("COMPLETED", interrupted["status"])
        self.assertIsNone(interrupted["result_path"])
        self.assertEqual(
            hashlib.sha256(self.png_bytes).hexdigest(),
            interrupted["raw_sha256"],
        )

        recovered, actions, _ = self.execute(expected_code=0)
        self.assertEqual("EVALUATION_REQUIRED", recovered["reason"])
        self.assertEqual([], actions)
        record = self.read_json(self.run_manifest)["requests"][0]
        self.assertEqual("raw/frame-01/attempt-01.png", record["result_path"])
        self.assertEqual(hashlib.sha256(self.png_bytes).hexdigest(), record["raw_sha256"])

    def test_unbound_preexisting_png_is_not_accepted_as_crash_recovery(self) -> None:
        self.seed_request("COMPLETED", "unbound-id")
        frame_dir = self.run_dir / "raw/frame-01"
        frame_dir.mkdir()
        planted = self.png_payload((1, 2, 3, 255))
        destination = frame_dir / "attempt-01.png"
        destination.write_bytes(planted)

        payload, actions, _ = self.execute(
            events=(
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            ),
            expected_code=2,
        )
        self.assertEqual("RESULT_DESTINATION_EXISTS", payload["reason"])
        self.assertEqual([], actions)
        self.assertEqual(planted, destination.read_bytes())
        record = self.read_json(self.run_manifest)["requests"][0]
        self.assertIsNone(record["result_path"])
        self.assertNotIn("raw_sha256", record)

    def test_mutated_or_linked_reference_blocks_before_client_import(self) -> None:
        master = self.run_dir / "input/source/master.png"
        original = master.read_bytes()
        master.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        self.assert_preimport_block("REFERENCE_HASH_MISMATCH")

        linked_dir, linked_run, linked_plan = self.prepare_authorized("linked-reference")
        linked_master = linked_dir / "input/source/master.png"
        target = self.root / "linked-master-target.png"
        linked_master.replace(target)
        try:
            os.symlink(target, linked_master)
        except OSError:
            # A hardlink is still an invalid non-single-link reference and needs no privilege.
            os.link(target, linked_master)
        self.assert_preimport_block(
            "WORKSPACE_CONFINEMENT_FAILED",
            run_manifest=linked_run,
            request_plan=linked_plan,
        )

    def test_noncanonical_movement_or_request_bytes_block_before_import(self) -> None:
        movement = self.run_dir / "input/movement-plan.json"
        movement.write_bytes(b" " + movement.read_bytes())
        self.assert_preimport_block("MOVEMENT_PLAN_MISMATCH")

        run_dir, run_manifest, request_plan = self.prepare_authorized(
            "noncanonical-request"
        )
        request_plan.write_bytes(b" " + request_plan.read_bytes())
        self.assert_preimport_block(
            "REQUEST_PLAN_INVALID",
            run_manifest=run_manifest,
            request_plan=request_plan,
        )

    def test_fake_client_injection_is_test_only_and_confined(self) -> None:
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name) / "outside-client.mjs"
        outside.write_text("export const fal = {};\n", encoding="utf-8")
        self.write_json(self.events, [])
        payload, _ = self.run_cli(
            [
                "execute",
                "--run-manifest",
                str(self.run_manifest),
                "--request-plan",
                str(self.request_plan),
                "--pricing",
                str(self.pricing),
                "--client-module",
                str(outside),
            ],
            expected_code=2,
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
        self.assertFalse(self.actions.exists())

    @unittest.skipUnless(os.name == "nt", "ADS semantics require Windows")
    def test_all_supplied_paths_reject_alternate_data_streams(self) -> None:
        client_ads = Path(f"{self.client}:transport")
        client_ads.write_bytes(self.client.read_bytes())
        payload, actions, _ = self.execute(
            client_module=client_ads,
            expected_code=2,
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
        self.assertEqual([], actions)

        pricing_ads = Path(f"{self.pricing}:pricing")
        pricing_ads.write_bytes(self.pricing.read_bytes())
        payload, actions, _ = self.execute(
            pricing=pricing_ads,
            expected_code=2,
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
        self.assertEqual([], actions)

        raw_fixture = self.inputs / "official-for-ads.json"
        self.write_json(raw_fixture, self.valid_official_response())
        fixture_ads = Path(f"{raw_fixture}:fixture")
        fixture_ads.write_bytes(raw_fixture.read_bytes())
        output = self.inputs / "ads-evidence.json"
        payload, _ = self.run_cli(
            ["pricing", "--source", str(fixture_ads), "--output", str(output)],
            expected_code=2,
            key=None,
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
        self.assertFalse(output.exists())

        output_ads = Path(f"{output}:evidence")
        payload, _ = self.run_cli(
            ["pricing", "--source", str(raw_fixture), "--output", str(output_ads)],
            expected_code=2,
            key=None,
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])

        for supplied_flag, supplied_path in (
            ("--run-manifest", Path(f"{self.run_manifest}:run")),
            ("--request-plan", Path(f"{self.request_plan}:request")),
        ):
            arguments = [
                "execute",
                "--run-manifest",
                str(self.run_manifest),
                "--request-plan",
                str(self.request_plan),
                "--pricing",
                str(self.pricing),
                "--client-module",
                str(self.client),
            ]
            arguments[arguments.index(supplied_flag) + 1] = str(supplied_path)
            payload, _ = self.run_cli(arguments, expected_code=2)
            self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
            self.assertFalse(self.actions.exists())

        payload, _ = self.run_cli(
            [
                "execute",
                "--run-manifest",
                str(self.run_manifest),
                "--request-plan",
                str(self.request_plan),
                "--pricing",
                str(self.pricing),
                "--client-module",
                str(self.client),
            ],
            expected_code=2,
            extra_environment={"NODE_ENV": "production"},
        )
        self.assertEqual("INVALID_ARGUMENTS", payload["reason"])
        self.assertFalse(self.actions.exists())

    def test_provider_error_is_reduced_to_safe_type_without_body_or_key(self) -> None:
        payload, actions, completed = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "provider-error"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "throw": True,
                    "error_name": "FalError",
                    "status": 422,
                    "message": "fake-key-never-persist data:image/png;base64,SECRET",
                    "body": {"detail": [{"type": "image_too_small", "input": "SECRET"}]},
                },
            ),
            expected_code=2,
        )
        self.assertEqual("REQUEST_FAILED", payload["reason"])
        self.assertEqual(
            ["import", "submit", "status:provider-error", "result:provider-error"],
            actions,
        )
        record = self.read_json(self.run_manifest)["requests"][0]
        self.assertEqual("FAILED", record["status"])
        self.assertEqual("image_too_small", record["safe_error_type"])
        audit = self.run_manifest.read_text(encoding="utf-8") + completed.stdout
        self.assertNotIn("fake-key-never-persist", audit)
        self.assertNotIn("data:image", audit)
        self.assertNotIn("SECRET", audit)

        repeated, repeated_actions, _ = self.execute(
            expected_code=2,
            key="image",
        )
        self.assertEqual("RUN_TERMINAL", repeated["reason"])
        self.assertEqual(1, repeated["submitted_requests"])
        self.assertEqual("provider-error", repeated["request_id"])
        self.assertEqual("FAILED", repeated["status"])
        self.assertEqual([], repeated_actions)

    def test_submit_error_omits_every_candidate_rejected_by_secret_policy(self) -> None:
        cases = (
            ("exact", "SubmitError", "SubmitError", "SubmitError"),
            (
                "key-contains-candidates",
                "image_too_small-FalError-SubmitError",
                "image_too_small",
                "FalError",
            ),
            ("candidates-contain-key", "Submit", "SubmitError", "SubmitError"),
        )
        for name, key, detail_type, error_name in cases:
            with self.subTest(relation=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"unsafe-submit-audit-{name}"
                )
                payload, actions, completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    events=(
                        {
                            "operation": "submit",
                            "throw": True,
                            "error_name": error_name,
                            "body": {"detail": [{"type": detail_type}]},
                        },
                    ),
                    expected_code=2,
                    key=key,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", payload["reason"])
                self.assertEqual(["import", "submit"], actions)
                record = self.read_json(run_manifest)["requests"][0]
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", record["status"])
                self.assertNotIn("safe_error_type", record)
                audit = (
                    run_manifest.read_text(encoding="utf-8")
                    + completed.stdout
                    + completed.stderr
                )
                self.assertNotIn(key, audit)

                repeated, repeated_actions, repeated_completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    expected_code=2,
                    key=key,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", repeated["reason"])
                self.assertEqual([], repeated_actions)
                repeated_audit = (
                    run_manifest.read_text(encoding="utf-8")
                    + repeated_completed.stdout
                    + repeated_completed.stderr
                )
                self.assertNotIn(key, repeated_audit)

        for name, key in (
            ("invalid-response-exact", "InvalidSubmissionResponse"),
            (
                "invalid-response-key-contains",
                "prefix-InvalidSubmissionResponse-suffix",
            ),
            ("invalid-response-candidate-contains", "SubmissionResponse"),
        ):
            with self.subTest(relation=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"unsafe-invalid-response-audit-{name}"
                )
                payload, actions, completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    events=(
                        {"operation": "submit", "value": {"request_id": " "}},
                    ),
                    expected_code=2,
                    key=key,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", payload["reason"])
                self.assertEqual(["import", "submit"], actions)
                record = self.read_json(run_manifest)["requests"][0]
                self.assertNotIn("safe_error_type", record)
                audit = (
                    run_manifest.read_text(encoding="utf-8")
                    + completed.stdout
                    + completed.stderr
                )
                self.assertNotIn(key, audit)

                repeated, repeated_actions, repeated_completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    expected_code=2,
                    key=key,
                )
                self.assertEqual("SUBMISSION_STATUS_UNKNOWN", repeated["reason"])
                self.assertEqual([], repeated_actions)
                repeated_audit = (
                    run_manifest.read_text(encoding="utf-8")
                    + repeated_completed.stdout
                    + repeated_completed.stderr
                )
                self.assertNotIn(key, repeated_audit)

    def test_result_error_omits_every_candidate_rejected_by_secret_policy(self) -> None:
        cases = (
            (
                "exact",
                "RESULT_VALIDATION_ERROR",
                "RESULT_VALIDATION_ERROR",
                "RESULT_VALIDATION_ERROR",
            ),
            (
                "key-contains-candidates",
                "image_too_small-FalError-RESULT_VALIDATION_ERROR",
                "image_too_small",
                "FalError",
            ),
            (
                "candidates-contain-key",
                "VALIDATION",
                "RESULT_VALIDATION_ERROR",
                "RESULT_VALIDATION_ERROR",
            ),
        )
        for index, (name, key, detail_type, error_name) in enumerate(cases):
            with self.subTest(relation=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"unsafe-result-audit-{name}"
                )
                self.run_dir, self.run_manifest, self.request_plan = (
                    run_dir,
                    run_manifest,
                    request_plan,
                )
                request_id = f"safe-result-{index}"
                self.seed_request("COMPLETED", request_id)
                payload, actions, completed = self.execute(
                    events=(
                        {
                            "operation": "result",
                            "throw": True,
                            "error_name": error_name,
                            "status": 422,
                            "body": {"detail": [{"type": detail_type}]},
                        },
                    ),
                    expected_code=2,
                    key=key,
                )
                self.assertEqual("REQUEST_FAILED", payload["reason"])
                self.assertEqual(["import", f"result:{request_id}"], actions)
                record = self.read_json(run_manifest)["requests"][0]
                self.assertEqual("FAILED", record["status"])
                self.assertNotIn("safe_error_type", record)
                audit = (
                    run_manifest.read_text(encoding="utf-8")
                    + completed.stdout
                    + completed.stderr
                )
                self.assertNotIn(key, audit)

                repeated, repeated_actions, repeated_completed = self.execute(
                    expected_code=2,
                    key=key,
                )
                self.assertEqual("RUN_TERMINAL", repeated["reason"])
                self.assertEqual(request_id, repeated["request_id"])
                self.assertEqual("FAILED", repeated["status"])
                self.assertEqual([], repeated_actions)
                repeated_audit = (
                    run_manifest.read_text(encoding="utf-8")
                    + repeated_completed.stdout
                    + repeated_completed.stderr
                )
                self.assertNotIn(key, repeated_audit)

    def test_result_rejects_multiple_images_unsafe_scheme_html_and_invalid_png(self) -> None:
        corrupt = bytearray(self.png_bytes)
        corrupt[29] ^= 1
        cases = (
            (
                "zero",
                {"data": {"images": []}},
                "RESULT_SCHEMA_INVALID",
            ),
            (
                "multiple",
                {"data": {"images": [{"url": self.png_uri}, {"url": self.png_uri}]}},
                "RESULT_SCHEMA_INVALID",
            ),
            (
                "unsafe",
                {"data": {"images": [{"url": "file:///secret.png"}]}},
                "RESULT_URL_NOT_ALLOWED",
            ),
            (
                "html",
                {
                    "data": {
                        "images": [
                            {
                                "url": "data:text/html;base64,"
                                + base64.b64encode(b"<html>secret</html>").decode("ascii")
                            }
                        ]
                    }
                },
                "RESULT_URL_NOT_ALLOWED",
            ),
            (
                "invalid-png",
                {
                    "data": {
                        "images": [
                            {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(bytes(corrupt)).decode("ascii")
                            }
                        ]
                    }
                },
                "RESULT_NOT_PNG",
            ),
        )
        for name, result, reason in cases:
            with self.subTest(case=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"bad-result-{name}"
                )
                payload, actions, _ = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    events=(
                        {"operation": "submit", "value": {"request_id": name}},
                        {"operation": "status", "value": {"status": "COMPLETED"}},
                        {"operation": "result", "value": result},
                    ),
                    expected_code=2,
                )
                self.assertEqual(reason, payload["reason"])
                self.assertNotIn("submit", actions[actions.index("import") + 2 :])
                self.assertEqual([], list((run_dir / "raw").rglob("*.png")))
                rejected_run = self.read_json(run_manifest)
                record = rejected_run["requests"][0]
                self.assertIsNone(record["result_path"])
                self.assertNotIn("raw_sha256", record)
                self.assertEqual("REJECTED", record["status"])
                self.assertEqual(reason, record["safe_error_type"])
                self.assertEqual("TECHNICAL_REJECTED", rejected_run["state"])
                self.assertEqual(reason, rejected_run["terminal_reason"])
                self.assertEqual(
                    [{"frame_index": 1, "status": "REJECTED"}],
                    rejected_run["frames"],
                )
                second, second_actions, second_completed = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    expected_code=2,
                )
                self.assertEqual("RUN_TERMINAL", second["reason"])
                self.assertEqual(1, second["submitted_requests"])
                self.assertEqual("REJECTED", second["status"])
                self.assertEqual(name, second["request_id"])
                self.assertEqual([], second_actions)
                self.assertNotIn("data:image", second_completed.stdout)

    def test_transient_result_failure_resumes_the_same_safe_request_id(self) -> None:
        first, actions, _ = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "retry-result-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {"operation": "result", "throw": True, "error_name": "NetworkError"},
            ),
            expected_code=2,
        )
        self.assertEqual("RESULT_RETRIEVAL_FAILED", first["reason"])
        self.assertEqual(
            ["import", "submit", "status:retry-result-id", "result:retry-result-id"],
            actions,
        )
        persisted = self.read_json(self.run_manifest)
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual("COMPLETED", persisted["requests"][0]["status"])

        second, second_actions, _ = self.execute(
            events=(
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            )
        )
        self.assertEqual("EVALUATION_REQUIRED", second["reason"])
        self.assertEqual(["import", "result:retry-result-id"], second_actions)

    def test_transient_download_failure_resumes_the_same_safe_request_id(self) -> None:
        first, actions, _ = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "retry-download-id"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {
                        "data": {
                            "images": [
                                {"url": "https://provider.example/result.png"}
                            ]
                        }
                    },
                },
            ),
            expected_code=2,
            extra_environment={"FAKE_FETCH_MODE": "download-failure"},
        )
        self.assertEqual("RESULT_DOWNLOAD_FAILED", first["reason"])
        self.assertEqual(
            [
                "import",
                "submit",
                "status:retry-download-id",
                "result:retry-download-id",
            ],
            actions,
        )
        persisted = self.read_json(self.run_manifest)
        self.assertEqual("AUTHORIZED", persisted["state"])
        self.assertEqual("COMPLETED", persisted["requests"][0]["status"])

        second, second_actions, _ = self.execute(
            events=(
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            )
        )
        self.assertEqual("EVALUATION_REQUIRED", second["reason"])
        self.assertEqual(["import", "result:retry-download-id"], second_actions)

    def test_result_size_is_bounded_before_and_during_allocation(self) -> None:
        cases = (
            ("declared", {"FAKE_FETCH_MODE": "declared-too-large"}),
            ("stream", {"FAKE_FETCH_MODE": "stream-too-large"}),
            ("data-uri", {"FAKE_OVERSIZE_DATA_URI": "1"}),
        )
        for name, extra_environment in cases:
            with self.subTest(case=name):
                run_dir, run_manifest, request_plan = self.prepare_authorized(
                    f"oversize-{name}"
                )
                result_url = (
                    "data:image/png;base64,AAAA"
                    if name == "data-uri"
                    else "https://provider.example/result.png"
                )
                payload, actions, _ = self.execute(
                    run_manifest=run_manifest,
                    request_plan=request_plan,
                    events=(
                        {"operation": "submit", "value": {"request_id": f"size-{name}"}},
                        {"operation": "status", "value": {"status": "COMPLETED"}},
                        {
                            "operation": "result",
                            "value": {"data": {"images": [{"url": result_url}]}},
                        },
                    ),
                    expected_code=2,
                    extra_environment=extra_environment,
                )
                self.assertEqual("RESULT_TOO_LARGE", payload["reason"])
                self.assertEqual(
                    [
                        "import",
                        "submit",
                        f"status:size-{name}",
                        f"result:size-{name}",
                    ],
                    actions,
                )
                rejected = self.read_json(run_manifest)
                self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
                self.assertEqual("REJECTED", rejected["requests"][0]["status"])
                self.assertEqual([], list((run_dir / "raw").rglob("*.png")))

    def test_preexisting_raw_destination_is_never_overwritten(self) -> None:
        frame_dir = self.run_dir / "raw/frame-01"
        frame_dir.mkdir()
        destination = frame_dir / "attempt-01.png"
        destination.write_bytes(b"SENTINEL")
        payload, actions, _ = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "preexisting"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            ),
            expected_code=2,
        )
        self.assertEqual("RESULT_DESTINATION_EXISTS", payload["reason"])
        self.assertEqual(0, payload["submitted_requests"])
        self.assertEqual([], actions)
        self.assertEqual(b"SENTINEL", destination.read_bytes())

    def test_late_raw_destination_race_is_durably_rejected_without_overwrite(self) -> None:
        destination = self.run_dir / "raw/frame-01/attempt-01.png"
        payload, actions, _ = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "late-raw-race"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            ),
            expected_code=2,
            extra_environment={"FAKE_PLANT_RAW_PATH": str(destination)},
        )
        self.assertEqual("RESULT_DESTINATION_EXISTS", payload["reason"])
        self.assertEqual(
            ["import", "submit", "status:late-raw-race", "result:late-raw-race"],
            actions,
        )
        self.assertEqual(b"LATE-SENTINEL", destination.read_bytes())
        rejected = self.read_json(self.run_manifest)
        self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
        self.assertEqual("REJECTED", rejected["requests"][0]["status"])
        self.assertEqual(
            hashlib.sha256(self.png_bytes).hexdigest(),
            rejected["requests"][0]["raw_sha256"],
        )
        second, second_actions, _ = self.execute(expected_code=2)
        self.assertEqual("RUN_TERMINAL", second["reason"])
        self.assertEqual([], second_actions)

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_late_raw_confinement_rejection_remains_terminal_on_rerun(self) -> None:
        frame_dir = self.run_dir / "raw/frame-01"
        external = self.root / "late-external-raw"
        external.mkdir()
        self.addCleanup(lambda: os.rmdir(frame_dir) if frame_dir.is_junction() else None)
        payload, actions, _ = self.execute(
            events=(
                {
                    "operation": "submit",
                    "value": {"request_id": "late-confinement-id"},
                },
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            ),
            expected_code=2,
            key="WORKSPACE_CONFINEMENT_FAILED",
            extra_environment={
                "FAKE_PLANT_RAW_JUNCTION_PATH": str(frame_dir),
                "FAKE_PLANT_RAW_JUNCTION_TARGET": str(external),
            },
        )
        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", payload["reason"])
        self.assertEqual(
            [
                "import",
                "submit",
                "status:late-confinement-id",
                "result:late-confinement-id",
            ],
            actions,
        )
        rejected = self.read_json(self.run_manifest)
        self.assertEqual("TECHNICAL_REJECTED", rejected["state"])
        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", rejected["terminal_reason"])
        record = rejected["requests"][0]
        self.assertEqual("late-confinement-id", record["request_id"])
        self.assertEqual("REJECTED", record["status"])
        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", record["safe_error_type"])

        second, second_actions, _ = self.execute(
            expected_code=2,
            key="WORKSPACE_CONFINEMENT_FAILED",
        )
        self.assertEqual("RUN_TERMINAL", second["reason"])
        self.assertEqual(1, second["submitted_requests"])
        self.assertEqual("late-confinement-id", second["request_id"])
        self.assertEqual("REJECTED", second["status"])
        self.assertEqual([], second_actions)

    @unittest.skipUnless(os.name == "nt", "requires a real Windows junction")
    def test_linked_raw_frame_directory_cannot_escape(self) -> None:
        external = self.root / "external-raw"
        external.mkdir()
        frame_dir = self.run_dir / "raw/frame-01"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(frame_dir), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, created.returncode, created.stderr or created.stdout)
        self.addCleanup(lambda: os.rmdir(frame_dir) if frame_dir.is_junction() else None)

        payload, actions, _ = self.execute(
            events=(
                {"operation": "submit", "value": {"request_id": "linked"}},
                {"operation": "status", "value": {"status": "COMPLETED"}},
                {
                    "operation": "result",
                    "value": {"data": {"images": [{"url": self.png_uri}]}},
                },
            ),
            expected_code=2,
        )
        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", payload["reason"])
        self.assertEqual(0, payload["submitted_requests"])
        self.assertEqual([], actions)
        self.assertEqual([], list(external.iterdir()))


if __name__ == "__main__":
    unittest.main()
