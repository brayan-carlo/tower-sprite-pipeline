"""Physical regional workflow tests: genuine local FFmpeg, temporary task history."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
import base64
import re
import zlib
from unittest.mock import patch
from PIL import Image
from sheet_workflow_state import encoded, scope_sha, sha_file, WorkflowError
import sprite_task_ledger as ledger
import sheet_video
try:
    import regional_video_workflow as regional
except ImportError:
    regional = None


class RegionalWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='regional-real-media-')
        cls.base = Path(cls.temp.name)
        cls.ffmpeg = sheet_video.resolve_ffmpeg()
        cls.source = cls.base/'source.png'
        im=Image.new('RGBA',(16,16),(0,0,0,0))
        for x in range(4,12):
            for y in range(2,14):im.putpixel((x,y),(120,60,30,255))
        im.save(cls.source)
        padded=Image.new('RGBA',(32,24));padded.paste(im,(8,4));padded.save(cls.base/'padded.png')
        cls.prepared=sheet_video.prepare_source(cls.base/'padded.png',cls.base/'provider.png')
        cls.color=Image.new('RGB',(1440,1440),(40,60,80));cls.color.save(cls.base/'color.png')
        cls.mask=Image.new('RGB',(1440,1440));cls.mask.paste((255,255,255),(200,200,240,240));cls.mask.save(cls.base/'mask.png')
        for name in ('color','mask'):
            subprocess.run([cls.ffmpeg,'-v','error','-loop','1','-i',str(cls.base/(name+'.png')),'-frames:v','81','-r','24','-c:v','libx264rgb','-crf','0','-preset','ultrafast','-y',str(cls.base/(name+'.mp4'))],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        tiny=[]
        for i in range(81):
            frame=Image.new('RGB',(16,16),(40,60,80));x=3+i%5;y=3+(i//5)%3
            frame.paste((80+i,90,50),(x,y,x+4,y+5));tiny.append(frame.tobytes())
        subprocess.run([cls.ffmpeg,'-v','error','-f','rawvideo','-pix_fmt','rgb24','-s','16x16','-r','24','-i','pipe:0','-frames:v','81','-c:v','libx264rgb','-crf','0','-preset','ultrafast','-y',str(cls.base/'result.mp4')],input=b''.join(tiny),check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        self.assertIsNotNone(regional,'regional consumer implementation missing')
        self.tmp=tempfile.TemporaryDirectory(prefix='regional-task-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fixture()

    def ref(self,path):return dict(path=str(Path(path).resolve()),sha256=sha_file(path))
    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(encoded(value));return p

    def fixture(self):
        self.h=sha_file(self.source)
        input_base=getattr(self,'input_base',self.base);width=self.color.width
        raw=input_base/'color.mp4';prepared=self.base/'padded.png'
        job=dict(path=str(raw.parent),state='DOWNLOADED',request_id='historical-id',raw_path=str(raw),raw_sha256=sha_file(raw))
        parent=dict(run_id='historical',source=self.ref(prepared),request=dict(canvas=[32,24],frame_ms=100,pixel_art=True),attempts=[dict(index=1,attempt_id=None,prepared=self.prepared,jobs=dict(primary=job))])
        if getattr(self,'extra_parent_attempt',False):
            other=dict(job,raw_path=str(self.base/'mask.mp4'),raw_sha256=sha_file(self.base/'mask.mp4'))
            parent['attempts'].append(dict(index=2,attempt_id='other-attempt',prepared=self.prepared,jobs=dict(primary=other)))
        pp=self.write('historical/run-manifest.json',parent)
        sp=self.write('sidecar.json',dict(source=str(self.source),source_sha256=self.h,prepared=str(prepared),prepared_sha256=sha_file(prepared),canvas=[32,24],offset=[8,4],source_pixels_unchanged=True,scale_factor=1,reason='fixture padding'))
        evidence=[self.ref(pp),self.ref(sp)]
        history=dict(history_id='historical-source',target_id='target',source_sha256=self.h,run_id='historical',attempt_id=None,attempt_index=1,source_history_id=None,kind='provider_generation',mechanism=dict(origin='fal',route='video',model='fal-ai/kling-video/v3/pro/image-to-video',conditioning='single_source',resolution='legacy_unsupported',current_descriptor=None),local_attempt_limit=2,global_attempt_limit=None,feedback=None,cost_basis='fal_operational_reservations',reservation_ids=['old'],reviews=[],evidence=evidence)
        if getattr(self,'additional_history_evidence',False):
            history['evidence']=[*evidence,self.ref(self.write('extra-history.json',dict(review='Additional actual sealed history evidence')))]
        bundle=dict(schema='sprite_task_history_v1',migration_id='test-migration',eligibility_policy='historical_generation_closed_local_source_v1',coverage=dict(target_ids=['target'],evidence=evidence,complete=True),reservations=[dict(import_id='old',target_id='target',kind='provider_animation',source_sha256=self.h,config_sha256='a'*64,evidence_sha256=sha_file(pp),amount_usd='0.1',provider_request_id='historical-id',uncertain=False)],attempts=[history])
        bundlehash=scope_sha(bundle)
        auth=dict(task_id='regional-fixture',cap_usd=getattr(self,'task_cap','1'),budget_basis='fal_operational_reservations',allowed_operations=list(ledger.OPERATION_KINDS),evidence_ref='test human authorization')
        target=dict(target_id='target',source_sha256=self.h,action='run',orientation='left',requirements=dict(frames=8,pixel_art=True))
        self.task=ledger.create_task(self.root/'tasks',auth,[target],historical_import=dict(schema='sprite_history_requirement_v1',migration_id='test-migration',bundle_sha256=bundlehash))['task_manifest']
        ledger.import_task_history(self.task,bundle)
        lineage=self.write('lineage.json',dict(observation='reviewed canonical padding'))
        linkage=self.write('linkage.json',dict(observation='reviewed historical evidence'))
        p=dict(schema='regional_provenance_v1',task_id='regional-fixture',target_id='target',history_bundle_sha256=bundlehash,history_id='historical-source',canonical=self.ref(self.source),prepared=self.ref(prepared),provider_prepared=self.ref(self.base/'provider.png'),parent_manifest=self.ref(pp),sidecar=self.ref(sp),raw=self.ref(raw),evidence=evidence,padding=dict(canvas=[32,24],offset=[8,4],scale=1),lineage_proof=self.ref(lineage),linkage_proof=self.ref(linkage))
        self.provenance=self.write('provenance.json',p)
        rgb=hashlib.sha256(self.color.tobytes()).hexdigest();mask=hashlib.sha256(self.mask.tobytes()).hexdigest()
        frames=[dict(source_index=i,pts=dict(num=i,den=24),source_rgb_sha256=rgb,video_rgb_sha256=rgb,mask_rgb_sha256=mask) for i in range(81)]
        geometry=dict(policy='inverse_parent_preparation_v1',resize_canvas=[32,32],crop=[0,4,32,24],output_canvas=[32,24],scale_numerator=32,scale_denominator=width)
        review=self.write('mask-review.json',dict(review='all 81 masks and crop inspected'))
        media=dict(schema='regional_media_v1',video=self.ref(raw),mask=self.ref(input_base/'mask.mp4'),identity=self.ref(self.source),window=dict(start=0,count=81,fps=24,width=width,height=width),frames=frames,selection=[0,3,6,9,13,16,19,22],geometry=geometry,mask_review=self.ref(review))
        self.media=self.write('media.json',media)
        checks={k:dict(pass_=True,notes='Reviewed physical frame evidence') for k in regional.INPUT_CHECKS}
        checks={k:{'pass':v['pass_'],'notes':v['notes']} for k,v in checks.items()}
        pre=dict(schema='regional_preflight_v1',decision='pass',provenance_sha256=sha_file(self.provenance),media_sha256=sha_file(self.media),reports=[self.ref(review)],defect_frames=[19,22],checks=checks,frame_findings=[dict(source_index=i,mask_sha256=mask,eligible=True,notes='Reviewed hand-local mask and far-hand control') for i in range(81)])
        self.preflight=self.write('preflight.json',pre)

    def prepare(self):return regional.prepare(self.task,'target',str(self.provenance),str(self.media),str(self.preflight),output_root=str(self.root/'runs'),prompt='Correct the near hand while retaining the actual stop.',negative_prompt='No changes outside the reviewed local mask.',ffmpeg_exe=self.ffmpeg)

    def test_legitimate_padding_prepares_without_consuming_attempt(self):
        r=self.prepare();self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',r['state'],r)
        self.assertEqual('0.9',r['task_budget']['available_estimate_usd'])
        self.assertEqual({},ledger.inspect_task(self.task)['attempts'])
        path,m,c=regional.validate_regional_attempt(r['run_manifest'],require_artifacts=False)
        self.assertEqual(1440,c['media']['geometry']['scale_denominator'])
        self.assertEqual([32,24],m['request']['canvas'])

    def fixture_1024(self):
        self.input_base=self.root/'input-1024';self.input_base.mkdir()
        self.color=self.color.resize((1024,1024),Image.Resampling.NEAREST);self.mask=self.mask.resize((1024,1024),Image.Resampling.NEAREST)
        for name in ('color','mask'):
            getattr(self,name).save(self.input_base/(name+'.png'))
            subprocess.run([self.ffmpeg,'-v','error','-loop','1','-i',str(self.input_base/(name+'.png')),'-frames:v','81','-r','24','-c:v','libx264rgb','-crf','0','-preset','ultrafast','-y',str(self.input_base/(name+'.mp4'))],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.fixture()

    def test_final_1024_input_through_real_consumer_bridge_transport(self):
        self.fixture_1024();manifest=self.ready_to_run()
        config=json.loads((Path(manifest).parent/'attempt-01/primary/config.json').read_bytes());self.assertEqual('720p',config['input']['resolution'])
        result=regional.run(manifest,transport=self.node_transport,poll_seconds=0)
        self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',result['state'],result)
        _,_,c=regional.validate_regional_attempt(manifest,require_artifacts=False)
        self.assertEqual(1024,c['plan']['output_policy']['geometry']['scale_denominator']);self.assertEqual(1,sum(v['posts'] for v in self.transport_calls))

    def test_final_declared_and_video_mask_dimensions_must_match_real_bytes(self):
        self.fixture_1024();media=json.loads(self.media.read_bytes());original=encoded(media)
        media['window'].update(width=1440,height=1440);media['geometry']['scale_denominator']=1440;self.update_media(media)
        self.assertEqual('REGIONAL_DIMENSIONS_INVALID',self.prepare()['block_reason'])
        media=json.loads(original);media['mask']=self.ref(self.base/'mask.mp4');self.update_media(media)
        with patch.object(ledger,'bind_run',wraps=ledger.bind_run) as bind:
            self.assertEqual('REGIONAL_DIMENSIONS_INVALID',self.prepare()['block_reason']);bind.assert_not_called()

    def ledger_known_fixture(self,stale=False):
        manifest=self.ready_to_run();regional.run(manifest,transport=lambda *_:dict(state='KEY_SETUP_REQUIRED'),poll_seconds=0)
        _,_,c=regional.validate_regional_attempt(manifest,require_artifacts=False);op=c['ledger']['operations'][c['attempt']['attempt_id']+':primary']
        expected=dict(schema='sprite_regional_submission_expected_v1',config_sha256=op['execution_binding']['config_sha256'],binding_sha256=op['binding_sha256'],**op['current_quote'],regional_state_sha256=c['current']['regional_state_sha256'])
        ledger.mark_submission(self.task,op['operation_id'],expected=expected)
        self.expected_recovery_id='ledger-known-regional'
        ledger.record_outcome(self.task,op['operation_id'],dict(status='accepted',provider_request_id=self.expected_recovery_id,evidence='temporary offline known-ID recovery fixture'))
        directory=c['attempt_dir']/'primary'
        if stale:
            receipt=op['current_quote']['quote_sha256'];created=regional.datetime.now(regional.timezone.utc).isoformat()
            (directory/'submission.claim').write_bytes(encoded(dict(schema_version=3,config_sha256=op['execution_binding']['config_sha256'],pricing_receipt_sha256=receipt,created_at=created)))
            (directory/'job-state.json').write_bytes(encoded(dict(schema_version=3,state='SUBMITTING',request_id=None,config_sha256=op['execution_binding']['config_sha256'],pricing_receipt_sha256=receipt,raw_path=None,raw_sha256=None)))
        return manifest,op

    def assert_ledger_known_recovery(self,stale):
        manifest,op=self.ledger_known_fixture(stale);before=ledger.inspect_task(self.task)
        events=Path(self.task).parent/'events';markers=sum(json.loads(p.read_bytes())['kind']=='mark_submission' for p in events.iterdir())
        quota_events={kind:sum(json.loads(p.read_bytes())['kind']==kind for p in events.iterdir()) for kind in ('begin_attempt','reserve_plan','bind_operation_config')}
        result=regional.resume(manifest,transport=self.node_transport,poll_seconds=0)
        self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',result['state'],result)
        self.assertEqual(0,sum(v['posts'] for v in self.transport_calls));self.assertGreaterEqual(sum(v['gets'] for v in self.transport_calls),3)
        _,_,c=regional.validate_regional_attempt(manifest,require_artifacts=False);state=regional._validate_job(c,'primary')['state']
        self.assertEqual('DOWNLOADED',state['state']);self.assertEqual(self.expected_recovery_id,state['request_id'])
        self.assertEqual(op['current_quote']['quote_sha256'] if stale else None,state.get('pricing_receipt_sha256'))
        self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',regional.inspect(manifest)['state'])
        after=ledger.inspect_task(self.task);self.assertEqual(before['reserved_total_usd'],after['reserved_total_usd']);self.assertEqual(before['strategies'],after['strategies']);self.assertEqual(1,len(after['attempts']))
        self.assertEqual(markers,sum(json.loads(p.read_bytes())['kind']=='mark_submission' for p in events.iterdir()))
        self.assertEqual(quota_events,{kind:sum(json.loads(p.read_bytes())['kind']==kind for p in events.iterdir()) for kind in quota_events})
        raw=Path(state['raw_path']);raw.write_bytes(raw.read_bytes()+b'tampered')
        self.assertEqual('REGIONAL_RAW_CHANGED',regional.inspect(manifest)['block_reason'])

    def test_final_ledger_known_absent_node_mirror_get_only_to_validated_raw(self):self.assert_ledger_known_recovery(False)

    def test_final_ledger_known_submitting_mirror_get_only_to_validated_raw(self):self.assert_ledger_known_recovery(True)

    def test_final_ledger_known_present_identity_receipt_claim_conflicts_block(self):
        manifest,op=self.ledger_known_fixture(True);directory=Path(manifest).parent/'attempt-01/primary'
        state=directory/'job-state.json';claim=directory/'submission.claim';state_bytes=state.read_bytes();claim_bytes=claim.read_bytes()
        for target,key,value in [(state,'request_id','different-id'),(state,'config_sha256','e'*64),(state,'pricing_receipt_sha256','e'*64),(claim,'config_sha256','e'*64),(claim,'pricing_receipt_sha256','e'*64)]:
            with self.subTest(target=target.name,key=key):
                payload=json.loads(target.read_bytes());payload[key]=value;target.write_bytes(encoded(payload));calls=[]
                result=regional.resume(manifest,transport=lambda *args:calls.append(args),poll_seconds=0)
                self.assertEqual('TASK_CONTEXT_CONFLICT',result['block_reason'],result);self.assertEqual([],calls)
                state.write_bytes(state_bytes);claim.write_bytes(claim_bytes)

    def test_changed_history_evidence_blocks_even_with_refreshed_document(self):
        self.write('sidecar.json',{'tampered':True})
        r=self.prepare();self.assertEqual('BLOCKED',r['state']);self.assertEqual({},ledger.inspect_task(self.task)['attempts'])

    def test_all_history_refs_verified_even_when_not_repeated_in_provenance_list(self):
        self.additional_history_evidence=True;self.fixture()
        result=self.prepare();self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',result['state'],result)
        self.write('extra-history.json',dict(tampered=True))
        self.assertEqual('REGIONAL_EVIDENCE_CHANGED',self.prepare()['block_reason'])

    def replace_history_raw_claim(self,*,forge_parent):
        p=json.loads(self.provenance.read_bytes());parent=json.loads(Path(p['parent_manifest']['path']).read_bytes())
        raw=self.base/'mask.mp4'
        if forge_parent:
            parent['attempts'][0]['jobs']['primary'].update(raw_path=str(raw),raw_sha256=sha_file(raw))
            p['parent_manifest']=self.ref(self.write('forged-parent.json',parent))
        p['raw']=self.ref(raw);self.provenance.write_bytes(encoded(p))
        media=json.loads(self.media.read_bytes());media['video']=self.ref(raw);rgb=hashlib.sha256(self.mask.tobytes()).hexdigest()
        for row in media['frames']:row.update(source_rgb_sha256=rgb,video_rgb_sha256=rgb)
        pre=json.loads(self.preflight.read_bytes());pre['provenance_sha256']=sha_file(self.provenance);self.update_media(media,pre)

    def test_fix1_unsealed_forged_parent_other_raw_rejected_before_any_registration(self):
        sealed=self.root/'historical/run-manifest.json';before=sealed.read_bytes();self.replace_history_raw_claim(forge_parent=True)
        with patch.object(ledger,'bind_run',wraps=ledger.bind_run) as bind,patch.object(ledger,'begin_attempt',wraps=ledger.begin_attempt) as begin,patch.object(ledger,'reserve_plan',wraps=ledger.reserve_plan) as reserve,patch.object(ledger,'mark_submission',wraps=ledger.mark_submission) as marker:
            result=self.prepare();self.assertEqual('REGIONAL_HISTORY_MISMATCH',result['block_reason'],result)
            for call in (bind,begin,reserve,marker):call.assert_not_called()
        self.assertEqual(before,sealed.read_bytes());task=ledger.inspect_task(self.task)
        self.assertEqual({},task['runs']);self.assertEqual({},task['attempts']);self.assertEqual({},task['operations'])

    def test_fix1_historical_attempt_index_selects_authenticated_attempt_not_last(self):
        self.extra_parent_attempt=True;self.fixture()
        positive=self.prepare();self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',positive['state'],positive)
        self.replace_history_raw_claim(forge_parent=False)
        with patch.object(ledger,'bind_run',wraps=ledger.bind_run) as bind:
            result=self.prepare();self.assertEqual('REGIONAL_HISTORY_MISMATCH',result['block_reason'],result);bind.assert_not_called()

    def test_fix1_identical_content_parent_and_raw_copies_remain_legitimate(self):
        p=json.loads(self.provenance.read_bytes())
        parent_copy=self.root/'identical-parent.json';parent_copy.write_bytes(Path(p['parent_manifest']['path']).read_bytes())
        raw_copy=self.root/'identical-raw.mp4';raw_copy.write_bytes(Path(p['raw']['path']).read_bytes())
        p['parent_manifest']=self.ref(parent_copy);p['raw']=self.ref(raw_copy);self.provenance.write_bytes(encoded(p))
        pre=json.loads(self.preflight.read_bytes());pre['provenance_sha256']=sha_file(self.provenance);self.preflight.write_bytes(encoded(pre))
        result=self.prepare();self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',result['state'],result)
        self.assertEqual({},ledger.inspect_task(self.task)['attempts'])

    def test_media_pts_count_and_rgb_claims_cannot_authorize(self):
        original=self.media.read_bytes()
        for field,value in [('frames',json.loads(original)['frames'][:-1]),('window',dict(start=0,count=81,fps=25,width=1440,height=1440))]:
            d=json.loads(original);d[field]=value;self.media.write_bytes(encoded(d))
            self.assertEqual('BLOCKED',self.prepare()['state'])
        self.media.write_bytes(original)
        d=json.loads(original);d['frames'][13]['pts']=dict(num=1,den=1);self.media.write_bytes(encoded(d))
        self.assertEqual('BLOCKED',self.prepare()['state'])

    def test_fix1_decode_reuses_probe_only_for_identical_physical_bytes(self):
        raw=self.root/'probe-copy.mp4';raw.write_bytes((self.base/'result.mp4').read_bytes())
        with patch.object(regional,'_run_ffmpeg',wraps=regional._run_ffmpeg) as probe:
            first=regional.decode(raw,self.ffmpeg)
            self.assertEqual(first,regional.decode(raw,self.ffmpeg))
            self.assertEqual(1,probe.call_count)
            raw.write_bytes(raw.read_bytes()+b'changed physical bytes')
            regional.decode(raw,self.ffmpeg)
            self.assertEqual(2,probe.call_count)

    def test_fix1_sampling_reuses_identical_bytes_but_rechecks_changed_artifacts(self):
        directory=self.root/'sampling-check';directory.mkdir()
        media=json.loads(self.media.read_bytes());raw=self.base/'result.mp4'
        c=dict(attempt_dir=directory,attempt={},ffmpeg=self.ffmpeg,media=media,plan_sha256='b'*64,plan=dict(output_policy=dict(geometry=media['geometry'])))
        job=dict(state=dict(state='DOWNLOADED',raw_path=str(raw)),operation=dict(outcomes=[dict(status='completed')]))
        with patch.object(regional,'_validate_job',return_value=job):
            regional._prepare_raw(c)
            with patch.object(regional,'image',wraps=regional.image) as opened:
                regional._validate_sampling(c);first=opened.call_count
                regional._validate_sampling(c)
                self.assertEqual(8,first);self.assertEqual(first,opened.call_count)
            frame=Path(c['attempt']['sampling']['frame_paths'][0]);original=frame.read_bytes()
            Image.new('RGB',(16,16),(255,0,0)).save(frame)
            with self.assertRaisesRegex(WorkflowError,'REGIONAL_SELECTION_CHANGED'):regional._validate_sampling(c)
            frame.write_bytes(original)
            proof=directory/'result-transform.json';original=proof.read_bytes();proof.write_bytes(original+b'changed')
            with self.assertRaisesRegex(WorkflowError,'REGIONAL_TRANSFORM_CHANGED'):regional._validate_sampling(c)

    def update_media(self,media,pre=None):
        self.media.write_bytes(encoded(media));value=json.loads(self.preflight.read_bytes()) if pre is None else pre
        value['media_sha256']=sha_file(self.media);self.preflight.write_bytes(encoded(value))

    def encode_mask(self,name,frames):
        directory=self.root/name;directory.mkdir();source=self.base/'mask.png'
        for i,frame in enumerate(frames):
            filename=directory/f'{i:03d}.png'
            if frame is None:filename.write_bytes(source.read_bytes())
            else:frame.save(filename)
        out=self.root/(name+'.mp4')
        subprocess.run([self.ffmpeg,'-v','error','-framerate','24','-i',str(directory/'%03d.png'),'-frames:v','81','-c:v','libx264rgb','-crf','0','-preset','ultrafast','-y',str(out)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        return out

    def test_black_individual_mask_is_valid_but_empty_or_uncovered_defect_is_not(self):
        black=Image.new('RGB',(1440,1440));file=self.encode_mask('first-black',[black]+[None]*80)
        media=json.loads(self.media.read_bytes());pre=json.loads(self.preflight.read_bytes());sha=hashlib.sha256(black.tobytes()).hexdigest()
        media['mask']=self.ref(file);media['frames'][0]['mask_rgb_sha256']=sha;pre['frame_findings'][0].update(mask_sha256=sha,eligible=False)
        self.update_media(media,pre);self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',self.prepare()['state'])
        # Exact zero-based source numbering: frame zero is selected but cannot
        # be called a covered defect when its physically decoded mask is black.
        pre=json.loads(self.preflight.read_bytes());pre['defect_frames']=[0];self.update_media(media,pre)
        self.assertEqual('REGIONAL_DEFECT_UNCOVERED',self.prepare()['block_reason'])
        empty=self.encode_mask('empty',[black]*81);media['mask']=self.ref(empty)
        for row,finding in zip(media['frames'],pre['frame_findings']):row['mask_rgb_sha256']=sha;finding.update(mask_sha256=sha,eligible=False)
        self.update_media(media,pre);self.assertEqual('REGIONAL_DEFECT_UNCOVERED',self.prepare()['block_reason'])

    def test_inverted_mask_and_changed_rgb_hashes_cannot_reuse_review(self):
        from PIL import ImageOps
        inverse=ImageOps.invert(self.mask);file=self.encode_mask('inverse',[inverse]*81)
        media=json.loads(self.media.read_bytes());media['mask']=self.ref(file);self.update_media(media)
        self.assertEqual('REGIONAL_FRAME_EVIDENCE_MISMATCH',self.prepare()['block_reason'])
        media=json.loads(self.media.read_bytes());media['frames'][13]['video_rgb_sha256']='0'*64;self.update_media(media)
        self.assertEqual('BLOCKED',self.prepare()['state'])

    def test_genuine_vfr_rate_and_count_fail_before_any_ledger_attempt(self):
        for name,vf,frames in [('rate','setpts=N/(25*TB)',81),('vfr','setpts=(N+gte(N\\,40))/(24*TB)',81),('count','null',80)]:
            out=self.root/(name+'.mp4')
            subprocess.run([self.ffmpeg,'-v','error','-i',str(self.base/'result.mp4'),'-vf',vf,'-fps_mode','passthrough','-frames:v',str(frames),'-c:v','libx264rgb','-crf','0','-y',str(out)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            with self.subTest(name=name):
                with self.assertRaises(WorkflowError):regional.decode(out,self.ffmpeg)
        self.assertEqual({},ledger.inspect_task(self.task)['attempts'])

    def test_preflight_checks_and_defects_are_bound_to_real_frame_numbers(self):
        media=json.loads(self.media.read_bytes());pre=json.loads(self.preflight.read_bytes());pre['defect_frames']=[80]
        self.update_media(media,pre);self.assertEqual('REGIONAL_DEFECT_UNCOVERED',self.prepare()['block_reason'])
        pre['defect_frames']=[19];pre['frame_findings'][19]['mask_sha256']=sha_file(self.base/'mask.png');self.update_media(media,pre)
        self.assertEqual('REGIONAL_PREFLIGHT_INVALID',self.prepare()['block_reason'])

    def test_crop_with_subject_and_changed_cleanup_dimensions_block(self):
        directory=self.root/'assembly';directory.mkdir()
        transform=dict(crop=[0,4,32,24],resize_canvas=[32,32],scale_numerator=32,scale_denominator=16)
        (directory/'result-transform.json').write_bytes(encoded(transform))
        output=self.root/'bad-cleanup.png';rgba=Image.new('RGBA',(64,32));rgba.paste((90,60,30,255),(0,0,4,8));rgba.save(output)
        c=dict(attempt_dir=directory,manifest=dict(request={}))
        sampling=dict(selection=dict(result_canvas=[16,16]));job=dict(state=dict(state='DOWNLOADED',raw_path=str(output)),operation=dict(outcomes=[dict(status='completed')]))
        with patch.object(regional,'_gate'),patch.object(regional,'_validate_sampling',return_value=sampling),patch.object(regional,'_validate_job',return_value=job):
            with self.assertRaisesRegex(WorkflowError,'REGIONAL_CROP_SUBJECT_CLIPPED'):regional._assembly_values(c)
            Image.new('RGBA',(65,32),(90,60,30,255)).save(output)
            with self.assertRaisesRegex(WorkflowError,'REGIONAL_CLEANUP_DIMENSIONS_CHANGED'):regional._assembly_values(c)

    def test_incomplete_quote_never_consumes_attempt_or_reservation(self):
        prepared=self.prepare();manifest=prepared['run_manifest'];self.transport_calls=[]
        def transport(command,directory):
            if command=='pricing-plan':return dict(state='BLOCKED',block_reason='PRICE_NOT_VERIFIABLE')
            return self.node_transport(command,directory)
        priced=regional.quote(manifest,transport=transport)
        self.assertEqual('PRICE_NOT_VERIFIABLE',priced['block_reason'],priced)
        task=ledger.inspect_task(self.task);self.assertEqual({},task['attempts']);self.assertEqual({},task['operations']);self.assertEqual(0,sum(c['posts'] for c in self.transport_calls))

    def ready_to_run(self):
        prepared=self.prepare();manifest=prepared['run_manifest'];self.transport_calls=[]
        self.assertEqual('REGIONAL_QUOTED',regional.quote(manifest,transport=self.node_transport)['state'])
        self.assertEqual('REGIONAL_GATE_RECORDED',regional.record_gate(manifest,str(self.gate(manifest,'input_ready')))['state'])
        return manifest

    def test_history_changed_after_prepare_blocks_before_marker(self):
        manifest=self.ready_to_run();sidecar=self.root/'sidecar.json';original=sidecar.read_bytes()
        def changed(command,directory):
            if command=='execute':sidecar.write_bytes(original+b'changed after prepare')
            return self.node_transport(command,directory)
        result=regional.run(manifest,transport=changed,poll_seconds=0)
        self.assertEqual('REGIONAL_EVIDENCE_CHANGED',result['block_reason'],result)
        task=ledger.inspect_task(self.task);self.assertEqual(1,len(task['attempts']))
        self.assertFalse(any(v['submission_started'] for v in task['operations'].values()))
        self.assertEqual(0,sum(c['posts'] for c in self.transport_calls))

    def test_physical_history_is_rechecked_between_begin_and_reserve(self):
        manifest=self.ready_to_run();begin=ledger.begin_attempt;sidecar=self.root/'sidecar.json'
        def changing(*args,**kwargs):
            result=begin(*args,**kwargs);sidecar.write_bytes(sidecar.read_bytes()+b'changed between transactions');return result
        with patch.object(ledger,'begin_attempt',side_effect=changing):
            result=regional.run(manifest,transport=self.node_transport,poll_seconds=0)
        self.assertEqual('REGIONAL_EVIDENCE_CHANGED',result['block_reason'],result)
        task=ledger.inspect_task(self.task);self.assertEqual(1,len(task['attempts']));self.assertEqual({},task['operations'])
        self.assertEqual(0,sum(c['posts'] for c in self.transport_calls))

    def test_fix1_bridge_rechecks_after_validation_and_expired_quote_recovers(self):
        manifest=self.ready_to_run()
        regional.run(manifest,transport=lambda *_:dict(state='KEY_SETUP_REQUIRED'),poll_seconds=0)
        _,_,c=regional.validate_regional_attempt(manifest,require_artifacts=False)
        op=c['ledger']['operations'][c['attempt']['attempt_id']+':primary'];binding=op['execution_binding']
        message=dict(schema='sprite_regional_before_submit_v1',job_dir=str(c['attempt_dir']/'primary'),**{k:binding[k] for k in ('config_sha256','config_file_sha256','input_set_sha256')},**op['current_quote'],regional_state_sha256=c['current']['regional_state_sha256'])
        real_datetime=regional.datetime
        class ExpiredClock:
            fromisoformat=staticmethod(real_datetime.fromisoformat)
            @staticmethod
            def now(tz):return real_datetime.now(tz)+regional.timedelta(minutes=11)
        with patch.object(regional,'datetime',ExpiredClock),patch.object(ledger,'mark_submission',wraps=ledger.mark_submission) as marker:
            with self.assertRaisesRegex(WorkflowError,'PRICE_STALE'):regional.regional_bridge_run(message,True)
            marker.assert_not_called()
        sidecar=self.root/'sidecar.json';original=sidecar.read_bytes();validate_quote=regional._validate_quote
        def change_after_earlier_validation(*args,**kwargs):
            result=validate_quote(*args,**kwargs);sidecar.write_bytes(original+b'changed after coherent validation');return result
        with patch.object(regional,'_validate_quote',side_effect=change_after_earlier_validation),patch.object(ledger,'mark_submission',wraps=ledger.mark_submission) as marker:
            with self.assertRaisesRegex(WorkflowError,'REGIONAL_EVIDENCE_CHANGED'):regional.regional_bridge_run(message,True)
            marker.assert_not_called()
        sidecar.write_bytes(original)
        self.assertFalse(any(o['submission_started'] for o in ledger.inspect_task(self.task)['operations'].values()))
        recovered=regional.run(manifest,transport=self.node_transport,poll_seconds=0)
        self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',recovered['state'],recovered)
        self.assertEqual(1,sum(call['posts'] for call in self.transport_calls))

    def test_unknown_marker_repeated_resume_never_posts_again(self):
        manifest=self.ready_to_run();self.transport_mode='unknown'
        first=regional.run(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual('SUBMISSION_STATUS_UNKNOWN',first['state'],first)
        second=regional.resume(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual('SUBMISSION_STATUS_UNKNOWN',second['state'],second)
        self.assertEqual(1,sum(c['posts'] for c in self.transport_calls));task=ledger.inspect_task(self.task)
        self.assertEqual('0.67',task['reserved_total_usd']);self.assertEqual(1,len(task['attempts']))
        primary=next(v for v in task['operations'].values() if v['kind']=='provider_animation');self.assertTrue(primary['submission_started']);self.assertIsNone(primary['provider_request_id'])

    def test_quote_refresh_retains_held_reservation_and_same_attempt(self):
        self.task_cap='0.67';self.fixture()
        manifest=self.ready_to_run()
        def no_submit(command,directory):return dict(state='KEY_SETUP_REQUIRED',block_reason='FAL_KEY_MISSING')
        blocked=regional.run(manifest,transport=no_submit,poll_seconds=0);self.assertEqual('KEY_SETUP_REQUIRED',blocked['state'],blocked)
        task=ledger.inspect_task(self.task);before={k:v['current_quote'] for k,v in task['operations'].items()}
        quoted=regional.quote(manifest,transport=self.node_transport);self.assertEqual('REGIONAL_QUOTED',quoted['state'],quoted)
        task=ledger.inspect_task(self.task);self.assertEqual(1,len(task['attempts']));self.assertEqual('0.67',task['reserved_total_usd']);self.assertEqual('0',task['available_estimate_usd'])
        self.assertTrue(all(v['current_quote']!=before[k] for k,v in task['operations'].items()))
        self.assertEqual(0,sum(c['posts'] for c in self.transport_calls))

    def test_positive_reprice_delta_fits_live_remaining_or_fails_atomically(self):
        manifest=self.ready_to_run()
        regional.run(manifest,transport=lambda *_:dict(state='KEY_SETUP_REQUIRED'),poll_seconds=0)
        self.primary_price='0.10';raised=regional.quote(manifest,transport=self.node_transport);self.assertEqual('REGIONAL_QUOTED',raised['state'],raised)
        task=ledger.inspect_task(self.task);self.assertEqual('0.81',task['reserved_total_usd']);operations=task['operations'];self.assertEqual('0.19',task['available_estimate_usd'])
        self.primary_price='0.14';denied=regional.quote(manifest,transport=self.node_transport);self.assertEqual('TASK_BUDGET_EXCEEDED',denied['block_reason'],denied)
        task=ledger.inspect_task(self.task);self.assertEqual(operations,task['operations']);self.assertEqual('0.81',task['reserved_total_usd']);self.assertEqual(1,len(task['attempts']))
        self.assertEqual(0,sum(c['posts'] for c in self.transport_calls))

    def test_source_window_start_zero_selects_exact_81_from_longer_raw(self):
        out=self.root/'long.mp4'
        subprocess.run([self.ffmpeg,'-v','error','-stream_loop','-1','-i',str(self.base/'result.mp4'),'-frames:v','100','-c:v','libx264rgb','-crf','0','-y',str(out)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        decoded=regional.decode(out,self.ffmpeg,start=0)
        self.assertEqual(81,decoded['frame_count'])
        with self.assertRaisesRegex(WorkflowError,'REGIONAL_FRAME_COUNT_INVALID'):regional.decode(out,self.ffmpeg)

    def test_ordinal_sampler_preserves_13_and_derives_result_width(self):
        media=json.loads(self.media.read_bytes());out=regional.sample_regional_video(str(self.base/'result.mp4'),str(self.root/'sample'),ffmpeg_exe=self.ffmpeg,media=media)
        self.assertEqual([0,3,6,9,13,16,19,22],out['selection']['indices'])
        self.assertEqual([16,16],out['selection']['result_canvas'])
        self.assertEqual(81,out['selection']['frame_count'])

    def test_public_generic_resume_and_revise_cannot_drive_regional(self):
        import generic_sprite_workflow as generic
        r=self.prepare();self.assertIsNotNone(r['run_manifest'],r)
        calls=[]
        for fn,args in [(generic.resume,()),(generic.revise,('new experiment',))]:
            out=fn(r['run_manifest'],*args,transport=lambda *a:calls.append(a))
            self.assertEqual('REGIONAL_DRIVER_REQUIRED',out['block_reason'],out)
        self.assertEqual([],calls)

    def gate(self,manifest,stage):
        _,m,c=regional.validate_regional_attempt(manifest,require_artifacts=False)
        evidence=self.write(stage+'-review.txt.json',{'review':'A human inspected the concrete frames, masks, crop and stop.'})
        report=dict(schema='sprite_regional_gate_v1',stage=stage,decision='pass',subject=regional._subject(c,stage),evidence=self.ref(evidence),findings=[dict(check=k,passed=True,notes='Reviewed concrete visual evidence for this exact artifact set.') for k in sorted(regional.INPUT_CHECKS if stage=='input_ready' else regional.RAW_CHECKS)])
        return self.write(stage+'-gate.json',report)

    def node_transport(self,command,directory):
        """Real Node transport and its default subprocess Python bridge; no net."""
        import generic_sprite_workflow as generic
        directory=Path(directory);role=directory.name
        cleanup=self.root/'cleanup-output.png'
        if role=='cleanup' and command=='execute':
            from PIL import ImageChops
            rgb=Image.open(directory.parent/'sampling/sample-grid.png').convert('RGB');difference=ImageChops.difference(rgb,Image.new('RGB',rgb.size,(40,60,80)))
            red,green,blue=difference.split();alpha=ImageChops.lighter(ImageChops.lighter(red,green),blue).point(lambda value:255 if value else 0)
            grid=rgb.convert('RGBA');grid.putalpha(alpha);grid.save(cleanup)
        data=dict(command=command,job=str(directory),module=Path(__file__).with_name('fal_sheet_transport.mjs').as_uri(),vace=base64.b64encode(zlib.decompress(base64.b64decode(VACE_SCHEMA_Z))).decode(),cleanup=base64.b64encode(zlib.decompress(base64.b64decode(CLEANUP_SCHEMA_Z))).decode(),output=str(getattr(self,'result_raw',self.base/'result.mp4')) if role=='primary' else str(cleanup),mode=getattr(self,'transport_mode','normal'),price=getattr(self,'primary_price','0.08'),diagnostic_bridge=os.environ.get('REGIONAL_BRIDGE_DIAGNOSTICS')=='1',diagnostic_timeout_ms=int(os.environ.get('REGIONAL_BRIDGE_DIAGNOSTIC_TIMEOUT_MS','15000')),python=sys.executable,bridge=str(Path(__file__).with_name('sprite_task_bridge.py')))
        data['recovery_id']=getattr(self,'expected_recovery_id',None)
        script=r"""
