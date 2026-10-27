"""Deterministic RGBA layer composition; no generation or artistic approval.

A recipe describes eight frames of a reviewed biped walk or planted attack.
The caller supplies native/source provenance and checks anatomy, motion and scale.
This module cannot recognize a walk or grant delivery.
"""
import hashlib,io,json,math,re
from pathlib import Path
from PIL import Image,UnidentifiedImageError

SCHEMA='native_articulated_layers_v1'
class ArticulationError(ValueError):pass

def _number(x):
 return type(x) in (int,float) and math.isfinite(x)

def _pair(p):
 return isinstance(p,(list,tuple)) and len(p)==2 and all(_number(x) for x in p)

def _input(root,record):
 if not isinstance(record,dict) or not isinstance(record.get('path'),str):
  raise ArticulationError('INPUT_PATH_INVALID')
 relative=Path(record['path'])
 if relative.is_absolute() or '..' in relative.parts:
  raise ArticulationError('INPUT_PATH_INVALID')
 p=root/relative
 for part in (p,*p.parents):
  if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
   raise ArticulationError('INPUT_PATH_INVALID')
 if root not in p.resolve().parents or not p.is_file():
  raise ArticulationError('INPUT_PATH_INVALID')
 if p.stat().st_size>64*1024*1024:raise ArticulationError('INPUT_LIMIT')
 digest=record.get('sha256')
 if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
  raise ArticulationError('INPUT_HASH_INVALID')
 with p.open('rb') as stream:payload=stream.read(64*1024*1024+1)
 if len(payload)>64*1024*1024:raise ArticulationError('INPUT_LIMIT')
 if hashlib.sha256(payload).hexdigest()!=digest:raise ArticulationError('INPUT_HASH_MISMATCH')
 return payload

def _load_asset(payload):
 try:
  with Image.open(io.BytesIO(payload)) as im:
   if (im.format!='PNG' or im.mode!='RGBA' or getattr(im,'n_frames',1)!=1
       or not(1<=im.width<=4096 and 1<=im.height<=4096)
       or im.width*im.height>8_000_000):
    raise ArticulationError('RGBA_PNG_REQUIRED_OR_LIMIT')
   im.load();out=im.copy()
 except (OSError,UnidentifiedImageError,Image.DecompressionBombError) as exc:
  raise ArticulationError('RGBA_PNG_INVALID') from exc
 if out.getchannel('A').getbbox() is None:raise ArticulationError('ASSET_EMPTY')
 return out

def _validate_transform(layer,canvas,asset):
 if not isinstance(layer,dict) or not _pair(layer.get('origin')) or not _pair(layer.get('target')):
  raise ArticulationError('TRANSFORM_INVALID')
 a=layer.get('angle');s=layer.get('scale')
 if not _number(a) or not -360<=a<=360 or not _number(s) or not .1<=s<=4:
  raise ArticulationError('TRANSFORM_INVALID')
 o=layer['origin'];t=layer['target']
 if any(abs(v)>16384 for v in (*o,*t)):raise ArticulationError('TRANSFORM_INVALID')
 c=math.cos(math.radians(a));sn=math.sin(math.radians(a))
 x0,y0,x1,y1=asset.getchannel('A').getbbox()
 for x,y in [(x0,y0),(x1,y0),(x0,y1),(x1,y1)]:
  dx=(x-o[0])*s;dy=(y-o[1])*s
  qx=t[0]+c*dx-sn*dy;qy=t[1]+sn*dx+c*dy
  if not(0<=qx<=canvas[0] and 0<=qy<=canvas[1]):
   raise ArticulationError('POTENTIAL_CLIPPING')
 # Bounding-box corners are a conservative bound. Do not shrink art to pass.
 return layer

def _transform(asset,layer,canvas):
 o=layer['origin'];t=layer['target'];s=layer['scale'];a=math.radians(layer['angle'])
 c=math.cos(a)/s;sn=math.sin(a)/s
 return asset.transform(tuple(canvas),Image.Transform.AFFINE,
  (c,sn,o[0]-c*t[0]-sn*t[1],-sn,c,o[1]+sn*t[0]-c*t[1]),resample=Image.Resampling.NEAREST)

