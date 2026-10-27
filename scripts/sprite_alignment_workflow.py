"""Offline, artifact-bound body-X derivation. Geometry never grants visual PASS."""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import uuid
import sprite_task_ledger as ledger

from PIL import Image
import sheet_images as codec
import native_alignment_source as native_source
from sprite_alignment import POLICY, align_body_x
from sheet_workflow_state import WorkflowError, atomic_json, encoded, new_or_identical, read_json, run_lock, scope_sha, sha_file


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _image(payload):
    with Image.open(io.BytesIO(payload)) as im:
        if im.format!='PNG' or im.width*im.height>64_000_000:
            raise WorkflowError('ALIGNMENT_ANNOTATIONS_INVALID')
        return im.convert('RGBA')


def _point(point, size):
    return (isinstance(point,list) and len(point)==2
            and all(type(v) in (int,float) and math.isfinite(v) for v in point)
            and 0<=point[0]<=size[0]-1 and 0<=point[1]<=size[1]-1)


def _annotations(value, parent, frames, source):
    import generic_sprite_workflow as workflow
    parent=native_source.view(parent)
    try:
        reference=value['reference'];items=value['frames']
        valid=(type(value['schema_version']) is int and value['schema_version']==1
            and value['artifact_set_sha256']==workflow._review_target(parent)
            and isinstance(reference,dict)
            and all(isinstance(reference[k],str) and 1<=len(reference[k].strip())<=2000 for k in ('name','description'))
            and _point(reference['source_point'],source.size)
            and isinstance(items,list) and len(items)==len(frames))
        if not valid:raise WorkflowError('ALIGNMENT_ANNOTATIONS_INVALID')
        for i,(item,frame) in enumerate(zip(items,frames),1):
            if (not isinstance(item,dict) or type(item.get('frame')) is not int or item['frame']!=i
                    or item.get('sha256')!=parent['artifact_hashes'][parent['artifact_paths']['frames'][i-1]]
                    or not _point(item.get('point'),frame.size)
                    or frame.getchannel('A').getbbox() is None):
                raise WorkflowError('ALIGNMENT_ANNOTATIONS_INVALID')
    except (KeyError,TypeError,IndexError):
        raise WorkflowError('ALIGNMENT_ANNOTATIONS_INVALID') from None
    return [item['point'] for item in items]


def _artifacts(frames, request, report, *, parent_preview=None):
    """Encode aligned pixels directly; never normalize/ground or rescale again."""
    version=request.get('alignment_codec_version',1)
    if type(version) is not int or version not in (1,2,3):raise WorkflowError('ALIGNMENT_CODEC_UNSUPPORTED')
    gif_options={'binary_alpha':True}
    if version==2:gif_options['palette_strategy']='max_coverage'
    width,height=frames[0].size
    if version==3:
        if parent_preview is None:raise WorkflowError('ALIGNMENT_SOURCE_GIF_REQUIRED')
        gif=codec._translated_gif_bytes(parent_preview,report['transforms'],
            input_canvas=tuple(report['input_canvas'][k] for k in ('width','height')),
            output_canvas=(width,height),frame_ms=request['frame_ms'],loop=request['loop'])
    else:gif=codec._gif_bytes(frames,request['frame_ms'],request['loop'],**gif_options)
    sheet=Image.new('RGBA',(width*len(frames),height))
    for i,frame in enumerate(frames):sheet.paste(frame,(i*width,0))
    payloads={f'frame-{i:02d}.png':codec._png_bytes(frame) for i,frame in enumerate(frames,1)}
    payloads.update({'sprite-sheet.png':codec._png_bytes(sheet),
        'preview.gif':gif,
        'contact.png':codec._png_bytes(codec._contact_image(frames,pixel_art=request['pixel_art']))})
    dimensions,durations,contact=codec._validate_encoded_artifacts(
        [payloads[f'frame-{i:02d}.png'] for i in range(1,len(frames)+1)],payloads['sprite-sheet.png'],
        payloads['preview.gif'],payloads['contact.png'],frame_count=len(frames),canvas=(width,height),
        frame_ms=request['frame_ms'],loop=request['loop'])
    qa={'state':'READY_FOR_VISUAL_REVIEW','semantic_review_required':True,'delivery_allowed':False,
        'consistency':codec.consistency_diagnostics(frames),'alignment':report,
        'canvas':[width,height],'frame_count':len(frames),'frame_ms':request['frame_ms'],
        'loop':request['loop'],'pixel_art':request['pixel_art'],'grounded':False,
        'sheet_dimensions':dimensions,'contact_dimensions':contact,
        'preview':{'frame_count':len(frames),'durations_ms':durations,'loop':request['loop'],
                   'alpha_threshold':128,'palette':'shared_255_colors'},
        'sha256':{name:_digest(payload) for name,payload in payloads.items()}}
    if 'alignment_codec_version' in request:
        qa['preview'].update(alignment_codec_version=version,
            palette_strategy='max_coverage' if version==2 else 'median_cut')
    if version==3:
        qa['preview'].pop('alpha_threshold')
        qa['preview'].update(palette='per_frame_exact_parent_colors',palette_strategy='exact_parent_per_frame',
            alpha_source='decoded_parent_gif',gif_mode='translated_parent_exact_v1',parent_gif_sha256=_digest(parent_preview))
    payloads['qa.json']=encoded(qa)
    return payloads,qa