import fs from 'node:fs';import path from 'node:path';import{spawnSync}from'node:child_process';
const x=JSON.parse(fs.readFileSync(0,'utf8'));const {runCommand}=await import(x.module);
const model=x.job.endsWith('primary')?'fal-ai/wan-vace-14b/inpainting':'fal-ai/ideogram/remove-background';
let posts=0,gets=0;const bridges=[];const json=v=>new Response(JSON.stringify(v));
const options={key:'fake-local-only',fetchImpl:async(url,init)=>{
 const u=String(url);if(x.recovery_id && u.startsWith('https://queue.fal.run/') && !u.includes('/requests/'+x.recovery_id))throw Error('wrong recovery request identity');if(init.method==='POST'){
  posts++;const input=JSON.parse(init.body);if(model.includes('vace') && (!input.video_url.startsWith('data:video/mp4;base64,') || !input.mask_video_url.startsWith('data:video/mp4;base64,') || input.ref_image_urls.length!==1))throw Error('input contract');
  if(x.mode==='unknown')return new Response('{}',{status:503});return json({request_id:'regional-'+path.basename(x.job)});
 }gets++;
 if(u.startsWith('https://api.fal.ai/'))return json({prices:[{endpoint_id:model,currency:'USD',unit:model.includes('vace')?'seconds':'images',unit_price:model.includes('vace')?x.price:'0.01'}]});
 if(u.startsWith('https://fal.ai/api/openapi/'))return new Response(Buffer.from(model.includes('vace')?x.vace:x.cleanup,'base64'));
 if(u.startsWith('https://fal.ai/models/'))return new Response(`<html><head><link rel="canonical" href="https://fal.ai/models/${model}"></head><body><p>Your request will cost $${x.price} per video second for 720p. Video seconds are calculated at 16 frames per second.</p></body></html>`);
 if(u.endsWith('/status?logs=0'))return json({status:'COMPLETED'});
 if(u.includes('/requests/'))return json(model.includes('vace')?{video:{url:'https://fal.media/output.mp4'}}:{image:{url:'https://fal.media/output.png'}});
 if(u.startsWith('https://fal.media/'))return new Response(fs.readFileSync(x.output),{headers:{'content-type':model.includes('vace')?'video/mp4':'image/png'}});
 throw Error('Unmocked network: '+u);
}};
if(x.diagnostic_bridge)options.taskBridge=async(command,payload)=>{
 const env={};for(const k of ['SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP'])if(process.env[k]!==undefined)env[k]=process.env[k];env.PYTHONDONTWRITEBYTECODE='1';
 const start=Date.now();const p=spawnSync(x.python,['-B',x.bridge,command],{input:JSON.stringify(payload),encoding:'utf8',env,shell:false,windowsHide:true,timeout:x.diagnostic_timeout_ms,maxBuffer:65536});
 bridges.push({command,elapsed_ms:Date.now()-start,status:p.status,signal:p.signal,error:p.error?.code??null,stderr:p.stderr,stdout:p.stdout});
 if(p.error || p.signal || ![0,2].includes(p.status))return {state:'BLOCKED',block_reason:'TASK_BRIDGE_FAILED'};
 return JSON.parse(p.stdout);
};
const result=await runCommand(x.command,x.job,options);process.stdout.write(JSON.stringify({result,posts,gets,bridges}));
"""
        env={k:os.environ[k] for k in ('SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP','PATH') if k in os.environ}
        proc=subprocess.run([generic._node_exe(),'--input-type=module','-e',script],input=encoded(data),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(0,proc.returncode,proc.stderr.decode('utf-8','replace'));out=json.loads(proc.stdout)
        self.transport_calls.append(dict(command=command,role=role,**out))
        if out.get('bridges'):print('BRIDGE_DIAGNOSTIC '+json.dumps(dict(role=role,bridges=out['bridges'])),flush=True)
        if command=='execute' and getattr(self,'transport_crash_once',False):
            self.transport_crash_once=False
            raise WorkflowError('TEST_CRASH_AFTER_DURABLE_ID')
        return out['result']

    def test_public_full_flow_real_node_bridge_ledger_and_review_export(self):
        import generic_sprite_workflow as generic
        from sprite_alignment_workflow import align_manifest
        r=self.prepare();self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',r['state'],r);manifest=r['run_manifest'];self.transport_calls=[]
        self.assertEqual('REGIONAL_INPUT_REVIEW_REQUIRED',regional.run(manifest,transport=self.node_transport,poll_seconds=0)['state']);self.assertEqual([],self.transport_calls)
        priced=regional.quote(manifest,transport=self.node_transport);self.assertEqual('REGIONAL_QUOTED',priced['state'],priced)
        self.assertEqual({},ledger.inspect_task(self.task)['attempts'])
        self.assertEqual('REGIONAL_GATE_RECORDED',regional.record_gate(manifest,str(self.gate(manifest,'input_ready')))['state'])
        self.transport_crash_once=True
        crashed=regional.run(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual('TEST_CRASH_AFTER_DURABLE_ID',crashed['block_reason'],crashed)
        raw=regional.resume(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',raw['state'],raw)
        self.assertEqual(1,sum(c['posts'] for c in self.transport_calls));self.assertFalse((Path(manifest).parent/'attempt-01/cleanup/config.json').exists())
        # A known durable ID remains visible if old physical history changes.
        historical=self.root/'sidecar.json';old=historical.read_bytes();historical.write_bytes(old+b'changed')
        retained=self.node_transport('execute',Path(manifest).parent/'attempt-01/primary')
        self.assertEqual('BLOCKED',retained['state'],retained);self.assertEqual('regional-primary',retained['request_id']);historical.write_bytes(old)
        no_gate=regional.resume(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',no_gate['state'],no_gate)
        self.assertEqual('REGIONAL_GATE_RECORDED',regional.record_gate(manifest,str(self.gate(manifest,'raw_ready')))['state'])
        # Substitution cannot borrow a review of another RAW or selection.
        rawpath=Path(manifest).parent/'attempt-01/primary/raw.mp4';before=rawpath.read_bytes();rawpath.write_bytes(before+b'changed')
        self.assertEqual('REGIONAL_RAW_CHANGED',regional.inspect(manifest)['block_reason']);rawpath.write_bytes(before)
        selectionpath=Path(manifest).parent/'attempt-01/sampling/selection.json';before=selectionpath.read_bytes();selection=json.loads(before);selection['indices'][4]=12;selectionpath.write_bytes(encoded(selection))
        self.assertEqual('REGIONAL_SELECTION_CHANGED',regional.inspect(manifest)['block_reason']);selectionpath.write_bytes(before)
        assembled=regional.resume(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual(generic.READY,assembled['state'],assembled)
        self.assertEqual(2,sum(c['posts'] for c in self.transport_calls))
        m=json.loads(Path(manifest).read_bytes());transform=json.loads((Path(manifest).parent/'attempt-01/result-transform.json').read_bytes())
        self.assertEqual(getattr(self,'expected_result_width',16),transform['scale_denominator']);self.assertEqual(1440,json.loads((Path(manifest).parent/'regional-plan.json').read_bytes())['output_policy']['geometry']['scale_denominator'])
        self.assertEqual('ALIGNMENT_REQUIRED',generic.export_artifacts(manifest,str(self.root/'unreviewed-export'))['block_reason'])
        anchors=[]
        for i,f in enumerate(m['artifact_paths']['frames'],1):
            im=Image.open(f).convert('RGBA');box=im.getchannel('A').getbbox();anchors.append(dict(frame=i,sha256=sha_file(f),point=[(box[0]+box[2]-1)/2,(box[1]+box[3]-1)/2]))
        annotation=self.write('anchors.json',dict(schema_version=1,artifact_set_sha256=generic._review_target(m),reference=dict(name='body center',description='Homologous torso center excluding weapon and hand.',source_point=[8,8]),frames=anchors))
        aligned=align_manifest(manifest,str(annotation),output_root=str(self.root/'aligned'))
        self.assertEqual(generic.READY,aligned['state'],aligned);am=aligned['run_manifest'];av=json.loads(Path(am).read_bytes())
        self.assertEqual('VISUAL_REVIEW_REQUIRED',generic.export_artifacts(am,str(self.root/'export'))['block_reason'])
        review=dict(artifact_set_sha256=generic._review_target(av),decision='pass',checks={k:dict(status='pass',notes='Human inspection of all concrete frames and closure completed.') for k in generic.VISUAL_CHECKS},scale_anchors=[dict(name=k,notes='Compared unchanged size across every output frame.') for k in ('head','torso')],flagged_frame_findings=[dict(frame=i,notes='Inspected this flagged frame against source and neighbors.') for i in range(1,9)],body_alignment=dict(proof_sha256=av['derivation']['proof_sha256'],reference_name='body center',notes='Fixed horizontal torso center preserves the natural vertical bounce.',frame_findings=[dict(frame=i,notes='Homologous torso center was reviewed in this exact frame.',homologous_body_point=True,fixed_x=True,preserved_y=True,excludes_weapon_tail_box=True) for i in range(1,9)]))
        reviewed=generic.record_visual_review(am,str(self.write('visual-review.json',review)));self.assertEqual(generic.DELIVERABLE,reviewed['state'],reviewed)
        exported=generic.export_artifacts(am,str(self.root/'export'));self.assertTrue(exported['delivery_allowed'],exported)
        again=regional.resume(manifest,transport=self.node_transport,poll_seconds=0);self.assertEqual(generic.READY,again['state'],again);self.assertEqual(2,sum(c['posts'] for c in self.transport_calls))
        self.assertEqual('0.67',ledger.inspect_task(self.task)['reserved_total_usd'])
        old=rawpath.read_bytes();rawpath.write_bytes(old+b'changed')
        self.assertEqual('REGIONAL_RAW_CHANGED',generic.export_artifacts(am,str(self.root/'export'))['block_reason']);rawpath.write_bytes(old)
        # Gate revocation after alignment is a separate live-parent failure.
        report=json.loads(self.gate(manifest,'raw_ready').read_bytes());report['decision']='hold'
        held=regional.record_gate(manifest,str(self.write('raw-hold.json',report)));self.assertEqual('REGIONAL_GATE_HOLD',held['state'],held)
        blocked=generic.export_artifacts(am,str(self.root/'export'));self.assertFalse(blocked['delivery_allowed']);self.assertEqual('REGIONAL_RAW_REVIEW_REQUIRED',blocked['block_reason'],blocked)

    def test_public_supported_720_square_runtime(self):
        self.result_raw=self.root/'synthetic-result-720.mp4';self.expected_result_width=720
        subprocess.run([self.ffmpeg,'-v','error','-i',str(self.base/'result.mp4'),'-vf','scale=720:720:flags=neighbor','-fps_mode','passthrough','-c:v','libx264rgb','-crf','0','-preset','ultrafast','-y',str(self.result_raw)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.test_public_full_flow_real_node_bridge_ledger_and_review_export()




# Reviewed official schema bytes, shared snapshot identity with transport tests.
VACE_SCHEMA_Z = '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'
CLEANUP_SCHEMA_Z = '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'


if __name__=='__main__':unittest.main()
