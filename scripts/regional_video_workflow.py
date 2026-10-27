"""Managed regional VACE consumer; physical proofs never grant artistic PASS."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlencode
from PIL import Image
import generic_sprite_workflow as generic
import sprite_task_ledger as ledger
from sheet_video import resolve_ffmpeg, _ffmpeg, png_bytes
from sheet_workflow_state import WorkflowError, atomic_json, encoded, new_or_identical, run_lock, scope_sha, sha_file
from endpoint_cycle_contract import node_config_sha

ROUTE='regional_video_inpaint_v1'
VACE='fal-ai/wan-vace-14b/inpainting'
CLEANUP='fal-ai/ideogram/remove-background'
INPUT_CHECKS={'current_defect','base_framewise','near_hand_only','far_hand_control','occlusions_accepted','stop_preserved','crop_background'}
RAW_CHECKS={'identity','anatomy','scale','material','orientation','framewise_motion','mask_scope','exterior','crop_background','loop_selection'}
FIXED=dict(seed=1,num_frames=81,frames_per_second=24,match_input_num_frames=True,match_input_frames_per_second=True,resolution='720p',aspect_ratio='1:1',preprocess=False,num_interpolated_frames=0,temporal_downsample_factor=0,enable_auto_downsample=False,auto_downsample_min_fps=15,sync_mode=False,enable_safety_checker=True,enable_prompt_expansion=False,num_inference_steps=30,guidance_scale=5,sampler='unipc',shift=5,acceleration='none',video_quality='high',video_write_mode='balanced',interpolator_model='film',transparency_mode='content_aware',return_frames_zip=False)
CLEANUP_POLICY=dict(model=CLEANUP,sync_mode=False,max_bytes=10485760,max_width=4096,max_height=4096)


def fail(code='REGIONAL_EVIDENCE_INVALID'):raise WorkflowError(code)

def keys(value,names,code='REGIONAL_EVIDENCE_INVALID'):
    if not isinstance(value,dict) or set(value)!=set(names):fail(code)

def text(value):
    if not isinstance(value,str) or not 1<=len(value.strip())<=10000:fail()
    return value

def path(value):
    p=Path(value)
    if not p.is_absolute() or p.is_symlink() or p.resolve()!=p.absolute():fail()
    return p

def load(filename):
    from sprite_task_bridge import _json
    return _json(path(str(filename)),code='REGIONAL_EVIDENCE_INVALID')

def ref(filename):return dict(path=str(path(str(filename))),sha256=sha_file(filename))

def check_ref(value):
    keys(value,('path','sha256'))
    if not isinstance(value['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',value['sha256']):fail()
    p=path(value['path'])
    if sha_file(p)!=value['sha256']:fail('REGIONAL_EVIDENCE_CHANGED')
    return p

def document(value):return load(check_ref(value))

def image(filename):
    with Image.open(filename) as opened:
        if opened.format!='PNG' or opened.width*opened.height>64_000_000:fail()
        opened.load();return opened.convert('RGBA')

def _historical_refs(value):
    if isinstance(value,dict):
        if set(value)=={'path','sha256'}:yield value
        else:
            for item in value.values():yield from _historical_refs(item)
    elif isinstance(value,list):
        for item in value:yield from _historical_refs(item)


def validate_provenance(task,target_id,p):
    keys(p,('schema','task_id','target_id','history_bundle_sha256','history_id','canonical','prepared','provider_prepared','parent_manifest','sidecar','raw','evidence','padding','lineage_proof','linkage_proof'))
    target=task['targets'].get(target_id)
    if (p['schema']!='regional_provenance_v1' or not target or p['task_id']!=task['task_id'] or p['target_id']!=target_id
            or task.get('history_status')!='complete' or p['history_bundle_sha256']!=task['historical_import']['bundle_sha256']):fail('REGIONAL_HISTORY_MISMATCH')
    history=task['historical_attempts'].get(p['history_id'])
    if not history or history['target_id']!=target_id or history['source_sha256']!=target['source_sha256'] or history['kind']!='provider_generation' or history['mechanism']['origin']!='fal' or history['mechanism']['resolution']=='unresolved':fail('REGIONAL_HISTORY_MISMATCH')
    if not isinstance(p['evidence'],list) or not p['evidence']:fail()
    for v in [p[k] for k in ('canonical','prepared','provider_prepared','parent_manifest','sidecar','raw','lineage_proof','linkage_proof')]+p['evidence']:check_ref(v)
    # The sealed import can retain more evidence than the concise provenance
    # list; open every actual Ref without requiring duplicate declarations.
    historical_refs=list(_historical_refs(history))
    for v in historical_refs:
        check_ref(v)
    # Authenticate the complete parent bytes to the selected sealed entry.
    # Matching run/request strings in a new self-consistent JSON are not proof.
    if not any(v['sha256']==p['parent_manifest']['sha256'] for v in historical_refs):fail('REGIONAL_HISTORY_MISMATCH')
    if p['canonical']['sha256']!=target['source_sha256']:fail('REGIONAL_HISTORY_MISMATCH')
    canonical=image(p['canonical']['path']);prepared=image(p['prepared']['path'])
    padding=p['padding'];keys(padding,('canvas','offset','scale'))
    if (padding['scale']!=1 or type(padding['scale']) not in (int,float) or padding['canvas']!=list(prepared.size)
            or not isinstance(padding['offset'],list) or len(padding['offset'])!=2 or any(type(v) is not int or v<0 for v in padding['offset'])):fail()
    x,y=padding['offset']
    if x+canonical.width>prepared.width or y+canonical.height>prepared.height:fail()
    expected=Image.new('RGBA',prepared.size);expected.paste(canonical,(x,y))
    if expected.tobytes()!=prepared.tobytes():fail('REGIONAL_PADDING_MISMATCH')
    sidecar=document(p['sidecar'])
    for k,v in dict(source_sha256=p['canonical']['sha256'],prepared_sha256=p['prepared']['sha256'],canvas=padding['canvas'],offset=padding['offset'],scale_factor=1).items():
        if sidecar.get(k)!=v:fail('REGIONAL_PADDING_MISMATCH')
    if path(sidecar['source'])!=path(p['canonical']['path']) or path(sidecar['prepared'])!=path(p['prepared']['path']):fail()
    parent=document(p['parent_manifest']);index=history['attempt_index']
    if type(index) is not int or index<1:fail('REGIONAL_HISTORY_MISMATCH')
    attempts=[a for a in parent['attempts'] if isinstance(a,dict) and type(a.get('index')) is int and a['index']==index]
    if len(attempts)!=1 or history['attempt_id'] is not None and attempts[0].get('attempt_id')!=history['attempt_id']:fail('REGIONAL_HISTORY_MISMATCH')
    a=attempts[0];job=a['jobs']['primary'];prep=a['prepared'];transform=prep['transform']
    if (parent['run_id']!=history['run_id'] or parent['source']['sha256']!=p['prepared']['sha256']
            or sha_file(parent['source']['path'])!=p['prepared']['sha256'] or job.get('raw_sha256')!=p['raw']['sha256']
            or sha_file(path(job['raw_path']))!=p['raw']['sha256'] or job.get('state')!='DOWNLOADED'
            or prep['sha256']!=p['provider_prepared']['sha256'] or prep['original_sha256']!=p['prepared']['sha256']
            or path(prep['path'])!=path(p['provider_prepared']['path'])):fail('REGIONAL_HISTORY_MISMATCH')
    if not any(task['imports'].get(k,{}).get('provider_request_id')==job.get('request_id') for k in history['reservation_ids']):fail('REGIONAL_HISTORY_MISMATCH')
    keys(transform,('scale','size','offset','canvas','background_rgb'))
    canvas=parent['request']['canvas'];n=max(canvas);c=transform['canvas']
    if canvas!=list(prepared.size) or not isinstance(c,list) or len(c)!=2 or c[0]!=c[1] or type(c[0]) is not int or c[0]<=0:fail()
    factor=c[0]/n;size=[max(1,round(v*factor)) for v in canvas];offset=[(c[0]-size[0])//2,(c[0]-size[1])//2]
    if (transform['scale']!=factor or transform['size']!=size or transform['offset']!=offset or transform['background_rgb']!=[110,110,110]):fail('REGIONAL_PREPARATION_MISMATCH')
    resized=prepared.resize(tuple(size),Image.Resampling.NEAREST if parent['request']['pixel_art'] else Image.Resampling.LANCZOS)
    provider=Image.new('RGB',tuple(c),(110,110,110));provider.paste(resized,tuple(offset),resized.getchannel('A'))
    actual=image(p['provider_prepared']['path']).convert('RGB')
    if provider.size!=actual.size or provider.tobytes()!=actual.tobytes():fail('REGIONAL_PREPARATION_MISMATCH')
    return parent,transform


def _run_ffmpeg(exe,args,maximum=1024*1024):
    try:r=subprocess.run([exe,'-nostdin','-v','info',*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired):fail('REGIONAL_VIDEO_DECODE_FAILED')
    if r.returncode or len(r.stdout)>maximum or len(r.stderr)>1024*1024:fail('REGIONAL_VIDEO_DECODE_FAILED')
    return r.stdout,r.stderr.decode('utf-8','replace')


@lru_cache(maxsize=24)
def _probe(filename,digest,exe):
    _,log=_run_ffmpeg(exe,['-i',str(filename),'-map','0:v:0','-vf','showinfo','-frames:v','1','-an','-f','null','-'])
    matches=re.findall(r'\bn:\s*\d+\s+pts:\s*(-?\d+).*?\bs:(\d+)x(\d+)',log)
    if not matches:fail('REGIONAL_VIDEO_DECODE_FAILED')
    return (int(matches[0][1]),int(matches[0][2]))


@lru_cache(maxsize=24)
def _decode_cached(filename,digest,exe,width,height,start,count,binary):
    """Stream one RGB24 frame; cache only facts keyed by a freshly checked SHA.

    showinfo timestamps precede output muxing so VFR cannot hide behind CFR
    output timestamps. stderr goes to disk, avoiding a blocked full pipe.
    """
    vf=(f'select=between(n\\,{start}\\,{start+count-1}),' if start is not None else '')+'format=rgb24,showinfo'
    unit=width*height*3;hashes=[];nonempty=[]
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen([exe,'-nostdin','-v','info','-i',filename,'-map','0:v:0','-vf',vf,'-an','-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=errors,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            while True:
                payload=bytearray()
                while len(payload)<unit:
                    part=process.stdout.read(unit-len(payload))
                    if not part:break
                    payload.extend(part)
                if not payload:break
                if len(payload)!=unit or len(hashes)>=count:fail('REGIONAL_FRAME_COUNT_INVALID')
                hashes.append(hashlib.sha256(payload).hexdigest())
                if binary:
                    rgb=bytes(payload);red=rgb[0::3];white=red.count(255)
                    if red!=rgb[1::3] or red!=rgb[2::3] or red.count(0)+white!=width*height:fail('REGIONAL_MASK_NOT_BINARY')
                    if white==width*height:fail('REGIONAL_MASK_GLOBAL')
                    nonempty.append(white>0)
            if process.wait(timeout=120):fail('REGIONAL_VIDEO_DECODE_FAILED')
        finally:
            process.stdout.close()
            if process.poll() is None:process.kill();process.wait()
        errors.seek(0);log=errors.read(1024*1024+1).decode('utf-8','replace')
    bases=re.findall(r'config in time_base:\s*(\d+)/(\d+)',log)
    rows=re.findall(r'\bn:\s*(\d+)\s+pts:\s*(-?\d+).*?\bs:(\d+)x(\d+)',log)
    if len(log)>1024*1024 or not bases or len(rows)!=count or len(hashes)!=count:fail('REGIONAL_FRAME_COUNT_INVALID')
    tb=Fraction(int(bases[0][0]),int(bases[0][1]));origin=int(rows[0][1]);pts=[]
    for i,(ordinal,tick,w,h) in enumerate(rows):
        if int(ordinal)!=i or [int(w),int(h)]!=[width,height]:fail('REGIONAL_DIMENSIONS_INVALID')
        value=(int(tick)-origin)*tb
        if value!=Fraction(i,24):fail('REGIONAL_PTS_INVALID')
        pts.append(dict(num=value.numerator,den=value.denominator))
    return dict(hashes=hashes,nonempty=nonempty,pts=pts,canvas=[width,height],frame_count=count)


def decode(filename,exe,*,start=None,count=81,binary=False,canvas=None):
    filename=str(path(str(filename)));digest=sha_file(filename);actual=list(_probe(filename,digest,exe))
    if canvas is not None and actual!=canvas:fail('REGIONAL_DIMENSIONS_INVALID')
    if actual[0]!=actual[1] or not 1<=actual[0]<=4096:fail('REGIONAL_DIMENSIONS_INVALID')
    return _decode_cached(filename,digest,exe,*actual,start,count,binary)


def validate_media(p,media,preflight,parent,transform,exe,provenance_sha,media_sha):
    keys(media,('schema','video','mask','identity','window','frames','selection','geometry','mask_review'))
    if media['schema']!='regional_media_v1':fail()
    for k in ('video','mask','identity','mask_review'):check_ref(media[k])
    if media['identity']['sha256']!=p['canonical']['sha256']:fail()
    window=media['window'];keys(window,('start','count','fps','width','height'))
    if (type(window['start']) is not int or window['start']<0 or any(type(window[k]) is not int for k in ('count','fps','width','height'))
            or [window[k] for k in ('count','fps')]!=[81,24] or window['width']!=window['height'] or not 1<=window['width']<=4096):fail('REGIONAL_WINDOW_INVALID')
    selection=media['selection']
    if not isinstance(selection,list) or len(selection)!=8 or any(type(i) is not int or not 0<=i<81 for i in selection) or selection!=sorted(set(selection)):fail('REGIONAL_SELECTION_INVALID')
    canvas=parent['request']['canvas'];n=max(canvas);c=transform['canvas'][0];off=transform['offset']
    geometry=dict(policy='inverse_parent_preparation_v1',resize_canvas=[n,n],crop=[round(off[0]*n/c),round(off[1]*n/c),*canvas],output_canvas=canvas,scale_numerator=n,scale_denominator=window['width'])
    if encoded(media['geometry'])!=encoded(geometry):fail('REGIONAL_GEOMETRY_INVALID')
    declared=[window['width'],window['height']]
    native=decode(p['raw']['path'],exe,start=window['start'],canvas=declared)
    video=decode(media['video']['path'],exe,canvas=declared);mask=decode(media['mask']['path'],exe,binary=True,canvas=declared)
    frames=media['frames']
    if not isinstance(frames,list) or len(frames)!=81:fail('REGIONAL_FRAME_COUNT_INVALID')
    for i,row in enumerate(frames):
        keys(row,('source_index','pts','source_rgb_sha256','video_rgb_sha256','mask_rgb_sha256'));keys(row['pts'],('num','den'))
        if (type(row['source_index']) is not int or row['source_index']!=window['start']+i or any(type(row['pts'][k]) is not int for k in ('num','den')) or row['pts']['den']<=0
                or Fraction(row['pts']['num'],row['pts']['den'])!=Fraction(i,24) or row['source_rgb_sha256']!=native['hashes'][i]
                or row['video_rgb_sha256']!=video['hashes'][i] or row['source_rgb_sha256']!=row['video_rgb_sha256'] or row['mask_rgb_sha256']!=mask['hashes'][i]):fail('REGIONAL_FRAME_EVIDENCE_MISMATCH')
    keys(preflight,('schema','decision','provenance_sha256','media_sha256','reports','defect_frames','checks','frame_findings'))
    if preflight['schema']!='regional_preflight_v1' or preflight['decision']!='pass' or preflight['provenance_sha256']!=provenance_sha or preflight['media_sha256']!=media_sha:fail('REGIONAL_PREFLIGHT_INVALID')
    if not isinstance(preflight['reports'],list) or not preflight['reports']:fail('REGIONAL_PREFLIGHT_INVALID')
    for report in preflight['reports']:check_ref(report)
    keys(preflight['checks'],INPUT_CHECKS)
    for check in preflight['checks'].values():
        keys(check,('pass','notes'));text(check['notes'])
        if check['pass'] is not True:fail('REGIONAL_PREFLIGHT_INVALID')
    findings=preflight['frame_findings']
    if not isinstance(findings,list) or len(findings)!=81:fail('REGIONAL_PREFLIGHT_INVALID')
    for i,finding in enumerate(findings):
        keys(finding,('source_index','mask_sha256','eligible','notes'));text(finding['notes'])
        if (type(finding['source_index']) is not int or finding['source_index']!=window['start']+i or finding['mask_sha256']!=mask['hashes'][i]
                or type(finding['eligible']) is not bool or not finding['eligible'] and mask['nonempty'][i]):fail('REGIONAL_PREFLIGHT_INVALID')
    defects=preflight['defect_frames']
    if (not isinstance(defects,list) or not defects or any(type(i) is not int for i in defects) or len(set(defects))!=len(defects)
            or not set(defects).issubset({window['start']+i for i in selection}) or not any(mask['nonempty'])):fail('REGIONAL_DEFECT_UNCOVERED')
    if any(not mask['nonempty'][i-window['start']] or not findings[i-window['start']]['eligible'] for i in defects):fail('REGIONAL_DEFECT_UNCOVERED')
    return geometry


def _config(path_,m,role,files,prompt=None,negative=None):
    tc=dict(task_ref=m['request']['task_ref'],run_manifest=str(path_),run_id=m['run_id'],attempt_id=m['run_id']+':attempt-01',operation_id=m['run_id']+':attempt-01:'+role)
    inputs=dict(FIXED,prompt=prompt,negative_prompt=negative) if role=='primary' else dict(sync_mode=False)
    return dict(schema_version=6,route=ROUTE,role=role,model=VACE if role=='primary' else CLEANUP,input_files=files,input=inputs,authorization=dict(request_authorized=True,task_context=tc),max_cost_usd=m['authorization']['max_cost_usd'])


def _write_job(root,m,role,config):
    directory=root/'attempt-01'/role;directory.mkdir(parents=True,exist_ok=True)
    new_or_identical(directory/'config.json',encoded(config));digest=node_config_sha(config)
    new_or_identical(directory/'config.sha256',(digest+'\n').encode())
    inputs=[dict(role=f['role'],sha256=f['sha256']) for f in config['input_files']]
    new_or_identical(directory/'inputs.json',encoded(inputs))
    m['attempts'][0]['jobs'][role]=dict(path=str(directory),state='PREPARED',operation_id=m['run_id']+':attempt-01:'+role,config_sha256=digest,transport_config_sha256=digest,config_file_sha256=sha_file(directory/'config.json'),reserved_usd=0)
    return digest,inputs


def prepare(task_manifest: str,target_id: str,provenance_file: str,media_file: str,preflight_file: str,*,output_root: str,prompt: str,negative_prompt: str,ffmpeg_exe: str)->dict:
    manifest_path=None;m=None
    try:
        text(prompt);text(negative_prompt);exe=resolve_ffmpeg(ffmpeg_exe)
        task=ledger.inspect_task(task_manifest);p=load(provenance_file);media=load(media_file);pre=load(preflight_file)
        parent,tr=validate_provenance(task,target_id,p)
        validate_media(p,media,pre,parent,tr,exe,sha_file(provenance_file),sha_file(media_file))
        target=task['targets'][target_id]
        output=Path(output_root).expanduser().resolve();installed=Path.home()/'.codex/skills'
        if output==installed or installed in output.parents:fail('WORKSPACE_NOT_ALLOWED')
        for protected in [Path(v['path']) for v in _historical_refs(p)]:
            if output==protected or protected in output.parents:fail('WORKSPACE_NOT_ALLOWED')
        run_id=uuid.uuid4().hex;root=output/run_id;root.mkdir(parents=True,exist_ok=False);manifest_path=root/'run-manifest.json'
        evidence=root/'regional-inputs';evidence.mkdir();docs={}
        for name,source in [('provenance',provenance_file),('media',media_file),('preflight',preflight_file)]:
            dest=evidence/(name+'.json');new_or_identical(dest,Path(source).read_bytes());docs[name]=ref(dest)
        original=root/'source.png';new_or_identical(original,check_ref(p['canonical']).read_bytes())
        taskref=dict(schema='sprite_task_ref_v1',task_id=task['task_id'],task_manifest=str(Path(task_manifest).resolve()),authorization_sha256=task['authorization_sha256'],target_id=target_id)
        q=dict(action=target['action'],orientation=target['orientation'],frames=8,frame_ms=100,loop=True,pixel_art=True,grounded=False,canvas=media['geometry']['output_canvas'],alignment_policy='body_x_preserve_y_v1',columns=4,rows=2,route=ROUTE,ffmpeg_exe=exe,ffmpeg_sha256=sha_file(exe),task_ref=taskref)
        if any(q.get(k)!=v for k,v in target['requirements'].items()):fail('TASK_POLICY_UNSUPPORTED')
        cap=float(task['cap_usd']);cap=int(cap) if cap.is_integer() else cap
        auth=dict(source='session_request',request_authorized=True,max_cost_usd=cap,max_generation_attempts=1)
        m=dict(schema_version=3,mode='whole_sheet',run_id=run_id,source=ref(original),request=q,authorization=auth,state='REGIONAL_INPUT_REVIEW_REQUIRED',artifact_paths={},artifact_hashes={},attempts=[dict(index=1,attempt_id=run_id+':attempt-01',jobs={},diagnoses=[])])
        files=[]
        for name in ('video','mask','identity'):
            src=check_ref(media[name]);dest=evidence/(name+src.suffix);new_or_identical(dest,src.read_bytes());files.append(dict(role=name,**ref(dest)))
        cfgsha,inputs=_write_job(root,m,'primary',_config(manifest_path,m,'primary',files,prompt,negative_prompt))
        plan=dict(schema='sprite_regional_plan_v1',attempt_id=run_id+':attempt-01',source_sha256=p['canonical']['sha256'],historical_source=dict(history_bundle_sha256=p['history_bundle_sha256'],history_id=p['history_id']),**docs,primary_config_sha256=cfgsha,primary_inputs=inputs,profile='vace81_24_720_v1',cleanup_policy=CLEANUP_POLICY,output_policy=dict(frames=8,frame_ms=100,loop=True,grounded=False,canvas=q['canvas'],selection=media['selection'],geometry=media['geometry'],alignment_policy='body_x_preserve_y_v1'))
        new_or_identical(root/'regional-plan.json',encoded(plan));q['regional_plan_sha256']=scope_sha(plan)
        scope=dict(run_id=run_id,source=m['source'],request=q,authorization=auth)
        new_or_identical(root/'scope.json',encoded(scope));new_or_identical(root/'scope.sha256',(scope_sha(scope)+'\n').encode());m['authorization_scope_sha256']=scope_sha(scope)
        cleanup=root/'attempt-01/cleanup';cleanup.mkdir(parents=True)
        new_or_identical(cleanup/'plan.json',encoded(dict(schema='sprite_regional_pricing_request_v1',regional_plan_sha256=scope_sha(plan),role='cleanup',model=CLEANUP)))
        atomic_json(manifest_path,m)
        ledger.bind_run(task_manifest,dict(run_id=run_id,target_id=target_id,scope_sha256=scope_sha(scope),manifest_path=str(manifest_path)))
        validate_regional_attempt(str(manifest_path),require_artifacts=False)
        return generic._result(m['state'],manifest_path,m)
    except (OSError,ValueError,KeyError,TypeError,IndexError) as error:return generic._result(path=manifest_path,manifest=m,reason=getattr(error,'code','REGIONAL_PREPARE_INVALID'))


def _context(manifest_path):
    filename=path(str(Path(manifest_path).expanduser().absolute()));root=filename.parent;m=load(filename);scope=load(root/'scope.json');plan=load(root/'regional-plan.json')
    if (root.name!=m['run_id'] or m.get('schema_version')!=3 or m.get('mode')!='whole_sheet' or m.get('derivation')
            or (root/'scope.json').read_bytes()!=encoded(scope) or (root/'regional-plan.json').read_bytes()!=encoded(plan)
            or (root/'scope.sha256').read_bytes()!=(scope_sha(scope)+'\n').encode() or m.get('authorization_scope_sha256')!=scope_sha(scope)
            or any(m.get(k)!=scope.get(k) for k in ('run_id','request','source','authorization'))
            or scope['request']['route']!=ROUTE or scope['request']['regional_plan_sha256']!=scope_sha(plan)):fail('SCOPE_TAMPERED')
    if root.is_relative_to(generic.SKILL_ROOT):fail('WORKSPACE_NOT_ALLOWED')
    task=generic._task_state(filename,m);r=m['request']['task_ref'];aid=m['run_id']+':attempt-01'
    if len(m['attempts'])!=1 or m['attempts'][0]['index']!=1 or m['attempts'][0]['attempt_id']!=aid or m['authorization']['max_generation_attempts']!=1 or m['authorization']['request_authorized'] is not True:fail('REGIONAL_PLAN_INVALID')
    ledger._regional_plan(plan,dict(attempt_id=aid),task['targets'][r['target_id']])
    if plan['attempt_id']!=aid or plan['source_sha256']!=m['source']['sha256'] or m['source']['path']!=str(root/'source.png') or sha_file(root/'source.png')!=plan['source_sha256']:fail()
    if any(m['request'].get(k)!=v for k,v in plan['output_policy'].items() if k in ('frames','frame_ms','loop','grounded','canvas','alignment_policy')):fail()
    if sha_file(m['request']['ffmpeg_exe'])!=m['request']['ffmpeg_sha256']:fail('REGIONAL_RUNTIME_CHANGED')
    current=next((v for v in task['attempts'].values() if v['run_id']==m['run_id'] and v['attempt_id']==aid),None)
    if current and current['regional_plan_sha256']!=scope_sha(plan):fail('REGIONAL_PLAN_INVALID')
    return dict(path=filename,root=root,manifest=m,plan=plan,plan_sha256=scope_sha(plan),ref=r,ledger=task,current=current,attempt=m['attempts'][0],attempt_dir=root/'attempt-01',ffmpeg=m['request']['ffmpeg_exe'])


def _validate_job(c,role):
    job=c['attempt']['jobs'].get(role)
    if not job:fail('TASK_OPERATION_NOT_MATERIALIZED')
    directory=c['attempt_dir']/role
    if job['path']!=str(directory) or job['operation_id']!=c['plan']['attempt_id']+':'+role:fail()
    config=load(directory/'config.json');keys(config,('schema_version','route','role','model','input_files','input','authorization','max_cost_usd'))
    expected=_config(c['path'],c['manifest'],role,config['input_files'],config['input'].get('prompt'),config['input'].get('negative_prompt'))
    if encoded(config)!=encoded(expected):fail('JOB_CONFIG_TAMPERED')
    if role=='primary':text(config['input']['prompt']);text(config['input']['negative_prompt'])
    roles=['video','mask','identity'] if role=='primary' else ['grid']
    if not isinstance(config['input_files'],list) or len(config['input_files'])!=len(roles):fail()
    for item,name in zip(config['input_files'],roles):
        keys(item,('role','path','sha256'));check_ref({k:item[k] for k in ('path','sha256')})
        expected_ref=c['media'][name] if role=='primary' else ref(c['attempt_dir']/'sampling/sample-grid.png')
        if item['role']!=name or item['sha256']!=expected_ref['sha256']:fail('REGIONAL_INPUT_CHANGED')
    inputs=[dict(role=f['role'],sha256=f['sha256']) for f in config['input_files']];cfgsha=node_config_sha(config)
    if ((directory/'inputs.json').read_bytes()!=encoded(inputs) or job['config_sha256']!=cfgsha or job['transport_config_sha256']!=cfgsha
            or job['config_file_sha256']!=sha_file(directory/'config.json') or (directory/'config.sha256').read_bytes()!=(cfgsha+'\n').encode()):fail('JOB_CONFIG_TAMPERED')
    if role=='primary' and (cfgsha!=c['plan']['primary_config_sha256'] or inputs!=c['plan']['primary_inputs']):fail()
    operation=c['ledger']['operations'].get(job['operation_id'])
    if operation and operation.get('execution_binding'):
        binding=_binding(c,role,config)
        if operation['execution_binding']!=binding or operation['binding_sha256']!=scope_sha(binding):fail('TASK_OPERATION_COLLISION')
    statepath=directory/'job-state.json';state=load(statepath) if statepath.exists() else {}
    node_id=state.get('request_id');ledger_id=(operation or {}).get('provider_request_id');known=node_id or ledger_id
    if known:
        if (not re.fullmatch('[A-Za-z0-9_-]{1,128}',str(known)) or ledger_id not in (None,known)
                or not operation or not operation['submission_started'] or not operation.get('execution_binding')
                or statepath.exists() and state.get('config_sha256')!=cfgsha):fail('TASK_CONTEXT_CONFLICT')
        if not node_id and (not ledger_id or statepath.exists() and state.get('state') not in ('SUBMITTING','SUBMISSION_STATUS_UNKNOWN','REQUEST_ACCEPTED')):fail('TASK_CONTEXT_CONFLICT')
        receipt=state.get('pricing_receipt_sha256');quote=operation['current_quote']['quote_sha256'];claimpath=directory/'submission.claim'
        if receipt is not None and (not isinstance(receipt,str) or not re.fullmatch('[a-f0-9]{64}',receipt) or ledger_id and receipt!=quote):fail('TASK_CONTEXT_CONFLICT')
        # A durable ledger identity permits GET-only mirror recovery without a
        # local claim/receipt. Present evidence must still agree; Node-only IDs
        # require the original claim and its receipt before ledger adoption.
        if claimpath.exists():
            claim=load(claimpath);keys(claim,('schema_version','config_sha256','pricing_receipt_sha256','created_at'),'TASK_CONTEXT_CONFLICT')
            claim_receipt=claim['pricing_receipt_sha256']
            if (claim['schema_version']!=3 or claim['config_sha256']!=cfgsha or not isinstance(claim_receipt,str)
                    or not re.fullmatch('[a-f0-9]{64}',claim_receipt) or receipt is not None and claim_receipt!=receipt
                    or ledger_id and claim_receipt!=quote or not ledger_id and claim_receipt!=receipt):fail('TASK_CONTEXT_CONFLICT')
            try:datetime.fromisoformat(claim['created_at'].replace('Z','+00:00'))
            except (ValueError,TypeError,AttributeError):fail('TASK_CONTEXT_CONFLICT')
        elif not ledger_id:fail('TASK_CONTEXT_CONFLICT')
        if state.get('state')=='DOWNLOADED':
            raw=directory/('raw.mp4' if role=='primary' else 'raw.png')
            if state.get('raw_path')!=str(raw) or state.get('raw_sha256')!=sha_file(raw):fail('REGIONAL_RAW_CHANGED')
    if job.get('request_id') and job['request_id']!=known:fail('TASK_CONTEXT_CONFLICT')
    if job.get('state')=='DOWNLOADED' and any(job.get(k)!=state.get(k) for k in ('raw_path','raw_sha256','request_id')):fail('REGIONAL_RAW_CHANGED')
    return dict(config=config,job=job,operation=operation,state=state,request_id=known)


def validate_regional_attempt(manifest_path: str,*,require_artifacts: bool)->tuple:
    c=_context(manifest_path);plan=c['plan'];m=c['manifest']
    p=document(plan['provenance']);media=document(plan['media']);pre=document(plan['preflight'])
    if plan['historical_source']!=dict(history_bundle_sha256=p['history_bundle_sha256'],history_id=p['history_id']):fail('REGIONAL_HISTORY_MISMATCH')
    parent,tr=validate_provenance(c['ledger'],c['ref']['target_id'],p)
    geometry=validate_media(p,media,pre,parent,tr,c['ffmpeg'],plan['provenance']['sha256'],plan['media']['sha256'])
    if plan['output_policy']['geometry']!=geometry or plan['output_policy']['selection']!=media['selection']:fail('REGIONAL_GEOMETRY_INVALID')
    c.update(provenance=p,media=media,preflight=pre)
    for role in c['attempt']['jobs']:_validate_job(c,role)
    for stage,gate_ref in m.get('regional_gate_reports',{}).items():_validate_gate(c,document(gate_ref),stage)
    if c['current']:
        for stage,gate in c['current'].get('regional_gates',{}).items():_validate_gate(c,gate,stage)
    if c['attempt'].get('sampling'):_validate_sampling(c)
    if require_artifacts:
        generic._verify_artifacts(m);expected,qa=_assembly_values(c);dest=c['attempt_dir']/'artifacts'
        for name,payload in expected.items():
            if (dest/name).read_bytes()!=payload:fail('ARTIFACT_TAMPERED')
    return c['path'],m,c


def sample_regional_video(raw_path: str,output_dir: str,*,ffmpeg_exe: str,media: dict)->dict:
    exe=resolve_ffmpeg(ffmpeg_exe);raw=path(str(raw_path));out=Path(output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    decoded=decode(raw,exe);indices=media['selection']
    if len(indices)!=8 or indices!=sorted(set(indices)) or any(type(i) is not int or not 0<=i<81 for i in indices):fail('REGIONAL_SELECTION_INVALID')
    frames=[];paths=[]
    for number,index in enumerate(indices,1):
        payload=_ffmpeg(exe,['-i',str(raw),'-map','0:v:0','-vf',f'select=eq(n\\,{index})','-fps_mode','passthrough','-frames:v','1','-an','-f','image2pipe','-vcodec','png','pipe:1'],50*1024*1024,timeout=120)
        with Image.open(io.BytesIO(payload)) as opened:frame=opened.convert('RGB')
        if list(frame.size)!=decoded['canvas'] or hashlib.sha256(frame.tobytes()).hexdigest()!=decoded['hashes'][index]:fail('REGIONAL_SELECTION_INVALID')
        dest=out/f'frame-{number:02d}.png';new_or_identical(dest,png_bytes(frame));frames.append(frame);paths.append(str(dest))
    width,height=frames[0].size;grid=Image.new('RGB',(width*4,height*2))
    for i,frame in enumerate(frames):grid.paste(frame,((i%4)*width,(i//4)*height))
    gridpath=out/'sample-grid.png';new_or_identical(gridpath,png_bytes(grid))
    if grid.width>4096 or grid.height>4096 or gridpath.stat().st_size>10485760:fail('REGIONAL_CLEANUP_LIMIT')
    selection=dict(schema='sprite_regional_selection_v1',raw_sha256=sha_file(raw),frame_count=decoded['frame_count'],fps=24,result_canvas=decoded['canvas'],indices=indices,pts=[decoded['pts'][i] for i in indices],rgb_sha256=[decoded['hashes'][i] for i in indices],frame_sha256=[sha_file(p) for p in paths],grid_sha256=sha_file(gridpath))
    selection_path=out/'selection.json';new_or_identical(selection_path,encoded(selection))
    return dict(grid_path=str(gridpath),frame_paths=paths,selection_path=str(selection_path),selection=selection)


def _prepare_raw(c):
    primary=_validate_job(c,'primary');state=primary['state']
    if state.get('state')!='DOWNLOADED' or not primary['operation'] or not any(o['status']=='completed' for o in primary['operation']['outcomes']):fail('TASK_PARENT_OPERATION_INCOMPLETE')
    sampling=sample_regional_video(state['raw_path'],str(c['attempt_dir']/'sampling'),ffmpeg_exe=c['ffmpeg'],media=c['media'])
    raw=Path(state['raw_path']);decoded=decode(raw,c['ffmpeg']);probe=dict(schema='sprite_regional_probe_v1',raw_sha256=sha_file(raw),**decoded)
    new_or_identical(c['attempt_dir']/'probe.json',encoded(probe))
    geometry=c['plan']['output_policy']['geometry'];width,height=decoded['canvas']
    transform=dict(schema='sprite_regional_result_transform_v1',regional_plan_sha256=c['plan_sha256'],raw_sha256=sha_file(raw),probe_sha256=sha_file(c['attempt_dir']/'probe.json'),result_canvas=[width,height],resize_canvas=geometry['resize_canvas'],crop=geometry['crop'],output_canvas=geometry['output_canvas'],scale_numerator=geometry['scale_numerator'],scale_denominator=width,resampling='nearest')
    new_or_identical(c['attempt_dir']/'result-transform.json',encoded(transform));c['attempt']['sampling']=sampling
    return sampling


@lru_cache(maxsize=24)
def _validate_sample_images(files,grid_file,width,height,selected_hashes):
    """Reuse only physical image facts for freshly hashed identical bytes.

    Arguments include each PNG and grid digest, never mtime or disk facts.
    State, proofs, job identity and gates remain outside this process cache.
    """
    grid=Image.new('RGB',(width*4,height*2))
    for i,((filename,digest),expected) in enumerate(zip(files,selected_hashes)):
        frame=image(filename).convert('RGB')
        if frame.size!=(width,height) or hashlib.sha256(frame.tobytes()).hexdigest()!=expected:fail('REGIONAL_SELECTION_CHANGED')
        grid.paste(frame,((i%4)*width,(i//4)*height))
    if Path(grid_file[0]).read_bytes()!=png_bytes(grid):fail('REGIONAL_SELECTION_CHANGED')


def _validate_sampling(c):
    sampling=c['attempt'].get('sampling')
    if not sampling:fail('REGIONAL_RAW_REVIEW_REQUIRED')
    state=_validate_job(c,'primary')['state'];raw=state.get('raw_path')
    if state.get('state')!='DOWNLOADED':fail('TASK_PARENT_OPERATION_INCOMPLETE')
    decoded=decode(raw,c['ffmpeg']);indices=c['media']['selection'];width,height=decoded['canvas'];directory=c['attempt_dir']/'sampling'
    paths=[str(directory/f'frame-{i:02d}.png') for i in range(1,9)]
    if (sampling['frame_paths']!=paths or sampling['grid_path']!=str(directory/'sample-grid.png')
            or sampling['selection_path']!=str(directory/'selection.json')):fail('REGIONAL_SELECTION_CHANGED')
    gridpath=directory/'sample-grid.png'
    files=tuple((filename,sha_file(path(filename))) for filename in paths);grid_file=(str(gridpath),sha_file(path(str(gridpath))))
    _validate_sample_images(files,grid_file,width,height,tuple(decoded['hashes'][i] for i in indices))
    expected=dict(schema='sprite_regional_selection_v1',raw_sha256=sha_file(raw),frame_count=decoded['frame_count'],fps=24,result_canvas=decoded['canvas'],indices=indices,pts=[decoded['pts'][i] for i in indices],rgb_sha256=[decoded['hashes'][i] for i in indices],frame_sha256=[digest for filename,digest in files],grid_sha256=grid_file[1])
    if sampling['selection']!=expected or (directory/'selection.json').read_bytes()!=encoded(expected):fail('REGIONAL_SELECTION_CHANGED')
    if width*4>4096 or height*2>4096 or gridpath.stat().st_size>10485760:fail('REGIONAL_CLEANUP_LIMIT')
    probe=dict(schema='sprite_regional_probe_v1',raw_sha256=sha_file(raw),**decoded);pp=c['attempt_dir']/'probe.json'
    if pp.read_bytes()!=encoded(probe):fail('REGIONAL_RAW_CHANGED')
    g=c['plan']['output_policy']['geometry'];transform=dict(schema='sprite_regional_result_transform_v1',regional_plan_sha256=c['plan_sha256'],raw_sha256=sha_file(raw),probe_sha256=sha_file(pp),result_canvas=decoded['canvas'],resize_canvas=g['resize_canvas'],crop=g['crop'],output_canvas=g['output_canvas'],scale_numerator=g['scale_numerator'],scale_denominator=decoded['canvas'][0],resampling='nearest')
    if (c['attempt_dir']/'result-transform.json').read_bytes()!=encoded(transform):fail('REGIONAL_TRANSFORM_CHANGED')
    return sampling


def _subject(c,stage):
    if stage=='input_ready':return dict(regional_plan_sha256=c['plan_sha256'],preflight_sha256=c['plan']['preflight']['sha256'],media_sha256=c['plan']['media']['sha256'])
    sampling=_validate_sampling(c);primary=_validate_job(c,'primary')
    return dict(regional_plan_sha256=c['plan_sha256'],request_id=primary['request_id'],raw_sha256=primary['state']['raw_sha256'],probe_sha256=sha_file(c['attempt_dir']/'probe.json'),selection_sha256=sha_file(sampling['selection_path']),grid_sha256=sha_file(sampling['grid_path']),result_transform_sha256=sha_file(c['attempt_dir']/'result-transform.json'))


def _validate_gate(c,gate,stage):
    keys(gate,('schema','stage','decision','subject','evidence','findings'))
    if gate['schema']!='sprite_regional_gate_v1' or stage not in ('input_ready','raw_ready') or gate['stage']!=stage or gate['decision'] not in ('pass','hold','reject'):fail('REGIONAL_GATE_INVALID')
    check_ref(gate['evidence']);checks=set();findings=gate['findings']
    if not isinstance(findings,list) or not findings:fail('REGIONAL_GATE_INVALID')
    for finding in findings:
        keys(finding,('check','passed','notes'));text(finding['notes']);text(finding['check'])
        if finding['check'] in checks or type(finding['passed']) is not bool or gate['decision']=='pass' and not finding['passed']:fail('REGIONAL_GATE_INVALID')
        checks.add(finding['check'])
    if checks!=(INPUT_CHECKS if stage=='input_ready' else RAW_CHECKS):fail('REGIONAL_GATE_INVALID')
    if encoded(gate['subject'])!=encoded(_subject(c,stage)):fail('REGIONAL_GATE_STALE')
    return gate


def record_gate(manifest_path: str,report_file: str)->dict:
    m=None
    try:
        path_,m,c=validate_regional_attempt(manifest_path,require_artifacts=False)
        with run_lock(path_.parent):
            path_,m,c=validate_regional_attempt(manifest_path,require_artifacts=False);gate=load(report_file);stage=gate['stage'];_validate_gate(c,gate,stage)
            destination=c['attempt_dir']/(stage+'-'+scope_sha(gate)+'.json');new_or_identical(destination,encoded(gate))
            old=m.get('regional_gate_reports',{}).get(stage)
            if old and document(old)['decision']=='reject' and document(old)!=gate:fail('TASK_ATTEMPT_CLOSED')
            if c['current'] and c['current'].get('regional_plan'):ledger.record_regional_gate(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],gate)
            m.setdefault('regional_gate_reports',{})[stage]=ref(destination)
            return generic._save(path_,m,'REGIONAL_GATE_RECORDED' if gate['decision']=='pass' else 'REGIONAL_GATE_'+gate['decision'].upper())
    except (OSError,ValueError,KeyError,TypeError,IndexError) as error:return generic._result(path=manifest_path,manifest=m,reason=getattr(error,'code','REGIONAL_GATE_INVALID'))


def _local_pricing(model,api,public,schema):
    script="""import fs from 'node:fs';import{validateRegionalPricingEvidence as validate}from MODULE;
