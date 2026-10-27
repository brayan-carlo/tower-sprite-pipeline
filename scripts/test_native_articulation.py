"""Meaningful renderer tests: compositing and invalid/tampered recipe rejection."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from PIL import Image,ImageDraw
from unittest.mock import patch
import native_articulation as renderer
from native_articulation import render_recipe,ArticulationError

class LayerRecipeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  a=Image.new('RGBA',(64,64));ImageDraw.Draw(a).rectangle((20,20,29,29),fill=(200,30,20,255))
  b=Image.new('RGBA',(64,64));ImageDraw.Draw(b).rectangle((25,25,34,34),fill=(20,50,220,128))
  for name,im in [('source',a),('native',a),('red',a),('blue',b)]:im.save(self.root/(name+'.png'))
  def ref(name):return {'path':name+'.png','sha256':hashlib.sha256((self.root/(name+'.png')).read_bytes()).hexdigest()}
  self.data={'schema':'native_articulated_layers_v1','canvas':[64,64],'frame_ms':100,'action':'walk','orientation':'left',
   'source':ref('source'),'native_inputs':[{**ref('native'),'provenance':'Observed native output retained for this test fixture.'}],
   'assets':{'red':ref('red'),'blue':ref('blue')},'frames':[]}
  for i in range(8):
   self.data['frames'].append({'frame':i+1,'layers':[{'id':'rear','asset':'red','origin':[0,0],'target':[i,0],'angle':0,'scale':1},
    {'id':'front','asset':'blue','origin':[0,0],'target':[0,0],'angle':0,'scale':1}]})
 def tearDown(self):self.temp.cleanup()
 def run_recipe(self,data=None):
  p=self.root/'recipe.json';p.write_text(json.dumps(self.data if data is None else data),encoding='utf8')
  return render_recipe(p)
 def test_exact_alpha_occlusion_and_eight_frames(self):
  frames,report=self.run_recipe();self.assertEqual(len(frames),8)
  expected=Image.new('RGBA',(64,64));expected.alpha_composite(Image.open(self.root/'red.png'));expected.alpha_composite(Image.open(self.root/'blue.png'))
  self.assertEqual(frames[0].tobytes(),expected.tobytes())
  self.assertEqual(frames[7].getpixel((20,20))[3],0)
  self.assertEqual(frames[7].getpixel((36,20)),(200,30,20,255))
  self.assertFalse(report['delivery_allowed'])
  self.assertEqual(len(report['frame_pixel_sha256']),8)
 def test_changed_asset_hash_rejected(self):
  Image.new('RGBA',(64,64)).save(self.root/'red.png')
  with self.assertRaisesRegex(ArticulationError,'HASH'):self.run_recipe()
 def test_duplicate_or_missing_instance_rejected(self):
  for mode in ('duplicate','missing'):
   data=copy.deepcopy(self.data)
   if mode=='duplicate':data['frames'][3]['layers'][1]['id']='rear'
   else:data['frames'][3]['layers'].pop()
   with self.subTest(mode=mode),self.assertRaises(ArticulationError):self.run_recipe(data)
 def test_nonfinite_transform_rejected(self):
  self.data['frames'][0]['layers'][0]['angle']=float('nan')
  with self.assertRaisesRegex(ArticulationError,'TRANSFORM'):self.run_recipe()
 def test_per_frame_scale_drift_rejected(self):
  self.data['frames'][2]['layers'][0]['scale']=1.1
  with self.assertRaisesRegex(ArticulationError,'SCALE'):self.run_recipe()
 def test_potential_clipping_rejected(self):
  self.data['frames'][4]['layers'][0]['target']=[60,0]
  with self.assertRaisesRegex(ArticulationError,'CLIPPING'):self.run_recipe()
 def test_input_path_escape_rejected(self):
  self.data['assets']['red']['path']='../red.png'
  with self.assertRaisesRegex(ArticulationError,'PATH'):self.run_recipe()
 def test_wrong_frame_ordinals_rejected(self):
  self.data['frames'][4]['frame']=8
  with self.assertRaisesRegex(ArticulationError,'FRAMES'):self.run_recipe()
 def test_rotation_retains_known_landmark(self):
  for frame in self.data['frames']:
   frame['layers'][0].update(origin=[20,20],target=[40,20],angle=90)
  frames,_=self.run_recipe()
  self.assertEqual(frames[0].getpixel((35,25)),(200,30,20,255))
 def test_modified_source_rejected(self):
  Image.new('RGBA',(64,64)).save(self.root/'source.png')
  with self.assertRaisesRegex(ArticulationError,'HASH'):self.run_recipe()

 def test_recipe_hash_binds_snapshot_used_for_render(self):
  expected=hashlib.sha256(json.dumps(self.data).encode('utf8')).hexdigest()
  original=renderer._transform
  def change_recipe(*args):
   altered=copy.deepcopy(self.data);altered['orientation']='right'
   (self.root/'recipe.json').write_text(json.dumps(altered),encoding='utf8')
   return original(*args)
  with patch.object(renderer,'_transform',side_effect=change_recipe):
   _,report=self.run_recipe()
  self.assertEqual(report['declared_orientation'],'left')
  self.assertEqual(report['recipe_sha256'],expected)

 def test_asset_decode_uses_the_verified_buffer(self):
  expected=self.data['assets']['red']['sha256'];original=renderer._load_asset;calls=0
  def change_asset(payload):
   nonlocal calls
   calls+=1
   if calls==2:
    im=Image.new('RGBA',(64,64));ImageDraw.Draw(im).rectangle((20,20,29,29),fill=(30,200,20,255));im.save(self.root/'red.png')
   return original(payload)
  with patch.object(renderer,'_load_asset',side_effect=change_asset):
   frames,report=self.run_recipe()
  self.assertEqual(frames[0].getpixel((20,20)),(200,30,20,255))
  self.assertEqual(report['asset_sha256']['red'],expected)

if __name__=='__main__':unittest.main()
