"""Managed facade integration: real ledger, Node and fixed bridge; fake network only."""
import hashlib
import copy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops
sys.path.insert(0,str(Path(__file__).resolve().parent))
import generic_sprite_workflow as workflow
import sprite_task_ledger as ledger
from sheet_workflow_state import encoded, sha_file
from sheet_workflow_state import WorkflowError
from test_sheet_workflow import sheet, Provider

NODE = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
TRANSPORT = Path(__file__).with_name('fal_sheet_transport.mjs').resolve().as_uri()

def history_fixture(root, source, *, action='walk', target_id='character', descriptor=None,
                    pending=False, unresolved=False, local_unknown=False):
    """Actual sealed synthetic history, isolated from historical scan roots."""
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    evidence=root/'historical-evidence.json';evidence.write_bytes(b'{"synthetic":true}\n')
    ref=dict(path=str(evidence),sha256=sha_file(evidence));source_sha=sha_file(source)
    descriptor=descriptor or dict(route='video',model=workflow.H3,conditioning='single_source',
        inputs=[dict(role='source',sha256=source_sha)])
    native=descriptor['route']=='native_motion_construction'
    local=native or local_unknown or unresolved
    mechanism=dict(origin='local' if local else 'fal',route=descriptor['route'],model=descriptor['model'],
        conditioning=descriptor['conditioning'],resolution='current_descriptor',current_descriptor=descriptor)
    if unresolved or local_unknown:
        mechanism=dict(origin='local',route=None,model=None,conditioning=None,resolution='unresolved',current_descriptor=None)
    reservations=[] if local else [dict(import_id='old-reservation',target_id=target_id,kind='provider_animation',
        source_sha256=source_sha,config_sha256='c'*64,evidence_sha256=ref['sha256'],amount_usd='0.1',
        provider_request_id='old-known-id',uncertain=False)]
    record=dict(history_id='old-attempt',target_id=target_id,source_sha256=source_sha,run_id='historical-run',
        attempt_id=None,attempt_index=4,source_history_id=None,
        kind='unresolved' if unresolved else 'local_derivation' if local_unknown else 'native_construction' if native else 'provider_generation',
        mechanism=mechanism,local_attempt_limit=7,global_attempt_limit=None,feedback='Preserved synthetic history',
        cost_basis='local_no_new_reservation' if local else 'fal_operational_reservations',
        reservation_ids=[] if local else ['old-reservation'],reviews=[],evidence=[ref])
    bundle=dict(schema='sprite_task_history_v1',migration_id='synthetic-consumer-migration',
        eligibility_policy='historical_generation_closed_local_source_v1',
        coverage=dict(target_ids=[target_id],evidence=[ref],complete=True),reservations=reservations,attempts=[record])
    requirement=dict(schema='sprite_history_requirement_v1',migration_id=bundle['migration_id'],
        bundle_sha256=hashlib.sha256(encoded(bundle)).hexdigest())
    task=ledger.create_task(root/'tasks',dict(task_id='synthetic-history',cap_usd='1',
        budget_basis='fal_operational_reservations',allowed_operations=list(ledger.OPERATION_KINDS),
        evidence_ref='Synthetic consumer authorization; never real history'),
        [dict(target_id=target_id,source_sha256=source_sha,action=action,orientation='left',requirements={'frames':8})],
        historical_import=requirement)
    if not pending:ledger.import_task_history(task['task_manifest'],bundle)
    return ledger.inspect_task(task['task_manifest'])

# The imported production transport uses its DEFAULT fixedTaskBridge. No fake
# Python bridge, callback marker or alternative JS canonicalizer is provided.
NETWORK = r'''
import fs from 'node:fs';
import path from 'node:path';
const {runCommand}=await import(process.argv[1]);
const [command,job,fixture]=process.argv.slice(2);
const scene=JSON.parse(fs.readFileSync(fixture));
const cfg=command==='pricing-plan'?JSON.parse(fs.readFileSync(path.join(job,'plan.json'))):JSON.parse(fs.readFileSync(path.join(job,'config.json')));
const model=cfg.model;
const response=x=>new Response(JSON.stringify(x),{status:200});
globalThis.fetch=()=>{throw Error('unmocked network forbidden');};
const fetchImpl=async(url,init)=>{
 url=String(url);const method=init.method;
 fs.appendFileSync(scene.log,JSON.stringify({command,job,model,method,url})+'\n');
 if(url.startsWith('https://api.fal.ai/v1/models/pricing?'))return response({prices:[{endpoint_id:model,currency:'USD',unit:model.startsWith('minimax')?'seconds':'image',unit_price:model.includes('ideogram')?scene.cleanup_price:'0.08'}]});
 if(url==='https://fal.ai/models/fal-ai/nano-banana-2/edit')return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><p>Your request will cost $0.08 per image. 2K and 4K outputs will be charged at 1.5 times and 2 times the standard rate, respectively.</p></body></html>');
 if(url==='https://fal.ai/models/minimax/h3-max/image-to-video')return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><p>Video costs $0.08 per second at 480p, $0.10 per second at 768p</p></body></html>');
 const id='req-'+path.basename(path.dirname(path.dirname(job)))+'-'+path.basename(path.dirname(job))+'-'+path.basename(job);
 if(method==='POST'&&url.startsWith('https://queue.fal.run/')){
  if(scene.post_barrier){fs.writeFileSync(scene.post_barrier.entered,'marker/claim persisted; POST response pending');const limit=Date.now()+10000;while(!fs.existsSync(scene.post_barrier.release)){if(Date.now()>limit)throw Error('offline barrier timeout');await new Promise(resolve=>setTimeout(resolve,20));}}
  if(scene.submit_failure)throw Error('simulated uncertain network');return response({request_id:id,...(scene.aux_invalid?{status_url:'https://invalid.example/'}:{})});
 }
 if(method==='GET'&&url.startsWith('https://queue.fal.run/')){
  if(url.includes('/status'))return response({status:scene.pending?'IN_QUEUE':'COMPLETED'});
  const media='https://fal.media/files/'+(model.includes('ideogram')?'clean':model.startsWith('minimax')?'video':'primary');
  return response(model.startsWith('minimax')?{video:{url:media}}:model.includes('ideogram')?{image:{url:media}}:{images:[{url:media}]});
 }
 if(url==='https://fal.media/files/primary')return new Response(fs.readFileSync(scene.primary));
 if(url==='https://fal.media/files/clean')return new Response(fs.readFileSync(scene.clean));
 if(url==='https://fal.media/files/video')return new Response(fs.readFileSync(scene.video));
 throw Error('unexpected offline URL');
};
const result=await runCommand(command,job,{key:scene.key?'synthetic-offline-key':'',fetchImpl});
process.stdout.write(JSON.stringify(result));
'''


class ManagedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sprite-managed-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.source=self.root/'original.png';sheet(self.source,count=2)
        self.primary=self.root/'primary.png';self.clean=self.root/'clean.png';sheet(self.primary);sheet(self.clean)
        self.log=self.root/'network.jsonl';self.scene_path=self.root/'network-fixture.json'
        self.scene=dict(log=str(self.log),primary=str(self.primary),clean=str(self.clean),cleanup_price='0.02',pending=False,key=True)
        self.task=ledger.create_task(self.root/'tasks',dict(task_id='human-test',cap_usd='1',budget_basis='fal_operational_reservations',allowed_operations=list(ledger.OPERATION_KINDS),evidence_ref='synthetic offline authorization'),[dict(target_id='character',source_sha256=sha_file(self.source),action='walk',orientation='left',requirements={'frames':8})])
        self.options=dict(source=str(self.source),action='caminar',orientation='izquierda',strategy='sheet',workspace_root=str(self.root/'runs'),poll_seconds=0,max_cost_usd=1.0,task_manifest=self.task['task_manifest'],target_id='character')

    def transport(self,command,job):
        self.scene_path.write_bytes(encoded(self.scene))
        env={k:os.environ[k] for k in ('SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP') if k in os.environ}
        env['PYTHONDONTWRITEBYTECODE']='1'
        child=subprocess.run([str(NODE),'--input-type=module','-e',NETWORK,TRANSPORT,command,str(job),str(self.scene_path)],capture_output=True,env=env,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr.decode(errors='replace'))
        return json.loads(child.stdout)

    def generate(self,**overrides):return workflow.generate(self.options|overrides,transport=self.transport)
    def read(self,result):return json.loads(Path(result['run_manifest']).read_text('utf-8'))
    def state(self):return ledger.inspect_task(self.task['task_manifest'])
    def calls(self):return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
    def posts(self):return [c for c in self.calls() if c['method']=='POST']

    def test_public_requires_context_without_creating_scope_or_pricing(self):
        result=self.generate(task_manifest=None)
        self.assertEqual(result['block_reason'],'TASK_CONTEXT_REQUIRED',result)
        self.assertFalse((self.root/'runs').exists());self.assertEqual(self.calls(),[])

    def test_history_public_blocks_before_scope_prompt_preparation_or_pricing(self):
        for kind,code in [('pending','TASK_HISTORY_REQUIRED'),('closed','TASK_HISTORY_MECHANISM_CLOSED'),
                          ('unresolved','TASK_HISTORY_UNRESOLVED')]:
            with self.subTest(kind=kind):
                task=history_fixture(self.root/kind,self.source,pending=kind=='pending',unresolved=kind=='unresolved',
                    descriptor=dict(route='sheet',model=workflow.NANO,conditioning='single_source',inputs=[dict(role='source',sha256=sha_file(self.source))]))
                output=self.root/(kind+'-runs')
                result=self.generate(task_manifest=task['task_manifest'],workspace_root=str(output))
                self.assertEqual(result['block_reason'],code,result);self.assertIsNone(result['run_manifest'])
                self.assertFalse(output.exists());self.assertEqual(self.calls(),[])
                if kind=='pending':
                    self.assertEqual(result['history_status'],'pending')
                    self.assertIsNone(result['task_budget']['reserved_total_usd'])
                    self.assertIsNone(result['task_budget']['available_estimate_usd'])

    def test_history_distinct_mechanism_known_id_recovers_through_actual_node(self):
        self.task=history_fixture(self.root/'history',self.source)
        self.options['task_manifest']=self.task['task_manifest'];self.scene['pending']=True
        result=self.generate();self.assertEqual(result['state'],'PENDING',result)
        self.assertEqual(result['history_status'],'complete');before=len(self.calls())
        self.scene['pending']=False;result=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(result['state'],workflow.READY,result)
        self.assertFalse(any(c['method']=='POST' or c['command'].startswith('pricing') for c in self.calls()[before:]))
        self.assertEqual(len(self.posts()),1);self.assertEqual(result['task_budget']['reserved_total_usd'],'0.22')

    def test_history_video_entry_checks_actual_mechanism_before_preparation(self):
        from test_sheet_video import FFMPEG
        for pending,code in [(True,'TASK_HISTORY_REQUIRED'),(False,'TASK_HISTORY_MECHANISM_CLOSED')]:
            task=history_fixture(self.root/('video-'+str(pending)),self.source,pending=pending)
            output=self.root/('video-runs-'+str(pending))
            with patch.object(workflow,'prepare_source',side_effect=AssertionError('history must block before preparation')):
                result=self.generate(task_manifest=task['task_manifest'],workspace_root=str(output),strategy='video',ffmpeg_exe=str(FFMPEG))
            self.assertEqual(result['block_reason'],code,result);self.assertFalse(output.exists());self.assertEqual(self.calls(),[])

    def test_history_price_and_local_work_use_real_read_guard(self):
        self.task=history_fixture(self.root/'history',self.source,local_unknown=True)
        self.options['task_manifest']=self.task['task_manifest'];sheet(self.primary,rgb=True)
        actual=ledger.check_history_context
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            result=self.generate()
        self.assertEqual(result['state'],workflow.READY,result);self.assertEqual(len(self.posts()),2)
        calls=[call.args for call in guarded.call_args_list]
        self.assertTrue(any(len(args)==3 and args[2] and args[2]['route']=='sheet' for args in calls))
        self.assertTrue(any(len(args)==2 or args[2] is None for args in calls))

    def test_held_primary_pricing_rechecks_history_before_reusing_binding(self):
        def lose_key_after_pricing(command,job):
            if command=='execute':self.scene['key']=False
            return self.transport(command,job)
        result=workflow.generate(self.options,transport=lose_key_after_pricing)
        self.assertEqual(result['state'],'KEY_SETUP_REQUIRED',result)
        path,m=workflow._load(result['run_manifest']);a=m['attempts'][0];actual=ledger.check_history_context
        before=len(self.calls())
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            self.assertIsNone(workflow._managed_price(path,m,a,a['jobs']['primary'],self.transport))
        self.assertEqual(len(self.calls()),before)
        self.assertEqual([c.args for c in guarded.call_args_list],[(self.task['task_manifest'],'character',
            dict(route='sheet',model=workflow.NANO,conditioning='single_source',inputs=[dict(role='source',sha256=sha_file(self.source))]))])

    def test_history_video_known_cleanup_recovers_before_new_local_processing(self):
        from test_sheet_video import VideoTests,FFMPEG
        fixture=VideoTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        self.scene['video']=str(fixture.video())
        self.task=history_fixture(self.root/'history',self.source,descriptor=dict(route='sheet',model=workflow.NANO,
            conditioning='single_source',inputs=[dict(role='source',sha256=sha_file(self.source))]))
        self.options['task_manifest']=self.task['task_manifest']
        def pending_cleanup(command,job):
            response=self.video_transport(command,job)
            if command=='execute' and job.name=='primary':self.scene['pending']=True
            return response
        result=workflow.generate(self.options|{'strategy':'video','ffmpeg_exe':str(FFMPEG)},transport=pending_cleanup)
        self.assertEqual(result['state'],'PENDING',result);manifest=self.read(result)
        self.assertTrue(manifest['attempts'][0]['jobs']['cleanup']['request_id'])
        before=len(self.calls());self.scene['pending']=False
        with patch.object(workflow,'prepare_source',side_effect=AssertionError('known video must reuse validated prepared bytes')):
            result=workflow.resume(result['run_manifest'],transport=self.video_transport,poll_seconds=0)
        self.assertEqual(result['state'],workflow.READY,result);self.assertEqual(len(self.posts()),2)
        self.assertFalse(any(c['method']=='POST' or c['command'].startswith('pricing') for c in self.calls()[before:]))

    def test_ordinary_results_do_not_add_history_status(self):
        result=self.generate();self.assertEqual(result['state'],workflow.READY,result)
        self.assertNotIn('history_status',result);self.assertNotIn('history_status',result['task_budget'])

    def test_actual_node_bridge_sheet_plan_release_and_digest_separation(self):
        result=self.generate();self.assertEqual(result['state'],workflow.READY,result)
        manifest=self.read(result);attempt=manifest['attempts'][0];job=attempt['jobs']['primary'];state=self.state()
        self.assertEqual(attempt['attempt_id'],manifest['run_id']+':attempt-01')
        self.assertNotEqual(job['config_sha256'],job['transport_config_sha256'])
        self.assertEqual(job['config_file_sha256'],sha_file(Path(job['path'])/'config.json'))
        op=state['operations'][job['operation_id']];self.assertEqual(op['outcomes'][-1]['status'],'completed')
        self.assertEqual(op['execution_binding']['config_sha256'],job['transport_config_sha256'])
        cleanup=state['operations'][attempt['attempt_id']+':cleanup'];self.assertEqual(cleanup['status'],'released')
        self.assertIn('deferred_config',cleanup['reservation']);self.assertNotIn('config_sha256',cleanup['reservation'])
        self.assertEqual(state['reserved_total_usd'],'0.12');self.assertEqual(result['task_budget']['reserved_total_usd'],'0.12')
        self.assertEqual(len(self.posts()),1)
        before=len(self.calls());again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again);self.assertEqual(len(self.calls()),before)

    def test_complete_pair_over_cap_has_no_post_or_partial_reservation(self):
        result=self.generate(max_cost_usd=.13)
        self.assertEqual(result['block_reason'],'COST_CAP_EXCEEDED',result)
        self.assertEqual(self.posts(),[]);self.assertEqual(self.state()['operations'],{})

    def test_opaque_primary_materializes_same_cleanup_operation(self):
        sheet(self.primary,rgb=True)
        result=self.generate();self.assertEqual(result['state'],workflow.READY,result)
        attempt=self.read(result)['attempts'][0];state=self.state();cleanup=state['operations'][attempt['attempt_id']+':cleanup']
        self.assertEqual(cleanup['outcomes'][-1]['status'],'completed');self.assertEqual(len(self.posts()),2)
        provenance=json.loads((Path(attempt['jobs']['cleanup']['path'])/'source-provenance.json').read_text())
        self.assertEqual(provenance['kind'],'primary_raw_png');self.assertEqual(provenance['evidence']['parent_request_id'],attempt['jobs']['primary']['request_id'])
        self.assertEqual(state['reserved_total_usd'],'0.14');self.assertIn('deferred_config',cleanup['reservation'])

    def test_missing_key_creates_real_bound_run_without_marker(self):
        self.scene['key']=False;result=self.generate()
        self.assertEqual(result['state'],'KEY_SETUP_REQUIRED',result)
        self.assertTrue(Path(result['run_manifest']).is_file());self.assertEqual(len(self.state()['runs']),1)
        self.assertFalse(any(op['submission_started'] for op in self.state()['operations'].values()));self.assertEqual(self.posts(),[])

    def test_known_id_resume_does_not_reprice_or_post(self):
        self.scene['pending']=True;result=self.generate();self.assertEqual(result['state'],'PENDING',result)
        before=len(self.calls());self.scene['pending']=False
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again)
        self.assertFalse(any(c['command'].startswith('pricing') or c['method']=='POST' for c in self.calls()[before:]))

    def test_removed_task_or_manifest_task_ref_cannot_select_legacy(self):
        self.scene['pending']=True;result=self.generate();manifest=self.read(result);manifest['request'].pop('task_ref',None)
        Path(result['run_manifest']).write_bytes(encoded(manifest));before=len(self.calls())
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],'BLOCKED');self.assertEqual(len(self.calls()),before)

    def test_shared_strategy_attempts_are_not_reset_by_uuid_or_auto(self):
        first=self.generate();second=self.generate(description='different prompt')
        self.assertEqual(first['state'],workflow.READY,first);self.assertEqual(second['state'],workflow.READY,second)
        third=self.generate();self.assertEqual(third['block_reason'],'TASK_STRATEGY_EXHAUSTED',third)
        self.assertEqual(len(self.posts()),2);self.assertEqual(len(self.state()['strategies']),1)

    def test_legacy_generation_remains_an_explicit_internal_entry(self):
        self.assertTrue(callable(getattr(workflow,'_generate_legacy',None)))
        result=workflow._generate_legacy(self.options,transport=Provider())
        self.assertEqual(result['state'],workflow.READY,result);self.assertNotIn('task_ref',self.read(result)['request'])

    def test_cleanup_price_increase_blocks_then_same_binding_recovers(self):
        sheet(self.primary,rgb=True)
        def price_jump(command,job):
            result=self.transport(command,job)
            if command=='execute' and job.name=='primary':self.scene['cleanup_price']='0.9'
            return result
        result=workflow.generate(self.options|{'max_cost_usd':.2},transport=price_jump)
        self.assertEqual(result['block_reason'],'COST_CAP_EXCEEDED',result);self.assertEqual(len(self.posts()),1)
        self.assertEqual(result['spent_estimate_usd'],.14)
        a=self.read(result)['attempts'][0];op=self.state()['operations'][a['attempt_id']+':cleanup'];binding=op['execution_binding']
        self.assertEqual(op['status'],'held');self.scene['cleanup_price']='0.03'
        result=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(result['state'],workflow.READY,result);self.assertEqual(len(self.posts()),2)
        op=self.state()['operations'][a['attempt_id']+':cleanup'];self.assertEqual(op['execution_binding'],binding)
        self.assertEqual(op['current_quote']['amount_usd'],'0.03');self.assertEqual(op['reservation']['amount_usd'],'0.02')

    def test_real_node_only_id_recovers_after_facade_interruption(self):
        interrupted=[False]
        def crash(command,job):
            response=self.transport(command,job)
            if command=='execute' and not interrupted[0]:interrupted[0]=True;raise RuntimeError('crash after actual Node durable result')
            return response
        self.scene['pending']=True;result=workflow.generate(self.options,transport=crash)
        op=next(op for op in self.state()['operations'].values() if op['kind']=='provider_animation')
        self.assertTrue(op['submission_started']);self.assertIsNone(op['provider_request_id']);before=len(self.calls())
        self.scene['pending']=False;again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again)
        self.assertEqual(len(self.posts()),1);self.assertFalse(any(c['command'].startswith('pricing') for c in self.calls()[before:]))
        self.assertIsNotNone(self.state()['operations'][op['operation_id']]['provider_request_id'])

    def test_real_ledger_only_id_repairs_node_without_pricing(self):
        self.scene['pending']=True;result=self.generate();job=self.read(result)['attempts'][0]['jobs']['primary']
        (Path(job['path'])/'job-state.json').unlink();before=len(self.calls());self.scene['pending']=False
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again);self.assertEqual(len(self.posts()),1)
        self.assertFalse(any(c['command'].startswith('pricing') for c in self.calls()[before:]))

    def test_marker_without_id_or_claim_remains_unknown(self):
        self.scene['submit_failure']=True;result=self.generate();self.assertEqual(result['state'],'SUBMISSION_STATUS_UNKNOWN',result)
        job=self.read(result)['attempts'][0]['jobs']['primary']
        for name in ('job-state.json','submission.claim'):
            candidate=Path(job['path'])/name
            if candidate.exists():candidate.unlink()
        self.scene['submit_failure']=False;before=len(self.calls())
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],'SUBMISSION_STATUS_UNKNOWN',again);self.assertEqual(len(self.calls()),before)
        self.assertEqual(self.state()['reserved_total_usd'],'0.14')

    def test_auxiliary_url_failure_keeps_exact_known_id_before_return(self):
        self.scene['aux_invalid']=True;result=self.generate();self.assertEqual(result['block_reason'],'AUXILIARY_QUEUE_URL_INVALID',result)
        job=self.read(result)['attempts'][0]['jobs']['primary'];op=self.state()['operations'][job['operation_id']]
        self.assertEqual(job['request_id'],op['provider_request_id']);self.assertIsNotNone(op['provider_request_id'])
        self.scene['aux_invalid']=False;again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again);self.assertEqual(len(self.posts()),1)

    def test_ledger_deletion_and_id_conflict_block_before_new_submit(self):
        self.scene['pending']=True;result=self.generate();job=self.read(result)['attempts'][0]['jobs']['primary']
        state_path=Path(job['path'])/'job-state.json';state=json.loads(state_path.read_text());state['request_id']='wrong-known-id';state_path.write_bytes(encoded(state))
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['block_reason'],'TASK_CONTEXT_CONFLICT',again);self.assertEqual(len(self.posts()),1)
        Path(self.task['task_manifest']).unlink();before=len(self.calls())
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],'BLOCKED');self.assertEqual(len(self.calls()),before)

    def test_actual_receipt_archive_reparse_blocks_coherent_body_tamper(self):
        def tamper(command,job):
            result=self.transport(command,job)
            if command=='pricing-plan' and result['state']=='PRICED':
                receipt_path=Path(result['quote']['receipt_path']);receipt=json.loads(receipt_path.read_text());body=receipt_path.parent/receipt['sources'][0]['archive_file']
                value=json.loads(body.read_text());value['prices'][0]['unit_price']='0.01';body.write_bytes(encoded(value));receipt['sources'][0]['body_sha256']=sha_file(body)
                receipt_path.write_bytes(encoded(receipt));result['quote']['receipt_sha256']=sha_file(receipt_path)
            return result
        result=workflow.generate(self.options,transport=tamper)
        self.assertEqual(result['block_reason'],'PRICE_RECEIPT_INVALID',result);self.assertEqual(self.posts(),[]);self.assertEqual(self.state()['operations'],{})

    def report(self,result,failed='scale'):
        from test_sheet_review import VisualReviewTests
        return VisualReviewTests.report(self,result,failed)

    def test_orphan_reject_reconciles_and_requires_structured_correction(self):
        result=self.generate();manifest=self.read(result);receipt=Path(result['run_manifest']).parent/'attempt-01/visual-review-001.json'
        report=self.report(result);receipt.write_bytes(encoded(report));before=len(self.calls())
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.VISUAL_REJECTED,again);self.assertEqual(len(self.calls()),before)
        attempt=next(iter(self.state()['attempts'].values()));self.assertEqual(attempt['review']['decision'],'reject')
        blocked=workflow.revise(result['run_manifest'],'preserve torso scale',transport=self.transport,poll_seconds=0)
        self.assertEqual(blocked['block_reason'],'TASK_CORRECTION_REQUIRED',blocked)
        correction=dict(defect=attempt['review']['defects'][0],plan='Preserve the torso and head size across poses.',evidence='synthetic correction authorization')
        revised=workflow.revise(result['run_manifest'],'preserve torso scale',correction=correction,transport=self.transport,poll_seconds=0)
        self.assertEqual(revised['state'],workflow.READY,revised);self.assertEqual(len(self.posts()),2)
        self.assertEqual(manifest['authorization'],self.read(revised)['authorization'])

    def test_repeat_identical_reject_is_idempotent(self):
        result=self.generate();review=self.root/'review.json';review.write_bytes(encoded(self.report(result)))
        first=workflow.record_visual_review(result['run_manifest'],review);self.assertEqual(first['state'],workflow.VISUAL_REJECTED,first)
        second=workflow.record_visual_review(result['run_manifest'],review);self.assertEqual(second['state'],workflow.VISUAL_REJECTED,second)
        self.assertEqual(len(list(Path(result['run_manifest']).parent.glob('attempt-01/visual-review-*.json'))),1)

    def test_orphan_pass_without_alignment_never_grants_delivery(self):
        result=self.generate();receipt=Path(result['run_manifest']).parent/'attempt-01/visual-review-001.json';receipt.write_bytes(encoded(self.report(result,None)))
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertFalse(again['delivery_allowed']);self.assertEqual(again['block_reason'],'ALIGNMENT_REQUIRED',again)
        self.assertNotIn('review',next(iter(self.state()['attempts'].values())))

    def test_unknown_requirements_ambiguous_targets_and_edge_only_permission(self):
        original=self.task
        for case in ('requirement','ambiguous','permission'):
            with self.subTest(case=case):
                target=dict(target_id='character',source_sha256=sha_file(self.source),action='walk',orientation='left',requirements={'unrepresented_policy':True} if case=='requirement' else {'frames':8})
                targets=[target,target|{'target_id':'other'}] if case=='ambiguous' else [target]
                self.task=ledger.create_task(self.root/case,dict(task_id=case,cap_usd='1',budget_basis='fal_operational_reservations',allowed_operations=['alpha_edge_cleanup'] if case=='permission' else list(ledger.OPERATION_KINDS),evidence_ref='synthetic'),targets)
                result=self.generate(task_manifest=self.task['task_manifest'],target_id=None if case=='ambiguous' else 'character')
                self.assertEqual(result['block_reason'],{'requirement':'TASK_POLICY_UNSUPPORTED','ambiguous':'TASK_TARGET_AMBIGUOUS','permission':'OPERATION_NOT_AUTHORIZED'}[case],result)
                self.assertEqual(self.calls(),[])
        self.task=original

    def video_transport(self,command,job):
        if command=='execute' and job.name=='cleanup':
            config=json.loads((job/'config.json').read_text())
            with Image.open(config['source']['path']) as image:out=image.convert('RGB')
            delta=ImageChops.difference(out,Image.new('RGB',out.size,(110,110,110)))
            out.putalpha(delta.convert('L').point(lambda value:255 if value>20 else 0));out.save(self.clean)
        return self.transport(command,job)

    def test_actual_video_preparation_sampling_and_cleanup_provenance(self):
        from test_sheet_video import VideoTests,FFMPEG
        fixture=VideoTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        self.scene['video']=str(fixture.video())
        result=workflow.generate(self.options|{'strategy':'video','ffmpeg_exe':str(FFMPEG)},transport=self.video_transport)
        self.assertEqual(result['state'],workflow.READY,result);attempt=self.read(result)['attempts'][0]
        for role,kind in [('primary','prepared_source_v1'),('cleanup','video_sample_grid_v1')]:
            record=json.loads((Path(attempt['jobs'][role]['path'])/'source-provenance.json').read_text());self.assertEqual(record['kind'],kind)
            self.assertEqual(record['original_source_sha256'],sha_file(self.source))
        self.assertEqual(attempt['sampling']['selection']['analysis_frame_indices'],[24,27,30,33,36,39,42,45]);self.assertEqual(len(self.posts()),2)
        # Mutable facade metadata and grid hashes cannot replace actual process verification.
        selection_path=Path(attempt['sampling']['selection_path']);selection=json.loads(selection_path.read_text());selection['analysis_frame_indices'][0]=25
        selection_path.write_bytes(encoded(selection));attempt['sampling']['selection']=selection
        manifest=self.read(result);manifest['attempts'][0]=attempt;Path(result['run_manifest']).write_bytes(encoded(manifest));before=len(self.calls())
        blocked=workflow.resume(result['run_manifest'],transport=self.video_transport,poll_seconds=0)
        self.assertEqual(blocked['state'],'BLOCKED',blocked);self.assertEqual(len(self.calls()),before)

    def test_prepared_pixels_cannot_be_replaced_by_mutable_metadata(self):
        from test_sheet_video import FFMPEG
        state=self.state();self.task=ledger.create_task(self.root/'larger-test-cap',state['authorization']|{'cap_usd':'2'},list(state['targets'].values()))
        self.options['task_manifest']=self.task['task_manifest']
        self.scene['pending']=True
        for pixel_art in ('true','false'):
            with self.subTest(pixel_art=pixel_art):
                result=self.generate(strategy='video',ffmpeg_exe=str(FFMPEG),pixel_art=pixel_art)
                self.assertEqual(result['state'],'PENDING',result);manifest=self.read(result);prepared=manifest['attempts'][0]['prepared'];path=Path(prepared['path'])
                with Image.open(path) as image:altered=image.copy()
                altered.putpixel((0,0),(1,2,3));altered.save(path);prepared['sha256']=sha_file(path);Path(result['run_manifest']).write_bytes(encoded(manifest))
                before=len(self.calls());blocked=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
                self.assertEqual(blocked['state'],'BLOCKED',blocked);self.assertEqual(len(self.calls()),before)

    def test_cycle_heuristic_is_persisted_as_technical_not_visual_failure(self):
        from test_sheet_video import VideoTests,FFMPEG
        fixture=VideoTests();fixture.setUp();self.addCleanup(fixture.tearDown);self.scene['video']=str(fixture.video(flat=True))
        options=self.options|{'strategy':'video','ffmpeg_exe':str(FFMPEG)}
        result=workflow.generate(options,transport=self.video_transport)
        self.assertEqual(result['state'],'TECHNICAL_REJECTED',result);attempt=next(iter(self.state()['attempts'].values()))
        self.assertEqual(attempt['review']['defects'],['technical_cycle:CYCLE_UNRELIABLE_ZERO_MOTION'])
        self.assertEqual(len(self.posts()),1);self.assertEqual(self.state()['reserved_total_usd'],'0.6')
        before=len(self.calls());again=workflow.resume(result['run_manifest'],transport=self.video_transport,poll_seconds=0)
        self.assertEqual(again['state'],'TECHNICAL_REJECTED',again);self.assertEqual(len(self.calls()),before)
        blocked=workflow.revise(result['run_manifest'],'show alternating contacts',transport=self.video_transport,poll_seconds=0)
        self.assertEqual(blocked['block_reason'],'TASK_CORRECTION_REQUIRED',blocked)

    def test_two_real_node_execute_processes_share_one_marker_and_post(self):
        def before_execute(command,job):
            if command=='execute':raise RuntimeError('pause after real pricing/reservation/binding')
            return self.transport(command,job)
        result=workflow.generate(self.options,transport=before_execute);job=self.read(result)['attempts'][0]['jobs']['primary']
        self.scene['pending']=True;self.scene_path.write_bytes(encoded(self.scene))
        env={k:os.environ[k] for k in ('SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP') if k in os.environ};env['PYTHONDONTWRITEBYTECODE']='1'
        args=[str(NODE),'--input-type=module','-e',NETWORK,TRANSPORT,'execute',job['path'],str(self.scene_path)]
        children=[subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env) for _ in range(2)]
        responses=[]
        for child in children:
            stdout,stderr=child.communicate(timeout=30);self.assertEqual(child.returncode,0,stderr);responses.append(json.loads(stdout))
        self.assertEqual(len(self.posts()),1,responses)
        self.assertTrue(any(response['state']=='IN_QUEUE' for response in responses),responses)
        self.assertEqual(self.state()['reserved_total_usd'],'0.14')
        self.assertTrue(self.state()['operations'][job['operation_id']]['submission_started'])
        self.scene['pending']=False;again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again);self.assertEqual(len(self.posts()),1)

    def test_shared_task_cap_blocks_second_run_atomically(self):
        state=self.state();self.task=ledger.create_task(self.root/'small-shared-cap',state['authorization']|{'cap_usd':'0.2'},list(state['targets'].values()))
        self.options['task_manifest']=self.task['task_manifest']
        first=self.generate();self.assertEqual(first['state'],workflow.READY,first)
        second=self.generate(description='same strategy, different prompt')
        self.assertEqual(second['block_reason'],'TASK_BUDGET_EXCEEDED',second);self.assertEqual(len(self.posts()),1)
        self.assertEqual(len(self.state()['operations']),2);self.assertEqual(self.state()['reserved_total_usd'],'0.12')

    def test_missing_key_after_reserve_does_not_mark_and_resumes_same_plan(self):
        def remove_key(command,job):
            if command=='execute':self.scene['key']=False
            return self.transport(command,job)
        result=workflow.generate(self.options,transport=remove_key)
        self.assertEqual(result['state'],'KEY_SETUP_REQUIRED',result);self.assertEqual(self.state()['reserved_total_usd'],'0.14')
        self.assertTrue(all(not op['submission_started'] for op in self.state()['operations'].values()));self.assertEqual(self.posts(),[])
        self.scene['key']=True;again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again);self.assertEqual(len(self.posts()),1)

    def test_stale_receipt_before_hook_refreshes_same_plan_without_marker(self):
        stale=[False];no_marker=[]
        def expire(command,job):
            if command=='execute' and not stale[0]:
                stale[0]=True;receipt_path=next((job/'pricing-receipts').glob('*.receipt.json'));receipt=json.loads(receipt_path.read_text())
                retrieved=datetime.now(timezone.utc)-timedelta(minutes=16)
                receipt['retrieved_at']=retrieved.isoformat().replace('+00:00','Z');receipt['expires_at']=(retrieved+timedelta(minutes=15)).isoformat().replace('+00:00','Z')
                for source in receipt['sources']:source['retrieved_at']=receipt['retrieved_at']
                replacement=receipt_path.parent/f'{int(retrieved.timestamp()*1000):016d}-{receipt["receipt_id"]}.receipt.json';replacement.write_bytes(encoded(receipt));receipt_path.unlink()
                result=self.transport(command,job)
                no_marker.append(all(not op['submission_started'] for op in self.state()['operations'].values()))
                self.assertEqual(result['block_reason'],'PRICE_STALE',result);return result
            return self.transport(command,job)
        result=workflow.generate(self.options,transport=expire)
        self.assertEqual(result['state'],workflow.READY,result);self.assertEqual(no_marker,[True]);self.assertEqual(len(self.posts()),1)
        op=next(op for op in self.state()['operations'].values() if op['kind']=='provider_animation')
        self.assertEqual(len(op['quote_history']),2)

    def test_validated_scope_without_bind_recovers_exactly_on_resume(self):
        # Actual bind failure after durable scope/manifest, without changing the
        # ledger core or claiming a fake successful bind.
        from unittest.mock import patch
        original=workflow._task_state
        once=[False]
        def interruption(path,manifest,**kwargs):
            if kwargs.get('recover_bind') and not once[0]:once[0]=True;raise OSError('interrupted before bind')
            return original(path,manifest,**kwargs)
        with patch.object(workflow,'_task_state',side_effect=interruption):result=self.generate()
        self.assertEqual(self.state()['runs'],{});self.assertEqual(self.calls(),[])
        self.assertEqual(workflow.inspect(result['run_manifest'])['state'],'BLOCKED');self.assertEqual(self.state()['runs'],{})
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.READY,again);self.assertEqual(len(self.state()['runs']),1)

    def test_completed_primary_still_requires_immutable_archived_plan(self):
        result=self.generate();plan=Path(result['run_manifest']).parent/'attempt-01/cleanup-plan/plan.json'
        value=json.loads(plan.read_text());value['source_limits']['max_width']=4095;plan.write_bytes(encoded(value));before=len(self.calls())
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['block_reason'],'TASK_PLAN_INVALID',again);self.assertEqual(len(self.calls()),before)

    def test_durable_reservation_before_manifest_recovers_from_exact_ledger(self):
        class AbruptTermination(BaseException):pass
        writer=workflow.atomic_json;interrupted=[False]
        def crash_after_write(path,value):
            writer(path,value)
            if Path(path).name=='reservation.json' and not interrupted[0]:
                interrupted[0]=True;raise AbruptTermination('durable mirror, manifest not yet saved')
        with patch.object(workflow,'atomic_json',side_effect=crash_after_write):
            with self.assertRaises(AbruptTermination):self.generate()
        manifest_path=next((self.root/'runs').glob('*/run-manifest.json'));manifest=json.loads(manifest_path.read_text());job=manifest['attempts'][0]['jobs']['primary']
        self.assertEqual(job['reserved_usd'],0);self.assertEqual(json.loads((Path(job['path'])/'reservation.json').read_text())['reserved_usd'],.12)
        self.assertEqual(self.state()['reserved_total_usd'],'0.14');self.assertEqual(self.posts(),[])
        before={str(p):(p.stat().st_mtime_ns,sha_file(p)) for p in manifest_path.parent.rglob('*') if p.is_file()}
        inspected=workflow.inspect(manifest_path);self.assertEqual(inspected['block_reason'],'RESERVATION_TAMPERED',inspected)
        self.assertEqual(before,{str(p):(p.stat().st_mtime_ns,sha_file(p)) for p in manifest_path.parent.rglob('*') if p.is_file()})
        calls_before=len(self.calls());result=workflow.resume(manifest_path,transport=self.transport,poll_seconds=0)
        self.assertEqual(result['state'],workflow.READY,result);self.assertEqual(len(self.posts()),1)
        self.assertFalse(any(c['command'].startswith('pricing') for c in self.calls()[calls_before:]))
        current=self.read(result)['attempts'][0]['jobs']['primary'];self.assertEqual(current['reserved_usd'],.12)
        self.assertEqual(json.loads((Path(current['path'])/'reservation.json').read_text()),{'reserved_usd':.12})

    def test_unknown_then_late_node_id_recovers_through_real_barrier(self):
        def before_execute(command,job):
            if command=='execute':raise RuntimeError('pause after actual plan/binding')
            return self.transport(command,job)
        result=workflow.generate(self.options,transport=before_execute);job=self.read(result)['attempts'][0]['jobs']['primary']
        entered=self.root/'post-entered';release=self.root/'post-release'
        self.scene.update(pending=True,post_barrier={'entered':str(entered),'release':str(release)})
        self.scene_path.write_bytes(encoded(self.scene));env={k:os.environ[k] for k in ('SystemRoot','WINDIR','USERPROFILE','HOME','HOMEDRIVE','HOMEPATH','TEMP','TMP') if k in os.environ};env['PYTHONDONTWRITEBYTECODE']='1'
        args=[str(NODE),'--input-type=module','-e',NETWORK,TRANSPORT,'execute',job['path'],str(self.scene_path)]
        child=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        try:
            deadline=time.monotonic()+10
            while not entered.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(entered.exists(),'actual Node did not reach POST-response barrier')
            unknown=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
            self.assertEqual(unknown['state'],'SUBMISSION_STATUS_UNKNOWN',unknown)
            release.touch();stdout,stderr=child.communicate(timeout=20);self.assertEqual(child.returncode,0,stderr)
            accepted=json.loads(stdout);self.assertEqual(accepted['state'],'IN_QUEUE',accepted)
            operation=self.state()['operations'][job['operation_id']];self.assertTrue(operation['submission_started']);self.assertIsNone(operation['provider_request_id'])
            self.scene['pending']=False;calls_before=len(self.calls())
            recovered=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
            self.assertEqual(recovered['state'],workflow.READY,recovered);self.assertEqual(len(self.posts()),1)
            self.assertFalse(any(c['method']=='POST' or c['command'].startswith('pricing') for c in self.calls()[calls_before:]))
            self.assertEqual(self.state()['operations'][job['operation_id']]['provider_request_id'],accepted['request_id'])
        finally:
            release.touch()
            if child.poll() is None:child.communicate(timeout=20)

    def repair_entry(self,kind):
        framing=kind=='position_x'
        keys=('all_frames_inspected','only_horizontal_body_translation','no_clipping_or_missing_pixels','no_y_scale_pose_anatomy_change') if framing else ('all_frames_inspected','only_exterior_alpha_edge_residue','no_missing_subject_pixels','no_rgb_anatomy_pose_change')
        return dict(check='framing' if framing else 'background',kind=kind,frame_findings=[dict(frame=2,notes='Observed the same complete torso point and exterior contour in this fixture.')],attestations=dict.fromkeys(keys,True))

    def test_repair_classification_maps_only_explicit_checks_and_binds_receipt(self):
        result=self.generate();manifest=self.read(result);report=self.report(result,'scale')
        for name in ('framing','background'):report['checks'][name]['status']='fail'
        report['repairable_defects']=[self.repair_entry('position_x'),self.repair_entry('alpha_edge')]
        expected=[({'framing':'position_x','background':'alpha_edge'}.get(name,name))+':'+report['checks'][name]['notes'].strip() for name in sorted(report['checks']) if report['checks'][name]['status']=='fail']
        self.assertEqual(workflow._validate_visual_report(manifest,report),expected)
        review=self.root/'classified-review.json';review.write_bytes(encoded(report));recorded=workflow.record_visual_review(result['run_manifest'],review)
        self.assertEqual(recorded['state'],workflow.VISUAL_REJECTED,recorded);current=self.read(recorded);receipt=Path(current['visual_review']['path'])
        attempt=next(iter(self.state()['attempts'].values()))
        archived=json.loads(receipt.read_text());archived_expected=[({'framing':'position_x','background':'alpha_edge'}.get(name,name))+':'+check['notes'].strip() for name,check in archived['checks'].items() if check['status']=='fail']
        self.assertEqual(attempt['review']['defects'],archived_expected);self.assertEqual(sha_file(receipt),current['visual_review']['sha256'])
        self.assertEqual(json.loads(receipt.read_text()),report);self.assertEqual(report['artifact_set_sha256'],result['artifact_set_sha256'])

    def test_repair_classification_absent_empty_and_text_do_not_reclassify(self):
        result=self.generate();manifest=self.read(result);report=self.report(result,'framing')
        report['checks']['framing']['notes']='Observed X alpha drift and clipping; words alone cannot classify repairs.'
        expected=['framing:'+report['checks']['framing']['notes']]
        self.assertEqual(workflow._validate_visual_report(manifest,report),expected)
        report['repairable_defects']=[];self.assertEqual(workflow._validate_visual_report(manifest,report),expected)

    def test_repair_classification_strict_negative_table_and_stale_precedence(self):
        result=self.generate();manifest=self.read(result);valid=self.report(result,'framing');valid['repairable_defects']=[self.repair_entry('position_x')]
        cases=[]
        for value in (None,{},'position_x',[1]):
            report=copy.deepcopy(valid);report['repairable_defects']=value;cases.append(report)
        for mutate in (
            lambda e:e.update(extra=True),lambda e:e.update(kind='alpha_edge'),lambda e:e.update(check='scale'),
            lambda e:e.update(frame_findings=[]),lambda e:e.update(frame_findings=[{'frame':True,'notes':'Observed complete torso reference.'}]),
            lambda e:e.update(frame_findings=[{'frame':0,'notes':'Observed complete torso reference.'}]),
            lambda e:e.update(frame_findings=[{'frame':9,'notes':'Observed complete torso reference.'}]),
            lambda e:e.update(frame_findings=[{'frame':2,'notes':'short'}]),
            lambda e:e['frame_findings'][0].update(extra=True),
            lambda e:e['frame_findings'].append(copy.deepcopy(e['frame_findings'][0])),
            lambda e:e['attestations'].update(all_frames_inspected=False),
            lambda e:e['attestations'].update(all_frames_inspected=1),
            lambda e:e['attestations'].pop('all_frames_inspected'),
            lambda e:e['attestations'].update(extra=True)):
            report=copy.deepcopy(valid);mutate(report['repairable_defects'][0]);cases.append(report)
        duplicate=copy.deepcopy(valid);duplicate['repairable_defects']*=2;cases.append(duplicate)
        passed=copy.deepcopy(valid);passed['checks']['framing']['status']='pass';passed['decision']='pass';cases.append(passed)
        for report in cases:
            with self.subTest(report=report):
                with self.assertRaises(WorkflowError) as error:workflow._validate_visual_report(manifest,report)
                self.assertEqual(error.exception.code,'VISUAL_REVIEW_INVALID')
        stale=copy.deepcopy(valid);stale['artifact_set_sha256']='0'*64;stale['repairable_defects']=None
        with self.assertRaises(WorkflowError) as error:workflow._validate_visual_report(manifest,stale)
        self.assertEqual(error.exception.code,'VISUAL_REVIEW_STALE')

    def logical_child(self,parent,route='body_x_alignment',name='logical-child'):
        """Ledger/projection fixture only: no claimed pixel transform or art PASS."""
        parent_attempt=parent['attempts'][-1];child_root=self.root/name;run_id=name;attempt_id=run_id+':attempt-01'
        child=copy.deepcopy(parent);child.update(run_id=run_id,source=parent['source']|{'path':str(child_root/'source.png')},attempts=[dict(index=1,attempt_id=attempt_id,jobs={})])
        ledger.bind_run(self.task['task_manifest'],dict(run_id=run_id,target_id='character',scope_sha256=parent['authorization_scope_sha256'],manifest_path=str(child_root/'run-manifest.json'),parent_run_id=parent['run_id']))
        descriptor=dict(route=route,model='body_x_preserve_y_v1' if route=='body_x_alignment' else 'alpha_edge_cleanup',conditioning='local',inputs=[dict(role='source',sha256=sha_file(self.source))])
        ledger.begin_attempt(self.task['task_manifest'],run_id,attempt_id,descriptor,source_attempt=dict(run_id=parent['run_id'],attempt_id=parent_attempt['attempt_id']))
        return child

    def test_live_effective_defects_block_previously_passed_logical_child(self):
        result=self.generate();parent=self.read(result);attempt=parent['attempts'][0]
        ledger.record_review(self.task['task_manifest'],parent['run_id'],attempt['attempt_id'],'pass',[],'logical fixture only; not validated art')
        child=self.logical_child(parent);ledger.record_review(self.task['task_manifest'],child['run_id'],child['attempts'][0]['attempt_id'],'pass',[],'logical child fixture only; no pixels/proof asserted')
        ledger.record_review(self.task['task_manifest'],parent['run_id'],attempt['attempt_id'],'reject',['identity:actual logical revocation fixture'],'logical late rejection fixture')
        output=workflow._result(workflow.DELIVERABLE,manifest=child)
        self.assertFalse(output['delivery_allowed'],output);self.assertEqual(output['block_reason'],'TASK_SOURCE_DEFECT_UNRESOLVED',output)
        self.assertEqual(output['effective_defects'],['identity:actual logical revocation fixture'])

    def test_empty_defects_without_effective_pass_cannot_grant_delivery(self):
        result=self.generate();output=workflow._result(workflow.DELIVERABLE,result['run_manifest'],self.read(result))
        self.assertFalse(output['delivery_allowed'],output);self.assertEqual(output['block_reason'],'VISUAL_REVIEW_REQUIRED')

    def test_reconciliation_requires_exact_canonical_pass_receipt(self):
        result=self.generate();manifest=self.read(result);attempt=manifest['attempts'][0]
        report=self.report(result,None);receipt=Path(result['run_manifest']).parent/'attempt-01/visual-review-001.json';receipt.write_bytes(encoded(report))
        ledger.record_review(self.task['task_manifest'],manifest['run_id'],attempt['attempt_id'],'pass',[],'logical canonical evidence has no matching receipt')
        reconciled=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(reconciled['block_reason'],'VISUAL_REVIEW_TAMPERED',reconciled)

    def test_classified_reject_revokes_logical_pass_without_alignment_claim(self):
        result=self.generate();manifest=self.read(result);directory=Path(result['run_manifest']).parent/'attempt-01'
        passed=self.report(result,None);first=directory/'visual-review-001.json';first.write_bytes(encoded(passed))
        # Logical history fixture only: this does not claim that the unaligned
        # generic parent passed a real visual/alignment contract.
        ledger.record_review(self.task['task_manifest'],manifest['run_id'],manifest['attempts'][0]['attempt_id'],'pass',[],workflow._review_evidence(first,manifest))
        rejected=self.report(result,'framing');rejected['repairable_defects']=[self.repair_entry('position_x')]
        second=directory/'visual-review-002.json';second.write_bytes(encoded(rejected))
        current=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(current['state'],workflow.VISUAL_REJECTED,current);self.assertFalse(current['delivery_allowed'])
        attempt=next(iter(self.state()['attempts'].values()));self.assertEqual([review['decision'] for review in attempt['review_history']],['pass','reject'])
        self.assertEqual(attempt['review']['defects'],['position_x:'+rejected['checks']['framing']['notes']])
        self.assertTrue(first.is_file());self.assertTrue(second.is_file())
        again=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(again['state'],workflow.VISUAL_REJECTED,again);self.assertEqual(len(next(iter(self.state()['attempts'].values()))['review_history']),2)

    def test_terminal_generic_reject_cannot_be_reinterpreted_as_repairable(self):
        result=self.generate();report=self.report(result,'framing');review=self.root/'review.json';review.write_bytes(encoded(report))
        rejected=workflow.record_visual_review(result['run_manifest'],review);self.assertEqual(rejected['state'],workflow.VISUAL_REJECTED,rejected)
        before=self.state();report['repairable_defects']=[self.repair_entry('position_x')];review.write_bytes(encoded(report))
        blocked=workflow.record_visual_review(result['run_manifest'],review)
        self.assertEqual(blocked['block_reason'],'TASK_REVIEW_CONFLICT',blocked);self.assertEqual(self.state()['sequence'],before['sequence'])
        self.assertEqual(len(list(Path(result['run_manifest']).parent.glob('attempt-01/visual-review-*.json'))),1)

    def test_canonical_pass_selection_and_unaligned_observation_cannot_repass(self):
        result=self.generate();manifest=self.read(result);directory=Path(result['run_manifest']).parent/'attempt-01'
        first_report=self.report(result,None);first=directory/'visual-review-001.json';first.write_bytes(encoded(first_report))
        canonical_report=copy.deepcopy(first_report);canonical_report['checks']['scale']['notes']='Canonical observed anchors remain the named source torso and skull.'
        canonical=directory/'visual-review-002.json';canonical.write_bytes(encoded(canonical_report))
        ledger.record_review(self.task['task_manifest'],manifest['run_id'],manifest['attempts'][0]['attempt_id'],'pass',[],workflow._review_evidence(canonical,manifest))
        review=next(iter(self.state()['attempts'].values()))['review'];selected,_=workflow._canonical_review_receipt(manifest,review)
        self.assertEqual(selected,canonical)
        before=self.state()['sequence']
        with self.assertRaises(WorkflowError) as error:workflow._record_task_review(first,manifest,first_report)
        self.assertEqual(error.exception.code,'ALIGNMENT_REQUIRED');self.assertEqual(self.state()['sequence'],before)
        self.assertEqual(next(iter(self.state()['attempts'].values()))['review']['evidence'],workflow._review_evidence(canonical,manifest))

    def test_real_ledger_removes_only_the_explicit_repair_class_logically(self):
        for kind,route,other in [('position_x','body_x_alignment','alpha_edge_cleanup'),('alpha_edge','alpha_edge_cleanup','body_x_alignment')]:
            with self.subTest(kind=kind):
                result=self.generate();parent=self.read(result);report=self.report(result,'framing' if kind=='position_x' else 'background')
                report['repairable_defects']=[self.repair_entry(kind)];review=self.root/(kind+'.json');review.write_bytes(encoded(report))
                rejected=workflow.record_visual_review(result['run_manifest'],review);self.assertEqual(rejected['state'],workflow.VISUAL_REJECTED,rejected)
                # A different source-generation strategy attempt must provide its
                # recorded correction; use a fresh isolated task for the next case.
                child=self.logical_child(parent,route,name='logical-'+kind+'-matched')
                ledger.record_review(self.task['task_manifest'],child['run_id'],child['attempts'][0]['attempt_id'],'pass',[],'logical repair projection, no art/proof claim')
                actual=next(a for a in self.state()['attempts'].values() if a['run_id']==child['run_id']);self.assertEqual(actual['effective_defects'],[])
                wrong=self.logical_child(parent,other,name='logical-'+kind+'-other')
                with self.assertRaises(WorkflowError) as error:ledger.record_review(self.task['task_manifest'],wrong['run_id'],wrong['attempts'][0]['attempt_id'],'pass',[],'logical wrong repair class')
                self.assertEqual(error.exception.code,'TASK_SOURCE_DEFECT_UNRESOLVED')
                state=self.state();self.task=ledger.create_task(self.root/('next-'+kind),state['authorization'],list(state['targets'].values()));self.options['task_manifest']=self.task['task_manifest']

    def test_recovery_does_not_accept_arbitrary_reservation_mirrors(self):
        self.scene['pending']=True;result=self.generate();manifest=self.read(result);job=manifest['attempts'][0]['jobs']['primary']
        job['reserved_usd']=.111;Path(result['run_manifest']).write_bytes(encoded(manifest));reservation=Path(job['path'])/'reservation.json';reservation.write_bytes(encoded({'reserved_usd':.111}))
        before=len(self.calls());sequence=self.state()['sequence'];blocked=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
        self.assertEqual(blocked['block_reason'],'RESERVATION_TAMPERED',blocked);self.assertEqual(self.state()['sequence'],sequence);self.assertEqual(len(self.calls()),before)

    def test_external_check_order_replays_and_repeats_with_identical_receipt(self):
        for classified in (False,True):
            with self.subTest(classified=classified):
                state=self.state();self.task=ledger.create_task(self.root/('ordered-'+str(classified)),state['authorization'],list(state['targets'].values()));self.options['task_manifest']=self.task['task_manifest']
                result=self.generate();report=self.report(result,'scale');report['checks']['framing']['status']='fail'
                if classified:
                    report['checks']['background']['status']='fail'
                    report['repairable_defects']=[self.repair_entry('position_x'),self.repair_entry('alpha_edge')]
                report['checks']={name:report['checks'][name] for name in ('scale','framing','identity','orientation','background','motion','loop')}
                review=self.root/('external-'+str(classified)+'.json');review.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');original=review.read_bytes()
                recorded=workflow.record_visual_review(result['run_manifest'],review);self.assertEqual(recorded['state'],workflow.VISUAL_REJECTED,recorded)
                manifest=self.read(recorded);receipt=Path(manifest['visual_review']['path']);archived=receipt.read_bytes();calls_before=len(self.calls())
                resumed=workflow.resume(result['run_manifest'],transport=self.transport,poll_seconds=0)
                self.assertEqual(resumed['state'],workflow.VISUAL_REJECTED,resumed);self.assertEqual(len(self.calls()),calls_before)
                current=self.state();attempt=next(iter(current['attempts'].values()));mapping={'framing':'position_x','background':'alpha_edge'} if classified else {}
                expected=[mapping.get(name,name)+':'+report['checks'][name]['notes'].strip() for name in sorted(report['checks']) if report['checks'][name]['status']=='fail']
                self.assertEqual(attempt['review']['defects'],expected)
                self.assertEqual(workflow._validate_visual_report(manifest,report),expected)
                self.assertEqual(workflow._validate_visual_report(manifest,json.loads(archived)),expected)
                self.assertEqual(attempt['review']['evidence'],workflow._review_evidence(receipt,manifest))
                repeated=workflow.record_visual_review(result['run_manifest'],review);self.assertEqual(repeated['state'],workflow.VISUAL_REJECTED,repeated)
                self.assertEqual(self.state()['sequence'],current['sequence']);self.assertEqual(receipt.read_bytes(),archived)
                self.assertEqual(review.read_bytes(),original);self.assertEqual(sha_file(receipt),manifest['visual_review']['sha256'])
                self.assertEqual(len(list(receipt.parent.glob('visual-review-*.json'))),1)


if __name__=='__main__':unittest.main()
