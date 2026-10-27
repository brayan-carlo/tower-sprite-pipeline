"""Bound native task/receipt helpers and a read-only native snapshot adapter.

No generic/provider ledger is manufactured. A derivative binds the native scope,
strict embedded review, exact checked artifact set and native construction inputs.
Moving/removing the parent blocks delivery until legitimate parent recovery.
Native lifecycle callers hold their run lock for receipt mutations; capture and
verify_origin only validate, and never repair a parent or its task history.
"""
import hashlib
import io
import json
from pathlib import Path
from PIL import Image
from sheet_workflow_state import WorkflowError, encoded, new_or_identical, read_json, run_lock, scope_sha
import sprite_task_ledger as ledger
from sprite_alignment import POLICY

SCHEMAS={'native_sheet_v1','native_articulated_workflow_v1'}


def task_ref(task_manifest, request, source_sha256, target_id=None):
    """Resolve existing human authorization; never manufacture a task or budget."""
    import generic_sprite_workflow as generic
    if not task_manifest:raise WorkflowError('TASK_CONTEXT_REQUIRED')
    task=ledger.inspect_task(task_manifest)
    target=generic._target(task,request,source_sha256,target_id)
    if 'native_motion_construction' not in task['authorization']['allowed_operations']:
        raise WorkflowError('OPERATION_NOT_AUTHORIZED')
    ledger.check_history_context(task['task_manifest'],target['target_id'],dict(route='native_motion_construction',
        model=request['profile'],conditioning='local',inputs=[dict(role='source',sha256=source_sha256)]))
    return dict(schema='sprite_task_ref_v1',task_id=task['task_id'],task_manifest=task['task_manifest'],
                authorization_sha256=task['authorization_sha256'],target_id=target['target_id'])


def native_scope(m):
    keys=('request','source','source_geometry') if m['schema']=='native_articulated_workflow_v1' else ('request','source')
    return {key:m[key] for key in keys}


def seal_native(path,m):
    if 'task_ref' not in m['request']:return
    scope=native_scope(m)
    new_or_identical(path.parent/'scope.json',encoded(scope))
    new_or_identical(path.parent/'scope.sha256',(scope_sha(scope)+'\n').encode())


def native_descriptor(m):
    return dict(route='native_motion_construction',model=m['request']['profile'],conditioning='local',
                inputs=[dict(role='source',sha256=m['source']['sha256'])])


def native_attempt(m,number,correction=None):
    ref=m['request']['task_ref'];descriptor=native_descriptor(m)
    return dict(attempt_id=m['run_id']+f':attempt-{number:02d}',
        strategy_sha256=scope_sha(dict(target_id=ref['target_id'],effective_descriptor=descriptor)),correction=correction)


def native_task_state(path,m,*,recover=False):
    """Independent native seal plus C's actual task/bind validation; no pixel waiver."""
    import generic_sprite_workflow as generic
    scope_path=path.parent/'scope.json'
    managed='task_ref' in m['request']
    if not managed and not scope_path.exists():return None
    scope=read_json(scope_path)
    if (not managed or 'task_ref' not in scope.get('request',{}) or scope!=native_scope(m)
            or scope_sha(scope)!=m['scope_sha256']
            or (path.parent/'scope.sha256').read_text().strip()!=scope_sha(scope)):
        raise WorkflowError('TASK_SCOPE_MISMATCH')
    task=generic._task_state(path,{**m,'authorization_scope_sha256':m['scope_sha256']},recover_bind=recover)
    if 'native_motion_construction' not in task['authorization']['allowed_operations']:
        raise WorkflowError('OPERATION_NOT_AUTHORIZED')
    limit=m['request'].get('task_max_attempts',m['request'].get('max_attempts'))
    if type(limit) is not int or not 1<=limit<=3:raise WorkflowError('ATTEMPT_LIMIT_INVALID')
    ref=m['request']['task_ref']
    # Native articulated BUILDING is persisted as ASSEMBLING.
    if recover and m['state'] in {'PREPARATION_PENDING','PREPARED','ASSEMBLING'}:
        ledger.check_history_context(ref['task_manifest'],ref['target_id'],native_descriptor(m))
    for number,a in enumerate(m['attempts'],1):
        expected=native_attempt(m,number,a.get('correction'))
        if any(a.get(key)!=value for key,value in expected.items()):raise WorkflowError('TASK_STRATEGY_MISMATCH')
        matches=[entry for entry in task['attempts'].values() if entry['run_id']==m['run_id'] and entry['attempt_id']==a['attempt_id']]
        if recover and not matches:
            ledger.check_history_context(ref['task_manifest'],ref['target_id'],native_descriptor(m))
            digest=ledger.begin_attempt(m['request']['task_ref']['task_manifest'],m['run_id'],a['attempt_id'],
                native_descriptor(m),limit,a.get('correction'))
            if digest!=a['strategy_sha256']:raise WorkflowError('TASK_STRATEGY_MISMATCH')
    if recover:task=ledger.inspect_task(m['request']['task_ref']['task_manifest'])
    for a in m['attempts']:
        matches=[entry for entry in task['attempts'].values() if entry['run_id']==m['run_id'] and entry['attempt_id']==a['attempt_id']]
        if (len(matches)!=1 or matches[0]['strategy_sha256']!=a['strategy_sha256']
                or matches[0]['correction']!=a.get('correction') or matches[0]['source_attempt'] is not None):
            raise WorkflowError('TASK_STRATEGY_MISMATCH')
        strategy=task['strategies'][a['strategy_sha256']]
        if (strategy['effective_descriptor']!=native_descriptor(m) or strategy['max_attempts']!=limit
                or strategy.get('strategy_scope','target')!='target'):
            raise WorkflowError('TASK_STRATEGY_MISMATCH')
    return task


