"""Offline lifecycle tests. Fixture processing uses the actual native image code."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
import native_sprite_workflow as workflow
from sheet_workflow_state import WorkflowError, atomic_json


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root/'source.png'
        im = Image.new('RGBA', (64, 64))
        ImageDraw.Draw(im).rectangle((12, 12, 50, 50), fill=(45, 40, 35, 255))
        im.save(self.source)
        self.raw = self.root/'raw.png'
        im = Image.new('RGB', (1774, 887), (244, 6, 242))
        d = ImageDraw.Draw(im)
        for i in range(8):
            x = (i % 4)*443+120
            y = (i//4)*443+150
            flight = 35 if i % 4 in (1, 3) else 0
            d.rectangle((x, y, x+160+i, y+180-flight), fill=(30+i*2, 40+i, 35))
        im.save(self.raw)

    def tearDown(self):
        self.tmp.cleanup()

    def prepared(self):
        # Exercise recovery/export compatibility for manifests predating geometry.
        return workflow._prepare(self.source, self.root/'runs', 'left')

    def generated(self):
        r = self.prepared()
        workflow.begin(r['run_manifest'])
        return workflow.ingest(r['run_manifest'], self.raw, 'observed native tool output in fixture')

    def report(self, r, failed=None):
        return {'artifact_set_sha256':r['artifact_set_sha256'],
            'decision':'reject' if failed else 'pass',
            'checks':{k:{'status':'fail' if k==failed else 'pass',
                        'notes':'Fixture review gate test; not a real visual quality claim.'}
                      for k in ('identity','scale','orientation','framing','background','motion','loop')},
            'scale_anchors':[{'name':'head','notes':'Fixture anchor evidence for schema validation.'},
                             {'name':'torso','notes':'Second fixture anchor evidence for schema validation.'}],
            'flagged_frame_findings':[{'frame':f,'notes':'Fixture deliberately varies shape; examined for gate coverage.'}
                                      for f in r['consistency']['flagged_frames']]}

    def test_unknown_generation_cannot_be_claimed_or_revised_again(self):
        r = self.prepared(); p = r['run_manifest']
        self.assertEqual(workflow.begin(p)['state'], 'NATIVE_GENERATION_STARTED')
        with self.assertRaisesRegex(WorkflowError, 'GENERATION_ALREADY_CLAIMED'):
            workflow.begin(p)
        with self.assertRaisesRegex(WorkflowError, 'REVISION_NOT_ALLOWED'):
            workflow.revise(p, 'Need another result after a timeout')
        self.assertFalse(workflow.inspect(p)['delivery_allowed'])

    def test_technical_success_requires_visual_review_then_exports_offline(self):
        # Any attempt to use fal's transport from this route is a regression.
        with patch('generic_sprite_workflow._transport', side_effect=AssertionError('fal is forbidden')):
            r = self.generated(); p = r['run_manifest']
            self.assertFalse(r['delivery_allowed'])
            with self.assertRaisesRegex(WorkflowError, 'VISUAL_REVIEW_REQUIRED'):
                workflow.export(p, self.root/'final')
            self.assertFalse((self.root/'final').exists())
            r = workflow.review(p, self.report(r))
            self.assertTrue(r['delivery_allowed'])
            r = workflow.export(p, self.root/'final')
            with Image.open(r['delivery_artifact_paths']['sheet']) as exported:
                self.assertEqual(exported.size, (512, 64))
            self.assertTrue((self.root/'final/visual-review.json').is_file())
            workflow.export(p, self.root/'final')  # Idempotent identical export.

    def test_rejection_sticks_and_generation_attempts_are_bounded(self):
        r = self.generated(); p = r['run_manifest']
        workflow.review(p, self.report(r, 'motion'))
        with self.assertRaisesRegex(WorkflowError, 'REJECTED_ARTIFACTS_REQUIRE_REVISION'):
            workflow.review(p, self.report(r))
        with self.assertRaisesRegex(WorkflowError, 'VISUAL_REJECTED'):
            workflow.export(p, self.root/'final')
        r = workflow.revise(p, 'Correct the gathered phase and preserve flight height.')
        self.assertEqual(r['attempt'], 2)
        # Reusing an identical rejected generated image is not a correction.
        with self.assertRaisesRegex(WorkflowError, 'UNCHANGED_REJECTED_RESULT'):
            workflow.ingest(p, self.raw, 'same observed result')
        im = Image.open(self.raw); im.putpixel((200, 200), (40, 35, 30)); im.save(self.root/'revised.png')
        r = workflow.ingest(p, self.root/'revised.png', 'observed revised tool result')
        workflow.review(p, self.report(r, 'motion'))
        with self.assertRaisesRegex(WorkflowError, 'ATTEMPT_LIMIT'):
            workflow.revise(p, 'Try a third time despite the fixed limit.')

    def test_changed_artifact_invalidates_approval(self):
        r = self.generated(); p = r['run_manifest']
        workflow.review(p, self.report(r))
        Path(r['artifact_paths']['sheet']).write_bytes(b'changed')
        with self.assertRaisesRegex(WorkflowError, 'HASH_MISMATCH'):
            workflow.export(p, self.root/'final')

    def test_same_rejected_pixels_with_different_png_compression_are_not_correction(self):
        r = self.generated(); p = r['run_manifest']
        workflow.review(p, self.report(r, 'motion'))
        workflow.revise(p, 'Correct the motion in a genuinely new image result.')
        with Image.open(self.raw) as im:
            im.save(self.root/'reencoded.png', compress_level=9)
        self.assertNotEqual(self.raw.read_bytes(), (self.root/'reencoded.png').read_bytes())
        with self.assertRaisesRegex(WorkflowError, 'UNCHANGED_REJECTED_RESULT'):
            workflow.ingest(p, self.root/'reencoded.png', 'same pixels reencoded by fixture')

    def test_revision_recovers_after_manifest_write_interruption(self):
        r = self.generated(); p = r['run_manifest']
        workflow.review(p, self.report(r, 'motion'))
        feedback = 'Correct the gathered phase before trying another review.'
        with patch.object(workflow, 'atomic_json', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                workflow.revise(p, feedback)
        self.assertEqual(workflow.inspect(p)['state'], 'VISUAL_REJECTED')
        r = workflow.revise(p, feedback)
        self.assertEqual(r['attempt'], 2)
        self.assertEqual(workflow.begin(p)['state'], 'NATIVE_GENERATION_STARTED')

    def test_stale_review_and_inconsistent_state_are_rejected(self):
        r = self.generated(); p = r['run_manifest']; report = self.report(r)
        report['artifact_set_sha256'] = '0'*64
        with self.assertRaisesRegex(WorkflowError, 'VISUAL_REVIEW_STALE'):
            workflow.review(p, report)
        m = json.loads(Path(p).read_text()); m['state'] = 'READY_FOR_DELIVERY'; atomic_json(p, m)
        with self.assertRaisesRegex(WorkflowError, 'VISUAL_REVIEW_INVALID'):
            workflow.inspect(p)

    def test_source_copy_and_prompt_are_bound_to_run(self):
        r = self.prepared(); p = r['run_manifest']
        Path(r['prompt_file']).write_text('changed')
        with self.assertRaisesRegex(WorkflowError, 'HASH_MISMATCH'):
            workflow.begin(p)

    def test_invalid_parameters_and_output_inside_skill_are_rejected(self):
        for value in (0, 95, True, 1001):
            with self.assertRaises(WorkflowError):
                workflow._prepare(self.source, self.root/'runs', 'left', frame_ms=value)
        with self.assertRaisesRegex(WorkflowError, 'ORIENTATION_UNSUPPORTED'):
            workflow._prepare(self.source, self.root/'runs', 'front')
        with self.assertRaisesRegex(WorkflowError, 'WORKSPACE_NOT_ALLOWED'):
            workflow._prepare(self.source, Path(__file__).parent/'bad-output', 'left')

    def test_conflicting_export_does_not_overwrite_user_files(self):
        r = self.generated(); p = r['run_manifest']; workflow.review(p, self.report(r))
        out = self.root/'final'; out.mkdir(); (out/'sprite-sheet.png').write_bytes(b'user file')
        with self.assertRaisesRegex(WorkflowError, 'OUTPUT_CONFLICT'):
            workflow.export(p, out)
        self.assertEqual((out/'sprite-sheet.png').read_bytes(), b'user file')
        self.assertFalse((out/'frame-01.png').exists())


if __name__ == '__main__':
    unittest.main()