def _safe_output(output, parent_root):
    import generic_sprite_workflow as workflow
    installed=Path.home()/'.codex/skills'
    if any(output==p or p in output.parents for p in (installed,workflow.SKILL_ROOT,parent_root)):
        raise WorkflowError('WORKSPACE_NOT_ALLOWED')


def _capture_parent(manifest_path):
    """Freeze exactly the parent validated under its existing lifecycle lock."""
    import generic_sprite_workflow as workflow
    parent_path,_=workflow._load(manifest_path)
    with run_lock(parent_path.parent):
        parent_bytes=parent_path.read_bytes()
        scope_path=parent_path.parent/'scope.json'
        digest_path=parent_path.parent/'scope.sha256'
        scope_bytes=scope_path.read_bytes();digest_bytes=digest_path.read_bytes()
        parent_path,parent=workflow._load(parent_path)
        if json.loads(parent_bytes)!=parent:raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
        if parent.get('mode')!='whole_sheet':raise WorkflowError('ALIGNMENT_PARENT_UNSUPPORTED')
        # Realign native-origin frames from their native parent so the live strict
        # review requirement cannot be dropped through a self-contained v3 hop.
        if (parent.get('derivation') or {}).get('native_origin'):raise WorkflowError('ALIGNMENT_NATIVE_PARENT_REQUIRED')
        if parent['state'] not in workflow.ASSEMBLED:raise WorkflowError('ALIGNMENT_REQUIRES_ARTIFACTS')
        workflow._verify_visual_state(parent)
        if parent['request'].get('alignment_policy',POLICY)!=POLICY:raise WorkflowError('ALIGNMENT_POLICY_UNSUPPORTED')
        input_bytes=[Path(p).read_bytes() for p in parent['artifact_paths']['frames']]
        source_bytes=Path(parent['source']['path']).read_bytes()
        preview_bytes=Path(parent['artifact_paths']['preview_gif']).read_bytes()
        # The OS lock coordinates normal lifecycle writers. Equality and a second
        # loader pass also detect writers that did not participate in that lock.
        _,captured=workflow._load(parent_path)
        if (parent_path.read_bytes()!=parent_bytes or json.loads(parent_bytes)!=captured
                or scope_path.read_bytes()!=scope_bytes or digest_path.read_bytes()!=digest_bytes
                or _digest(source_bytes)!=parent['source']['sha256']
                or _digest(preview_bytes)!=parent['artifact_hashes'][parent['artifact_paths']['preview_gif']]
                or any(_digest(payload)!=parent['artifact_hashes'][name]
                       for payload,name in zip(input_bytes,parent['artifact_paths']['frames']))):
            raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
        return parent_path,parent,parent_bytes,scope_bytes,input_bytes,source_bytes,preview_bytes