def native_budget(m):
    if 'task_ref' not in m['request']:return {}
    task=ledger.inspect_task(m['request']['task_ref']['task_manifest'])
    return dict(**({'history_status':task['history_status']} if 'history_status' in task else {}),
        task_budget={**{key:task[key] for key in ('cap_usd','reserved_total_usd','available_estimate_usd','uncertainty')},
        'budget_basis':task['authorization']['budget_basis'],'invoice_verified':False},
        external_native_costs='unverified; this local invocation does not price external image generation')


def native_review_records(path,m,validate):
    """Validate all immutable native receipts against this exact current artwork."""
    records=[];directory=path.parent/f'attempt-{len(m["attempts"]):02d}'
    for number,receipt in enumerate(sorted(directory.glob('visual-review-*.json')),1):
        if receipt.name!=f'visual-review-{number:03d}.json' or receipt.is_symlink():
            raise WorkflowError('VISUAL_REVIEW_TAMPERED')
        report=read_json(receipt);validate(report);records.append((receipt,report))
    return records


def reconcile_native_reviews(path,m,validate,*,write=False):
    """Native receipts have their own layout; reuse only C's pure mapping/projection."""
    import generic_sprite_workflow as generic
    if 'task_ref' not in m['request']:return None
    records=native_review_records(path,m,validate)
    current=generic._task_review_state(m).get('review')
    if not records:
        if current or m['attempts'][-1].get('visual_review'):raise WorkflowError('VISUAL_REVIEW_TAMPERED')
        return None
    context=view(m)
    def evidence(receipt):return generic._review_evidence(receipt,context)
    canonical=None
    if current:
        canonical=next(((p,r) for p,r in records if evidence(p)==current['evidence']),None)
        if (canonical is None or canonical[1]['decision']!=current['decision']
                or generic._validate_visual_report(context,canonical[1])!=current['defects']):
            raise WorkflowError('VISUAL_REVIEW_TAMPERED')
    rejects=[item for item in records if item[1]['decision']=='reject']
    selected=rejects[0] if rejects else canonical or records[0]
    if current and current['decision']=='reject' and any(evidence(p)!=current['evidence'] for p,_ in rejects):
        raise WorkflowError('TASK_REVIEW_CONFLICT')
    receipt,report=selected
    if write:
        ledger.record_review(m['request']['task_ref']['task_manifest'],m['run_id'],m['attempts'][-1]['attempt_id'],
            report['decision'],generic._validate_visual_report(context,report),evidence(receipt))
    elif (current is None or evidence(receipt)!=current['evidence']
            or m['attempts'][-1].get('visual_review')!=report):
        raise WorkflowError('NATIVE_REVIEW_RECONCILIATION_REQUIRED')
    if report['decision']=='pass':generic._task_review_state(m,require_pass=True)
    return report


def archive_native_review(path,m,report,validate):
    import generic_sprite_workflow as generic
    validate(report);current=generic._task_review_state(m).get('review')
    if current and current['decision']=='reject':raise WorkflowError('TASK_REVIEW_CONFLICT')
    directory=path.parent/f'attempt-{len(m["attempts"]):02d}'
    receipt=directory/f'visual-review-{len(list(directory.glob("visual-review-*.json")))+1:03d}.json'
    new_or_identical(receipt,encoded(report))
    return reconcile_native_reviews(path,m,validate,write=True)

def is_native(parent):
    return parent.get('schema') in SCHEMAS

def view(parent):
    if not is_native(parent):return parent
    a=parent['attempts'][-1]
    return {**parent,'artifact_paths':a['artifact_paths'],'artifact_hashes':a['artifact_hashes'],
            'consistency':a['consistency'],'authorization_scope_sha256':parent['scope_sha256']}

