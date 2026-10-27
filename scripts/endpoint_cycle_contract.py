"""Closed, local evidence validator for the explicit endpoint-cycle experiment.

No network, budget authority, provider driver, quality decision or alternate export.
The validation DAG intentionally never calls generic._load or the private bridge.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import errno
from html import unescape
from html.parser import HTMLParser
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import struct
import sys
import tempfile
import uuid
import warnings
import zlib

from PIL import Image, ImageFile, features, __version__ as PILLOW_VERSION
import sheet_images as codec
import sprite_task_ledger as ledger
from sheet_video import resolve_ffmpeg
from sheet_workflow_state import WorkflowError, atomic_json, encoded, new_or_identical, read_json, run_lock, scope_sha, sha_file

ROLES = ('pose_edit', 'video_ab', 'video_ba', 'cleanup')
STAGES = ('input_ready', 'pair_ready', 'ab_ready', 'cycle_ready')
MODELS = dict(pose_edit='fal-ai/nano-banana-2/edit', video_ab='fal-ai/wan-flf2v',
              video_ba='fal-ai/wan-flf2v', cleanup='fal-ai/ideogram/remove-background')
SCHEMAS = {
    MODELS['pose_edit']: ('NanoBanana2EditInput', '7f069b57442e0acd2b46bd3cc9131e0501132c07144c44ad45ece9c6e4054ce3'),
    MODELS['video_ab']: ('WanFlf2vInput', '8c57ca8d3b9a29600d1d65f6d7fc9593461d443db9d64a8a8ccaa02f0732b4eb'),
    MODELS['cleanup']: ('IdeogramRemoveBackgroundInput', '20a65966f81b36ab7c815f7e65f0294393156ee142e541796d1690396a7377f3')}
GATE_CHECKS = {
    'input_ready': {'a_identity','a_scale','a_material','a_anatomy','a_orientation','a_framing','b_correction_scope'},
    'pair_ready': {'identity','scale','material','anatomy','opposition','orientation','framing'},
    'ab_ready': {'identity','scale','material','anatomy','orientation','framing','half_cycle','endpoint_match'},
    'cycle_ready': {'identity','scale','material','anatomy','orientation','framing','phase_order','join_a','join_b','sampling_full_range'}}
TRANSFORM = dict(scale=1,size=[1254,1254],offset=[397,397],canvas=[2048,2048],background_rgb=[110,110,110])
ASSEMBLED = {'READY_FOR_VISUAL_REVIEW','READY_FOR_DELIVERY','VISUAL_REJECTED'}
SEEDREAM_VARIANT='seedream45_a_identity_v1'
SEEDREAM_MODEL='fal-ai/bytedance/seedream/v4.5/edit'


def seedream(context):
    return context['plan'].get('variant')==SEEDREAM_VARIANT


def model_for(context,role):
    return SEEDREAM_MODEL if seedream(context) and role=='pose_edit' else MODELS[role]


def decoder_capabilities():
    """Observe only the installed fixed runtime; config cannot select a decoder."""
    fixed=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Path(sys.executable).resolve()!=fixed.resolve() or not features.check_codec('jpg') or ImageFile.LOAD_TRUNCATED_IMAGES
            or not callable(getattr(Image.core,'jpeg_decoder',None)) or not callable(getattr(Image.core,'zip_decoder',None))):
        fail('SEEDREAM_DECODER_UNAVAILABLE')
    jpeg=features.version_codec('jpg')
    if not isinstance(jpeg,str) or not jpeg.strip() or not PILLOW_VERSION:fail('SEEDREAM_DECODER_UNAVAILABLE')
    return dict(pillow_version=PILLOW_VERSION,jpeg_codec_version=jpeg)


def _exif_orientation_invalid(blocks):
    # Parse raw TIFF IFDs so duplicate Orientation tags/EXIF blocks cannot be
    # silently collapsed by Pillow. No orientation transform is ever applied.
    if len(blocks)>1:return True
    if not blocks:return False
    data=blocks[0]
    try:
        order='<' if data[:2]==b'II' else '>' if data[:2]==b'MM' else None
        if order is None or struct.unpack_from(order+'H',data,2)[0]!=42:return True
        offset=struct.unpack_from(order+'I',data,4)[0];seen=set();values=[]
        while offset:
            if offset in seen or offset<8:return True
            seen.add(offset);count=struct.unpack_from(order+'H',data,offset)[0]
            if count>4096:return True
            for i in range(count):
                pos=offset+2+12*i;tag,kind,n=struct.unpack_from(order+'HHI',data,pos)
                if tag==274:
                    if kind!=3 or n!=1:return True
                    values.append(struct.unpack_from(order+'H',data,pos+8)[0])
            offset=struct.unpack_from(order+'I',data,offset+2+12*count)[0]
        return len(values)>1 or any(value!=1 for value in values)
    except (struct.error,IndexError):return True


def _raster_headers(raw,fmt):
    """Retain determinable properties even when later complete bytes corrupt."""
    size=None;mode_bad=False;exif=[];corrupt=False
    try:
        if fmt=='PNG':
            pos=8;ended=False;ihdr=False
            while pos<len(raw):
                length=struct.unpack_from('>I',raw,pos)[0];kind=raw[pos+4:pos+8];end=pos+12+length
                if end>len(raw):corrupt=True;break
                data=raw[pos+8:pos+8+length]
                if zlib.crc32(kind+data)&0xffffffff!=struct.unpack_from('>I',raw,pos+8+length)[0]:corrupt=True
                if kind==b'IHDR':
                    if ihdr or length!=13:corrupt=True
                    else:
                        width,height,depth,color,compression,filtering,interlace=struct.unpack('>IIBBBBB',data)
                        size=(width,height);mode_bad=depth!=8 or color!=2
                        if compression or filtering or interlace not in (0,1):corrupt=True
                    ihdr=True
                if kind==b'acTL':mode_bad=True
                if kind==b'eXIf':exif.append(data)
                if kind==b'IEND':
                    ended=True
                    if end!=len(raw) or length:corrupt=True
                    break
                pos=end
            if not ihdr or not ended:corrupt=True
        else:
            pos=2;ended=False;in_scan=False
            while pos<len(raw):
                if in_scan:
                    pos=raw.find(b'\xff',pos)
                    if pos<0:corrupt=True;break
                elif raw[pos]!=255:corrupt=True;break
                while pos<len(raw) and raw[pos]==255:pos+=1
                marker=raw[pos];pos+=1
                if in_scan and (marker==0 or marker==0x01 or 0xd0<=marker<=0xd7):continue
                if marker==0xd9:
                    ended=True
                    if pos!=len(raw):corrupt=True
                    break
                if marker==0 or marker==0xd8:corrupt=True;break
                if marker==0x01 or 0xd0<=marker<=0xd7:continue
                length=struct.unpack_from('>H',raw,pos)[0]
                if length<2 or pos+length>len(raw):corrupt=True;break
                data=raw[pos+2:pos+length];pos+=length
                # SOS begins entropy-coded bytes; FF00 is a stuffed data byte,
                # restart/TEM markers do not end that scan. Other markers are
                # parsed by their segment lengths, including APP1 after SOS.
                in_scan=marker==0xda or (in_scan and marker==0xdc)
                if marker in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf):
                    precision,height,width,components=struct.unpack_from('>BHHB',data)
                    if size is not None:corrupt=True
                    size=(width,height);mode_bad=precision!=8 or components!=3
                if marker==0xe1 and data.startswith(b'Exif\0\0'):exif.append(data[6:])
            if not ended:corrupt=True
    except (struct.error,IndexError):corrupt=True
    return size,mode_bad,_exif_orientation_invalid(exif),corrupt


def seedream_raster(raw,metadata,content_type,decoder):
    """Pure strict classification after complete capture, never local I/O errors."""
    keys(decoder,('pillow_version','jpeg_codec_version'),'SEEDREAM_DECODER_UNAVAILABLE')
    if decoder!=decoder_capabilities():fail('SEEDREAM_DECODER_CHANGED')
    result=dict(reason=None,raw_format=None,pixels=None)
    def bad(reason):return dict(result,reason=reason)
    fmt='PNG' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'JPEG' if raw.startswith(b'\xff\xd8') else None
    if fmt is None:return bad('RESULT_FORMAT_UNSUPPORTED')
    result['raw_format']=fmt
    size,mode_bad,orientation_bad,corrupt=_raster_headers(raw,fmt)
    if size is not None and size!=(2048,2048):return bad('RESULT_DIMENSIONS_INVALID')
    if mode_bad:return bad('RESULT_MODE_UNSUPPORTED')
    if orientation_bad:return bad('RESULT_ORIENTATION_INVALID')
    if corrupt:return bad('RESULT_IMAGE_CORRUPT')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with Image.open(io.BytesIO(raw)) as image:
                if image.size!=(2048,2048):return bad('RESULT_DIMENSIONS_INVALID')
                if image.mode!='RGB' or getattr(image,'n_frames',1)!=1:return bad('RESULT_MODE_UNSUPPORTED')
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load();pixels=image.tobytes()
    except (OSError,ValueError,SyntaxError,Warning):return bad('RESULT_IMAGE_CORRUPT')
    except (MemoryError,OverflowError):fail('SEEDREAM_LOCAL_RESOURCE_BLOCK')
    mime='image/png' if fmt=='PNG' else 'image/jpeg'
    for key,expected in (('width',2048),('height',2048),('file_size',len(raw))):
        if metadata.get(key) is not None and (type(metadata[key]) is not int or metadata[key]!=expected):return bad('RESULT_METADATA_CONFLICT')
    for key in ('file_name','content_type'):
        if metadata.get(key) is not None and not isinstance(metadata[key],str):return bad('RESULT_METADATA_CONFLICT')
    def token(value):
        if not isinstance(value,str) or ',' in value or '\r' in value or '\n' in value:return None
        return value.split(';',1)[0].strip().lower()
    if metadata.get('content_type') is not None and token(metadata['content_type'])!=mime:return bad('RESULT_METADATA_CONFLICT')
    if content_type is not None and token(content_type) not in (mime,'application/octet-stream'):return bad('RESULT_METADATA_CONFLICT')
    return dict(result,pixels=pixels)


def fail(code='ENDPOINT_EVIDENCE_INVALID'):
    raise WorkflowError(code)


def keys(value, names, code='ENDPOINT_EVIDENCE_INVALID'):
    if not isinstance(value,dict) or set(value)!=set(names): fail(code)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def checked_path(value, code='ENDPOINT_EVIDENCE_INVALID'):
    if not isinstance(value,str) or not value or len(value)>4096 or '\0' in value: fail(code)
    path=Path(value)
    if not path.is_absolute() or path.is_symlink() or path.absolute()!=path.resolve(): fail(code)
    return path


def file_bytes(path, maximum=5*1024*1024, code='ENDPOINT_EVIDENCE_INVALID',*,allow_empty=False):
    path=checked_path(str(path),code)
    if not path.is_file() or not (0 if allow_empty else 1)<=path.stat().st_size<=maximum: fail(code)
    return path.read_bytes()


def strict_json(payload, code='ENDPOINT_EVIDENCE_INVALID', *, decimals=False):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value: fail(code)
            value[key]=item
        return value
    def invalid(_): fail(code)
    try:
        return json.loads(payload.decode('utf-8'),object_pairs_hook=pairs,parse_constant=invalid,
                          **({'parse_float':Decimal} if decimals else {}))
    except (ValueError,TypeError,UnicodeError,RecursionError): fail(code)


def canonical_record(path, code='ENDPOINT_EVIDENCE_INVALID'):
    payload=file_bytes(path,code=code);value=strict_json(payload,code)
    if payload!=encoded(value): fail(code)
    return value


def node_config_sha(config):
    """Use the same fixed Node number/string serializer as the transport."""
    return node_local('digest',dict(config=config))


def node_local(command,value):
    executable=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    if not executable.is_file():fail('NODE_RUNTIME_REQUIRED')
    # Constant code and import only. No manifest-provided executable/commands,
    # preload hooks, credentials or network calls enter this subprocess.
    script="""import fs from 'node:fs';import{createHash}from'node:crypto';
