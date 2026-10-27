"""Lifecycle guarantees with real local RGBA artifacts; fixtures are not art approval."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from native_components import analyze_components
from sheet_workflow_state import WorkflowError
try:
    import native_articulated_workflow as workflow
except ModuleNotFoundError:
    workflow = None

NOTE = 'Synthetic fixture observation for lifecycle testing, not an artistic approval.'

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write(path, data):
    Path(path).write_text(json.dumps(data), encoding='utf-8')

def fixture(root):
    root.mkdir()
    im = Image.new('RGBA', (64, 64))
    ImageDraw.Draw(im).rectangle((20, 10, 39, 50), fill=(200, 30, 20, 255))
    for name in ('source', 'native', 'body'):
        im.save(root / (name + '.png'))
    def ref(name):
        return {'path':name+'.png', 'sha256':digest(root/(name+'.png'))}
    data = {'schema':'native_articulated_layers_v1', 'canvas':[64,64],
            'frame_ms':100, 'action':'walk', 'orientation':'left', 'source':ref('source'),
            'native_inputs':[{**ref('native'), 'provenance':NOTE}], 'assets':{'body':ref('body')},
            'frames':[{'frame':i+1, 'layers':[{'id':'body', 'asset':'body', 'origin':[0,0],
                       'target':[i,0], 'angle':0, 'scale':1}]} for i in range(8)]}
    geometry = {'source_sha256':digest(root/'source.png'), 'anchors':[
        {'name':'head', 'points':[[25,14],[25,20]], 'notes':NOTE},
        {'name':'torso', 'points':[[25,25],[25,40]], 'notes':NOTE}],
        'body_anchor':[30,30], 'pivot':[32,50], 'uncertainty_pixels':1,
        'held_items':[], 'notes':NOTE}
    path = root/'recipe.json'; write(path, data)
    return path, geometry

def pass_report(result, geometry):
    frames = []
    components = []
    for i, path in enumerate(result['artifact_paths']['frames']):
        im = Image.open(path).convert('RGBA')
        anchors = copy.deepcopy(geometry['anchors'])
        for anchor in anchors:
            for point in anchor['points']:
                point[0] += i
        roles = ('contact','contact') if i in (0,4) else (('stance','swing') if i<4 else ('swing','stance'))
        supports = {name:{'role':role, 'point':None if role=='swing' else [x+i,y]}
                    for name,role,x,y in [('near',roles[0],25,45),('far',roles[1],34,44)]}
        frames.append({'frame':i+1,'anchors':anchors,'body_anchor':[30+i,30],
                       'supports':supports,'notes':NOTE})
        component = analyze_components(im)
        components.append({'frame':i+1,'image_pixel_sha256':component['image_pixel_sha256'],
                           'component_decisions':[]})
    return {'decision':'pass','artifact_set_sha256':result['artifact_set_sha256'],
            'checks':{name:{'status':'pass','notes':NOTE} for name in
                      ('identity','scale','orientation','framing','background','motion','loop')},
            'scale_anchors':[{'name':a['name'],'notes':NOTE} for a in geometry['anchors']],
            'flagged_frame_findings':[],
            'geometry':{'pivot':[32,50],'first_support_limb':'near','frames':frames},
            'components':components,
            'motion':{'frames':[{'frame':i+1,'phase':('contact','down','passing','up')[i%4],
                                  'notes':NOTE,'equipment':[]} for i in range(8)],
                      'transitions':[{'from':i+1,'to':(i+1)%8+1,'status':'pass','notes':NOTE,
                                      'equipment':[]} for i in range(8)],
                      'playback':[{'frame_ms':ms,'status':'pass','method':'agent_frame_and_playback_review',
                                   'notes':NOTE} for ms in (100,300)]}}

class ArticulatedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(workflow, 'Articulated workflow capability is not implemented yet')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe, self.geometry = fixture(self.root/'input')

    def build(self):
        return workflow._build(self.recipe, self.geometry, self.root/'runs')

    def approved(self):
        result = self.build()
        return workflow.review(result['run_manifest'], pass_report(result,self.geometry))

    def pending_export(self):
        result = self.approved(); dest = self.root/'delivery'
        with patch.object(workflow.native_exports,'write_export',side_effect=OSError('interrupted export')):
            with self.assertRaises(OSError): workflow.export(result['run_manifest'],dest)
        return result, dest

    def changed_recipe(self):
        path = self.recipe.parent/'body.png'
        with Image.open(path) as im:
            im.putpixel((30,32), (20,200,30,255)); im.save(path)
        data = read(self.recipe); data['assets']['body']['sha256'] = digest(path)
        write(self.recipe, data)
        return self.recipe

    def test_build_preserves_pixels_and_requires_review_before_export(self):
        result = self.build()
        self.assertEqual(result['state'],'READY_FOR_VISUAL_REVIEW')
        self.assertFalse(result['delivery_allowed'])
        checked = workflow.inspect(result['run_manifest'])
        self.assertEqual(checked['request']['action'],'walk')
        with Image.open(result['artifact_paths']['frames'][7]) as im:
            self.assertEqual(im.getpixel((27,10)),(200,30,20,255))
            self.assertEqual(im.getpixel((20,10))[3],0)
        with self.assertRaises(WorkflowError):
            workflow.export(result['run_manifest'],self.root/'delivery')
        self.assertFalse((self.root/'delivery').exists())

    def test_complete_review_exports_matching_frames_and_receipts(self):
        result = self.approved()
        self.assertTrue(result['delivery_allowed'])
        dest = self.root/'delivery'
        exported = workflow.export(result['run_manifest'],dest)
        self.assertTrue(exported['delivery_allowed'])
        self.assertTrue(read(dest/'delivery.json')['delivery_allowed'])
        self.assertEqual(read(dest/'visual-review.json')['decision'],'pass')
        with Image.open(dest/'sprite-sheet.png') as sheet:
            self.assertEqual(sheet.size,(512,64))
            for i, frame in enumerate(result['artifact_paths']['frames']):
                with Image.open(frame) as im:
                    self.assertEqual(sheet.crop((i*64,0,(i+1)*64,64)).tobytes(),im.tobytes())
                self.assertEqual((dest/Path(frame).name).read_bytes(),Path(frame).read_bytes())
        self.assertFalse((dest/'inputs').exists())

    def test_incomplete_pass_does_not_change_review_state(self):
        result = self.build(); report = pass_report(result,self.geometry); report.pop('motion')
        with self.assertRaises(WorkflowError):
            workflow.review(result['run_manifest'],report)
        current = workflow.inspect(result['run_manifest'])
        self.assertEqual(current['state'],'READY_FOR_VISUAL_REVIEW')
        self.assertFalse(current['delivery_allowed'])

    def test_modified_frame_withdraws_approval_and_revokes_owned_receipts(self):
        result = self.approved(); dest = self.root/'delivery'
        workflow.export(result['run_manifest'],dest)
        old_export = (dest/'frame-01.png').read_bytes()
        Path(result['artifact_paths']['frames'][0]).write_bytes(b'changed PNG')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])
        self.assertFalse(read(dest/'delivery.json')['delivery_allowed'])
        self.assertTrue(read(dest/'delivery.json')['revoked'])
        self.assertEqual((dest/'frame-01.png').read_bytes(),old_export)

    def test_source_tampering_prevents_delivery(self):
        result = self.approved()
        Path(result['source']).write_bytes(b'changed source')
        with self.assertRaises(WorkflowError):
            workflow.export(result['run_manifest'],self.root/'delivery')
        self.assertFalse((self.root/'delivery').exists())

    def test_source_tampering_after_export_withdraws_owned_receipts(self):
        result = self.approved(); dest = self.root/'delivery'; workflow.export(result['run_manifest'],dest)
        Path(result['source']).write_bytes(b'changed source')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])
        self.assertFalse(read(dest/'delivery.json')['delivery_allowed'])
        self.assertTrue(read(dest/'delivery.json')['revoked'])

    def test_rejected_pixels_require_actual_revision_and_new_review(self):
        result = self.build(); report = pass_report(result,self.geometry)
        report['decision']='reject'; report['checks']['motion']['status']='fail'
        rejected = workflow.review(result['run_manifest'],report)
        self.assertEqual(rejected['state'],'VISUAL_REJECTED')
        with self.assertRaises(WorkflowError):
            workflow.review(result['run_manifest'],pass_report(result,self.geometry))
        unchanged = workflow.revise(result['run_manifest'],self.recipe,NOTE)
        self.assertEqual(unchanged['state'],'TECHNICAL_REJECTED')
        self.assertEqual(unchanged['block_reason'],'UNCHANGED_REJECTED_RESULT')
        revised = workflow.revise(result['run_manifest'],self.changed_recipe(),NOTE)
        self.assertEqual(revised['attempt'],3)
        self.assertFalse(revised['delivery_allowed'])
        with self.assertRaises(WorkflowError):
            workflow.review(result['run_manifest'],pass_report(result,self.geometry))

    def test_scope_change_is_refused_without_replacing_current_attempt(self):
        result = self.approved(); data = read(self.recipe); data['orientation']='right'; write(self.recipe,data)
        with self.assertRaises(WorkflowError):
            workflow.revise(result['run_manifest'],self.recipe,NOTE)
        current = workflow.inspect(result['run_manifest'])
        self.assertEqual(current['attempt'],1)
        self.assertTrue(current['delivery_allowed'])

    def test_export_conflict_is_preflighted_without_partial_artifacts(self):
        result = self.approved(); dest = self.root/'delivery'; dest.mkdir()
        (dest/'frame-07.png').write_bytes(b'foreign file')
        with self.assertRaises(WorkflowError): workflow.export(result['run_manifest'],dest)
        self.assertEqual(sorted(p.name for p in dest.iterdir()),['frame-07.png'])
        self.assertEqual((dest/'frame-07.png').read_bytes(),b'foreign file')

    def test_partial_export_is_recovered_before_receipts_become_available(self):
        result = self.approved(); dest = self.root/'delivery'
        original = workflow.native_exports.new_or_identical
        def interrupt(path,payload):
            if Path(path).name=='frame-02.png': raise OSError('simulated write interruption')
            return original(path,payload)
        with patch.object(workflow.native_exports,'new_or_identical',side_effect=interrupt):
            with self.assertRaises(OSError): workflow.export(result['run_manifest'],dest)
        self.assertFalse((dest/'delivery.json').exists())
        self.assertFalse(workflow.inspect(result['run_manifest'])['delivery_allowed'])
        recovered = workflow.recover_exports(result['run_manifest'])
        self.assertTrue(recovered['delivery_allowed'])
        self.assertTrue(read(dest/'delivery.json')['delivery_allowed'])

    def test_recovery_rejects_noncanonical_revocation_receipt_keys_before_writing(self):
        cases = {
            'arbitrary': {'unexpected.json': {}},
            'traversal': {'../escaped.json': {}},
            'empty': {},
            'omitted': None,
            'absolute': {str((self.root/'absolute.json').resolve()): {}},
        }
        for label, revoked in cases.items():
            with self.subTest(label=label):
                result, dest = self.pending_export(); manifest = Path(result['run_manifest'])
                m = read(manifest); record = m['exports'][0]; record['status'] = 'REVOCATION_PENDING'
                if revoked is None:
                    record.pop('revoked_receipts',None)
                else:
                    record['revoked_receipts'] = revoked
                write(manifest,m)
                with self.assertRaises(WorkflowError): workflow.recover_exports(manifest)
                self.assertFalse((dest/'unexpected.json').exists())
                self.assertFalse((self.root/'escaped.json').exists())
                self.assertFalse((self.root/'absolute.json').exists())
                self.assertEqual(read(manifest)['exports'][0]['status'],'REVOCATION_PENDING')

    def test_recovery_rejects_forged_revocation_receipt_values(self):
        result, dest = self.pending_export(); manifest = Path(result['run_manifest'])
        m = read(manifest); workflow.native_exports.mark_revoked(m)
        m['exports'][0]['revoked_receipts']['delivery.json']['delivery_allowed'] = True
        write(manifest,m)
        with self.assertRaises(WorkflowError): workflow.recover_exports(manifest)
        self.assertFalse((dest/'delivery.json').exists())
        self.assertEqual(read(manifest)['exports'][0]['status'],'REVOCATION_PENDING')

    def test_recovery_rejects_forged_complete_delivery_receipt_after_rehash(self):
        result, dest = self.pending_export(); manifest = Path(result['run_manifest'])
        m = read(manifest); record = m['exports'][0]
        delivery = json.loads(record['receipts']['delivery.json'])
        delivery['generation_claimed'] = True; delivery['geometry']['scale'] = 9
        forged = json.dumps(delivery,ensure_ascii=False,indent=2)
        record['receipts']['delivery.json'] = forged
        record['hashes']['delivery.json'] = hashlib.sha256(forged.encode('utf-8')).hexdigest()
        write(manifest,m)
        with self.assertRaises(WorkflowError): workflow.recover_exports(manifest)
        self.assertFalse((dest/'delivery.json').exists())

    def test_completed_destination_tampering_blocks_delivery_without_overwrite_and_can_revoke(self):
        result = self.approved(); dest = self.root/'delivery'; workflow.export(result['run_manifest'],dest)
        frame = dest/'frame-01.png'; original = frame.read_bytes(); frame.write_bytes(b'foreign change')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])
        self.assertEqual(frame.read_bytes(),b'foreign change')
        current = read(result['run_manifest'])
        self.assertEqual(current['state'],'INTEGRITY_REJECTED')
        self.assertEqual(current['exports'][0]['status'],'REVOKED')
        self.assertFalse(read(dest/'delivery.json')['delivery_allowed'])
        frame.write_bytes(original)
        recovered = workflow.recover_exports(result['run_manifest'])
        self.assertFalse(recovered['delivery_allowed'])
        self.assertFalse(recovered['export_revocation_pending'])

    def test_completed_receipt_tampering_blocks_delivery_and_recovers_after_conflict_is_restored(self):
        result = self.approved(); dest = self.root/'delivery'; workflow.export(result['run_manifest'],dest)
        receipt = dest/'delivery.json'; original = receipt.read_bytes(); receipt.write_bytes(b'foreign receipt')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])
        self.assertEqual(receipt.read_bytes(),b'foreign receipt')
        current = read(result['run_manifest'])
        self.assertEqual(current['state'],'INTEGRITY_REJECTED')
        self.assertEqual(current['exports'][0]['status'],'REVOCATION_PENDING')
        receipt.write_bytes(original)
        recovered = workflow.recover_exports(result['run_manifest'])
        self.assertFalse(recovered['delivery_allowed'])
        self.assertFalse(recovered['export_revocation_pending'])
        self.assertTrue(read(receipt)['revoked'])

    def test_rejected_rereview_uses_exported_pass_history_to_revoke_receipts(self):
        result = self.approved(); dest = self.root/'delivery'; workflow.export(result['run_manifest'],dest)
        rejected = pass_report(result,self.geometry); rejected['decision']='reject'
        rejected['checks']['motion']['status']='fail'; rejected.pop('geometry')
        reviewed = workflow.review(result['run_manifest'],rejected)
        self.assertEqual(reviewed['state'],'VISUAL_REJECTED')
        inspected = workflow.inspect(result['run_manifest'])
        self.assertFalse(inspected['delivery_allowed'])
        self.assertEqual(inspected['export_destinations'][0]['status'],'REVOKED')
        self.assertTrue(read(dest/'delivery.json')['revoked'])

    def test_new_pass_notes_do_not_invalidate_receipt_from_prior_pass_in_same_attempt(self):
        result = self.approved(); dest = self.root/'delivery'; workflow.export(result['run_manifest'],dest)
        rereview = pass_report(result,self.geometry)
        rereview['checks']['identity']['notes']='A distinct valid synthetic rereview note.'
        workflow.review(result['run_manifest'],rereview)
        inspected = workflow.inspect(result['run_manifest'])
        self.assertTrue(inspected['delivery_allowed'])
        self.assertEqual(inspected['export_destinations'][0]['status'],'EXPORTED')
        self.assertEqual(read(dest/'visual-review.json')['checks']['identity']['notes'],NOTE)

    def test_revision_revokes_exports_and_preserves_conflicting_foreign_receipt(self):
        result = self.approved(); dest=self.root/'delivery'
        workflow.export(result['run_manifest'],dest)
        original_receipt=(dest/'delivery.json').read_bytes()
        (dest/'delivery.json').write_bytes(b'user modification')
        revised=workflow.revise(result['run_manifest'],self.changed_recipe(),NOTE)
        self.assertTrue(revised['export_revocation_pending'])
        self.assertEqual((dest/'delivery.json').read_bytes(),b'user modification')
        approved=workflow.review(result['run_manifest'],pass_report(revised,self.geometry))
        self.assertFalse(approved['delivery_allowed'])
        with self.assertRaises(WorkflowError): workflow.export(result['run_manifest'],self.root/'new-delivery')
        (dest/'delivery.json').write_bytes(original_receipt)
        recovered=workflow.recover_exports(result['run_manifest'])
        self.assertFalse(recovered['export_revocation_pending'])
        self.assertFalse(read(dest/'delivery.json')['delivery_allowed'])

    def test_interrupted_build_resumes_its_snapshot_not_changed_external_inputs(self):
        with patch.object(workflow,'_write_artifacts',side_effect=RuntimeError('interrupted build')):
            with self.assertRaises(RuntimeError): self.build()
        manifest = next((self.root/'runs').glob('*/run-manifest.json'))
        self.assertEqual(read(manifest)['state'],'ASSEMBLING')
        (self.recipe.parent/'body.png').write_bytes(b'external input changed')
        resumed=workflow.resume(manifest)
        self.assertEqual(resumed['attempt'],1)
        self.assertEqual(resumed['state'],'READY_FOR_VISUAL_REVIEW')
        with Image.open(resumed['artifact_paths']['frames'][0]) as im:
            self.assertEqual(im.getpixel((20,10)),(200,30,20,255))

    def test_state_or_artifact_list_forgery_cannot_grant_delivery(self):
        result = self.build(); m=read(result['run_manifest']); m['state']='READY_FOR_DELIVERY'
        write(result['run_manifest'],m)
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])
        self.assertFalse(read(result['run_manifest'])['state']=='READY_FOR_DELIVERY')

    def test_omitted_artifact_is_detected_before_export(self):
        result=self.approved(); m=read(result['run_manifest']); a=m['attempts'][-1]
        missing=a['artifact_paths']['frames'].pop(); a['artifact_hashes'].pop(missing)
        write(result['run_manifest'],m)
        with self.assertRaises(WorkflowError): workflow.export(result['run_manifest'],self.root/'delivery')
        self.assertFalse((self.root/'delivery').exists())

    def test_export_uses_verified_bytes_even_if_source_artifact_changes_after_read(self):
        result=self.approved(); frame=Path(result['artifact_paths']['frames'][0]); approved=frame.read_bytes()
        original=workflow.native_exports.register_export
        def mutate_after_validation(*args,**kwargs):
            frame.write_bytes(b'new unreviewed bytes')
            return original(*args,**kwargs)
        with patch.object(workflow.native_exports,'register_export',side_effect=mutate_after_validation):
            workflow.export(result['run_manifest'],self.root/'delivery')
        self.assertEqual((self.root/'delivery/frame-01.png').read_bytes(),approved)

    def replace_recorded_artifact_hash(self,result,key):
        m=read(result['run_manifest']); target=result['artifact_paths'][key]
        m['attempts'][-1]['artifact_hashes'][target]=digest(target)
        write(result['run_manifest'],m)

    def test_contact_sheet_cannot_misrepresent_frames_even_with_updated_file_hash(self):
        result=self.build()
        Image.new('RGBA',(256,128),(1,2,3,255)).save(result['artifact_paths']['contact'])
        self.replace_recorded_artifact_hash(result,'contact')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])

    def test_provenance_cannot_fabricate_a_generation_claim(self):
        result=self.build(); target=result['artifact_paths']['provenance']
        provenance=read(target); provenance['generation_claimed']=True; write(target,provenance)
        self.replace_recorded_artifact_hash(result,'provenance')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])

    def test_source_reference_must_preserve_dimensions_as_well_as_byte_sequence(self):
        result=self.build(); target=result['artifact_paths']['source_reference']
        with Image.open(target) as im: wrong=Image.frombytes('RGBA',(128,32),im.tobytes())
        wrong.save(target); self.replace_recorded_artifact_hash(result,'source_reference')
        with self.assertRaises(WorkflowError): workflow.inspect(result['run_manifest'])

if __name__=='__main__': unittest.main()