def render_recipe(recipe_path):
 path=Path(recipe_path).absolute()
 if path.is_symlink() or path.stat().st_size>2*1024*1024:raise ArticulationError('RECIPE_PATH_OR_LIMIT')
 root=path.parent.resolve()
 try:
  with path.open('rb') as stream:recipe_bytes=stream.read(2*1024*1024+1)
  if len(recipe_bytes)>2*1024*1024:raise ArticulationError('RECIPE_PATH_OR_LIMIT')
  data=json.loads(recipe_bytes.decode('utf8'))
 except (ValueError,OSError) as exc:raise ArticulationError('RECIPE_INVALID') from exc
 if not isinstance(data,dict) or data.get('schema')!=SCHEMA:raise ArticulationError('RECIPE_SCHEMA_INVALID')
 canvas=data.get('canvas')
 if not isinstance(canvas,list) or len(canvas)!=2 or any(type(v) is not int or not 16<=v<=2048 for v in canvas):
  raise ArticulationError('CANVAS_INVALID')
 ms=data.get('frame_ms')
 if type(ms) is not int or not 20<=ms<=1000 or ms%10:raise ArticulationError('TIMING_INVALID')
 if data.get('action') not in ('walk','attack') or data.get('orientation') not in ('left','right'):
  raise ArticulationError('PROFILE_UNSUPPORTED')
 _load_asset(_input(root,data.get('source')))
 native=data.get('native_inputs')
 if not isinstance(native,list) or not 1<=len(native)<=8:raise ArticulationError('NATIVE_PROVENANCE_REQUIRED')
 for record in native:
  _input(root,record)
  if not isinstance(record.get('provenance'),str) or len(record['provenance'].strip())<10:
   raise ArticulationError('NATIVE_PROVENANCE_REQUIRED')
 records=data.get('assets')
 if not isinstance(records,dict) or not 1<=len(records)<=32:raise ArticulationError('ASSETS_INVALID')
 assets={};pixel_total=0
 for name,record in records.items():
  if not isinstance(name,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',name):raise ArticulationError('ASSET_ID_INVALID')
  asset=_load_asset(_input(root,record));pixel_total+=asset.width*asset.height
  if pixel_total>64_000_000:raise ArticulationError('ASSET_PIXEL_LIMIT')
  assets[name]=asset
 frames=data.get('frames')
 if not isinstance(frames,list) or len(frames)!=8 or any(not isinstance(f,dict) for f in frames) or [f.get('frame') for f in frames]!=list(range(1,9)):
  raise ArticulationError('FRAMES_INVALID')
 initial=None;initial_roles=None
 for frame in frames:
  layers=frame.get('layers')
  if not isinstance(layers,list) or not 1<=len(layers)<=32:raise ArticulationError('LAYERS_INVALID')
  instances={};roles={}
  for layer in layers:
   if not isinstance(layer,dict):raise ArticulationError('LAYER_INVALID')
   name=layer.get('id');asset_name=layer.get('asset')
   if not isinstance(name,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',name) or name in instances:
    raise ArticulationError('LAYER_INSTANCE_INVALID')
   if not isinstance(asset_name,str) or asset_name not in assets:raise ArticulationError('LAYER_ASSET_INVALID')
   role=layer.get('role')
   if role not in (None,'joint_overlap'):raise ArticulationError('LAYER_ROLE_INVALID')
   if type(layer.get('visible',True)) is not bool or (layer.get('visible') is False and role!='joint_overlap'):
    raise ArticulationError('LAYER_VISIBILITY_INVALID')
   roles[name]=role
   _validate_transform(layer,canvas,assets[asset_name]);instances[name]=(asset_name,layer['scale'])
  if initial is None:initial=instances;initial_roles=roles
  elif set(initial)!=set(instances):raise ArticulationError('MISSING_OR_EXTRA_LAYER_INSTANCE')
  elif initial!=instances:raise ArticulationError('LAYER_ASSET_OR_SCALE_DRIFT')
  elif initial_roles!=roles:raise ArticulationError('LAYER_ROLE_DRIFT')
  if not any(layer.get('role') is None for layer in layers):raise ArticulationError('BODY_LAYER_REQUIRED')
 result=[]
 for frame in frames:
  im=Image.new('RGBA',tuple(canvas))
  for layer in frame['layers']:
   if layer.get('visible',True):im.alpha_composite(_transform(assets[layer['asset']],layer,canvas))
  result.append(im)
 def pixel_sha(im):
  return hashlib.sha256(im.width.to_bytes(4,'big')+im.height.to_bytes(4,'big')+im.tobytes()).hexdigest()
 hashes=[pixel_sha(im) for im in result]
 report={'schema':SCHEMA,'state':'RENDERED_UNREVIEWED','delivery_allowed':False,
  'recipe_sha256':hashlib.sha256(recipe_bytes).hexdigest(),'source_sha256':data['source']['sha256'],
  'native_inputs':native,'asset_sha256':{name:r['sha256'] for name,r in records.items()},
  'canvas':canvas,'frame_ms':ms,'frame_count':8,'frame_pixel_sha256':hashes,
  'duplicate_frame_pairs':[[a+1,b+1] for a in range(8) for b in range(a+1,8) if hashes[a]==hashes[b]],
  'declared_action':data['action'],'declared_orientation':data['orientation'],
  'construction':'Native/source RGBA parts with local fixed-scale rigid layer transforms. No service call.',
  'limits':['No automatic anatomy, direction, support, scale-equivalence or fluency recognition.',
            'Source and native-input hashes bind supplied provenance; they do not independently attest the image service.',
            'Potential-clipping check uses conservative transformed alpha bounding boxes.',
            'Artistic review and controlled delivery lifecycle are still required.']}
 return result,report
