"""New-run geometry gates plus compatibility-independent end-to-end exports."""
import copy
import unittest
from pathlib import Path
from PIL import Image
import native_sprite_workflow as w
from sheet_workflow_state import WorkflowError,sha_file,read_json
import test_native_workflow as legacy_fixture
from test_native_task_workflow import task_options


class GeometryWorkflowTests(unittest.TestCase):
    setUp=legacy_fixture.NativeWorkflowTests.setUp
    tearDown=legacy_fixture.NativeWorkflowTests.tearDown
    report=legacy_fixture.NativeWorkflowTests.report

    def contract(self):
        return {'source_sha256':sha_file(self.source),'anchors':{
            'head':{'points':[[14,20],[24,20]],'notes':'Fixture visible head length'},
            'torso':{'points':[[30,15],[30,35]],'notes':'Fixture visible torso length'}},
            'body_anchor':[35,30],'pivot':[35,51],'notes':'Fixture projected root and ground',
            'component_decisions':[]}

    def ingested(self):
        r=w.prepare(self.source,self.root/'runs','left',source_geometry=self.contract(),
            **task_options(self.root,self.source,'run'))
        w.begin(r['run_manifest'])
        return w.ingest(r['run_manifest'],self.raw,'Observed synthetic native tool fixture')

    def geometry_report(self,r):
        report={'geometry_target_sha256':r['geometry_target_sha256'],'frames':[]}
        with Image.open(r['processing_paths']['registered']) as im:
            for i in range(8):
                cell=im.crop(((i%4)*512,(i//4)*512,(i%4+1)*512,(i//4+1)*512))
                x,y,x2,y2=cell.getchannel('A').getbbox()
                report['frames'].append({'frame':i+1,'anchors':{
                    'head':{'points':[[x+20,y+20],[x+40,y+20]],'notes':'Synthetic same head landmarks'},
                    'torso':{'points':[[x+80,y+30],[x+80,y+70]],'notes':'Synthetic same torso landmarks'}},
                    'body_anchor':[x+80,y+80],'support_point':[x+80,y2-1] if i%2==0 else None,
                    'component_decisions':[],'notes':'Fixture geometry, no artistic claim'})
        return report

    def test_public_prepare_requires_source_contract(self):
        with self.assertRaisesRegex(WorkflowError,'SOURCE_GEOMETRY_REQUIRED'):
            w.prepare(self.source,self.root/'runs','left')
        self.assertFalse((self.root/'runs').exists())

    def test_geometry_gate_stale_target_and_full_export(self):
        r=self.ingested();p=r['run_manifest']
        self.assertEqual(r['state'],w.GEOMETRY);self.assertFalse(r['delivery_allowed'])
        with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_REQUIRED'):w.export(p,self.root/'final')
        with self.assertRaisesRegex(WorkflowError,'VISUAL_REVIEW_REQUIRES_ARTIFACTS'):w.review(p,{})
        report=self.geometry_report(r);bad=copy.deepcopy(report);bad['geometry_target_sha256']='0'*64
        with self.assertRaisesRegex(WorkflowError,'GEOMETRY_REVIEW_STALE'):w.calibrate(p,bad)
        self.assertEqual(w.inspect(p)['state'],w.GEOMETRY)
        r=w.calibrate(p,report)
        self.assertEqual(r['state'],w.READY);self.assertFalse(r['delivery_allowed'])
        r=w.review(p,self.report(r))
        self.assertEqual(r['state'],w.ALIGNMENT)
        with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_REQUIRED'):w.export(p,self.root/'final')
        self.assertEqual(r['geometry']['scale'],.5)
        self.assertTrue(Path(r['artifact_paths']['source_reference']).is_file())
        with self.assertRaisesRegex(WorkflowError,'GEOMETRY_REVIEW_NOT_ALLOWED'):w.calibrate(p,report)
        Path(r['artifact_paths']['geometry']).write_text('{}')
        with self.assertRaisesRegex(WorkflowError,'HASH_MISMATCH'):w.inspect(p)

    def test_failed_measurement_preserves_review_for_annotation_correction(self):
        r=self.ingested();report=self.geometry_report(r)
        report['frames'][0]['anchors']['head']['points'][1][0]+=60
        with self.assertRaisesRegex(WorkflowError,'ANATOMICAL_SCALE_MISMATCH'):w.calibrate(r['run_manifest'],report)
        self.assertEqual(w.inspect(r['run_manifest'])['state'],w.GEOMETRY)
        r=w.revise(r['run_manifest'],'Observed anatomy wrong; generate corrected poses at same viewing angle.')
        self.assertEqual(r['attempt'],2)

    def test_corner_cleanup_requires_bound_review_and_preserves_generation_attempt(self):
        with Image.open(self.raw) as im:
            im.putpixel((0,886),(180,60,180));im.save(self.raw)
        r=self.ingested();p=r['run_manifest']
        self.assertEqual(r['block_reason'],'UNREVIEWED_ALPHA_LOSS')
        with self.assertRaisesRegex(WorkflowError,'INGEST_NOT_ALLOWED'):
            w.ingest(p,self.raw,'Retry without explicit decisions')
        r=w.diagnose_matte(p);diag=read_json(r['matte_diagnostic_paths']['components_report'])
        decisions=[{'id':c['id'],'decision':'remove_as_background' if c['pixels']==1 else 'preserve_as_art',
            'notes':'Isolated outer corner residue' if c['pixels']==1 else 'Separate valid pose must remain'}
            for c in diag['components'] if not c['primary']]
        bad=w.ingest(p,self.raw,'Same result with deliberately stale fixture review',
            {'image_pixel_sha256':'0'*64,'decisions':decisions})
        self.assertEqual(bad['state'],'TECHNICAL_REJECTED')
        r=w.ingest(p,self.raw,'Observed same result with explicit background component cleanup',
            {'image_pixel_sha256':diag['image_pixel_sha256'],'decisions':decisions})
        self.assertEqual(r['state'],w.GEOMETRY);self.assertEqual(r['attempt'],1)
        self.assertIn('matte_review',r['processing_paths'])


if __name__=='__main__':unittest.main()
