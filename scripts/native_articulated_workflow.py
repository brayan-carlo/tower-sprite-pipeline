"""Reviewed local biped composition lifecycle. No provider calls or automatic art approval.

Inputs are imported as verified snapshots. A revision starts atomically only once
its snapshot exists; its output cannot inherit the prior attempt's approval.
"""
import argparse
import copy
import hashlib
import io
import json
import uuid
from pathlib import Path

from PIL import Image
import native_articulation as articulation
import native_exports
from native_articulated_review import validate_review, validate_source_geometry
from native_components import analyze_components
from generic_sprite_workflow import _review_target
from sheet_images import _png_bytes, _gif_bytes, _contact_image, consistency_diagnostics
from sheet_workflow_state import (WorkflowError, atomic_json, encoded, new_or_identical,
                                 read_json, run_lock, scope_sha)

SCHEMA = 'native_articulated_workflow_v1'
READY, PASS, REJECT = 'READY_FOR_VISUAL_REVIEW', 'READY_FOR_DELIVERY', 'VISUAL_REJECTED'
ALIGNMENT = 'READY_FOR_ALIGNMENT'
BUILDING, TECHNICAL, INTEGRITY = 'ASSEMBLING', 'TECHNICAL_REJECTED', 'INTEGRITY_REJECTED'
FILES = {'sheet':'sprite-sheet.png', 'preview':'preview.gif', 'contact':'contact.png',
         'components':'components.json', 'qa':'qa.json', 'provenance':'provenance.json',
         'source_reference':'source-reference.png', 'source_geometry':'source-geometry.json',
         'render_report':'render-report.json'}


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _outside_skill(value):
    path = native_exports.safe_path(value)
    for root in (Path(__file__).resolve().parents[1], Path.home()/'.codex/skills'):
        if path == root or root in path.parents:
            raise WorkflowError('WORKSPACE_NOT_ALLOWED')
    return path


def _inside(root, value):
    path = native_exports.safe_path(value)
    if root not in path.parents:
        raise WorkflowError('STATE_PATH_INVALID')
    return path


def _bytes(path, limit=256*1024*1024):
    path = native_exports.safe_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise WorkflowError('FILE_INVALID_OR_LIMIT')
    with path.open('rb') as stream:
        payload = stream.read(limit+1)
    if len(payload) > limit:
        raise WorkflowError('FILE_INVALID_OR_LIMIT')
    return payload


def _checked(path, expected, limit=256*1024*1024):
    payload = _bytes(path, limit)
    if _hash(payload) != expected:
        raise WorkflowError('HASH_MISMATCH')
    return payload


