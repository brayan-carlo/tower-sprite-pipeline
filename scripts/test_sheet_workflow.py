"""Offline integration: fake only the provider; real Pillow, files and run locks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
import generic_sprite_workflow as workflow
from sheet_workflow_state import run_lock, WorkflowError


def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def sheet(path, count=8, rgb=False, duplicate=False):
    cols=min(4,count); im=Image.new('RGB' if rgb else 'RGBA',(cols*96,((count+cols-1)//cols)*96),(110,110,110) if rgb else (0,0,0,0)); d=ImageDraw.Draw(im)
    for i in range(count):
        x=(i%cols)*96; y=(i//cols)*96; k=0 if duplicate else i
        d.rectangle((x+25,y+15,x+62,y+65),fill=(180,45+k*14,80)); d.rectangle((x+20+k*2,y+65,x+31+k*2,y+82),fill=(70,150,210));d.rectangle((x+52-k,y+60,x+66-k,y+83),fill=(210,160,50))
    im.save(path)

class Provider:
    def __init__(self, rgb=False, pending=False, unknown=False, reject=False): self.calls=[];self.rgb=rgb;self.pending=pending;self.unknown=unknown;self.reject=reject;self.key=True;self.prices=[.12];self.crash=False
    def __call__(self, command, job_dir):
        self.calls.append((command,str(job_dir))); c=json.loads((job_dir/'config.json').read_text()); cleanup='ideogram' in c['model']
        if not self.key: return {'state':'KEY_SETUP_REQUIRED','block_reason':'FAL_KEY_MISSING','retry_safe':True}
        if command=='pricing': return {'state':'PRICED','estimated_cost_usd':.01 if cleanup else self.prices.pop(0) if len(self.prices)>1 else self.prices[0]}
        if self.unknown: return {'state':'SUBMISSION_STATUS_UNKNOWN','retry_safe':False}
        if self.pending: self.pending=False;return {'state':'IN_PROGRESS','request_id':'known-1','retry_safe':True}
        p=job_dir/'raw.png'
        if not p.exists(): sheet(p,rgb=self.rgb and not cleanup,duplicate=self.reject)
        if self.crash: self.crash=False;raise RuntimeError('simulated child crash')
        return {'state':'DOWNLOADED','raw_path':str(p),'raw_sha256':digest(p),'request_id':'known-1'}

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.source=self.root/'original.png';sheet(self.source,count=2)
        self.options={'source':str(self.source),'action':'caminar','orientation':'izquierda','strategy':'sheet','workspace_root':str(self.root/'runs'),'poll_seconds':0}
    def tearDown(self): self.temp.cleanup()
    def read(self,r): return json.loads(Path(r['run_manifest']).read_text())
    def test_defaults_real_1254_assembly_and_ready_offline(self):
        Image.new('RGBA',(1254,1254),(80,100,120,255)).save(self.source); before=digest(self.source); p=Provider();r=workflow._generate_legacy(self.options,transport=p)
        self.assertEqual(r['state'],'READY_FOR_VISUAL_REVIEW',r);m=self.read(r);self.assertEqual(m['request']['action'],'walk');self.assertEqual(m['request']['orientation'],'left');self.assertEqual(m['request']['canvas'],[1254,1254]);self.assertTrue(m['request']['loop']);self.assertEqual(len(p.calls),2)
        with Image.open(r['artifact_paths']['sheet']) as im:self.assertEqual(im.size,(10032,1254));self.assertEqual(im.mode,'RGBA')
        with Image.open(r['artifact_paths']['preview_gif']) as im:self.assertEqual(im.n_frames,8);self.assertEqual(im.info['duration'],100)
        self.assertEqual(digest(self.source),before);self.assertEqual(workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('offline'))['state'],'READY_FOR_VISUAL_REVIEW')
    def test_cleanup_once(self):
        p=Provider(rgb=True);r=workflow._generate_legacy(self.options,transport=p);self.assertEqual(r['state'],'READY_FOR_VISUAL_REVIEW',r);self.assertEqual(len(p.calls),4);self.assertAlmostEqual(r['spent_estimate_usd'],.13)
    def test_only_missing_fields_zero_jobs(self):
        r=workflow._generate_legacy({'source':str(self.source)},transport=lambda *_:self.fail('network'));self.assertEqual(r['state'],'NEEDS_INPUT');self.assertEqual(r['missing_fields'],['action','orientation']);self.assertFalse((self.root/'runs').exists())
    def test_invalid_input_no_output(self):
        for override in [{'frames':17},{'frame_ms':25},{'canvas':'99999x3'},{'max_generation_attempts':4},{'source':str(self.root/'missing.png')},{'cycle_start_seconds':1}]:
            r=workflow._generate_legacy(self.options|override,transport=lambda *_:self.fail('network'));self.assertEqual(r['state'],'BLOCKED',r);self.assertFalse((self.root/'runs').exists())
    def test_custom_defaults_overrides(self):
        r=workflow._generate_legacy(self.options|{'action':'saludar "hola"','orientation':'NE','frames':8,'frame_ms':120,'canvas':'64x80'},transport=Provider());self.assertEqual(r['state'],'READY_FOR_VISUAL_REVIEW',r);q=self.read(r)['request'];self.assertFalse(q['loop']);self.assertFalse(q['grounded']);self.assertEqual(q['orientation'],'northeast');self.assertEqual(q['canvas'],[64,80])
    def test_missing_key_then_resume(self):
        p=Provider();p.key=False;r=workflow._generate_legacy(self.options,transport=p);self.assertEqual(r['state'],'KEY_SETUP_REQUIRED');self.assertEqual(r['spent_estimate_usd'],0);p.key=True;self.assertEqual(workflow.resume(r['run_manifest'],transport=p,poll_seconds=0)['state'],'READY_FOR_VISUAL_REVIEW')
    def test_pending_resume_same_attempt(self):
        p=Provider(pending=True);r=workflow._generate_legacy(self.options,transport=p);self.assertEqual(r['state'],'PENDING');self.assertTrue(r['retry_safe']);r=workflow.resume(r['run_manifest'],transport=p,poll_seconds=0);self.assertEqual(r['state'],'READY_FOR_VISUAL_REVIEW');self.assertEqual(len(self.read(r)['attempts']),1);self.assertEqual(sum(c=='pricing' for c,_ in p.calls),1)
    def test_unknown_never_new_job(self):
        p=Provider(unknown=True);r=workflow._generate_legacy(self.options,transport=p);self.assertEqual(r['state'],'SUBMISSION_STATUS_UNKNOWN');count=len(p.calls);r=workflow.resume(r['run_manifest'],transport=p);self.assertEqual(len(p.calls),count);self.assertFalse(r['retry_safe']);self.assertEqual(workflow.revise(r['run_manifest'],'opposite contact',transport=p)['state'],'SUBMISSION_STATUS_UNKNOWN')
    def test_rejection_bounded_feedback(self):
        p=Provider(reject=True);r=workflow._generate_legacy(self.options,transport=p);self.assertEqual(r['state'],'TECHNICAL_REJECTED',r);m=self.read(r);self.assertEqual(len(m['attempts']),2);self.assertTrue(m['attempts'][1]['feedback']);self.assertNotEqual(m['attempts'][0]['prompt'],m['attempts'][1]['prompt'])
    def test_cap_and_revise(self):
        p=Provider();r=workflow._generate_legacy(self.options|{'max_cost_usd':.13},transport=p);r=workflow.revise(r['run_manifest'],'opposite knee forward',transport=p);self.assertEqual(r['block_reason'],'COST_CAP_EXCEEDED');self.assertEqual(sum(c=='execute' for c,_ in p.calls),1)
    def test_source_and_request_tamper(self):
        r=workflow._generate_legacy(self.options,transport=Provider());m=self.read(r);m['request']['frames']=16;Path(r['run_manifest']).write_text(json.dumps(m));self.assertEqual(workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('network'))['block_reason'],'SCOPE_TAMPERED')
    def test_source_snapshot_tamper(self):
        r=workflow._generate_legacy(self.options,transport=Provider());m=self.read(r);Path(m['source']['path']).write_bytes(b'changed');self.assertEqual(workflow.inspect(r['run_manifest'])['block_reason'],'SOURCE_TAMPERED')
    def test_raw_crash_resume(self):
        p=Provider();p.crash=True;r=workflow._generate_legacy(self.options,transport=p);self.assertEqual(r['state'],'BLOCKED');r=workflow.resume(r['run_manifest'],transport=p,poll_seconds=0);self.assertEqual(r['state'],'READY_FOR_VISUAL_REVIEW');self.assertEqual(sum(c=='pricing' for c,_ in p.calls),1)
    def test_artifact_tamper_offline(self):
        r=workflow._generate_legacy(self.options,transport=Provider());Path(r['artifact_paths']['sheet']).write_bytes(b'bad');self.assertEqual(workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('network'))['block_reason'],'ARTIFACT_TAMPERED')
    def test_missing_ffmpeg_pre_spend(self):
        r=workflow._generate_legacy(self.options|{'strategy':'video','ffmpeg_exe':str(self.root/'missing.exe')},transport=lambda *_:self.fail('network'));self.assertEqual(r['state'],'RUNTIME_REQUIRED');self.assertFalse((self.root/'runs').exists())
    def test_real_os_lock_and_crash_release(self):
        lockroot=self.root/'lock';lockroot.mkdir();code='from sheet_workflow_state import run_lock,WorkflowError;from pathlib import Path;import sys\ntry:\n with run_lock(Path(sys.argv[1])): print("ACQUIRED")\nexcept WorkflowError as e: print(e.code)'
        with run_lock(lockroot):
            child=subprocess.run([sys.executable,'-B','-c',code,str(lockroot)],capture_output=True,text=True,timeout=10,cwd=Path(__file__).parent);self.assertEqual(child.stdout.strip(),'RUN_LOCKED')
        child=subprocess.run([sys.executable,'-B','-c',code,str(lockroot)],capture_output=True,text=True,timeout=10,cwd=Path(__file__).parent);self.assertEqual(child.stdout.strip(),'ACQUIRED')

    def test_price_refresh_rechecks_remaining_cap(self):
        p=Provider();p.prices=[.12,.20];real=p.__call__;stale=[True]
        def transport(command,job):
            if command=='execute' and stale[0]:stale[0]=False;return {'state':'BLOCKED','block_reason':'PRICE_STALE'}
            return real(command,job)
        r=workflow._generate_legacy(self.options|{'max_cost_usd':.15},transport=transport)
        self.assertEqual(r['block_reason'],'COST_CAP_EXCEEDED');self.assertEqual(r['spent_estimate_usd'],.12)
        self.assertEqual(sum(c=='execute' for c,_ in p.calls),0)

    def test_config_tamper_even_with_manifest_config_hash_changed(self):
        p=Provider(pending=True);r=workflow._generate_legacy(self.options,transport=p);m=self.read(r);j=m['attempts'][0]['jobs']['primary'];config_path=Path(j['path'])/'config.json';c=json.loads(config_path.read_text());c['max_cost_usd']=99;config_path.write_text(json.dumps(c));j['config_sha256']=workflow.scope_sha(c);Path(r['run_manifest']).write_text(json.dumps(m))
        result=workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('network'))
        self.assertEqual(result['block_reason'],'JOB_CONFIG_TAMPERED')

    def test_partial_real_assembly_recovers_identical_outputs(self):
        import sheet_images
        writer=sheet_images._write_new_or_identical
        def interrupted(payloads):
            first=next(iter(payloads.items()));writer(dict([first]));raise OSError('simulated crash')
        p=Provider()
        with patch.object(sheet_images,'_write_new_or_identical',interrupted):r=workflow._generate_legacy(self.options,transport=p)
        self.assertEqual(r['state'],'BLOCKED');before=len(p.calls);result=workflow.resume(r['run_manifest'],transport=p,poll_seconds=0)
        self.assertEqual(result['state'],'READY_FOR_VISUAL_REVIEW',result);self.assertEqual(len(p.calls),before)

    def test_two_real_resume_processes_and_os_crash_release(self):
        p=Provider(pending=True);r=workflow._generate_legacy(self.options,transport=p);signal=self.root/'entered';code="""import json,sys,time
