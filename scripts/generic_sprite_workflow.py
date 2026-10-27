"""Single-operation v3 sprite generation. Provider transport is the only network boundary."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from PIL import Image
from sheet_images import assemble_sheet, consistency_diagnostics, SheetImageError
from sheet_video import prepare_source, sample_video, resolve_ffmpeg, VideoError
from sheet_workflow_state import WorkflowError, atomic_json, encoded, new_or_identical, read_json, run_lock, scope_sha, sha_file
import sprite_task_ledger as task_ledger

NANO='fal-ai/nano-banana-2/edit'
H3='minimax/h3-max/image-to-video'
CLEANUP='fal-ai/ideogram/remove-background'
SKILL_ROOT=Path(__file__).resolve().parent.parent
READY='READY_FOR_VISUAL_REVIEW'
DELIVERABLE='READY_FOR_DELIVERY'
VISUAL_REJECTED='VISUAL_REJECTED'
ASSEMBLED={READY,DELIVERABLE,VISUAL_REJECTED}
VISUAL_CHECKS={'identity','scale','orientation','framing','background','motion','loop'}
PENDING={'IN_QUEUE','IN_PROGRESS','REQUEST_ACCEPTED','COMPLETED','RESULT_READY'}
TECHNICAL={'GRID_INVALID','CELL_EMPTY','CELL_CLIPPED','DUPLICATE_FRAME'}
CYCLE_REJECTIONS={'CYCLE_UNRELIABLE_ZERO_MOTION','CYCLE_UNRELIABLE_TOO_SHORT','CYCLE_UNRELIABLE_FLAT_SCORES'}

def _normal(text):return ''.join(c for c in unicodedata.normalize('NFKD',text.strip().lower()) if not unicodedata.combining(c))
ACTIONS={}
for canonical,aliases in {'walk':['walk','walking','caminar','caminando','camina'],'run':['run','running','correr','corriendo'],'idle':['idle','reposo','quieto'],'attack':['attack','atacar','ataque'],'hit':['hit','golpe','herido'],'death':['death','morir','muerte'],'fly':['fly','flying','volar','volando']}.items():
    ACTIONS.update({a:canonical for a in aliases})
ORIENTATIONS={}
for canonical,aliases in {'left':['left','izquierda','izquierdo','west','w','o','oeste'],'right':['right','derecha','derecho','east','e','este'],'front':['front','frente','frontal'],'back':['back','espalda','atras','detras'],'north':['north','n','norte'],'south':['south','s','sur'],'northeast':['northeast','ne','noreste'],'northwest':['northwest','nw','no','noroeste'],'southeast':['southeast','se','sureste','sudeste'],'southwest':['southwest','sw','so','suroeste','sudoeste']}.items():
    ORIENTATIONS.update({a:canonical for a in aliases})

def _result(state='BLOCKED', path=None, manifest=None, reason=None, retry=False, *, task_snapshot=None, **extra):
    m=manifest or {};allowed=state==DELIVERABLE
    action='export_reviewed_artifacts' if allowed else 'inspect_artifacts_and_record_visual_review' if state==READY else 'correct_rejected_artifacts_within_authorization' if state==VISUAL_REJECTED else 'provide_missing_fields' if state=='NEEDS_INPUT' else 'resume' if retry else 'inspect_diagnosis'
    if state==READY and m.get('request',{}).get('alignment_policy') and not m.get('derivation'):
        action='annotate_homologous_body_point_and_align_body_x_before_visual_pass'
    budget={}
    if _managed(m) or task_snapshot is not None:
        try:
            task=task_snapshot if task_snapshot is not None else task_ledger.inspect_task(m['request']['task_ref']['task_manifest'])
            budget={'task_budget':{key:task[key] for key in ('cap_usd','reserved_total_usd','available_estimate_usd','uncertainty')}}
            budget['task_budget'].update(budget_basis=task['authorization']['budget_basis'],invoice_verified=False)
            if 'history_status' in task:budget['history_status']=task['history_status']
            prospective_regional=(m.get('request',{}).get('route')=='regional_video_inpaint_v1' and m.get('attempts')
                and not any(a['run_id']==m.get('run_id') and a['attempt_id']==m['attempts'][-1]['attempt_id'] for a in task['attempts'].values()))
            if allowed and prospective_regional:
                state='BLOCKED';allowed=False;reason='TASK_STRATEGY_MISMATCH';action='inspect_diagnosis'
            if m.get('attempts') and not prospective_regional:
                projection=_task_review_state(m,task=task)
                budget['effective_defects']=projection['effective_defects']
                if allowed and (projection.get('review',{}).get('decision')!='pass' or projection['effective_defects']):
                    state='BLOCKED';allowed=False;reason='TASK_SOURCE_DEFECT_UNRESOLVED' if projection['effective_defects'] else 'VISUAL_REVIEW_REQUIRED';action='inspect_diagnosis'
        except (OSError,ValueError,KeyError,TypeError) as error:
            budget={'task_budget':None}
            if allowed:state='BLOCKED';allowed=False;reason=getattr(error,'code','TASK_SCOPE_MISMATCH');action='inspect_diagnosis'
        budget['spent_estimate_basis']='local reservation estimate; not invoice or additional task cost'
    return {'state':state,'run_manifest':str(path) if path else None,'artifact_paths':m.get('artifact_paths',{}),
            'delivery_allowed':allowed,'delivery_artifact_paths':m.get('artifact_paths',{}) if allowed else {},
            'human_acceptance':False,'artifact_set_sha256':_review_target(m) if m.get('artifact_hashes') else None,
            'consistency':m.get('consistency'),
            'spent_estimate_usd':round(sum(j.get('reserved_usd',0) for a in m.get('attempts',[]) for j in a.get('jobs',{}).values()),12),
            'next_action':action,'retry_safe':retry,'block_reason':reason,'defaults_notice':m.get('defaults_notice'),**budget,**extra}


def _review_target(m):
    return scope_sha({'run_id':m['run_id'],'attempt':len(m['attempts']),'artifact_hashes':m['artifact_hashes']})

def _bool(value,default):
    if value is None or value=='auto':return default
    if value is True or value=='true':return True
    if value is False or value=='false':return False
    raise WorkflowError('INPUT_INVALID')

def _options(options):
    missing=[k for k in ('source','action','orientation') if not options.get(k)]
    if missing:return None,missing
    action=options['action'];orientation=options['orientation']
    if not isinstance(action,str) or not 1<=len(action.strip())<=200 or not isinstance(orientation,str):raise WorkflowError('INPUT_INVALID')
    action=ACTIONS.get(_normal(action),action.strip());orientation=ORIENTATIONS.get(_normal(orientation))
    if orientation is None:raise WorkflowError('ORIENTATION_INVALID')
    try:source=Path(options['source']).expanduser().resolve(strict=True)
    except (OSError,ValueError):raise WorkflowError('SOURCE_INVALID') from None
    if not source.is_file() or source.stat().st_size>20*1024*1024:raise WorkflowError('SOURCE_INVALID')
    try:
        payload=source.read_bytes()
        with Image.open(io.BytesIO(payload)) as im:
            if im.format!='PNG' or im.width*im.height>64_000_000:raise WorkflowError('SOURCE_INVALID')
            size=im.size
            if im.convert('RGBA').getchannel('A').getbbox() is None:raise WorkflowError('SOURCE_INVALID')
    except (OSError,ValueError,Image.DecompressionBombError):raise WorkflowError('SOURCE_INVALID') from None
    frames=options.get('frames',8);ms=options.get('frame_ms',100);attempts=options.get('max_generation_attempts',2);cap=options.get('max_cost_usd',.50)
    if any(not isinstance(v,int) or isinstance(v,bool) for v in (frames,ms,attempts)) or not 2<=frames<=16 or not 20<=ms<=1000 or ms%10 or not 1<=attempts<=3:raise WorkflowError('INPUT_INVALID')
    if not isinstance(cap,(int,float)) or isinstance(cap,bool) or not math.isfinite(cap) or cap<0:raise WorkflowError('INPUT_INVALID')
    canvas=options.get('canvas','source')
    if canvas=='source':canvas=list(size)
    elif isinstance(canvas,str) and re.fullmatch(r'\d+x\d+',canvas):canvas=[int(v) for v in canvas.split('x')]
    else:raise WorkflowError('INPUT_INVALID')
    if any(not 16<=v<=4096 for v in canvas) or canvas[0]*canvas[1]*frames>64_000_000:raise WorkflowError('INPUT_INVALID')
    loop=_bool(options.get('loop'),action in {'walk','run','idle','fly'});pixel_art=_bool(options.get('pixel_art'),True)
    strategy=options.get('strategy','auto')
    if strategy not in {'auto','sheet','video'}:raise WorkflowError('INPUT_INVALID')
    route='video' if strategy=='video' or strategy=='auto' and action=='walk' and orientation in {'left','right'} else 'sheet'
    start=options.get('cycle_start_seconds');duration=options.get('cycle_duration_seconds')
    if (start is None)!=(duration is None):raise WorkflowError('CYCLE_OPTIONS_INVALID')
    if start is not None and (route!='video' or any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in (start,duration)) or start<0 or duration<=0 or start+duration>30 or round(duration*24)<frames):raise WorkflowError('CYCLE_OPTIONS_INVALID')
    description=options.get('description')
    if description is not None and (not isinstance(description,str) or len(description)>2000):raise WorkflowError('INPUT_INVALID')
    workspace=Path(options.get('workspace_root') or Path.home()/'.codex/work/tower-sprite-pipeline/sheet-runs').expanduser().resolve()
    installed=Path.home()/'.codex/skills'
    if workspace==SKILL_ROOT or SKILL_ROOT in workspace.parents or workspace==installed or installed in workspace.parents:raise WorkflowError('WORKSPACE_NOT_ALLOWED')
    ffmpeg=resolve_ffmpeg(options.get('ffmpeg_exe')) if route=='video' else None
    # All new routes preserve source-cell Y; body X is reviewed in a local derivation.
    grounded=False
    request={'action':action,'orientation':orientation,'frames':frames,'frame_ms':ms,'canvas':canvas,'loop':loop,'pixel_art':pixel_art,'grounded':grounded,'alignment_policy':'body_x_preserve_y_v1','columns':min(4,frames),'rows':math.ceil(frames/min(4,frames)),'strategy':strategy,'route':route,'description':description,'cycle_start_seconds':start,'cycle_duration_seconds':duration,'ffmpeg_exe':ffmpeg}
    return {'source':source,'payload':payload,'request':request,'workspace':workspace,'authorization':{'source':'session_request','request_authorized':True,'max_cost_usd':float(cap),'max_generation_attempts':attempts}},None

def _poll(value):
    if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or not 0<=value<=300:raise WorkflowError('POLL_SECONDS_INVALID')
    return value

def _node_exe():
    configured=os.environ.get('TOWER_SPRITE_NODE_EXE')
    candidates=[configured] if configured else [str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'),shutil.which('node')]
    for value in candidates:
        if value and Path(value).is_file():return str(Path(value).resolve())
    raise WorkflowError('NODE_RUNTIME_REQUIRED')

def _transport(command, job_dir):
    # Never emit the key; child environment is an allowlist without Node preload/debug options.
    key=os.environ.get('FAL_KEY')
    if (not isinstance(key,str) or not key.strip()) and os.name=='nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as handle:key=winreg.QueryValueEx(handle,'FAL_KEY')[0]
        except OSError:key=None
    if not isinstance(key,str) or not key.strip():return {'state':'KEY_SETUP_REQUIRED','block_reason':'FAL_KEY_MISSING','retry_safe':True}
    env={name:os.environ[name] for name in ('SystemRoot','WINDIR','TEMP','TMP','USERPROFILE','HOME','PATH') if name in os.environ};env['FAL_KEY']=key
    try:r=subprocess.run([_node_exe(),str(SKILL_ROOT/'scripts/fal_sheet_transport.mjs'),command,'--job-dir',str(job_dir)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=110,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:raise WorkflowError('TRANSPORT_INTERRUPTED') from None
    if len(r.stdout)>65536 or len(r.stderr)>65536:raise WorkflowError('TRANSPORT_RESPONSE_INVALID')
    try:value=json.loads(r.stdout)
    except (ValueError,UnicodeError):raise WorkflowError('TRANSPORT_RESPONSE_INVALID') from None
    if not isinstance(value,dict) or not isinstance(value.get('state'),str):raise WorkflowError('TRANSPORT_RESPONSE_INVALID')
    return value

def _managed(m):return 'task_ref' in m.get('request',{})


def _target(task,request,original,target_id=None):
    def matches(target):
        return (target['source_sha256']==original and target['action']==request['action'] and target['orientation']==request['orientation']
                and all(key in request and request[key]==value for key,value in target['requirements'].items()))
    if target_id is None:
        candidates=[value for value in task['targets'].values() if matches(value)]
        if len(candidates)!=1:raise WorkflowError('TASK_TARGET_AMBIGUOUS' if len(candidates)>1 else 'TASK_SOURCE_MISMATCH')
        return candidates[0]
    target=task['targets'].get(target_id)
    if not target:raise WorkflowError('TASK_SOURCE_MISMATCH')
    if any(key not in request for key in target['requirements']):raise WorkflowError('TASK_POLICY_UNSUPPORTED')
    if not matches(target):raise WorkflowError('TASK_SOURCE_MISMATCH')
    return target


def _task_state(path,m,*,recover_bind=False):
    ref=m['request']['task_ref']
    if (not isinstance(ref,dict) or set(ref)!={'schema','task_id','task_manifest','authorization_sha256','target_id'}
            or ref['schema']!='sprite_task_ref_v1' or not isinstance(ref['task_manifest'],str)
            or str(Path(ref['task_manifest']).resolve())!=ref['task_manifest']):raise WorkflowError('TASK_SCOPE_MISMATCH')
    task=task_ledger.inspect_task(ref['task_manifest'])
    if task['task_id']!=ref['task_id'] or task['authorization_sha256']!=ref['authorization_sha256']:raise WorkflowError('TASK_SCOPE_MISMATCH')
    _target(task,m['request'],m['source']['sha256'],ref['target_id'])
    if task.get('history_status')=='pending':task_ledger.check_history_context(ref['task_manifest'],ref['target_id'])
    expected=dict(run_id=m['run_id'],target_id=ref['target_id'],scope_sha256=m['authorization_scope_sha256'],manifest_path=str(path))
    if m.get('derivation'):
        parent=m['derivation'].get('parent_run_id')
        if parent:expected['parent_run_id']=parent
    bound=task['runs'].get(m['run_id'])
    if bound is None and recover_bind:
        task_ledger.check_history_context(ref['task_manifest'],ref['target_id'])
        task_ledger.bind_run(ref['task_manifest'],expected);task=task_ledger.inspect_task(ref['task_manifest']);bound=task['runs'].get(m['run_id'])
    if bound!=expected:raise WorkflowError('TASK_SCOPE_MISMATCH')
    return task


def _task_context(path,m,a,role):
    return dict(task_ref=m['request']['task_ref'],run_manifest=str(path),run_id=m['run_id'],attempt_id=a['attempt_id'],operation_id=a['attempt_id']+':'+role)


def _verify_prepared(m,a):
    prepared=a['prepared'];source=Path(m['source']['path']);dest=source.parent/f'attempt-{a["index"]:02d}'/'prepared-source.png'
    with Image.open(source) as opened:im=opened.convert('RGBA')
    scale=min(1024/im.width,1024/im.height);size=[max(1,round(im.width*scale)),max(1,round(im.height*scale))];offset=[(1024-size[0])//2,(1024-size[1])//2]
    im=im.resize(tuple(size),Image.Resampling.NEAREST if m['request']['pixel_art'] else Image.Resampling.LANCZOS)
    expected=Image.new('RGB',(1024,1024),(110,110,110));expected.paste(im,tuple(offset),im.getchannel('A'))
    payload=io.BytesIO();expected.save(payload,format='PNG')
    record=dict(path=str(dest),sha256=hashlib.sha256(payload.getvalue()).hexdigest(),original_sha256=m['source']['sha256'],transform=dict(scale=scale,size=size,offset=offset,canvas=[1024,1024],background_rgb=[110,110,110]))
    if prepared!=record or dest.read_bytes()!=payload.getvalue():raise WorkflowError('TASK_SOURCE_PROVENANCE_MISMATCH')


def _verify_sampling(m,a):
    q=m['request'];record=a['sampling'];raw=_verified_raw(a['jobs']['primary'])
    with tempfile.TemporaryDirectory(prefix='sprite-sampling-verify-') as temp:
        rebuilt=sample_video(raw,Path(temp),ffmpeg_exe=q['ffmpeg_exe'],frame_count=q['frames'],loop=q['loop'],cycle_start_seconds=q['cycle_start_seconds'],cycle_duration_seconds=q['cycle_duration_seconds'])
        if rebuilt['selection']!=record['selection']:raise WorkflowError('TASK_SOURCE_PROVENANCE_MISMATCH')
        actual=[record['grid_path'],record['selection_path'],*record['frame_paths']]
        expected=[rebuilt['grid_path'],rebuilt['selection_path'],*rebuilt['frame_paths']]
        if len(actual)!=len(expected) or any(Path(left).read_bytes()!=Path(right).read_bytes() for left,right in zip(actual,expected)):
            raise WorkflowError('TASK_SOURCE_PROVENANCE_MISMATCH')


def _review_evidence(path,m):return f'{path}#sha256={sha_file(path)};artifact_set={_review_target(m)}'


def _task_review_state(m,*,task=None,require_pass=False):
    """Live logical projection only; callers still validate their actual artifacts."""
    task=task or task_ledger.inspect_task(m['request']['task_ref']['task_manifest']);ref=m['request']['task_ref']
    if task['task_id']!=ref['task_id'] or task['authorization_sha256']!=ref['authorization_sha256']:raise WorkflowError('TASK_SCOPE_MISMATCH')
    run=task['runs'].get(m['run_id'])
    if not run or run['target_id']!=ref['target_id']:raise WorkflowError('TASK_SCOPE_MISMATCH')
    attempts=[attempt for attempt in task['attempts'].values() if attempt['run_id']==m['run_id'] and attempt['attempt_id']==m['attempts'][-1]['attempt_id']]
    if len(attempts)!=1 or not isinstance(attempts[0].get('effective_defects'),list):raise WorkflowError('TASK_STRATEGY_MISMATCH')
    attempt=attempts[0]
    if require_pass:
        if attempt['effective_defects']:raise WorkflowError('TASK_SOURCE_DEFECT_UNRESOLVED')
        if attempt.get('review',{}).get('decision')!='pass':raise WorkflowError('VISUAL_REVIEW_REQUIRED')
    return attempt


def _canonical_review_receipt(m,review):
    directory=Path(m['source']['path']).parent/f'attempt-{m["attempts"][-1]["index"]:02d}'
    for receipt in sorted(directory.glob('visual-review-*.json')):
        if re.fullmatch(r'visual-review-\d{3}\.json',receipt.name) and _review_evidence(receipt,m)==review['evidence']:
            report=read_json(receipt);defects=_validate_visual_report(m,report)
            if report['decision']!=review['decision'] or defects!=review['defects']:raise WorkflowError('VISUAL_REVIEW_TAMPERED')
            return receipt,report
    raise WorkflowError('VISUAL_REVIEW_TAMPERED')


def _record_task_review(path,m,report):
    defects=_validate_visual_report(m,report);attempt=_task_review_state(m)
    if report['decision']=='pass' and attempt.get('review',{}).get('decision')=='pass':
        canonical,original=_canonical_review_receipt(m,attempt['review'])
        if m['request'].get('alignment_policy'):
            from sprite_alignment_workflow import validate_body_review
            validate_body_review(m,original)
        _task_review_state(m,require_pass=True)
        return canonical  # A validated observation never replaces/re-PASSes its canonical ledger review.
    task_ledger.record_review(m['request']['task_ref']['task_manifest'],m['run_id'],m['attempts'][-1]['attempt_id'],report['decision'],defects,_review_evidence(path,m))
    if report['decision']=='pass':_task_review_state(m,require_pass=True)
    return path


def _reconcile_reviews(path,m):
    if not _managed(m) or not m.get('attempts'):return
    a=m['attempts'][-1];directory=path.parent/f'attempt-{a["index"]:02d}'
    technical=directory/'cycle-rejection.json'
    if technical.exists():
        record=read_json(technical)
        if (set(record)!={'schema','run_id','attempt_id','code','raw_sha256'} or record['schema']!='sprite_technical_cycle_rejection_v1'
                or record['run_id']!=m['run_id'] or record['attempt_id']!=a['attempt_id'] or record['code'] not in CYCLE_REJECTIONS
                or record['raw_sha256']!=sha_file(_verified_raw(a['jobs']['primary']))):raise WorkflowError('TECHNICAL_REVIEW_TAMPERED')
        task_ledger.record_review(m['request']['task_ref']['task_manifest'],m['run_id'],a['attempt_id'],'reject',['technical_cycle:'+record['code']],f'{technical}#sha256={sha_file(technical)}')
        m.update(state='TECHNICAL_REJECTED',block_reason=record['code']);atomic_json(path,m)
    receipts=sorted(directory.glob('visual-review-*.json'))
    if not receipts:return
    _visual_context(m);records=[]
    for number,receipt in enumerate(receipts,1):
        if receipt.name!=f'visual-review-{number:03d}.json':raise WorkflowError('VISUAL_REVIEW_TAMPERED')
        report=read_json(receipt);_validate_visual_report(m,report);records.append((receipt,report))
    rejects=[item for item in records if item[1]['decision']=='reject'];current=_task_review_state(m).get('review')
    if rejects:
        receipt,report=rejects[0]
        if current and current['decision']=='reject':
            receipt,report=_canonical_review_receipt(m,current)
            if any(_review_evidence(candidate,m)!=current['evidence'] for candidate,_ in rejects):raise WorkflowError('TASK_REVIEW_CONFLICT')
    elif current:receipt,report=_canonical_review_receipt(m,current)
    else:receipt,report=records[0]
    if report['decision']=='pass' and m['request'].get('alignment_policy'):
        from sprite_alignment_workflow import validate_body_review
        validate_body_review(m,report)
    receipt=_record_task_review(receipt,m,report)
    m.update(visual_review={'path':str(receipt),'sha256':sha_file(receipt)},state=VISUAL_REJECTED if rejects else DELIVERABLE)
    atomic_json(path,m)


def _reservation_value(operation):return float(operation['amount_usd']) if operation['status']!='released' else 0


def _reservation_lag(job,operation,record,*,allowed=False):
    """Only ledger-recorded amounts (or initial zero) can be stale mirrors."""
    expected=_reservation_value(operation);manifest_value=job['reserved_usd']
    if (isinstance(record,dict) and type(record.get('reserved_usd')) in (int,float) and record=={'reserved_usd':expected}
            and type(manifest_value) in (int,float) and manifest_value==expected):return False
    values={0,float(operation['amount_usd']),float(operation['reservation']['amount_usd'])}
    values.update(float(quote['amount_usd']) for quote in operation['quote_history'])
    def historical(value):return type(value) in (int,float) and math.isfinite(value) and value in values
    if (not allowed or not historical(manifest_value) or (record is not None and (
            not isinstance(record,dict) or set(record)!={'reserved_usd'} or not historical(record['reserved_usd'])))):
        raise WorkflowError('RESERVATION_TAMPERED')
    return True


def _repair_reservation_mirrors(path,m):
    """Called only after full managed load while holding the run lifecycle lock."""
    if not _managed(m):return
    task=_task_state(path,m);changed=False
    for attempt in m['attempts']:
        for job in attempt['jobs'].values():
            operation=task['operations'].get(job['operation_id'])
            if not operation or not operation.get('execution_binding'):continue
            target=Path(job['path'])/'reservation.json';record=read_json(target) if target.exists() else None
            if _reservation_lag(job,operation,record,allowed=True):
                job['reserved_usd']=_reservation_value(operation)
                atomic_json(target,{'reserved_usd':job['reserved_usd']});changed=True
    if changed:atomic_json(path,m)


def _load(path,*,recover_bind=False,allow_reservation_lag=False):
    path=Path(path).expanduser().resolve();root=path.parent
    installed=Path.home()/'.codex/skills'
    if root==SKILL_ROOT or SKILL_ROOT in root.parents or root==installed or installed in root.parents:raise WorkflowError('WORKSPACE_NOT_ALLOWED')
    m=read_json(path);scope=read_json(root/'scope.json')
    if (root/'scope.sha256').read_text().strip()!=scope_sha(scope) or m.get('authorization_scope_sha256')!=scope_sha(scope) or m.get('request')!=scope.get('request') or m.get('authorization')!=scope.get('authorization') or m.get('source')!=scope.get('source') or m.get('run_id')!=scope.get('run_id'):raise WorkflowError('SCOPE_TAMPERED')
    if m.get('schema_version')!=3 or not isinstance(m.get('attempts'),list):raise WorkflowError('STATE_INVALID')
    if m.get('derivation')!=scope.get('derivation'):raise WorkflowError('SCOPE_TAMPERED')
    if m.get('derivation') and m['request'].get('derivation_sha256')!=scope_sha(m['derivation']):raise WorkflowError('SCOPE_TAMPERED')
    if 'alignment_policy' in m['request'] and m['request']['alignment_policy']!='body_x_preserve_y_v1':raise WorkflowError('ALIGNMENT_POLICY_UNSUPPORTED')
    if (root/'regional-plan.json').exists() and not m.get('derivation') and m['request'].get('route')!='regional_video_inpaint_v1':
        raise WorkflowError('REGIONAL_CONTEXT_REQUIRED')
    if m['request'].get('route')=='regional_video_inpaint_v1' and not m.get('derivation'):
        from regional_video_workflow import validate_regional_attempt
        verified,manifest,_=validate_regional_attempt(path,require_artifacts=m.get('state') in ASSEMBLED)
        return verified,manifest
    if m['request'].get('route')=='endpoint_cycle_v1' and not m.get('derivation'):
        from endpoint_cycle_contract import validate_endpoint_attempt
        verified,manifest,_=validate_endpoint_attempt(path,require_artifacts=m.get('state') in ASSEMBLED)
        return verified,manifest
    source=Path(m['source']['path'])
    if source!=root/'source.png' or sha_file(source)!=m['source']['sha256']:raise WorkflowError('SOURCE_TAMPERED')
    managed=_managed(scope)
    if managed:task=_task_state(path,m,recover_bind=recover_bind)
    dirs=sorted(root.glob('attempt-*'))
    if len(dirs)!=len(m['attempts']) or len(dirs)>m['authorization']['max_generation_attempts']:raise WorkflowError('ATTEMPT_LEDGER_TAMPERED')
    for i,a in enumerate(m['attempts'],1):
        if a['index']!=i:raise WorkflowError('ATTEMPT_LEDGER_TAMPERED')
        if managed:
            cleanup=task['operations'].get(a['attempt_id']+':cleanup')
            if cleanup and 'deferred_config' in cleanup:
                plan=root/f'attempt-{i:02d}'/'cleanup-plan'/'plan.json'
                if plan.is_symlink() or plan.read_bytes()!=encoded(cleanup['deferred_config']):raise WorkflowError('TASK_PLAN_INVALID')
        for role,j in a['jobs'].items():
            if role not in {'primary','cleanup'} or Path(j['path'])!=root/f'attempt-{i:02d}'/role:raise WorkflowError('JOB_CONFIG_TAMPERED')
            config=read_json(Path(j['path'])/'config.json')
            config_hash=scope_sha(config)
            authorization={'request_authorized':True,'scope_sha256':m['authorization_scope_sha256']}
            if managed:authorization['task_context']=_task_context(path,m,a,role)
            if config_hash!=j['config_sha256'] or (Path(j['path'])/'config.sha256').read_text().strip()!=config_hash or config.get('max_cost_usd',float('inf'))>m['authorization']['max_cost_usd'] or config.get('authorization')!=authorization:raise WorkflowError('JOB_CONFIG_TAMPERED')
            if managed and (config['max_cost_usd']!=m['authorization']['max_cost_usd'] or j.get('operation_id')!=a['attempt_id']+':'+role
                            or j.get('config_file_sha256')!=sha_file(Path(j['path'])/'config.json')):raise WorkflowError('JOB_CONFIG_TAMPERED')
            reservation=Path(j['path'])/'reservation.json'
            if not managed and reservation.exists() and read_json(reservation)!= {'reserved_usd':j['reserved_usd']}:raise WorkflowError('RESERVATION_TAMPERED')
            if j['state']=='DOWNLOADED':_verified_raw(j)
            if managed:
                op=task['operations'].get(j['operation_id'])
                if op and op.get('execution_binding'):
                    from sprite_task_bridge import check_context
                    binding=op['execution_binding']
                    check_context(dict(schema='sprite_transport_context_v1',job_dir=j['path'],config_sha256=binding['config_sha256'],config_file_sha256=j['config_file_sha256'],source_sha256=config['source']['sha256']))
                    _reservation_lag(j,op,read_json(reservation) if reservation.exists() else None,allowed=allow_reservation_lag)
                elif reservation.exists() and read_json(reservation)!={'reserved_usd':j['reserved_usd']}:raise WorkflowError('RESERVATION_TAMPERED')
        if a.get('prepared') and sha_file(a['prepared']['path'])!=a['prepared']['sha256']:raise WorkflowError('SOURCE_TAMPERED')
        if managed and a.get('prepared'):_verify_prepared(m,a)
        if managed and a.get('sampling'):_verify_sampling(m,a)
    if _result(manifest=m)['spent_estimate_usd']>m['authorization']['max_cost_usd']+1e-10:raise WorkflowError('RESERVATION_TAMPERED')
    if m.get('derivation'):
        if m['state'] not in ASSEMBLED:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        from sprite_alignment_workflow import verify_alignment
        verify_alignment(m)
        parent=read_json(root/'derivation/parent-manifest.json')
        if parent.get('request',{}).get('route')=='regional_video_inpaint_v1':
            if parent.get('derivation'):
                _,live=_load(m['derivation']['parent_manifest'])
            else:
                from regional_video_workflow import validate_regional_attempt
                _,live,_=validate_regional_attempt(m['derivation']['parent_manifest'],require_artifacts=True)
            _verify_visual_state(live)
            if (any(live.get(k)!=parent.get(k) for k in ('run_id','source','request','authorization_scope_sha256','visual_review','state'))
                    or _review_target(live)!=_review_target(parent)):
                raise WorkflowError('ALIGNMENT_PROOF_INVALID')
    return path,m

def _verify_artifacts(m):
    if not m.get('attempts'):raise WorkflowError('ARTIFACT_TAMPERED')
    active=m['attempts'][-1];directory=Path(m['source']['path']).parent/f'attempt-{active["index"]:02d}'/'artifacts'
    expected={'sheet':str(directory/'sprite-sheet.png'),'preview_gif':str(directory/'preview.gif'),'contact':str(directory/'contact.png'),'qa_report':str(directory/'qa.json'),'frames':[str(directory/f'frame-{i+1:02d}.png') for i in range(m['request']['frames'])]}
    if m.get('artifact_paths')!=expected:raise WorkflowError('ARTIFACT_TAMPERED')
    required=[]
    for value in expected.values():required.extend(value if isinstance(value,list) else [value])
    if active.get('sampling'):required.extend([active['sampling']['selection_path'],active['sampling']['grid_path'],*active['sampling']['frame_paths']])
    if set(m.get('artifact_hashes',{}))!=set(required):raise WorkflowError('ARTIFACT_TAMPERED')
    for name,digest in m.get('artifact_hashes',{}).items():
        try:actual=sha_file(name)
        except (OSError,WorkflowError):raise WorkflowError('ARTIFACT_TAMPERED') from None
        if actual!=digest:raise WorkflowError('ARTIFACT_TAMPERED')
    if not m.get('artifact_hashes'):raise WorkflowError('ARTIFACT_TAMPERED')

def inspect(manifest_path):
    try:
        path,m=_load(manifest_path)
        if m['state'] in ASSEMBLED:_verify_visual_state(m)
        return _result(m['state'],path,m,m.get('block_reason'),m.get('retry_safe',False))
    except (OSError,ValueError,KeyError,TypeError) as e:return _result(path=manifest_path,reason=getattr(e,'code','STATE_INVALID'))


def _visual_context(m):
    _verify_artifacts(m)
    frames=[]
    for path in m['artifact_paths']['frames']:
        with Image.open(path) as image:frames.append(image.convert('RGBA'))
    m['consistency']=consistency_diagnostics(frames)


def _validate_visual_report(m, report):
    if not isinstance(report,dict):raise WorkflowError('VISUAL_REVIEW_INVALID')
    if report.get('artifact_set_sha256')!=_review_target(m):raise WorkflowError('VISUAL_REVIEW_STALE')
    decision=report.get('decision');checks=report.get('checks')
    if decision not in {'pass','reject'} or not isinstance(checks,dict) or set(checks)!=VISUAL_CHECKS:
        raise WorkflowError('VISUAL_REVIEW_INVALID')
    def text_present(value):return isinstance(value,str) and 10<=len(value.strip())<=4000
    for check in checks.values():
        if not isinstance(check,dict) or check.get('status') not in {'pass','fail'} or not text_present(check.get('notes')):
            raise WorkflowError('VISUAL_REVIEW_INVALID')
    failures=any(check['status']=='fail' for check in checks.values())
    if (decision=='pass' and failures) or (decision=='reject' and not failures):raise WorkflowError('VISUAL_REVIEW_INVALID')
    if decision=='pass':
        anchors=report.get('scale_anchors')
        if not isinstance(anchors,list) or len(anchors)<2 or any(not isinstance(a,dict) or not isinstance(a.get('name'),str) or not a['name'].strip() or not text_present(a.get('notes')) for a in anchors):
            raise WorkflowError('VISUAL_REVIEW_INVALID')
        if len({a['name'].strip().lower() for a in anchors})<2:raise WorkflowError('VISUAL_REVIEW_INVALID')
    findings=report.get('flagged_frame_findings',[])
    if not isinstance(findings,list):raise WorkflowError('VISUAL_REVIEW_INVALID')
    seen=set()
    for item in findings:
        if not isinstance(item,dict) or type(item.get('frame')) is not int or not 1<=item['frame']<=m['request']['frames'] or not text_present(item.get('notes')) or item['frame'] in seen:
            raise WorkflowError('VISUAL_REVIEW_INVALID')
        seen.add(item['frame'])
    if decision=='pass' and not set(m['consistency']['flagged_frames']).issubset(seen):
        raise WorkflowError('FLAGGED_FRAMES_UNREVIEWED')
    repairable=report.get('repairable_defects',[]);mapping={};kinds=set()
    rules={
        'framing':('position_x',{'all_frames_inspected','only_horizontal_body_translation','no_clipping_or_missing_pixels','no_y_scale_pose_anatomy_change'}),
        'background':('alpha_edge',{'all_frames_inspected','only_exterior_alpha_edge_residue','no_missing_subject_pixels','no_rgb_anatomy_pose_change'})}
    if not isinstance(repairable,list) or len(repairable)>2:raise WorkflowError('VISUAL_REVIEW_INVALID')
    for entry in repairable:
        if not isinstance(entry,dict) or set(entry)!={'check','kind','frame_findings','attestations'}:raise WorkflowError('VISUAL_REVIEW_INVALID')
        check=entry['check'];kind=entry['kind']
        if (not isinstance(check,str) or not isinstance(kind,str) or check not in rules or kind!=rules[check][0]
                or check in mapping or kind in kinds or decision!='reject' or checks[check]['status']!='fail'):raise WorkflowError('VISUAL_REVIEW_INVALID')
        attestations=entry['attestations']
        if not isinstance(attestations,dict) or set(attestations)!=rules[check][1] or any(value is not True for value in attestations.values()):raise WorkflowError('VISUAL_REVIEW_INVALID')
        findings=entry['frame_findings'];frames=set()
        if not isinstance(findings,list) or not findings:raise WorkflowError('VISUAL_REVIEW_INVALID')
        for finding in findings:
            if (not isinstance(finding,dict) or set(finding)!={'frame','notes'} or type(finding['frame']) is not int
                    or not 1<=finding['frame']<=m['request']['frames'] or finding['frame'] in frames or not text_present(finding['notes'])):raise WorkflowError('VISUAL_REVIEW_INVALID')
            frames.add(finding['frame'])
        mapping[check]=kind;kinds.add(kind)
    # JSON object order is not semantic. Canonical check-name order is shared
    # before archival/ledger submission and when replaying an encoded receipt.
    return [mapping.get(name,name)+':'+checks[name]['notes'].strip() for name in sorted(checks) if checks[name]['status']=='fail']


def _verify_visual_state(m):
    _visual_context(m)
    # The semantic report validator is shared with native workflows and stays pure.
    if m.get('derivation'):
        from sprite_alignment_workflow import verify_alignment
        verify_alignment(m)
    try:
        directory=Path(m['source']['path']).parent/f'attempt-{len(m["attempts"]):02d}'
        if m['state']!=READY:
            receipt=m['visual_review'];path=Path(receipt['path'])
            if path.parent!=directory or not re.fullmatch(r'visual-review-\d{3}\.json',path.name) or sha_file(path)!=receipt['sha256']:
                raise WorkflowError('VISUAL_REVIEW_TAMPERED')
            report=read_json(path);_validate_visual_report(m,report)
            if report['decision']=='pass' and m.get('request',{}).get('alignment_policy'):
                from sprite_alignment_workflow import validate_body_review
                validate_body_review(m,report)
            if (report['decision']=='pass')!=(m['state']==DELIVERABLE):raise WorkflowError('VISUAL_REVIEW_TAMPERED')
        # A receipt can survive an interrupted manifest save. Rejects are
        # authoritative; orphan passes never grant delivery implicitly.
        for number,path in enumerate(sorted(directory.glob('visual-review-*.json')),1):
            if path.name!=f'visual-review-{number:03d}.json':raise WorkflowError('VISUAL_REVIEW_TAMPERED')
            report=read_json(path);_validate_visual_report(m,report)
            if report['decision']=='reject':
                m['visual_review']={'path':str(path),'sha256':sha_file(path)}
                m['state']=VISUAL_REJECTED
        if _managed(m) and m['state']==DELIVERABLE:_task_review_state(m,require_pass=True)
    except WorkflowError as error:
        if error.code in {'ALIGNMENT_REQUIRED','ALIGNMENT_PROOF_INVALID','ALIGNMENT_POLICY_UNSUPPORTED','BODY_ALIGNMENT_REVIEW_INVALID','TASK_SOURCE_DEFECT_UNRESOLVED','VISUAL_REVIEW_REQUIRED','TASK_SCOPE_MISMATCH','TASK_STRATEGY_MISMATCH'}:raise
        raise WorkflowError('VISUAL_REVIEW_TAMPERED') from None
    except (OSError,KeyError,ValueError,TypeError):raise WorkflowError('VISUAL_REVIEW_TAMPERED') from None


def record_visual_review(manifest_path, review_file):
    path=Path(manifest_path).expanduser().resolve();m=None
    try:
        path,m=_load(path)
        with run_lock(path.parent):
            path,m=_load(path)
            _reconcile_reviews(path,m)
            if m['state'] not in ASSEMBLED:raise WorkflowError('VISUAL_REVIEW_REQUIRES_ARTIFACTS')
            _verify_visual_state(m)
            report=read_json(Path(review_file));_validate_visual_report(m,report)
            if _managed(m) and m.get('visual_review') and read_json(m['visual_review']['path'])==report:
                return _result(m['state'],path,m)
            if m['state']==VISUAL_REJECTED and report['decision']=='pass':
                raise WorkflowError('REJECTED_ARTIFACTS_REQUIRE_REVISION')
            if _managed(m) and _task_review_state(m).get('review',{}).get('decision')=='reject':
                raise WorkflowError('TASK_REVIEW_CONFLICT')
            if report['decision']=='pass' and m.get('request',{}).get('alignment_policy'):
                from sprite_alignment_workflow import validate_body_review
                validate_body_review(m,report)
            directory=path.parent/f'attempt-{len(m["attempts"]):02d}'
            number=len(list(directory.glob('visual-review-*.json')))+1
            receipt=directory/f'visual-review-{number:03d}.json'
            new_or_identical(receipt,encoded(report))
            if _managed(m):receipt=_record_task_review(receipt,m,report)
            m['visual_review']={'path':str(receipt),'sha256':sha_file(receipt)}
            return _save(path,m,DELIVERABLE if report['decision']=='pass' else VISUAL_REJECTED)
    except (OSError,ValueError,KeyError,TypeError) as e:
        return _result(path=path,manifest=m,reason=getattr(e,'code','VISUAL_REVIEW_INVALID'))


def export_artifacts(manifest_path, output_dir):
    path=Path(manifest_path).expanduser().resolve();m=None
    try:
        path,m=_load(path)
        with run_lock(path.parent):
            path,m=_load(path)
            _reconcile_reviews(path,m)
            if m['state'] not in ASSEMBLED:raise WorkflowError('VISUAL_REVIEW_REQUIRED')
            _verify_visual_state(m)
            if m['state']==READY and m.get('request',{}).get('alignment_policy'):
                from sprite_alignment_workflow import verify_alignment
                verify_alignment(m)
            if m['state']!=DELIVERABLE:raise WorkflowError(VISUAL_REJECTED if m['state']==VISUAL_REJECTED else 'VISUAL_REVIEW_REQUIRED')
            destination=Path(output_dir).expanduser().resolve()
            installed=Path.home()/'.codex/skills'
            if any(destination==p or p in destination.parents for p in (installed,SKILL_ROOT,path.parent)):
                raise WorkflowError('WORKSPACE_NOT_ALLOWED')
            files=[Path(p) for value in m['artifact_paths'].values() for p in (value if isinstance(value,list) else [value])]
            payloads={destination/p.name:p.read_bytes() for p in files}
            payloads[destination/'visual-review.json']=Path(m['visual_review']['path']).read_bytes()
            if m.get('derivation'):
                from sprite_alignment_workflow import export_provenance
                payloads.update(export_provenance(m,destination))
            for target,payload in payloads.items():
                if target.exists() and (not target.is_file() or target.read_bytes()!=payload):raise WorkflowError('OUTPUT_CONFLICT')
            if _managed(m):_task_review_state(m,require_pass=True)
            destination.mkdir(parents=True,exist_ok=True)
            for target,payload in payloads.items():
                target.parent.mkdir(parents=True,exist_ok=True);new_or_identical(target,payload)
            if any(target.read_bytes()!=payload for target,payload in payloads.items()):raise WorkflowError('OUTPUT_CONFLICT')
            exported={key:[str(destination/Path(p).name) for p in value] if isinstance(value,list) else str(destination/Path(value).name) for key,value in m['artifact_paths'].items()}
            return _result(DELIVERABLE,path,m,delivery_artifact_paths=exported,export_directory=str(destination))
    except (OSError,ValueError,KeyError,TypeError) as e:
        return _result(path=path,manifest=m,reason=getattr(e,'code','EXPORT_FAILED'))

def _prompt(q,feedback):
    details=json.dumps({'action':q['action'],'orientation':q['orientation'],'description':q['description'],'correction':feedback},ensure_ascii=False)
    style='pixel art' if q['pixel_art'] else 'original reference rendering'
    shared=f'Preserve the exact source character identity, silhouette, palette, clothing and {style} style. Full body always visible with safe margins. Fixed camera and constant character scale. No automatic mirroring. Requested action and orientation as JSON data: {details}. Nose, torso and toes point toward the requested page edge. No environment, grid lines, text, outline glow or added objects. '
    gait='For walk/run, show opposite near/far leg contacts: near leg forward and far leg back, passing pose, far leg forward and near leg back, opposite passing pose. Knees and feet must alternate with a complete weight transfer. ' if q['action'] in {'walk','run'} else ''
    if q['route']=='video':
        ending='Smooth complete repeating motion cycle. Return to the initial pose at the end.' if q['loop'] else 'Perform the complete requested action once with a natural start and finish.'
        return shared+gait+'Animate naturally in place on a perfectly flat neutral gray background. No camera movement or sliding. '+ending
    phases=[{'frame':i+1,'cycle_phase':round(i/q['frames'],4)} for i in range(q['frames'])]
    return shared+gait+f'Create exactly {q["frames"]} distinct sequential full-body poses in a uniform {q["columns"]}-column by {q["rows"]}-row grid, read left to right then top to bottom. Keep each pose strictly inside its cell. Leave {q["columns"]*q["rows"]-q["frames"]} trailing cells empty. Transparent background, no visible dividers. Phase schedule: '+json.dumps(phases)

def _save(path,m,state=None,reason=None,retry=False):
    if state is not None:m.update(state=state,block_reason=reason,retry_safe=retry)
    atomic_json(path,m)
    return _result(m['state'],path,m,m.get('block_reason'),m.get('retry_safe',False))


def _quote(directory,response,*,plan=None):
    """Reopen exact Node receipt and archived API evidence before reserving."""
    quote=response.get('quote');planning=plan is not None
    fields={'receipt_path','receipt_sha256','estimated_cost_usd_decimal'}|({'plan_sha256'} if planning else {'config_sha256','config_file_sha256'})
    if not isinstance(quote,dict) or set(quote)!=fields:raise WorkflowError('PRICE_RECEIPT_INVALID')
    receipt_dir=directory/('planning-pricing-receipts' if planning else 'pricing-receipts')
    receipt_path=Path(quote['receipt_path'])
    if not receipt_path.is_absolute() or receipt_path.parent!=receipt_dir or receipt_dir.is_symlink() or sha_file(receipt_path)!=quote['receipt_sha256']:
        raise WorkflowError('PRICE_RECEIPT_INVALID')
    def strict_json(payload):
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise WorkflowError('PRICE_RECEIPT_INVALID')
                result[key]=value
            return result
        def invalid(_):raise WorkflowError('PRICE_RECEIPT_INVALID')
        return json.loads(payload,parse_float=Decimal,object_pairs_hook=pairs,parse_constant=invalid)
    def amount(value):
        if not isinstance(value,(str,int,Decimal)) or isinstance(value,bool):raise WorkflowError('PRICE_RECEIPT_INVALID')
        try:return Decimal(task_ledger._money(value,True))
        except ValueError:raise WorkflowError('PRICE_RECEIPT_INVALID') from None
    receipt=strict_json(receipt_path.read_bytes())
    if not isinstance(quote['estimated_cost_usd_decimal'],str):raise WorkflowError('PRICE_RECEIPT_INVALID')
    estimate=amount(quote['estimated_cost_usd_decimal'])
    common={'receipt_id','model','retrieved_at','expires_at','currency','api_unit','estimated_cost_usd','reserved_units','planning_basis','sources'}
    expected=common|({'schema','plan_sha256','unit_price_usd'} if planning else {'schema_version','config_sha256','base_unit_price_usd','resolution_multiplier','effective_unit_price_usd','input_duration_seconds','driving_video_sha256','estimated_cost_usd_decimal'})
    if set(receipt)!=expected:raise WorkflowError('PRICE_RECEIPT_INVALID')
    if planning:
        model=CLEANUP
        if receipt['schema']!='sprite_cleanup_planning_quote_v1' or quote['plan_sha256']!=sha_file(directory/'plan.json') or receipt['plan_sha256']!=quote['plan_sha256'] or read_json(directory/'plan.json')!=plan:
            raise WorkflowError('PRICE_RECEIPT_INVALID')
    else:
        config=read_json(directory/'config.json');model=config['model']
        if (receipt['schema_version']!=3 or receipt['config_sha256']!=quote['config_sha256'] or not re.fullmatch('[0-9a-f]{64}',str(quote['config_sha256']))
                or quote['config_file_sha256']!=sha_file(directory/'config.json') or amount(receipt['estimated_cost_usd_decimal'])!=estimate):raise WorkflowError('PRICE_RECEIPT_INVALID')
    units=6 if model==H3 else 1;basis='configured_duration+1_reserved_output_second' if model==H3 else 'configured_output_count'
    if (receipt['model']!=model or receipt['currency']!='USD' or receipt['api_unit'] not in ({'seconds'} if model==H3 else {'image','images'})
            or type(receipt['reserved_units']) is not int or receipt['reserved_units']!=units or receipt['planning_basis']!=basis
            or amount(receipt['estimated_cost_usd'])!=estimate):raise WorkflowError('PRICE_RECEIPT_INVALID')
    retrieved=datetime.fromisoformat(receipt['retrieved_at'].replace('Z','+00:00'));expires=datetime.fromisoformat(receipt['expires_at'].replace('Z','+00:00'))
    if retrieved.utcoffset()!=timedelta(0) or expires-retrieved!=timedelta(minutes=15):raise WorkflowError('PRICE_RECEIPT_INVALID')
    if not -timedelta(seconds=30)<=datetime.now(timezone.utc)-retrieved<=timedelta(minutes=15):raise WorkflowError('PRICE_STALE')
    rid=receipt['receipt_id']
    if str(uuid.UUID(rid,version=4))!=rid or receipt_path.name!=f'{int(retrieved.timestamp()*1000):016d}-{rid}.receipt.json':raise WorkflowError('PRICE_RECEIPT_INVALID')
    sources=receipt['sources']
    if not isinstance(sources,list) or len(sources)!=(2 if model in {NANO,H3} else 1):raise WorkflowError('PRICE_RECEIPT_INVALID')
    base=None;public=None
    for i,source in enumerate(sources):
        if set(source)!={'source_url','retrieved_at','body_sha256','archive_file','selected'} or source['retrieved_at']!=receipt['retrieved_at']:
            raise WorkflowError('PRICE_RECEIPT_INVALID')
        archive=rid+('.api.body' if i==0 else '.public.body')
        if source['archive_file']!=archive or sha_file(receipt_dir/archive)!=source['body_sha256']:raise WorkflowError('PRICE_RECEIPT_INVALID')
        if i==0:
            from urllib.parse import urlencode
            if source['source_url']!='https://api.fal.ai/v1/models/pricing?'+urlencode({'endpoint_id':model}):raise WorkflowError('PRICE_RECEIPT_INVALID')
            body=strict_json((receipt_dir/archive).read_bytes());prices=body.get('prices')
            if not isinstance(prices,list):raise WorkflowError('PRICE_RECEIPT_INVALID')
            matches=[item for item in prices if isinstance(item,dict) and item.get('endpoint_id')==model]
            selected=source['selected']
            if len(matches)!=1 or set(selected)!={'endpoint_id','currency','unit','unit_price_usd'}:raise WorkflowError('PRICE_RECEIPT_INVALID')
            api=matches[0];base=amount(api.get('unit_price'))
            if (api.get('currency')!='USD' or api.get('unit')!=receipt['api_unit'] or selected['endpoint_id']!=model or selected['currency']!='USD'
                    or selected['unit']!=receipt['api_unit'] or amount(selected['unit_price_usd'])!=base):raise WorkflowError('PRICE_RECEIPT_INVALID')
            multiplier=api.get('resolution_multipliers',{}).get('2K')
            if model==NANO and multiplier is not None and amount(multiplier)!=Decimal('1.5'):raise WorkflowError('PRICE_RECEIPT_INVALID')
        else:
            if source['source_url']!='https://fal.ai/models/'+model:raise WorkflowError('PRICE_RECEIPT_INVALID')
            # The production Node parser remains authoritative for visible HTML;
            # reopen actual UTF8 bytes and check the selected exact evidence too.
            public=(receipt_dir/archive).read_bytes().decode('utf-8');selected=source['selected']
            if amount(selected['base_unit_price_usd'])!=base or selected['resolution']!=('2K' if model==NANO else '768P'):raise WorkflowError('PRICE_RECEIPT_INVALID')
            if model==NANO:
                if set(selected)!={'base_unit_price_usd','resolution','resolution_multiplier'} or amount(selected['resolution_multiplier'])!=Decimal('1.5'):raise WorkflowError('PRICE_RECEIPT_INVALID')
            elif set(selected)!={'base_unit_price_usd','resolution','effective_unit_price_usd'}:raise WorkflowError('PRICE_RECEIPT_INVALID')
            if not public:raise WorkflowError('PRICE_RECEIPT_INVALID')
    with localcontext() as context:
        context.prec=512
        if planning:
            if amount(receipt['unit_price_usd'])!=base or estimate!=base:raise WorkflowError('PRICE_RECEIPT_INVALID')
        else:
            effective=amount(receipt['effective_unit_price_usd'])
            if amount(receipt['base_unit_price_usd'])!=base or estimate!=effective*units:raise WorkflowError('PRICE_RECEIPT_INVALID')
            if model==NANO and (effective!=base*Decimal('1.5') or receipt['resolution_multiplier']!=Decimal('1.5')):raise WorkflowError('PRICE_RECEIPT_INVALID')
            if model==CLEANUP and effective!=base:raise WorkflowError('PRICE_RECEIPT_INVALID')
            if model==H3 and effective!=amount(sources[1]['selected']['effective_unit_price_usd']):raise WorkflowError('PRICE_RECEIPT_INVALID')
            if receipt['input_duration_seconds']!=(5 if model==H3 else None) or receipt['driving_video_sha256'] is not None:raise WorkflowError('PRICE_RECEIPT_INVALID')
    return quote


def _generation_descriptor(request,source_sha256):
    return dict(route=request['route'],model=H3 if request['route']=='video' else NANO,
        conditioning='single_source',inputs=[dict(role='source',sha256=source_sha256)])


def _new_attempt(path,m,feedback,correction=None):
    index=len(m['attempts'])+1
    if index>m['authorization']['max_generation_attempts']:raise WorkflowError('ATTEMPT_LIMIT_REACHED')
    identity={}
    if _managed(m):
        task=_task_state(path,m);allowed=task['authorization']['allowed_operations']
        if not {'provider_animation','alpha_edge_cleanup'}.issubset(allowed):raise WorkflowError('OPERATION_NOT_AUTHORIZED')
        attempt_id=m['run_id']+f':attempt-{index:02d}';q=m['request']
        descriptor=_generation_descriptor(q,m['source']['sha256'])
        strategy=task_ledger.begin_attempt(m['request']['task_ref']['task_manifest'],m['run_id'],attempt_id,descriptor,m['authorization']['max_generation_attempts'],correction)
        identity=dict(attempt_id=attempt_id,strategy_sha256=strategy,correction=correction)
    directory=path.parent/f'attempt-{index:02d}';directory.mkdir()
    m.pop('visual_review',None);m.pop('consistency',None)
    a={'index':index,'feedback':feedback,'prompt':_prompt(m['request'],feedback),'jobs':{},'diagnoses':[],**identity};m['attempts'].append(a);_save(path,m,'GENERATING');return a

def _job(path,m,a,role,source):
    q=m['request'];directory=path.parent/f'attempt-{a["index"]:02d}'/role
    if role in a['jobs']:return a['jobs'][role]
    model=CLEANUP if role=='cleanup' else H3 if q['route']=='video' else NANO
    if model==CLEANUP:inputs={'sync_mode':False}
    elif model==H3:inputs={'prompt':a['prompt'],'duration':5,'resolution':'768P','prompt_expansion_mode':'balanced','enable_safety_checker':True,'sync_mode':False,'end_image_same_as_start':q['loop']}
    else:inputs={'prompt':a['prompt'],'resolution':'2K','aspect_ratio':'16:9','num_images':1,'output_format':'png','limit_generations':True,'enable_web_search':False}
    remaining=round(m['authorization']['max_cost_usd']-_result(manifest=m)['spent_estimate_usd'],12)
    auth={'request_authorized':True,'scope_sha256':m['authorization_scope_sha256']}
    if _managed(m):auth['task_context']=_task_context(path,m,a,role);remaining=m['authorization']['max_cost_usd']
    config={'schema_version':3,'model':model,'source':{'path':str(source),'sha256':sha_file(source)},'input':inputs,'authorization':auth,'max_cost_usd':remaining}
    directory.mkdir(exist_ok=True);new_or_identical(directory/'config.json',encoded(config));new_or_identical(directory/'config.sha256',(scope_sha(config)+'\n').encode());job={'path':str(directory),'config_sha256':scope_sha(config),'reserved_usd':0,'state':'NEW','request_id':None}
    if _managed(m):
        job.update(operation_id=a['attempt_id']+':'+role,config_file_sha256=sha_file(directory/'config.json'))
        evidence={};kind='original_source'
        if role=='primary' and q['route']=='video':
            _verify_prepared(m,a);kind='prepared_source_v1';evidence={'transform':a['prepared']['transform']}
        if role=='cleanup':
            parent=a['jobs']['primary'];_verified_raw(parent)
            kind='video_sample_grid_v1' if q['route']=='video' else 'primary_raw_png'
            evidence=dict(parent_operation_id=parent['operation_id'],parent_request_id=parent['request_id'],parent_raw_sha256=parent['raw_sha256'])
            if q['route']=='video':
                _verify_sampling(m,a);evidence.update(selection_sha256=sha_file(a['sampling']['selection_path']),grid_sha256=sha_file(source))
            with Image.open(source) as image:
                image.load()
                if image.format!='PNG' or image.width>4096 or image.height>4096 or source.stat().st_size>10485760:raise WorkflowError('TASK_SOURCE_PROVENANCE_MISMATCH')
        new_or_identical(directory/'source-provenance.json',encoded(dict(schema='sprite_source_provenance_v1',kind=kind,original_source_sha256=m['source']['sha256'],input_sha256=sha_file(source),evidence=evidence)))
    a['jobs'][role]=job;_save(path,m);return job

def _verified_raw(job):
    try:
        raw=Path(job['raw_path'])
        if raw.parent!=Path(job['path']) or sha_file(raw)!=job['raw_sha256']:
            raise WorkflowError('RAW_TAMPERED')
    except (OSError,KeyError,TypeError,ValueError):
        raise WorkflowError('RAW_TAMPERED') from None
    return raw


def _plan_operations(task,a):
    ids=[a['attempt_id']+':primary',a['attempt_id']+':cleanup'];result=[]
    for name in ids:
        op=task['operations'][name]
        item={key:op['reservation'][key] for key in ('operation_id','kind','config_sha256','deferred_config') if key in op['reservation']}
        item.update(op['current_quote']);result.append(item)
    return result


def _local_cap(task,m,operations):
    with localcontext() as context:
        context.prec=512
        total=sum((Decimal(op['amount_usd']) for op in task['operations'].values() if op['run_id']==m['run_id'] and op['status']!='released'),Decimal(0))
        for proposed in operations:
            existing=task['operations'].get(proposed['operation_id']);previous=Decimal(existing['amount_usd']) if existing else Decimal(0)
            if existing and existing['status']=='released':raise WorkflowError('TASK_PLAN_INVALID')
            total+=max(Decimal(proposed['amount_usd'])-previous,Decimal(0))
        if total>Decimal(str(m['authorization']['max_cost_usd'])):raise WorkflowError('COST_CAP_EXCEEDED')


def _mirror_reservations(path,m,a,task):
    for job in a['jobs'].values():
        op=task['operations'].get(job['operation_id'])
        if op:
            job['reserved_usd']=_reservation_value(op)
            atomic_json(Path(job['path'])/'reservation.json',{'reserved_usd':job['reserved_usd']})
    _save(path,m)


def _materialize(path,m,a,job,quote,plan=None):
    directory=Path(job['path']);config=read_json(directory/'config.json')
    job.update(transport_config_sha256=quote['config_sha256'],config_file_sha256=quote['config_file_sha256'])
    _save(path,m)
    task_ledger.bind_operation_config(m['request']['task_ref']['task_manifest'],job['operation_id'],dict(schema='sprite_execution_binding_v1',config_sha256=quote['config_sha256'],config_file_sha256=quote['config_file_sha256'],source_sha256=sha_file(config['source']['path']),provenance_sha256=sha_file(directory/'source-provenance.json'),deferred_plan_sha256=scope_sha(plan) if plan else None))
    _mirror_reservations(path,m,a,_task_state(path,m))


def _managed_price(path,m,a,job,transport,*,refresh=False):
    task=_task_state(path,m);task_path=m['request']['task_ref']['task_manifest'];primary=a['jobs']['primary'];is_primary=job is primary
    current=task['operations'].get(job['operation_id'])
    if not current or not (current['submission_started'] or current.get('provider_request_id')):
        task_ledger.check_history_context(task_path,m['request']['task_ref']['target_id'],
            _generation_descriptor(m['request'],m['source']['sha256']) if is_primary else None)
    operation=task['operations'].get(primary['operation_id'])
    if operation is None:
        response=transport('pricing',Path(primary['path']))
        if response.get('state')!='PRICED':return response
        primary_quote=_quote(Path(primary['path']),response)
        plan=dict(schema='sprite_deferred_cleanup_v1',primary_operation_id=primary['operation_id'],primary_config_sha256=primary_quote['config_sha256'],model=CLEANUP,input={'sync_mode':False},reserved_units=1,source_from='video_sample_grid_v1' if m['request']['route']=='video' else 'primary_raw_png',source_limits=dict(mime='image/png',max_bytes=10485760,max_width=4096,max_height=4096))
        directory=path.parent/f'attempt-{a["index"]:02d}'/'cleanup-plan';directory.mkdir(exist_ok=True);new_or_identical(directory/'plan.json',encoded(plan))
        response=transport('pricing-plan',directory)
        if response.get('state')!='PRICED':return response
        cleanup_quote=_quote(directory,response,plan=plan)
        operations=[dict(operation_id=primary['operation_id'],kind='provider_animation',config_sha256=primary_quote['config_sha256'],quote_sha256=primary_quote['receipt_sha256'],amount_usd=primary_quote['estimated_cost_usd_decimal']),dict(operation_id=a['attempt_id']+':cleanup',kind='alpha_edge_cleanup',deferred_config=plan,quote_sha256=cleanup_quote['receipt_sha256'],amount_usd=cleanup_quote['estimated_cost_usd_decimal'])]
        _local_cap(task,m,operations)
        task_ledger.reserve_plan(task_path,m['run_id'],a['attempt_id'],a['strategy_sha256'],operations)
        _materialize(path,m,a,primary,primary_quote)
        primary.update(state='PRICED',needs_price=False);_mirror_reservations(path,m,a,_task_state(path,m));return None
    operation=task['operations'].get(job['operation_id'])
    if operation is None:raise WorkflowError('TASK_PLAN_INVALID')
    binding=operation.get('execution_binding')
    if binding and not refresh:
        job.update(transport_config_sha256=binding['config_sha256'],config_file_sha256=binding['config_file_sha256'])
        # A cleanup bind may have persisted before its exact executable quote
        # repricing. Only the matching current quote completes materialization.
        current=operation['current_quote']['quote_sha256']
        receipts=Path(job['path'])/'pricing-receipts'
        executable=next((p for p in receipts.glob('*.receipt.json') if sha_file(p)==current),None)
        if executable is not None or operation['submission_started']:
            _mirror_reservations(path,m,a,task);return None
    if operation['submission_started'] or operation.get('provider_request_id'):return None
    response=transport('pricing',Path(job['path']))
    if response.get('state')!='PRICED':return response
    quote=_quote(Path(job['path']),response);operations=_plan_operations(task,a)
    chosen=next(item for item in operations if item['operation_id']==job['operation_id'])
    chosen.update(quote_sha256=quote['receipt_sha256'],amount_usd=quote['estimated_cost_usd_decimal'])
    plan=operation.get('deferred_config');_materialize(path,m,a,job,quote,plan)
    _local_cap(task,m,operations);task_ledger.reprice_plan(task_path,m['run_id'],a['attempt_id'],operations)
    job.update(state='PRICED',needs_price=False);_mirror_reservations(path,m,a,_task_state(path,m));return None


def _capture_managed(path,m,job,response):
    task=_task_state(path,m);op=task['operations'][job['operation_id']];directory=Path(job['path']);state_path=directory/'job-state.json'
    durable=read_json(state_path) if state_path.exists() else {}
    known=response.get('request_id') or durable.get('request_id') or op.get('provider_request_id')
    if known:
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(known)) or durable.get('request_id')!=known
                or durable.get('config_sha256')!=job.get('transport_config_sha256')
                or (op.get('provider_request_id') and op['provider_request_id']!=known)):
            raise WorkflowError('TASK_CONTEXT_CONFLICT')
        if not op['submission_started']:raise WorkflowError('TASK_CONTEXT_CONFLICT')
        if not op.get('provider_request_id'):
            claim=read_json(directory/'submission.claim')
            if claim.get('config_sha256')!=job['transport_config_sha256'] or claim.get('pricing_receipt_sha256')!=durable.get('pricing_receipt_sha256'):
                raise WorkflowError('TASK_CONTEXT_CONFLICT')
        job['request_id']=known
        state=durable.get('state');status={'FAILED':'failed','CANCELLED':'cancelled','REMOTE_REJECTED':'rejected','REQUEST_ACCEPTED':'accepted'}.get(state,'pending')
        if state=='DOWNLOADED':
            job.update({key:durable[key] for key in ('raw_path','raw_sha256')});raw=_verified_raw(job)
            if raw.suffix=='.png':
                with Image.open(raw) as image:image.load()
            status='completed'
        previous=op['outcomes'][-1]['status'] if op['outcomes'] else None
        if previous in {'completed','failed','cancelled','rejected'}:status=previous
        evidence=f'{state_path}#sha256={sha_file(state_path)}'
        task_ledger.record_outcome(m['request']['task_ref']['task_manifest'],job['operation_id'],dict(status=status,provider_request_id=known,evidence=evidence))
        if response.get('state')!='BLOCKED':job['state']=state
    elif op['submission_started']:
        evidence=f'{directory}#marker_without_known_id'
        task_ledger.record_outcome(m['request']['task_ref']['task_manifest'],job['operation_id'],dict(status='uncertain',evidence=evidence))
        job['state']='SUBMISSION_STATUS_UNKNOWN';response=dict(response,state='SUBMISSION_STATUS_UNKNOWN',retry_safe=False)
    _save(path,m);return response


def _run_managed_job(path,m,job,transport,deadline):
    a=next(item for item in m['attempts'] if job in item['jobs'].values());delay_index=0;refreshed=False
    while True:
        task=_task_state(path,m);op=task['operations'].get(job['operation_id'])
        if op and (op['submission_started'] or op.get('provider_request_id')):
            binding=op.get('execution_binding')
            if not binding:raise WorkflowError('TASK_OPERATION_NOT_MATERIALIZED')
            job.update(transport_config_sha256=binding['config_sha256'],config_file_sha256=binding['config_file_sha256']);_save(path,m)
        else:
            response=_managed_price(path,m,a,job,transport,refresh=bool(job.get('needs_price')))
            if response is not None:return response
        response=transport('execute',Path(job['path']))
        response=_capture_managed(path,m,job,response)
        if response.get('block_reason')=='PRICE_STALE' and not refreshed:
            op=_task_state(path,m)['operations'][job['operation_id']]
            if not op['submission_started']:
                job['needs_price']=True;refreshed=True;_save(path,m);continue
        state=response.get('state')
        if state not in PENDING:return response
        remaining=deadline-time.monotonic();delay=[2,5,10][min(delay_index,2)]
        if remaining<=0 or delay>remaining:return response
        time.sleep(delay);delay_index+=1


def _run_job(path,m,job,transport,deadline):
    if _managed(m):return _run_managed_job(path,m,job,transport,deadline)
    directory=Path(job['path'])
    if job['state']=='SUBMISSION_STATUS_UNKNOWN':return {'state':'SUBMISSION_STATUS_UNKNOWN','retry_safe':False}
    if job['state']=='DOWNLOADED':
        _verified_raw(job)
        return dict(job)
    if job['state'] in {'REMOTE_REJECTED','FAILED','CANCELLED'}:return dict(job)
    delay_index=0;refreshed=False
    while True:
        if job['state']=='NEW' or job.get('needs_price'):
            priced=transport('pricing',directory)
            if priced.get('state')!='PRICED':return priced
            price=priced.get('estimated_cost_usd')
            if not isinstance(price,(float,int)) or isinstance(price,bool) or not math.isfinite(price) or price<=0:raise WorkflowError('PRICE_NOT_VERIFIABLE')
            spent=_result(manifest=m)['spent_estimate_usd']-job['reserved_usd'];config=read_json(directory/'config.json')
            if price+spent>m['authorization']['max_cost_usd']+1e-10 or price>config['max_cost_usd']:return {'state':'BLOCKED','block_reason':'COST_CAP_EXCEEDED'}
            job.update(reserved_usd=price,state='PRICED',needs_price=False)
            atomic_json(directory/'reservation.json',{'reserved_usd':price});_save(path,m)
        response=transport('execute',directory)
        state=response.get('state')
        if state=='BLOCKED' and response.get('block_reason')=='PRICE_STALE' and not refreshed and not job.get('request_id') and not (directory/'submission.claim').exists():
            job['needs_price']=True;_save(path,m);refreshed=True;continue
        if state=='KEY_SETUP_REQUIRED':return response
        job.update({k:response[k] for k in ('state','request_id','raw_path','raw_sha256') if response.get(k) is not None});_save(path,m)
        if state not in PENDING:return response
        if not job.get('request_id'):raise WorkflowError('TRANSPORT_RESPONSE_INVALID')
        remaining=deadline-time.monotonic()
        delay=[2,5,10][min(delay_index,2)]
        if remaining<=0 or delay>remaining:return response
        time.sleep(delay);delay_index+=1

def _drive(path,m,transport,poll_seconds):
    _reconcile_reviews(path,m)
    if m['state'] in ASSEMBLED:_verify_visual_state(m);return _result(m['state'],path,m)
    if m['state']=='SUBMISSION_STATUS_UNKNOWN' and not _managed(m):return _result(m['state'],path,m,m.get('block_reason'),False)
    if m['state']=='TECHNICAL_REJECTED' and m.get('block_reason') in CYCLE_REJECTIONS:
        _verified_raw(m['attempts'][-1]['jobs']['primary'])
        return _result('TECHNICAL_REJECTED',path,m,m['block_reason'])
    deadline=time.monotonic()+poll_seconds
    while True:
        a=m['attempts'][-1] if m['attempts'] else _new_attempt(path,m,None);q=m['request'];source=Path(m['source']['path'])
        recovering=False
        if _managed(m):
            task=_task_state(path,m);ref=q['task_ref'];operation=task['operations'].get(a['attempt_id']+':primary')
            recovering=bool(operation and (operation['submission_started'] or operation.get('provider_request_id')))
            if not recovering:task_ledger.check_history_context(ref['task_manifest'],ref['target_id'],_generation_descriptor(q,m['source']['sha256']))
        if q['route']=='video':
            resolve_ffmpeg(q['ffmpeg_exe'])
            if recovering:
                source=Path(a['prepared']['path'])  # Existing bytes were validated by _load; recovery does not prepare new artwork.
            else:
                prepared=prepare_source(source,path.parent/f'attempt-{a["index"]:02d}'/'prepared-source.png',pixel_art=q['pixel_art']);a['prepared']=prepared;source=Path(prepared['path']);_save(path,m)
        primary=_job(path,m,a,'primary',source);response=_run_job(path,m,primary,transport,deadline)
        if response.get('state')!='DOWNLOADED':return _job_result(path,m,response)
        raw=_verified_raw(primary)
        if q['route']=='video' and recovering and a.get('sampling'):
            raw=Path(a['sampling']['grid_path'])  # Validated existing sampling; recover a known cleanup before new local work.
        elif q['route']=='video':
            if _managed(m):task_ledger.check_history_context(ref['task_manifest'],ref['target_id'])
            try:
                sampling=sample_video(raw,path.parent/f'attempt-{a["index"]:02d}'/'sampling',ffmpeg_exe=q['ffmpeg_exe'],frame_count=q['frames'],loop=q['loop'],cycle_start_seconds=q['cycle_start_seconds'],cycle_duration_seconds=q['cycle_duration_seconds'])
            except VideoError as error:
                if error.code not in CYCLE_REJECTIONS:
                    raise
                a['diagnoses'].append({'code':error.code,'source_sha256':sha_file(raw)})
                if _managed(m):
                    diagnostic=path.parent/f'attempt-{a["index"]:02d}'/'cycle-rejection.json'
                    new_or_identical(diagnostic,encoded(dict(schema='sprite_technical_cycle_rejection_v1',run_id=m['run_id'],attempt_id=a['attempt_id'],code=error.code,raw_sha256=sha_file(raw))))
                    _release_cleanup(path,m,a,f'{diagnostic}#sha256={sha_file(diagnostic)}')
                    _reconcile_reviews(path,m)
                return _save(path,m,'TECHNICAL_REJECTED',error.code)
            a['sampling']=sampling;raw=Path(sampling['grid_path']);_save(path,m)
        while True:
            if 'cleanup' in a['jobs']:
                response=_run_job(path,m,a['jobs']['cleanup'],transport,deadline)
                if response.get('state')!='DOWNLOADED':return _job_result(path,m,response)
                raw=Path(a['jobs']['cleanup']['raw_path'])
            if _managed(m):task_ledger.check_history_context(ref['task_manifest'],ref['target_id'])
            try:
                assembled=assemble_sheet(raw,path.parent/f'attempt-{a["index"]:02d}'/'artifacts',frame_count=q['frames'],columns=q['columns'],rows=q['rows'],canvas=tuple(q['canvas']),frame_ms=q['frame_ms'],loop=q['loop'],pixel_art=q['pixel_art'],grounded=q['grounded'])
            except SheetImageError as e:
                if e.code=='ALPHA_REQUIRED' and 'cleanup' not in a['jobs']:
                    _job(path,m,a,'cleanup',raw);continue
                a['diagnoses'].append({'code':e.code,'source_sha256':sha_file(raw)});_save(path,m,'TECHNICAL_REJECTED',e.code)
                if _managed(m):
                    diagnostic=path.parent/f'attempt-{a["index"]:02d}'/f'technical-diagnosis-{len(a["diagnoses"]):03d}.json'
                    new_or_identical(diagnostic,encoded(a['diagnoses'][-1]));_release_cleanup(path,m,a,f'{diagnostic}#sha256={sha_file(diagnostic)}')
                if e.code in TECHNICAL and len(m['attempts'])<m['authorization']['max_generation_attempts']:
                    _new_attempt(path,m,f'Technical rejection {e.code}. Correct the pose separation, empty trailing cells and distinct silhouettes; use a materially different phase realization.');break
                return _result('TECHNICAL_REJECTED',path,m,e.code)
            m['artifact_paths']=assembled['artifact_paths'];files=[]
            for value in m['artifact_paths'].values():files.extend(value if isinstance(value,list) else [value])
            if a.get('sampling'):files.extend([a['sampling']['selection_path'],a['sampling']['grid_path'],*a['sampling']['frame_paths']])
            m['artifact_hashes']={f:sha_file(f) for f in files};m['consistency']=assembled['qa']['consistency']
            if _managed(m):
                _release_cleanup(path,m,a,f'{m["artifact_paths"]["qa_report"]}#sha256={sha_file(m["artifact_paths"]["qa_report"])};assembled_alpha_verified;artifact_set={_review_target(m)}')
            return _save(path,m,READY)


def _release_cleanup(path,m,a,evidence):
    task=_task_state(path,m);operation=task['operations'].get(a['attempt_id']+':cleanup')
    if operation and operation['status']=='held' and not operation['submission_started']:
        task_ledger.release_unsubmitted(m['request']['task_ref']['task_manifest'],operation['operation_id'],evidence)
        _mirror_reservations(path,m,a,_task_state(path,m))

def _job_result(path,m,response):
    state=response.get('state','BLOCKED');reason=response.get('block_reason');retry=response.get('retry_safe',False)
    if state in PENDING:state='PENDING';retry=bool(response.get('request_id'))
    return _save(path,m,state,reason,retry)

def _create_run(prepared,poll,transport):
    path=None;m=None
    try:
        run_id=uuid.uuid4().hex;root=prepared['workspace']/run_id;root.mkdir(parents=True);source=root/'source.png';new_or_identical(source,prepared['payload'])
        scope={'run_id':run_id,'source':{'path':str(source),'sha256':sha_file(source),'original_path':str(prepared['source'])},'request':prepared['request'],'authorization':prepared['authorization']}
        new_or_identical(root/'scope.json',encoded(scope));new_or_identical(root/'scope.sha256',(scope_sha(scope)+'\n').encode())
        m={'schema_version':3,'mode':'whole_sheet',**scope,'authorization_scope_sha256':scope_sha(scope),'attempts':[],'estimates':{'type':'conservative_operational_reservations','provider_billing_guarantee':False},'state':'INITIALIZED','artifact_paths':{},'defaults_notice':'Pixel art is enabled by default; 8 frames, 100 ms, source canvas unless explicitly overridden.'};path=root/'run-manifest.json';atomic_json(path,m)
        if _managed(m):_task_state(path,m,recover_bind=True)
        with run_lock(root):return _drive(path,m,transport or _transport,poll)
    except Exception as e:
        code=getattr(e,'code','WORKFLOW_INTERRUPTED');state='RUNTIME_REQUIRED' if code in {'RUNTIME_REQUIRED','NODE_RUNTIME_REQUIRED'} else 'BLOCKED'
        if path and m:return _save(path,m,state,code,code=='WORKFLOW_INTERRUPTED')
        return _result(state,path,m,code)


def _generate_legacy(options:dict,*,transport=None)->dict:
    try:
        poll=_poll(options.get('poll_seconds',45));prepared,missing=_options(options)
        if missing:return _result('NEEDS_INPUT',missing_fields=missing)
        return _create_run(prepared,poll,transport)
    except Exception as error:return _result('RUNTIME_REQUIRED' if getattr(error,'code',None)=='RUNTIME_REQUIRED' else 'BLOCKED',reason=getattr(error,'code','WORKFLOW_INTERRUPTED'))


def generate(options:dict,*,transport=None)->dict:
    validated_task=None
    try:
        poll=_poll(options.get('poll_seconds',45));prepared,missing=_options(options)
        if missing:return _result('NEEDS_INPUT',missing_fields=missing)
        if not options.get('task_manifest'):raise WorkflowError('TASK_CONTEXT_REQUIRED')
        task_path=str(Path(options['task_manifest']).expanduser().resolve());task=task_ledger.inspect_task(task_path)
        target=_target(task,prepared['request'],hashlib.sha256(prepared['payload']).hexdigest(),options.get('target_id'))
        if not {'provider_animation','alpha_edge_cleanup'}.issubset(task['authorization']['allowed_operations']):raise WorkflowError('OPERATION_NOT_AUTHORIZED')
        validated_task=task
        task_ledger.check_history_context(task_path,target['target_id'],_generation_descriptor(prepared['request'],hashlib.sha256(prepared['payload']).hexdigest()))
        prepared['request']['task_ref']=dict(schema='sprite_task_ref_v1',task_id=task['task_id'],task_manifest=task_path,authorization_sha256=task['authorization_sha256'],target_id=target['target_id'])
        return _create_run(prepared,poll,transport)
    except Exception as error:return _result('RUNTIME_REQUIRED' if getattr(error,'code',None)=='RUNTIME_REQUIRED' else 'BLOCKED',reason=getattr(error,'code','WORKFLOW_INTERRUPTED'),task_snapshot=validated_task)

def resume(manifest_path,*,transport=None,poll_seconds=45)->dict:
    path=Path(manifest_path).expanduser().resolve();m=None
    try:
        poll=_poll(poll_seconds)
        path,m=_load(path,recover_bind=True,allow_reservation_lag=True)  # Read/validate a recoverable mirror lag before locking.
        if m['request'].get('route')=='regional_video_inpaint_v1' and not m.get('derivation'):
            raise WorkflowError('REGIONAL_DRIVER_REQUIRED')
        if m['request'].get('route')=='endpoint_cycle_v1' and not m.get('derivation'):
            raise WorkflowError('ENDPOINT_DRIVER_REQUIRED')
        with run_lock(path.parent):
            path,m=_load(path,allow_reservation_lag=True);_repair_reservation_mirrors(path,m)
            return _drive(path,m,transport or _transport,poll)
    except Exception as e:
        code=getattr(e,'code','WORKFLOW_INTERRUPTED')
        # Do not mutate outside the lock, particularly after scope/tamper failures.
        return _result('RUNTIME_REQUIRED' if code=='RUNTIME_REQUIRED' else 'BLOCKED',path,m,code,code=='WORKFLOW_INTERRUPTED')

def revise(manifest_path,feedback,*,transport=None,poll_seconds=45,correction=None):
    path=Path(manifest_path).expanduser().resolve();m=None
    try:
        if not isinstance(feedback,str) or not 1<=len(feedback.strip())<=2000:raise WorkflowError('FEEDBACK_REQUIRED')
        poll=_poll(poll_seconds)
        path,m=_load(path)  # Validate before creating a lock file; re-read under lock below.
        if m['request'].get('route')=='regional_video_inpaint_v1' and not m.get('derivation'):
            raise WorkflowError('REGIONAL_DRIVER_REQUIRED')
        if m['request'].get('route')=='endpoint_cycle_v1' and not m.get('derivation'):
            raise WorkflowError('ENDPOINT_DRIVER_REQUIRED')
        with run_lock(path.parent):
            path,m=_load(path)
            _reconcile_reviews(path,m)
            if m.get('derivation'):raise WorkflowError('LOCAL_DERIVATION_REVISION_REQUIRED')
            if m['state']=='SUBMISSION_STATUS_UNKNOWN':return _result(m['state'],path,m,m.get('block_reason'))
            if m['state'] not in ASSEMBLED|{'TECHNICAL_REJECTED','REMOTE_REJECTED','FAILED','CANCELLED'}:raise WorkflowError('REVISION_REQUIRES_FINISHED_ATTEMPT')
            if m['state'] in ASSEMBLED:_verify_visual_state(m)
            _new_attempt(path,m,feedback.strip(),correction);m['artifact_paths']={};m['artifact_hashes']={};_save(path,m);return _drive(path,m,transport or _transport,poll)
    except Exception as e:return _result(path=path,manifest=m,reason=getattr(e,'code','WORKFLOW_INTERRUPTED'))

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    gen=commands.add_parser('generate');gen.add_argument('--source');gen.add_argument('--action');gen.add_argument('--orientation');gen.add_argument('--frames',type=int,default=8);gen.add_argument('--frame-ms',type=int,default=100);gen.add_argument('--canvas',default='source');gen.add_argument('--loop',choices=['auto','true','false'],default='auto');gen.add_argument('--max-cost-usd',type=float,default=.5);gen.add_argument('--max-generation-attempts',type=int,default=2);gen.add_argument('--workspace-root');gen.add_argument('--description');gen.add_argument('--pixel-art',choices=['true','false']);gen.add_argument('--strategy',choices=['auto','sheet','video'],default='auto');gen.add_argument('--ffmpeg-exe');gen.add_argument('--cycle-start-seconds',type=float);gen.add_argument('--cycle-duration-seconds',type=float)
    gen.add_argument('--poll-seconds',type=float,default=45)
    gen.add_argument('--task-manifest');gen.add_argument('--target-id')
    for name in ['resume','inspect','revise']:
        sub=commands.add_parser(name);sub.add_argument('--run-manifest',required=True)
        if name!='inspect':sub.add_argument('--poll-seconds',type=float,default=45)
        if name=='revise':sub.add_argument('--feedback',required=True);sub.add_argument('--correction-file')
    review_parser=commands.add_parser('review');review_parser.add_argument('--run-manifest',required=True);review_parser.add_argument('--review-file',required=True)
    export_parser=commands.add_parser('export');export_parser.add_argument('--run-manifest',required=True);export_parser.add_argument('--output-dir',required=True)
    options=vars(parser.parse_args(argv));command=options.pop('command')
    if command=='generate':result=generate(options)
    elif command=='inspect':result=inspect(options['run_manifest'])
    elif command=='resume':result=resume(options['run_manifest'],poll_seconds=options['poll_seconds'])
    elif command=='review':result=record_visual_review(options['run_manifest'],options['review_file'])
    elif command=='export':result=export_artifacts(options['run_manifest'],options['output_dir'])
    else:result=revise(options['run_manifest'],options['feedback'],poll_seconds=options['poll_seconds'],correction=read_json(options['correction_file']) if options.get('correction_file') else None)
    print(json.dumps(result,ensure_ascii=False));return 0 if result['state'] in {READY,DELIVERABLE,'PENDING','NEEDS_INPUT'} else 2
if __name__=='__main__':raise SystemExit(main())