def _json(payload):
    try:
        return json.loads(payload.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise WorkflowError('JSON_INVALID') from exc


def _image(payload):
    try:
        with Image.open(io.BytesIO(payload)) as im:
            if im.format != 'PNG' or im.mode != 'RGBA' or getattr(im,'n_frames',1) != 1:
                raise WorkflowError('RGBA_PNG_REQUIRED')
            im.load()
            return im.copy()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise WorkflowError('IMAGE_INVALID') from exc


def _references(data):
    try:
        native, assets = data['native_inputs'], data['assets']
        if not isinstance(native,list) or not 1 <= len(native) <= 8:
            raise WorkflowError('NATIVE_PROVENANCE_REQUIRED')
        if not isinstance(assets,dict) or not 1 <= len(assets) <= 32:
            raise WorkflowError('ASSETS_INVALID')
        return [data['source'], *native, *assets.values()]
    except (KeyError, TypeError) as exc:
        raise WorkflowError('RECIPE_INVALID') from exc


def _request(data, alignment_policy=None):
    attack = data['action'] == 'attack'
    return {**({'alignment_policy':alignment_policy} if alignment_policy is not None else {}), 'profile':'articulated_biped_planted_attack' if attack else 'articulated_biped_walk', 'action':data['action'],
            'orientation':data['orientation'], 'frame_ms':data['frame_ms'],
            'canvas':data['canvas'], 'frames':8, 'loop':not attack}


def _preview_bytes(frames, request):
    return _gif_bytes(frames, request['frame_ms'], request['loop'],
                      binary_alpha=request['action']=='attack')


def _collect(recipe_path, alignment_policy=None):
    """Read/hash each declared input once, before writing any snapshot or state."""
    path = native_exports.safe_path(recipe_path)
    payload = _bytes(path, 2*1024*1024)
    data = _json(payload)
    if not isinstance(data,dict) or data.get('schema') != articulation.SCHEMA:
        raise WorkflowError('RECIPE_INVALID')
    inputs = {}
    try:
        for record in _references(data):
            content = articulation._input(path.parent,record)
            target = Path(record['path'])
            if target in inputs and inputs[target] != content:
                raise WorkflowError('INPUT_CONFLICT')
            inputs[target] = content
        source = articulation._load_asset(inputs[Path(data['source']['path'])])
        request = _request(data,alignment_policy)
    except (articulation.ArticulationError, KeyError, TypeError) as exc:
        raise WorkflowError(str(exc) if isinstance(exc,articulation.ArticulationError) else 'RECIPE_INVALID') from exc
    return {'path':path,'bytes':payload,'data':data,'inputs':inputs,'source':source,'request':request}


def _snapshot(root, collected):
    folder = root/'imports'/uuid.uuid4().hex
    folder.mkdir(parents=True)
    hashes = {}
    for relative,payload in collected['inputs'].items():
        path = _inside(folder,folder/relative)
        path.parent.mkdir(parents=True,exist_ok=True)
        new_or_identical(path,payload)
        hashes[str(path)] = _hash(payload)
    recipe = folder/collected['path'].name
    new_or_identical(recipe,collected['bytes'])
    return {'recipe_path':str(recipe),'recipe_sha256':_hash(collected['bytes']),
            'input_hashes':hashes,'observed_recipe_path':str(collected['path'])}


def _scope(m):
    return {key:m[key] for key in ('request','source','source_geometry')}


def _context(m, number=None):
    number = len(m['attempts']) if number is None else number
    a = m['attempts'][number-1]
    return {'run_id':m['run_id'],'attempts':m['attempts'][:number], 'request':m['request'],
            'source_sha256':m['source']['sha256'],'artifact_hashes':a['artifact_hashes'],
            'consistency':a['consistency']}


def _paths(folder):
    return {'frames':[str(folder/f'frame-{i:02}.png') for i in range(1,9)],
            **{key:str(folder/name) for key,name in FILES.items()}}


def _flat(paths):
    return [p for value in paths.values() for p in (value if isinstance(value,list) else [value])]


def _provenance(m,a,render_report):
    return {'schema':SCHEMA,'generation_claimed':False,
            'construction':'Observed native/source inputs plus local layer articulation.',
            'source_sha256':m['source']['sha256'],
            'recipe_sha256':a['snapshot']['recipe_sha256'],
            'native_inputs':render_report['native_inputs'],
            'asset_sha256':render_report['asset_sha256']}


def _delivery_receipt(path,m,a,source_size,review):
    native_exports.require_direct_delivery(m)
    pivot=review['geometry']['pivot']; w,h=m['request']['canvas']
    return {'schema':SCHEMA,'state':PASS,'delivery_allowed':True,
            'run_manifest':str(path),'artifact_set_sha256':_review_target(_context(m,a['number'])),
            'request':m['request'],'source_sha256':m['source']['sha256'],
            'recipe_sha256':a['snapshot']['recipe_sha256'],'generation_claimed':False,
            'geometry':{'canvas':[w,h],'source_canvas':list(source_size),
                        'source_pivot':m['source_geometry']['pivot'],'pivot_top_left_px':pivot,
                        'pivot_bottom_left_normalized':[pivot[0]/w,1-pivot[1]/h],'scale':1},
            'user_acceptance':'pending','unity_validation':'not_run'}


def _revoked_receipts(record):
    delivery=_json(record['receipts']['delivery.json'].encode('utf-8'))
    delivery.update(state=REJECT,delivery_allowed=False,revoked=True,
                    revocation_reason='approval_withdrawn')
    return {'delivery.json':delivery,
            'visual-review.json':{'decision':'reject','revoked':True,'delivery_allowed':False,
                'artifact_set_sha256':record['artifact_set_sha256'],
                'revocation_reason':'approval_withdrawn',
                'previous_review':_json(record['receipts']['visual-review.json'].encode('utf-8'))}}


def _export_review(record,a):
    review=_json(record['receipts']['visual-review.json'].encode('utf-8'))
    if review.get('decision')!='pass' or review not in a.get('review_history',[]):
        raise WorkflowError('EXPORT_STATE_INVALID')
    return review


def _write_artifacts(path, m, frames, render_report):
    a = m['attempts'][-1]
    folder = path.parent/f'attempt-{a["number"]:02d}'/('output-'+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    paths = _paths(folder)
    source_payload = _checked(m['source']['path'],m['source']['sha256'])
    source = _image(source_payload)
    components = [analyze_components(im) for im in frames]
    consistency = consistency_diagnostics(frames)
    sheet = Image.new('RGBA',(frames[0].width*8,frames[0].height))
    payloads = {}
    for i,im in enumerate(frames):
        sheet.paste(im,(i*im.width,0))
        payloads[paths['frames'][i]] = _png_bytes(im)
    qa = {'schema':SCHEMA,'state':READY,'delivery_allowed':False,'frame_count':8,
          'canvas':list(frames[0].size),'frame_ms':m['request']['frame_ms'],'loop':m['request']['loop'],
          'consistency':consistency, 'semantic_review_required':True}
    provenance = _provenance(m,a,render_report)
    payloads.update({paths['sheet']:_png_bytes(sheet),
        paths['preview']:_preview_bytes(frames,m['request']),
        paths['contact']:_png_bytes(_contact_image(frames,pixel_art=True)),
        paths['components']:encoded(components), paths['qa']:encoded(qa),
        paths['provenance']:encoded(provenance), paths['source_reference']:_png_bytes(source),
        paths['source_geometry']:encoded(m['source_geometry']), paths['render_report']:encoded(render_report)})
    for target,payload in payloads.items():
        new_or_identical(target,payload)
    a.update(output_directory=str(folder),artifact_paths=paths,
             artifact_hashes={p:_hash(data) for p,data in payloads.items()},
             consistency=consistency,frame_pixel_sha256=render_report['frame_pixel_sha256'],status='BUILT')
    a.pop('block_reason',None)
    m['state'] = READY


def _render(snapshot):
    try:
        frames,report = articulation.render_recipe(snapshot['recipe_path'])
    except (articulation.ArticulationError,OSError) as exc:
        raise WorkflowError(str(exc) if isinstance(exc,articulation.ArticulationError) else 'RECIPE_UNAVAILABLE') from exc
    if report['recipe_sha256'] != snapshot['recipe_sha256']:
        raise WorkflowError('HASH_MISMATCH')
    return frames,report


def _finish(path,m):
    a = m['attempts'][-1]
    try:
        frames,report = _render(a['snapshot'])
        if any(old.get('visual_review',{}).get('decision')=='reject'
               and old.get('frame_pixel_sha256')==report['frame_pixel_sha256'] for old in m['attempts'][:-1]):
            raise WorkflowError('UNCHANGED_REJECTED_RESULT')
        _write_artifacts(path,m,frames,report)
    except (WorkflowError,OSError,ValueError) as exc:
        a['status'] = 'REJECTED'
        a['block_reason'] = getattr(exc,'code','LOCAL_ASSEMBLY_FAILED')
        m['state'] = TECHNICAL
    atomic_json(path,m)


def build(recipe_path, source_geometry, workspace, *, task_manifest=None, target_id=None, max_attempts=2):
    import native_alignment_source as managed
    if type(max_attempts) is not int or not 1<=max_attempts<=3:raise WorkflowError('ATTEMPT_LIMIT_INVALID')
    collected=_collect(recipe_path,'body_x_preserve_y_v1')
    request={**collected['request'],'pixel_art':True,'task_max_attempts':max_attempts}
    request['task_ref']=managed.task_ref(task_manifest,request,collected['data']['source']['sha256'],target_id)
    return _build(recipe_path,source_geometry,workspace,alignment_policy='body_x_preserve_y_v1',managed_request=request)


def _build(recipe_path, source_geometry, workspace, *, alignment_policy=None, managed_request=None):
    # Internal policy-absent fixture compatibility; public build always seals policy.
    collected = _collect(recipe_path,alignment_policy)
    if managed_request is not None:collected['request']=managed_request
    geometry = copy.deepcopy(source_geometry)
    validate_source_geometry(collected['source'],collected['data']['source']['sha256'],geometry)
    directory = _outside_skill(workspace)/('articulated-'+uuid.uuid4().hex)
    directory.mkdir(parents=True)
    path = directory/'run-manifest.json'
    with run_lock(directory):
        source_path = directory/'source.png'
        source_record = collected['data']['source']
        new_or_identical(source_path,collected['inputs'][Path(source_record['path'])])
        snapshot = _snapshot(directory,collected)
        m = {'schema':SCHEMA,'run_id':directory.name,'state':BUILDING,
             'request':collected['request'],'source_geometry':geometry,
             'source':{'path':str(source_path),'sha256':source_record['sha256'],
                       'original_path':source_record.get('original_path',str(collected['path'].parent/source_record['path']))},
             'attempts':[{'number':1,'snapshot':snapshot,'status':'ASSEMBLING','generation_claimed':False}],
             'exports':[]}
        m['scope_sha256'] = scope_sha(_scope(m))
        if 'task_ref' in m['request']:
            import native_alignment_source as managed
            m['attempts'][0].update(managed.native_attempt(m,1))
            managed.seal_native(path,m)
        atomic_json(path,m)
        if 'task_ref' in m['request']:managed.native_task_state(path,m,recover=True)
        _finish(path,m)
        return _result(path,m)


def _load(path, *, recover_task=False):
    path = _outside_skill(path)
    m = read_json(path)
    requires_alignment=native_exports.alignment_required(m)
    if requires_alignment and m.get('state')==PASS:raise WorkflowError('ALIGNMENT_REQUIRED')
    try:
        if (path.name != 'run-manifest.json' or m['schema'] != SCHEMA or m['run_id'] != path.parent.name
                or m['scope_sha256'] != scope_sha(_scope(m))
                or m['state'] not in (BUILDING,TECHNICAL,INTEGRITY,READY,PASS,REJECT,ALIGNMENT)
                or not isinstance(m['attempts'],list) or not m['attempts']
                or [a['number'] for a in m['attempts']] != list(range(1,len(m['attempts'])+1))):
            raise WorkflowError('STATE_INVALID')
        if native_exports.safe_path(m['source']['path']) != path.parent/'source.png':
            raise WorkflowError('STATE_PATH_INVALID')
        for a in m['attempts']:
            snapshot = a['snapshot']
            recipe = _inside(path.parent/'imports',snapshot['recipe_path'])
            for input_path in snapshot['input_hashes']:
                _inside(recipe.parent,input_path)
            if 'artifact_paths' in a:
                output = _inside(path.parent/f'attempt-{a["number"]:02d}',a['output_directory'])
                for artifact in _flat(a['artifact_paths']): _inside(output,artifact)
        _validate_exports(path,m)
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,WorkflowError): raise
        raise WorkflowError('STATE_INVALID') from exc
    import native_alignment_source as managed
    managed.native_task_state(path,m,recover=recover_task)
    return path,m


def _validate_exports(path,m):
    """Validate ownership context before any automatic receipt revocation writes."""
    if not isinstance(m.get('exports',[]),list): raise WorkflowError('EXPORT_STATE_INVALID')
    destinations=set()
    for record in m.get('exports',[]):
        destination=_outside_skill(record['destination'])
        if destination==path.parent or path.parent in destination.parents or str(destination) in destinations:
            raise WorkflowError('EXPORT_STATE_INVALID')
        destinations.add(str(destination))
        number=record['attempt']
        if type(number) is not int or not 1<=number<=len(m['attempts']): raise WorkflowError('EXPORT_STATE_INVALID')
        a=m['attempts'][number-1]; context=_context(m,number)
        if record['artifact_set_sha256']!=_review_target(context): raise WorkflowError('EXPORT_STATE_INVALID')
        if record['status'] not in ('EXPORT_PENDING','EXPORTED','REVOCATION_PENDING','REVOKED'):
            raise WorkflowError('EXPORT_STATE_INVALID')
        if set(record['receipts'])!={'delivery.json','visual-review.json'}: raise WorkflowError('EXPORT_STATE_INVALID')
        _export_review(record,a)
        expected={Path(p).name:v for p,v in a['artifact_hashes'].items()}
        expected.update({name:_hash(value.encode('utf-8')) for name,value in record['receipts'].items()})
        if record['hashes']!=expected: raise WorkflowError('EXPORT_STATE_INVALID')
        revoked=record.get('revoked_receipts')
        if record['status'] in ('REVOCATION_PENDING','REVOKED'):
            if revoked!=_revoked_receipts(record): raise WorkflowError('EXPORT_STATE_INVALID')
        elif revoked is not None:
            raise WorkflowError('EXPORT_STATE_INVALID')
        for name in ('delivery.json','visual-review.json'):
            target=native_exports.safe_path(destination/name)
            if target.parent!=destination: raise WorkflowError('EXPORT_STATE_INVALID')


def _validate_export_semantics(path,m,source_size):
    for record in m.get('exports',[]):
        a=m['attempts'][record['attempt']-1]
        review=_export_review(record,a)
        expected=_delivery_receipt(path,m,a,source_size,review)
        if record['receipts']['delivery.json']!=encoded(expected).decode('utf-8'):
            raise WorkflowError('EXPORT_STATE_INVALID')


def _verify_completed_exports(m):
    for record in m.get('exports',[]):
        if record['status']!='EXPORTED': continue
        destination=native_exports.safe_path(record['destination'])
        for name,digest in record['hashes'].items():
            target=native_exports.safe_path(destination/name)
            if target.parent!=destination or not target.is_file() or _hash(_bytes(target))!=digest:
                raise WorkflowError('OUTPUT_CONFLICT')


def _verify_snapshot(m,a):
    snapshot=a['snapshot']
    recipe=Path(snapshot['recipe_path'])
    data=_json(_checked(recipe,snapshot['recipe_sha256'],2*1024*1024))
    expected=_request(data,m['request'].get('alignment_policy'))
    if 'task_ref' in m['request']:
        expected.update({key:m['request'][key] for key in ('task_ref','task_max_attempts','pixel_art')})
    if expected!=m['request'] or data['source']['sha256']!=m['source']['sha256']:
        raise WorkflowError('IMMUTABLE_SCOPE_CHANGED')
    expected={str(recipe.parent/record['path']):record['sha256'] for record in _references(data)}
    if expected!=snapshot['input_hashes']: raise WorkflowError('INPUT_SET_INVALID')
    for input_path,value in expected.items(): _checked(input_path,value,64*1024*1024)


def _verify_attempt(m,a,source):
    _verify_snapshot(m,a)
    if a['status']!='BUILT': return None
    paths=a['artifact_paths']; expected=_paths(Path(a['output_directory']))
    if paths!=expected or set(a['artifact_hashes'])!=set(_flat(expected)):
        raise WorkflowError('ARTIFACT_LAYOUT_INVALID')
    payloads={p:_checked(p,value) for p,value in a['artifact_hashes'].items()}
    frames=[_image(payloads[p]) for p in paths['frames']]
    rendered,report=_render(a['snapshot'])
    if (len(frames)!=8 or report['frame_pixel_sha256']!=a['frame_pixel_sha256']
            or any(im.size!=tuple(m['request']['canvas']) for im in frames)
            or any(x.tobytes()!=y.tobytes() for x,y in zip(frames,rendered))):
        raise WorkflowError('FRAME_RECIPE_MISMATCH')
    if _json(payloads[paths['render_report']])!=report: raise WorkflowError('RENDER_REPORT_INVALID')
    sheet=_image(payloads[paths['sheet']]); w,h=frames[0].size
    if sheet.size!=(w*8,h) or any(sheet.crop((i*w,0,(i+1)*w,h)).tobytes()!=im.tobytes() for i,im in enumerate(frames)):
        raise WorkflowError('SHEET_FRAME_MISMATCH')
    if payloads[paths['preview']]!=_preview_bytes(frames,m['request']):
        raise WorkflowError('PREVIEW_FRAME_MISMATCH')
    with Image.open(io.BytesIO(payloads[paths['preview']])) as gif:
        timing=[]
        for i in range(gif.n_frames):
            gif.seek(i); timing.append(gif.info.get('duration'))
        loop_valid = gif.info.get('loop')==0 if m['request']['loop'] else 'loop' not in gif.info
        if timing!=[m['request']['frame_ms']]*8 or not loop_valid:
            raise WorkflowError('PREVIEW_TIMING_INVALID')
    reference=_image(payloads[paths['source_reference']])
    if reference.size!=source.size or reference.tobytes()!=source.tobytes():
        raise WorkflowError('SOURCE_REFERENCE_CHANGED')
    contact=_image(payloads[paths['contact']]); expected_contact=_contact_image(frames,pixel_art=True)
    if contact.size!=expected_contact.size or contact.tobytes()!=expected_contact.tobytes():
        raise WorkflowError('CONTACT_FRAME_MISMATCH')
    if _json(payloads[paths['provenance']])!=_provenance(m,a,report):
        raise WorkflowError('PROVENANCE_INVALID')
    if _json(payloads[paths['source_geometry']])!=m['source_geometry']:
        raise WorkflowError('SOURCE_GEOMETRY_CHANGED')
    consistency=consistency_diagnostics(frames)
    if consistency!=a['consistency'] or _json(payloads[paths['qa']])['consistency']!=consistency:
        raise WorkflowError('CONSISTENCY_INVALID')
    components=_json(payloads[paths['components']])
    if components!=[analyze_components(im) for im in frames]: raise WorkflowError('COMPONENT_REPORT_INVALID')
    for review_report in a.get('review_history',[]):
        validate_review(_context(m,a['number']),review_report,source,m['source_geometry'],frames,components)
    if 'visual_review' in a and (not a.get('review_history') or a['visual_review']!=a['review_history'][-1]):
        raise WorkflowError('VISUAL_REVIEW_INVALID')
    if (native_exports.alignment_required(m) and a.get('visual_review',{}).get('decision')=='pass'
            and any(report.get('decision')=='reject' for report in a.get('review_history',[]))):
        raise WorkflowError('REJECTED_ARTIFACTS_REQUIRE_REVISION')
    return {'frames':frames,'components':components,'payloads':payloads}


def _verify(path,m,verify_exports=True, *, mutate_on_error=True):
    """Return buffers used for review/export; never re-read them to assign new hashes."""
    try:
        if verify_exports: _verify_completed_exports(m)
        source_bytes=_checked(m['source']['path'],m['source']['sha256'],64*1024*1024)
        source=_image(source_bytes)
        validate_source_geometry(source,m['source']['sha256'],m['source_geometry'])
        current=None
        for a in m['attempts']: current=_verify_attempt(m,a,source)
        latest=m['attempts'][-1]; decision=latest.get('visual_review',{}).get('decision')
        if m['state'] in (READY,PASS,REJECT,ALIGNMENT):
            required={READY:None,PASS:'pass',REJECT:'reject',ALIGNMENT:'pass'}[m['state']]
            if (m['state']==ALIGNMENT)!=bool(native_exports.alignment_required(m) and decision=='pass'):
                raise WorkflowError('VISUAL_REVIEW_INVALID')
            if latest['status']!='BUILT' or decision!=required:
                raise WorkflowError('VISUAL_REVIEW_INVALID')
        elif m['state']==BUILDING and latest['status']!='ASSEMBLING': raise WorkflowError('STATE_INVALID')
        elif m['state']==TECHNICAL and latest['status']!='REJECTED': raise WorkflowError('STATE_INVALID')
        _validate_export_semantics(path,m,source.size)
        if current is not None: current.update(source=source,source_bytes=source_bytes)
        return current
    except (WorkflowError,OSError,ValueError,KeyError,TypeError) as exc:
        reason=getattr(exc,'code','ARTIFACT_VERIFICATION_FAILED')
        if not mutate_on_error:raise WorkflowError(reason) from exc
        m['state']=INTEGRITY; m['integrity_error']=reason
        native_exports.mark_revoked(m)
        atomic_json(path,m)
        native_exports.revoke_exports(path,m)
        raise WorkflowError(reason) from exc


def _result(path,m):
    a=m['attempts'][-1]
    result={'run_manifest':str(path),'schema':SCHEMA,'state':m['state'],'attempt':len(m['attempts']),
            'request':m['request'],'source':m['source']['path'],'generation_claimed':False,
            'artifact_paths':a.get('artifact_paths',{}),'block_reason':a.get('block_reason',m.get('integrity_error')),
            'user_acceptance':'pending','unity_validation':'not_run',
            **native_exports.export_summary(m),'delivery_allowed':native_exports.delivery_allowed(m)}
    if native_exports.alignment_required(m):result['alignment_required']=True
    if 'artifact_hashes' in a:
        result.update(artifact_set_sha256=_review_target(_context(m)),consistency=a['consistency'])
    if 'task_ref' in m['request']:
        import native_alignment_source as managed
        result.update(managed.native_budget(m))
    return result


def _sync_reviews(path,m,verified,*,write=False):
    if 'task_ref' not in m['request'] or verified is None:return
    import native_alignment_source as managed
    def validate(report):
        return validate_review(_context(m),report,verified['source'],m['source_geometry'],verified['frames'],verified['components'])
    report=managed.reconcile_native_reviews(path,m,validate,write=write)
    if report is not None and write:
        a=m['attempts'][-1]
        a['visual_review']=report;a['review_summary']=validate(report)
        if not a.get('review_history') or a['review_history'][-1]!=report:a.setdefault('review_history',[]).append(report)
        m['state']=ALIGNMENT if report['decision']=='pass' else REJECT
        if report['decision']=='reject':native_exports.mark_revoked(m)
        atomic_json(path,m);native_exports.revoke_exports(path,m)


def inspect(manifest):
    path=_outside_skill(manifest)
    with run_lock(path.parent):
        path,m=_load(path); verified=_verify(path,m)
        if m['attempts'][-1].get('visual_review'):_sync_reviews(path,m,verified)
        return _result(path,m)


def resume(manifest):
    path=_outside_skill(manifest)
    with run_lock(path.parent):
        path,m=_load(path,recover_task=True); verified=_verify(path,m)
        _sync_reviews(path,m,verified,write=True)
        if m['state']==BUILDING: _finish(path,m)
        return _result(path,m)


def review(manifest, report):
    path=_outside_skill(manifest)
    with run_lock(path.parent):
        path,m=_load(path); verified=_verify(path,m)
        _sync_reviews(path,m,verified,write=True)
        if m['state'] not in (READY,PASS,REJECT,ALIGNMENT): raise WorkflowError('VISUAL_REVIEW_REQUIRES_ARTIFACTS')
        report=copy.deepcopy(report)
        summary=validate_review(_context(m),report,verified['source'],m['source_geometry'],
                                verified['frames'],verified['components'])
        if m['state']==REJECT and report['decision']=='pass':
            raise WorkflowError('REJECTED_ARTIFACTS_REQUIRE_REVISION')
        a=m['attempts'][-1]
        if 'task_ref' in m['request']:
            if a.get('visual_review')==report:return _result(path,m)
            import native_alignment_source as managed
            report=managed.archive_native_review(path,m,report,lambda value:validate_review(
                _context(m),value,verified['source'],m['source_geometry'],verified['frames'],verified['components']))
            summary=validate_review(_context(m),report,verified['source'],m['source_geometry'],verified['frames'],verified['components'])
        a.setdefault('review_history',[]).append(report)
        a['visual_review']=report; a['review_summary']=summary
        m['state']=(ALIGNMENT if native_exports.alignment_required(m) else PASS) if report['decision']=='pass' else REJECT
        if report['decision']=='reject': native_exports.mark_revoked(m)
        atomic_json(path,m)
        native_exports.revoke_exports(path,m)
        return _result(path,m)


def revise(manifest, recipe_path, feedback, *, correction=None):
    if not isinstance(feedback,str) or not 10<=len(feedback.strip())<=4000:
        raise WorkflowError('REVISION_FEEDBACK_REQUIRED')
    path=_outside_skill(manifest)
    with run_lock(path.parent):
        path,m=_load(path,recover_task=True); verified=_verify(path,m,verify_exports=False)
        _sync_reviews(path,m,verified,write=True)
        if m['state']==BUILDING: raise WorkflowError('ASSEMBLY_RESUME_REQUIRED')
        collected=_collect(recipe_path,m['request'].get('alignment_policy'))
        if 'task_ref' in m['request']:
            collected['request'].update({key:m['request'][key] for key in ('task_ref','task_max_attempts','pixel_art')})
        if collected['request']!=m['request'] or collected['data']['source']['sha256']!=m['source']['sha256']:
            raise WorkflowError('IMMUTABLE_SCOPE_CHANGED')
        metadata={}
        if 'task_ref' in m['request']:
            import native_alignment_source as managed
            metadata=managed.native_attempt(m,len(m['attempts'])+1,correction)
            managed.ledger.begin_attempt(m['request']['task_ref']['task_manifest'],m['run_id'],metadata['attempt_id'],
                managed.native_descriptor(m),m['request']['task_max_attempts'],correction)
        snapshot=_snapshot(path.parent,collected)
        m['attempts'].append({'number':len(m['attempts'])+1,'snapshot':snapshot,
                              'status':'ASSEMBLING','generation_claimed':False,'feedback':feedback.strip(),**metadata})
        m['state']=BUILDING; m.pop('integrity_error',None)
        native_exports.mark_revoked(m)
        atomic_json(path,m)
        native_exports.revoke_exports(path,m)
        _finish(path,m)
        return _result(path,m)


def export(manifest,destination):
    path=_outside_skill(manifest)
    with run_lock(path.parent):
        path,m=_load(path); verified=_verify(path,m)
        _sync_reviews(path,m,verified,write=True)
        native_exports.require_direct_delivery(m)
        if m['state']!=PASS: raise WorkflowError('VISUAL_REVIEW_REQUIRED')
        if native_exports.export_summary(m)['export_revocation_pending']:
            raise WorkflowError('EXPORT_REVOCATION_PENDING')
        destination=_outside_skill(destination)
        if destination==path.parent or path.parent in destination.parents:
            raise WorkflowError('WORKSPACE_NOT_ALLOWED')
        a=m['attempts'][-1]
        payloads={destination/Path(p).name:data for p,data in verified['payloads'].items()}
        payloads[destination/'visual-review.json']=encoded(a['visual_review'])
        payloads[destination/'delivery.json']=encoded(_delivery_receipt(
            path,m,a,verified['source'].size,a['visual_review']))
        record=native_exports.register_export(path,m,destination,payloads,_review_target(_context(m)))
        native_exports.write_export(path,m,record,payloads)
        result=_result(path,m); result['export_directory']=str(destination)
        result['delivery_artifact_paths']={key:[str(destination/Path(p).name) for p in value] if isinstance(value,list)
                                           else str(destination/Path(value).name) for key,value in a['artifact_paths'].items()}
        return result


def recover_exports(manifest):
    path=_outside_skill(manifest)
    with run_lock(path.parent):
        path,m=_load(path); verified=_verify(path,m)
        _sync_reviews(path,m,verified,write=True)
        if m['state']!=PASS:
            native_exports.mark_revoked(m); atomic_json(path,m)
        native_exports.revoke_exports(path,m)
        # Recover from the same verified buffers, not a second read of source artifacts.
        for record in m.get('exports',[]):
            if record['status']!='EXPORT_PENDING': continue
            if record['attempt']!=len(m['attempts']) or m['state']!=PASS:
                raise WorkflowError('EXPORT_STATE_INVALID')
            destination=native_exports.safe_path(record['destination'])
            payloads={destination/Path(p).name:data for p,data in verified['payloads'].items()}
            a=m['attempts'][record['attempt']-1]
            review=_export_review(record,a)
            payloads[destination/'visual-review.json']=encoded(review)
            payloads[destination/'delivery.json']=encoded(_delivery_receipt(
                path,m,a,verified['source'].size,review))
            native_exports.write_export(path,m,record,payloads)
        return _result(path,m)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    build_parser=sub.add_parser('build')
    build_parser.add_argument('--recipe',required=True)
    build_parser.add_argument('--source-geometry',required=True)
    build_parser.add_argument('--workspace-root',required=True)
    build_parser.add_argument('--task-manifest',required=True);build_parser.add_argument('--target-id')
    build_parser.add_argument('--max-attempts',type=int,default=2)
    for name in ('inspect','resume','review','revise','export','recover-exports'):
        cmd=sub.add_parser(name); cmd.add_argument('--run-manifest',required=True)
        if name=='review': cmd.add_argument('--review-file',required=True)
        if name=='revise':
            cmd.add_argument('--recipe',required=True); cmd.add_argument('--feedback',required=True)
            cmd.add_argument('--correction-file')
        if name=='export': cmd.add_argument('--destination',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='build': result=build(args.recipe,read_json(args.source_geometry),args.workspace_root,
            task_manifest=args.task_manifest,target_id=args.target_id,max_attempts=args.max_attempts)
        elif args.command=='review': result=review(args.run_manifest,read_json(args.review_file))
        elif args.command=='revise': result=revise(args.run_manifest,args.recipe,args.feedback,
            correction=read_json(args.correction_file) if args.correction_file else None)
        elif args.command=='export': result=export(args.run_manifest,args.destination)
        else: result={'inspect':inspect,'resume':resume,'recover-exports':recover_exports}[args.command](args.run_manifest)
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (WorkflowError,OSError,ValueError) as exc:
        print(json.dumps({'error':getattr(exc,'code','LOCAL_WORKFLOW_FAILED'),'delivery_allowed':False})); return 2


if __name__=='__main__': raise SystemExit(main())
