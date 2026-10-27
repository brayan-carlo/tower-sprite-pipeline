"""Local native-image lifecycle. Generation itself is an explicit agent tool call.

This module never submits to an image service and never resolves FAL_KEY.
The native schema is deliberately separate from fal's job/price ledger.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import uuid
import native_exports
import native_geometry
from PIL import Image

from generic_sprite_workflow import _review_target, _validate_visual_report
from native_sheet_images import pixel_sha256, process_sheet, validate_source, diagnose_matte as _diagnose_matte
from sheet_images import assemble_sheet, consistency_diagnostics, SheetImageError
from sheet_workflow_state import (WorkflowError, atomic_json, encoded,
                                 new_or_identical, read_json, run_lock,
                                 scope_sha, sha_file)

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 'native_sheet_v1'
READY = 'READY_FOR_VISUAL_REVIEW'
PASS = 'READY_FOR_DELIVERY'
REJECT = 'VISUAL_REJECTED'
GEOMETRY = 'READY_FOR_GEOMETRY_REVIEW'
ALIGNMENT = 'READY_FOR_ALIGNMENT'


def _outside_skill(value):
    path = Path(value).expanduser().resolve()
    for forbidden in (SKILL_ROOT, Path.home()/'.codex/skills'):
        if path == forbidden or forbidden in path.parents:
            raise WorkflowError('WORKSPACE_NOT_ALLOWED')
    return path


def _prompt(request, feedback):
    direction = request['orientation'].upper()
    return (
        'Create a 2D game animation from the attached SINGLE isolated character PNG. '
        'Preserve the exact character identity, palette, skull shape, body mass, accessories '
        'and pixel-art rendering. Exactly four anatomically attached legs. '
        f'Character description (data): {json.dumps(request["description"], ensure_ascii=False)}. '
        f'Full body, facing {direction} in every frame, running in place. '
        'Match the exact viewing angle of the reference, including its slight three-quarter '
        'side view when present. Never turn into a flatter profile during extension. '
        'Create EXACTLY 8 poses in a regular 4-column x 2-row grid, ideally 2048x1024. '
        'Keep every pose inside its cell with generous margins around head, feet and tail. '
        'Same anatomical scale and fixed ground plane. No perspective or camera changes. '
        f'Eight frames cover TWO consecutive galloping strides over {8*request["frame_ms"]} ms. '
        'Each row is one four-frame stride. Make all four key poses obviously different. '
        'Frame 1: FORELIMB SUPPORT, a forepaw down beneath the shoulder supporting body weight; '
        'hind paws swing forward underneath the body. Frame 2: COLLECTED SUSPENSION, all four '
        'paws off ground, hind legs forward under belly, front legs retracting under chest, '
        'flexed back, tail curves upward and accessories lag. Frame 3: HINDLIMB PROPULSION, '
        'a hindpaw down behind the hip pushing, front legs reaching toward the requested '
        'direction off ground, chest rising. Frame 4: EXTENDED SUSPENSION, forelegs reach '
        'forward and hindlegs backward, spine opens moderately, tail streams, accessories swing. '
        'Keep the source chest volume, broad muscular torso and skull width through extension. '
        'Frames 5-8 repeat those four phases with small near/far limb offsets and continuing '
        'secondary motion, never identical pixel copies. Frame 8 flows into support at frame 1. '
        'Preserve modest vertical flight; do not draw consecutive nearly identical extensions. '
        'Background: perfectly flat solid MAGENTA #FF00FF including all gaps between limbs '
        'and accessories. No gradient, checkerboard, ground shadow, magenta illumination, '
        'text, frame numbers, dividers or border. '
        f'Correction feedback (data): {json.dumps(feedback, ensure_ascii=False)}.\n'
    )


def _new_attempt(path, manifest, feedback, correction=None, *, defer_prompt=False):
    number = len(manifest['attempts'])+1
    directory = path.parent/f'attempt-{number:02d}'
    # A manifest-save interruption can leave only the prepared prompt. Reuse
    # that identical preparation; evidence of generation is not safe to reset.
    if directory.is_symlink():
        raise WorkflowError('PATH_INVALID')
    prompt_file = directory/'prompt.txt'
    payload=_prompt(manifest['request'], feedback).encode('utf-8')
    if not defer_prompt:
        directory.mkdir(exist_ok=True)
        if any(p.name != 'prompt.txt' for p in directory.iterdir()):
            raise WorkflowError('GENERATION_STATUS_UNKNOWN')
        new_or_identical(prompt_file,payload)
    metadata={}
    if 'task_ref' in manifest['request']:
        import native_alignment_source as managed
        metadata=managed.native_attempt(manifest,number,correction)
    manifest['attempts'].append({'number':number, 'feedback':feedback,
        'prompt_file':str(prompt_file), 'prompt_sha256':hashlib.sha256(payload).hexdigest(),**metadata})
    manifest['state'] = 'PREPARATION_PENDING' if defer_prompt else 'PREPARED'


def prepare(source, workspace, orientation, frame_ms=100, max_attempts=2, description='', source_geometry=None,
            *, task_manifest=None, target_id=None):
    if source_geometry is None:
        raise WorkflowError('SOURCE_GEOMETRY_REQUIRED')
    validate_source(Path(source).expanduser().resolve())
    native_geometry.validate_source_geometry(source, source_geometry)
    import native_alignment_source as managed
    request=dict(action='run',profile='quadruped_two_stride',orientation=orientation,frames=8,frame_ms=frame_ms,
        max_attempts=max_attempts,pixel_art=True,canvas=list(validate_source(Path(source).resolve())),loop=True,
        description=description,geometry_protocol=1,source_geometry=source_geometry,alignment_policy='body_x_preserve_y_v1')
    ref=managed.task_ref(task_manifest,request,sha_file(source),target_id)
    return _prepare(source,workspace,orientation,frame_ms,max_attempts,description,source_geometry,
        alignment_policy='body_x_preserve_y_v1',managed_ref=ref)


def _prepare(source, workspace, orientation, frame_ms=100, max_attempts=2, description='', source_geometry=None,
             *, alignment_policy=None, managed_ref=None):
    # Internal legacy-fixture entry point. New agent/API runs use prepare().
    if orientation not in ('left', 'right'):
        raise WorkflowError('ORIENTATION_UNSUPPORTED')
    if type(frame_ms) is not int or not 20 <= frame_ms <= 1000 or frame_ms % 10:
        raise WorkflowError('TIMING_INVALID')
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise WorkflowError('ATTEMPT_LIMIT_INVALID')
    if not isinstance(description, str) or len(description) > 4000:
        raise WorkflowError('DESCRIPTION_INVALID')
    source = Path(source).expanduser().resolve()
    canvas = validate_source(source)
    workspace = _outside_skill(workspace)
    directory = workspace/('native-'+uuid.uuid4().hex)
    directory.mkdir(parents=True)
    source_copy = directory/'source.png'
    new_or_identical(source_copy, source.read_bytes())
    path = directory/'run-manifest.json'
    request = {'action':'run', 'profile':'quadruped_two_stride', 'orientation':orientation,
               'frames':8, 'frame_ms':frame_ms, 'canvas':list(canvas), 'loop':True,
               'pixel_art':True, 'description':description, 'max_attempts':max_attempts}
    if alignment_policy is not None:request['alignment_policy']=alignment_policy
    if source_geometry is not None:
        request.update(geometry_protocol=1,source_geometry=source_geometry)
    if managed_ref is not None:request['task_ref']=managed_ref
    m = {'schema':SCHEMA, 'run_id':directory.name, 'request':request,
         'source':{'path':str(source_copy), 'sha256':sha_file(source_copy),
                   'original_path':str(source)}, 'attempts':[]}
    m['scope_sha256'] = scope_sha({'request':request,'source':m['source']})
    _new_attempt(path, m, '',defer_prompt=managed_ref is not None)
    if managed_ref is not None:
        import native_alignment_source as managed
        managed.seal_native(path,m)
    atomic_json(path, m)
    if managed_ref is not None:
        with run_lock(directory):_load(path,recover_task=True)
    return inspect(path)


def _verified_file(value, digest, root):
    path = Path(value)
    if path.is_symlink() or root not in path.resolve().parents:
        raise WorkflowError('PATH_INVALID')
    if sha_file(path) != digest:
        raise WorkflowError('HASH_MISMATCH')


def _context(m):
    attempt = m['attempts'][-1]
    return {'run_id':m['run_id'], 'attempts':m['attempts'],
            'request':m['request'], 'artifact_hashes':attempt['artifact_hashes'],
            'consistency':attempt['consistency']}


def _verified_review_context(m, *, frames=None, qa=None):
    """Bind new-policy review completeness to checked pixels and QA, without repair."""
    a=m['attempts'][-1]
    def checked_bytes(path):
        payload=Path(path).read_bytes()
        if hashlib.sha256(payload).hexdigest()!=a['artifact_hashes'][path]:
            raise WorkflowError('HASH_MISMATCH')
        return payload
    if frames is None:
        frames=[]
        for path in a['artifact_paths']['frames']:
            with Image.open(io.BytesIO(checked_bytes(path))) as image:frames.append(image.convert('RGBA'))
    if qa is None:qa=json.loads(checked_bytes(a['artifact_paths']['qa_report']))
    if len(frames)!=m['request']['frames']:raise WorkflowError('CONSISTENCY_INVALID')
    consistency=consistency_diagnostics(frames)
    if not isinstance(qa,dict) or consistency!=a.get('consistency') or consistency!=qa.get('consistency'):
        raise WorkflowError('CONSISTENCY_INVALID')
    return {**_context(m),'consistency':consistency}


def _geometry_target(m):
    a=m['attempts'][-1]
    return scope_sha({'scope':m['scope_sha256'],'attempt':a['number'],
        'raw':a['raw']['sha256'],'processing':a['processing_hashes']})


def _load(manifest, *, recover_task=False):
    path = Path(manifest).expanduser().resolve()
    m = read_json(path)
    if m.get('schema') != SCHEMA or m.get('run_id') != path.parent.name:
        raise WorkflowError('NATIVE_MANIFEST_INVALID')
    if scope_sha({'request':m['request'],'source':m['source']}) != m['scope_sha256']:
        raise WorkflowError('SCOPE_MISMATCH')
    requires_alignment=native_exports.alignment_required(m)
    if requires_alignment and m['state']==PASS:raise WorkflowError('ALIGNMENT_REQUIRED')
    if not 1 <= len(m['attempts']) <= m['request']['max_attempts']:
        raise WorkflowError('ATTEMPT_LIMIT')
    pending=m['state']=='PREPARATION_PENDING'
    if pending and ('task_ref' not in m['request'] or len(m['attempts'])!=1):
        raise WorkflowError('NATIVE_MANIFEST_INVALID')
    _verified_file(m['source']['path'], m['source']['sha256'], path.parent)
    for number, attempt in enumerate(m['attempts'], 1):
        root = path.parent/f'attempt-{number:02d}'
        if attempt['number'] != number:
            raise WorkflowError('NATIVE_MANIFEST_INVALID')
        if pending:
            required={'number','feedback','prompt_file','prompt_sha256','attempt_id','strategy_sha256','correction'}
            if (set(attempt)!=required or attempt['feedback']!='' or attempt['correction'] is not None
                    or attempt['prompt_file']!=str(root/'prompt.txt')
                    or attempt['prompt_sha256']!=hashlib.sha256(_prompt(m['request'],'').encode('utf-8')).hexdigest()):
                raise WorkflowError('NATIVE_MANIFEST_INVALID')
            if Path(attempt['prompt_file']).exists():_verified_file(attempt['prompt_file'],attempt['prompt_sha256'],root)
        else:_verified_file(attempt['prompt_file'], attempt['prompt_sha256'], root)
        if 'raw' in attempt:
            _verified_file(attempt['raw']['path'], attempt['raw']['sha256'], root)
        for file, digest in attempt.get('artifact_hashes', {}).items():
            _verified_file(file, digest, root)
        for file, digest in attempt.get('processing_hashes', {}).items():
            _verified_file(file, digest, root)
        for file,digest in attempt.get('matte_diagnostic_hashes',{}).items():
            _verified_file(file,digest,root)
    current = m['attempts'][-1]
    if m['request'].get('geometry_protocol') and m['state'] in (GEOMETRY,READY,PASS,REJECT,ALIGNMENT):
        listed=set(current['processing_paths'].values())
        if listed!=set(current['processing_hashes']):raise WorkflowError('PROCESSING_LIST_INVALID')
        if m['state']!=GEOMETRY and not current.get('geometry_validated'):
            raise WorkflowError('GEOMETRY_REVIEW_REQUIRED')
    if m['state'] in (READY, PASS, REJECT, ALIGNMENT):
        listed = {str(p) for value in current['artifact_paths'].values()
                  for p in (value if isinstance(value, list) else [value])}
        if listed != set(current['artifact_hashes']):
            raise WorkflowError('ARTIFACT_LIST_INVALID')
        context=_verified_review_context(m) if requires_alignment else _context(m)
        if 'visual_review' in current:
            _validate_visual_report(context, current['visual_review'])
            expected = (ALIGNMENT if requires_alignment else PASS) if current['visual_review']['decision'] == 'pass' else REJECT
            if m['state'] != expected:
                raise WorkflowError('VISUAL_REVIEW_INVALID')
        elif m['state'] != READY:
            raise WorkflowError('VISUAL_REVIEW_INVALID')
    import native_alignment_source as managed
    managed.native_task_state(path,m,recover=recover_task)
    if pending and recover_task:
        attempt=m['attempts'][0];directory=path.parent/'attempt-01'
        if directory.is_symlink():raise WorkflowError('PATH_INVALID')
        directory.mkdir(exist_ok=True)
        if any(p.name!='prompt.txt' for p in directory.iterdir()):raise WorkflowError('GENERATION_STATUS_UNKNOWN')
        new_or_identical(Path(attempt['prompt_file']),_prompt(m['request'],'').encode('utf-8'))
        _verified_file(attempt['prompt_file'],attempt['prompt_sha256'],directory)
        m['state']='PREPARED';atomic_json(path,m)
    return path, m


def _result(path, m):
    a = m['attempts'][-1]
    r = {'run_manifest':str(path), 'schema':SCHEMA, 'state':m['state'],
         'attempt':len(m['attempts']), 'max_attempts':m['request']['max_attempts'],
         'delivery_allowed':m['state']==PASS, 'source':m['source']['path'],
         'prompt_file':a['prompt_file'], 'request':m['request'],
         'artifact_paths':a.get('artifact_paths', {}), 'block_reason':a.get('block_reason'),
         'user_acceptance':'pending', 'unity_validation':'not_run'}
    if native_exports.alignment_required(m):r['alignment_required']=True
    r.update(native_exports.export_summary(m))
    r['delivery_allowed'] = native_exports.delivery_allowed(m)
    if m['state']==GEOMETRY:
        r.update(geometry_target_sha256=_geometry_target(m),processing_paths=a['processing_paths'])
    if 'geometry_summary' in a:r['geometry']=a['geometry_summary']
    if 'matte_diagnostic_paths' in a:r['matte_diagnostic_paths']=a['matte_diagnostic_paths']
    if 'artifact_hashes' in a:
        r.update(artifact_set_sha256=_review_target(_context(m)), consistency=a['consistency'])
    if 'task_ref' in m['request']:
        import native_alignment_source as managed
        r.update(managed.native_budget(m))
    return r


def _sync_reviews(path,m,*,write=False):
    if 'task_ref' not in m['request'] or 'artifact_hashes' not in m['attempts'][-1]:return
    import native_alignment_source as managed
    context=_verified_review_context(m)
    report=managed.reconcile_native_reviews(path,m,lambda value:_validate_visual_report(context,value),write=write)
    if report is not None and write:
        m['attempts'][-1]['visual_review']=report
        m['state']=ALIGNMENT if report['decision']=='pass' else REJECT
        if report['decision']=='reject':native_exports.mark_revoked(m)
        atomic_json(path,m);native_exports.revoke_exports(path,m)


def inspect(manifest):
    path, m = _load(manifest)
    if m['attempts'][-1].get('visual_review'):_sync_reviews(path,m)
    return _result(path, m)


def begin(manifest):
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path,recover_task=True)
        _sync_reviews(path,m,write=True)
        if m['state'] != 'PREPARED':
            raise WorkflowError('GENERATION_ALREADY_CLAIMED')
        m['state'] = 'NATIVE_GENERATION_STARTED'
        m['attempts'][-1]['generation_claimed'] = True
        atomic_json(path, m)
    return inspect(path)


def diagnose_matte(manifest):
    path=Path(manifest).resolve()
    with run_lock(path.parent):
        path,m=_load(path);a=m['attempts'][-1]
        if 'raw' not in a:raise WorkflowError('OBSERVED_RESULT_REQUIRED')
        output=path.parent/f'attempt-{a["number"]:02d}'/('matte-diagnostic-'+uuid.uuid4().hex)
        result=_diagnose_matte(a['raw']['path'],output)
        a['matte_diagnostic_paths']=result['paths']
        a['matte_diagnostic_hashes']={str(p):sha_file(p) for p in result['paths'].values()}
        atomic_json(path,m)
    return inspect(path)


def ingest(manifest, raw, provenance, matte_review=None):
    if not isinstance(provenance, str) or not 10 <= len(provenance.strip()) <= 4000:
        raise WorkflowError('OBSERVED_RESULT_PROVENANCE_REQUIRED')
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path,recover_task=True)
        _sync_reviews(path,m,write=True)
        correcting_matte=(m['state']=='TECHNICAL_REJECTED' and matte_review is not None
            and m['request'].get('geometry_protocol')
            and 'raw' in m['attempts'][-1])
        if m['state'] not in ('PREPARED', 'NATIVE_GENERATION_STARTED', 'ASSEMBLING') and not correcting_matte:
            raise WorkflowError('INGEST_NOT_ALLOWED')
        raw = Path(raw).expanduser().resolve()
        digest = sha_file(raw)
        pixels = pixel_sha256(raw)
        if any('raw' in old and pixel_sha256(old['raw']['path']) == pixels
               for old in m['attempts'][:-1]):
            raise WorkflowError('UNCHANGED_REJECTED_RESULT')
        a = m['attempts'][-1]
        if 'raw' in a and a['raw']['sha256'] != digest:
            raise WorkflowError('RESULT_ALREADY_RECORDED')
        directory = path.parent/f'attempt-{len(m["attempts"]):02d}'
        raw_copy = directory/'raw.png'
        new_or_identical(raw_copy, raw.read_bytes())
        if sha_file(raw_copy) != digest:
            raise WorkflowError('HASH_MISMATCH')
        a['raw'] = {'path':str(raw_copy), 'sha256':digest, 'observed_path':str(raw),
                    'pixel_sha256':pixels,
                    'provenance':provenance, 'generation_claimed':a.get('generation_claimed', False)}
        m['state'] = 'ASSEMBLING'
        atomic_json(path, m)
        # A crash may leave partial outputs. A recovery keeps them as evidence
        # and works in a fresh directory, always from the already recorded raw.
        output = directory/('processing-'+uuid.uuid4().hex)
        try:
            processed = process_sheet(raw_copy, output,
                strict_components=bool(m['request'].get('geometry_protocol')),matte_review=matte_review,
                alignment_policy=m['request'].get('alignment_policy'))
            paths = processed['paths']
            a['processing_hashes'] = {str(p):sha_file(p) for p in paths.values()}
            a['processing_paths'] = paths
            a.pop('block_reason',None)
            if m['request'].get('geometry_protocol'):
                m['state']=GEOMETRY
            else:
                result = assemble_sheet(paths['registered'], output/'candidate', frame_count=8,
                    columns=4, rows=2, canvas=tuple(m['request']['canvas']),
                    frame_ms=m['request']['frame_ms'], loop=True, pixel_art=True, grounded=False)
                _assembled(m,result)
        except (WorkflowError, SheetImageError, OSError, ValueError) as exc:
            m['state'] = 'TECHNICAL_REJECTED'
            a['block_reason'] = getattr(exc, 'code', 'IMAGE_PROCESSING_FAILED')
        atomic_json(path, m)
    return inspect(path)


def _assembled(m,result):
    a=m['attempts'][-1]
    a['artifact_paths']=result['artifact_paths']
    a['artifact_hashes']={str(p):sha_file(p) for value in a['artifact_paths'].values()
        for p in (value if isinstance(value,list) else [value])}
    a['consistency']=result['qa']['consistency']
    m['state']=READY


def calibrate(manifest,report):
    path=Path(manifest).resolve()
    with run_lock(path.parent):
        path,m=_load(path)
        if m['state']!=GEOMETRY:raise WorkflowError('GEOMETRY_REVIEW_NOT_ALLOWED')
        if report.get('geometry_target_sha256')!=_geometry_target(m):
            raise WorkflowError('GEOMETRY_REVIEW_STALE')
        a=m['attempts'][-1]
        with Image.open(a['processing_paths']['registered']) as sheet:
            frames,meta,reference=native_geometry.calibrate_frames(sheet.convert('RGBA'),
                m['source']['path'],m['request']['source_geometry'],report,alignment_policy=m['request'].get('alignment_policy'))
        output=path.parent/f'attempt-{a["number"]:02d}'/('calibrated-'+uuid.uuid4().hex)
        result=native_geometry.write_artifacts(frames,meta,reference,report,output,m['request']['frame_ms'])
        a['geometry_validated']=True
        a['geometry_summary']={k:meta[k] for k in ('canvas','scale','pivot_top_left_px',
            'pivot_bottom_left_normalized','source_canvas','source_pivot','policy')}
        _assembled(m,result)
        atomic_json(path,m)
    return inspect(path)


def revise(manifest, feedback, *, correction=None):
    if not isinstance(feedback, str) or not 10 <= len(feedback.strip()) <= 4000:
        raise WorkflowError('REVISION_FEEDBACK_REQUIRED')
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path,recover_task=True)
        _sync_reviews(path,m,write=True)
        if m['state'] not in (REJECT, 'TECHNICAL_REJECTED', GEOMETRY):
            raise WorkflowError('REVISION_NOT_ALLOWED')
        if len(m['attempts']) >= m['request']['max_attempts']:
            raise WorkflowError('ATTEMPT_LIMIT')
        if 'task_ref' in m['request']:
            import native_alignment_source as managed
            metadata=managed.native_attempt(m,len(m['attempts'])+1,correction)
            managed.ledger.begin_attempt(m['request']['task_ref']['task_manifest'],m['run_id'],metadata['attempt_id'],
                managed.native_descriptor(m),m['request']['max_attempts'],correction)
        _new_attempt(path, m, feedback,correction)
        atomic_json(path, m)
    return inspect(path)


def review(manifest, report):
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path)
        _sync_reviews(path,m,write=True)
        if m['state'] not in (READY, PASS, REJECT, ALIGNMENT):
            raise WorkflowError('VISUAL_REVIEW_REQUIRES_ARTIFACTS')
        context=_verified_review_context(m) if native_exports.alignment_required(m) else _context(m)
        _validate_visual_report(context, report)
        if m['state'] == REJECT and report['decision'] == 'pass':
            raise WorkflowError('REJECTED_ARTIFACTS_REQUIRE_REVISION')
        if 'task_ref' in m['request']:
            if m['attempts'][-1].get('visual_review')==report:return _result(path,m)
            import native_alignment_source as managed
            report=managed.archive_native_review(path,m,report,lambda value:_validate_visual_report(context,value))
        # Report and decision share one atomic write: no orphan pass receipt.
        m['attempts'][-1]['visual_review'] = report
        m['state'] = (ALIGNMENT if native_exports.alignment_required(m) else PASS) if report['decision'] == 'pass' else REJECT
        if report['decision'] == 'reject':
            native_exports.mark_revoked(m)
        atomic_json(path, m)
        native_exports.revoke_exports(path, m)
    return inspect(path)


def export(manifest, destination):
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path)
        _sync_reviews(path,m,write=True)
        native_exports.require_direct_delivery(m)
        if m['state'] != PASS:
            raise WorkflowError(REJECT if m['state'] == REJECT else 'VISUAL_REVIEW_REQUIRED')
        destination = _outside_skill(native_exports.safe_path(destination))
        if destination == path.parent or path.parent in destination.parents:
            raise WorkflowError('WORKSPACE_NOT_ALLOWED')
        a = m['attempts'][-1]
        payloads = {destination/Path(p).name:Path(p).read_bytes()
                    for value in a['artifact_paths'].values()
                    for p in (value if isinstance(value, list) else [value])}
        payloads[destination/'visual-review.json'] = encoded(a['visual_review'])
        payloads[destination/'delivery.json'] = encoded({
            'schema':SCHEMA,'state':PASS,'artifact_set_sha256':_review_target(_context(m)),
            'source_sha256':m['source']['sha256'],'raw_sha256':a['raw']['sha256'],
            'request':m['request'],'run_manifest':str(path),
            'delivery_allowed':True,'user_acceptance':'pending','unity_validation':'not_run',
            **({'geometry':a['geometry_summary']} if 'geometry_summary' in a else {})})
        if native_exports.export_summary(m)['export_revocation_pending']:
            raise WorkflowError('EXPORT_REVOCATION_PENDING')
        record = native_exports.register_export(path, m, destination, payloads, _review_target(_context(m)))
        native_exports.write_export(path, m, record, payloads)
        result = _result(path, m)
        result['delivery_artifact_paths'] = {
            k:[str(destination/Path(p).name) for p in v] if isinstance(v, list)
              else str(destination/Path(v).name) for k, v in a['artifact_paths'].items()}
        result['export_directory'] = str(destination)
        return result


def recover_exports(manifest):
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path)
        _sync_reviews(path,m,write=True)
        native_exports.reconcile_exports(path, m)
    return inspect(path)


def adopt_export(manifest, destination):
    path = Path(manifest).resolve()
    with run_lock(path.parent):
        path, m = _load(path)
        _sync_reviews(path,m,write=True)
        destination = _outside_skill(native_exports.safe_path(destination))
        if destination == path.parent or path.parent in destination.parents:
            raise WorkflowError('WORKSPACE_NOT_ALLOWED')
        native_exports.adopt_export(path, m, destination)
    return inspect(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('prepare')
    p.add_argument('--source', required=True); p.add_argument('--workspace-root', required=True)
    p.add_argument('--orientation', choices=['left','right'], required=True)
    p.add_argument('--frame-ms', type=int, default=100); p.add_argument('--max-attempts', type=int, default=2)
    p.add_argument('--description', default='')
    p.add_argument('--source-geometry',required=True)
    p.add_argument('--task-manifest',required=True);p.add_argument('--target-id')
    for command in ('begin','ingest','inspect','revise','review','export','calibrate','recover-exports','adopt-export','diagnose-matte'):
        p = subs.add_parser(command); p.add_argument('--run-manifest', required=True)
        if command == 'ingest':
            p.add_argument('--raw', required=True); p.add_argument('--provenance', required=True)
            p.add_argument('--matte-review-file')
        elif command == 'revise':
            p.add_argument('--feedback', required=True);p.add_argument('--correction-file')
        elif command == 'review': p.add_argument('--review-file', required=True)
        elif command in ('export','adopt-export'): p.add_argument('--output-dir', required=True)
        elif command == 'calibrate': p.add_argument('--geometry-file',required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.source, args.workspace_root, args.orientation,
                             args.frame_ms, args.max_attempts, args.description,read_json(args.source_geometry),
                             task_manifest=args.task_manifest,target_id=args.target_id)
        elif args.command == 'ingest': result = ingest(args.run_manifest,args.raw,args.provenance,
            read_json(args.matte_review_file) if args.matte_review_file else None)
        elif args.command == 'revise': result = revise(args.run_manifest,args.feedback,
            correction=read_json(args.correction_file) if args.correction_file else None)
        elif args.command == 'review': result = review(args.run_manifest,read_json(args.review_file))
        elif args.command == 'export': result = export(args.run_manifest,args.output_dir)
        elif args.command == 'adopt-export': result = adopt_export(args.run_manifest,args.output_dir)
        elif args.command == 'calibrate': result = calibrate(args.run_manifest,read_json(args.geometry_file))
        else: result = {'begin':begin,'inspect':inspect,'recover-exports':recover_exports,
            'diagnose-matte':diagnose_matte}[args.command](args.run_manifest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['state'] in ('TECHNICAL_REJECTED',REJECT) else 0
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'state':'BLOCKED','delivery_allowed':False,
                          'block_reason':getattr(exc,'code','NATIVE_WORKFLOW_INVALID')}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