def _managed_alignment(m,parent,*,allow_setup_missing=False):
    import generic_sprite_workflow as workflow
    d=m['derivation'];ref=m['request']['task_ref'];root=Path(m['source']['path']).parent
    if (ref!=parent['request'].get('task_ref') or not isinstance(ref,dict)
            or set(ref)!={'schema','task_id','task_manifest','authorization_sha256','target_id'}
            or ref['schema']!='sprite_task_ref_v1' or str(Path(ref['task_manifest']).resolve())!=ref['task_manifest']):
        raise WorkflowError('TASK_SCOPE_MISMATCH')
    task=ledger.inspect_task(ref['task_manifest'])
    if task['task_id']!=ref['task_id'] or task['authorization_sha256']!=ref['authorization_sha256']:
        raise WorkflowError('TASK_SCOPE_MISMATCH')
    workflow._target(task,m['request'],m['source']['sha256'],ref['target_id'])
    workflow._target(task,parent['request'],parent['source']['sha256'],ref['target_id'])
    if 'body_x_alignment' not in task['authorization']['allowed_operations']:raise WorkflowError('OPERATION_NOT_AUTHORIZED')
    source=dict(run_id=parent['run_id'],attempt_id=parent['attempts'][-1]['attempt_id'])
    if (d['source_attempt']!=source or source['attempt_id']!=parent['run_id']+f':attempt-{len(parent["attempts"]):02d}'):
        raise WorkflowError('TASK_SOURCE_ATTEMPT_MISMATCH')
    parent_bound=dict(run_id=parent['run_id'],target_id=ref['target_id'],scope_sha256=d['parent_scope_sha256'],manifest_path=d['parent_manifest'])
    if parent.get('derivation'):parent_bound['parent_run_id']=parent['derivation']['parent_run_id']
    if task['runs'].get(parent['run_id'])!=parent_bound:raise WorkflowError('TASK_SCOPE_MISMATCH')
    bound=dict(run_id=m['run_id'],target_id=ref['target_id'],scope_sha256=m['authorization_scope_sha256'],
        manifest_path=str(root/'run-manifest.json'),parent_run_id=parent['run_id'])
    actual=task['runs'].get(m['run_id'])
    if actual!=bound and (actual is not None or not allow_setup_missing):raise WorkflowError('TASK_SCOPE_MISMATCH')
    descriptor=dict(route='body_x_alignment',model=POLICY,conditioning='local',inputs=[dict(role='source',sha256=m['source']['sha256'])])
    group=ledger._alignment_source_group(task,source)
    spec=d['alignment_strategy']
    if (set(spec)!={'strategy_scope','max_attempts','source_group'} or spec['strategy_scope']!='alignment_source_v1'
            or type(spec['max_attempts']) is not int or not 1<=spec['max_attempts']<=3 or spec['source_group']!=group):
        raise WorkflowError('TASK_STRATEGY_MISMATCH')
    digest=scope_sha(dict(target_id=ref['target_id'],effective_descriptor=descriptor,strategy_scope='alignment_source_v1',source_group=group))
    a=m['attempts'][0];expected={'index':1,'jobs':{},'local_derivation':True,'diagnoses':[],
        'attempt_id':m['run_id']+':attempt-01','strategy_sha256':digest,'correction':d.get('correction')}
    if a!=expected:raise WorkflowError('TASK_STRATEGY_MISMATCH')
    matches=[entry for entry in task['attempts'].values() if entry['run_id']==m['run_id'] and entry['attempt_id']==a['attempt_id']]
    if matches:
        entry=matches[0];strategy=task['strategies'][entry['strategy_sha256']]
        if (len(matches)!=1 or entry['strategy_sha256']!=digest or entry['source_attempt']!=source
                or entry['correction']!=a['correction'] or strategy['max_attempts']!=spec['max_attempts']
                or strategy['source_group']!=group or strategy['strategy_scope']!='alignment_source_v1'
                or strategy['effective_descriptor']!=descriptor):raise WorkflowError('TASK_STRATEGY_MISMATCH')
    elif not allow_setup_missing:raise WorkflowError('TASK_STRATEGY_MISMATCH')
    return task,bound,descriptor


