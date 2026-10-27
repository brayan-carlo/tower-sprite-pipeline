"""Offline native-origin alignment integration; synthetic pixels are not art approval."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
import native_articulated_workflow as articulated
import native_sprite_workflow as native
import native_exports
import generic_sprite_workflow as generic
import sprite_alignment_workflow as alignment
from sheet_workflow_state import WorkflowError, scope_sha, sha_file
from test_native_articulated_workflow import fixture, pass_report, read, write, NOTE
import test_native_geometry as geometry_fixture
import test_native_geometry_workflow as sheet_fixture
from test_sprite_alignment_workflow import body_review
import test_sheet_review as review_fixture
from test_native_task_workflow import task_options

POLICY='body_x_preserve_y_v1'

class NativeAlignmentTests(unittest.TestCase):
    def context(self):
        return task_options(self.root,self.recipe.parent/'source.png',read(self.recipe)['action'])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.recipe,self.geometry=fixture(self.root/'input')
        recipe=read(self.recipe)
        for i,frame in enumerate(recipe['frames']):frame['layers'][0]['target'][1]=i%2
        write(self.recipe,recipe)

    def native_report(self,r):
        report=pass_report(r,self.geometry)
        for i,frame in enumerate(report['geometry']['frames']):
            frame['body_anchor'][1]+=i%2
            for anchor in frame['anchors']:
                for point in anchor['points']:point[1]+=i%2
            for support in frame['supports'].values():
                if support['point'] is not None:support['point'][1]+=i%2
        return report

    def parent(self):
        r=articulated.build(self.recipe,self.geometry,self.root/'runs',**self.context())
        return articulated.review(r['run_manifest'],self.native_report(r))

    def derive(self,r):
        a={'schema_version':1,'artifact_set_sha256':r['artifact_set_sha256'],
           'reference':{'name':'torso midpoint','description':NOTE,'source_point':[30,30]},
           'frames':[{'frame':i+1,'sha256':sha_file(p),'point':[30+i,30+i%2]}
                     for i,p in enumerate(r['artifact_paths']['frames'])]}
        path=self.root/'annotations.json';write(path,a)
        return alignment.align_manifest(r['run_manifest'],path,output_root=self.root/'derived')

    def child_review(self,r):
        report=review_fixture.VisualReviewTests.report(self,r);report['body_alignment']=body_review(r)
        path=self.root/'review.json';write(path,report)
        return generic.record_visual_review(r['run_manifest'],path)

    def test_new_articulated_semantic_pass_requires_alignment_and_blocks_direct_helpers(self):
        r=self.parent();m=read(r['run_manifest'])
        self.assertEqual(r['request'].get('alignment_policy'),POLICY)
        self.assertEqual(r['state'],'READY_FOR_ALIGNMENT');self.assertFalse(r['delivery_allowed'])
        for call in (lambda:articulated.export(r['run_manifest'],self.root/'delivery'),
                     lambda:native_exports.register_export(Path(r['run_manifest']),m,self.root/'delivery',{},r['artifact_set_sha256']),
                     lambda:native_exports.write_export(Path(r['run_manifest']),m,{},{}),
                     lambda:native_exports.adopt_export(Path(r['run_manifest']),m,self.root/'delivery'),
                     lambda:articulated._delivery_receipt(Path(r['run_manifest']),m,m['attempts'][-1],(64,64),m['attempts'][-1]['visual_review'])):
            with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_REQUIRED'):call()
        self.assertFalse(articulated.recover_exports(r['run_manifest'])['delivery_allowed'])
        m['state']='READY_FOR_DELIVERY';write(r['run_manifest'],m)
        with self.assertRaises(WorkflowError):articulated.export(r['run_manifest'],self.root/'delivery')
        self.assertFalse((self.root/'delivery').exists())

    def test_native_derivative_requires_fresh_review_and_live_parent_remains_authoritative(self):
        r=self.parent();parent=Path(r['run_manifest']).parent
        before={str(p):p.read_bytes() for p in parent.rglob('*') if p.is_file()}
        d=self.derive(r);self.assertEqual(d['state'],generic.READY,d)
        child=read(d['run_manifest']);pm=read(r['run_manifest'])
        preview=pm['attempts'][-1]['artifact_paths']['preview']
        snapshot=Path(d['run_manifest']).parent/'derivation/parent-preview.gif'
        self.assertEqual(child['request']['alignment_codec_version'],3)
        self.assertEqual(snapshot.read_bytes(),Path(preview).read_bytes())
        self.assertEqual(read(snapshot.parent/'proof.json')['parent_gif_sha256'],sha_file(preview))
        self.assertFalse(d['delivery_allowed']);self.assertEqual(d['spent_estimate_usd'],0)
        self.assertEqual(before,{str(p):p.read_bytes() for p in parent.rglob('*') if p.is_file()})
        self.assertTrue(self.child_review(d)['delivery_allowed'])
        self.assertTrue(generic.export_artifacts(d['run_manifest'],self.root/'delivery')['delivery_allowed'])
        rejected=self.native_report(r);rejected['decision']='reject';rejected['checks']['motion']['status']='fail'
        articulated.review(r['run_manifest'],rejected)
        self.assertFalse(generic.export_artifacts(d['run_manifest'],self.root/'delivery2')['delivery_allowed'])
        self.assertEqual(articulated.inspect(r['run_manifest'])['state'],articulated.REJECT)

    def test_incomplete_native_support_equipment_and_transition_review_cannot_be_laundered(self):
        for section in ('geometry','components','motion'):
            r=articulated.build(self.recipe,self.geometry,self.root/'runs',**self.context());report=pass_report(r,self.geometry);report.pop(section)
            with self.assertRaises(WorkflowError):articulated.review(r['run_manifest'],report)
            d=self.derive(r);self.assertEqual(d['state'],'BLOCKED',d)

    def test_new_sheet_policy_and_semantic_state(self):
        helper=sheet_fixture.GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        r=helper.ingested();self.assertEqual(r['request'].get('alignment_policy'),POLICY)
        r=native.calibrate(r['run_manifest'],helper.geometry_report(r))
        r=native.review(r['run_manifest'],helper.report(r))
        self.assertEqual(r['state'],'READY_FOR_ALIGNMENT');self.assertFalse(r['delivery_allowed'])
        with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_REQUIRED'):native.export(r['run_manifest'],self.root/'sheet-delivery')
        for call in (lambda:native.adopt_export(r['run_manifest'],self.root/'missing-delivery'),
                     lambda:native.recover_exports(r['run_manifest'])):
            try:self.assertFalse(call()['delivery_allowed'])
            except WorkflowError as error:self.assertEqual(error.code,'ALIGNMENT_REQUIRED')
        geometry=read(r['artifact_paths']['geometry'])
        a={'schema_version':1,'artifact_set_sha256':r['artifact_set_sha256'],
           'reference':{'name':'torso midpoint','description':NOTE,'source_point':helper.contract()['body_anchor']},
           'frames':[{'frame':i+1,'sha256':sha_file(p),'point':f['output_body_anchor']}
                     for i,(p,f) in enumerate(zip(r['artifact_paths']['frames'],geometry['frames']))]}
        annotation=self.root/'sheet-anchors.json';write(annotation,a)
        d=alignment.align_manifest(r['run_manifest'],annotation,output_root=self.root/'sheet-derived')
        self.assertEqual(d['state'],generic.READY,d);self.assertTrue(self.child_review(d)['delivery_allowed'])
        self.assertTrue(generic.export_artifacts(d['run_manifest'],self.root/'sheet-delivery')['delivery_allowed'])
        rejected=helper.report(r,'motion');native.review(r['run_manifest'],rejected)
        self.assertFalse(generic.export_artifacts(d['run_manifest'],self.root/'sheet-delivery2')['delivery_allowed'])

    def test_common_geometry_origin_preserves_row_y_and_common_scale(self):
        import native_geometry as geometry
        helper=geometry_fixture.GeometryTests();helper.setUp();self.addCleanup(helper.doCleanups)
        for i in range(4,8):
            x=(i%4)*512;y=512
            cell=helper.sheet.crop((x,y,x+512,y+512));helper.sheet.paste(Image.new('RGBA',(512,512)),(x,y))
            helper.sheet.paste(cell,(x,y+17))
            a=helper.report['frames'][i]
            a['body_anchor'][1]+=17
            for anchor in a['anchors'].values():
                for p in anchor['points']:p[1]+=17
            if a['support_point'] is not None:a['support_point'][1]+=17
        frames,meta,_=geometry.calibrate_frames(helper.sheet,helper.source,helper.contract,helper.report,alignment_policy=POLICY)
        self.assertEqual(meta['scale'],.5)
        ys=[a['output_body_anchor'][1] for a in meta['frames']]
        self.assertEqual(ys[4]-ys[0],8.5);self.assertEqual(ys[1]-ys[0],-20)
        self.assertEqual(len({a['output_body_anchor'][0] for a in meta['frames']}),1)

    def test_geometry_limit_applies_to_all_eight_output_frames(self):
        import native_geometry as geometry
        from types import SimpleNamespace
        helper=geometry_fixture.GeometryTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.contract['anchors']['head']['points']=[[14,20],[42,20]]
        helper.contract['anchors']['torso']['points']=[[30,15],[30,43]]
        for i,a in enumerate(helper.report['frames']):
            cell=Image.new('RGBA',(512,512));ImageDraw.Draw(cell).rectangle((10,10,490,490),fill=(80+i,40,40,255))
            helper.sheet.paste(cell,((i%4)*512,(i//4)*512))
            a['anchors']['head']['points']=[[20,20],[24,20]]
            a['anchors']['torso']['points']=[[30,25],[30,29]]
            a['body_anchor']=[250,250];a['support_point']=[250,490] if i%2==0 else None
        # Dimension-only resize doubles prevent a rejected-boundary fixture from
        # allocating hundreds of MB before the output limit can be asserted.
        def dimensions_only(_image,size,*args):return SimpleNamespace(width=size[0],height=size[1])
        real_new=Image.new
        def bounded_new(mode,size,*args):
            if size[0]*size[1]>8_000_000:self.fail('Oversized output was allocated before aggregate pixel validation')
            return real_new(mode,size,*args)
        with patch.object(Image.Image,'resize',dimensions_only),patch.object(Image,'new',bounded_new):
            with self.assertRaisesRegex(WorkflowError,'OUTPUT_CANVAS_LIMIT'):
                geometry.calibrate_frames(helper.sheet,helper.source,helper.contract,helper.report,alignment_policy=POLICY)

    def test_registration_common_origin_preserves_row_and_airborne_accessory_pixels(self):
        from native_sheet_images import _register_strides
        im=Image.new('RGBA',(2048,1024))
        for i in range(8):
            x=(i%4)*512+100;y=(i//4)*512+100+(17 if i>=4 else 0)-(40 if i%2 else 0)
            draw=ImageDraw.Draw(im);draw.rectangle((x,y,x+100,y+180),fill=(100+i,40,40,255))
            if i%2:draw.rectangle((x+80,y+160,x+120,y+230),fill=(100+i,40,40,255))
        out,meta=_register_strides(im,strict_components=True,alignment_policy=POLICY)
        boxes=[out.crop(((i%4)*512,(i//4)*512,(i%4+1)*512,(i//4+1)*512)).getbbox() for i in range(8)]
        self.assertEqual(boxes[4][1]-boxes[0][1],17);self.assertEqual(boxes[1][1]-boxes[0][1],-40)
        self.assertEqual(im.getchannel('A').histogram()[1:],out.getchannel('A').histogram()[1:])
        legacy,_=_register_strides(im,strict_components=True)
        legacy_boxes=[legacy.crop(((i%4)*512,(i//4)*512,(i%4+1)*512,(i//4+1)*512)).getbbox() for i in range(8)]
        self.assertEqual(legacy_boxes[4][1]-legacy_boxes[0][1],0)
        oversized=Image.new('RGBA',(2048,1200))
        for i in range(8):
            x=(i%4)*512+100;y=(i//4)*600+10
            ImageDraw.Draw(oversized).rectangle((x,y,x+100,y+540),fill=(100,40,40,255))
        with self.assertRaises(WorkflowError):_register_strides(oversized,strict_components=True,alignment_policy=POLICY)

    def test_legacy_geometry_scope_remains_policy_absent_and_exports_full_receipt(self):
        helper=sheet_fixture.GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        r=native._prepare(helper.source,helper.root/'runs','left',source_geometry=helper.contract())
        r=native.ingest(r['run_manifest'],helper.raw,NOTE)
        r=native.calibrate(r['run_manifest'],helper.geometry_report(r));r=native.review(r['run_manifest'],helper.report(r))
        self.assertNotIn('alignment_policy',r['request']);self.assertEqual(r['state'],native.PASS)
        native.export(r['run_manifest'],self.root/'legacy-delivery')
        receipt=read(self.root/'legacy-delivery/delivery.json')
        self.assertEqual(receipt['geometry']['scale'],.5);self.assertTrue(receipt['delivery_allowed'])

    def test_live_tamper_stale_review_and_removed_parent_block_child_without_mutating_parent(self):
        for kind in ('frame','review','recipe','removed'):
            with self.subTest(kind=kind):
                r=self.parent();d=self.derive(r);self.assertEqual(d['state'],generic.READY,d)
                p=Path(r['run_manifest']);m=read(p)
                if kind=='frame':Path(r['artifact_paths']['frames'][0]).write_bytes(b'changed frame')
                elif kind=='recipe':Path(m['attempts'][-1]['snapshot']['recipe_path']).write_bytes(b'changed recipe')
                elif kind=='removed':p.rename(p.with_suffix('.removed'))
                else:
                    m['attempts'][-1]['visual_review']['artifact_set_sha256']='0'*64;write(p,m)
                before=p.read_bytes() if p.exists() else None
                self.assertFalse(generic.export_artifacts(d['run_manifest'],self.root/f'delivery-{kind}')['delivery_allowed'])
                if before is not None:self.assertEqual(p.read_bytes(),before)

    def test_native_adapter_uses_verified_buffers_and_rejects_input_changed_after_verification(self):
        r=self.parent();original=articulated._verify;target=Path(r['artifact_paths']['frames'][0]);old=target.read_bytes()
        def changed_after_verification(*args,**kwargs):
            verified=original(*args,**kwargs);target.write_bytes(b'new unreviewed bytes');return verified
        with patch.object(articulated,'_verify',side_effect=changed_after_verification):
            d=self.derive(r)
        self.assertEqual(d['state'],'BLOCKED',d)
        if (self.root/'derived').exists():
            for captured in (self.root/'derived').glob('*/derivation/input-01.png'):
                self.assertEqual(captured.read_bytes(),old)

    def test_new_articulated_resume_revise_and_pixels_preserve_recipe_and_policy(self):
        import native_articulation
        expected,_=native_articulation.render_recipe(self.recipe)
        with patch.object(articulated,'_write_artifacts',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):articulated.build(self.recipe,self.geometry,self.root/'runs',**self.context())
        p=next((self.root/'runs').glob('*/run-manifest.json'))
        r=articulated.resume(p);self.assertEqual(r['request']['alignment_policy'],POLICY)
        for path,frame in zip(r['artifact_paths']['frames'],expected):
            with Image.open(path) as im:self.assertEqual(im.tobytes(),frame.tobytes())
        r=articulated.revise(p,self.recipe,NOTE)
        self.assertEqual(r['request']['alignment_policy'],POLICY)
        legacy=articulated._build(self.recipe,self.geometry,self.root/'legacy')
        self.assertNotIn('alignment_policy',articulated.resume(legacy['run_manifest'])['request'])
        legacy=articulated.revise(legacy['run_manifest'],self.recipe,NOTE)
        self.assertNotIn('alignment_policy',legacy['request'])

    def test_unknown_policy_and_forged_ready_without_review_fail_closed(self):
        for policy in (None,'unknown',True):
            r=articulated.build(self.recipe,self.geometry,self.root/'runs',**self.context());m=read(r['run_manifest'])
            m['request']['alignment_policy']=policy;m['scope_sha256']=scope_sha(articulated._scope(m));write(r['run_manifest'],m)
            with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_POLICY_UNSUPPORTED'):articulated.inspect(r['run_manifest'])
        r=articulated.build(self.recipe,self.geometry,self.root/'runs',**self.context());m=read(r['run_manifest']);m['state']='READY_FOR_ALIGNMENT';write(r['run_manifest'],m)
        self.assertEqual(self.derive(r)['state'],'BLOCKED')

    def test_native_origin_cannot_be_laundered_by_rederiving_a_v3_child(self):
        r=self.parent();d=self.derive(r);self.assertEqual(d['state'],generic.READY,d)
        m=read(d['run_manifest']);a={'schema_version':1,'artifact_set_sha256':d['artifact_set_sha256'],
            'reference':{'name':'torso midpoint','description':NOTE,'source_point':[30,30]},
            'frames':[{'frame':i+1,'sha256':sha_file(p),'point':[30,30+i%2]}
                      for i,p in enumerate(m['artifact_paths']['frames'])]}
        path=self.root/'child-anchors.json';write(path,a)
        result=alignment.align_manifest(d['run_manifest'],path,output_root=self.root/'grandchild')
        self.assertEqual(result['state'],'BLOCKED',result)
        self.assertFalse((self.root/'grandchild').exists())

    def test_rejection_in_native_history_cannot_be_overridden_by_forged_later_pass(self):
        r=self.parent();original=copy.deepcopy(read(r['run_manifest'])['attempts'][-1]['visual_review'])
        report=self.native_report(r);report['decision']='reject';report['checks']['motion']['status']='fail'
        articulated.review(r['run_manifest'],report)
        m=read(r['run_manifest']);a=m['attempts'][-1]
        a['review_history'].append(original);a['visual_review']=original;m['state']='READY_FOR_ALIGNMENT';write(r['run_manifest'],m)
        self.assertEqual(self.derive(r)['state'],'BLOCKED')

    def test_sheet_source_chain_replays_raw_not_only_mutable_processing_hashes(self):
        import native_alignment_source
        from generic_sprite_workflow import _review_target
        helper=sheet_fixture.GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        r=helper.ingested();r=native.calibrate(r['run_manifest'],helper.geometry_report(r))
        r=native.review(r['run_manifest'],helper.report(r));m=read(r['run_manifest']);a=m['attempts'][-1]
        with Image.open(a['raw']['path']) as source:
            source.putpixel((200,200),(240,240,240));source.save(a['raw']['path'])
        a['raw']['sha256']=sha_file(a['raw']['path'])
        from native_sheet_images import pixel_sha256
        a['raw']['pixel_sha256']=pixel_sha256(a['raw']['path'])
        processing_path=a['processing_paths']['processing_report'];processing=read(processing_path)
        processing['input_sha256']=a['raw']['sha256'];write(processing_path,processing)
        a['processing_hashes'][processing_path]=sha_file(processing_path)
        report_path=a['artifact_paths']['geometry_review'];report=read(report_path)
        report['geometry_target_sha256']=native._geometry_target(m);write(report_path,report)
        a['artifact_hashes'][report_path]=sha_file(report_path)
        a['visual_review']['artifact_set_sha256']=_review_target(native._context(m));write(r['run_manifest'],m)
        with self.assertRaises(WorkflowError):native_alignment_source.capture(r['run_manifest'])

    def test_sheet_adapter_replays_explicit_matte_component_decisions(self):
        import native_alignment_source
        helper=sheet_fixture.GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        helper.test_corner_cleanup_requires_bound_review_and_preserves_generation_attempt()
        p=next((helper.root/'runs').glob('*/run-manifest.json'));r=native.inspect(p)
        r=native.calibrate(p,helper.geometry_report(r));r=native.review(p,helper.report(r))
        captured,evidence,origin=native_alignment_source.capture(p)
        self.assertEqual(captured[1]['state'],'READY_FOR_ALIGNMENT')
        self.assertEqual(origin['artifact_set_sha256'],r['artifact_set_sha256'])

    def test_native_capture_holds_parent_lifecycle_lock(self):
        import native_alignment_source
        from sheet_workflow_state import run_lock
        r=self.parent();original=articulated._verify
        def verify_while_locked(path,*args,**kwargs):
            with self.assertRaisesRegex(WorkflowError,'RUN_LOCKED'):
                with run_lock(Path(path).parent):pass
            return original(path,*args,**kwargs)
        with patch.object(articulated,'_verify',side_effect=verify_while_locked):native_alignment_source.capture(r['run_manifest'])

    def flagged_sheet(self):
        helper=sheet_fixture.GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        with Image.open(helper.raw) as raw:
            ImageDraw.Draw(raw).rectangle((120,150,440,330),fill=(30,40,35));raw.save(helper.raw)
        r=helper.ingested();r=native.calibrate(r['run_manifest'],helper.geometry_report(r))
        self.assertEqual(r['consistency']['flagged_frames'],[1])
        return helper,r

    def sheet_annotation(self,r):
        geometry=read(r['artifact_paths']['geometry']);path=self.root/'flagged-anchors.json'
        write(path,{'schema_version':1,'artifact_set_sha256':r['artifact_set_sha256'],
            'reference':{'name':'torso midpoint','description':NOTE,'source_point':[35,30]},
            'frames':[{'frame':i+1,'sha256':sha_file(p),'point':frame['output_body_anchor']}
                      for i,(p,frame) in enumerate(zip(r['artifact_paths']['frames'],geometry['frames']))]})
        return path

    def test_native_review_recomputes_flagged_findings_after_manifest_only_diagnostic_mutation(self):
        helper,r=self.flagged_sheet();report=helper.report(r);report['flagged_frame_findings']=[]
        with self.assertRaisesRegex(WorkflowError,'FLAGGED_FRAMES_UNREVIEWED'):native.review(r['run_manifest'],report)
        p=Path(r['run_manifest']);m=read(p);scope=m['scope_sha256'];hashes=copy.deepcopy(m['attempts'][-1]['artifact_hashes'])
        m['attempts'][-1]['consistency']['flagged_frames']=[];write(p,m);before=p.read_bytes()
        with self.assertRaisesRegex(WorkflowError,'CONSISTENCY_INVALID'):native.review(p,report)
        self.assertEqual(p.read_bytes(),before);self.assertEqual(read(p)['scope_sha256'],scope)
        self.assertEqual(read(p)['attempts'][-1]['artifact_hashes'],hashes)

    def test_native_diagnostic_mutation_blocks_capture_creation_and_live_child_gates(self):
        import native_alignment_source
        helper,r=self.flagged_sheet();r=native.review(r['run_manifest'],helper.report(r))
        annotation=self.sheet_annotation(r)
        d=alignment.align_manifest(r['run_manifest'],annotation,output_root=self.root/'flagged-derived')
        self.assertEqual(d['state'],generic.READY,d)
        p=Path(r['run_manifest']);m=read(p);m['attempts'][-1]['consistency']['flagged_frames']=[];write(p,m)
        before=p.read_bytes()
        with self.assertRaisesRegex(WorkflowError,'CONSISTENCY_INVALID'):native_alignment_source.capture(p)
        rejected=alignment.align_manifest(p,annotation,output_root=self.root/'tampered-derived')
        self.assertEqual(rejected['state'],'BLOCKED',rejected)
        self.assertFalse((self.root/'tampered-derived').exists())
        self.assertEqual(self.child_review(d)['block_reason'],'ALIGNMENT_PROOF_INVALID')
        exported=generic.export_artifacts(d['run_manifest'],self.root/'tampered-delivery')
        self.assertFalse(exported['delivery_allowed']);self.assertEqual(exported['block_reason'],'ALIGNMENT_PROOF_INVALID')
        self.assertFalse((self.root/'tampered-delivery').exists());self.assertEqual(p.read_bytes(),before)

    def test_native_review_rejects_captured_qa_consistency_disagreement(self):
        from generic_sprite_workflow import _review_target
        helper,r=self.flagged_sheet();p=Path(r['run_manifest']);m=read(p);a=m['attempts'][-1]
        qa_path=a['artifact_paths']['qa_report'];qa=read(qa_path);qa['consistency']['flagged_frames']=[];write(qa_path,qa)
        a['artifact_hashes'][qa_path]=sha_file(qa_path);write(p,m)
        report=helper.report(r);report['artifact_set_sha256']=_review_target(native._context(m));before=p.read_bytes()
        with self.assertRaisesRegex(WorkflowError,'CONSISTENCY_INVALID'):native.review(p,report)
        self.assertEqual(p.read_bytes(),before)

if __name__=='__main__':unittest.main()