def request(parent):
    return {**parent['request'],**({'pixel_art':True} if is_native(parent) else {})}

def _hash(payload):return hashlib.sha256(payload).hexdigest()

def _image(payload):
    with Image.open(io.BytesIO(payload)) as im:return im.convert('RGBA')

def _verify_sheet_processing(attempt,payloads):
    """Replay the native raw→matte→registration chain from the captured buffers."""
    import native_sheet_images as images
    from native_components import analyze_components, apply_component_review
    paths=attempt['processing_paths'];raw=attempt['raw'];source=_image(payloads[raw['path']])
    digest=_hash(source.width.to_bytes(4,'big')+source.height.to_bytes(4,'big')+source.tobytes())
    if digest!=raw['pixel_sha256']:raise WorkflowError('NATIVE_PROCESSING_MISMATCH')
    alpha,matte=images._difference_matte(source)
    review_summary=None
    if 'matte_review' in paths:
        receipt=json.loads(payloads[paths['matte_review']]);before=analyze_components(alpha)
        if (receipt['schema']!='native_matte_review_v1' or receipt['input_sha256']!=raw['sha256']
                or receipt['image_pixel_sha256']!=before['image_pixel_sha256']
                or receipt['components_report']!=before
                or payloads[paths['alpha_before_review']]!=images._png_bytes(alpha)):
            raise WorkflowError('NATIVE_PROCESSING_MISMATCH')
        alpha=apply_component_review(alpha,before,receipt['decisions']);after=analyze_components(alpha)
        if receipt['reviewed_image_pixel_sha256']!=after['image_pixel_sha256']:raise WorkflowError('NATIVE_PROCESSING_MISMATCH')
        review_summary={'image_pixel_sha256':before['image_pixel_sha256'],
                        'reviewed_image_pixel_sha256':after['image_pixel_sha256'],
                        'artifact_sha256':{k:_hash(payloads[paths[k]]) for k in ('alpha_before_review','matte_review','alpha')}}
    registered,registration=images._register_strides(alpha,strict_components=True,alignment_policy=POLICY)
    components={'schema':'native_sheet_components_v1','frames':[
        {'frame':i+1,**analyze_components(registered.crop(((i%4)*512,(i//4)*512,(i%4+1)*512,(i//4+1)*512)))} for i in range(8)]}
    report={'schema':'native_sheet_processing_v1','input_sha256':raw['sha256'],
            'matte':matte,'registration':registration,'visual_review_required':True,'delivery_allowed':False}
    if review_summary is not None:report['matte_review']=review_summary
    if (payloads[paths['alpha']]!=images._png_bytes(alpha)
            or payloads[paths['registered']]!=images._png_bytes(registered)
            or json.loads(payloads[paths['components_report']])!=components
            or json.loads(payloads[paths['processing_report']])!=report):
        raise WorkflowError('NATIVE_PROCESSING_MISMATCH')

def capture(manifest_path):
    """Verify native semantics under its lifecycle lock, returning exact checked bytes."""
    import native_articulated_workflow as articulated
    import native_sprite_workflow as sheet
    path=articulated._outside_skill(manifest_path)
    with run_lock(path.parent):
        before=path.read_bytes();raw=json.loads(before)
        if raw.get('schema')==articulated.SCHEMA:
            module=articulated;path,parent=module._load(path)
            verified=module._verify(path,parent,mutate_on_error=False)
            scope=module._scope(parent)
            payloads=dict(verified['payloads']) if verified else {}
            source_bytes=verified['source_bytes'] if verified else b''
        elif raw.get('schema')==sheet.SCHEMA:
            module=sheet;path,parent=module._load(path)
            scope={'request':parent['request'],'source':parent['source']}
            payloads={};source_bytes=Path(parent['source']['path']).read_bytes()
        else:raise WorkflowError('ALIGNMENT_PARENT_UNSUPPORTED')
        a=parent['attempts'][-1]
        if (parent!=raw or parent['request'].get('alignment_policy')!=POLICY
                or parent['state']!='READY_FOR_ALIGNMENT'
                or a.get('visual_review',{}).get('decision')!='pass'):
            raise WorkflowError('NATIVE_ALIGNMENT_REQUIRES_REVIEW')
        # Include exact construction evidence, not a synthetic visual-review path.
        hashes={**a['artifact_hashes'],**a.get('processing_hashes',{})}
        if 'raw' in a:hashes[a['raw']['path']]=a['raw']['sha256']
        if 'prompt_file' in a:hashes[a['prompt_file']]=a['prompt_sha256']
        if 'snapshot' in a:
            snapshot=a['snapshot'];hashes.update(snapshot['input_hashes'])
            hashes[snapshot['recipe_path']]=snapshot['recipe_sha256']
        for name,digest in hashes.items():
            payload=payloads[name] if name in payloads else Path(name).read_bytes()
            if _hash(payload)!=digest:raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
            payloads[name]=payload
        if _hash(source_bytes)!=parent['source']['sha256']:raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
        input_bytes=[payloads[p] for p in a['artifact_paths']['frames']]
        if module is sheet:
            import native_geometry
            if not parent['request'].get('geometry_protocol') or not a.get('geometry_validated'):
                raise WorkflowError('GEOMETRY_REVIEW_REQUIRED')
            _verify_sheet_processing(a,payloads)
            report=json.loads(payloads[a['artifact_paths']['geometry_review']])
            if report.get('geometry_target_sha256')!=sheet._geometry_target(parent):
                raise WorkflowError('GEOMETRY_REVIEW_STALE')
            frames,geometry,reference=native_geometry.calibrate_frames(
                _image(payloads[a['processing_paths']['registered']]),parent['source']['path'],
                parent['request']['source_geometry'],report,alignment_policy=POLICY)
            if (json.loads(payloads[a['artifact_paths']['geometry']])!=geometry
                    or any(_image(p).size!=f.size or _image(p).tobytes()!=f.tobytes() for p,f in zip(input_bytes,frames))
                    or _image(payloads[a['artifact_paths']['source_reference']]).tobytes()!=reference.tobytes()):
                raise WorkflowError('NATIVE_GEOMETRY_MISMATCH')
            # Completeness depends on actual captured pixels, not mutable flags.
            context=sheet._verified_review_context(parent,frames=[_image(p) for p in input_bytes],
                qa=json.loads(payloads[a['artifact_paths']['qa_report']]))
            sheet._validate_visual_report(context,a['visual_review'])
        if 'task_ref' in parent['request']:
            if module is articulated:module._sync_reviews(path,parent,verified,write=False)
            else:module._sync_reviews(path,parent,write=False)
        # A noncooperating writer cannot replace verified inputs with newly trusted bytes.
        _,after=module._load(path)
        if path.read_bytes()!=before or after!=parent:raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
        from generic_sprite_workflow import _review_target
        target=_review_target(view(parent))
        evidence={};records=[]
        for i,(name,payload) in enumerate(sorted(payloads.items()),1):
            snapshot_name=f'native-{i:03d}.bin';evidence[snapshot_name]=payload
            records.append({'path':name,'sha256':_hash(payload),'snapshot':snapshot_name})
        evidence['native-evidence.json']=encoded(records)
        evidence['native-review.json']=encoded(a['visual_review'])
        origin={'schema':parent['schema'],'attempt':len(parent['attempts']),
                'review_sha256':_hash(evidence['native-review.json']),
                'artifact_set_sha256':target}
        return (path,parent,before,encoded(scope),input_bytes,source_bytes),evidence,origin

def verify_origin(derivation,parent,parent_scope,root):
    """Re-run native validation, then compare live checked origin with bound snapshots."""
    captured,evidence,origin=capture(derivation['parent_manifest'])
    _,live,_,scope_bytes,inputs,source=captured
    expected_scope={key:parent[key] for key in ('request','source')}
    if parent['schema']=='native_articulated_workflow_v1':expected_scope['source_geometry']=parent['source_geometry']
    if (parent_scope!=expected_scope or origin!=derivation.get('native_origin') or scope_sha(parent_scope)!=parent['scope_sha256']
            or scope_sha(json.loads(scope_bytes))!=derivation['parent_scope_sha256']
            or parent['run_id']!=live['run_id']
            or parent['attempts'][-1]['visual_review']!=live['attempts'][-1]['visual_review']
            or parent['attempts'][-1]['artifact_hashes']!=live['attempts'][-1]['artifact_hashes']
            or _hash(source)!=derivation['files']['source.png']):
        raise WorkflowError('NATIVE_ALIGNMENT_ORIGIN_CHANGED')
    required=set(evidence)
    if {n for n in derivation['files'] if n.startswith('native-')}!=required:
        raise WorkflowError('NATIVE_ALIGNMENT_ORIGIN_CHANGED')
    for name,payload in evidence.items():
        if (root/'derivation'/name).read_bytes()!=payload:raise WorkflowError('NATIVE_ALIGNMENT_ORIGIN_CHANGED')
    for i,payload in enumerate(inputs,1):
        if _hash(payload)!=derivation['files'][f'input-{i:02d}.png']:raise WorkflowError('NATIVE_ALIGNMENT_ORIGIN_CHANGED')