from pathlib import Path
from generic_sprite_workflow import resume
from test_sheet_workflow import Provider
p=Provider()
def transport(command,job):
 Path(sys.argv[2]).write_text('entered')
 time.sleep(2)
 return p(command,job)
print(json.dumps(resume(sys.argv[1],transport=transport,poll_seconds=0)))
"""
        child=subprocess.Popen([sys.executable,'-B','-c',code,r['run_manifest'],str(signal)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=Path(__file__).parent)
        import time
        end=time.monotonic()+10
        while not signal.exists() and time.monotonic()<end:time.sleep(.02)
        self.assertTrue(signal.exists());second=workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('locked transport'))
        self.assertEqual(second['block_reason'],'RUN_LOCKED');stdout,stderr=child.communicate(timeout=10);self.assertEqual(child.returncode,0,stderr);self.assertEqual(json.loads(stdout)['state'],'READY_FOR_VISUAL_REVIEW')
        code="from sheet_workflow_state import run_lock;from pathlib import Path;import sys,os\nwith run_lock(Path(sys.argv[1])):os._exit(0)"
        subprocess.run([sys.executable,'-B','-c',code,str(self.root)],cwd=Path(__file__).parent,timeout=10,check=True)
        with run_lock(self.root):pass

    def test_h3_known_video_resume_cleanup_once_real_sampler(self):
        from test_sheet_video import VideoTests, FFMPEG
        fixture=VideoTests();fixture.setUp()
        try:
            raw=fixture.video();calls=[];pending=[True]
            Image.new('RGBA',(1254,1254),(80,100,120,255)).save(self.source)
            def transport(command,job):
                import shutil
                from PIL import ImageChops
                c=json.loads((job/'config.json').read_text());calls.append((command,c['model']))
                if command=='pricing':return {'state':'PRICED','estimated_cost_usd':.01 if c['model']==workflow.CLEANUP else .12}
                if c['model']==workflow.H3:
                    if pending[0]:pending[0]=False;return {'state':'IN_PROGRESS','request_id':'h3-1','retry_safe':True}
                    out=job/'raw.mp4';shutil.copyfile(raw,out)
                else:
                    with Image.open(c['source']['path']) as im:out_image=im.convert('RGB')
                    gray=Image.new('RGB',out_image.size,(110,110,110));delta=ImageChops.difference(out_image,gray);mask=delta.convert('L').point(lambda v:255 if v>20 else 0);out_image.putalpha(mask);out=job/'raw.png';out_image.save(out)
                return {'state':'DOWNLOADED','raw_path':str(out),'raw_sha256':digest(out),'request_id':'job-known'}
            r=workflow._generate_legacy(self.options|{'strategy':'auto','ffmpeg_exe':str(FFMPEG)},transport=transport);self.assertEqual(r['state'],'PENDING')
            r=workflow.resume(r['run_manifest'],transport=transport,poll_seconds=0);self.assertEqual(r['state'],'READY_FOR_VISUAL_REVIEW',r)
            self.assertEqual(calls,[('pricing',workflow.H3),('execute',workflow.H3),('execute',workflow.H3),('pricing',workflow.CLEANUP),('execute',workflow.CLEANUP)])
            m=self.read(r);self.assertEqual(m['attempts'][0]['sampling']['selection']['analysis_frame_indices'],[24,27,30,33,36,39,42,45]);self.assertEqual(len(m['artifact_paths']['frames']),8)
            self.assertEqual(workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('offline'))['state'],'READY_FOR_VISUAL_REVIEW')
        finally:fixture.tearDown()

    def test_nonloop_video_prompt_does_not_request_repeat(self):
        q={'action':'attack','orientation':'left','description':None,'pixel_art':True,'route':'video','loop':False}
        prompt=workflow._prompt(q,None)
        self.assertNotIn('repeating',prompt);self.assertNotIn('Return to the initial pose',prompt)

    def test_inspect_strictly_read_only(self):
        r=workflow._generate_legacy(self.options,transport=Provider());root=Path(r['run_manifest']).parent
        before={str(p):(p.stat().st_mtime_ns,digest(p)) for p in root.rglob('*') if p.is_file()}
        with patch.object(workflow,'_transport',side_effect=AssertionError('network')):result=workflow.inspect(r['run_manifest'])
        self.assertEqual(result['state'],'READY_FOR_VISUAL_REVIEW');self.assertEqual(before,{str(p):(p.stat().st_mtime_ns,digest(p)) for p in root.rglob('*') if p.is_file()})

    def test_native_child_environment_isolated_without_reading_real_key(self):
        from types import SimpleNamespace
        captured=[]
        def child(args,**kwargs):captured.append((args,kwargs));return SimpleNamespace(stdout=b'{"state":"PRICED","estimated_cost_usd":0.12}',stderr=b'',returncode=0)
        with patch.dict(os.environ,{'FAL_KEY':'synthetic-test-key','NODE_OPTIONS':'--require malicious','NODE_PATH':'malicious','PATH':'synthetic'},clear=True),patch.object(workflow,'_node_exe',return_value='node'),patch.object(workflow.subprocess,'run',side_effect=child):
            result=workflow._transport('pricing',self.root)
        self.assertEqual(result['state'],'PRICED');args,kwargs=captured[0];self.assertNotIn('synthetic-test-key',repr(args));self.assertEqual(kwargs['env']['FAL_KEY'],'synthetic-test-key');self.assertNotIn('NODE_OPTIONS',kwargs['env']);self.assertNotIn('NODE_PATH',kwargs['env'])

    def test_explicit_cycle_too_short_rejected_before_spend(self):
        from test_sheet_video import FFMPEG
        r=workflow._generate_legacy(self.options|{'strategy':'video','ffmpeg_exe':str(FFMPEG),'cycle_start_seconds':1,'cycle_duration_seconds':.01},transport=lambda *_:self.fail('network'))
        self.assertEqual(r['block_reason'],'CYCLE_OPTIONS_INVALID');self.assertFalse((self.root/'runs').exists())

    @unittest.skipUnless(os.name=='nt','Windows user environment fallback')
    def test_blank_process_key_uses_only_mocked_user_fallback(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        fake=SimpleNamespace(HKEY_CURRENT_USER=object(),OpenKey=MagicMock(),QueryValueEx=MagicMock(return_value=('fake-user-key',1)))
        with patch.dict(os.environ,{'FAL_KEY':'   '},clear=True),patch.dict(sys.modules,{'winreg':fake}),patch.object(workflow,'_node_exe',return_value='node'),patch.object(workflow.subprocess,'run',return_value=SimpleNamespace(stdout=b'{"state":"PRICED"}',stderr=b'',returncode=0)) as child:
            result=workflow._transport('pricing',self.root)
        self.assertEqual(result['state'],'PRICED');self.assertEqual(child.call_args.kwargs['env']['FAL_KEY'],'fake-user-key');self.assertEqual(fake.QueryValueEx.call_args.args[1],'FAL_KEY')

    def test_invalid_resume_does_not_create_lock_or_output(self):
        before=set(self.root.iterdir());result=workflow.resume(self.root/'missing.json',transport=lambda *_:self.fail('network'))
        self.assertEqual(result['state'],'BLOCKED');self.assertEqual(before,set(self.root.iterdir()))

    def test_ready_artifact_path_tamper_is_detected(self):
        r=workflow._generate_legacy(self.options,transport=Provider());m=self.read(r);m['artifact_paths']['preview_gif']=str(self.source);Path(r['run_manifest']).write_text(json.dumps(m))
        self.assertEqual(workflow.resume(r['run_manifest'],transport=lambda *_:self.fail('offline'))['block_reason'],'ARTIFACT_TAMPERED')

    def test_unreliable_completed_video_is_revisable_without_resume_resubmit(self):
        from test_sheet_video import VideoTests, FFMPEG
        fixture=VideoTests();fixture.setUp()
        try:
            flat=fixture.video(flat=True);calls=[]
            def transport(command,job):
                import shutil
                c=json.loads((job/'config.json').read_text());calls.append((command,c['model']))
                if command=='pricing':return {'state':'PRICED','estimated_cost_usd':.12}
                out=job/'raw.mp4';shutil.copyfile(flat,out)
                return {'state':'DOWNLOADED','raw_path':str(out),'raw_sha256':digest(out),'request_id':'flat-known-id'}
            options=self.options|{'strategy':'video','ffmpeg_exe':str(FFMPEG)}
            r=workflow._generate_legacy(options,transport=transport)
            self.assertEqual(r['state'],'TECHNICAL_REJECTED',r);self.assertEqual(r['block_reason'],'CYCLE_UNRELIABLE_ZERO_MOTION');self.assertEqual(r['spent_estimate_usd'],.12)
            m=self.read(r);first=m['attempts'][0];primary=first['jobs']['primary']
            self.assertEqual(primary['state'],'DOWNLOADED');self.assertEqual(primary['request_id'],'flat-known-id');self.assertEqual(first['diagnoses'],[{'code':'CYCLE_UNRELIABLE_ZERO_MOTION','source_sha256':digest(flat)}]);self.assertNotIn('cleanup',first['jobs'])
            resumed=workflow.resume(r['run_manifest'],transport=transport,poll_seconds=0)
            self.assertEqual(resumed['state'],'TECHNICAL_REJECTED');self.assertEqual(calls,[('pricing',workflow.H3),('execute',workflow.H3)]);self.assertEqual(self.read(r)['attempts'][0],first)
            revised=workflow.revise(r['run_manifest'],'Alternate the near and far contact legs instead of standing still.',transport=transport,poll_seconds=0)
            self.assertEqual(revised['state'],'TECHNICAL_REJECTED',revised);self.assertEqual(revised['spent_estimate_usd'],.24)
            m2=self.read(r);self.assertEqual(m2['attempts'][0],first);self.assertEqual(len(m2['attempts']),2);self.assertNotEqual(m2['attempts'][1]['prompt'],first['prompt']);self.assertEqual(m2['authorization'],m['authorization'])
            config=json.loads((Path(m2['attempts'][1]['jobs']['primary']['path'])/'config.json').read_text());self.assertEqual(config['max_cost_usd'],.38)
            self.assertEqual(calls,[('pricing',workflow.H3),('execute',workflow.H3)]*2)
            exhausted=workflow.revise(r['run_manifest'],'Try another complete contact cycle.',transport=transport,poll_seconds=0)
            self.assertEqual(exhausted['block_reason'],'ATTEMPT_LIMIT_REACHED');self.assertEqual(len(calls),4)
        finally:fixture.tearDown()

    def test_unreliable_video_revision_obeys_cap_and_rejects_raw_tamper(self):
        from test_sheet_video import VideoTests, FFMPEG
        fixture=VideoTests();fixture.setUp()
        try:
            flat=fixture.video(flat=True);calls=[];wrong_hash=[False]
            def transport(command,job):
                import shutil
                calls.append(command)
                if command=='pricing':return {'state':'PRICED','estimated_cost_usd':.12}
                out=job/'raw.mp4';shutil.copyfile(flat,out)
                return {'state':'DOWNLOADED','raw_path':str(out),'raw_sha256':'0'*64 if wrong_hash[0] else digest(out),'request_id':'flat-known-id'}
            options=self.options|{'strategy':'video','ffmpeg_exe':str(FFMPEG),'max_cost_usd':.13}
            r=workflow._generate_legacy(options,transport=transport);self.assertEqual(r['state'],'TECHNICAL_REJECTED',r)
            revised=workflow.revise(r['run_manifest'],'Show actual alternating steps.',transport=transport,poll_seconds=0)
            self.assertEqual(revised['block_reason'],'COST_CAP_EXCEEDED');self.assertEqual(revised['spent_estimate_usd'],.12);self.assertEqual(calls,['pricing','execute','pricing'])
            wrong_hash[0]=True;tampered=workflow._generate_legacy(options,transport=transport)
            self.assertEqual(tampered['state'],'BLOCKED');self.assertEqual(tampered['block_reason'],'RAW_TAMPERED')
            blocked=workflow.revise(tampered['run_manifest'],'Show actual alternating steps.',transport=transport,poll_seconds=0)
            self.assertEqual(blocked['state'],'BLOCKED');self.assertEqual(len(self.read(tampered)['attempts']),1)
            self.assertNotEqual(blocked['block_reason'],'ATTEMPT_LIMIT_REACHED')
        finally:fixture.tearDown()

    def test_pending_attempt_cannot_be_revised(self):
        provider=Provider(pending=True);result=workflow._generate_legacy(self.options,transport=provider)
        revised=workflow.revise(result['run_manifest'],'Use the opposite leg contact.',transport=provider,poll_seconds=0)
        self.assertEqual(revised['block_reason'],'REVISION_REQUIRES_FINISHED_ATTEMPT');self.assertEqual(len(provider.calls),2)

if __name__=='__main__':unittest.main()