const x=JSON.parse(fs.readFileSync(0,'utf8'));const d=v=>v===null?null:Buffer.from(v,'base64');process.stdout.write(JSON.stringify(validate(x.model,d(x.api),d(x.public),d(x.schema))));""".replace('MODULE',json.dumps(Path(__file__).with_name('fal_sheet_transport.mjs').as_uri()))
    payload=encoded(dict(model=model,api=base64.b64encode(api).decode(),public=None if public is None else base64.b64encode(public).decode(),schema=base64.b64encode(schema).decode()))
    env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','USERPROFILE','HOME','PATH') if k in os.environ}
    result=subprocess.run([generic._node_exe(),'--input-type=module','-e',script],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=12,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode or len(result.stdout)>65536 or len(result.stderr)>65536:fail('PRICE_RECEIPT_INVALID')
    return json.loads(result.stdout)


def _validate_quote(c,role,quote_sha,amount,*,planning=False,fresh=True):
    directory=c['attempt_dir']/role/('planning-pricing-receipts' if planning else 'pricing-receipts')
    candidates=[p for p in directory.glob('*.receipt.json') if sha_file(p)==quote_sha]
    if len(candidates)!=1:fail('PRICE_RECEIPT_INVALID')
    filename=candidates[0];q=load(filename)
    fields=('schema','receipt_id','kind','regional_plan_sha256','role','model','config_sha256','input_set_sha256','retrieved_at','expires_at','currency','api_unit','unit_price_usd','billing_frames','billing_fps','estimated_units','reserved_units','estimated_amount_usd','reserved_amount_usd','sources')
    keys(q,fields,'PRICE_RECEIPT_INVALID');model=VACE if role=='primary' else CLEANUP
    reserved=7 if role=='primary' else 1;units='5.0625' if role=='primary' else '1'
    cfg=None if planning else _validate_job(c,role)['config'];inputs=None if planning else [dict(role=f['role'],sha256=f['sha256']) for f in cfg['input_files']]
    expected=dict(schema='sprite_regional_quote_v1',kind='planning' if planning else 'execution',regional_plan_sha256=c['plan_sha256'],role=role,model=model,config_sha256=None if planning else node_config_sha(cfg),input_set_sha256=None if planning else scope_sha(inputs),currency='USD',api_unit='seconds' if role=='primary' else 'images',billing_frames=81 if role=='primary' else None,billing_fps=16 if role=='primary' else None,estimated_units=units,reserved_units=reserved)
    if any(q[k]!=v or type(q[k]) is not type(v) for k,v in expected.items()):fail('PRICE_RECEIPT_INVALID')
    rid=uuid.UUID(q['receipt_id'])
    if rid.version!=4 or str(rid)!=q['receipt_id']:fail('PRICE_RECEIPT_INVALID')
    retrieved=datetime.fromisoformat(q['retrieved_at'].replace('Z','+00:00'));expires=datetime.fromisoformat(q['expires_at'].replace('Z','+00:00'))
    if (retrieved.utcoffset()!=timedelta(0) or expires.utcoffset()!=timedelta(0) or expires-retrieved!=timedelta(minutes=10)
            or filename.name!=f'{int(retrieved.timestamp()*1000):016d}-{rid}.receipt.json'):fail('PRICE_RECEIPT_INVALID')
    if fresh and not -timedelta(seconds=30)<=datetime.now(timezone.utc)-retrieved<=timedelta(minutes=10):fail('PRICE_STALE')
    layout=[('api','https://api.fal.ai/v1/models/pricing?'+urlencode(dict(endpoint_id=model)))]+([] if role=='cleanup' else [('public','https://fal.ai/models/'+model)])+[('schema','https://fal.ai/api/openapi/queue/openapi.json?'+urlencode(dict(endpoint_id=model)))]
    if not isinstance(q['sources'],list) or len(q['sources'])!=len(layout):fail('PRICE_RECEIPT_INVALID')
    buffers={}
    for s,(kind,url) in zip(q['sources'],layout):
        keys(s,('source_url','retrieved_at','body_sha256','archive_file','selected'),'PRICE_RECEIPT_INVALID')
        if s['source_url']!=url or s['retrieved_at']!=q['retrieved_at'] or s['archive_file']!=str(rid)+'.'+kind+'.body':fail('PRICE_RECEIPT_INVALID')
        fp=directory/s['archive_file']
        if sha_file(fp)!=s['body_sha256'] or fp.stat().st_size>5*1024*1024:fail('PRICE_RECEIPT_INVALID')
        buffers[kind]=fp.read_bytes()
    selections=_local_pricing(model,buffers['api'],buffers.get('public'),buffers['schema'])
    if any(s['selected']!=selections[kind] for s,(kind,_) in zip(q['sources'],layout)):fail('PRICE_RECEIPT_INVALID')
    base=Decimal(ledger._money(q['unit_price_usd'],True))
    with localcontext() as ctx:
        ctx.prec=512
        if (base!=Decimal(str(selections['api']['unit_price_usd'])) or q['reserved_amount_usd']!=ledger._money(base*reserved,True)
                or q['estimated_amount_usd']!=ledger._money(base*Decimal(units),True) or ledger._money(amount,True)!=q['reserved_amount_usd']):fail('PRICE_RECEIPT_INVALID')
    return q


def quote(manifest_path: str,*,transport=None)->dict:
    m=None
    try:
        path_,m,c=validate_regional_attempt(manifest_path,require_artifacts=False)
        with run_lock(path_.parent):
            path_,m,c=validate_regional_attempt(manifest_path,require_artifacts=False)
            if c['current'] and any(o['submission_started'] for o in c['ledger']['operations'].values() if o['run_id']==m['run_id']):fail('TASK_PLAN_SUBMITTED')
            quotes={}
            for role,command in [('primary','pricing'),('cleanup','pricing-plan')]:
                response=(transport or generic._transport)(command,c['attempt_dir']/role)
                if response.get('state')!='PRICED':return generic._result(response.get('state','BLOCKED'),path_,m,response.get('block_reason'))
                q=response['quote'];_validate_quote(c,role,q['receipt_sha256'],q['estimated_cost_usd_decimal'],planning=role=='cleanup')
                quotes[role]=dict(quote_sha256=q['receipt_sha256'],amount_usd=q['estimated_cost_usd_decimal'])
            _,_,fresh=validate_regional_attempt(str(path_),require_artifacts=False)
            if fresh['current'] and fresh['current'].get('regional_plan'):
                ledger.reprice_plan(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],_operations(c,quotes),regional_plan=c['plan'])
            elif sum(Decimal(q['amount_usd']) for q in quotes.values())>Decimal(fresh['ledger']['available_estimate_usd']):fail('TASK_BUDGET_EXCEEDED')
            m['regional_quotes']=quotes
            return generic._save(path_,m,'REGIONAL_QUOTED')
    except (OSError,ValueError,KeyError,TypeError,IndexError,subprocess.SubprocessError) as error:return generic._result(path=manifest_path,manifest=m,reason=getattr(error,'code','REGIONAL_QUOTE_INVALID'))


def _binding(c,role,config):
    directory=c['attempt_dir']/role;inputs=[dict(role=f['role'],sha256=f['sha256']) for f in config['input_files']]
    deferred=dict(schema='sprite_regional_deferred_v1',regional_plan_sha256=c['plan_sha256'],role='cleanup') if role=='cleanup' else None
    return dict(schema='sprite_regional_execution_binding_v1',config_sha256=node_config_sha(config),config_file_sha256=sha_file(directory/'config.json'),input_set_sha256=scope_sha(inputs),provenance_sha256=c['plan']['provenance']['sha256'],deferred_plan_sha256=scope_sha(deferred) if deferred else None)


def _operations(c,quotes):
    aid=c['attempt']['attempt_id']
    return [dict(operation_id=aid+':primary',kind='provider_animation',config_sha256=c['plan']['primary_config_sha256'],**quotes['primary']),dict(operation_id=aid+':cleanup',kind='alpha_edge_cleanup',deferred_config=dict(schema='sprite_regional_deferred_v1',regional_plan_sha256=c['plan_sha256'],role='cleanup'),**quotes['cleanup'])]


def _gate(c,stage):
    gate=(c.get('current') or {}).get('regional_gates',{}).get(stage)
    if not gate or gate['decision']!='pass':fail('REGIONAL_'+('INPUT' if stage=='input_ready' else 'RAW')+'_REVIEW_REQUIRED')
    return _validate_gate(c,gate,stage)


def regional_bridge_run(message: dict,before: bool)->dict:
    fields=('schema','job_dir','config_sha256','config_file_sha256','input_set_sha256')+(('quote_sha256','amount_usd','regional_state_sha256') if before else ())
    keys(message,fields,'TASK_BRIDGE_INPUT_INVALID')
    if message['schema']!=('sprite_regional_before_submit_v1' if before else 'sprite_regional_transport_context_v1'):fail('TASK_BRIDGE_INPUT_INVALID')
    directory=path(message['job_dir']);role=directory.name
    if role not in ('primary','cleanup') or directory.parent.name!='attempt-01':fail('TASK_SCOPE_MISMATCH')
    _,m,c=validate_regional_attempt(str(directory.parent.parent/'run-manifest.json'),require_artifacts=False)
    verified=_validate_job(c,role);op=verified['operation'];known=verified['request_id']
    if not op or not op.get('execution_binding'):fail('TASK_OPERATION_NOT_MATERIALIZED')
    binding=op['execution_binding']
    if any(message[k]!=binding[k] for k in ('config_sha256','config_file_sha256','input_set_sha256')):fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    _gate(c,'input_ready' if role=='primary' else 'raw_ready');marked=None
    if before and known:marked=dict(submit_allowed=False)
    elif before:
        for remaining in (('primary','cleanup') if role=='primary' else ('cleanup',)):
            pending=c['ledger']['operations'][c['attempt']['attempt_id']+':'+remaining];q=pending['current_quote']
            _validate_quote(c,remaining,q['quote_sha256'],q['amount_usd'],planning=remaining=='cleanup' and not pending.get('execution_binding'))
        if message['quote_sha256']!=op['current_quote']['quote_sha256'] or message['amount_usd']!=op['current_quote']['amount_usd']:fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        # No manifest write in this callback: Node compares its exact physical
        # bytes after compare-and-mark and before the durable claim and POST.
        _,_,fresh=validate_regional_attempt(str(c['path']),require_artifacts=False)
        if fresh['current']['regional_state_sha256']!=message['regional_state_sha256']:fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        expected=dict(schema='sprite_regional_submission_expected_v1',config_sha256=message['config_sha256'],binding_sha256=op['binding_sha256'],quote_sha256=message['quote_sha256'],amount_usd=message['amount_usd'],regional_state_sha256=message['regional_state_sha256'])
        marked=ledger.mark_submission(c['ref']['task_manifest'],op['operation_id'],expected=expected)
        task=ledger.inspect_task(c['ref']['task_manifest']);op=task['operations'][op['operation_id']];current=task['attempts'][op['attempt_key']]
        if current['regional_state_sha256']!=marked['regional_state_sha256'] or op['binding_sha256']!=scope_sha(binding):fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        c.update(ledger=task,current=current)
    result=dict(schema='sprite_regional_transport_context_result_v1',state='SUBMISSION_AUTHORIZED' if before else 'CONTEXT_VERIFIED',task_id=c['ref']['task_id'],target_id=c['ref']['target_id'],run_id=m['run_id'],attempt_id=c['attempt']['attempt_id'],operation_id=op['operation_id'],config_sha256=binding['config_sha256'],config_file_sha256=binding['config_file_sha256'],input_set_sha256=binding['input_set_sha256'],binding_sha256=op['binding_sha256'],provider_request_id=known or op.get('provider_request_id'),submission_started=op['submission_started'],status=op['status'],outcome_status=op['outcomes'][-1]['status'] if op['outcomes'] else None,regional_state_sha256=c['current']['regional_state_sha256'])
    if before:result['submit_allowed']=marked['submit_allowed']
    return result


def _capture(c,role,response):
    verified=_validate_job(c,role);job=verified['job'];op=verified['operation'];state=verified['state'];known=verified['request_id']
    if known:
        job['request_id']=known;status={'DOWNLOADED':'completed','FAILED':'failed','CANCELLED':'cancelled','REMOTE_REJECTED':'rejected','REQUEST_ACCEPTED':'accepted'}.get(state.get('state'),'pending')
        if op['outcomes'] and op['outcomes'][-1]['status'] in ('completed','failed','cancelled','rejected'):status=op['outcomes'][-1]['status']
        ledger.record_outcome(c['ref']['task_manifest'],job['operation_id'],dict(status=status,provider_request_id=known,evidence=str(Path(job['path'])/'job-state.json')+'#sha256='+sha_file(Path(job['path'])/'job-state.json')))
        job['state']=state['state']
        if state['state']=='DOWNLOADED':job.update(raw_path=state['raw_path'],raw_sha256=state['raw_sha256'])
    elif op['submission_started']:
        ledger.record_outcome(c['ref']['task_manifest'],job['operation_id'],dict(status='uncertain',evidence=str(Path(job['path']))+'#marker_without_id'))
        response=dict(state='SUBMISSION_STATUS_UNKNOWN',block_reason='SUBMISSION_STATUS_UNKNOWN',retry_safe=False);job['state']='SUBMISSION_STATUS_UNKNOWN'
    atomic_json(c['path'],c['manifest']);return response


def _execute(c,role,transport,deadline):
    while True:
        response=transport('execute',c['attempt_dir']/role)
        _,m,c=validate_regional_attempt(str(c['path']),require_artifacts=False);response=_capture(c,role,response)
        if response.get('state') not in generic.PENDING or time.monotonic()>=deadline:return response
        remaining=deadline-time.monotonic()
        if remaining<2:return response
        time.sleep(min(2,remaining));_,_,c=validate_regional_attempt(str(c['path']),require_artifacts=False)


def _assembly_values(c):
    _gate(c,'raw_ready');sampling=_validate_sampling(c);cleanup=_validate_job(c,'cleanup');state=cleanup['state']
    if state.get('state')!='DOWNLOADED' or not any(o['status']=='completed' for o in cleanup['operation']['outcomes']):fail('TASK_PARENT_OPERATION_INCOMPLETE')
    grid=image(state['raw_path']);width,height=sampling['selection']['result_canvas']
    if grid.size!=(width*4,height*2):fail('REGIONAL_CLEANUP_DIMENSIONS_CHANGED')
    transform=load(c['attempt_dir']/'result-transform.json');x,y,w,h=transform['crop'];frames=[]
    for i in range(8):
        frame=grid.crop(((i%4)*width,(i//4)*height,(i%4+1)*width,(i//4+1)*height)).resize(tuple(transform['resize_canvas']),Image.Resampling.NEAREST)
        outside=frame.copy();outside.paste((0,0,0,0),(x,y,x+w,y+h))
        if outside.getchannel('A').getbbox() is not None:fail('REGIONAL_CROP_SUBJECT_CLIPPED')
        frame=frame.crop((x,y,x+w,y+h))
        if frame.getchannel('A').getbbox() is None:fail('CELL_EMPTY')
        frames.append(frame)
    from sprite_alignment_workflow import _artifacts
    report=dict(policy='body_x_preserve_y_v1',stage='regional_common_inverse_map',result_transform_sha256=sha_file(c['attempt_dir']/'result-transform.json'),scale_numerator=transform['scale_numerator'],scale_denominator=transform['scale_denominator'],crop=transform['crop'])
    return _artifacts(frames,c['manifest']['request'],report)


def _assemble(c):
    payloads,qa=_assembly_values(c);directory=c['attempt_dir']/'artifacts';directory.mkdir(exist_ok=True)
    for name,payload in payloads.items():new_or_identical(directory/name,payload)
    m=c['manifest'];m['artifact_paths']=dict(sheet=str(directory/'sprite-sheet.png'),preview_gif=str(directory/'preview.gif'),contact=str(directory/'contact.png'),qa_report=str(directory/'qa.json'),frames=[str(directory/f'frame-{i:02d}.png') for i in range(1,9)])
    sampling=c['attempt']['sampling'];files=[str(directory/name) for name in payloads]+[sampling['selection_path'],sampling['grid_path'],*sampling['frame_paths']]
    m['artifact_hashes']={p:sha_file(p) for p in files};m['consistency']=qa['consistency']
    return generic._save(c['path'],m,generic.READY)


def run(manifest_path: str,*,transport=None,poll_seconds=45)->dict:
    m=None;path_=Path(manifest_path).expanduser().resolve()
    try:
        poll=generic._poll(poll_seconds);transport=transport or generic._transport
        path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
        with run_lock(path_.parent):
            path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            if m['state'] in generic.ASSEMBLED:return generic.inspect(str(path_))
            if not c['current']:
                gate_ref=m.get('regional_gate_reports',{}).get('input_ready')
                if not gate_ref or document(gate_ref)['decision']!='pass':fail('REGIONAL_INPUT_REVIEW_REQUIRED')
                _validate_gate(c,document(gate_ref),'input_ready');quotes=m.get('regional_quotes')
                if not quotes:fail('REGIONAL_QUOTE_REQUIRED')
                for role in ('primary','cleanup'):_validate_quote(c,role,quotes[role]['quote_sha256'],quotes[role]['amount_usd'],planning=role=='cleanup')
                if sum(Decimal(v['amount_usd']) for v in quotes.values())>Decimal(c['ledger']['available_estimate_usd']):fail('TASK_BUDGET_EXCEEDED')
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
                descriptor=dict(schema='sprite_regional_strategy_v1',route=ROUTE,family='spatial_video_mask_reference_v1',model=VACE,conditioning='video_mask_reference',inputs=[dict(role='source',sha256=c['plan']['source_sha256'])])
                strategy=ledger.begin_attempt(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],descriptor,max_attempts=1,regional_plan_sha256=c['plan_sha256'])
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
                ledger.reserve_plan(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],strategy,_operations(c,quotes),regional_plan=c['plan'])
                ledger.record_regional_gate(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],document(gate_ref))
                c['attempt']['strategy_sha256']=strategy;atomic_json(path_,m);path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            elif not c['current'].get('regional_plan'):
                quotes=m.get('regional_quotes');gate=document(m['regional_gate_reports']['input_ready'])
                for role in ('primary','cleanup'):_validate_quote(c,role,quotes[role]['quote_sha256'],quotes[role]['amount_usd'],planning=role=='cleanup')
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
                ledger.reserve_plan(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],c['current']['strategy_sha256'],_operations(c,quotes),regional_plan=c['plan'])
                ledger.record_regional_gate(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],gate);path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            if 'input_ready' not in c['current'].get('regional_gates',{}):
                ledger.record_regional_gate(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],document(m['regional_gate_reports']['input_ready']));path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            _gate(c,'input_ready');primary=_validate_job(c,'primary')
            if not primary['operation'].get('execution_binding'):ledger.bind_operation_config(c['ref']['task_manifest'],primary['job']['operation_id'],_binding(c,'primary',primary['config']))
            path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False);deadline=time.monotonic()+poll
            if _validate_job(c,'primary')['state'].get('request_id'):
                _capture(c,'primary',dict(state=_validate_job(c,'primary')['state']['state']))
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            if _validate_job(c,'primary')['state'].get('state')!='DOWNLOADED':
                response=_execute(c,'primary',transport,deadline)
                if response.get('state')!='DOWNLOADED':return generic._result(response.get('state','BLOCKED'),path_,load(path_),response.get('block_reason'),response.get('retry_safe',False))
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            if not c['attempt'].get('sampling'):
                _prepare_raw(c);generic._save(path_,m,'REGIONAL_RAW_REVIEW_REQUIRED');path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            _gate(c,'raw_ready')
            if 'cleanup' not in c['attempt']['jobs']:
                sampling=_validate_sampling(c);cfg=_config(path_,m,'cleanup',[dict(role='grid',**ref(sampling['grid_path']))]);_write_job(c['root'],m,'cleanup',cfg);atomic_json(path_,m);path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            cleanup=_validate_job(c,'cleanup')
            if cleanup['state'].get('request_id'):
                _capture(c,'cleanup',dict(state=cleanup['state']['state']))
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False);cleanup=_validate_job(c,'cleanup')
            if not cleanup['operation']['submission_started']:
                response=transport('pricing',c['attempt_dir']/'cleanup')
                if response.get('state')!='PRICED':return generic._result(response.get('state','BLOCKED'),path_,m,response.get('block_reason'))
                q=response['quote'];_validate_quote(c,'cleanup',q['receipt_sha256'],q['estimated_cost_usd_decimal'])
                quotes={r:c['ledger']['operations'][c['attempt']['attempt_id']+':'+r]['current_quote'] for r in ('primary','cleanup')};quotes['cleanup']=dict(quote_sha256=q['receipt_sha256'],amount_usd=q['estimated_cost_usd_decimal'])
                ledger.reprice_plan(c['ref']['task_manifest'],m['run_id'],c['attempt']['attempt_id'],_operations(c,quotes),regional_plan=c['plan'])
                ledger.bind_operation_config(c['ref']['task_manifest'],cleanup['job']['operation_id'],_binding(c,'cleanup',cleanup['config']));path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            for role in ('primary','cleanup'):
                job=c['attempt']['jobs'][role];op=c['ledger']['operations'][job['operation_id']];job['reserved_usd']=float(op['amount_usd'])
            atomic_json(path_,m)
            if _validate_job(c,'cleanup')['state'].get('state')!='DOWNLOADED':
                response=_execute(c,'cleanup',transport,deadline)
                if response.get('state')!='DOWNLOADED':return generic._result(response.get('state','BLOCKED'),path_,load(path_),response.get('block_reason'),response.get('retry_safe',False))
                path_,m,c=validate_regional_attempt(str(path_),require_artifacts=False)
            return _assemble(c)
    except (OSError,ValueError,KeyError,TypeError,IndexError,subprocess.SubprocessError) as error:
        reason=getattr(error,'code','REGIONAL_WORKFLOW_INTERRUPTED');state=reason if reason in ('REGIONAL_INPUT_REVIEW_REQUIRED','REGIONAL_RAW_REVIEW_REQUIRED') else 'BLOCKED'
        return generic._result(state,path_,m,reason)


def resume(manifest_path: str,*,transport=None,poll_seconds=45)->dict:return run(manifest_path,transport=transport,poll_seconds=poll_seconds)


def inspect(manifest_path: str)->dict:
    try:
        path_,m,c=validate_regional_attempt(manifest_path,require_artifacts=load(manifest_path).get('state') in generic.ASSEMBLED)
        if m['state'] in generic.ASSEMBLED:generic._verify_visual_state(m)
        return generic._result(m['state'],path_,m,m.get('block_reason'),m.get('retry_safe',False))
    except (OSError,ValueError,KeyError,TypeError,IndexError) as error:return generic._result(path=manifest_path,reason=getattr(error,'code','REGIONAL_EVIDENCE_INVALID'))
