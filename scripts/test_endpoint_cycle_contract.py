"""Closed endpoint route boundary tests; synthetic pixels never establish art quality."""
import importlib
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
from sheet_workflow_state import WorkflowError, encoded, sha_file, scope_sha
import sprite_task_ledger as ledger

SCHEMA_ROOT=Path(os.environ.get('TOWER_SPRITE_ENDPOINT_SCHEMA_DIR', str(Path(__file__).resolve().parents[1]/'assets/fixtures/private/endpoint-schemas')))
NO_MEDIA_RECORDED_BODY=Path(os.environ.get('TOWER_SPRITE_NO_MEDIA_FIXTURE', str(SCHEMA_ROOT.parent/'provider-error-response.body.json')))


class EndpointFixture:
    """Real ledger/files; provider outcomes are explicitly synthetic offline data."""
    def __init__(self,root,*,prepared_compression=6,video_size=64):
        import endpoint_cycle_contract as e
        self.e=e;self.video_size=video_size;self.root=Path(root)/'run1';self.root.mkdir();self.attempt=self.root/'attempt-01';self.attempt.mkdir()
        self.snap=self.root/'snapshots';self.snap.mkdir();self.path=self.root/'run-manifest.json'
        source=Image.new('RGBA',(1254,1254));source.paste((80,120,140,255),(400,200,850,1100))
        source.save(self.root/'source.png');self.source=sha_file(self.root/'source.png')
        proof_inputs=[]
        for role,name,out,index in [('edit','b-candidate-original.png','edit.png',1),('identity','source-original.png','identity.png',0),('endpoint_a','a-original.png','endpoint-a.png',2)]:
            image=source.copy()
            if index:image.putpixel((600+index,300),(130+index,70,100,255))
            image.save(self.snap/name)
            prepared=Image.new('RGB',(2048,2048),(110,110,110));prepared.paste(image,(397,397),image.getchannel('A'));prepared.save(self.snap/out,compress_level=prepared_compression)
            proof_inputs.append(dict(role=role,original=e.ref(self.snap/name),prepared=e.ref(self.snap/out)))
        self.write(self.snap/'preparation-proof.json',dict(schema='sprite_endpoint_preparation_v1',policy='pose_canvas_2048_v1',transform=e.TRANSFORM,inputs=proof_inputs))
        self.task=ledger.create_task(Path(root)/'tasks',dict(task_id='offline-endpoint',cap_usd='2',budget_basis='fal_operational_reservations',allowed_operations=list(ledger.OPERATION_KINDS),evidence_ref='Synthetic offline endpoint boundary'),[dict(target_id='golem',source_sha256=self.source,action='run',orientation='left',requirements=dict(frames=8))])
        self.ref=dict(schema='sprite_task_ref_v1',task_id=self.task['task_id'],task_manifest=self.task['task_manifest'],authorization_sha256=self.task['authorization_sha256'],target_id='golem')
        self.aid='run1:attempt-01'
        for role in e.ROLES:(self.attempt/role).mkdir()
        self.configs={};self.jobs={};self.plan=None
        config=self.make_config('pose_edit')
        self.plan=dict(schema='sprite_endpoint_plan_v1',attempt_id=self.aid,source_sha256=self.source,pose_config_sha256=e.node_config_sha(config),pose_inputs=[dict(role=v['role'],sha256=v['sha256']) for v in config['input_files']],endpoint_a_sha256=sha_file(self.snap/'endpoint-a.png'),preparation_policy='pose_canvas_2048_v1',video_policy=dict(frames=81,fps=24,resolution='720p',aspect_ratio='1:1',steps=30,acceleration='none',prompt_expansion=False),sampling_policy='endpoint_full_range_81_four_phases_v1',cleanup_policy=dict(model=e.MODELS['cleanup'],sync_mode=False,max_bytes=10485760,max_width=4096,max_height=4096),output_policy=dict(frames=8,frame_ms=100,loop=True,canvas=[2048,2048],geometry_policy='common_nearest_2048_v1',alignment_policy='body_x_preserve_y_v1'))
        self.write(self.root/'endpoint-plan.json',self.plan)
        self.descriptor=dict(schema='sprite_strategy_descriptor_v2',route='endpoint_cycle_v1',family='nano2_wan_flf2v_cycle_v1',model=e.MODELS['video_ab'],conditioning='pose_edit_then_opposite_endpoints',inputs=[dict(role='source',sha256=self.source)])
        request=dict(action='run',orientation='left',frames=8,frame_ms=100,loop=True,canvas=[2048,2048],columns=4,rows=2,pixel_art=True,grounded=False,route='endpoint_cycle_v1',strategy='endpoint_cycle_v1',task_ref=self.ref,effective_descriptor=self.descriptor,endpoint_plan_sha256=scope_sha(self.plan),alignment_policy='body_x_preserve_y_v1',ffmpeg_exe=e.resolve_ffmpeg())
        self.scope=dict(run_id='run1',source=dict(path=str(self.root/'source.png'),sha256=self.source,original_path=str(self.root/'source.png')),request=request,authorization=dict(source='session_request',request_authorized=True,max_cost_usd=1.14,max_generation_attempts=2))
        self.write(self.root/'scope.json',self.scope);(self.root/'scope.sha256').write_text(scope_sha(self.scope)+'\n')
        ledger.bind_run(self.task['task_manifest'],dict(run_id='run1',target_id='golem',scope_sha256=scope_sha(self.scope),manifest_path=str(self.path)))
        strategy=ledger.begin_attempt(self.task['task_manifest'],'run1',self.aid,self.descriptor)
        self.manifest=dict(schema_version=3,mode='whole_sheet',**self.scope,authorization_scope_sha256=scope_sha(self.scope),attempts=[dict(index=1,attempt_id=self.aid,strategy_sha256=strategy,correction=None,diagnoses=[],jobs=self.jobs)],state='GENERATING',artifact_paths={},artifact_hashes={})
        self.save()
        self.quotes={}
        for role in e.ROLES:self.quotes[role]=self.quote(role,execution=role=='pose_edit')
        ops=[dict(operation_id=self.aid+':'+role,kind='alpha_edge_cleanup' if role=='cleanup' else 'provider_animation',quote_sha256=self.quotes[role][0],amount_usd=self.quotes[role][1],**(dict(config_sha256=e.node_config_sha(config)) if role=='pose_edit' else dict(deferred_config=dict(schema='sprite_endpoint_deferred_v1',endpoint_plan_sha256=scope_sha(self.plan),role=role)))) for role in e.ROLES]
        ledger.reserve_plan(self.task['task_manifest'],'run1',self.aid,strategy,ops,endpoint_plan=self.plan)
        self.materialize('pose_edit');self.gate('input_ready',dict(endpoint_plan_sha256=scope_sha(self.plan)))

    def write(self,path,value):path.write_bytes(encoded(value))
    def save(self):self.write(self.path,self.manifest)
    def state(self):return ledger.inspect_task(self.task['task_manifest'])
    def current(self):return next(a for a in self.state()['attempts'].values() if a['attempt_id']==self.aid)
    def make_config(self,role):
        e=self.e;a=self.snap/'endpoint-a.png';b=self.attempt/'pose_edit/raw.png'
        paths=[self.snap/'edit.png',self.snap/'identity.png'] if role=='pose_edit' else [self.attempt/'cycle-grid.png'] if role=='cleanup' else [a,b] if role=='video_ab' else [b,a]
        roles=['edit','identity'] if role=='pose_edit' else ['grid'] if role=='cleanup' else ['start','end']
        inputs=[dict(role=r,path=str(p),sha256=sha_file(p)) for r,p in zip(roles,paths)]
        options=dict(sync_mode=False) if role=='cleanup' else dict(prompt='Concrete pose material correction preserving actual body and near-far opposition.',resolution='2K',aspect_ratio='1:1',num_images=1,output_format='png',limit_generations=True,enable_web_search=False,sync_mode=False,safety_tolerance='4') if role=='pose_edit' else dict(prompt='One complete half stride with actual endpoints and unchanged material.',frames=81,fps=24,resolution='720p',aspect_ratio='1:1',steps=30,acceleration='none',prompt_expansion=False,enable_safety_checker=True)
        config=dict(schema_version=4,route='endpoint_cycle_v1',role=role,model=e.MODELS[role],input_files=inputs,input=options,authorization=dict(request_authorized=True,task_context=dict(task_ref=self.ref,run_manifest=str(self.path),run_id='run1',attempt_id=self.aid,operation_id=self.aid+':'+role)),max_cost_usd=1.14)
        self.configs[role]=config;self.write(self.attempt/role/'config.json',config)
        return config

    def quote(self,role,*,execution):
        e=self.e;directory=self.attempt/role/('pricing-receipts' if execution else 'planning-pricing-receipts');directory.mkdir(exist_ok=True)
        rid=str(uuid.uuid4());now=datetime.now(timezone.utc);now=now.replace(microsecond=(now.microsecond//1000)*1000);when=now.isoformat(timespec='milliseconds').replace('+00:00','Z')
        model=e.MODELS[role];unit='videos' if role in ('video_ab','video_ba') else 'images';base='0.08' if role=='pose_edit' else '0.01' if role=='cleanup' else '0.4';amount='0.12' if role=='pose_edit' else '0.01' if role=='cleanup' else '0.4';mult='1.5' if role=='pose_edit' else '1'
        from urllib.parse import urlencode
        sources=[]
        def archive(suffix,payload,url,selected):
            name=rid+'.'+suffix+'.body';(directory/name).write_bytes(payload);sources.append(dict(source_url=url,retrieved_at=when,body_sha256=e.digest(payload),archive_file=name,selected=selected))
        archive('api',encoded(dict(prices=[dict(endpoint_id=model,currency='USD',unit=unit,unit_price=base)])),'https://api.fal.ai/v1/models/pricing?'+urlencode(dict(endpoint_id=model)),dict(endpoint_id=model,currency='USD',unit=unit,unit_price_usd=float(base)))
        if role!='cleanup':
            text='Your request will cost $0.08 per image. 2K and 4K outputs will be charged at 1.5 times and 2 times the standard rate, respectively.' if role=='pose_edit' else 'For a video generation, your request will cost $0.2 at 480p resolution and $0.4 at 720p resolution. with the default frames. More than the default frames will cost 1.25x more.'
            html=('<html><head><link rel="canonical" href="https://fal.ai/models/'+model+'"></head><body><p>'+text+'</p></body></html>').encode()
            archive('public',html,'https://fal.ai/models/'+model,dict(base_unit_price_usd=float(base),resolution='2K',resolution_multiplier=1.5) if role=='pose_edit' else dict(resolution='720p',frames=81,billing_units=1,base_unit_price_usd=float(base)))
        schema=SCHEMA_ROOT/('nano-openapi.json' if role=='pose_edit' else 'cleanup-openapi.json' if role=='cleanup' else 'wan-openapi.json')
        archive('schema',schema.read_bytes(),'https://fal.ai/api/openapi/queue/openapi.json?'+urlencode(dict(endpoint_id=model)),dict(input_schema=e.SCHEMAS[model][0],policy='endpoint_cycle_v1'))
        receipt=dict(receipt_id=rid,model=model,retrieved_at=when,expires_at=(now+timedelta(minutes=15)).isoformat(timespec='milliseconds').replace('+00:00','Z'),currency='USD',api_unit=unit,estimated_cost_usd=float(amount) if execution else amount,reserved_units=1,planning_basis='configured_output_count' if role=='pose_edit' else 'joined_grid_one_image_v1' if role=='cleanup' else 'wan_720p_81_video_v1',sources=sources)
        if execution:receipt.update(schema_version=3,config_sha256=e.node_config_sha(self.configs[role]),base_unit_price_usd=float(base),resolution_multiplier=1.5 if role=='pose_edit' else None,effective_unit_price_usd=float(amount),estimated_cost_usd_decimal=amount,input_duration_seconds=None,driving_video_sha256=None)
        else:receipt.update(schema='sprite_endpoint_planning_quote_v1',endpoint_plan_sha256=scope_sha(self.plan),role=role,unit_price_usd=base,multiplier_decimal=mult)
        millis=int(now.timestamp()*1000);path=directory/f'{millis:016d}-{rid}.receipt.json';self.write(path,receipt)
        return sha_file(path),amount,path

    def materialize(self,role):
        e=self.e;config=self.configs[role] if role=='pose_edit' else self.make_config(role);directory=self.attempt/role;inputs=[dict(role=i['role'],sha256=i['sha256']) for i in config['input_files']];self.write(directory/'inputs.json',inputs)
        current=self.current();gates=current['endpoint_gates']
        if role=='pose_edit':evidence=dict(preparation_proof_sha256=sha_file(self.snap/'preparation-proof.json'),original_ref=e.ref(self.snap/'source-original.png'),b_candidate_ref=e.ref(self.snap/'b-candidate-original.png'))
        elif role!='cleanup':evidence=dict(pose_operation_id=self.aid+':pose_edit',pose_request_id='known-pose_edit',pose_raw_sha256=sha_file(self.attempt/'pose_edit/raw.png'),pair_gate_sha256=scope_sha(gates['pair_ready']),direction='ab' if role=='video_ab' else 'ba')
        else:evidence=dict(ab_operation_id=self.aid+':video_ab',ab_request_id='known-video_ab',ab_raw_sha256=sha_file(self.attempt/'video_ab/raw.mp4'),ba_operation_id=self.aid+':video_ba',ba_request_id='known-video_ba',ba_raw_sha256=sha_file(self.attempt/'video_ba/raw.mp4'),cycle_gate_sha256=scope_sha(gates['cycle_ready']),selection_sha256=sha_file(self.attempt/'selection.json'),grid_sha256=sha_file(self.attempt/'cycle-grid.png'))
        provenance=dict(schema='sprite_endpoint_provenance_v1',kind='pose_edit_inputs' if role=='pose_edit' else 'two_video_full_range_grid' if role=='cleanup' else 'ordered_endpoint_pair',original_source_sha256=self.source,input_set_sha256=scope_sha(inputs),endpoint_plan_sha256=scope_sha(self.plan),evidence=evidence);self.write(directory/'source-provenance.json',provenance)
        binding=dict(schema='sprite_execution_binding_v2',config_sha256=e.node_config_sha(config),config_file_sha256=sha_file(directory/'config.json'),input_set_sha256=scope_sha(inputs),provenance_sha256=sha_file(directory/'source-provenance.json'),deferred_plan_sha256=None if role=='pose_edit' else scope_sha(dict(schema='sprite_endpoint_deferred_v1',endpoint_plan_sha256=scope_sha(self.plan),role=role)))
        ledger.bind_operation_config(self.task['task_manifest'],self.aid+':'+role,binding)
        self.jobs[role]=dict(path=str(directory),operation_id=self.aid+':'+role,config_sha256=scope_sha(config),transport_config_sha256=e.node_config_sha(config),config_file_sha256=sha_file(directory/'config.json'),state='CONFIGURED',reserved_usd=float(self.quotes[role][1]));self.save()
        if role!='pose_edit':
            self.quotes[role]=self.quote(role,execution=True);state=self.state();ops=[]
            for name in e.ROLES:
                op=state['operations'][self.aid+':'+name];value={k:op['reservation'][k] for k in ('operation_id','kind',*(['config_sha256'] if name=='pose_edit' else ['deferred_config']))};value.update(quote_sha256=self.quotes[name][0],amount_usd=self.quotes[name][1]);ops.append(value)
            ledger.reprice_plan(self.task['task_manifest'],'run1',self.aid,ops,endpoint_plan=self.plan)
        return binding

    def gate(self,stage,subject,decision='pass'):
        e=self.e;note=self.attempt/(stage+'-observed.txt');note.write_text('Synthetic review assertion for software-only test. No artistic quality established.')
        refs=[e.ref(note)];checks={name:dict(status='pass',notes='Synthetic observation of matching subject, for software test only.') for name in e.GATE_CHECKS[stage]}
        if decision!='pass':checks[sorted(checks)[0]]['status']='hold' if decision=='hold' else 'fail'
        report=dict(schema='sprite_endpoint_gate_report_v1',stage=stage,subject=subject,decision=decision,checks=checks,notes='Offline synthetic assertions only; never art approval.',evidence=refs)
        path=self.attempt/(stage+'-'+decision+'-report.json');self.write(path,report)
        gate=dict(schema='sprite_endpoint_gate_v1',stage=stage,subject=subject,decision=decision,defects=[name+':'+checks[name]['notes'] for name in sorted(checks) if checks[name]['status']=='fail'],evidence=[*refs,e.ref(path)])
        return ledger.record_endpoint_gate(self.task['task_manifest'],'run1',self.aid,gate)

    def message(self,role,before=False):
        op=self.state()['operations'][self.aid+':'+role];binding=op['execution_binding'];value=dict(schema='sprite_before_submit_v2' if before else 'sprite_transport_context_v2',job_dir=str(self.attempt/role),**{k:binding[k] for k in ('config_sha256','config_file_sha256','input_set_sha256')})
        if before:value.update(quote_sha256=self.quotes[role][0],amount_usd=self.quotes[role][1],experiment_state_sha256=self.current()['experiment_state_sha256'])
        return value

    def complete(self,role):
        import sprite_task_bridge as bridge
        e=self.e;result=bridge.before_submit(self.message(role,True))
        if not result['submit_allowed']:raise AssertionError(result)
        directory=self.attempt/role;rid='known-'+role;config_sha=e.node_config_sha(self.configs[role]);quote_sha=self.quotes[role][0]
        raw=directory/('raw.mp4' if role.startswith('video') else 'raw.png')
        if role=='pose_edit':
            image=Image.new('RGB',(2048,2048),(110,110,110));image.paste((80,120,140),(650,600,1300,1700));image.save(raw)
        elif role=='cleanup':
            image=Image.new('RGBA',(256,128))
            for i in range(8):image.paste((70+i,110,130,170+i),(i%4*64+30,i//4*64+27+i%3,i%4*64+34,i//4*64+33+i%3))
            image.save(raw)
        else:
            command=[e.resolve_ffmpeg(),'-nostdin','-v','error','-f','lavfi','-i',f'testsrc2=size={self.video_size}x{self.video_size}:rate=24','-frames:v','81','-c:v','libx264','-pix_fmt','yuv420p',str(raw)]
            subprocess.run(command,check=True,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        state=dict(schema_version=3,state='DOWNLOADED',config_sha256=config_sha,request_id=rid,pricing_receipt_sha256=quote_sha,raw_path=str(raw),raw_sha256=sha_file(raw));self.write(directory/'job-state.json',state)
        self.write(directory/'submission.claim',dict(schema_version=3,config_sha256=config_sha,pricing_receipt_sha256=quote_sha,created_at=datetime.now(timezone.utc).isoformat()))
        ledger.record_outcome(self.task['task_manifest'],self.aid+':'+role,dict(status='completed',provider_request_id=rid,evidence='Synthetic offline provider outcome'))
        self.jobs[role].update(state='DOWNLOADED',request_id=rid,raw_path=str(raw),raw_sha256=sha_file(raw));self.save()

    def full_cycle(self):
        e=self.e;self.complete('pose_edit');pose=sha_file(self.attempt/'pose_edit/raw.png')
        pair=self.gate('pair_ready',dict(endpoint_plan_sha256=scope_sha(self.plan),pose_request_id='known-pose_edit',pose_raw_sha256=pose,endpoint_a_sha256=self.plan['endpoint_a_sha256'],endpoint_b_sha256=pose))
        self.materialize('video_ab');self.complete('video_ab');e.prepare_endpoint_video_evidence(self.path,'video_ab')
        ab=self.gate('ab_ready',dict(pair_gate_sha256=scope_sha(pair),ab_request_id='known-video_ab',ab_raw_sha256=sha_file(self.attempt/'video_ab/raw.mp4'),probe_sha256=sha_file(self.attempt/'video_ab/probe.json')))
        self.materialize('video_ba');self.complete('video_ba');e.prepare_endpoint_video_evidence(self.path,'video_ba');e.prepare_endpoint_cycle_evidence(self.path)
        self.gate('cycle_ready',dict(pair_gate_sha256=scope_sha(pair),ab_gate_sha256=scope_sha(ab),ab_request_id='known-video_ab',ab_raw_sha256=sha_file(self.attempt/'video_ab/raw.mp4'),ba_request_id='known-video_ba',ba_raw_sha256=sha_file(self.attempt/'video_ba/raw.mp4'),selection_sha256=sha_file(self.attempt/'selection.json'),grid_sha256=sha_file(self.attempt/'cycle-grid.png')))
        self.materialize('cleanup');self.complete('cleanup')
        return e.assemble_endpoint_attempt(self.path)

class SeedreamRasterTests(unittest.TestCase):
    """Synthetic provider bytes; no artwork or paid task mutation."""
    def raster(self, mode='RGB', size=(2048,2048), fmt='PNG', **options):
        image=Image.new(mode,size);buffer=io.BytesIO();image.save(buffer,format=fmt,**options);return buffer.getvalue()

    def classify(self, payload, metadata=None, content_type=None):
        import endpoint_cycle_contract as e
        return e.seedream_raster(payload, metadata or {}, content_type, e.decoder_capabilities())

    def test_png_and_jpeg_derive_exact_decoded_rgb(self):
        for fmt in ('PNG','JPEG'):
            with self.subTest(fmt=fmt):
                payload=self.raster(fmt=fmt)
                result=self.classify(payload)
                self.assertIsNone(result['reason']);self.assertEqual(result['raw_format'],fmt)
                with Image.open(io.BytesIO(payload)) as image:
                    self.assertEqual(result['pixels'],image.tobytes())

    def test_closed_defect_priority(self):
        cases=[(b'', 'RESULT_FORMAT_UNSUPPORTED'),(b'GIF89a','RESULT_FORMAT_UNSUPPORTED'),
               (self.raster(size=(16,16)), 'RESULT_DIMENSIONS_INVALID'),
               (self.raster(mode='RGBA'), 'RESULT_MODE_UNSUPPORTED'),
               (self.raster(mode='L'),'RESULT_MODE_UNSUPPORTED'),
               (self.raster(mode='P'),'RESULT_MODE_UNSUPPORTED'),
               (self.raster(mode='I;16'),'RESULT_MODE_UNSUPPORTED'),
               (self.raster(mode='CMYK',fmt='JPEG'),'RESULT_MODE_UNSUPPORTED'),
               (self.raster()[:-30],'RESULT_IMAGE_CORRUPT')]
        for payload,reason in cases:
            with self.subTest(reason=reason):self.assertEqual(self.classify(payload)['reason'],reason)

    def test_orientation_and_multiframe(self):
        exif=Image.Exif();exif[274]=2
        self.assertEqual(self.classify(self.raster(fmt='JPEG',exif=exif))['reason'],'RESULT_ORIENTATION_INVALID')
        frames=[Image.new('RGB',(2048,2048),color) for color in ('red','blue')]
        out=io.BytesIO();frames[0].save(out,format='PNG',save_all=True,append_images=frames[1:],duration=100)
        self.assertEqual(self.classify(out.getvalue())['reason'],'RESULT_MODE_UNSUPPORTED')

    def test_metadata_conflict_only_after_acceptable_raster(self):
        raw=self.raster()
        for metadata,mime in [({'width':True},None),({'height':5},None),({'file_size':1},None),({'content_type':'image/jpeg'},None),({},''),({},'text/plain'),({},'image/jpeg')]:
            with self.subTest(metadata=metadata,mime=mime):self.assertEqual(self.classify(raw,metadata,mime)['reason'],'RESULT_METADATA_CONFLICT')
        self.assertIsNone(self.classify(raw,{'width':None,'content_type':None},'application/octet-stream')['reason'])
        self.assertEqual(self.classify(self.raster(mode='RGBA'),{'width':1})['reason'],'RESULT_MODE_UNSUPPORTED')

    def test_codec_change_is_local_block(self):
        import endpoint_cycle_contract as e
        capabilities=e.decoder_capabilities();capabilities['pillow_version']='changed'
        with self.assertRaises(WorkflowError):e.seedream_raster(self.raster(),{},None,capabilities)

    def test_ambiguous_exif_and_multiframe_are_closed(self):
        import endpoint_cycle_contract as e
        import struct
        tiff=b'II'+struct.pack('<HIH',42,8,2)+struct.pack('<HHIHH',274,3,1,1,0)*2+struct.pack('<I',0)
        self.assertTrue(e._exif_orientation_invalid([tiff]))
        self.assertTrue(e._exif_orientation_invalid([b'invalid']))
        self.assertTrue(e._exif_orientation_invalid([tiff,tiff]))

    def test_corrupt_decoder_and_local_resource_exceptions_are_separate(self):
        import endpoint_cycle_contract as e
        raw=self.raster()
        with patch.object(e.Image,'open',side_effect=OSError('strict complete-byte decode failure')):
            self.assertEqual(self.classify(raw)['reason'],'RESULT_IMAGE_CORRUPT')
        with patch.object(e.Image,'open',side_effect=MemoryError('local resource exhaustion')):
            self.assertRaises(WorkflowError,self.classify,raw)

    def test_fix1_exif_after_first_scan_reproduces_review_witness(self):
        import endpoint_cycle_contract as e
        import struct
        exif1=Image.Exif();exif1[274]=1
        raw=self.raster(fmt='JPEG',exif=exif1)
        # The reviewer witness is deterministic for these exact source pixels.
        image=Image.new('RGB',(2048,2048),(1,2,3));out=io.BytesIO();image.save(out,format='JPEG',exif=exif1);raw=out.getvalue()
        exif2=Image.Exif();exif2[274]=2;block=exif2.tobytes();app1=b'\xff\xe1'+struct.pack('>H',len(block)+2)+block
        witness=raw[:-2]+app1+raw[-2:]
        self.assertEqual(e.digest(witness),'0c397646262ddd391cee457dafb88657b28c1dbf287ab89c5b5b8f0c1b4972db')
        with Image.open(io.BytesIO(witness)) as decoded:decoded.verify()
        with Image.open(io.BytesIO(witness)) as decoded:decoded.load();pixels=decoded.tobytes()
        with Image.open(io.BytesIO(raw)) as decoded:self.assertEqual(pixels,decoded.tobytes())
        self.assertEqual(self.classify(witness)['reason'],'RESULT_ORIENTATION_INVALID')

    def test_fix1_baseline_and_progressive_keep_exact_decoder_pixels(self):
        for progressive in (False,True):
            raw=self.raster(fmt='JPEG',progressive=progressive)
            result=self.classify(raw);self.assertIsNone(result['reason'])
            with Image.open(io.BytesIO(raw)) as image:self.assertEqual(result['pixels'],image.tobytes())

    def test_fix1_marker_walk_respects_entropy_stuffing_restart_and_multiple_scans(self):
        import endpoint_cycle_contract as e
        import struct
        def segment(marker,data):return b'\xff'+bytes([marker])+struct.pack('>H',len(data)+2)+data
        sof=segment(0xc2,struct.pack('>BHHB',8,2048,2048,3)+b'\x01\x11\x00\x02\x11\x01\x03\x11\x01')
        sos=segment(0xda,b'\x01\x01\x00\x00\x3f\x00')
        # These are parser fixtures, not decodable raster claims. FF00 prevents
        # the following E1/Exif-like entropy bytes from becoming APP1 metadata.
        stuffed=b'abc\xff\x00\xe1Exif\0\0payload\xff\xd0xyz\xff\xff\xd7'
        raw=b'\xff\xd8'+sof+sos+stuffed+sos+b'abc\xff\xd9'
        self.assertEqual(e._raster_headers(raw,'JPEG'),((2048,2048),False,False,False))
        exif=Image.Exif();exif[274]=2
        raw=raw[:-2]+segment(0xe1,exif.tobytes())+raw[-2:]
        self.assertEqual(e._raster_headers(raw,'JPEG'),((2048,2048),False,True,False))


class SeedreamPublicationTests(unittest.TestCase):
    def test_fix1_orphan_temporaries_are_not_adopted_or_overwritten(self):
        import endpoint_cycle_contract as e
        with tempfile.TemporaryDirectory(prefix='seedream-c-publish-') as temporary:
            directory=Path(temporary);orphan=directory/'.endpoint-b.png.interrupted.partial';orphan.write_bytes(b'interrupted prefix')
            target=directory/'endpoint-b.png';e.seedream_publish(target,b'complete new bytes')
            self.assertEqual(target.read_bytes(),b'complete new bytes');self.assertEqual(orphan.read_bytes(),b'interrupted prefix')

    def test_fix1_atomic_new_identical_and_conflicting_concurrent_publication(self):
        import endpoint_cycle_contract as e
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory(prefix='seedream-c-publish-') as temporary:
            path=Path(temporary)/'pose-output-proof.json';payload=b'complete canonical bytes'
            with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda _:e.seedream_publish(path,payload),range(6)))
            self.assertEqual(path.read_bytes(),payload)
            with self.assertRaises(WorkflowError):e.seedream_publish(path,b'changed')
            target=Path(temporary)/'endpoint-b.png'
            def publish(value):
                try:e.seedream_publish(target,value);return True
                except WorkflowError:return False
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(publish,[b'first',b'second']))
            self.assertEqual(sum(results),1);self.assertIn(target.read_bytes(),(b'first',b'second'))
            self.assertEqual(list(Path(temporary).glob('*.partial')),[])


class EndpointContractTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('endpoint_cycle_contract')

    def no_media_body(self,size):
        value=dict(detail=[dict(loc=['body'],msg='Synthetic completed request with no media.',type='no_media_generated',url='https://fal.ai/docs/documentation/model-apis/errors',input=dict(padding=''))])
        empty=json.dumps(value,separators=(',',':')).encode('utf-8');value['detail'][0]['input']['padding']='x'*(size-len(empty))
        payload=json.dumps(value,separators=(',',':')).encode('utf-8')
        self.assertEqual(len(payload),size)
        return payload

    def no_media_producer_fixture(self,temp,payload,*,retry_header=None):
        """Actual B+fixed bridge; mocked HTTP only, fresh isolated task/run."""
        e=self.module();import sprite_task_bridge as bridge
        f=EndpointFixture(temp);bridge.before_submit(f.message('pose_edit',True));directory=f.attempt/'pose_edit';request_id='synthetic-no-media-id';config_sha=e.node_config_sha(f.configs['pose_edit']);quote_sha=f.quotes['pose_edit'][0]
        queue='https://queue.fal.run/fal-ai/nano-banana-2/requests/'+request_id
        f.write(directory/'submission.claim',dict(schema_version=3,config_sha256=config_sha,pricing_receipt_sha256=quote_sha,created_at=datetime.now(timezone.utc).isoformat()))
        f.write(directory/'job-state.json',dict(schema_version=3,state='COMPLETED',request_id=request_id,config_sha256=config_sha,pricing_receipt_sha256=quote_sha,raw_path=None,raw_sha256=None,status_url=queue+'/status?logs=0',result_url=queue))
        body_path=Path(temp)/'mock-no-media-result.body.json';body_path.write_bytes(payload)
        # Result body/input/msg never appears in stdout, stderr or request logs.
        program="""import fs from'node:fs';const{runCommand}=await import(process.argv[1]);const calls=[];const body=fs.readFileSync(process.argv[3]);const retry=JSON.parse(process.argv[4]);
const fetchImpl=async(url,options={})=>{const method=options.method??'GET';calls.push({method,url:String(url)});if(method!=='GET')throw Error('unexpected POST');if(String(url).endsWith('/status?logs=0'))return new Response(JSON.stringify({status:'COMPLETED',request_id:'synthetic-no-media-id'}),{status:200});if(String(url).endsWith('/requests/synthetic-no-media-id'))return new Response(body,{status:422,headers:retry===null?{}:{'X-Fal-Needs-Retry':retry}});throw Error('unexpected mocked URL');};
const result=await runCommand('execute',process.argv[2],{key:'synthetic-offline-key',fetchImpl});process.stdout.write(JSON.stringify({result,calls}));"""
        node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe';env={k:os.environ[k] for k in ('SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP') if k in os.environ}
        child=subprocess.run([str(node),'--input-type=module','-e',program,Path(bridge.__file__).with_name('fal_sheet_transport.mjs').as_uri(),str(directory),str(body_path),json.dumps(retry_header)],capture_output=True,env=env,timeout=30)
        self.assertEqual(child.returncode,0,'Mock producer exited unsuccessfully; raw diagnostics deliberately omitted.')
        self.assertLess(len(child.stdout),65536);self.assertLess(len(child.stderr),65536);self.assertNotIn(b'data:image',child.stdout+child.stderr)
        result=json.loads(child.stdout);self.assertTrue(all(call['method']=='GET' for call in result['calls']));self.assertEqual(len(result['calls']),2)
        return f,result['result']

    @unittest.skipUnless(SCHEMA_ROOT.is_dir() and NO_MEDIA_RECORDED_BODY.is_file(),'read-only recorded no-media/schema evidence unavailable')
    def test_no_media_v2_actual_recorded_body_through_real_mocked_producer_and_c_reader(self):
        e=self.module();payload=NO_MEDIA_RECORDED_BODY.read_bytes();original_sha=sha_file(NO_MEDIA_RECORDED_BODY)
        self.assertEqual(len(payload),2973863);self.assertEqual(original_sha,'608adcbfb46f8791a2ad140296051b46c1a37bc338bd0b7aecab3176cbe07ab7')
        with tempfile.TemporaryDirectory(prefix='endpoint-no-media-recorded-') as temp:
            f,produced=self.no_media_producer_fixture(temp,payload);self.assertEqual(produced['state'],'RESULT_INVALID');self.assertEqual(produced['block_reason'],'PROVIDER_NO_MEDIA_GENERATED')
            sequence=f.state()['sequence'];verified=e.validate_endpoint_invalid_result(f.path,'pose_edit')
            self.assertEqual(verified['reason'],'PROVIDER_NO_MEDIA_GENERATED');self.assertEqual(verified['request_id'],'synthetic-no-media-id');self.assertEqual(f.state()['sequence'],sequence)
        self.assertEqual(sha_file(NO_MEDIA_RECORDED_BODY),original_sha)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed schemas unavailable')
    def test_no_media_v2_result_over_five_mib_and_exact_32mib_are_bounded_and_fast(self):
        e=self.module()
        for size in (5*1024*1024+4096,32*1024*1024):
            with self.subTest(result_bytes=size),tempfile.TemporaryDirectory(prefix='endpoint-no-media-bounds-') as temp:
                f,produced=self.no_media_producer_fixture(temp,self.no_media_body(size));self.assertEqual(produced['state'],'RESULT_INVALID')
                started=time.perf_counter();verified=e.validate_endpoint_invalid_result(f.path,'pose_edit');elapsed=time.perf_counter()-started
                self.assertEqual(verified['reason'],'PROVIDER_NO_MEDIA_GENERATED');self.assertLess(elapsed,15)
                print(json.dumps(dict(check='no-media-c-bounded-read',result_bytes=size,validation_seconds=round(elapsed,6))),flush=True)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed schemas unavailable')
    def test_no_media_v2_tamper_schema_fallback_and_oversize_do_not_gain_terminal_authority(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-no-media-tamper-') as temp:
            f,produced=self.no_media_producer_fixture(temp,self.no_media_body(5*1024*1024+4096));self.assertEqual(produced['state'],'RESULT_INVALID');directory=f.attempt/'pose_edit';proof_path=directory/'result-invalid.json';original=proof_path.read_bytes();proof=json.loads(original)
            state_path=directory/'job-state.json';state_original=state_path.read_bytes();state=json.loads(state_original);state['state']='COMPLETED';state.pop('result_invalid_sha256',None);state.pop('result_invalid_reason',None);f.write(state_path,state)
            original_node=e.node_local
            for schema in ('sprite_endpoint_invalid_result_v1','sprite_endpoint_invalid_result_v3'):
                changed=copy.deepcopy(proof);changed['schema']=schema;f.write(proof_path,changed)
                def prohibit_invalid(command,value):
                    if command=='invalid-result':self.fail('Non-v2 oversized result must stop at physical five-MiB bound.')
                    return original_node(command,value)
                with patch.object(e,'node_local',side_effect=prohibit_invalid),self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')
            proof_path.write_bytes(original)
            changed=copy.deepcopy(proof);changed['result']['headers']['x_fal_needs_retry']='true';f.write(proof_path,changed)
            with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')
            proof_path.write_bytes(original);body=directory/'result-evidence'/('result-'+proof['result']['body_sha256']+'.body.json');body_original=body.read_bytes();body.write_bytes(body_original+b' ')
            with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')
            body.write_bytes(body_original);state_path.write_bytes(state_original)
        with tempfile.TemporaryDirectory(prefix='endpoint-no-media-oversize-') as temp:
            f,produced=self.no_media_producer_fixture(temp,self.no_media_body(32*1024*1024+1));self.assertEqual(produced['state'],'BLOCKED');self.assertFalse((f.attempt/'pose_edit/result-invalid.json').exists())
            self.assertIsNone(f.state()['operations'][f.aid+':pose_edit']['provider_request_id'])

    def test_public_validator_and_assembly_are_present(self):
        module = self.module()
        self.assertTrue(callable(module.validate_endpoint_attempt))
        self.assertTrue(callable(module.assemble_endpoint_attempt))

    def test_quantized_24fps_timescale1000_keeps_full_cadence_but_25fps_fails(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-ticks-') as temp:
            directory=Path(temp);role=directory/'video_ab';role.mkdir();exe=e.resolve_ffmpeg()
            context=dict(attempt_dir=directory,manifest=dict(request=dict(ffmpeg_exe=exe)))
            for fps in (24,25):
                raw=role/'raw.mp4'
                subprocess.run([exe,'-nostdin','-v','error','-y','-f','lavfi','-i',f'testsrc2=size=64x64:rate={fps}','-frames:v','81','-c:v','libx264','-video_track_timescale','1000',str(raw)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                if fps==24:
                    stream=e.video_stream(context,'video_ab');self.assertEqual(stream['time_base'],[1,1000]);self.assertEqual(len(stream['timestamps']),81)
                else:
                    with self.assertRaises(WorkflowError):e.video_stream(context,'video_ab')

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_node_only_durable_id_recovers_after_gate_reject_and_expired_quote_with_mirror_lag(self):
        e=self.module();import sprite_task_bridge as bridge
        for downloaded in (False,True):
            with self.subTest(downloaded=downloaded),tempfile.TemporaryDirectory(prefix='endpoint-node-id-') as temp:
                f=EndpointFixture(temp);f.complete('pose_edit');pose=sha_file(f.attempt/'pose_edit/raw.png')
                pair=f.gate('pair_ready',dict(endpoint_plan_sha256=scope_sha(f.plan),pose_request_id='known-pose_edit',pose_raw_sha256=pose,endpoint_a_sha256=f.plan['endpoint_a_sha256'],endpoint_b_sha256=pose));f.materialize('video_ab')
                bridge.before_submit(f.message('video_ab',True));directory=f.attempt/'video_ab';rid='known-ab-node-only';node_sha=e.node_config_sha(f.configs['video_ab']);quote_sha=f.quotes['video_ab'][0]
                queue='https://queue.fal.run/fal-ai/wan-flf2v/requests/'+rid
                state=dict(schema_version=3,state='REQUEST_ACCEPTED',request_id=rid,config_sha256=node_sha,pricing_receipt_sha256=quote_sha,raw_path=None,raw_sha256=None,status_url=queue+'/status?logs=0',result_url=queue)
                if downloaded:
                    raw=directory/'raw.mp4';subprocess.run([e.resolve_ffmpeg(),'-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=64x64:rate=24','-frames:v','81','-c:v','libx264',str(raw)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    state.update(state='DOWNLOADED',raw_path=str(raw),raw_sha256=sha_file(raw))
                f.write(directory/'job-state.json',state);f.write(directory/'submission.claim',dict(schema_version=3,config_sha256=node_sha,pricing_receipt_sha256=quote_sha,created_at=datetime.now(timezone.utc).isoformat()))
                self.assertIsNone(f.state()['operations'][f.aid+':video_ab']['provider_request_id'])
                self.assertEqual(f.jobs['video_ab']['state'],'CONFIGURED')
                f.gate('pair_ready',pair['subject'],'reject')
                with patch.object(e,'validate_quote',side_effect=WorkflowError('PRICE_STALE')),patch.object(ledger,'mark_submission',side_effect=AssertionError('known-ID recovery cannot mark/POST')):
                    result=bridge.check_context(f.message('video_ab'));self.assertEqual(result['provider_request_id'],rid)
                    result=bridge.before_submit(f.message('video_ab',True));self.assertEqual(result['provider_request_id'],rid);self.assertFalse(result['submit_allowed'])
                # Real Node transport + fixed Python bridge. The only mocked
                # boundary is fetch: known recovery must not consult pricing or
                # POST even when Node's clock sees expired receipts.
                program="""const{runCommand}=await import(process.argv[1]);const calls=[];
const fetchImpl=async(url,options={})=>{calls.push({url:String(url),method:options.method??'GET'});if((options.method??'GET')!=='GET'||!String(url).includes('/requests/known-ab-node-only/'))throw Error('unexpected recovery request');return new Response(JSON.stringify({status:'IN_PROGRESS'}),{status:200});};
const result=await runCommand('execute',process.argv[2],{fetchImpl,key:'synthetic-offline-key',now:()=>Date.now()+7200000});process.stdout.write(JSON.stringify({result,calls}));"""
                node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe';env={k:os.environ[k] for k in ('SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP') if k in os.environ}
                child=subprocess.run([str(node),'--input-type=module','-e',program,Path(bridge.__file__).with_name('fal_sheet_transport.mjs').as_uri(),str(directory)],capture_output=True,env=env,timeout=30)
                self.assertEqual(child.returncode,0,child.stderr.decode(errors='replace'));transport=json.loads(child.stdout)
                self.assertEqual(transport['result']['state'],'DOWNLOADED' if downloaded else 'IN_PROGRESS',transport)
                self.assertTrue(all(call['method']=='GET' for call in transport['calls']))
                self.assertEqual(len(transport['calls']),0 if downloaded else 1)
                self.assertIsNone(f.state()['operations'][f.aid+':video_ab']['provider_request_id'])
                # Conflicts and missing claims cannot use the recovery exception.
                claim=directory/'submission.claim';original=claim.read_bytes();bad=json.loads(original);bad['config_sha256']='f'*64;claim.write_bytes(encoded(bad))
                with self.assertRaises(WorkflowError):bridge.check_context(f.message('video_ab'))
                claim.write_bytes(original);f.jobs['video_ab']['request_id']='foreign-id';f.save()
                with self.assertRaises(WorkflowError):bridge.check_context(f.message('video_ab'))
                f.jobs['video_ab'].pop('request_id');f.save();state_path=directory/'job-state.json';original_state=state_path.read_bytes();conflict=json.loads(original_state);conflict['request_id']='known-pose_edit';f.write(state_path,conflict)
                with self.assertRaises(WorkflowError):bridge.check_context(f.message('video_ab'))
                state_path.write_bytes(original_state)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_marker_without_durable_id_never_acquires_recovery_post_authority(self):
        e=self.module();import sprite_task_bridge as bridge
        with tempfile.TemporaryDirectory(prefix='endpoint-unknown-') as temp:
            f=EndpointFixture(temp);bridge.before_submit(f.message('pose_edit',True));directory=f.attempt/'pose_edit';node_sha=e.node_config_sha(f.configs['pose_edit']);quote_sha=f.quotes['pose_edit'][0]
            f.write(directory/'job-state.json',dict(schema_version=3,state='SUBMISSION_STATUS_UNKNOWN',request_id=None,config_sha256=node_sha,pricing_receipt_sha256=quote_sha))
            f.write(directory/'submission.claim',dict(schema_version=3,config_sha256=node_sha,pricing_receipt_sha256=quote_sha,created_at=datetime.now(timezone.utc).isoformat()))
            checked=bridge.check_context(f.message('pose_edit'));self.assertIsNone(checked['provider_request_id']);self.assertTrue(checked['submission_started'])
            with self.assertRaises(WorkflowError):bridge.before_submit(f.message('pose_edit',True))

    def invalid_pose_fixture(self,temp,*,node_state='RESULT_READY'):
        e=self.module();import sprite_task_bridge as bridge
        f=EndpointFixture(temp);bridge.before_submit(f.message('pose_edit',True));directory=f.attempt/'pose_edit';rid='known-invalid-pose';config_sha=e.node_config_sha(f.configs['pose_edit']);quote_sha=f.quotes['pose_edit'][0]
        claim=directory/'submission.claim';f.write(claim,dict(schema_version=3,config_sha256=config_sha,pricing_receipt_sha256=quote_sha,created_at=datetime.now(timezone.utc).isoformat()))
        evidence=directory/'result-evidence';evidence.mkdir();queue='https://queue.fal.run/fal-ai/nano-banana-2/requests/'+rid;output_url='https://fal.media/files/invalid-pose.png'
        status=encoded(dict(status='COMPLETED',request_id=rid));result=encoded(dict(images=[dict(url=output_url)]));buffer=io.BytesIO();Image.new('RGB',(1024,1024),(110,110,110)).save(buffer,format='PNG');output=buffer.getvalue()
        for prefix,suffix,payload in [('status','.body.json',status),('result','.body.json',result),('output','.bin',output)]:
            (evidence/(prefix+'-'+e.digest(payload)+suffix)).write_bytes(payload)
        binding=f.state()['operations'][f.aid+':pose_edit']['execution_binding']
        proof=dict(schema='sprite_endpoint_invalid_result_v1',role='pose_edit',model=e.MODELS['pose_edit'],operation_id=f.aid+':pose_edit',request_id=rid,config_sha256=config_sha,config_file_sha256=binding['config_file_sha256'],input_set_sha256=binding['input_set_sha256'],claim_sha256=sha_file(claim),reason='RESULT_IMAGE_INVALID',status=dict(url=queue+'/status?logs=0',http_status=200,body_sha256=e.digest(status)),result=dict(url=queue,http_status=200,body_sha256=e.digest(result)),download=dict(url=output_url,http_status=200,body_sha256=e.digest(output),bytes=len(output)))
        f.write(directory/'result-invalid.json',proof)
        state=dict(schema_version=3,state=node_state,request_id=rid,config_sha256=config_sha,pricing_receipt_sha256=quote_sha,raw_path=None,raw_sha256=None,status_url=queue+'/status?logs=0',result_url=queue,output_url=output_url)
        if node_state=='RESULT_INVALID':state.update(result_invalid_sha256=sha_file(directory/'result-invalid.json'),result_invalid_reason=proof['reason'])
        f.write(directory/'job-state.json',state)
        return f,proof

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_completed_invalid_result_reader_is_readonly_known_id_bound_and_crash_recoverable(self):
        e=self.module()
        for state in ('RESULT_READY','COMPLETED','RESULT_INVALID'):
            with self.subTest(state=state),tempfile.TemporaryDirectory(prefix='endpoint-invalid-result-') as temp:
                f,proof=self.invalid_pose_fixture(temp,node_state=state);directory=f.attempt/'pose_edit';f.gate('input_ready',dict(endpoint_plan_sha256=scope_sha(f.plan)),'reject')
                before=f.state()['sequence'];verified=e.validate_endpoint_invalid_result(f.path,'pose_edit')
                self.assertEqual(verified['state'],'RESULT_INVALID');self.assertEqual(verified['request_id'],proof['request_id']);self.assertEqual(verified['evidence_ref'],dict(path=str(directory/'result-invalid.json'),sha256=sha_file(directory/'result-invalid.json')))
                self.assertEqual(f.state()['sequence'],before);self.assertIsNone(f.state()['operations'][f.aid+':pose_edit']['provider_request_id'])
                for relative in ('result-invalid.json','submission.claim','result-evidence/status-'+proof['status']['body_sha256']+'.body.json','result-evidence/result-'+proof['result']['body_sha256']+'.body.json','result-evidence/output-'+proof['download']['body_sha256']+'.bin'):
                    with self.subTest(tamper=relative):
                        path=directory/relative;original=path.read_bytes();path.write_bytes(original+b'x')
                        with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')
                        path.write_bytes(original)
                path=directory/'job-state.json';original=path.read_bytes();changed=json.loads(original);changed['request_id']=None;path.write_bytes(encoded(changed))
                with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')
                path.write_bytes(original)
                (directory/'raw.png').write_bytes(b'unexpected normal output')
                with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')
                (directory/'raw.png').unlink()
                changed=copy.deepcopy(proof);changed['request_id']='foreign-proof-request';f.write(directory/'result-invalid.json',changed)
                with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_invalid_result_proof_cannot_terminalize_a_valid_2048_output(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-valid-result-') as temp:
            f,proof=self.invalid_pose_fixture(temp);directory=f.attempt/'pose_edit';buffer=io.BytesIO();Image.new('RGB',(2048,2048),(110,110,110)).save(buffer,format='PNG');payload=buffer.getvalue()
            proof['download'].update(body_sha256=e.digest(payload),bytes=len(payload));(directory/'result-evidence'/('output-'+e.digest(payload)+'.bin')).write_bytes(payload);f.write(directory/'result-invalid.json',proof)
            with self.assertRaises(WorkflowError):e.validate_endpoint_invalid_result(f.path,'pose_edit')

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_completed_empty_200_output_is_preserved_and_rederived_as_not_png(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-empty-result-') as temp:
            f,proof=self.invalid_pose_fixture(temp);directory=f.attempt/'pose_edit';payload=b''
            proof['reason']='RESULT_NOT_PNG';proof['download'].update(body_sha256=e.digest(payload),bytes=0);(directory/'result-evidence'/('output-'+e.digest(payload)+'.bin')).write_bytes(payload);f.write(directory/'result-invalid.json',proof)
            verified=e.validate_endpoint_invalid_result(f.path,'pose_edit');self.assertEqual(verified['reason'],'RESULT_NOT_PNG')

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_actual_invalid_result_failed_outcome_supports_directed_correction_with_same_config_and_id(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-invalid-correction-') as temp:
            f,proof=self.invalid_pose_fixture(temp,node_state='RESULT_INVALID');verified=e.validate_endpoint_invalid_result(f.path,'pose_edit');reference=verified['evidence_ref']
            ledger.record_outcome(f.task['task_manifest'],f.aid+':pose_edit',dict(status='failed',provider_request_id=verified['request_id'],evidence=reference['path']+'#sha256='+reference['sha256']))
            old=f.attempt/'pose_edit/config.json';directory=Path(temp)/'run2/attempt-01/pose_edit';directory.mkdir(parents=True);path=directory/'config.json';review_path=directory.parent/'correction-review.json'
            config=copy.deepcopy(f.configs['pose_edit']);config['input']['prompt']='Return exactly one2048pixel square image; preserve source anatomy and the supplied B pose.';path.write_bytes(encoded(config))
            changes=e._payload_changes(e._payload_projection(f.configs['pose_edit']),e._payload_projection(config))
            review=dict(schema='sprite_endpoint_correction_review_v1',previous_run_id='run1',previous_attempt_id=f.aid,previous_pose_config_sha256=f.plan['pose_config_sha256'],pose_config_sha256=e.node_config_sha(config),defect='RESULT_IMAGE_INVALID: actual completed output was1024square, not the requested2048square.',evidence=[e.ref(old),e.ref(path),reference],changed_fields=[c['field'] for c in changes],decision='pass',notes='The concrete added output-dimension instruction addresses the measured1024square provider failure; it does not assert any artistic defect or guaranteed effect.',correction_kind='prompt_semantic_change',changes=[dict(**c,defect_effect='Adds explicit2048square output constraints targeted at the actual1024square result.') for c in changes])
            review_path.write_bytes(encoded(review));self.assertEqual(e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path),review)

    def test_common_nearest_preserves_all_rgba_and_y_with_one_formula(self):
        module = self.module()
        source = Image.new('RGBA', (3, 3))
        for y in range(3):
            for x in range(3): source.putpixel((x, y), (x * 70, y * 80, x + y, 17 + x + y))
        out = module.common_nearest_2048(source)
        self.assertEqual(out.size, (2048, 2048))
        for y in (0, 681, 682, 1024, 1365, 2047):
            for x in (0, 681, 682, 1024, 1365, 2047):
                self.assertEqual(out.getpixel((x, y)), source.getpixel(((2*x+1)*3//4096,(2*y+1)*3//4096)))

    def test_real_raw_frame_count_fps_square_and_boundaries_are_verified(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-video-boundaries-') as temp:
            directory=Path(temp);role=directory/'video_ab';role.mkdir();exe=e.resolve_ffmpeg()
            context=dict(attempt_dir=directory,manifest=dict(request=dict(ffmpeg_exe=exe)))
            for name,size,fps,count,valid in [('valid','64x64',24,81,True),('short','64x64',24,80,False),('fps','64x64',25,81,False),('rectangle','64x32',24,81,False),('wide','962x962',24,1,False)]:
                with self.subTest(name=name):
                    raw=role/'raw.mp4'
                    subprocess.run([exe,'-nostdin','-v','error','-y','-f','lavfi','-i',f'testsrc2=size={size}:rate={fps}','-frames:v',str(count),'-c:v','libx264','-pix_fmt','yuv420p',str(raw)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    if valid:self.assertEqual(e.video_stream(context,'video_ab')['time_base'],[1,12288])
                    else:
                        with self.assertRaises(WorkflowError):e.video_stream(context,'video_ab')

    def test_different_raw_widths_and_altered_full_frame_evidence_are_rejected(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-widths-') as temp:
            directory=Path(temp);exe=e.resolve_ffmpeg();context=dict(attempt_dir=directory,manifest=dict(request=dict(ffmpeg_exe=exe)))
            for role,width in [('video_ab',64),('video_ba',32)]:
                job=directory/role;job.mkdir();decoded=job/'decoded';decoded.mkdir();raw=job/'raw.mp4'
                subprocess.run([exe,'-nostdin','-v','error','-f','lavfi','-i',f'testsrc2=size={width}x{width}:rate=24','-frames:v','81','-c:v','libx264',str(raw)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                e.ffmpeg(exe,['-i',str(raw),'-map','0:v:0','-an','-pix_fmt','rgb24','-fps_mode','passthrough','-frames:v','82','-start_number','0',str(decoded/'frame-%03d.png')])
                stream=e.video_stream(context,role);(job/'framehash.txt').write_bytes(stream['payload'])
                proof=dict(schema='sprite_endpoint_probe_v1',raw_sha256=sha_file(raw),width=width,height=width,frame_count=81,fps=24,time_base=stream['time_base'],timestamps=stream['timestamps'],frame_sha256=[sha_file(decoded/f'frame-{i:03d}.png') for i in range(81)],framehash_sha256=e.digest(stream['payload']))
                (job/'probe.json').write_bytes(encoded(proof));e.validate_video_evidence(context,role)
            with self.assertRaises(WorkflowError):e.cycle_values(context)
            path=directory/'video_ab/decoded/frame-080.png'
            with Image.open(path) as image:changed=image.copy()
            changed.putpixel((0,0),(1,2,3));changed.save(path)
            with self.assertRaises(WorkflowError):e.validate_video_evidence(context,'video_ab')

    def test_gate_report_derives_decision_defects_and_reopens_bytes(self):
        module = self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-gate-') as temp:
            root = Path(temp)
            evidence = root/'observed.txt';evidence.write_text('Observed actual playback, endpoints and full range.')
            refs = [dict(path=str(evidence),sha256=sha_file(evidence))]
            checks = {key:dict(status='pass',notes='Observed the actual subject at the required common scale.') for key in module.GATE_CHECKS['pair_ready']}
            subject = dict(endpoint_plan_sha256='a'*64,pose_request_id='pose-known',pose_raw_sha256='b'*64,endpoint_a_sha256='c'*64,endpoint_b_sha256='b'*64)
            report = dict(schema='sprite_endpoint_gate_report_v1',stage='pair_ready',subject=subject,decision='pass',checks=checks,notes='All actual input evidence was viewed.',evidence=refs)
            path = root/'report.json';path.write_bytes(encoded(report))
            gate = dict(schema='sprite_endpoint_gate_v1',stage='pair_ready',subject=subject,decision='pass',defects=[],evidence=[*refs,dict(path=str(path),sha256=sha_file(path))])
            self.assertEqual(module.validate_gate_report(gate),report)
            evidence.write_text('tampered')
            with self.assertRaises(WorkflowError): module.validate_gate_report(gate)

    def test_v2_message_rejects_scalar_source_and_requires_token_before_marker(self):
        import sprite_task_bridge as bridge
        module = self.module()
        value=dict(schema='sprite_transport_context_v2',job_dir=str(Path(tempfile.gettempdir())/'run/attempt-01/pose_edit'),config_sha256='a'*64,config_file_sha256='b'*64,input_set_sha256='c'*64)
        module.validate_bridge_message(value,False)
        bad={**value,'source_sha256':'d'*64}
        with self.assertRaises(WorkflowError): module.validate_bridge_message(bad,False)
        before={**value,'schema':'sprite_before_submit_v2','quote_sha256':'e'*64,'amount_usd':'0.12'}
        with self.assertRaises(WorkflowError): module.validate_bridge_message(before,True)
        result=subprocess.run([sys.executable,'-B',str(Path(bridge.__file__)),'check-context'],input=encoded(value),capture_output=True)
        self.assertEqual(result.returncode,2)
        self.assertEqual(set(json.loads(result.stdout)),{'state','block_reason'})

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_pose_bridge_real_quote_and_known_id_recovery_survive_later_reject(self):
        import sprite_task_bridge as bridge
        with tempfile.TemporaryDirectory(prefix='endpoint-boundary-') as temp:
            fixture=EndpointFixture(temp)
            checked=bridge.check_context(fixture.message('pose_edit'))
            self.assertEqual(checked['schema'],'sprite_transport_context_result_v2')
            fixture.complete('pose_edit')
            fixture.gate('input_ready',dict(endpoint_plan_sha256=scope_sha(fixture.plan)),'reject')
            checked=bridge.check_context(fixture.message('pose_edit'))
            self.assertEqual(checked['provider_request_id'],'known-pose_edit')
            marked=bridge.before_submit(fixture.message('pose_edit',True))
            self.assertFalse(marked['submit_allowed'])

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_remaining_quote_tamper_blocks_pose_before_marker(self):
        import sprite_task_bridge as bridge
        with tempfile.TemporaryDirectory(prefix='endpoint-boundary-') as temp:
            fixture=EndpointFixture(temp);fixture.quotes['video_ba'][2].write_bytes(b'{}')
            with self.assertRaises(WorkflowError):bridge.before_submit(fixture.message('pose_edit',True))
            self.assertFalse(fixture.state()['operations'][fixture.aid+':pose_edit']['submission_started'])

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_preparation_accepts_other_lossless_encoding_but_rejects_any_changed_pixel(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-prep-') as temp:
            fixture=EndpointFixture(temp,prepared_compression=9);e.load_context(fixture.path)
            target=fixture.snap/'edit.png'
            with Image.open(target) as image:changed=image.copy()
            changed.putpixel((400,400),(1,2,3));changed.save(target)
            with self.assertRaises(WorkflowError):e.load_context(fixture.path)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_hold_stale_reports_and_experiment_token_race_block_new_post(self):
        e=self.module();import sprite_task_bridge as bridge
        with tempfile.TemporaryDirectory(prefix='endpoint-gates-') as temp:
            fixture=EndpointFixture(temp);message=fixture.message('pose_edit',True)
            fixture.gate('input_ready',dict(endpoint_plan_sha256=scope_sha(fixture.plan)),'hold')
            with self.assertRaises(WorkflowError):bridge.before_submit(fixture.message('pose_edit',True))
            fixture.gate('input_ready',dict(endpoint_plan_sha256=scope_sha(fixture.plan)),'pass')
            with self.assertRaises(WorkflowError):bridge.before_submit(message)
            self.assertFalse(fixture.state()['operations'][fixture.aid+':pose_edit']['submission_started'])
            report=fixture.attempt/'input_ready-pass-report.json';original=report.read_bytes();value=json.loads(original)
            value['subject']['endpoint_plan_sha256']='0'*64;report.write_bytes(encoded(value))
            with self.assertRaises(WorkflowError):bridge.check_context(fixture.message('pose_edit'))
            report.write_bytes(original)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_future_quotes_expiry_unit_multiplier_addons_and_amount_replay(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-quotes-') as temp:
            fixture=EndpointFixture(temp);context=e.load_context(fixture.path);path=fixture.quotes['video_ba'][2];original=path.read_bytes()
            for kind in ('expired','seconds','multiplier','amount','addon','plan'):
                with self.subTest(kind=kind):
                    value=json.loads(original)
                    if kind=='expired':value['expires_at']='2000-01-01T00:15:00.000Z'
                    elif kind=='seconds':value['api_unit']='seconds'
                    elif kind=='multiplier':value['multiplier_decimal']='1.25'
                    elif kind=='amount':value['estimated_cost_usd']='0.41'
                    elif kind=='addon':value['high_thinking']=True
                    else:value['endpoint_plan_sha256']='0'*64
                    path.write_bytes(encoded(value))
                    with self.assertRaises(WorkflowError):e.validate_quote(context,'video_ba',execution=False,quote_sha=sha_file(path),amount='0.4')
            path.write_bytes(original)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_second_endpoint_wrong_request_claim_and_materialization_are_not_accepted(self):
        e=self.module();import sprite_task_bridge as bridge
        with tempfile.TemporaryDirectory(prefix='endpoint-pair-') as temp:
            f=EndpointFixture(temp);f.complete('pose_edit');pose=sha_file(f.attempt/'pose_edit/raw.png')
            f.gate('pair_ready',dict(endpoint_plan_sha256=scope_sha(f.plan),pose_request_id='known-pose_edit',pose_raw_sha256=pose,endpoint_a_sha256=f.plan['endpoint_a_sha256'],endpoint_b_sha256=pose));f.materialize('video_ab')
            for relative,key,value in [('pose_edit/job-state.json','request_id','foreign-request'),('pose_edit/submission.claim','config_sha256','f'*64),('video_ab/config.json','input_files',f.configs['video_ab']['input_files'][:1])]:
                with self.subTest(file=relative):
                    path=f.attempt/relative;original=path.read_bytes();changed=json.loads(original);changed[key]=value;path.write_bytes(encoded(changed))
                    with self.assertRaises(WorkflowError):bridge.check_context(f.message('video_ab'))
                    path.write_bytes(original)
            raw=f.attempt/'pose_edit/raw.png';original=raw.read_bytes();shutil.copyfile(f.snap/'endpoint-a.png',raw)
            with self.assertRaises(WorkflowError):bridge.check_context(f.message('video_ab'))
            raw.write_bytes(original)

    def test_economic_digest_uses_actual_node_numbers_for_real_budget_values(self):
        e=self.module()
        self.assertEqual(e.node_config_sha(dict(cap=1.0)),scope_sha(dict(cap=1)))
        for cap in (1.14,29.232,0.000001,1e-7):
            code="const fs=require('fs'),c=require('crypto');let v=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(c.createHash('sha256').update(JSON.stringify(v,null,2)+'\\n').digest('hex'));"
            node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
            expected=subprocess.run([str(node),'-e',code],input=json.dumps(dict(cap=cap)).encode(),stdout=subprocess.PIPE,check=True).stdout.decode()
            self.assertEqual(e.node_config_sha(dict(cap=cap)),expected)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_second_attempt_requires_real_directed_correction_review_and_exact_payload_delta(self):
        e=self.module()
        with tempfile.TemporaryDirectory(prefix='endpoint-correction-') as temp:
            f=EndpointFixture(temp);rejection=f.gate('input_ready',dict(endpoint_plan_sha256=scope_sha(f.plan)),'reject')
            new=Path(temp)/'run2/attempt-01/pose_edit';new.mkdir(parents=True);path=new/'config.json';review_path=new.parent/'correction-review.json'
            config=copy.deepcopy(f.configs['pose_edit']);context=config['authorization']['task_context'];context.update(run_id='run2',attempt_id='run2:attempt-01',operation_id='run2:attempt-01:pose_edit',run_manifest=str(new.parent.parent/'run-manifest.json'))
            path.write_bytes(encoded(config))
            with self.assertRaises(WorkflowError):e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path)
            config['input']['prompt']='  '+config['input']['prompt']+'  ';path.write_bytes(encoded(config))
            with self.assertRaises(WorkflowError):e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path)
            config['input']['prompt']='Preserve all three fingers of the near hand; do not merge the fingertips in the corrected endpoint.';path.write_bytes(encoded(config))
            # New words alone never grant a retry: the directed review is required.
            with self.assertRaises(WorkflowError):e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path)
            old=f.attempt/'pose_edit/config.json';changes=e._payload_changes(e._payload_projection(f.configs['pose_edit']),e._payload_projection(config))
            report=dict(schema='sprite_endpoint_correction_review_v1',previous_run_id='run1',previous_attempt_id=f.aid,previous_pose_config_sha256=f.plan['pose_config_sha256'],pose_config_sha256=e.node_config_sha(config),defect=rejection['defects'][0],evidence=[e.ref(old),e.ref(path),*rejection['evidence']],changed_fields=[c['field'] for c in changes],decision='pass',notes='The new explicit finger separation instruction addresses the actual observed hand merger; this is reviewer judgment, not guaranteed art quality.',correction_kind='prompt_semantic_change',changes=[dict(**c,defect_effect='Explicitly constrains the merged near-hand fingertips observed in the rejected artifact.') for c in changes])
            review_path.write_bytes(encoded(report));self.assertEqual(e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path),report)
            for kind in ('classification','field','before','after','evidence'):
                with self.subTest(kind=kind):
                    bad=copy.deepcopy(report)
                    if kind=='classification':bad['correction_kind']='input_change'
                    elif kind=='field':bad['changes'][0]['field']='input.frames'
                    elif kind=='before':bad['changes'][0]['before']='invented prior payload'
                    elif kind=='after':bad['changes'][0]['after']='invented corrected payload'
                    else:bad['evidence']=bad['evidence'][:2]
                    review_path.write_bytes(encoded(bad))
                    with self.assertRaises(WorkflowError):e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_directed_correction_can_bind_actual_known_terminal_failure_evidence(self):
        e=self.module();import sprite_task_bridge as bridge
        with tempfile.TemporaryDirectory(prefix='endpoint-failed-correction-') as temp:
            f=EndpointFixture(temp);bridge.before_submit(f.message('pose_edit',True))
            old=f.attempt/'pose_edit/config.json';state_path=f.attempt/'pose_edit/job-state.json'
            state=dict(schema_version=3,state='REMOTE_REJECTED',request_id='known-rejected-pose',config_sha256=f.plan['pose_config_sha256'],remote_detail='Input pose constraint failed; recorded synthetic fixture only.')
            state_path.write_bytes(encoded(state));ledger.record_outcome(f.task['task_manifest'],f.aid+':pose_edit',dict(status='rejected',provider_request_id='known-rejected-pose',evidence=str(state_path)+'#sha256='+sha_file(state_path)))
            directory=Path(temp)/'run2/attempt-01/pose_edit';directory.mkdir(parents=True);path=directory/'config.json';review_path=directory.parent/'correction-review.json'
            config=copy.deepcopy(f.configs['pose_edit']);config['input']['prompt']='Constrain correction to the supplied near hand while preserving all other source anatomy and material.';path.write_bytes(encoded(config))
            changes=e._payload_changes(e._payload_projection(f.configs['pose_edit']),e._payload_projection(config))
            report=dict(schema='sprite_endpoint_correction_review_v1',previous_run_id='run1',previous_attempt_id=f.aid,previous_pose_config_sha256=f.plan['pose_config_sha256'],pose_config_sha256=e.node_config_sha(config),defect='Observed material edit exceeded supplied pose constraint.',evidence=[e.ref(old),e.ref(path),e.ref(state_path)],changed_fields=[c['field'] for c in changes],decision='pass',notes='The specific additional pose preservation instruction targets the observed terminal failure; reviewer intent is not proof of provider success.',correction_kind='prompt_semantic_change',changes=[dict(**c,defect_effect='Adds a concrete restriction addressing the observed supplied-pose constraint failure.') for c in changes])
            review_path.write_bytes(encoded(report));self.assertEqual(e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path),report)
            state['request_id']='foreign';state_path.write_bytes(encoded(state))
            with self.assertRaises(WorkflowError):e.validate_endpoint_correction_review(f.task['task_manifest'],'run1',path,review_path)

    @unittest.skipUnless(SCHEMA_ROOT.is_dir(),'read-only reviewed endpoint schemas unavailable')
    def test_full_composite_parent_public_alignment_review_export_and_gate_revocation(self):
        import generic_sprite_workflow as workflow
        from sprite_alignment_workflow import align_manifest
        from test_sprite_alignment_workflow import annotate,body_review
        from test_sheet_review import VisualReviewTests
        with tempfile.TemporaryDirectory(prefix='endpoint-full-') as temp:
            root=Path(temp);fixture=EndpointFixture(root);result=fixture.full_cycle()
            self.assertEqual(result['state'],workflow.READY,result)
            self.assertEqual(workflow.resume(fixture.path,transport=lambda *_:self.fail('network'))['block_reason'],'ENDPOINT_DRIVER_REQUIRED')
            self.assertEqual(workflow.revise(fixture.path,'A new directed correction',transport=lambda *_:self.fail('network'))['block_reason'],'ENDPOINT_DRIVER_REQUIRED')
            anchors=annotate(result,root/'anchors.json');value=json.loads(anchors.read_text())
            value['reference']['source_point']=[650,600]
            for i,item in enumerate(value['frames']):item['point']=[1024,1024+i%3]
            anchors.write_bytes(encoded(value))
            derived=align_manifest(fixture.path,anchors,output_root=root/'aligned')
            self.assertEqual(derived['state'],workflow.READY,derived)
            parent=json.loads(fixture.path.read_text());child=json.loads(Path(derived['run_manifest']).read_text())
            self.assertEqual(child['derivation']['source_attempt'],dict(run_id='run1',attempt_id=fixture.aid))
            proof=json.loads((Path(derived['run_manifest']).parent/'derivation/proof.json').read_text())
            self.assertTrue(all(t['dy']==0 and t['scale']==1 for t in proof['transforms']))
            report=VisualReviewTests.report(self,derived);report['body_alignment']=body_review(derived)
            review=root/'review.json';review.write_bytes(encoded(report))
            accepted=workflow.record_visual_review(derived['run_manifest'],review)
            self.assertTrue(accepted['delivery_allowed'],accepted)
            exported=workflow.export_artifacts(derived['run_manifest'],root/'export')
            self.assertTrue(exported['delivery_allowed'],exported)
            selection=fixture.attempt/'selection.json';original=selection.read_bytes();bad=json.loads(original);bad['indices']=[10,30,50,70];selection.write_bytes(encoded(bad))
            self.assertFalse(workflow.inspect(fixture.path)['delivery_allowed'])
            self.assertIsNotNone(workflow.inspect(fixture.path)['block_reason'])
            selection.write_bytes(original)
            proof_path=fixture.attempt/'assembly-provenance.json';original=proof_path.read_bytes()
            for kind in ('factor','origin','hash'):
                bad=json.loads(original)
                if kind=='factor':bad['geometry']['scale_denominator']=65
                elif kind=='origin':bad['geometry']['origin']=[0,1]
                else:bad['geometry']['output_frame_sha256'][0]='0'*64
                proof_path.write_bytes(encoded(bad));self.assertIsNotNone(workflow.inspect(fixture.path)['block_reason'])
            proof_path.write_bytes(original)
            # A crash-surviving final reject receipt is authoritative even before
            # the mutable parent mirror or ledger projection has reconciled it.
            rejected=VisualReviewTests.report(self,result);rejected['decision']='reject';rejected['checks']['motion']['status']='fail'
            orphan=fixture.attempt/'visual-review-001.json';orphan.write_bytes(encoded(rejected))
            self.assertFalse(workflow.export_artifacts(derived['run_manifest'],root/'parent-review-revoked')['delivery_allowed'])
            orphan.unlink()  # isolated synthetic fixture only; recover pre-reject branch for next case
            fixture.gate('cycle_ready',fixture.current()['endpoint_gates']['cycle_ready']['subject'],'reject')
            self.assertFalse(workflow.export_artifacts(derived['run_manifest'],root/'after-revoke')['delivery_allowed'])

if __name__ == '__main__':unittest.main()