def _recover_setup(path,m,anchors_file,output_root,alignment_max_attempts,correction):
    import generic_sprite_workflow as workflow
    ref=m['request']['task_ref'];task=ledger.inspect_task(ref['task_manifest'])
    attempt_id=m['run_id']+':attempt-01'
    if (m['run_id'] in task['runs'] and any(a['run_id']==m['run_id'] and a['attempt_id']==attempt_id for a in task['attempts'].values())):
        return None
    with run_lock(path.parent):
        m=read_json(path)
        ref=m['request']['task_ref']
        if output_root is not None and Path(output_root).expanduser().resolve()!=path.parent.parent:
            raise WorkflowError('WORKSPACE_NOT_ALLOWED')
        if _digest(Path(anchors_file).read_bytes())!=m['derivation']['files']['annotations.json']:
            raise WorkflowError('ALIGNMENT_ANNOTATIONS_INVALID')
        if (alignment_max_attempts!=m['derivation']['alignment_strategy']['max_attempts']
                or (correction is not None and correction!=m['derivation'].get('correction'))):
            raise WorkflowError('TASK_STRATEGY_COLLISION')
        if m['request'].get('alignment_policy')!=POLICY:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        proof=_verify_alignment(m,allow_setup_missing=True)
        if not isinstance(proof,dict):raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        parent=read_json(path.parent/'derivation/parent-manifest.json')
        _,bound,descriptor=_managed_alignment(m,parent,allow_setup_missing=True)
        ledger.check_history_context(ref['task_manifest'],ref['target_id'])
        ledger.bind_run(ref['task_manifest'],bound)
        digest=ledger.begin_attempt(ref['task_manifest'],m['run_id'],attempt_id,descriptor,
            m['derivation']['alignment_strategy']['max_attempts'],m['derivation'].get('correction'),
            source_attempt=m['derivation']['source_attempt'],strategy_scope='alignment_source_v1')
        if digest!=m['attempts'][0]['strategy_sha256']:raise WorkflowError('TASK_STRATEGY_MISMATCH')
        verify_alignment(m);atomic_json(path,m)
        return workflow._result(workflow.READY,path,m)


