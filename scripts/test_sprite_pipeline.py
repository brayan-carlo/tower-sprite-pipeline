import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = SKILL_ROOT / "scripts" / "sprite_pipeline.py"
FAL_ADAPTER = SKILL_ROOT / "scripts" / "fal_generate.mjs"
PYTHON = Path(os.environ.get("TOWER_SPRITE_PYTHON", os.sys.executable))


def _resolve_node():
    configured = os.environ.get("TOWER_SPRITE_NODE")
    if configured:
        return configured
    on_path = shutil.which("node")
    if on_path:
        return on_path
    bundled = Path(os.sys.executable).resolve().parent.parent / "node" / "bin" / "node.exe"
    if bundled.is_file():
        return str(bundled)
    raise RuntimeError("Node.js not found; set TOWER_SPRITE_NODE to a real executable")


NODE = _resolve_node()


class SpritePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tower-sprite-pipeline-test-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.workspace = self.root / "workspace"
        self.source_relative = Path("Assets/fixture/Ogre_Walk_S_Base.png")
        self.source = self.project / self.source_relative
        self.source.parent.mkdir(parents=True)
        self.workspace.mkdir()
        self._write_source(self.source)
        self.rig_path = self.root / "rig_manifest.json"
        self.rig_path.write_text(json.dumps(self._rig(), indent=2), encoding="utf-8")
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(self._manifest(), indent=2), encoding="utf-8"
        )
        self.valid_guide = self.root / "valid_guide.json"
        self.valid_guide.write_text(
            json.dumps(
                {
                    "six_complete_bodies": True,
                    "distinct_phases": True,
                    "south_orientation": True,
                    "no_duplicated_limbs": True,
                    "single_row": True,
                    "pose_extraction_reliable": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write_source(path):
        image = Image.new("RGBA", (242, 399), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((70, 10, 172, 92), fill=(68, 92, 51, 255))
        draw.rectangle((64, 76, 178, 285), fill=(73, 97, 54, 255))
        draw.polygon([(64, 150), (20, 185), (43, 270), (86, 245)], fill=(66, 88, 48, 255))
        draw.polygon([(178, 150), (224, 188), (205, 270), (155, 245)], fill=(65, 87, 47, 255))
        draw.polygon([(70, 268), (116, 263), (111, 383), (50, 383)], fill=(58, 78, 43, 255))
        draw.polygon([(124, 263), (183, 258), (202, 383), (139, 383)], fill=(57, 77, 42, 255))
        draw.rectangle((92, 118, 150, 205), fill=(117, 78, 45, 255))
        image.save(path)

    def _rig(self):
        return {
            "sourceSize": [242, 399],
            "frameSize": [256, 416],
            "anchor": "bottom-center",
            "layers": {
                "leftArm": [[20, 150], [75, 135], [100, 210], [45, 275]],
                "rightArm": [[165, 140], [222, 170], [207, 275], [150, 230]],
                "leftLeg": [[48, 250], [120, 250], [118, 388], [42, 388]],
                "rightLeg": [[118, 245], [192, 245], [208, 388], [132, 388]],
            },
            "pivots": {
                "leftArm": [72, 176],
                "rightArm": [184, 184],
                "leftLeg": [87, 282],
                "rightLeg": [151, 278],
            },
            "limits": {
                "armRotationDegrees": 8,
                "legRotationDegrees": 8,
                "translationX": 12,
                "translationY": 6,
                "torsoBounceY": 6,
            },
            "layerOrder": ["leftLeg", "rightLeg", "core", "leftArm", "rightArm"],
        }

    def _manifest(self):
        return {
            "project_root": self.project.as_posix(),
            "source_path": self.source_relative.as_posix(),
            "entity": "ogre",
            "action": "walk",
            "direction": "S",
            "frame_count": 6,
            "frame_width": 256,
            "frame_height": 416,
            "anchor": "bottom-center",
            "max_budget_usd": 0.17,
            "primary_model": "fal-ai/kling-image/o1",
            "fallback_model": "fal-ai/nano-banana-2/edit",
        }

    def _write_manifest(self, data):
        path = self.root / f"manifest-{hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:8]}.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def _run_json(self, command, expected_code=0, env=None):
        process = subprocess.run(
            command, capture_output=True, text=True, check=False, env=env
        )
        self.assertEqual(
            expected_code,
            process.returncode,
            msg=f"command={command!r}\nstdout={process.stdout}\nstderr={process.stderr}",
        )
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            self.fail(
                f"command did not emit JSON: {error}\nstdout={process.stdout}\nstderr={process.stderr}"
            )
        return payload, process

    def _pipeline(self, *args, expected_code=0):
        return self._run_json([str(PYTHON), str(PIPELINE), *map(str, args)], expected_code)

    def _run_offline(self, run_id):
        payload, process = self._pipeline(
            "run-offline",
            "--manifest",
            self.manifest_path,
            "--rig-manifest",
            self.rig_path,
            "--guide-fixture",
            self.valid_guide,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
        )
        return payload, process, self.workspace / run_id

    def test_missing_required_manifest_field_blocks_before_workspace_creation(self):
        manifest = self._manifest()
        del manifest["anchor"]
        path = self._write_manifest(manifest)
        payload, _ = self._pipeline("validate", "--manifest", path, expected_code=2)
        self.assertEqual("BLOCKED", payload["state"])
        self.assertIn("anchor", payload["errors"][0])
        self.assertEqual([], list(self.workspace.iterdir()))

    def test_missing_source_blocks_before_workspace_creation(self):
        self.source.unlink()
        payload, _ = self._pipeline(
            "prepare",
            "--manifest",
            self.manifest_path,
            "--workspace-root",
            self.workspace,
            "--run-id",
            "20260817-120000-ogre-walk-s",
            expected_code=2,
        )
        self.assertEqual("BLOCKED", payload["state"])
        self.assertIn("source", " ".join(payload["errors"]).lower())
        self.assertEqual([], list(self.workspace.iterdir()))

    def test_incompatible_pilot_value_is_rejected(self):
        manifest = self._manifest()
        manifest["frame_count"] = 8
        path = self._write_manifest(manifest)
        payload, _ = self._pipeline("validate", "--manifest", path, expected_code=2)
        self.assertEqual("BLOCKED", payload["state"])
        self.assertIn("frame_count", " ".join(payload["errors"]))

    def test_workspace_inside_project_is_rejected(self):
        unsafe = self.project / "work"
        unsafe.mkdir()
        payload, _ = self._pipeline(
            "prepare",
            "--manifest",
            self.manifest_path,
            "--workspace-root",
            unsafe,
            "--run-id",
            "20260817-120001-ogre-walk-s",
            expected_code=2,
        )
        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual([], list(unsafe.iterdir()))

    def test_existing_run_directory_is_never_reused(self):
        run_id = "20260817-120002-ogre-walk-s"
        existing = self.workspace / run_id
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("user-data", encoding="utf-8")
        payload, _ = self._pipeline(
            "prepare",
            "--manifest",
            self.manifest_path,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
            expected_code=2,
        )
        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual("user-data", marker.read_text(encoding="utf-8"))

    def test_prepare_creates_only_the_five_authorized_directories(self):
        run_id = "20260817-120003-ogre-walk-s"
        payload, _ = self._pipeline(
            "prepare",
            "--manifest",
            self.manifest_path,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
        )
        self.assertEqual("DRAFT", payload["state"])
        run_dir = self.workspace / run_id
        self.assertEqual(
            ["input", "normalized", "previews", "raw", "reports"],
            sorted(path.name for path in run_dir.iterdir() if path.is_dir()),
        )
        run_manifest = json.loads((run_dir / "reports/run_manifest.json").read_text())
        self.assertEqual("DRAFT", run_manifest["state"])

    def test_valid_fixture_produces_required_offline_outputs(self):
        payload, _, run_dir = self._run_offline("20260817-120004-ogre-walk-s")
        self.assertEqual("READY_FOR_VISUAL_REVIEW", payload["state"])
        required = [
            "normalized/Ogre_Walk_S_6f_256x416.png",
            "previews/contact_sheet.png",
            "previews/loop.gif",
            "reports/run_manifest.json",
            "reports/qa_report.json",
        ]
        self.assertTrue(all((run_dir / relative).is_file() for relative in required))

    def test_sheet_has_six_exact_rgba_cells(self):
        _, _, run_dir = self._run_offline("20260817-120005-ogre-walk-s")
        sheet = Image.open(run_dir / "normalized/Ogre_Walk_S_6f_256x416.png")
        self.assertEqual("RGBA", sheet.mode)
        self.assertEqual((1536, 416), sheet.size)
        cells = [sheet.crop((index * 256, 0, (index + 1) * 256, 416)) for index in range(6)]
        self.assertTrue(all(cell.size == (256, 416) and cell.getbbox() for cell in cells))
        self.assertTrue(all(cell.getchannel("A").getextrema() == (0, 255) for cell in cells))

    def test_frame_one_equals_hand_normalized_source(self):
        _, _, run_dir = self._run_offline("20260817-120006-ogre-walk-s")
        sheet = Image.open(run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").convert("RGBA")
        actual = sheet.crop((0, 0, 256, 416))
        source = Image.open(self.source).convert("RGBA")
        expected = Image.new("RGBA", (256, 416), (0, 0, 0, 0))
        bbox = source.getbbox()
        expected.alpha_composite(source, ((256 - source.width) // 2, 416 - 1 - bbox[3]))
        self.assertEqual(expected.tobytes(), actual.tobytes())

    def test_frames_are_nonempty_distinct_and_pose_diffs_nonzero(self):
        _, _, run_dir = self._run_offline("20260817-120007-ogre-walk-s")
        sheet = Image.open(run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").convert("RGBA")
        frames = [sheet.crop((index * 256, 0, (index + 1) * 256, 416)) for index in range(6)]
        hashes = [hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames]
        self.assertEqual(6, len(set(hashes)))
        self.assertTrue(all(a.tobytes() != b.tobytes() for a, b in zip(frames, frames[1:])))

    def test_bottom_anchor_and_ground_line_are_stable(self):
        _, _, run_dir = self._run_offline("20260817-120008-ogre-walk-s")
        sheet = Image.open(run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").convert("RGBA")
        bboxes = [sheet.crop((i * 256, 0, (i + 1) * 256, 416)).getbbox() for i in range(6)]
        self.assertEqual([415] * 6, [bbox[3] for bbox in bboxes])
        centers = [(bbox[0] + bbox[2]) / 2 for bbox in bboxes]
        self.assertTrue(all(abs(center - centers[0]) <= 8 for center in centers))

    def test_required_layer_coverage_rejects_foot_pixels_left_out_of_leg_mask(self):
        source = Image.open(self.source).convert("RGBA")
        ImageDraw.Draw(source).rectangle((199, 355, 224, 382), fill=(57, 77, 42, 255))
        source.save(self.source)
        rig = self._rig()
        rig["requiredLayerCoverage"] = [
            {
                "layer": "rightLeg",
                "region": [[208, 350], [225, 350], [225, 389], [208, 389]],
                "maxUncoveredOpaquePixels": 0,
            }
        ]
        self.rig_path.write_text(json.dumps(rig, indent=2), encoding="utf-8")

        run_id = "20260817-120014-ogre-walk-s"
        payload, _ = self._pipeline(
            "run-offline",
            "--manifest",
            self.manifest_path,
            "--rig-manifest",
            self.rig_path,
            "--guide-fixture",
            self.valid_guide,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
            expected_code=3,
        )

        self.assertEqual("TECHNICAL_REJECTED", payload["state"])
        qa = json.loads(
            (self.workspace / run_id / "reports/qa_report.json").read_text()
        )
        coverage = qa["checks"]["required_layer_coverage"]
        self.assertFalse(coverage["passed"])
        self.assertGreater(coverage["results"][0]["uncoveredOpaquePixels"], 0)

    def test_minimum_limb_excursion_rejects_an_imperceptible_walk(self):
        rig = self._rig()
        rig["minimumLimbExcursionPixels"] = {
            "leftArm": 100.0,
            "rightArm": 100.0,
            "leftLeg": 100.0,
            "rightLeg": 100.0,
        }
        self.rig_path.write_text(json.dumps(rig, indent=2), encoding="utf-8")

        run_id = "20260817-120015-ogre-walk-s"
        payload, _ = self._pipeline(
            "run-offline",
            "--manifest",
            self.manifest_path,
            "--rig-manifest",
            self.rig_path,
            "--guide-fixture",
            self.valid_guide,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
            expected_code=3,
        )

        self.assertEqual("TECHNICAL_REJECTED", payload["state"])
        qa = json.loads(
            (self.workspace / run_id / "reports/qa_report.json").read_text()
        )
        excursion = qa["checks"]["minimum_limb_excursion"]
        self.assertFalse(excursion["passed"])
        self.assertTrue(all(not result["passed"] for result in excursion["results"]))

    def test_walk_meets_perceptible_limb_excursion_requirements(self):
        rig = self._rig()
        rig["minimumLimbExcursionPixels"] = {
            "leftArm": 14.0,
            "rightArm": 14.0,
            "leftLeg": 17.0,
            "rightLeg": 17.0,
        }
        self.rig_path.write_text(json.dumps(rig, indent=2), encoding="utf-8")

        _, _, run_dir = self._run_offline("20260817-120016-ogre-walk-s")
        qa = json.loads((run_dir / "reports/qa_report.json").read_text())
        excursion = qa["checks"]["minimum_limb_excursion"]
        self.assertTrue(excursion["passed"])
        self.assertTrue(all(result["passed"] for result in excursion["results"]))

    def test_motion_requirement_without_a_defined_pose_is_technical_rejected(self):
        rig = self._rig()
        rig["layers"]["cape"] = [[100, 100], [130, 100], [130, 180], [100, 180]]
        rig["pivots"]["cape"] = [115, 110]
        rig["minimumLimbExcursionPixels"] = {"cape": 1.0}
        self.rig_path.write_text(json.dumps(rig, indent=2), encoding="utf-8")

        run_id = "20260817-120017-ogre-walk-s"
        payload, _ = self._pipeline(
            "run-offline",
            "--manifest",
            self.manifest_path,
            "--rig-manifest",
            self.rig_path,
            "--guide-fixture",
            self.valid_guide,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
            expected_code=3,
        )

        self.assertEqual("TECHNICAL_REJECTED", payload["state"])
        qa = json.loads(
            (self.workspace / run_id / "reports/qa_report.json").read_text()
        )
        result = qa["checks"]["minimum_limb_excursion"]["results"][0]
        self.assertEqual("cape", result["layer"])
        self.assertFalse(result["passed"])

    def test_locked_identity_region_matches_after_bounce_compensation(self):
        _, _, run_dir = self._run_offline("20260817-120009-ogre-walk-s")
        sheet = Image.open(run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").convert("RGBA")
        source = Image.open(self.source).convert("RGBA")
        source_locked = source.crop((105, 100, 140, 180)).tobytes()
        x_offset = (256 - source.width) // 2
        y_offset = 416 - 1 - source.getbbox()[3]
        bounces = [0, 3, 6, 0, 3, 6]
        for index, bounce in enumerate(bounces):
            frame = sheet.crop((index * 256, 0, (index + 1) * 256, 416))
            locked = frame.crop(
                (105 + x_offset, 100 + y_offset + bounce, 140 + x_offset, 180 + y_offset + bounce)
            )
            self.assertEqual(source_locked, locked.tobytes(), msg=f"frame {index + 1}")

    def test_offline_pipeline_stderr_is_clean(self):
        _, process, _ = self._run_offline("20260817-120013-ogre-walk-s")
        self.assertEqual("", process.stderr)

    def test_qa_pass_maps_only_to_ready_for_visual_review(self):
        _, _, run_dir = self._run_offline("20260817-120010-ogre-walk-s")
        qa = json.loads((run_dir / "reports/qa_report.json").read_text())
        self.assertEqual("PASS", qa["technical_result"])
        self.assertTrue(all(check["passed"] for check in qa["checks"].values()))
        manifest = json.loads((run_dir / "reports/run_manifest.json").read_text())
        self.assertEqual("READY_FOR_VISUAL_REVIEW", manifest["state"])
        self.assertNotIn(manifest["state"], ["READY_FOR_UNITY_UAT", "APPROVED"])
        self.assertIn("python", manifest["runtime_versions"])
        self.assertIn("pillow", manifest["runtime_versions"])

    def test_invalid_guide_is_guide_rejected_without_candidate(self):
        invalid = self.root / "invalid_guide.json"
        invalid.write_text(
            json.dumps(
                {
                    "six_complete_bodies": False,
                    "distinct_phases": False,
                    "south_orientation": False,
                    "no_duplicated_limbs": False,
                    "single_row": True,
                    "pose_extraction_reliable": False,
                }
            ),
            encoding="utf-8",
        )
        run_id = "20260817-120011-ogre-walk-s"
        payload, _ = self._pipeline(
            "run-offline",
            "--manifest",
            self.manifest_path,
            "--rig-manifest",
            self.rig_path,
            "--guide-fixture",
            invalid,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
            expected_code=3,
        )
        run_dir = self.workspace / run_id
        self.assertEqual("GUIDE_REJECTED", payload["state"])
        self.assertFalse((run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").exists())
        self.assertFalse((run_dir / "previews/contact_sheet.png").exists())
        self.assertTrue((run_dir / "reports/run_manifest.json").is_file())

    def test_resume_reads_run_manifest_without_automatic_generation(self):
        _, _, run_dir = self._run_offline("20260817-120012-ogre-walk-s")
        payload, _ = self._pipeline(
            "resume", "--run-manifest", run_dir / "reports/run_manifest.json"
        )
        self.assertEqual("READY_FOR_VISUAL_REVIEW", payload["state"])
        self.assertEqual([], payload["automatic_actions"])

    def _fal(self, manifest, fixture, output, mode="fixture", expected_code=0, env=None):
        command = [
            NODE,
            str(FAL_ADAPTER),
            "--mode",
            mode,
            "--manifest",
            str(manifest),
            "--fixture",
            str(fixture),
            "--output",
            str(output),
        ]
        process = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
        self.assertEqual(
            expected_code,
            process.returncode,
            msg=f"stdout={process.stdout}\nstderr={process.stderr}",
        )
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"Fal adapter did not emit JSON: {error}\n{process.stdout}\n{process.stderr}")
        return payload, process

    def _write_fal_fixture(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def _prepare_run(self, run_id="20260817-130000-ogre-walk-s"):
        payload, _ = self._pipeline(
            "prepare",
            "--manifest",
            self.manifest_path,
            "--workspace-root",
            self.workspace,
            "--run-id",
            run_id,
        )
        self.assertEqual("DRAFT", payload["state"])
        run_dir = self.workspace / run_id
        return run_dir, run_dir / "reports/run_manifest.json"

    def _write_pricing(self, primary=0.028, fallback=0.08, verified_at=None):
        if verified_at is None:
            verified_at = datetime.now(timezone.utc).isoformat()
        return self._write_fal_fixture(
            f"pricing-{hashlib.sha256(str((primary, fallback, verified_at)).encode()).hexdigest()[:8]}.json",
            {
                "verified_at": verified_at,
                "source_url": "https://api.fal.ai/v1/models/pricing",
                "prices": [
                    {
                        "endpoint_id": "fal-ai/kling-image/o1",
                        "unit_price": primary,
                        "unit": "image",
                        "currency": "USD",
                    },
                    {
                        "endpoint_id": "fal-ai/nano-banana-2/edit",
                        "unit_price": fallback,
                        "unit": "image",
                        "currency": "USD",
                    },
                ],
            },
        )

    def _png_data_uri(self):
        image = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), buffer.getvalue()

    def _write_request_input(self, model="fal-ai/kling-image/o1"):
        data_uri, _ = self._png_data_uri()
        return self._write_fal_fixture(
            f"request-{model.replace('/', '-')}.json",
            {
                "model": model,
                "input": {
                    "prompt": "Create one row with six complete South-facing walk phases from @Image1.",
                    "image_urls": [data_uri],
                    "resolution": "1K",
                    "num_images": 1,
                    "aspect_ratio": "21:9",
                    "output_format": "png",
                },
            },
        )

    def _write_fake_fal_client(self, events=None):
        events = dict(events or {})
        data_uri, _ = self._png_data_uri()
        events.setdefault("submit", {"request_id": "req-1"})
        events.setdefault("status", {"status": "COMPLETED"})
        events.setdefault("result", {"data": {"images": [{"url": data_uri}]}, "requestId": "req-1"})
        config_path = self._write_fal_fixture("fake-fal-config.json", events)
        action_log = self.root / "fake-fal-actions.jsonl"
        module_path = self.root / "fake-fal-client.mjs"
        module_path.write_text(
            """import fs from 'node:fs';
const config = JSON.parse(fs.readFileSync(process.env.FAKE_FAL_CONFIG, 'utf8'));
const log = (payload) => fs.appendFileSync(process.env.FAKE_FAL_ACTION_LOG, JSON.stringify(payload) + '\\n', 'utf8');
export const fal = { queue: {
  submit: async (model, options) => {
    log({ action: 'submit', model, input_keys: Object.keys(options.input).sort() });
    if (config.submit_error) {
      const error = new Error(config.submit_error.message || 'submit timeout');
      error.submissionMayHaveOccurred = Boolean(config.submit_error.submissionMayHaveOccurred);
      throw error;
    }
    return config.submit;
  },
  status: async (model, options) => {
    const run = JSON.parse(fs.readFileSync(process.env.FAKE_FAL_RUN_MANIFEST, 'utf8'));
    const persisted = (run.fal_requests || []).find((item) => item.request_id === options.requestId) || null;
    log({ action: 'status', model, request_id: options.requestId, persisted_request: persisted });
    return config.status;
  },
  result: async (model, options) => {
    log({ action: 'result', model, request_id: options.requestId });
    if (config.result_error) {
      const error = new Error(config.result_error.message || 'result failed');
      error.status = config.result_error.status;
      error.body = config.result_error.body;
      throw error;
    }
    return config.result;
  }
}};
""",
            encoding="utf-8",
        )
        return module_path, config_path, action_log

    def _fal_actions(self, action_log):
        if not action_log.exists():
            return []
        return [json.loads(line) for line in action_log.read_text(encoding="utf-8").splitlines()]

    def _fal_real(
        self,
        run_manifest,
        pricing,
        request_input,
        client_module,
        config_path,
        action_log,
        expected_code=0,
        include_key=True,
    ):
        env = os.environ.copy()
        if include_key:
            env["FAL_KEY"] = "DO-NOT-LEAK-FAKE-FAL-KEY"
        else:
            env.pop("FAL_KEY", None)
        env["FAKE_FAL_CONFIG"] = str(config_path)
        env["FAKE_FAL_ACTION_LOG"] = str(action_log)
        env["FAKE_FAL_RUN_MANIFEST"] = str(run_manifest)
        command = [
            NODE,
            str(FAL_ADAPTER),
            "--mode",
            "real",
            "--manifest",
            str(self.manifest_path),
            "--run-manifest",
            str(run_manifest),
            "--pricing",
            str(pricing),
            "--request-input",
            str(request_input),
            "--client-module",
            str(client_module),
        ]
        return self._run_json(command, expected_code=expected_code, env=env)

    def test_fal_offline_fixture_preserves_request_ids_and_attempt_count(self):
        fixture = self._write_fal_fixture(
            "fal-accepted.json",
            {
                "pricing": {"primary": 0.028, "fallback": 0.08},
                "requests": [
                    {"request_id": "fake-request-1", "model": "fal-ai/kling-image/o1", "status": "rejected", "estimated_cost_usd": 0.028},
                    {"request_id": "fake-request-2", "model": "fal-ai/kling-image/o1", "status": "accepted", "estimated_cost_usd": 0.028},
                ],
            },
        )
        output = self.root / "fal-output.json"
        payload, _ = self._fal(self.manifest_path, fixture, output)
        self.assertEqual("GUIDE_ACCEPTED", payload["state"])
        self.assertEqual(2, payload["attempts"])
        self.assertEqual(["fake-request-1", "fake-request-2"], payload["request_ids"])
        self.assertAlmostEqual(0.056, payload["estimated_cost_usd"])
        self.assertEqual(payload, json.loads(output.read_text()))

    def test_fal_worst_case_over_budget_is_blocked_without_attempt(self):
        fixture = self._write_fal_fixture(
            "fal-over-budget.json",
            {"pricing": {"primary": 0.04, "fallback": 0.08}, "requests": []},
        )
        payload, _ = self._fal(self.manifest_path, fixture, self.root / "blocked.json", expected_code=2)
        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual(0, payload["attempts"])
        self.assertEqual([], payload["request_ids"])

    def test_fal_existing_request_is_queried_before_any_resubmit(self):
        fixture = self._write_fal_fixture(
            "fal-existing.json",
            {
                "pricing": {"primary": 0.028, "fallback": 0.08},
                "requests": [
                    {"request_id": "fake-in-progress", "model": "fal-ai/kling-image/o1", "status": "in_progress", "estimated_cost_usd": 0.028},
                    {"request_id": "must-not-submit", "model": "fal-ai/kling-image/o1", "status": "accepted", "estimated_cost_usd": 0.028},
                ],
            },
        )
        payload, _ = self._fal(self.manifest_path, fixture, self.root / "existing.json")
        self.assertEqual("DRAFT", payload["state"])
        self.assertEqual(["query:fake-in-progress"], payload["actions"])
        self.assertEqual(["fake-in-progress"], payload["request_ids"])

    def test_fal_real_transport_blocks_without_a_prepared_run(self):
        fixture = self._write_fal_fixture(
            "fal-real.json", {"pricing": {"primary": 0.028, "fallback": 0.08}, "requests": []}
        )
        payload, _ = self._fal(
            self.manifest_path,
            fixture,
            self.root / "real.json",
            mode="real",
            expected_code=2,
        )
        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", payload["reason"])

    def test_fal_output_never_contains_fixture_secret(self):
        sentinel = "DO-NOT-LEAK-FAKE-FAL-KEY"
        fixture = self._write_fal_fixture(
            "fal-secret.json",
            {
                "pricing": {"primary": 0.028, "fallback": 0.08},
                "secret": sentinel,
                "requests": [],
            },
        )
        env = os.environ.copy()
        env["FAL_KEY"] = sentinel
        output = self.root / "secret-output.json"
        _, process = self._fal(self.manifest_path, fixture, output, env=env)
        combined = process.stdout + process.stderr + output.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, combined)

    def test_real_mode_blocks_before_client_import_when_fal_key_is_absent(self):
        _, run_manifest = self._prepare_run("20260817-130001-ogre-walk-s")
        pricing = self._write_pricing()
        request_input = self._write_request_input()
        module, config, actions = self._write_fake_fal_client()
        payload, _ = self._fal_real(
            run_manifest,
            pricing,
            request_input,
            module,
            config,
            actions,
            expected_code=2,
            include_key=False,
        )
        self.assertEqual("BLOCKED", payload["state"])
        self.assertEqual("FAL_KEY_MISSING", payload["reason"])
        self.assertEqual([], self._fal_actions(actions))
        self.assertEqual([], json.loads(run_manifest.read_text())["fal_requests"])

    def test_real_mode_blocks_stale_or_incomplete_pricing_before_submit(self):
        request_input = self._write_request_input()
        for suffix, pricing in (
            (
                "missing",
                self._write_fal_fixture(
                    "pricing-missing.json",
                    {
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                        "source_url": "https://api.fal.ai/v1/models/pricing",
                        "prices": [
                            {
                                "endpoint_id": "fal-ai/kling-image/o1",
                                "unit_price": 0.028,
                                "unit": "image",
                                "currency": "USD",
                            }
                        ],
                    },
                ),
            ),
            (
                "stale",
                self._write_pricing(
                    verified_at=(datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
                ),
            ),
        ):
            with self.subTest(suffix=suffix):
                _, run_manifest = self._prepare_run(f"20260817-13001{0 if suffix == 'missing' else 1}-ogre-walk-s")
                module, config, actions = self._write_fake_fal_client()
                payload, _ = self._fal_real(
                    run_manifest,
                    pricing,
                    request_input,
                    module,
                    config,
                    actions,
                    expected_code=2,
                )
                self.assertEqual("PRICE_NOT_VERIFIABLE", payload["reason"])
                self.assertEqual([], self._fal_actions(actions))
                self.assertEqual([], json.loads(run_manifest.read_text())["fal_requests"])

    def test_real_mode_accepts_authenticated_platform_price_unit_images(self):
        _, run_manifest = self._prepare_run("20260818-000001-ogre-walk-s")
        pricing = self._write_fal_fixture(
            "pricing-platform-api-plural-unit.json",
            {
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "source_url": "https://api.fal.ai/v1/models/pricing",
                "prices": [
                    {
                        "endpoint_id": "fal-ai/kling-image/o1",
                        "unit_price": 0.028,
                        "unit": "images",
                        "currency": "USD",
                    },
                    {
                        "endpoint_id": "fal-ai/nano-banana-2/edit",
                        "unit_price": 0.08,
                        "unit": "images",
                        "currency": "USD",
                    },
                ],
            },
        )
        module, config, actions = self._write_fake_fal_client(
            {"status": {"status": "IN_PROGRESS"}}
        )

        payload, _ = self._fal_real(
            run_manifest,
            pricing,
            self._write_request_input(),
            module,
            config,
            actions,
        )

        self.assertEqual("DRAFT", payload["state"])
        self.assertAlmostEqual(0.028, payload["estimated_cost_usd"])
        self.assertEqual(["submit", "status"], [item["action"] for item in self._fal_actions(actions)])

    def test_real_mode_blocks_when_remaining_worst_case_exceeds_budget(self):
        _, run_manifest = self._prepare_run("20260817-130020-ogre-walk-s")
        module, config, actions = self._write_fake_fal_client()
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(primary=0.04, fallback=0.08),
            self._write_request_input(),
            module,
            config,
            actions,
            expected_code=2,
        )
        self.assertEqual("WORST_CASE_EXCEEDS_BUDGET", payload["reason"])
        self.assertAlmostEqual(0.20, payload["worst_case_usd"])
        self.assertEqual([], self._fal_actions(actions))

    def test_real_mode_rejects_manifest_outside_prepared_workspace(self):
        _, run_manifest = self._prepare_run("20260817-130021-ogre-walk-s")
        outside_manifest = self.root / "outside-run-manifest.json"
        outside_manifest.write_bytes(run_manifest.read_bytes())
        module, config, actions = self._write_fake_fal_client()
        payload, _ = self._fal_real(
            outside_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
            expected_code=2,
        )
        self.assertEqual("WORKSPACE_CONFINEMENT_FAILED", payload["reason"])
        self.assertEqual([], self._fal_actions(actions))

    def test_real_submit_persists_id_model_attempt_and_estimate_before_status_poll(self):
        run_dir, run_manifest = self._prepare_run("20260817-130030-ogre-walk-s")
        module, config, actions = self._write_fake_fal_client()
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )
        self.assertEqual("DRAFT", payload["state"])
        log = self._fal_actions(actions)
        self.assertEqual(["submit", "status", "result"], [item["action"] for item in log])
        persisted = log[1]["persisted_request"]
        self.assertEqual("req-1", persisted["request_id"])
        self.assertEqual("fal-ai/kling-image/o1", persisted["model"])
        self.assertEqual(1, persisted["attempt"])
        self.assertAlmostEqual(0.028, persisted["estimated_cost_usd"])
        self.assertTrue((run_dir / "raw/attempt-01-kling-image-o1.png").is_file())

    def test_real_mode_queries_existing_request_before_any_new_submit(self):
        _, run_manifest = self._prepare_run("20260817-130031-ogre-walk-s")
        run = json.loads(run_manifest.read_text())
        run["fal_requests"] = [
            {
                "attempt": 1,
                "model": "fal-ai/kling-image/o1",
                "estimated_cost_usd": 0.028,
                "request_id": "req-existing",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "IN_PROGRESS",
                "result_path": None,
            }
        ]
        run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
        module, config, actions = self._write_fake_fal_client(
            {"status": {"status": "IN_PROGRESS"}}
        )
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )
        self.assertEqual("DRAFT", payload["state"])
        log = self._fal_actions(actions)
        self.assertEqual(["status"], [item["action"] for item in log])
        self.assertEqual("req-existing", log[0]["request_id"])

    def test_pending_third_primary_is_queried_before_attempt_limit(self):
        _, run_manifest = self._prepare_run("20260817-130034-ogre-walk-s")
        run = json.loads(run_manifest.read_text())
        run["fal_requests"] = [
            {
                "attempt": index + 1,
                "model": "fal-ai/kling-image/o1",
                "estimated_cost_usd": 0.028,
                "request_id": f"req-{index + 1}",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "IN_PROGRESS" if index == 2 else "FAILED",
                "result_path": None,
            }
            for index in range(3)
        ]
        run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
        module, config, actions = self._write_fake_fal_client(
            {"status": {"status": "IN_PROGRESS"}}
        )
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )
        self.assertEqual("REQUEST_PENDING", payload["reason"])
        self.assertEqual(["status"], [item["action"] for item in self._fal_actions(actions)])
        self.assertEqual("req-3", self._fal_actions(actions)[0]["request_id"])

    def test_local_submit_timeout_counts_attempt_and_blocks_blind_resubmit(self):
        _, run_manifest = self._prepare_run("20260817-130032-ogre-walk-s")
        module, config, actions = self._write_fake_fal_client(
            {
                "submit_error": {
                    "message": "local timeout",
                    "submissionMayHaveOccurred": True,
                }
            }
        )
        call = lambda: self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
            expected_code=2,
        )
        first, _ = call()
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", first["reason"])
        run = json.loads(run_manifest.read_text())
        self.assertEqual(1, len(run["fal_requests"]))
        self.assertIsNone(run["fal_requests"][0]["request_id"])
        second, _ = call()
        self.assertEqual("SUBMISSION_STATUS_UNKNOWN", second["reason"])
        self.assertEqual(1, len([item for item in self._fal_actions(actions) if item["action"] == "submit"]))

    def test_resume_completed_request_fetches_result_without_new_submit(self):
        run_dir, run_manifest = self._prepare_run("20260817-130033-ogre-walk-s")
        run = json.loads(run_manifest.read_text())
        run["fal_requests"] = [
            {
                "attempt": 1,
                "model": "fal-ai/kling-image/o1",
                "estimated_cost_usd": 0.028,
                "request_id": "req-done",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "COMPLETED",
                "result_path": None,
            }
        ]
        run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
        data_uri, _ = self._png_data_uri()
        module, config, actions = self._write_fake_fal_client(
            {
                "status": {"status": "COMPLETED"},
                "result": {"data": {"images": [{"url": data_uri}]}, "requestId": "req-done"},
            }
        )
        self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )
        self.assertEqual(["status", "result"], [item["action"] for item in self._fal_actions(actions)])
        self.assertTrue((run_dir / "raw/attempt-01-kling-image-o1.png").is_file())

    def test_result_validation_error_is_audited_without_persisting_error_input(self):
        _, run_manifest = self._prepare_run("20260818-000002-ogre-walk-s")
        module, config, actions = self._write_fake_fal_client(
            {
                "status": {"status": "COMPLETED"},
                "result_error": {
                    "message": "Unprocessable Entity",
                    "status": 422,
                    "body": {
                        "detail": [
                            {
                                "loc": ["body"],
                                "msg": "Image dimensions are too small.",
                                "type": "image_too_small",
                                "input": "DO-NOT-PERSIST-INPUT",
                            }
                        ]
                    },
                },
            }
        )

        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )

        self.assertEqual("DRAFT", payload["state"])
        self.assertEqual("REQUEST_FAILED", payload["reason"])
        self.assertEqual("image_too_small", payload["failure_reason"])
        run_text = run_manifest.read_text(encoding="utf-8")
        run = json.loads(run_text)
        self.assertEqual("FAILED", run["fal_requests"][0]["status"])
        self.assertEqual("image_too_small", run["fal_requests"][0]["failure_reason"])
        self.assertNotIn("DO-NOT-PERSIST-INPUT", run_text)

    def test_real_mode_caps_primary_at_three_and_fallback_at_one(self):
        for suffix, model in (("primary", "fal-ai/kling-image/o1"), ("fallback", "fal-ai/nano-banana-2/edit")):
            with self.subTest(model=model):
                _, run_manifest = self._prepare_run(
                    f"20260817-13004{0 if suffix == 'primary' else 1}-ogre-walk-s"
                )
                run = json.loads(run_manifest.read_text())
                run["fal_requests"] = [
                    {
                        "attempt": index + 1,
                        "model": "fal-ai/kling-image/o1",
                        "estimated_cost_usd": 0.028,
                        "request_id": f"req-primary-{index + 1}",
                        "submitted_at": datetime.now(timezone.utc).isoformat(),
                        "status": "FAILED",
                        "result_path": None,
                    }
                    for index in range(3)
                ]
                if suffix == "fallback":
                    run["fal_requests"].append(
                        {
                            "attempt": 4,
                            "model": "fal-ai/nano-banana-2/edit",
                            "estimated_cost_usd": 0.08,
                            "request_id": "req-fallback-1",
                            "submitted_at": datetime.now(timezone.utc).isoformat(),
                            "status": "FAILED",
                            "result_path": None,
                        }
                    )
                run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
                module, config, actions = self._write_fake_fal_client()
                payload, _ = self._fal_real(
                    run_manifest,
                    self._write_pricing(),
                    self._write_request_input(model),
                    module,
                    config,
                    actions,
                    expected_code=2,
                )
                self.assertEqual("ATTEMPT_LIMIT_REACHED", payload["reason"])
                self.assertEqual([], self._fal_actions(actions))

    def test_fallback_requires_full_cost_to_fit_remaining_budget(self):
        _, run_manifest = self._prepare_run("20260817-130042-ogre-walk-s")
        run = json.loads(run_manifest.read_text())
        run["fal_requests"] = [
            {
                "attempt": index + 1,
                "model": "fal-ai/kling-image/o1",
                "estimated_cost_usd": cost,
                "request_id": f"req-{index + 1}",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "FAILED",
                "result_path": None,
            }
            for index, cost in enumerate((0.034, 0.033, 0.033))
        ]
        run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
        module, config, actions = self._write_fake_fal_client()
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input("fal-ai/nano-banana-2/edit"),
            module,
            config,
            actions,
            expected_code=2,
        )
        self.assertEqual("FALLBACK_EXCEEDS_REMAINING_BUDGET", payload["reason"])
        self.assertEqual([], self._fal_actions(actions))

    def test_accepted_guide_stops_all_further_submissions(self):
        _, run_manifest = self._prepare_run("20260817-130043-ogre-walk-s")
        run = json.loads(run_manifest.read_text())
        run["guide_accepted"] = True
        run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
        module, config, actions = self._write_fake_fal_client()
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )
        self.assertEqual("GUIDE_ACCEPTED", payload["state"])
        self.assertEqual("GUIDE_ALREADY_ACCEPTED", payload["reason"])
        self.assertEqual([], self._fal_actions(actions))

    def test_real_outputs_redact_fal_key(self):
        _, run_manifest = self._prepare_run("20260817-130044-ogre-walk-s")
        module, config, actions = self._write_fake_fal_client()
        _, process = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
        )
        combined = process.stdout + process.stderr + run_manifest.read_text(encoding="utf-8")
        for receipt in run_manifest.parent.glob("*.json"):
            combined += receipt.read_text(encoding="utf-8")
        self.assertNotIn("DO-NOT-LEAK-FAKE-FAL-KEY", combined)

    def test_invalid_result_schema_blocks_without_accepting_guide(self):
        run_dir, run_manifest = self._prepare_run("20260817-130045-ogre-walk-s")
        module, config, actions = self._write_fake_fal_client(
            {"result": {"data": {"images": []}, "requestId": "req-1"}}
        )
        payload, _ = self._fal_real(
            run_manifest,
            self._write_pricing(),
            self._write_request_input(),
            module,
            config,
            actions,
            expected_code=2,
        )
        self.assertEqual("RESULT_SCHEMA_INVALID", payload["reason"])
        run = json.loads(run_manifest.read_text())
        self.assertFalse(run.get("guide_accepted", False))
        self.assertEqual([], list((run_dir / "raw").glob("*.png")))

    def test_run_prepared_preserves_fal_audit_and_builds_outputs(self):
        run_dir, run_manifest = self._prepare_run("20260817-130046-ogre-walk-s")
        run = json.loads(run_manifest.read_text())
        run["fal_requests"] = [
            {
                "attempt": 1,
                "model": "fal-ai/kling-image/o1",
                "estimated_cost_usd": 0.028,
                "request_id": "req-1",
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "COMPLETED",
                "result_path": "raw/attempt-01-kling-image-o1.png",
            }
        ]
        run["estimated_cost_usd"] = 0.028
        run_manifest.write_text(json.dumps(run, indent=2), encoding="utf-8")
        assessment = self.root / "valid-assessment.json"
        assessment.write_text(self.valid_guide.read_text(encoding="utf-8"), encoding="utf-8")
        payload, _ = self._pipeline(
            "run-prepared",
            "--manifest",
            self.manifest_path,
            "--run-manifest",
            run_manifest,
            "--rig-manifest",
            self.rig_path,
            "--guide-assessment",
            assessment,
        )
        self.assertEqual("READY_FOR_VISUAL_REVIEW", payload["state"])
        final_run = json.loads(run_manifest.read_text())
        self.assertEqual("req-1", final_run["fal_requests"][0]["request_id"])
        self.assertAlmostEqual(0.028, final_run["estimated_cost_usd"])
        qa = json.loads((run_dir / "reports/qa_report.json").read_text())
        self.assertAlmostEqual(0.028, qa["checks"]["budget"]["estimated_cost_usd"])
        self.assertTrue((run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").is_file())
        self.assertTrue((run_dir / "previews/contact_sheet.png").is_file())
        self.assertTrue((run_dir / "previews/loop.gif").is_file())

    def test_run_prepared_rejects_invalid_guide_without_candidate(self):
        run_dir, run_manifest = self._prepare_run("20260817-130047-ogre-walk-s")
        assessment = self.root / "invalid-assessment.json"
        invalid = json.loads(self.valid_guide.read_text())
        invalid["six_complete_bodies"] = False
        assessment.write_text(json.dumps(invalid, indent=2), encoding="utf-8")
        payload, _ = self._pipeline(
            "run-prepared",
            "--manifest",
            self.manifest_path,
            "--run-manifest",
            run_manifest,
            "--rig-manifest",
            self.rig_path,
            "--guide-assessment",
            assessment,
            expected_code=3,
        )
        self.assertEqual("GUIDE_REJECTED", payload["state"])
        self.assertFalse((run_dir / "normalized/Ogre_Walk_S_6f_256x416.png").exists())
        self.assertFalse((run_dir / "previews/contact_sheet.png").exists())
        self.assertFalse((run_dir / "previews/loop.gif").exists())

    def test_skill_blocks_instead_of_writing_an_alternative_runtime(self):
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("intérprete Python 3 real", skill_text)
        self.assertIn("Pillow", skill_text)
        self.assertIn("load_workspace_dependencies", skill_text)
        self.assertIn("codex-primary-runtime", skill_text)
        self.assertIn("no crear un compositor ni una implementación alternativa", skill_text)


if __name__ == "__main__":
    unittest.main()
