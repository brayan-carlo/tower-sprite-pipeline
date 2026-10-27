"""Real-image and offline review/export regression tests; only provider is replaced."""
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
import sheet_images
import generic_sprite_workflow as workflow
from test_sheet_workflow import Provider, sheet


FIXTURE = Path(os.environ.get('TOWER_SPRITE_WOLF_SCALE_FIXTURE', str(Path(__file__).resolve().parent.parent / 'assets/qa/wolf-run-scale-drift.png')))


class ConsistencyTests(unittest.TestCase):
    def diagnostics(self, frames):
        self.assertTrue(callable(getattr(sheet_images, 'consistency_diagnostics', None)),
                        'Real frame 8 must produce consistency evidence before delivery')
        return sheet_images.consistency_diagnostics(frames)

    @unittest.skipUnless(FIXTURE.is_file(), 'Original wolf artwork is not distributed; set TOWER_SPRITE_WOLF_SCALE_FIXTURE to opt in')
    def test_real_frame_eight_is_flagged_without_claiming_anatomical_proof(self):
        with Image.open(FIXTURE) as im:
            frames = [im.crop((i*216, 0, (i+1)*216, 231)) for i in range(8)]
        result = self.diagnostics(frames)
        self.assertEqual(result['flagged_frames'], [8])
        self.assertEqual(result['frames'][7]['visible_area'], 14315)
        self.assertFalse(result['proves_anatomical_scale'])

    def test_valid_crouch_and_jump_do_not_trigger_a_height_only_rejection(self):
        frames = []
        # Equal body mass, radically different height/position: no zoom.
        for i in range(8):
            im = Image.new('RGBA', (128,128)); d = ImageDraw.Draw(im)
            if i == 7: d.rectangle((20,80,83,111), fill=(80,90,100,255))
            else: d.rectangle((40,10+i*3,71,73+i*3), fill=(80,90,100,255))
            frames.append(im)
        result = self.diagnostics(frames)
        self.assertEqual(result['flagged_frames'], [])
        self.assertTrue(result['visual_review_required'])

    def test_uniform_zoom_is_flagged(self):
        frames=[]
        for i in range(8):
            im=Image.new('RGBA',(128,128));d=ImageDraw.Draw(im)
            d.rectangle((20,20,79 if i==7 else 59,79 if i==7 else 59),fill=(90,50,30,255))
            frames.append(im)
        self.assertEqual(self.diagnostics(frames)['flagged_frames'],[8])


class VisualReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'source.png';sheet(self.source,count=2)
        self.options={'source':str(self.source),'action':'run','orientation':'left','strategy':'sheet',
                      'workspace_root':str(self.root/'runs'),'poll_seconds':0}

    def tearDown(self):self.tmp.cleanup()

    def generated(self):
        from test_sprite_alignment_workflow import aligned
        return aligned(workflow._generate_legacy(self.options,transport=Provider()),self.root)

    def review(self,r,report):
        self.assertTrue(callable(getattr(workflow,'record_visual_review',None)),
                        'A hash-bound visual rejection must be persisted')
        path=self.root/'review.json';path.write_text(json.dumps(report))
        return workflow.record_visual_review(r['run_manifest'],path)

    def report(self,r,failed=None):
        report={'artifact_set_sha256':r.get('artifact_set_sha256'),'decision':'reject' if failed else 'pass',
                'checks':{k:{'status':'fail' if k==failed else 'pass','notes':f'Inspected {k} across all poses and adjacent frames.'}
                          for k in ('identity','scale','orientation','framing','background','motion','loop')},
                'scale_anchors':[{'name':'head','notes':'Skull and muzzle remain comparable at the same view.'},
                                 {'name':'torso','notes':'Torso mass is consistent through compression and extension.'}],
                'flagged_frame_findings':[]}
        if r.get('run_manifest') and Path(r['run_manifest']).is_file():
            m=json.loads(Path(r['run_manifest']).read_text())
            if m.get('derivation'):
                from test_sprite_alignment_workflow import body_review
                report['body_alignment']=body_review(r)
        return report

    def test_technical_success_cannot_be_delivered_or_exported(self):
        r=self.generated()
        self.assertIs(r.get('delivery_allowed'),False)
        self.assertEqual(r.get('delivery_artifact_paths'),{})
        self.assertEqual(r['next_action'],'inspect_artifacts_and_record_visual_review')
        self.assertTrue(callable(getattr(workflow,'export_artifacts',None)))
        out=self.root/'export';result=workflow.export_artifacts(r['run_manifest'],out)
        self.assertEqual(result['block_reason'],'VISUAL_REVIEW_REQUIRED');self.assertFalse(out.exists())

    def test_confirmed_scale_failure_blocks_resume_and_export_offline(self):
        r=self.generated();checked=self.review(r,self.report(r,'scale'))
        self.assertEqual(checked['state'],'VISUAL_REJECTED');self.assertFalse(checked['delivery_allowed'])
        with patch.object(workflow,'_transport',side_effect=AssertionError('review must be offline')):
            self.assertEqual(workflow.resume(r['run_manifest'])['state'],'VISUAL_REJECTED')
            self.assertEqual(workflow.export_artifacts(r['run_manifest'],self.root/'out')['block_reason'],'VISUAL_REJECTED')
        self.assertFalse((self.root/'out').exists())

    def test_confirmed_rejection_cannot_be_overridden_without_correcting_artifacts(self):
        r=self.generated();self.review(r,self.report(r,'scale'))
        result=self.review(r,self.report(r))
        self.assertEqual(result['block_reason'],'REJECTED_ARTIFACTS_REQUIRE_REVISION')
        self.assertEqual(workflow.inspect(r['run_manifest'])['state'],'VISUAL_REJECTED')

    def test_interrupted_rejection_is_recovered_from_receipt_and_cannot_be_overridden(self):
        r=self.generated()
        with patch.object(workflow,'atomic_json',side_effect=OSError('interrupted manifest write')):
            self.review(r,self.report(r,'scale'))
        before=Path(r['run_manifest']).read_bytes()
        inspected=workflow.inspect(r['run_manifest'])
        self.assertEqual(inspected['state'],'VISUAL_REJECTED')
        self.assertEqual(Path(r['run_manifest']).read_bytes(),before,'inspect stays read-only')
        result=self.review(r,self.report(r))
        self.assertEqual(result['block_reason'],'REJECTED_ARTIFACTS_REQUIRE_REVISION')
        self.assertFalse(workflow.export_artifacts(r['run_manifest'],self.root/'out')['delivery_allowed'])

    def test_interrupted_pass_does_not_implicitly_enable_export(self):
        r=self.generated()
        with patch.object(workflow,'atomic_json',side_effect=OSError('interrupted manifest write')):
            self.review(r,self.report(r))
        self.assertFalse(workflow.inspect(r['run_manifest'])['delivery_allowed'])
        self.assertEqual(workflow.export_artifacts(r['run_manifest'],self.root/'out')['block_reason'],'VISUAL_REVIEW_REQUIRED')

    def test_complete_review_allows_verified_export_without_human_acceptance_claim(self):
        r=self.generated();checked=self.review(r,self.report(r))
        self.assertEqual(checked['state'],'READY_FOR_DELIVERY');self.assertTrue(checked['delivery_allowed'])
        self.assertIs(checked['human_acceptance'],False)
        exported=workflow.export_artifacts(r['run_manifest'],self.root/'out')
        self.assertTrue(exported['delivery_allowed']);self.assertTrue((self.root/'out/sprite-sheet.png').is_file())
        with Image.open(self.root/'out/preview.gif') as im:self.assertEqual(im.n_frames,8)

    def test_no_blind_pass_without_scale_anchors_or_with_failing_check(self):
        for bad in ('anchors','failure','missing'):
            r=self.generated();report=self.report(r)
            if bad=='anchors':report['scale_anchors']=[]
            if bad=='failure':report['checks']['scale']['status']='fail'
            if bad=='missing':del report['checks']['scale']
            result=self.review(r,report)
            self.assertEqual(result['block_reason'],'VISUAL_REVIEW_INVALID');self.assertFalse(result['delivery_allowed'])

    def test_stale_review_cannot_approve_other_artifacts(self):
        a=self.generated();b=self.generated();report=self.report(a)
        result=self.review(b,report)
        self.assertEqual(result['block_reason'],'VISUAL_REVIEW_STALE');self.assertFalse(result['delivery_allowed'])

    def test_changed_png_or_receipt_invalidates_delivery(self):
        for target in ('image','receipt'):
            r=self.generated();checked=self.review(r,self.report(r));self.assertTrue(checked['delivery_allowed'])
            m=json.loads(Path(r['run_manifest']).read_text())
            if target=='image':Path(r['artifact_paths']['frames'][7]).write_bytes(b'changed')
            else:Path(m['visual_review']['path']).write_text('{}')
            result=workflow.inspect(r['run_manifest']);self.assertFalse(result['delivery_allowed'])
            self.assertIn(result['block_reason'],('ARTIFACT_TAMPERED','VISUAL_REVIEW_TAMPERED'))

    def test_revision_invalidates_previous_approval(self):
        # This regression is specifically the legacy provider approval lifecycle.
        # Seal a genuine policy-absent request before generation, preserving its
        # ability to PASS and then use a bounded provider revision.
        options=workflow._options
        def legacy(request):
            prepared,missing=options(request)
            prepared['request'].pop('alignment_policy');prepared['request']['grounded']=True
            return prepared,missing
        with patch.object(workflow,'_options',side_effect=legacy):
            r=workflow._generate_legacy(self.options,transport=Provider())
        self.assertTrue(self.review(r,self.report(r))['delivery_allowed'])
        revised=workflow.revise(r['run_manifest'],'Preserve torso size.',transport=Provider(),poll_seconds=0)
        self.assertEqual(revised['state'],'READY_FOR_VISUAL_REVIEW');self.assertFalse(revised['delivery_allowed'])
        self.assertEqual(revised['delivery_artifact_paths'],{})

    def test_pending_cannot_be_reviewed_and_review_does_not_spend(self):
        provider=Provider(pending=True);r=workflow._generate_legacy(self.options,transport=provider)
        result=self.review(r,self.report(r));self.assertEqual(result['block_reason'],'VISUAL_REVIEW_REQUIRES_ARTIFACTS')
        self.assertEqual(len(provider.calls),2)

    @unittest.skipUnless(FIXTURE.is_file(), 'Original wolf artwork is not distributed; set TOWER_SPRITE_WOLF_SCALE_FIXTURE to opt in')
    def test_real_outlier_requires_explicit_frame_eight_assessment_to_pass(self):
        class WolfProvider(Provider):
            def __call__(self, command, job_dir):
                if command=='execute':
                    # Repack real frames to the provider's 4x2 grid, preserving pixels.
                    with Image.open(FIXTURE) as source:
                        im=Image.new('RGBA',(864,462))
                        for i in range(8):im.paste(source.crop((i*216,0,(i+1)*216,231)),((i%4)*216,(i//4)*231))
                        im.save(job_dir/'raw.png')
                return super().__call__(command,job_dir)
        r=workflow._generate_legacy(self.options,transport=WolfProvider())
        self.assertEqual(r.get('consistency',{}).get('flagged_frames'),[8])
        result=self.review(r,self.report(r))
        self.assertEqual(result['block_reason'],'FLAGGED_FRAMES_UNREVIEWED')
        report=self.report(r,'scale');report['flagged_frame_findings']=[{'frame':8,'notes':'Head and torso proportions jump relative to frames 7 and 1.'}]
        self.assertEqual(self.review(r,report)['state'],'VISUAL_REJECTED')

    def test_cli_review_and_export_enforce_real_gate(self):
        r=self.generated();script=Path(workflow.__file__);out=self.root/'cli-out'
        blocked=subprocess.run([sys.executable,'-B',str(script),'export','--run-manifest',r['run_manifest'],'--output-dir',str(out)],capture_output=True,text=True)
        self.assertEqual(blocked.returncode,2);self.assertEqual(json.loads(blocked.stdout)['block_reason'],'VISUAL_REVIEW_REQUIRED')
        report=self.root/'cli-review.json';report.write_text(json.dumps(self.report(r)))
        reviewed=subprocess.run([sys.executable,'-B',str(script),'review','--run-manifest',r['run_manifest'],'--review-file',str(report)],capture_output=True,text=True)
        self.assertEqual(reviewed.returncode,0,reviewed.stderr);self.assertTrue(json.loads(reviewed.stdout)['delivery_allowed'])
        exported=subprocess.run([sys.executable,'-B',str(script),'export','--run-manifest',r['run_manifest'],'--output-dir',str(out)],capture_output=True,text=True)
        self.assertEqual(exported.returncode,0,exported.stderr);self.assertTrue((out/'visual-review.json').is_file())

    def test_export_conflict_preserves_existing_file_without_partial_copy(self):
        r=self.generated();self.review(r,self.report(r));out=self.root/'conflict';out.mkdir()
        (out/'sprite-sheet.png').write_bytes(b'user-file')
        result=workflow.export_artifacts(r['run_manifest'],out)
        self.assertEqual(result['block_reason'],'OUTPUT_CONFLICT')
        self.assertEqual(list(out.iterdir()),[out/'sprite-sheet.png'])
        self.assertEqual((out/'sprite-sheet.png').read_bytes(),b'user-file')


if __name__=='__main__':unittest.main()