def align_manifest(manifest_path, anchors_file, *, output_root=None, alignment_max_attempts=2, correction=None):
    """Create a local v3 scope with no additional provider reservation."""
    import generic_sprite_workflow as workflow
    path=None;m=None
    try:
        candidate=read_json(Path(manifest_path).expanduser().resolve())
        if (candidate.get('derivation') and 'task_ref' in candidate.get('request',{})
                and 'alignment_strategy' in candidate['derivation']):
            recovered=_recover_setup(Path(manifest_path).resolve(),candidate,anchors_file,output_root,alignment_max_attempts,correction)
            if recovered is not None:return recovered
        native_evidence={};native_origin=None
        if native_source.is_native(candidate):
            captured,native_evidence,native_origin=native_source.capture(manifest_path)
            view=native_source.view(captured[1])
            preview_key='preview' if candidate['schema']=='native_articulated_workflow_v1' else 'preview_gif'
            preview_path=view['artifact_paths'][preview_key]
            records=json.loads(native_evidence['native-evidence.json'])
            matches=[r for r in records if r['path']==preview_path and r['sha256']==view['artifact_hashes'][preview_path]]
            if len(matches)!=1:raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
            parent_preview=native_evidence[matches[0]['snapshot']]
            if _digest(parent_preview)!=view['artifact_hashes'][preview_path]:raise WorkflowError('ALIGNMENT_PARENT_CHANGED')
        elif candidate.get('mode')=='whole_sheet':captured=_capture_parent(manifest_path)
        else:raise WorkflowError('ALIGNMENT_PARENT_UNSUPPORTED')
        parent_path,parent,parent_bytes,parent_scope_bytes,input_bytes,source_bytes=captured[:6]
        if not native_origin:parent_preview=captured[6]
        managed='task_ref' in parent['request']
        if managed:
            if type(alignment_max_attempts) is not int or not 1<=alignment_max_attempts<=3:raise WorkflowError('ATTEMPT_LIMIT_INVALID')
            task=workflow._task_state(parent_path,{**parent,'authorization_scope_sha256':native_source.view(parent)['authorization_scope_sha256']})
            if 'body_x_alignment' not in task['authorization']['allowed_operations']:raise WorkflowError('OPERATION_NOT_AUTHORIZED')
            ledger.check_history_context(parent['request']['task_ref']['task_manifest'],parent['request']['task_ref']['target_id'])
            source_attempt=dict(run_id=parent['run_id'],attempt_id=parent['attempts'][-1]['attempt_id'])
            source_group=ledger._alignment_source_group(task,source_attempt)
        parent_view=native_source.view(parent)
        annotation_path=Path(anchors_file).expanduser().resolve()
        try:
            annotations=read_json(annotation_path);annotation_bytes=annotation_path.read_bytes()
        except (OSError,ValueError):raise WorkflowError('ALIGNMENT_ANNOTATIONS_INVALID') from None
        frames=[_image(payload) for payload in input_bytes]
        points=_annotations(annotations,parent,frames,_image(source_bytes))
        aligned,report=align_body_x(frames,points)
        request={**native_source.request(parent),'alignment_policy':POLICY,'grounded':False,
                 'canvas':[aligned[0].width,aligned[0].height],'alignment_codec_version':3}
        output=Path(output_root).expanduser().resolve() if output_root else parent_path.parent.parent/'aligned-runs'
        _safe_output(output,parent_path.parent)
        run_id=uuid.uuid4().hex;root=output/run_id;_safe_output(root,parent_path.parent)
        if root.exists():raise WorkflowError('OUTPUT_CONFLICT')
        payloads,qa=_artifacts(aligned,request,report,parent_preview=parent_preview)
        proof={'schema_version':1,**report,'parent_artifact_set_sha256':workflow._review_target(native_source.view(parent)),
            'parent_manifest_sha256':_digest(parent_bytes),'source_sha256':_digest(source_bytes),
            'annotations_sha256':_digest(annotation_bytes),'input_sha256':[_digest(p) for p in input_bytes],
            'output_sha256':{name:_digest(p) for name,p in payloads.items()},
            'frame_ms':request['frame_ms'],'loop':request['loop'],'pixel_art':request['pixel_art']}
        if 'alignment_codec_version' in request:proof['alignment_codec_version']=request['alignment_codec_version']
        if request.get('alignment_codec_version')==3:
            proof.update(parent_gif_sha256=_digest(parent_preview),gif_mode='translated_parent_exact_v1')
        snapshots={'parent-manifest.json':parent_bytes,
            'parent-scope.json':parent_scope_bytes,
            'annotations.json':annotation_bytes,'proof.json':encoded(proof),**native_evidence,
            'source.png':source_bytes,**{f'input-{i:02d}.png':p for i,p in enumerate(input_bytes,1)}}
        if request.get('alignment_codec_version')==3:snapshots['parent-preview.gif']=parent_preview
        derivation={'schema_version':1,'kind':'body_x_alignment','parent_run_id':parent['run_id'],
            'parent_manifest':str(parent_path),'parent_scope_sha256':parent_view['authorization_scope_sha256'],
            'parent_artifact_set_sha256':workflow._review_target(native_source.view(parent)),
            'parent_reserved_cost_usd':0 if native_origin else workflow._result(manifest=parent)['spent_estimate_usd'],
            'proof_sha256':_digest(snapshots['proof.json']),
            'files':{name:_digest(p) for name,p in snapshots.items()}}
        if native_origin:derivation['native_origin']=native_origin
        if managed:
            derivation.update(source_attempt=source_attempt,alignment_strategy=dict(strategy_scope='alignment_source_v1',
                max_attempts=alignment_max_attempts,source_group=source_group),correction=correction)
        request['derivation_sha256']=scope_sha(derivation)
        authorization={'source':'session_local_derivation','request_authorized':True,'max_cost_usd':0.0,'max_generation_attempts':1}
        source={'path':str(root/'source.png'),'sha256':_digest(source_bytes),'original_path':parent['source']['path']}
        scope={'run_id':run_id,'source':source,'request':request,'authorization':authorization,'derivation':derivation}
        artifacts=root/'attempt-01/artifacts'
        paths={'sheet':str(artifacts/'sprite-sheet.png'),'preview_gif':str(artifacts/'preview.gif'),
               'contact':str(artifacts/'contact.png'),'qa_report':str(artifacts/'qa.json'),
               'frames':[str(artifacts/f'frame-{i:02d}.png') for i in range(1,len(frames)+1)]}
        m={'schema_version':3,'mode':'whole_sheet',**scope,'authorization_scope_sha256':scope_sha(scope),
            'attempts':[{'index':1,'jobs':{},'local_derivation':True,'diagnoses':[]}],
            'state':workflow.READY,'artifact_paths':paths,
            'artifact_hashes':{str(artifacts/name):_digest(p) for name,p in payloads.items()},
            'consistency':qa['consistency'],'estimates':{'type':'local_derivation_zero_additional_reservation','provider_billing_guarantee':False}}
        if managed:
            descriptor=dict(route='body_x_alignment',model=POLICY,conditioning='local',inputs=[dict(role='source',sha256=source['sha256'])])
            digest=scope_sha(dict(target_id=request['task_ref']['target_id'],effective_descriptor=descriptor,
                strategy_scope='alignment_source_v1',source_group=source_group))
            m['attempts'][0].update(attempt_id=run_id+':attempt-01',strategy_sha256=digest,correction=correction)
        root.mkdir(parents=True,exist_ok=False);path=root/'run-manifest.json'
        with run_lock(root):
            (root/'derivation').mkdir();artifacts.mkdir(parents=True)
            for name,payload in snapshots.items():new_or_identical(root/'derivation'/name,payload)
            new_or_identical(root/'source.png',source_bytes)
            for name,payload in payloads.items():new_or_identical(artifacts/name,payload)
            new_or_identical(root/'scope.json',encoded(scope));new_or_identical(root/'scope.sha256',(scope_sha(scope)+'\n').encode())
            if managed:
                atomic_json(path,m)
                ref=request['task_ref']
                ledger.bind_run(ref['task_manifest'],dict(run_id=run_id,target_id=ref['target_id'],scope_sha256=scope_sha(scope),
                    manifest_path=str(path),parent_run_id=parent['run_id']))
                actual=ledger.begin_attempt(ref['task_manifest'],run_id,m['attempts'][0]['attempt_id'],descriptor,
                    alignment_max_attempts,correction,source_attempt=source_attempt,strategy_scope='alignment_source_v1')
                if actual!=digest:raise WorkflowError('TASK_STRATEGY_MISMATCH')
            verify_alignment(m);atomic_json(path,m)
        return workflow._result(workflow.READY,path,m)
    except (OSError,ValueError,KeyError,TypeError) as error:
        return workflow._result(path=path,manifest=m,reason=getattr(error,'code','ALIGNMENT_FAILED'))


