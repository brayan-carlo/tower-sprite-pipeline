"""Offline video integration: preserve shared origins and legacy immutable scopes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw
import generic_sprite_workflow as workflow
from test_sheet_video import FFMPEG
from test_sheet_workflow import digest, sheet


class VideoGroundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        root=Path(cls.fixture.name);cls.source=root/'source.png';sheet(cls.source,count=2)
        # Encode once: fixed body X, changing tail width and known vertical lift.
        for i in range(96):
            phase=i%24;dy=-14 if 8<=phase<16 else 0
            tail_end=(86,94,110,118,110,94,86,82)[phase//3]
            im=Image.new('RGB',(128,128),(110,110,110));draw=ImageDraw.Draw(im)
            draw.rectangle((75,52+dy,tail_end,57+dy),fill=(50,180,65))
            draw.rectangle((35,20+dy,75,70+dy),fill=(180,60,70))
            draw.rectangle((25+phase,75+dy,38+phase,110+dy),fill=(40,150,210))
            draw.rectangle((85-phase,75+dy,98-phase,110+dy),fill=(210,150,30))
            im.save(root/f'input-{i:03d}.png')
        cls.raw_video=root/'fixture.mp4'
        encoded=subprocess.run([str(FFMPEG),'-nostdin','-v','error','-framerate','24',
            '-i',str(root/'input-%03d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(cls.raw_video)],
            capture_output=True,timeout=30)
        if encoded.returncode:raise RuntimeError(encoded.stderr.decode(errors='replace'))

    def transport(self,command,job):
        config=json.loads((job/'config.json').read_text())
        self.assertIn(config['model'],(workflow.H3,workflow.CLEANUP))
        if command=='pricing':return {'state':'PRICED','estimated_cost_usd':.01 if config['model']==workflow.CLEANUP else .12}
        if config['model']==workflow.H3:
            out=job/'raw.mp4';shutil.copyfile(self.raw_video,out)
        else:
            with Image.open(config['source']['path']) as im:cleaned=im.convert('RGB')
            delta=ImageChops.difference(cleaned,Image.new('RGB',cleaned.size,(110,110,110)))
            cleaned.putalpha(delta.convert('L').point(lambda v:255 if v>20 else 0))
            out=job/'raw.png';cleaned.save(out)
        return {'state':'DOWNLOADED','raw_path':str(out),'raw_sha256':digest(out),'request_id':'offline-fixture'}

    def options(self,workspace,**overrides):
        return {'source':str(self.source),'action':'run','orientation':'left','strategy':'video',
            'canvas':'192x192','workspace_root':str(workspace),'ffmpeg_exe':str(FFMPEG),
            'cycle_start_seconds':1,'cycle_duration_seconds':1,'poll_seconds':0}|overrides

    def geometry(self,result):
        self.assertEqual(result['state'],'READY_FOR_VISUAL_REVIEW',result)
        body_x=[];body_y=[];bottoms=[]
        for path in result['artifact_paths']['frames']:
            with Image.open(path) as im:frame=im.convert('RGBA')
            # Independent color marker identifies the known rigid body, not its silhouette.
            red,green,blue,alpha=frame.split()
            mask=red.point(lambda v:255 if v>140 else 0)
            for band,limit in ((green,100),(blue,130)):
                mask=ImageChops.multiply(mask,band.point(lambda v,limit=limit:255 if v<limit else 0))
            mask=ImageChops.multiply(mask,alpha.point(lambda v:255 if v>=128 else 0))
            box=mask.getbbox();self.assertIsNotNone(box,'The fixture body must remain visible')
            body_x.append((box[0]+box[2])/2);body_y.append((box[1]+box[3])/2)
            bottoms.append(frame.getchannel('A').getbbox()[3])
        return body_x,body_y,bottoms

    def test_video_walk_keeps_body_origin_and_height(self):
        # A walk/video scope accidentally using per-pose grounding must fail this.
        with tempfile.TemporaryDirectory() as temp:
            for strategy,orientation in [('video','left'),('auto','left'),('auto','right')]:
                with self.subTest(strategy=strategy,orientation=orientation):
                    result=workflow._generate_legacy(self.options(Path(temp)/f'{strategy}-{orientation}',
                        action='walk',strategy=strategy,orientation=orientation),transport=self.transport)
                    x,y,bottoms=self.geometry(result)
                    self.assertLessEqual(max(x)-min(x),3,'Variable tail width must not translate the fixed body')
                    self.assertGreaterEqual(max(y)-min(y),15,'Preserve the body lift in the sampled cells')
                    self.assertGreaterEqual(max(bottoms)-min(bottoms),15,'Do not flatten relative foot heights')

    def test_video_run_keeps_suspension_and_existing_grounded_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);options=self.options(root/'new')
            new=workflow._generate_legacy(options,transport=self.transport)
            _,_,bottoms=self.geometry(new)
            self.assertGreaterEqual(max(bottoms)-min(bottoms),15,'The airborne phase must not be snapped to the floor')
            qa=json.loads(Path(new['artifact_paths']['qa_report']).read_text())
            self.assertFalse(qa['grounded'])
            parse=workflow._options
            def old_options(value):
                parsed,missing=parse(value);parsed['request']['grounded']=True
                return parsed,missing
            for action in ('run','walk'):
                with self.subTest(legacy_action=action):
                    with patch.object(workflow,'_options',old_options):
                        old=workflow._generate_legacy(options|{'action':action,'workspace_root':str(root/f'legacy-{action}')},transport=self.transport)
                    self.assertEqual(old['state'],'READY_FOR_VISUAL_REVIEW')
                    directory=Path(old['run_manifest']).parent
                    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
                    result=workflow.resume(old['run_manifest'],transport=lambda *_:self.fail('Resume must not resubmit'))
                    self.assertEqual(result['state'],'READY_FOR_VISUAL_REVIEW')
                    after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
                    self.assertEqual(before,after)
                    self.assertTrue(json.loads(Path(old['run_manifest']).read_text())['request']['grounded'])
            for action,strategy in [('walk','sheet'),('run','sheet')]:
                parsed,_=parse(options|{'action':action,'strategy':strategy,
                    'cycle_start_seconds':None,'cycle_duration_seconds':None})
                self.assertFalse(parsed['request']['grounded'])
                self.assertEqual(parsed['request']['alignment_policy'],'body_x_preserve_y_v1')


if __name__=='__main__':unittest.main()