const p=JSON.parse(fs.readFileSync(0,'utf8'));
const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
if(p.command==='digest')process.stdout.write(JSON.stringify(createHash('sha256').update(JSON.stringify(canonical(p.value.config),null,2)+'\\n').digest('hex')));
else{const m=await import(p.module);const v=p.value;const decode=x=>x===null?null:Buffer.from(x,'base64');
const result=p.command==='seedream-capture'?m.validateSeedreamCaptureEvidence(decode(v.config),decode(v.claim),decode(v.capture),decode(v.status),decode(v.result),decode(v.raw),v.expectedIdentity):p.command==='invalid-result'?m.validateEndpointInvalidResultEvidence(decode(v.config),decode(v.claim),decode(v.evidence),decode(v.status),decode(v.result),decode(v.output),decode(v.source),v.expected_request_id):m.validateEndpointPricingEvidence(v.model,decode(v.api),decode(v.public),decode(v.schema));process.stdout.write(JSON.stringify(result));}
"""
    env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','USERPROFILE','HOME','PATH') if k in os.environ}
    payload=encoded(dict(command=command,value=value,module=Path(__file__).with_name('fal_sheet_transport.mjs').as_uri()))
    try:result=subprocess.run([str(executable),'--input-type=module','-e',script],input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired):fail('ENDPOINT_LOCAL_VALIDATION_FAILED')
    if result.returncode or len(result.stdout)>65536 or len(result.stderr)>65536:fail('SEEDREAM_CAPTURE_INVALID' if command=='seedream-capture' else 'INVALID_RESULT_EVIDENCE_INVALID' if command=='invalid-result' else 'PRICE_RECEIPT_INVALID' if command!='digest' else 'ENDPOINT_LOCAL_VALIDATION_FAILED')
    return strict_json(result.stdout,decimals=command!='digest')


def economic_digest(context,config):
    # Per-validation memo of a pure serialization only. Every caller has just
    # reopened/parsed the actual file; no filesystem/evidence result is cached.
    memo=context.setdefault('_economic_digests',{})
    payload=encoded(config)
    if payload not in memo:memo[payload]=node_config_sha(config)
    return memo[payload]


def png(path, *, mode=None, size=None, maximum=20*1024*1024):
    try:
        payload=file_bytes(path,maximum)
        with Image.open(io.BytesIO(payload)) as image:
            if image.format!='PNG' or image.width*image.height>64_000_000 or (mode and image.mode!=mode) or (size and image.size!=size): fail()
            return image.convert('RGBA' if mode is None else mode)
    except (OSError,ValueError): fail()


def ref(path):
    return dict(path=str(path),sha256=sha_file(path))


def validate_ref(value):
    keys(value,('path','sha256'))
    if digest(file_bytes(checked_path(value['path']),100*1024*1024))!=value['sha256']: fail()
    return value


def validate_preparation(context):
    root=context['root'];snap=root/'snapshots';plan=context['plan']
    proof=canonical_record(snap/'preparation-proof.json')
    if seedream(context):
        keys(proof,('schema','policy','variant','transform','refs'))
        if (proof['schema']!='sprite_endpoint_preparation_v2' or proof['variant']!=SEEDREAM_VARIANT
                or proof['policy']!='pose_canvas_2048_v1' or proof['transform']!=TRANSFORM):fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        expected=dict(source_original=ref(snap/'source-original.png'),endpoint_a_original=ref(snap/'a-original.png'),
                      identity_prepared=ref(snap/'identity.png'),edit_prepared=ref(snap/'edit.png'),endpoint_a_prepared=ref(snap/'endpoint-a.png'))
        if proof['refs']!=expected:fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        for original,prepared in [('source-original.png','identity.png'),('a-original.png','endpoint-a.png')]:
            image=Image.new('RGBA',(2048,2048),(110,110,110,255));image.alpha_composite(png(snap/original,size=(1254,1254)),(397,397))
            if image.convert('RGB').tobytes()!=png(snap/prepared,mode='RGB',size=(2048,2048)).tobytes():fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        if (file_bytes(snap/'edit.png',20*1024*1024)!=file_bytes(snap/'endpoint-a.png',20*1024*1024)
                or file_bytes(snap/'source-original.png',20*1024*1024)!=file_bytes(root/'source.png',20*1024*1024)
                or plan['pose_inputs']!=[dict(role='edit',sha256=expected['edit_prepared']['sha256']),dict(role='identity',sha256=expected['identity_prepared']['sha256'])]
                or plan['endpoint_a_sha256']!=expected['endpoint_a_prepared']['sha256']):fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        return proof
    keys(proof,('schema','policy','transform','inputs'))
    if proof['schema']!='sprite_endpoint_preparation_v1' or proof['policy']!='pose_canvas_2048_v1' or proof['transform']!=TRANSFORM: fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    expected=[]
    for role,original,prepared in (('edit','b-candidate-original.png','edit.png'),('identity','source-original.png','identity.png'),('endpoint_a','a-original.png','endpoint-a.png')):
        original_path,prepared_path=snap/original,snap/prepared
        source=png(original_path,size=(1254,1254))
        image=Image.new('RGBA',(2048,2048),(110,110,110,255));image.alpha_composite(source,(397,397));image=image.convert('RGB')
        if png(prepared_path,mode='RGB',size=(2048,2048)).tobytes()!=image.tobytes(): fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        expected.append(dict(role=role,original=ref(original_path),prepared=ref(prepared_path)))
    if (proof['inputs']!=expected or file_bytes(snap/'source-original.png',20*1024*1024)!=file_bytes(root/'source.png',20*1024*1024)
            or plan['pose_inputs']!=[dict(role=e['role'],sha256=e['prepared']['sha256']) for e in expected[:2]]
            or plan['endpoint_a_sha256']!=expected[2]['prepared']['sha256']): fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    return proof


def load_context(run_manifest):
    path=checked_path(str(Path(run_manifest).expanduser().absolute()),'TASK_SCOPE_MISMATCH');root=path.parent
    installed=Path.home()/'.codex/skills'
    if root==installed or installed in root.parents: fail('WORKSPACE_NOT_ALLOWED')
    m=strict_json(file_bytes(path));scope=canonical_record(root/'scope.json','TASK_SCOPE_MISMATCH');q=m.get('request',{})
    if (path.name!='run-manifest.json' or m.get('schema_version')!=3 or m.get('mode')!='whole_sheet' or m.get('derivation')
            or m.get('run_id')!=root.name or any(m.get(k)!=scope.get(k) for k in ('run_id','source','request','authorization'))
            or set(scope)!=set(('run_id','source','request','authorization')) or m.get('authorization_scope_sha256')!=scope_sha(scope)
            or file_bytes(root/'scope.sha256',128).decode().strip()!=scope_sha(scope)
            or q.get('route')!='endpoint_cycle_v1' or q.get('strategy')!='endpoint_cycle_v1'
            or q.get('action')!='run' or q.get('frames')!=8 or q.get('frame_ms')!=100 or q.get('loop') is not True
            or q.get('canvas')!=[2048,2048] or q.get('columns')!=4 or q.get('rows')!=2
            or q.get('grounded') is not False or q.get('alignment_policy')!='body_x_preserve_y_v1'
            or type(q.get('pixel_art')) is not bool): fail('TASK_SCOPE_MISMATCH')
    if (m['source']['path']!=str(root/'source.png') or sha_file(root/'source.png')!=m['source']['sha256']): fail('SOURCE_TAMPERED')
    task_ref=q.get('task_ref');keys(task_ref,('schema','task_id','task_manifest','authorization_sha256','target_id'),'TASK_CONTEXT_REQUIRED')
    if task_ref['schema']!='sprite_task_ref_v1': fail('TASK_SCOPE_MISMATCH')
    checked_path(task_ref['task_manifest'],'TASK_SCOPE_MISMATCH')
    task=ledger.inspect_task(task_ref['task_manifest']);target=task['targets'].get(task_ref['target_id'])
    authorization=m['authorization']
    keys(authorization,('source','request_authorized','max_cost_usd','max_generation_attempts'),'TASK_SCOPE_MISMATCH')
    if (authorization['source']!='session_request' or authorization['request_authorized'] is not True
            or type(authorization['max_generation_attempts']) is not int or authorization['max_generation_attempts']!=(1 if q.get('effective_descriptor',{}).get('family')=='seedream45_wan_flf2v_cycle_v1' else 2)
            or isinstance(authorization['max_cost_usd'],bool) or not isinstance(authorization['max_cost_usd'],(int,float))
            or not math.isfinite(authorization['max_cost_usd']) or authorization['max_cost_usd']<=0
            or Decimal(str(authorization['max_cost_usd']))>Decimal(task['authorization']['cap_usd'])):fail('TASK_SCOPE_MISMATCH')
    bound=dict(run_id=m['run_id'],target_id=task_ref['target_id'],scope_sha256=scope_sha(scope),manifest_path=str(path))
    if (task['task_id']!=task_ref['task_id'] or task['authorization_sha256']!=task_ref['authorization_sha256'] or not target
            or task['runs'].get(m['run_id'])!=bound or target['source_sha256']!=m['source']['sha256']
            or target['action']!=q['action'] or target['orientation']!=q['orientation']
            or any(q.get(k)!=v for k,v in target['requirements'].items())): fail('TASK_SCOPE_MISMATCH')
    if not isinstance(m.get('attempts'),list) or len(m['attempts'])!=1 or [p.name for p in root.glob('attempt-*')]!=['attempt-01']: fail('TASK_PLAN_INVALID')
    a=m['attempts'][0];attempt_id=m['run_id']+':attempt-01'
    matches=[entry for entry in task['attempts'].values() if entry['run_id']==m['run_id'] and entry['attempt_id']==attempt_id]
    if (len(matches)!=1 or a.get('index')!=1 or a.get('attempt_id')!=attempt_id or a.get('strategy_sha256')!=matches[0]['strategy_sha256']
            or a.get('correction')!=matches[0].get('correction')): fail('TASK_PLAN_INVALID')
    current=matches[0];descriptor=task['strategies'][current['strategy_sha256']]['effective_descriptor']
    expected=dict(schema='sprite_strategy_descriptor_v2',route='endpoint_cycle_v1',family='nano2_wan_flf2v_cycle_v1',model=MODELS['video_ab'],conditioning='pose_edit_then_opposite_endpoints',inputs=[dict(role='source',sha256=m['source']['sha256'])])
    if descriptor.get('family')=='seedream45_wan_flf2v_cycle_v1':expected.update(family='seedream45_wan_flf2v_cycle_v1',conditioning='valid_pose_edit_then_opposite_endpoints')
    if descriptor!=expected or q.get('effective_descriptor')!=expected: fail('TASK_STRATEGY_MISMATCH')
    plan=canonical_record(root/'endpoint-plan.json','TASK_PLAN_INVALID')
    ledger._endpoint_plan(plan,current,target,seedream=descriptor['family']=='seedream45_wan_flf2v_cycle_v1')
    if current.get('endpoint_plan')!=plan or q.get('endpoint_plan_sha256')!=scope_sha(plan) or current.get('plan_operation_ids')!=[attempt_id+':'+r for r in ROLES]: fail('TASK_PLAN_INVALID')
    if not isinstance(a.get('jobs'),dict) or set(a['jobs'])-set(ROLES): fail('TASK_PLAN_INVALID')
    context=dict(path=path,root=root,manifest=m,scope=scope,ref=task_ref,ledger=task,attempt=a,current=current,plan=plan,plan_sha256=scope_sha(plan),attempt_dir=root/'attempt-01')
    validate_preparation(context)
    if current.get('correction') is not None:
        if q.get('correction_review_sha256')!=sha_file(context['attempt_dir']/'correction-review.json'):fail('ENDPOINT_CORRECTION_REVIEW_REQUIRED')
        validate_correction_review(context)
    elif 'correction_review_sha256' in q:fail('ENDPOINT_CORRECTION_REVIEW_REQUIRED')
    return context


def _payload_projection(config):
    return dict(model=config['model'],input=config['input'],input_files=[dict(role=i['role'],sha256=i['sha256']) for i in config['input_files']])


def _payload_changes(before,after,prefix=''):
    changes=[]
    for key in sorted(set(before)|set(after)):
        field=prefix+key;old=before.get(key);new=after.get(key)
        if isinstance(old,dict) and isinstance(new,dict):changes.extend(_payload_changes(old,new,field+'.'))
        elif old!=new:changes.append(dict(field=field,before=old,after=new))
    return changes


def validate_correction_review(context):
    current=context['current'];task=context['ledger'];strategy=task['strategies'][current['strategy_sha256']]
    prior=[task['attempts'][key] for key in strategy['attempt_keys'] if key!=operation(context,'pose_edit')['attempt_key']]
    if not prior:fail('ENDPOINT_CORRECTION_REVIEW_REQUIRED')
    report=validate_endpoint_correction_review(context['ref']['task_manifest'],prior[-1]['run_id'],
        context['attempt_dir']/'pose_edit/config.json',context['attempt_dir']/'correction-review.json')
    if report['defect']!=current['correction']['defect']:fail('ENDPOINT_CORRECTION_REVIEW_REQUIRED')
    return report


def validate_endpoint_correction_review(task_manifest,previous_run_id,pose_config_path,review_path):
    """Read-only directed-payload review preflight, usable before second begin.

    Human/agent observations establish semantic intent, not artistic effectiveness.
    """
    code='ENDPOINT_CORRECTION_REVIEW_REQUIRED';task=ledger.inspect_task(task_manifest)
    prior=[a for a in task['attempts'].values() if a['run_id']==previous_run_id]
    if len(prior)!=1 or prior[0].get('endpoint_status') not in ('failed','rejected'):fail(code)
    previous=prior[0];old_run=task['runs'][previous_run_id];old_root=checked_path(old_run['manifest_path']).parent
    new_config_path=checked_path(str(pose_config_path),code);review_path=checked_path(str(review_path),code)
    if new_config_path.parent.name!='pose_edit' or new_config_path.parent.parent.name!='attempt-01' or review_path!=new_config_path.parent.parent/'correction-review.json':fail(code)
    old_config_path=old_root/'attempt-01/pose_edit/config.json'
    old_config=strict_json(file_bytes(old_config_path));new_config=strict_json(file_bytes(new_config_path))
    old_hash=node_config_sha(old_config);new_hash=node_config_sha(new_config)
    if old_hash!=previous['endpoint_plan']['pose_config_sha256']:fail(code)
    auth=new_config.get('authorization',{}).get('task_context',{});ref_value=auth.get('task_ref',{})
    if (ref_value.get('task_manifest')!=str(task_manifest) or ref_value.get('task_id')!=task['task_id'] or ref_value.get('authorization_sha256')!=task['authorization_sha256']
            or ref_value.get('target_id')!=old_run['target_id'] or new_config.get('role')!='pose_edit' or new_config.get('model')!=MODELS['pose_edit']):fail(code)
    validate_input_options('pose_edit',new_config['input'])
    changes=_payload_changes(_payload_projection(old_config),_payload_projection(new_config))
    if not changes:fail(code)
    for change in changes:
        if change['field']=='input.prompt' and ' '.join(change['before'].split())==' '.join(change['after'].split()):fail(code)
    review=canonical_record(review_path,code)
    keys(review,('schema','previous_run_id','previous_attempt_id','previous_pose_config_sha256','pose_config_sha256','defect','evidence','changed_fields','decision','notes','correction_kind','changes'),code)
    kind='prompt_semantic_change' if all(c['field']=='input.prompt' for c in changes) else 'input_change'
    if (review['schema']!='sprite_endpoint_correction_review_v1' or review['decision']!='pass' or review['correction_kind']!=kind
            or review['previous_run_id']!=previous['run_id'] or review['previous_attempt_id']!=previous['attempt_id']
            or review['previous_pose_config_sha256']!=old_hash or review['pose_config_sha256']!=new_hash
            or review['changed_fields']!=[c['field'] for c in changes] or not isinstance(review['changes'],list) or len(review['changes'])!=len(changes)):fail(code)
    for actual,change in zip(review['changes'],changes):
        keys(actual,('field','before','after','defect_effect'),code)
        if any(actual[k]!=v for k,v in change.items()) or not isinstance(actual['defect_effect'],str) or len(actual['defect_effect'].strip())<20:fail(code)
    if not isinstance(review['notes'],str) or len(review['notes'].strip())<40 or not isinstance(review['defect'],str) or len(review['defect'].strip())<10:fail(code)
    if not isinstance(review['evidence'],list):fail(code)
    for evidence in review['evidence']:validate_ref(evidence)
    if ref(old_config_path) not in review['evidence'] or ref(new_config_path) not in review['evidence']:fail(code)
    rejection_refs=[e for gate in previous.get('endpoint_gate_history',[]) if gate['decision']=='reject' for e in gate['evidence']]
    final=previous.get('review') or {}
    if final.get('decision')=='reject':
        match=re.match(r'^(.*?)#sha256=([a-f0-9]{64});artifact_set=',final.get('evidence',''))
        if match:rejection_refs.append(dict(path=match[1],sha256=match[2]))
    terminal_refs=[];invalid_reasons=[]
    for operation_id in previous.get('plan_operation_ids',[]):
        op=task['operations'][operation_id];role=operation_id.rsplit(':',1)[1]
        for outcome in op['outcomes']:
            if outcome['status'] not in ('failed','cancelled','rejected'):continue
            match=re.fullmatch(r'(.*?)#sha256=([a-f0-9]{64})',outcome.get('evidence',''))
            if not match:continue
            evidence=dict(path=match[1],sha256=match[2]);validate_ref(evidence)
            actual_path=checked_path(evidence['path'])
            if actual_path==old_root/'attempt-01'/role/'result-invalid.json':
                verified=validate_endpoint_invalid_result(old_run['manifest_path'],role)
                if (outcome['status']!='failed' or verified['evidence_ref']!=evidence or verified['request_id']!=op['provider_request_id']
                        or verified['config_sha256']!=op['execution_binding']['config_sha256']):fail(code)
                terminal_refs.append(evidence);invalid_reasons.append(verified['reason']);continue
            if actual_path!=old_root/'attempt-01'/role/'job-state.json':fail(code)
            state=strict_json(file_bytes(evidence['path']))
            expected_state=dict(failed='FAILED',cancelled='CANCELLED',rejected='REMOTE_REJECTED')[outcome['status']]
            if (state.get('state')!=expected_state or state.get('request_id')!=op['provider_request_id'] or not op['provider_request_id']
                    or state.get('config_sha256')!=op['execution_binding']['config_sha256']):fail(code)
            terminal_refs.append(evidence)
    if not any(e in review['evidence'] for e in rejection_refs+terminal_refs):fail(code)
    defects=[d for gate in previous.get('endpoint_gate_history',[]) if gate['decision']=='reject' for d in gate['defects']]+final.get('defects',[])
    if defects and review['defect'] not in defects:fail(code)
    if not defects and not any(e in review['evidence'] for e in terminal_refs):fail(code)
    if not defects and invalid_reasons and not any(review['defect']==reason or review['defect'].startswith(reason+':') for reason in invalid_reasons):fail(code)
    return review


def validate_gate_report(gate):
    keys(gate,('schema','stage','decision','subject','defects','evidence'))
    stage=gate['stage']
    if gate['schema'] not in ('sprite_endpoint_gate_v1','sprite_endpoint_gate_v2') or stage not in GATE_CHECKS or not isinstance(gate['evidence'],list): fail('TASK_ENDPOINT_GATE_CONFLICT')
    report_schema='sprite_endpoint_gate_report_v2' if gate['schema']=='sprite_endpoint_gate_v2' else 'sprite_endpoint_gate_report_v1'
    reports=[]
    for evidence in gate['evidence']:
        validate_ref(evidence)
        if Path(evidence['path']).suffix=='.json':
            value=strict_json(file_bytes(evidence['path']))
            if isinstance(value,dict) and value.get('schema')==report_schema: reports.append((evidence,value))
    if len(reports)!=1: fail('TASK_ENDPOINT_GATE_REQUIRED')
    report_ref,report=reports[0];keys(report,('schema','stage','subject','decision','checks','notes','evidence'))
    if (any(report[k]!=gate[k] for k in ('stage','subject','decision')) or not isinstance(report['notes'],str) or not report['notes'].strip()
            or report['evidence']!=[e for e in gate['evidence'] if e!=report_ref]): fail('TASK_ENDPOINT_GATE_CONFLICT')
    checks=report['checks'];keys(checks,GATE_CHECKS[stage])
    for check in checks.values():
        keys(check,('status','notes'))
        if check['status'] not in ('pass','hold','fail') or not isinstance(check['notes'],str) or not check['notes'].strip(): fail('TASK_ENDPOINT_GATE_CONFLICT')
    statuses={check['status'] for check in checks.values()}
    decision='reject' if 'fail' in statuses else 'hold' if 'hold' in statuses else 'pass'
    defects=[name+':'+checks[name]['notes'].strip() for name in sorted(checks) if checks[name]['status']=='fail']
    if decision!=gate['decision'] or defects!=gate['defects']: fail('TASK_ENDPOINT_GATE_CONFLICT')
    return report


def gate_for(context, stage, *, gate_sha=None, live=True):
    current=context['current'];gate=current.get('endpoint_gates',{}).get(stage)
    if not live:
        candidates=[g for g in current.get('endpoint_gate_history',[]) if g['stage']==stage and scope_sha(g)==gate_sha]
        if not candidates: fail('TASK_ENDPOINT_GATE_REQUIRED')
        gate=candidates[0]
    if not gate or gate['decision']!='pass' or (gate_sha and scope_sha(gate)!=gate_sha): fail('TASK_ENDPOINT_GATE_REQUIRED')
    if gate['schema']!=('sprite_endpoint_gate_v2' if seedream(context) else 'sprite_endpoint_gate_v1'):fail('TASK_ENDPOINT_GATE_CONFLICT')
    validate_gate_report(gate)
    if seedream(context) and stage=='pair_ready':
        pose=validate_job(context,'pose_edit',completed=True,live=live)
        if gate['subject']!=pair_subject(context,pose) or any(pose['seedream_pose'][name] not in gate['evidence'] for name in ('capture_ref','raw_ref','png_ref','output_proof_ref')):fail('TASK_ENDPOINT_GATE_CONFLICT')
    if live:
        if stage!='input_ready': gate_for(context,'input_ready')
        if stage in ('ab_ready','cycle_ready'): gate_for(context,'pair_ready',gate_sha=gate['subject']['pair_gate_sha256'])
        if stage=='cycle_ready': gate_for(context,'ab_ready',gate_sha=gate['subject']['ab_gate_sha256'])
    return gate


def operation(context,role):
    return context['ledger']['operations'][context['attempt']['attempt_id']+':'+role]


def validate_input_options(role,value,*,variant=None):
    expected=dict(sync_mode=False) if role=='cleanup' else dict(resolution='2K',aspect_ratio='1:1',num_images=1,output_format='png',limit_generations=True,enable_web_search=False,sync_mode=False,safety_tolerance='4') if role=='pose_edit' else dict(frames=81,fps=24,resolution='720p',aspect_ratio='1:1',steps=30,acceleration='none',prompt_expansion=False,enable_safety_checker=True)
    if variant is not None:
        if variant!=SEEDREAM_VARIANT:fail('CONFIG_INVALID')
        if role=='pose_edit':expected=dict(image_size=dict(width=2048,height=2048),num_images=1,max_images=1,sync_mode=False,enable_safety_checker=True)
    keys(value,(*expected,*(('prompt',) if role!='cleanup' else ())))
    if role!='cleanup' and (not isinstance(value['prompt'],str) or not 3<=len(value['prompt'].strip())<=10000): fail('CONFIG_INVALID')
    if any(type(value[k]) is not type(v) or value[k]!=v for k,v in expected.items()): fail('CONFIG_INVALID')


def validate_job(context,role,*,completed=False,live=True,recovery=False):
    if role not in ROLES: fail('TASK_SCOPE_MISMATCH')
    root=context['root'];directory=context['attempt_dir']/role;op=operation(context,role)
    job=context['attempt']['jobs'].get(role)
    if not job or job.get('path')!=str(directory) or job.get('operation_id')!=op['operation_id']: fail('TASK_SCOPE_MISMATCH')
    config_path=directory/'config.json';config=strict_json(file_bytes(config_path))
    variant=SEEDREAM_VARIANT if seedream(context) else None
    keys(config,('schema_version','route','role','model','input_files','input','authorization','max_cost_usd',*(('variant',) if variant else ())), 'TASK_SCOPE_MISMATCH')
    expected_auth=dict(request_authorized=True,task_context=dict(task_ref=context['ref'],run_manifest=str(context['path']),run_id=context['manifest']['run_id'],attempt_id=context['attempt']['attempt_id'],operation_id=op['operation_id']))
    if (type(config['schema_version']) is not int or config['schema_version']!=(5 if variant else 4) or (variant and config['variant']!=variant) or config['route']!='endpoint_cycle_v1' or config['role']!=role
            or config['model']!=model_for(context,role) or config['authorization']!=expected_auth or isinstance(config['max_cost_usd'],bool)
            or not isinstance(config['max_cost_usd'],(int,float)) or not math.isfinite(config['max_cost_usd'])
            or config['max_cost_usd']!=context['manifest']['authorization']['max_cost_usd']): fail('TASK_SCOPE_MISMATCH')
    validate_input_options(role,config['input'],variant=variant)
    node_sha=economic_digest(context,config);config_file_sha=sha_file(config_path);binding=op.get('execution_binding')
    if (job.get('config_sha256')!=scope_sha(config) or job.get('transport_config_sha256')!=node_sha or job.get('config_file_sha256')!=config_file_sha
            or not binding or op.get('binding_sha256')!=scope_sha(binding)): fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    # A durable Node ID can precede its ledger/mirror projection. Verify the
    # identity/claim before deciding whether live quality gates apply. This
    # exception is available only to the transport recovery boundary; completed
    # dependency/artifact validation retains its reconciled-ledger requirements.
    state_path=directory/'job-state.json';state=strict_json(file_bytes(state_path)) if state_path.exists() else None
    node_id=state.get('request_id') if state else None;ledger_id=op.get('provider_request_id')
    if state and (state.get('schema_version')!=3 or state.get('config_sha256')!=node_sha): fail('TASK_CONTEXT_CONFLICT')
    if node_id is not None:
        if (not isinstance(node_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',node_id)
                or not op['submission_started'] or (ledger_id and node_id!=ledger_id)): fail('TASK_CONTEXT_CONFLICT')
        ledger._request_unique(context['ledger'],node_id,op['operation_id'])
    if job.get('request_id') and job['request_id']!=(node_id or ledger_id): fail('TASK_CONTEXT_CONFLICT')
    claim_path=directory/'submission.claim'
    if node_id and not claim_path.exists(): fail('TASK_CONTEXT_CONFLICT')
    if claim_path.exists():
        claim=strict_json(file_bytes(claim_path));keys(claim,('schema_version','config_sha256','pricing_receipt_sha256','created_at'))
        if (claim['schema_version']!=3 or claim['config_sha256']!=node_sha or not state
                or claim['pricing_receipt_sha256']!=state.get('pricing_receipt_sha256')
                or claim['pricing_receipt_sha256']!=op['current_quote']['quote_sha256'] or not op['submission_started']): fail('TASK_CONTEXT_CONFLICT')
        try: datetime.fromisoformat(claim['created_at'].replace('Z','+00:00'))
        except (ValueError,TypeError,AttributeError): fail('TASK_CONTEXT_CONFLICT')
    recovery_id=(node_id or ledger_id) if recovery else None
    if recovery_id:live=False
    snap=root/'snapshots';a=snap/'endpoint-a.png';b=context['attempt_dir']/('pose_edit/endpoint-b.png' if variant else 'pose_edit/raw.png')
    paths=[snap/'edit.png',snap/'identity.png'] if role=='pose_edit' else [context['attempt_dir']/'cycle-grid.png'] if role=='cleanup' else [a,b] if role=='video_ab' else [b,a]
    names=['edit','identity'] if role=='pose_edit' else ['grid'] if role=='cleanup' else ['start','end']
    inputs=[dict(role=name,path=str(path),sha256=sha_file(path)) for name,path in zip(names,paths)]
    if config['input_files']!=inputs or (len(inputs)==2 and inputs[0]['sha256']==inputs[1]['sha256']): fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    for path in paths: png(path,size=None if role=='cleanup' else (2048,2048),maximum=10485760 if role=='cleanup' else 20*1024*1024)
    ordered=[dict(role=i['role'],sha256=i['sha256']) for i in inputs]
    if canonical_record(directory/'inputs.json')!=ordered: fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    provenance=canonical_record(directory/'source-provenance.json')
    keys(provenance,('schema','kind','original_source_sha256','input_set_sha256','endpoint_plan_sha256','evidence'))
    e=provenance['evidence'];kind='pose_edit_inputs' if role=='pose_edit' else 'two_video_full_range_grid' if role=='cleanup' else 'ordered_endpoint_pair'
    if (provenance['schema']!=('sprite_endpoint_provenance_v2' if variant else 'sprite_endpoint_provenance_v1') or provenance['kind']!=kind or provenance['original_source_sha256']!=context['plan']['source_sha256']
            or provenance['input_set_sha256']!=scope_sha(ordered) or provenance['endpoint_plan_sha256']!=context['plan_sha256']): fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    expected_binding=dict(schema='sprite_execution_binding_v2',config_sha256=node_sha,config_file_sha256=config_file_sha,input_set_sha256=scope_sha(ordered),provenance_sha256=sha_file(directory/'source-provenance.json'),deferred_plan_sha256=scope_sha(op['deferred_config']) if op.get('deferred_config') else None)
    if binding!=expected_binding: fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    if role=='pose_edit':
        expected=pose_provenance(context) if variant else dict(preparation_proof_sha256=sha_file(snap/'preparation-proof.json'),original_ref=ref(snap/'source-original.png'),b_candidate_ref=ref(snap/'b-candidate-original.png'))
        if node_sha!=context['plan']['pose_config_sha256'] or ordered!=context['plan']['pose_inputs']: fail('TASK_PLAN_INVALID')
    elif role in ('video_ab','video_ba'):
        pose=validate_job(context,'pose_edit',completed=True,live=live);pair=gate_for(context,'pair_ready',gate_sha=e.get('pair_gate_sha256'),live=live)
        expected=dict(pose_operation_id=pose['operation']['operation_id'],pose_request_id=pose['operation']['provider_request_id'],pose_raw_sha256=pose['raw_sha256'],pair_gate_sha256=scope_sha(pair),direction='ab' if role=='video_ab' else 'ba')
        if variant:expected.update(pose_output_proof_sha256=pose['seedream_pose']['output_proof_ref']['sha256'],endpoint_b_sha256=pose['seedream_pose']['png_ref']['sha256'])
        subject=pair['subject']
        if subject!=pair_subject(context,pose): fail('TASK_ENDPOINT_GATE_CONFLICT')
    else:
        ab=validate_job(context,'video_ab',completed=True,live=live);ba=validate_job(context,'video_ba',completed=True,live=live)
        cycle=gate_for(context,'cycle_ready',gate_sha=e.get('cycle_gate_sha256'),live=live)
        selection=validate_cycle_evidence(context)
        expected=dict(ab_operation_id=ab['operation']['operation_id'],ab_request_id=ab['operation']['provider_request_id'],ab_raw_sha256=ab['raw_sha256'],ba_operation_id=ba['operation']['operation_id'],ba_request_id=ba['operation']['provider_request_id'],ba_raw_sha256=ba['raw_sha256'],cycle_gate_sha256=scope_sha(cycle),selection_sha256=sha_file(context['attempt_dir']/'selection.json'),grid_sha256=selection['grid_sha256'])
        subject=cycle['subject']
        if any(subject[k]!=expected[k] for k in ('ab_request_id','ab_raw_sha256','ba_request_id','ba_raw_sha256','selection_sha256','grid_sha256')): fail('TASK_ENDPOINT_GATE_CONFLICT')
    if e!=expected: fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    if variant and role=='pose_edit':
        pose=validate_seedream_pose(context,config,state,job,completed=completed,recovery=recovery)
        return dict(config=config,operation=op,job=job,raw_path=Path(pose['raw_ref']['path']) if pose['raw_ref'] else None,raw_sha256=pose['raw_ref']['sha256'] if pose['raw_ref'] else None,recovery_request_id=recovery_id,seedream_pose=pose)
    if completed or (state and state.get('state')=='DOWNLOADED'):
        raw=directory/('raw.mp4' if role in ('video_ab','video_ba') else 'raw.png');raw_sha=digest(file_bytes(raw,100*1024*1024))
        if (not claim_path.exists() or not state or state.get('state')!='DOWNLOADED' or not node_id
                or state.get('raw_path')!=str(raw) or state.get('raw_sha256')!=raw_sha): fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        if recovery_id and not completed:
            if any(job.get(k) not in (None,v) for k,v in dict(raw_path=str(raw),raw_sha256=raw_sha).items()): fail('TASK_CONTEXT_CONFLICT')
        elif (not ledger_id or not any(o['status']=='completed' for o in op['outcomes'])
                or any(job.get(k)!=v for k,v in dict(state='DOWNLOADED',request_id=ledger_id,raw_path=str(raw),raw_sha256=raw_sha).items())): fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        if role=='pose_edit': png(raw,size=(2048,2048))
        return dict(config=config,operation=op,job=job,raw_path=raw,raw_sha256=raw_sha,recovery_request_id=recovery_id)
    return dict(config=config,operation=op,job=job,recovery_request_id=recovery_id)


def pose_provenance(context):
    snap=context['root']/'snapshots'
    return dict(preparation_proof_sha256=sha_file(snap/'preparation-proof.json'),original_ref=ref(snap/'source-original.png'),a_candidate_ref=ref(snap/'a-original.png'))


def pair_subject(context,pose):
    value=dict(endpoint_plan_sha256=context['plan_sha256'],pose_request_id=pose['operation']['provider_request_id'],pose_raw_sha256=pose['raw_sha256'],endpoint_a_sha256=context['plan']['endpoint_a_sha256'],endpoint_b_sha256=pose['raw_sha256'])
    if seedream(context):
        value.update(pose_output_proof_sha256=pose['seedream_pose']['output_proof_ref']['sha256'],endpoint_b_sha256=pose['seedream_pose']['png_ref']['sha256'])
        if value['endpoint_b_sha256']==value['endpoint_a_sha256']:fail('TASK_ENDPOINT_GATE_CONFLICT')
    return value


def seedream_pose_state(phase='pending',**refs):
    return dict(schema='sprite_seedream_pose_state_v1',phase=phase,**{name:refs.get(name) for name in ('capture_ref','raw_ref','png_ref','output_proof_ref','invalid_proof_ref')})


def _seedream_capture(context,config,state):
    import base64
    code='SEEDREAM_CAPTURE_INVALID';directory=context['attempt_dir']/'pose_edit';op=operation(context,'pose_edit');binding=op['execution_binding']
    if not state or state.get('state')!='SEEDREAM_CAPTURED':fail(code)
    capture_path=directory/'capture/complete.json';capture=canonical_record(capture_path,code)
    download=capture.get('download');raw_path=directory/'provider-raw.bin';raw=file_bytes(raw_path,50*1024*1024,code,allow_empty=True) if download is not None else None
    expected=dict(variant=SEEDREAM_VARIANT,attempt_id=context['attempt']['attempt_id'],operation_id=op['operation_id'],request_id=state['request_id'],
                  config_sha256=binding['config_sha256'],config_file_sha256=binding['config_file_sha256'],input_set_sha256=binding['input_set_sha256'],binding_sha256=op['binding_sha256'],claim_sha256=sha_file(directory/'submission.claim'))
    data=dict(config=file_bytes(directory/'config.json'),claim=file_bytes(directory/'submission.claim'),capture=file_bytes(capture_path),
              status=file_bytes(directory/'capture/status.body'),result=file_bytes(directory/'capture/result.body',32*1024*1024),raw=raw)
    args={name:base64.b64encode(value).decode('ascii') if value is not None else None for name,value in data.items()}
    checked=node_local('seedream-capture',dict(args,expectedIdentity=expected))
    capture_ref=ref(capture_path);raw_ref=ref(raw_path) if raw is not None else None
    if (checked!=dict(capture_sha256=capture_ref['sha256'],request_id=state['request_id'],raw_sha256=raw_ref['sha256'] if raw_ref else None,result_class=checked.get('result_class'))
            or checked['result_class'] not in ('one_image','count','no_media')
            or state.get('seedream_capture')!=dict(schema='sprite_seedream_capture_state_v1',capture_ref=capture_ref,raw_ref=raw_ref)
            or state.get('raw_path')!=(str(raw_path) if raw is not None else None) or state.get('raw_sha256')!=(raw_ref['sha256'] if raw_ref else None)):
        fail(code)
    # Fixed paths are independently enforced here, in addition to B's byte-only
    # helper. Ref paths cannot choose where C reads a provider response.
    if (capture['status']['body_ref']!=ref(directory/'capture/status.body') or capture['result']['body_ref']!=ref(directory/'capture/result.body')
            or (download is not None and download['raw_ref']!=raw_ref) or (raw is None and raw_path.exists())):fail(code)
    return dict(capture=capture,capture_ref=capture_ref,raw_ref=raw_ref,raw=raw,result=strict_json(data['result']),result_class=checked['result_class'])


def _seedream_derived(context,config,state,captured):
    base=dict(variant=SEEDREAM_VARIANT,attempt_id=context['attempt']['attempt_id'],operation_id=operation(context,'pose_edit')['operation_id'],request_id=state['request_id'],config_sha256=economic_digest(context,config),capture_ref=captured['capture_ref'])
    reason={'count':'RESULT_IMAGE_COUNT','no_media':'RESULT_NO_MEDIA'}.get(captured['result_class']);decoder=None;raster=None
    if reason is None:
        decoder=context['plan']['pose_decoder'];raster=seedream_raster(captured['raw'],captured['result']['images'][0],captured['capture']['download']['content_type'],decoder)
        reason=raster['reason']
    if reason is not None:return dict(invalid=dict(schema='sprite_seedream_invalid_output_v1',**base,reason=reason,decoder=decoder),output=None,png=None)
    pixels=raster['pixels'];image=Image.frombytes('RGB',(2048,2048),pixels);buffer=io.BytesIO();image.save(buffer,format='PNG');payload=buffer.getvalue()
    with Image.open(io.BytesIO(payload)) as reopened:
        reopened.load()
        if reopened.mode!='RGB' or reopened.size!=(2048,2048) or reopened.tobytes()!=pixels:fail('SEEDREAM_LOCAL_DERIVATION_FAILED')
    png_ref=dict(path=str(context['attempt_dir']/'pose_edit/endpoint-b.png'),sha256=digest(payload))
    output=dict(schema='sprite_seedream_pose_output_v1',**base,raw_ref=captured['raw_ref'],raw_format=raster['raw_format'],png_ref=png_ref,width=2048,height=2048,mode='RGB',pixel_sha256=digest(pixels),decoder=decoder,transform='decode_rgb8_no_orientation_no_resize_v1')
    return dict(invalid=None,output=output,png=payload)


def validate_seedream_pose(context,config,state,job,*,completed=False,recovery=False):
    """Read only. Missing derived files remain recoverable, never created here."""
    directory=context['attempt_dir']/'pose_edit';op=operation(context,'pose_edit');mirror=job.get('seedream_pose')
    keys(mirror,('schema','phase','capture_ref','raw_ref','png_ref','output_proof_ref','invalid_proof_ref'),'SEEDREAM_POSE_STATE_INVALID')
    if mirror['schema']!='sprite_seedream_pose_state_v1' or mirror['phase'] not in ('pending','raw_captured','png_ready','invalid'):fail('SEEDREAM_POSE_STATE_INVALID')
    if not state or state.get('state')!='SEEDREAM_CAPTURED':
        if (completed or mirror!=seedream_pose_state() or (state and ('seedream_capture' not in state or state['seedream_capture'] is not None or state.get('raw_path') is not None or state.get('raw_sha256') is not None))
                or job.get('raw_path') is not None or job.get('raw_sha256') is not None):fail('SEEDREAM_POSE_STATE_INVALID')
        if state and state.get('state') in ('DOWNLOADED','RESULT_INVALID','FAILED','CANCELLED','REMOTE_REJECTED'):fail('SEEDREAM_RESULT_UNRECONCILED')
        return mirror
    captured=_seedream_capture(context,config,state);derived=_seedream_derived(context,config,state,captured)
    output_path=directory/'pose-output-proof.json';invalid_path=directory/'pose-invalid-proof.json';png_path=directory/'endpoint-b.png'
    if output_path.exists() and invalid_path.exists():fail('SEEDREAM_PROOF_CONFLICT')
    phase=seedream_pose_state('raw_captured',capture_ref=captured['capture_ref'],raw_ref=captured['raw_ref'])
    if derived['invalid'] is not None:
        if png_path.exists() or output_path.exists():fail('SEEDREAM_PROOF_CONFLICT')
        if invalid_path.exists():
            if canonical_record(invalid_path)!=derived['invalid']:fail('SEEDREAM_PROOF_CONFLICT')
            phase=seedream_pose_state('invalid',capture_ref=captured['capture_ref'],raw_ref=captured['raw_ref'],invalid_proof_ref=ref(invalid_path))
    else:
        if invalid_path.exists():fail('SEEDREAM_PROOF_CONFLICT')
        if png_path.exists() and file_bytes(png_path,50*1024*1024)!=derived['png']:fail('SEEDREAM_PNG_TAMPERED')
        if output_path.exists():
            if not png_path.exists() or canonical_record(output_path)!=derived['output']:fail('SEEDREAM_PROOF_CONFLICT')
            phase=seedream_pose_state('png_ready',capture_ref=captured['capture_ref'],raw_ref=captured['raw_ref'],png_ref=ref(png_path),output_proof_ref=ref(output_path))
    if completed:
        proof=phase['output_proof_ref'];evidence='pose-output-proof.json#sha256='+proof['sha256'] if proof else None
        if (phase['phase']!='png_ready' or not op.get('provider_request_id') or not any(o['status']=='completed' and o['provider_request_id']==state['request_id'] and o.get('evidence')==evidence for o in op['outcomes'])):fail('SEEDREAM_POSE_NOT_READY')
    # Recovery may repair an absent/older mirror only. Existing populated refs
    # and terminal phases can never be replaced by a different projection.
    if recovery and not completed:
        order={'pending':0,'raw_captured':1,'png_ready':2,'invalid':2}
        if order[mirror['phase']]>order[phase['phase']] or any(value is not None and value!=phase[name] for name,value in mirror.items() if name.endswith('_ref')):fail('SEEDREAM_POSE_STATE_INVALID')
    elif mirror!=phase:fail('SEEDREAM_POSE_STATE_INVALID')
    if completed and (job.get('state')!='DOWNLOADED' or job.get('request_id')!=state['request_id']
            or job.get('raw_path')!=state['raw_path'] or job.get('raw_sha256')!=state['raw_sha256']):fail('SEEDREAM_POSE_STATE_INVALID')
    terminals=[o for o in op['outcomes'] if o['status'] in ('completed','failed','rejected','cancelled')]
    if terminals and ((phase['phase']=='png_ready' and terminals[-1]['status']!='completed') or (phase['phase']=='invalid' and terminals[-1]['status']!='failed') or phase['phase']=='raw_captured'):fail('SEEDREAM_TERMINAL_CONFLICT')
    return phase


def _seedream_sync_directory(directory):
    try:descriptor=os.open(directory,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    except OSError as error:
        # Windows does not expose a POSIX directory descriptor through os.open.
        # Files themselves are flushed before the atomic hard-link publication.
        if os.name=='nt' and error.errno in (errno.EACCES,errno.EPERM,errno.EISDIR):return
        raise
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def seedream_publish(path,payload):
    """Publish only a complete new Seedream artifact; never replace a final."""
    code='SEEDREAM_PUBLICATION_CONFLICT';path=checked_path(str(path),code)
    def identical():
        if file_bytes(path,max(len(payload),1),code,allow_empty=True)!=payload:fail(code)
    if path.exists():
        identical();_seedream_sync_directory(path.parent);return
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.partial')
    try:
        with temporary.open('xb') as stream:
            if stream.write(payload)!=len(payload):raise OSError('Incomplete Seedream local write')
            stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,path)
        except FileExistsError:identical()
        _seedream_sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_pose_output(run_manifest):
    """Explicit local durable conversion; no network or financial mutation."""
    path=checked_path(str(Path(run_manifest).absolute()))
    with run_lock(path.parent):
        context=load_context(path)
        if not seedream(context):fail('SEEDREAM_VARIANT_REQUIRED')
        verified=validate_job(context,'pose_edit',recovery=True);directory=context['attempt_dir']/'pose_edit';job=verified['job']
        state=strict_json(file_bytes(directory/'job-state.json'));captured=_seedream_capture(context,verified['config'],state);derived=_seedream_derived(context,verified['config'],state,captured)
        if derived['invalid'] is not None:seedream_publish(directory/'pose-invalid-proof.json',encoded(derived['invalid']))
        else:
            seedream_publish(directory/'endpoint-b.png',derived['png'])
            seedream_publish(directory/'pose-output-proof.json',encoded(derived['output']))
        phase=validate_seedream_pose(context,verified['config'],state,job,recovery=True)
        job.update(seedream_pose=phase,state='DOWNLOADED' if phase['phase']=='png_ready' else 'RESULT_INVALID',request_id=state['request_id'],raw_path=state['raw_path'],raw_sha256=state['raw_sha256'])
        atomic_json(path,context['manifest'])
        return dict(state=job['state'],request_id=state['request_id'],raw_path=state['raw_path'],raw_sha256=state['raw_sha256'],seedream_pose=phase)


def fixed_ffmpeg(context):
    executable=resolve_ffmpeg()
    if context['manifest']['request'].get('ffmpeg_exe')!=executable: fail('RUNTIME_REQUIRED')
    return executable


def validate_endpoint_invalid_result(run_manifest,role):
    """Verify one completed-invalid result without mutating the task or state.

    The known ID comes from the verified durable job/ledger, never the proof.
    A proof committed before its Node state transition remains recoverable.
    """
    code='INVALID_RESULT_EVIDENCE_INVALID'
    if role not in ROLES:fail(code)
    context=load_context(run_manifest);directory=context['attempt_dir']/role
    state_path=directory/'job-state.json';state_bytes=file_bytes(state_path);state=strict_json(state_bytes)
    if state.get('state') not in ('RESULT_READY','COMPLETED','RESULT_INVALID'):fail(code)
    raw_name='raw.mp4' if role in ('video_ab','video_ba') else 'raw.png'
    if (directory/raw_name).exists() or state.get('raw_path') is not None or state.get('raw_sha256') is not None:fail(code)
    job=validate_job(context,role,recovery=True);request_id=job['recovery_request_id']
    if not request_id:fail(code)
    op=operation(context,role);binding=op['execution_binding'];proof_path=directory/'result-invalid.json';proof_bytes=file_bytes(proof_path);proof=strict_json(proof_bytes)
    if proof_bytes!=encoded(proof):fail(code)
    proof_sha=digest(proof_bytes)
    if state.get('result_invalid_sha256') not in (None,proof_sha):fail(code)
    if state.get('result_invalid_reason') not in (None,proof.get('reason')):fail(code)
    if state['state']=='RESULT_INVALID' and (state.get('result_invalid_sha256')!=proof_sha or state.get('result_invalid_reason')!=proof.get('reason')):fail(code)
    evidence_dir=directory/'result-evidence'
    def body(name,entry,suffix,maximum):
        if not isinstance(entry,dict) or not isinstance(entry.get('body_sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',entry['body_sha256']):fail(code)
        payload=file_bytes(evidence_dir/(name+'-'+entry['body_sha256']+suffix),maximum,code,allow_empty=name=='output')
        if digest(payload)!=entry['body_sha256']:fail(code)
        return payload
    result_limit=32*1024*1024 if proof.get('schema')=='sprite_endpoint_invalid_result_v2' else 5*1024*1024
    status=body('status',proof.get('status'),'.body.json',5*1024*1024);result=body('result',proof.get('result'),'.body.json',result_limit)
    output=None if proof.get('download') is None else body('output',proof['download'],'.bin',100*1024*1024)
    source=file_bytes(context['attempt_dir']/'cycle-grid.png',10485760,code) if role=='cleanup' else None
    import base64
    as_base64=lambda value:base64.b64encode(value).decode('ascii') if value is not None else None
    config_bytes=file_bytes(directory/'config.json');claim_bytes=file_bytes(directory/'submission.claim')
    payload=dict(config=as_base64(config_bytes),claim=as_base64(claim_bytes),evidence=as_base64(proof_bytes),status=as_base64(status),result=as_base64(result),output=as_base64(output),source=as_base64(source),expected_request_id=request_id)
    verified=node_local('invalid-result',payload)
    keys(verified,('state','request_id','operation_id','role','reason','evidence_sha256','config_sha256','config_file_sha256','input_set_sha256','claim_sha256'),code)
    if (verified['state']!='RESULT_INVALID' or verified['request_id']!=request_id or verified['role']!=role or verified['operation_id']!=op['operation_id']
            or any(verified[name]!=binding[name] for name in ('config_sha256','config_file_sha256','input_set_sha256'))
            or verified['claim_sha256']!=digest(claim_bytes) or verified['evidence_sha256']!=proof_sha):fail(code)
    current=ledger.inspect_task(context['ref']['task_manifest'])['operations'][op['operation_id']]
    if current.get('provider_request_id') not in (None,request_id) or current.get('binding_sha256')!=op['binding_sha256'] or not current['submission_started']:fail('TASK_CONTEXT_CONFLICT')
    if file_bytes(state_path)!=state_bytes or file_bytes(proof_path)!=proof_bytes:fail(code)
    return {**verified,'evidence_ref':dict(path=str(proof_path),sha256=proof_sha)}


def ffmpeg(executable,args,*,limit=1024*1024,timeout=12):
    try:
        result=subprocess.run([executable,'-nostdin','-v','error',*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired): fail('ENDPOINT_VIDEO_INVALID')
    if result.returncode or len(result.stdout)>limit or len(result.stderr)>65536: fail('ENDPOINT_VIDEO_INVALID')
    return result.stdout


def video_stream(context,role):
    raw=context['attempt_dir']/role/'raw.mp4';executable=fixed_ffmpeg(context)
    payload=ffmpeg(executable,['-copyts','-i',str(raw),'-map','0:v:0','-an','-pix_fmt','rgb24','-fps_mode','passthrough','-enc_time_base','demux','-frames:v','82','-f','framemd5','pipe:1'])
    text=payload.decode('ascii');tb=re.search(r'^#tb 0: (\d+)/(\d+)$',text,re.M);dimensions=re.search(r'^#dimensions 0: (\d+)x(\d+)$',text,re.M)
    if not tb or not dimensions: fail('ENDPOINT_VIDEO_INVALID')
    width,height=map(int,dimensions.groups());base=Fraction(int(tb[1]),int(tb[2]));rows=[]
    if width!=height or not 1<=width<=960 or base<=0: fail('ENDPOINT_VIDEO_INVALID')
    for line in text.splitlines():
        if line.startswith('#'): continue
        fields=[v.strip() for v in line.split(',')]
        if len(fields)!=6 or fields[0]!='0' or not re.fullmatch(r'[a-f0-9]{32}',fields[5]): fail('ENDPOINT_VIDEO_INVALID')
        rows.append((int(fields[2]),int(fields[3]),int(fields[4]),fields[5]))
    if len(rows)!=81 or any(size!=width*height*3 for _,_,size,_ in rows): fail('ENDPOINT_VIDEO_INVALID')
    timestamps=[r[0] for r in rows]
    if any(abs(Fraction(timestamps[i]-timestamps[0])*base-Fraction(i,24))>base for i in range(81)) or any(b<=a for a,b in zip(timestamps,timestamps[1:])): fail('ENDPOINT_VIDEO_INVALID')
    if any(duration<=0 or abs(Fraction(duration)*base-Fraction(1,24))>base for _,duration,_,_ in rows): fail('ENDPOINT_VIDEO_INVALID')
    # Container ticks can alternate41/42ms at24fps. Bound the final presentation
    # end as well as every PTS, rather than accumulating rounding error81 times.
    if abs(Fraction(timestamps[-1]-timestamps[0]+rows[-1][1])*base-Fraction(81,24))>base: fail('ENDPOINT_VIDEO_INVALID')
    return dict(payload=payload,width=width,height=height,time_base=[base.numerator,base.denominator],timestamps=timestamps,pixels=[r[3] for r in rows])


def validate_video_evidence(context,role):
    directory=context['attempt_dir']/role;stream=video_stream(context,role);paths=[directory/'decoded'/f'frame-{i:03d}.png' for i in range(81)]
    if sorted(p.name for p in (directory/'decoded').glob('*.png'))!=[p.name for p in paths]: fail('ENDPOINT_VIDEO_INVALID')
    hashes=[]
    for path,pixel_hash in zip(paths,stream['pixels']):
        image=png(path,mode='RGB',size=(stream['width'],stream['height']))
        if hashlib.md5(image.tobytes()).hexdigest()!=pixel_hash: fail('ENDPOINT_VIDEO_INVALID')
        hashes.append(sha_file(path))
    expected=dict(schema='sprite_endpoint_probe_v1',raw_sha256=sha_file(directory/'raw.mp4'),width=stream['width'],height=stream['height'],frame_count=81,fps=24,time_base=stream['time_base'],timestamps=stream['timestamps'],frame_sha256=hashes,framehash_sha256=digest(stream['payload']))
    if file_bytes(directory/'framehash.txt')!=stream['payload'] or canonical_record(directory/'probe.json')!=expected: fail('ENDPOINT_VIDEO_INVALID')
    return expected


def prepare_endpoint_video_evidence(run_manifest,role):
    """Archive all81 decoded frames/probe from a completed known-ID video, locally."""
    if role not in ('video_ab','video_ba'): fail('ENDPOINT_VIDEO_INVALID')
    context=load_context(run_manifest);validate_job(context,role,completed=True)
    directory=context['attempt_dir']/role;decoded=directory/'decoded';stream=video_stream(context,role)
    with tempfile.TemporaryDirectory(prefix='endpoint-video-') as temp:
        pattern=str(Path(temp)/'frame-%03d.png')
        ffmpeg(fixed_ffmpeg(context),['-i',str(directory/'raw.mp4'),'-map','0:v:0','-an','-pix_fmt','rgb24','-fps_mode','passthrough','-frames:v','82','-start_number','0',pattern],timeout=45)
        paths=[Path(temp)/f'frame-{i:03d}.png' for i in range(81)]
        if len(list(Path(temp).glob('*.png')))!=81: fail('ENDPOINT_VIDEO_INVALID')
        decoded.mkdir(exist_ok=True)
        for path in paths: new_or_identical(decoded/path.name,file_bytes(path,10*1024*1024))
    proof=dict(schema='sprite_endpoint_probe_v1',raw_sha256=sha_file(directory/'raw.mp4'),width=stream['width'],height=stream['height'],frame_count=81,fps=24,time_base=stream['time_base'],timestamps=stream['timestamps'],frame_sha256=[sha_file(decoded/f'frame-{i:03d}.png') for i in range(81)],framehash_sha256=digest(stream['payload']))
    new_or_identical(directory/'framehash.txt',stream['payload']);new_or_identical(directory/'probe.json',encoded(proof))
    return validate_video_evidence(context,role)


def cycle_values(context):
    ab=validate_video_evidence(context,'video_ab');ba=validate_video_evidence(context,'video_ba')
    if (ab['width'],ab['height'])!=(ba['width'],ba['height']): fail('ENDPOINT_VIDEO_INVALID')
    ab_times=[Fraction(t-ab['timestamps'][0])*Fraction(*ab['time_base']) for t in ab['timestamps']]
    ba_times=[Fraction(t-ba['timestamps'][0])*Fraction(*ba['time_base']) for t in ba['timestamps']]
    if any(abs(a-b)>max(Fraction(*ab['time_base']),Fraction(*ba['time_base'])) for a,b in zip(ab_times,ba_times)): fail('ENDPOINT_VIDEO_INVALID')
    paths=[context['attempt_dir']/role/'decoded'/f'frame-{i:03d}.png' for role in ('video_ab','video_ba') for i in (0,20,40,60)]
    width=ab['width'];grid=Image.new('RGB',(4*width,2*width))
    for i,path in enumerate(paths): grid.paste(png(path,mode='RGB'),((i%4)*width,(i//4)*width))
    payload=codec._png_bytes(grid)
    selection=dict(schema='sprite_endpoint_selection_v1',policy='endpoint_full_range_81_four_phases_v1',ab_raw_sha256=ab['raw_sha256'],ba_raw_sha256=ba['raw_sha256'],ab_probe_sha256=sha_file(context['attempt_dir']/'video_ab/probe.json'),ba_probe_sha256=sha_file(context['attempt_dir']/'video_ba/probe.json'),indices=[0,20,40,60],reviewed_range=[0,80],frame_sha256=[sha_file(p) for p in paths],grid_sha256=digest(payload))
    return selection,payload


def validate_cycle_evidence(context):
    selection,payload=cycle_values(context)
    if canonical_record(context['attempt_dir']/'selection.json')!=selection or file_bytes(context['attempt_dir']/'cycle-grid.png',10485760)!=payload: fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    return selection


def prepare_endpoint_cycle_evidence(run_manifest):
    """Archive the fixed eight samples/grid after both actual videos complete."""
    context=load_context(run_manifest)
    for role in ('video_ab','video_ba'): validate_job(context,role,completed=True)
    selection,payload=cycle_values(context)
    new_or_identical(context['attempt_dir']/'cycle-grid.png',payload);new_or_identical(context['attempt_dir']/'selection.json',encoded(selection))
    return selection


def common_nearest_2048(image):
    """One pixel-center floor mapping for every channel, without Pillow resampling."""
    if image.width!=image.height or not 1<=image.width<=960: fail('ENDPOINT_GEOMETRY_INVALID')
    image=image.convert('RGBA');width=image.width;source=image.tobytes();indices=[(2*x+1)*width//4096 for x in range(2048)];rows={}
    for y in set(indices):
        row=source[y*width*4:(y+1)*width*4]
        rows[y]=b''.join(row[x*4:x*4+4] for x in indices)
    return Image.frombytes('RGBA',(2048,2048),b''.join(rows[y] for y in indices))


def assembly_values(context):
    jobs={role:validate_job(context,role,completed=True) for role in ROLES}
    for stage in STAGES: gate_for(context,stage)
    directory=context['attempt_dir'];selection=validate_cycle_evidence(context);probe=canonical_record(directory/'video_ab/probe.json');width=probe['width']
    gates=context['current']['endpoint_gates']
    if gates['input_ready']['subject']!=dict(endpoint_plan_sha256=context['plan_sha256']):fail('TASK_ENDPOINT_GATE_CONFLICT')
    ab=jobs['video_ab'];ba=jobs['video_ba']
    expected_ab=dict(pair_gate_sha256=scope_sha(gates['pair_ready']),ab_request_id=ab['operation']['provider_request_id'],ab_raw_sha256=ab['raw_sha256'],probe_sha256=sha_file(directory/'video_ab/probe.json'))
    expected_cycle=dict(pair_gate_sha256=scope_sha(gates['pair_ready']),ab_gate_sha256=scope_sha(gates['ab_ready']),ab_request_id=ab['operation']['provider_request_id'],ab_raw_sha256=ab['raw_sha256'],ba_request_id=ba['operation']['provider_request_id'],ba_raw_sha256=ba['raw_sha256'],selection_sha256=sha_file(directory/'selection.json'),grid_sha256=selection['grid_sha256'])
    if gates['ab_ready']['subject']!=expected_ab or gates['cycle_ready']['subject']!=expected_cycle:fail('TASK_ENDPOINT_GATE_CONFLICT')
    clean=png(jobs['cleanup']['raw_path'],size=(4*width,2*width),maximum=50*1024*1024)
    cells=[clean.crop(((i%4)*width,(i//4)*width,(i%4+1)*width,(i//4+1)*width)) for i in range(8)]
    frames=[common_nearest_2048(cell) for cell in cells]
    before=[codec._png_bytes(cell) for cell in cells];after=[codec._png_bytes(frame) for frame in frames]
    geometry=dict(schema='sprite_endpoint_geometry_v1',policy='common_nearest_2048_v1',input_canvas=[width,width],output_canvas=[2048,2048],scale_numerator=2048,scale_denominator=width,origin=[0,0],sampling='pixel_center_floor_v1',channels='rgba',preparation_proof_sha256=sha_file(context['root']/'snapshots/preparation-proof.json'),pre_transform_frame_sha256=[digest(v) for v in before],output_frame_sha256=[digest(v) for v in after])
    binding={}
    for role in ('video_ab','video_ba','cleanup'):
        item=jobs[role];binding[role]=dict(request_id=item['operation']['provider_request_id'],config_sha256=economic_digest(context,item['config']),raw_sha256=item['raw_sha256'])
        if role!='cleanup': binding[role]['probe_sha256']=sha_file(directory/role/'probe.json')
    proof=dict(schema='sprite_endpoint_assembly_v1',endpoint_plan_sha256=context['plan_sha256'],jobs=binding,selection_sha256=sha_file(directory/'selection.json'),grid_sha256=selection['grid_sha256'],geometry=geometry)
    from sprite_alignment_workflow import _artifacts
    payloads,qa=_artifacts(frames,context['manifest']['request'],{'policy':'common_nearest_2048_v1'})
    return proof,before,payloads,qa


def validate_endpoint_attempt(run_manifest,*,require_artifacts):
    context=load_context(run_manifest);m=context['manifest']
    for role in context['attempt']['jobs']: validate_job(context,role,live=True)
    for gate in context['current'].get('endpoint_gates',{}).values(): validate_gate_report(gate)
    if require_artifacts:
        proof,cells,payloads,qa=assembly_values(context);directory=context['attempt_dir'];artifacts=directory/'artifacts'
        if canonical_record(directory/'assembly-provenance.json')!=proof: fail('ENDPOINT_GEOMETRY_INVALID')
        for i,payload in enumerate(cells,1):
            if file_bytes(directory/'cells'/f'frame-{i:02d}.png',10*1024*1024)!=payload: fail('ENDPOINT_GEOMETRY_INVALID')
        expected=dict(sheet=str(artifacts/'sprite-sheet.png'),preview_gif=str(artifacts/'preview.gif'),contact=str(artifacts/'contact.png'),qa_report=str(artifacts/'qa.json'),frames=[str(artifacts/f'frame-{i:02d}.png') for i in range(1,9)])
        if m.get('artifact_paths')!=expected or m.get('artifact_hashes')!={str(artifacts/name):digest(payload) for name,payload in payloads.items()}: fail('ARTIFACT_TAMPERED')
        for name,payload in payloads.items():
            if file_bytes(artifacts/name,50*1024*1024)!=payload: fail('ARTIFACT_TAMPERED')
        if m.get('consistency')!=qa['consistency']: fail('ARTIFACT_TAMPERED')
    return context['path'],m,context


def assemble_endpoint_attempt(run_manifest):
    """Only deterministic local assembly; the normal public review grants delivery."""
    import generic_sprite_workflow as workflow
    path=Path(run_manifest).expanduser().absolute();m=None
    try:
        context=load_context(path);m=context['manifest']
        with run_lock(path.parent):
            context=load_context(path);m=context['manifest']
            if m['state'] in ASSEMBLED:
                validate_endpoint_attempt(path,require_artifacts=True);return workflow._result(m['state'],path,m)
            proof,cells,payloads,qa=assembly_values(context);directory=context['attempt_dir'];artifacts=directory/'artifacts'
            artifacts.mkdir(exist_ok=True);(directory/'cells').mkdir(exist_ok=True)
            for i,payload in enumerate(cells,1): new_or_identical(directory/'cells'/f'frame-{i:02d}.png',payload)
            for name,payload in payloads.items(): new_or_identical(artifacts/name,payload)
            new_or_identical(directory/'assembly-provenance.json',encoded(proof))
            m.update(state=workflow.READY,artifact_paths=dict(sheet=str(artifacts/'sprite-sheet.png'),preview_gif=str(artifacts/'preview.gif'),contact=str(artifacts/'contact.png'),qa_report=str(artifacts/'qa.json'),frames=[str(artifacts/f'frame-{i:02d}.png') for i in range(1,9)]),artifact_hashes={str(artifacts/name):digest(payload) for name,payload in payloads.items()},consistency=qa['consistency'])
            atomic_json(path,m);validate_endpoint_attempt(path,require_artifacts=True)
            return workflow._result(workflow.READY,path,m)
    except (OSError,ValueError,KeyError,TypeError) as error:
        return workflow._result(path=path,manifest=m,reason=getattr(error,'code','ENDPOINT_ASSEMBLY_INVALID'))


def validate_bridge_message(message,before):
    fields=('schema','job_dir','config_sha256','config_file_sha256','input_set_sha256')
    keys(message,fields+(('quote_sha256','amount_usd','experiment_state_sha256') if before else ()),'TASK_BRIDGE_INPUT_INVALID')
    if message['schema']!=('sprite_before_submit_v2' if before else 'sprite_transport_context_v2'): fail('TASK_BRIDGE_INPUT_INVALID')
    checked_path(message['job_dir'],'TASK_BRIDGE_INPUT_INVALID')
    for name in ('config_sha256','config_file_sha256','input_set_sha256',*(('quote_sha256','experiment_state_sha256') if before else ())):
        if not isinstance(message[name],str) or not re.fullmatch('[a-f0-9]{64}',message[name]): fail('TASK_BRIDGE_INPUT_INVALID')
    if before:
        if not isinstance(message['amount_usd'],str): fail('TASK_BRIDGE_INPUT_INVALID')
        ledger._money(message['amount_usd'],True)


def validate_quote(context,role,*,execution,quote_sha,amount,now=None):
    """Replay a real exact receipt and every archived pricing/defaults byte."""
    from urllib.parse import urlencode
    code='PRICE_RECEIPT_INVALID';directory=context['attempt_dir']/role/('pricing-receipts' if execution else 'planning-pricing-receipts')
    if not directory.is_dir() or directory.is_symlink():fail(code)
    names=list(directory.glob('*.receipt.json'))
    if len(names)>4096:fail(code)
    matches=[p for p in names if sha_file(p)==quote_sha]
    if len(matches)!=1:fail(code)
    path=matches[0];receipt=strict_json(file_bytes(path),code,decimals=True);model=model_for(context,role)
    common=('receipt_id','model','retrieved_at','expires_at','currency','api_unit','estimated_cost_usd','reserved_units','planning_basis','sources')
    keys(receipt,common+(('schema_version','config_sha256','base_unit_price_usd','resolution_multiplier','effective_unit_price_usd','estimated_cost_usd_decimal','input_duration_seconds','driving_video_sha256') if execution else ('schema','endpoint_plan_sha256','role','unit_price_usd','multiplier_decimal')),code)
    basis='configured_output_count' if role=='pose_edit' else 'joined_grid_one_image_v1' if role=='cleanup' else 'wan_720p_81_video_v1'
    units='videos' if role in ('video_ab','video_ba') else 'images';multiplier=Decimal('1.5') if role=='pose_edit' and not seedream(context) else Decimal(1)
    if (receipt['model']!=model or receipt['currency']!='USD' or receipt['api_unit']!=units or type(receipt['reserved_units']) is not int or receipt['reserved_units']!=1 or receipt['planning_basis']!=basis):fail(code)
    if execution:
        binding=operation(context,role).get('execution_binding')
        if (receipt['schema_version']!=3 or not binding or receipt['config_sha256']!=binding['config_sha256'] or receipt['input_duration_seconds'] is not None or receipt['driving_video_sha256'] is not None
                or receipt['resolution_multiplier']!=(multiplier if role=='pose_edit' and not seedream(context) else None)):fail(code)
        base=Decimal(ledger._money(receipt['base_unit_price_usd'],True))
    else:
        if (role=='pose_edit' or receipt['schema']!='sprite_endpoint_planning_quote_v1' or receipt['role']!=role or receipt['endpoint_plan_sha256']!=context['plan_sha256'] or receipt['multiplier_decimal']!=str(multiplier)):fail(code)
        base=Decimal(ledger._money(receipt['unit_price_usd'],True))
    try:
        uuid_value=uuid.UUID(receipt['receipt_id'])
        if uuid_value.version!=4 or str(uuid_value)!=receipt['receipt_id']:fail(code)
        retrieved=datetime.fromisoformat(receipt['retrieved_at'].replace('Z','+00:00'));expires=datetime.fromisoformat(receipt['expires_at'].replace('Z','+00:00'))
        if retrieved.utcoffset()!=timedelta(0) or expires.utcoffset()!=timedelta(0) or expires-retrieved!=timedelta(minutes=15):fail(code)
        millis=int((retrieved-datetime(1970,1,1,tzinfo=timezone.utc)).total_seconds()*1000)
        if path.name!=f'{millis:016d}-{receipt["receipt_id"]}.receipt.json':fail(code)
    except (ValueError,TypeError,AttributeError):fail(code)
    sources=receipt['sources'];suffixes=['api',*(['public'] if role!='cleanup' else []),'schema']
    if not isinstance(sources,list) or len(sources)!=len(suffixes):fail(code)
    buffers={}
    for source,suffix in zip(sources,suffixes):
        keys(source,('source_url','retrieved_at','body_sha256','archive_file','selected'),code)
        archive=receipt['receipt_id']+'.'+suffix+'.body'
        if source['archive_file']!=archive or source['retrieved_at']!=receipt['retrieved_at']:fail(code)
        payload=file_bytes(directory/archive,code=code)
        if digest(payload)!=source['body_sha256']:fail(code)
        expected_url=('https://api.fal.ai/v1/models/pricing?'+urlencode(dict(endpoint_id=model)) if suffix=='api' else 'https://fal.ai/models/'+model if suffix=='public' else 'https://fal.ai/api/openapi/queue/openapi.json?'+urlencode(dict(endpoint_id=model)))
        if source['source_url']!=expected_url:fail(code)
        buffers[suffix]=payload
    import base64
    as_base64=lambda v:base64.b64encode(v).decode('ascii') if v is not None else None
    selected=node_local('pricing',dict(model=model,api=as_base64(buffers['api']),public=as_base64(buffers.get('public')),schema=as_base64(buffers['schema'])))
    if set(selected)!={'api','public','schema'}:fail(code)
    for source,suffix in zip(sources,suffixes):
        if source['selected']!=selected[suffix]:fail(code)
    if (selected['api']['unit']!=units or selected['api']['currency']!='USD'
            or Decimal(str(selected['api']['unit_price_usd']))!=base):fail(code)
    with localcontext() as ctx:
        ctx.prec=512;estimate=base*multiplier
        if Decimal(ledger._money(receipt['estimated_cost_usd'],True))!=estimate or Decimal(ledger._money(amount,True))!=estimate:fail(code)
        if execution and (Decimal(ledger._money(receipt['effective_unit_price_usd'],True))!=estimate or Decimal(ledger._money(receipt['estimated_cost_usd_decimal'],True))!=estimate):fail(code)
    now=now or datetime.now(timezone.utc)
    if now-retrieved < -timedelta(seconds=30) or now>expires:fail('PRICE_STALE')
    return receipt


def bridge_run(message,before):
    validate_bridge_message(message,before);directory=checked_path(message['job_dir'],'TASK_SCOPE_MISMATCH');role=directory.name
    if role not in ROLES or directory.parent.name!='attempt-01':fail('TASK_SCOPE_MISMATCH')
    context=load_context(directory.parent.parent/'run-manifest.json');op=operation(context,role)
    job=validate_job(context,role,recovery=True);recovery_id=job['recovery_request_id'];recovering=bool(recovery_id)
    binding=op['execution_binding']
    if any(message[k]!=binding[k] for k in ('config_sha256','config_file_sha256','input_set_sha256')):fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    if not recovering and not op['submission_started']:
        gate_for(context,STAGES[ROLES.index(role)])
        if role=='video_ba':
            gate=gate_for(context,'ab_ready');probe=validate_video_evidence(context,'video_ab');subject=gate['subject']
            ab=validate_job(context,'video_ab',completed=True)
            if subject['ab_raw_sha256']!=ab['raw_sha256'] or subject['ab_request_id']!=ab['operation']['provider_request_id'] or subject['probe_sha256']!=sha_file(context['attempt_dir']/'video_ab/probe.json'):fail('TASK_ENDPOINT_GATE_CONFLICT')
        ledger.check_history_context(context['ref']['task_manifest'],context['ref']['target_id'],context['manifest']['request']['effective_descriptor'])
    marked=None
    if before and recovering:
        # The existing result shape exposes a verified durable Node ID without
        # pretending its ledger projection has already committed. Same-ID GET
        # is the only permitted transport action; never compare/mark a new POST.
        marked=dict(submit_allowed=False,provider_request_id=recovery_id)
    elif before:
        if not recovering:
            if seedream(context) and context['plan']['pose_decoder']!=decoder_capabilities():fail('SEEDREAM_DECODER_CHANGED')
            for remaining in ROLES[ROLES.index(role):]:
                pending=operation(context,remaining)
                if pending['status']!='held' or pending['submission_started']:fail('TASK_PLAN_INVALID')
                quote=pending['current_quote']
                # Only current operation must be executable. Future roles can
                # retain planning quotes until their exact config materializes.
                execution=remaining==role or bool(pending.get('execution_binding'))
                validate_quote(context,remaining,execution=execution,quote_sha=quote['quote_sha256'],amount=quote['amount_usd'])
            if message['quote_sha256']!=op['current_quote']['quote_sha256'] or Decimal(message['amount_usd'])!=Decimal(op['current_quote']['amount_usd']):fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        expected=dict(schema='sprite_endpoint_submission_expected_v1',config_sha256=message['config_sha256'],binding_sha256=op['binding_sha256'],quote_sha256=message['quote_sha256'],amount_usd=message['amount_usd'],experiment_state_sha256=message['experiment_state_sha256'])
        marked=ledger.mark_submission(context['ref']['task_manifest'],op['operation_id'],expected=expected)
        task=ledger.inspect_task(context['ref']['task_manifest']);updated=task['operations'][op['operation_id']];current=task['attempts'][op['attempt_key']]
        if updated.get('binding_sha256')!=op['binding_sha256'] or current['experiment_state_sha256']!=marked['experiment_state_sha256']:fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        context.update(ledger=task,current=current);op=updated
    result=dict(schema='sprite_transport_context_result_v2',state='SUBMISSION_AUTHORIZED' if before else 'CONTEXT_VERIFIED',task_id=context['ref']['task_id'],target_id=context['ref']['target_id'],run_id=op['run_id'],attempt_id=context['attempt']['attempt_id'],operation_id=op['operation_id'],config_sha256=binding['config_sha256'],config_file_sha256=binding['config_file_sha256'],input_set_sha256=binding['input_set_sha256'],binding_sha256=op['binding_sha256'],provider_request_id=recovery_id or op.get('provider_request_id'),submission_started=op['submission_started'],status=op['status'],outcome_status=op['outcomes'][-1]['status'] if op['outcomes'] else None,experiment_state_sha256=context['current']['experiment_state_sha256'])
    if before:result['submit_allowed']=marked['submit_allowed']
    return result