def verify_alignment(manifest):
    return _verify_alignment(manifest)


def _verify_alignment(manifest, *, allow_setup_missing=False):
    """Replay immutable input snapshots and encoded artifacts, raising stable codes."""
    import generic_sprite_workflow as workflow
    if 'alignment_policy' not in manifest.get('request',{}):return None
    policy=manifest['request']['alignment_policy']
    if policy!=POLICY:raise WorkflowError('ALIGNMENT_POLICY_UNSUPPORTED')
    if not manifest.get('derivation'):raise WorkflowError('ALIGNMENT_REQUIRED')
    try:
        m=manifest;d=m['derivation'];root=Path(m['source']['path']).parent
        if m.get('mode')!='whole_sheet' or m.get('schema_version')!=3:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        scope=read_json(root/'scope.json')
        if (any(scope.get(k)!=m.get(k) for k in ('run_id','source','request','authorization','derivation'))
                or Path(m['source']['path'])!=root/'source.png'
                or scope_sha(scope)!=m['authorization_scope_sha256']
                or (root/'scope.sha256').read_text().strip()!=scope_sha(scope)
                or m['request'].get('derivation_sha256')!=scope_sha(d)):
            raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        count=m['request']['frames']
        required={'parent-manifest.json','parent-scope.json','annotations.json','proof.json','source.png',
                  *{f'input-{i:02d}.png' for i in range(1,count+1)}}
        if m['request'].get('alignment_codec_version')==3:required.add('parent-preview.gif')
        if 'native_origin' in d:required.update(n for n in d['files'] if n.startswith('native-'))
        if set(d['files'])!=required:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        for name,digest in d['files'].items():
            if sha_file(root/'derivation'/name)!=digest:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        if d['proof_sha256']!=d['files']['proof.json']:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        parent=read_json(root/'derivation/parent-manifest.json');parent_scope=read_json(root/'derivation/parent-scope.json')
        if native_source.is_native(parent):
            native_source.verify_origin(d,parent,parent_scope,root)
            if (parent['run_id']!=d['parent_run_id'] or parent['scope_sha256']!=d['parent_scope_sha256']
                    or workflow._review_target(native_source.view(parent))!=d['parent_artifact_set_sha256']
                    or d['parent_reserved_cost_usd']!=0):raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        elif 'native_origin' in d:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        elif (parent.get('mode')!='whole_sheet' or parent.get('schema_version')!=3
                or scope_sha(parent_scope)!=d['parent_scope_sha256']
                or parent['authorization_scope_sha256']!=d['parent_scope_sha256']
                or any(parent.get(k)!=parent_scope.get(k) for k in ('run_id','request','source','authorization','derivation'))
                or parent['run_id']!=d['parent_run_id']
                or workflow._review_target(parent)!=d['parent_artifact_set_sha256']
                or workflow._result(manifest=parent)['spent_estimate_usd']!=d['parent_reserved_cost_usd']):
            raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        if parent.get('request',{}).get('route')=='endpoint_cycle_v1' and not parent.get('derivation'):
            from endpoint_cycle_contract import validate_endpoint_attempt
            live_path,live,_=validate_endpoint_attempt(d['parent_manifest'],require_artifacts=True)
            workflow._verify_visual_state(live)
            if (live['run_id']!=parent['run_id'] or workflow._review_target(live)!=d['parent_artifact_set_sha256']
                    or live['source']!=parent['source'] or live['request']!=parent['request']
                    or live['authorization_scope_sha256']!=d['parent_scope_sha256']
                    or live.get('visual_review')!=parent.get('visual_review') or live['state']!=parent['state']):
                raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        managed='task_ref' in m['request']
        if managed:_managed_alignment(m,parent,allow_setup_missing=allow_setup_missing)
        if (m['authorization']!={'source':'session_local_derivation','request_authorized':True,'max_cost_usd':0.0,'max_generation_attempts':1}
                or len(m['attempts'])!=1 or (not managed and m['attempts'][0]!={'index':1,'jobs':{},'local_derivation':True,'diagnoses':[]})
                or m['request']['grounded'] is not False):raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        if sha_file(root/'source.png')!=d['files']['source.png'] or parent['source']['sha256']!=d['files']['source.png']:
            raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        frames=[]
        parent_view=native_source.view(parent)
        for i,p in enumerate(parent_view['artifact_paths']['frames'],1):
            if parent_view['artifact_hashes'][p]!=d['files'][f'input-{i:02d}.png']:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
            frames.append(_image((root/'derivation'/f'input-{i:02d}.png').read_bytes()))
        annotations=read_json(root/'derivation/annotations.json')
        points=_annotations(annotations,parent,frames,_image((root/'source.png').read_bytes()))
        output,report=align_body_x(frames,points)
        expected_request={**native_source.request(parent),'alignment_policy':POLICY,'grounded':False,
            'canvas':[output[0].width,output[0].height],'derivation_sha256':scope_sha(d)}
        expected_request.pop('alignment_codec_version',None)
        if 'alignment_codec_version' in m['request']:
            expected_request['alignment_codec_version']=m['request']['alignment_codec_version']
        if m['request']!=expected_request:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        parent_preview=None
        if m['request'].get('alignment_codec_version')==3:
            preview_key='preview' if parent.get('schema')=='native_articulated_workflow_v1' else 'preview_gif'
            preview_path=parent_view['artifact_paths'][preview_key]
            parent_preview=(root/'derivation/parent-preview.gif').read_bytes()
            if _digest(parent_preview)!=parent_view['artifact_hashes'][preview_path]:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        payloads,_=_artifacts(output,m['request'],report,parent_preview=parent_preview)
        expected={'schema_version':1,**report,'parent_artifact_set_sha256':d['parent_artifact_set_sha256'],
            'parent_manifest_sha256':d['files']['parent-manifest.json'],'source_sha256':d['files']['source.png'],
            'annotations_sha256':d['files']['annotations.json'],
            'input_sha256':[d['files'][f'input-{i:02d}.png'] for i in range(1,count+1)],
            'output_sha256':{name:_digest(p) for name,p in payloads.items()},
            'frame_ms':m['request']['frame_ms'],'loop':m['request']['loop'],'pixel_art':m['request']['pixel_art']}
        if 'alignment_codec_version' in m['request']:expected['alignment_codec_version']=m['request']['alignment_codec_version']
        if m['request'].get('alignment_codec_version')==3:
            expected.update(parent_gif_sha256=_digest(parent_preview),gif_mode='translated_parent_exact_v1')
        if read_json(root/'derivation/proof.json')!=expected:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        workflow._verify_artifacts(m)
        for name,payload in payloads.items():
            if (root/'attempt-01/artifacts'/name).read_bytes()!=payload:raise WorkflowError('ALIGNMENT_PROOF_INVALID')
        return expected
    except WorkflowError as error:
        if error.code=='ARTIFACT_TAMPERED':raise
        raise WorkflowError('ALIGNMENT_PROOF_INVALID') from None
    except (OSError,ValueError,KeyError,TypeError,IndexError):
        raise WorkflowError('ALIGNMENT_PROOF_INVALID') from None


