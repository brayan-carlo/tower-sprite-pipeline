"""Offline derivation integration: real images, only provider transport is fake."""
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import generic_sprite_workflow as workflow
from sheet_workflow_state import sha_file
from test_sheet_workflow import Provider, sheet


def later_color_frames():
    """Detailed common tones plus a small red feature only in strike frame 4."""
    frames=[]
    for i in range(8):
        image=Image.new('RGBA',(96,96))
        for y in range(4,92):
            for x in range(4,92):
                image.putpixel((x,y),((x*13+y*7)%110+30,(x*7+y*13)%130+70,(x*5+y*3)%90+20,255))
        image.putpixel((5+i,5),(10,10,240,255))
        if i==3:
            for y in range(40,44):
                for x in range(40,44):image.putpixel((x,y),(230,10,35,255))
        frames.append(image)
    return frames


def annotate(result, path):
    m=json.loads(Path(result['run_manifest']).read_text())
    annotation={'schema_version':1,'artifact_set_sha256':result['artifact_set_sha256'],
        'reference':{'name':'torso midpoint','description':'Homologous center of torso, excluding equipment and tail.','source_point':[40,40]},
        'frames':[{'frame':i,'sha256':sha_file(p),'point':[40+i%3,40+i%2]}
                  for i,p in enumerate(m['artifact_paths']['frames'],1)]}
    path.write_text(json.dumps(annotation));return path


def aligned(result, root):
    from sprite_alignment_workflow import align_manifest
    return align_manifest(result['run_manifest'],annotate(result,root/'anchors.json'),output_root=root/'derived')


def body_review(result):
    m=json.loads(Path(result['run_manifest']).read_text())
    return {'proof_sha256':m['derivation']['proof_sha256'],'reference_name':'torso midpoint',
            'notes':'Compared the homologous torso reference, fixed X and preserved natural Y across every frame.',
            'frame_findings':[{'frame':i,'homologous_body_point':True,'fixed_x':True,'preserved_y':True,'excludes_weapon_tail_box':True,
                'notes':'Torso center follows the same anatomy; no weapon or tail bounding box used.'}
                for i in range(1,m['request']['frames']+1)]}


class AlignmentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'original.png';sheet(self.source,count=2)
        self.options={'source':str(self.source),'action':'run','orientation':'left','strategy':'sheet',
            'workspace_root':str(self.root/'runs'),'poll_seconds':0}
    def tearDown(self):self.tmp.cleanup()
    def generate(self):return workflow._generate_legacy(self.options,transport=Provider())
    def report(self,r):
        from test_sheet_review import VisualReviewTests
        report=VisualReviewTests.report(self,r)
        m=json.loads(Path(r['run_manifest']).read_text())
        if m.get('derivation'):report['body_alignment']=body_review(r)
        return report
    def review(self,r,report=None):
        path=self.root/'review.json';path.write_text(json.dumps(report or self.report(r)))
        return workflow.record_visual_review(r['run_manifest'],path)
    def test_new_scope_pass_requires_alignment_but_reject_remains_available(self):
        r=self.generate();m=json.loads(Path(r['run_manifest']).read_text())
        self.assertEqual(m['request'].get('alignment_policy'),'body_x_preserve_y_v1')
        self.assertFalse(m['request']['grounded'])
        self.assertEqual(self.review(r)['block_reason'],'ALIGNMENT_REQUIRED')
        self.assertIn('align',r['next_action'])
        self.assertEqual(workflow.export_artifacts(r['run_manifest'],self.root/'out')['block_reason'],'ALIGNMENT_REQUIRED')
    def test_offline_derivation_entrypoint_exists(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('sprite_alignment_workflow'))

    def test_derivative_full_review_export_and_replay_preserve_parent(self):
        r=self.generate();parent=Path(r['run_manifest']).parent
        before={str(p):p.read_bytes() for p in parent.rglob('*') if p.is_file()}
        with patch.object(workflow,'_transport',side_effect=AssertionError('offline')):
            d=aligned(r,self.root);self.assertEqual(d['state'],workflow.READY,d)
            m=json.loads(Path(d['run_manifest']).read_text());self.assertEqual(m['attempts'][0]['jobs'],{})
            self.assertEqual(m['authorization']['max_cost_usd'],0);self.assertEqual(d['spent_estimate_usd'],0)
            from sprite_alignment_workflow import verify_alignment
            proof=verify_alignment(m);self.assertEqual(proof['policy'],'body_x_preserve_y_v1')
            self.assertTrue(all(t['dy']==0 and t['scale']==1 for t in proof['transforms']))
            self.assertEqual([p[1] for p in proof['anchors_before']],[p[1] for p in proof['anchors_after']])
            checked=self.review(d);self.assertTrue(checked['delivery_allowed'],checked)
            out=self.root/'export';e=workflow.export_artifacts(d['run_manifest'],out)
            self.assertTrue(e['delivery_allowed'],e);self.assertTrue((out/'alignment/proof.json').is_file())
            self.assertTrue(workflow.export_artifacts(d['run_manifest'],out)['delivery_allowed'])
            (out/'alignment/proof.json').write_text('changed')
            self.assertEqual(workflow.export_artifacts(d['run_manifest'],out)['block_reason'],'OUTPUT_CONFLICT')
            with Image.open(out/'preview.gif') as im:
                self.assertEqual(im.n_frames,8);self.assertEqual(im.info['duration'],100);self.assertEqual(im.info['loop'],0)
        self.assertEqual(before,{str(p):p.read_bytes() for p in parent.rglob('*') if p.is_file()})

    def test_rejected_parent_remains_rejected_and_local_revision_never_spends(self):
        r=self.generate();report=self.report(r);report['decision']='reject';report['checks']['scale']['status']='fail'
        self.assertEqual(self.review(r,report)['state'],workflow.VISUAL_REJECTED)
        before=Path(r['run_manifest']).read_bytes();d=aligned(r,self.root)
        self.assertTrue(self.review(d)['delivery_allowed']);self.assertEqual(Path(r['run_manifest']).read_bytes(),before)
        self.assertEqual(workflow.inspect(r['run_manifest'])['state'],workflow.VISUAL_REJECTED)
        self.assertEqual(workflow.revise(d['run_manifest'],'Keep the torso anchor.',transport=lambda *_:self.fail('network'))['block_reason'],'LOCAL_DERIVATION_REVISION_REQUIRED')

    def test_annotations_reject_stale_incomplete_bool_and_nonfinite_points(self):
        from sprite_alignment_workflow import align_manifest
        r=self.generate();p=annotate(r,self.root/'annotations.json');original=json.loads(p.read_text())
        for kind in ('set','count','order','hash','bool','nan','outside','name','description','source'):
            with self.subTest(kind=kind):
                a=json.loads(json.dumps(original))
                if kind=='set':a['artifact_set_sha256']='0'*64
                elif kind=='count':a['frames'].pop()
                elif kind=='order':a['frames'].reverse()
                elif kind=='hash':a['frames'][0]['sha256']='0'*64
                elif kind=='bool':a['frames'][0]['point'][0]=True
                elif kind=='nan':a['frames'][0]['point'][0]=float('nan')
                elif kind=='outside':a['frames'][0]['point'][1]=-1
                elif kind=='name':a['reference']['name']=' '
                elif kind=='description':a['reference']['description']=' '
                else:a['reference']['source_point']=[9999,0]
                p.write_text(json.dumps(a));d=align_manifest(r['run_manifest'],p,output_root=self.root/'derived')
                self.assertEqual(d['block_reason'],'ALIGNMENT_ANNOTATIONS_INVALID',d)
                self.assertFalse((self.root/'derived').exists())

    def test_all_snapshot_proof_output_and_approval_tampering_block_delivery(self):
        for kind in ('input','annotation','proof','parent','source','output','approval','manifest_derivation','gif'):
            with self.subTest(kind=kind):
                d=aligned(self.generate(),self.root);self.assertTrue(self.review(d)['delivery_allowed'])
                p=Path(d['run_manifest']);m=json.loads(p.read_text());root=p.parent
                targets={'input':root/'derivation/input-01.png','annotation':root/'derivation/annotations.json',
                    'proof':root/'derivation/proof.json','parent':root/'derivation/parent-manifest.json',
                    'source':root/'source.png','output':Path(m['artifact_paths']['frames'][0]),
                    'approval':Path(m['visual_review']['path'])}
                targets['gif']=root/'derivation/parent-preview.gif'
                if kind=='manifest_derivation':m['derivation']['proof_sha256']='0'*64;p.write_text(json.dumps(m))
                else:targets[kind].write_bytes(b'tampered')
                inspected=workflow.inspect(p);self.assertFalse(inspected['delivery_allowed'],inspected)
                self.assertIn(inspected['block_reason'],{'ALIGNMENT_PROOF_INVALID','ARTIFACT_TAMPERED','VISUAL_REVIEW_TAMPERED','SOURCE_TAMPERED','SCOPE_TAMPERED'})
                self.assertFalse(workflow.export_artifacts(p,self.root/'out')['delivery_allowed'])

    def test_pass_requires_every_frame_semantic_body_findings(self):
        d=aligned(self.generate(),self.root)
        for kind in ('missing','proof','name','count','bool','duplicate','equipment_box'):
            with self.subTest(kind=kind):
                r=self.report(d);b=r['body_alignment']
                if kind=='missing':r.pop('body_alignment')
                elif kind=='proof':b['proof_sha256']='0'*64
                elif kind=='name':b['reference_name']='tail box'
                elif kind=='count':b['frame_findings'].pop()
                elif kind=='bool':b['frame_findings'][0]['fixed_x']=1
                elif kind=='equipment_box':b['frame_findings'][0]['excludes_weapon_tail_box']=False
                else:b['frame_findings'][-1]['frame']=1
                self.assertEqual(self.review(d,r)['block_reason'],'BODY_ALIGNMENT_REVIEW_INVALID')

    def test_legacy_policy_absent_scope_still_passes_and_exports(self):
        original=workflow._options
        def legacy(options):
            p,missing=original(options);p['request'].pop('alignment_policy');p['request']['grounded']=True;return p,missing
        with patch.object(workflow,'_options',side_effect=legacy):r=self.generate()
        self.assertTrue(self.review(r)['delivery_allowed'])
        self.assertTrue(workflow.export_artifacts(r['run_manifest'],self.root/'legacy')['delivery_allowed'])
        self.assertFalse((self.root/'legacy/alignment').exists())

    def test_replay_detects_output_even_when_mutable_manifest_hashes_are_rewritten(self):
        d=aligned(self.generate(),self.root);self.assertTrue(self.review(d)['delivery_allowed'])
        p=Path(d['run_manifest']);m=json.loads(p.read_text());target=Path(m['artifact_paths']['frames'][0])
        with Image.open(target) as im:image=im.convert('RGBA')
        image.putpixel((0,0),(1,2,3,4));image.save(target)
        m['artifact_hashes'][str(target)]=sha_file(target);p.write_text(json.dumps(m))
        self.assertEqual(workflow.inspect(p)['block_reason'],'ALIGNMENT_PROOF_INVALID')

    def test_unknown_policy_and_forged_approved_unaligned_scope_fail_closed(self):
        original=workflow._options
        def unknown(options):
            prepared,missing=original(options);prepared['request']['alignment_policy']='future_unreviewed_v99';return prepared,missing
        with patch.object(workflow,'_options',side_effect=unknown):r=self.generate()
        self.assertEqual(workflow.inspect(r['run_manifest'])['block_reason'],'ALIGNMENT_POLICY_UNSUPPORTED')
        r=self.generate();p=Path(r['run_manifest']);m=json.loads(p.read_text());receipt=p.parent/'attempt-01/visual-review-001.json'
        receipt.write_text(json.dumps(self.report(r)));m['state']=workflow.DELIVERABLE
        m['visual_review']={'path':str(receipt),'sha256':sha_file(receipt)};p.write_text(json.dumps(m))
        self.assertEqual(workflow.inspect(p)['block_reason'],'ALIGNMENT_REQUIRED')

    def test_width_expansion_limits_transparent_inputs_and_nonloop_timing(self):
        from sprite_alignment_workflow import align_manifest
        r=workflow._generate_legacy(self.options|{'loop':False,'frame_ms':120},transport=Provider())
        p=annotate(r,self.root/'edge.json');a=json.loads(p.read_text())
        for item in a['frames']:item['point']=[0,40]
        p.write_text(json.dumps(a));d=align_manifest(r['run_manifest'],p,output_root=self.root/'expanded')
        self.assertEqual(d['state'],workflow.READY,d)
        m=json.loads(Path(d['run_manifest']).read_text());self.assertGreater(m['request']['canvas'][0],192)
        with Image.open(d['artifact_paths']['preview_gif']) as im:
            self.assertNotIn('loop',im.info);self.assertEqual(im.info['duration'],120)
        # Actual sealed-parent artifacts with a dimension just over the helper cap
        # are rejected before producing a derived directory.
        parent=Path(r['run_manifest']);m=json.loads(parent.read_text())
        for name in m['artifact_paths']['frames']:
            image=Image.new('RGBA',(4097,96));image.putpixel((10,10),(1,2,3,255));image.save(name)
            m['artifact_hashes'][name]=sha_file(name)
        parent.write_text(json.dumps(m));r=workflow.inspect(parent);p=annotate(r,self.root/'limit.json')
        limited=align_manifest(parent,p,output_root=self.root/'limit')
        self.assertEqual(limited['block_reason'],'DIMENSION_LIMIT');self.assertFalse((self.root/'limit').exists())
        # An empty frame cannot supply an anatomical reference, even with valid hashes.
        empty=m['artifact_paths']['frames'][0];Image.new('RGBA',(192,96)).save(empty)
        m['artifact_hashes'][empty]=sha_file(empty);parent.write_text(json.dumps(m))
        self.assertFalse(align_manifest(parent,p,output_root=self.root/'empty')['delivery_allowed'])

    def test_cli_is_offline_and_returns_a_new_scope(self):
        import subprocess,sys
        import sprite_alignment_workflow
        r=self.generate();p=annotate(r,self.root/'cli-points.json')
        result=subprocess.run([sys.executable,'-B',sprite_alignment_workflow.__file__,'align','--run-manifest',
            r['run_manifest'],'--anchors-file',str(p),'--output-root',str(self.root/'cli')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        d=json.loads(result.stdout);self.assertEqual(d['state'],workflow.READY)
        self.assertNotEqual(d['run_manifest'],r['run_manifest'])

    def test_repeated_scope_name_conflict_preserves_existing_derivation(self):
        from types import SimpleNamespace
        import sprite_alignment_workflow as alignment
        r=self.generate();d=aligned(r,self.root);root=Path(d['run_manifest']).parent
        before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        with patch.object(alignment.uuid,'uuid4',return_value=SimpleNamespace(hex=root.name)):
            result=alignment.align_manifest(r['run_manifest'],self.root/'anchors.json',output_root=root.parent)
        self.assertEqual(result['block_reason'],'OUTPUT_CONFLICT')
        self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_public_verifier_checks_scope_source_provenance_without_loader(self):
        from sprite_alignment_workflow import verify_alignment
        from sheet_workflow_state import WorkflowError
        d=aligned(self.generate(),self.root);m=json.loads(Path(d['run_manifest']).read_text())
        m['source']['original_path']='forged-parent-path'
        with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_PROOF_INVALID'):verify_alignment(m)

    def test_parent_manifest_changed_during_verified_capture_is_rejected(self):
        from sprite_alignment_workflow import align_manifest
        r=self.generate();p=annotate(r,self.root/'race-points.json')
        real=workflow._verify_visual_state
        def mutate_after_verification(parent):
            real(parent)
            path=Path(r['run_manifest']);changed=json.loads(path.read_text())
            changed['attempts'][0]['jobs']['primary']['config_sha256']='0'*64
            path.write_text(json.dumps(changed))
        with patch.object(workflow,'_verify_visual_state',side_effect=mutate_after_verification):
            result=align_manifest(r['run_manifest'],p,output_root=self.root/'race-derived')
        self.assertEqual(result['state'],'BLOCKED',result)
        self.assertFalse((self.root/'race-derived').exists())
        self.assertEqual(workflow.inspect(r['run_manifest'])['block_reason'],'JOB_CONFIG_TAMPERED')

    def test_nonobject_body_findings_return_stable_error_and_preserve_ready(self):
        d=aligned(self.generate(),self.root);manifest=Path(d['run_manifest'])
        before=manifest.read_bytes()
        for value in (None,'not an object',17,[]):
            with self.subTest(value=value):
                report=self.report(d);report['body_alignment']['frame_findings'][0]=value
                result=self.review(d,report)
                self.assertEqual(result['block_reason'],'BODY_ALIGNMENT_REVIEW_INVALID')
                self.assertEqual(manifest.read_bytes(),before)
                self.assertEqual(workflow.inspect(manifest)['state'],workflow.READY)

    def test_completed_derivative_is_replayable_without_live_parent(self):
        import shutil
        r=self.generate();d=aligned(r,self.root)
        shutil.rmtree(Path(r['run_manifest']).parent)
        self.assertEqual(workflow.inspect(d['run_manifest'])['state'],workflow.READY)
        self.assertTrue(self.review(d)['delivery_allowed'])
        self.assertTrue(workflow.export_artifacts(d['run_manifest'],self.root/'detached')['delivery_allowed'])

    def test_parent_lock_is_held_and_scope_bytes_must_stay_unchanged(self):
        from sprite_alignment_workflow import align_manifest
        from sheet_workflow_state import WorkflowError,run_lock
        r=self.generate();p=annotate(r,self.root/'scope-race.json');root=Path(r['run_manifest']).parent
        real=workflow._verify_visual_state
        def mutate_scope_after_verification(parent):
            with self.assertRaisesRegex(WorkflowError,'RUN_LOCKED'):
                with run_lock(root):pass
            real(parent)
            # Semantically identical JSON still changes captured snapshot bytes.
            scope=root/'scope.json';scope.write_bytes(scope.read_bytes()+b'\n')
        with patch.object(workflow,'_verify_visual_state',side_effect=mutate_scope_after_verification):
            result=align_manifest(r['run_manifest'],p,output_root=self.root/'scope-race-derived')
        self.assertEqual(result['block_reason'],'ALIGNMENT_PARENT_CHANGED',result)
        self.assertFalse((self.root/'scope-race-derived').exists())

    def test_version_two_preserves_color_unique_to_later_frame_and_exact_animation_contract(self):
        from sprite_alignment_workflow import _artifacts
        frames=later_color_frames()
        for loop in (True,False):
            request={'frame_ms':150,'loop':loop,'pixel_art':True,'alignment_codec_version':2}
            payloads,qa=_artifacts(frames,request,{})
            with Image.open(io.BytesIO(payloads['preview.gif'])) as gif:
                self.assertEqual(gif.n_frames,8)
                self.assertEqual(gif.info.get('loop'),0 if loop else None)
                for i,source in enumerate(frames):
                    gif.seek(i);decoded=gif.convert('RGBA')
                    self.assertEqual(gif.info['duration'],150)
                    self.assertEqual(decoded.getchannel('A').tobytes(),source.getchannel('A').tobytes())
                    self.assertEqual(decoded.getpixel((5+i,5)),(10,10,240,255))
                gif.seek(3)
                self.assertEqual(gif.convert('RGBA').getpixel((41,41)),(230,10,35,255))
            self.assertEqual(qa['preview']['palette_strategy'],'max_coverage')

    def test_new_codec_version_is_sealed_and_version_tamper_blocks(self):
        d=aligned(self.generate(),self.root);p=Path(d['run_manifest']);m=json.loads(p.read_text())
        original=p.read_bytes()
        self.assertEqual(m['request'].get('alignment_codec_version'),3)
        proof=json.loads((p.parent/'derivation/proof.json').read_text())
        self.assertEqual(proof.get('alignment_codec_version'),3)
        self.assertEqual(proof.get('gif_mode'),'translated_parent_exact_v1')
        self.assertEqual(proof.get('parent_gif_sha256'),sha_file(p.parent/'derivation/parent-preview.gif'))
        m['request']['alignment_codec_version']=1;p.write_text(json.dumps(m))
        self.assertEqual(workflow.inspect(p)['block_reason'],'SCOPE_TAMPERED')
        p.write_bytes(original);proof['alignment_codec_version']=1
        (p.parent/'derivation/proof.json').write_text(json.dumps(proof))
        self.assertEqual(workflow.inspect(p)['block_reason'],'ALIGNMENT_PROOF_INVALID')

    def test_codec_version_absent_fixture_replays_historical_bytes(self):
        import sprite_alignment_workflow as alignment
        original=alignment._artifacts
        def legacy_before_seal(frames,request,report,**kwargs):
            request.pop('alignment_codec_version',None)
            return original(frames,request,report)
        with patch.object(alignment,'_artifacts',side_effect=legacy_before_seal):d=aligned(self.generate(),self.root)
        m=json.loads(Path(d['run_manifest']).read_text());self.assertNotIn('alignment_codec_version',m['request'])
        self.assertNotIn('alignment_codec_version',alignment.verify_alignment(m))
        before={str(p):p.read_bytes() for p in Path(d['run_manifest']).parent.rglob('*') if p.is_file()}
        self.assertEqual(workflow.inspect(d['run_manifest'])['state'],workflow.READY)
        self.assertEqual(before,{str(p):p.read_bytes() for p in Path(d['run_manifest']).parent.rglob('*') if p.is_file()})

    def test_unknown_or_boolean_alignment_codec_versions_fail_closed(self):
        from sprite_alignment_workflow import _artifacts
        from sheet_workflow_state import WorkflowError
        for version in (0,4,None,True,'2'):
            with self.subTest(version=version):
                with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_CODEC_UNSUPPORTED'):
                    _artifacts(later_color_frames(),{'alignment_codec_version':version},{})

    def test_codec3_gif_snapshot_is_exact_source_and_output_is_exact_decoded_translation(self):
        r=self.generate();d=aligned(r,self.root);self.assertEqual(d['state'],workflow.READY,d)
        m=json.loads(Path(d['run_manifest']).read_text());parent=json.loads(Path(r['run_manifest']).read_text())
        captured=Path(d['run_manifest']).parent/'derivation/parent-preview.gif'
        self.assertEqual(captured.read_bytes(),Path(parent['artifact_paths']['preview_gif']).read_bytes())
        proof=json.loads((captured.parent/'proof.json').read_text())
        with Image.open(captured) as source,Image.open(m['artifact_paths']['preview_gif']) as child:
            self.assertEqual(child.n_frames,source.n_frames)
            for i,t in enumerate(proof['transforms']):
                source.seek(i);child.seek(i);expected=Image.new('RGBA',child.size)
                expected.paste(source.convert('RGBA'),(t['dx'],0))
                import sheet_images
                self.assertEqual(sheet_images._canonical_rgba_bytes(child.convert('RGBA')),sheet_images._canonical_rgba_bytes(expected))

    def test_codec2_fixture_is_not_migrated_and_retains_its_original_proof(self):
        import sprite_alignment_workflow as alignment
        original=alignment._artifacts
        for version in (1,2):
            with self.subTest(version=version):
                def stored_version_before_seal(frames,request,report,**kwargs):
                    request['alignment_codec_version']=version
                    return original(frames,request,report)
                with patch.object(alignment,'_artifacts',side_effect=stored_version_before_seal):d=aligned(self.generate(),self.root)
                m=json.loads(Path(d['run_manifest']).read_text());proof=alignment.verify_alignment(m)
                self.assertEqual(proof['alignment_codec_version'],version);self.assertNotIn('parent_gif_sha256',proof)
                self.assertNotIn('parent-preview.gif',m['derivation']['files'])
                self.assertEqual(workflow.inspect(d['run_manifest'])['state'],workflow.READY)

    def test_parent_preview_changed_during_capture_cannot_become_a_trusted_snapshot(self):
        from sprite_alignment_workflow import align_manifest
        r=self.generate();p=annotate(r,self.root/'gif-race.json');real=workflow._verify_visual_state
        def change_gif_after_verification(parent):
            real(parent);Path(parent['artifact_paths']['preview_gif']).write_bytes(b'changed gif')
        with patch.object(workflow,'_verify_visual_state',side_effect=change_gif_after_verification):
            d=align_manifest(r['run_manifest'],p,output_root=self.root/'gif-race')
        self.assertEqual(d['block_reason'],'ALIGNMENT_PARENT_CHANGED',d)
        self.assertFalse((self.root/'gif-race').exists())

if __name__=='__main__':unittest.main()