def validate_body_review(manifest, report):
    """Explicit anatomical judgment complements the replayable numeric proof."""
    proof=verify_alignment(manifest)
    if proof is None:return
    try:
        body=report['body_alignment']
        annotation=read_json(Path(manifest['source']['path']).parent/'derivation/annotations.json')
        def text(value):return isinstance(value,str) and 10<=len(value.strip())<=4000
        findings=body['frame_findings']
        if (body['proof_sha256']!=manifest['derivation']['proof_sha256']
                or body['reference_name']!=annotation['reference']['name'] or not text(body['notes'])
                or not isinstance(findings,list) or len(findings)!=manifest['request']['frames']):
            raise WorkflowError('BODY_ALIGNMENT_REVIEW_INVALID')
        seen=set()
        for finding in findings:
            if (not isinstance(finding,dict) or type(finding.get('frame')) is not int or not 1<=finding['frame']<=manifest['request']['frames']
                    or finding['frame'] in seen or not text(finding.get('notes'))
                    or any(finding.get(k) is not True for k in ('homologous_body_point','fixed_x','preserved_y','excludes_weapon_tail_box'))):
                raise WorkflowError('BODY_ALIGNMENT_REVIEW_INVALID')
            seen.add(finding['frame'])
    except (KeyError,TypeError,ValueError):raise WorkflowError('BODY_ALIGNMENT_REVIEW_INVALID') from None


def export_provenance(manifest, destination):
    """Return collision-safe additional payloads after verification."""
    verify_alignment(manifest)
    root=Path(manifest['source']['path']).parent
    return {destination/'alignment'/name:(root/'derivation'/name).read_bytes()
            for name in manifest['derivation']['files']}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    align=sub.add_parser('align');align.add_argument('--run-manifest',required=True)
    align.add_argument('--anchors-file',required=True);align.add_argument('--output-root')
    align.add_argument('--alignment-max-attempts',type=int,default=2);align.add_argument('--correction-file')
    args=parser.parse_args(argv)
    result=align_manifest(args.run_manifest,args.anchors_file,output_root=args.output_root,
        alignment_max_attempts=args.alignment_max_attempts,correction=read_json(args.correction_file) if args.correction_file else None)
    print(json.dumps(result,ensure_ascii=False));return 0 if result['state']=='READY_FOR_VISUAL_REVIEW' else 2


if __name__=='__main__':raise SystemExit(main())
