import copy
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
from native_components import analyze_components
from sheet_workflow_state import WorkflowError, sha_file
import native_geometry as g


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name)/'source.png'
        im = Image.new('RGBA', (64,64))
        ImageDraw.Draw(im).rectangle((12,12,50,50), fill=(70,70,70,255))
        im.save(self.source)
        self.contract = {'source_sha256':sha_file(self.source), 'anchors':{
            'head':{'points':[[14,20],[24,20]],'notes':'Visible head reference'},
            'torso':{'points':[[30,15],[30,35]],'notes':'Visible torso reference'}},
            'body_anchor':[35,30], 'pivot':[35,51], 'notes':'Ground below supporting paw',
            'component_decisions':[]}
        self.sheet=Image.new('RGBA',(2048,1024))
        self.report={'frames':[]}
        for i in range(8):
            y=200 if i%2==0 else 160
            cell=Image.new('RGBA',(512,512))
            ImageDraw.Draw(cell).rectangle((150,y,350,y+240),fill=(60+i,60,60,255))
            self.sheet.paste(cell,((i%4)*512,(i//4)*512))
            self.report['frames'].append({'frame':i+1, 'anchors':{
                'head':{'points':[[170,y+40],[190,y+40]],'notes':'Same visible head landmarks'},
                'torso':{'points':[[240,y+40],[240,y+80]],'notes':'Same visible torso landmarks'}},
                'body_anchor':[250,y+100], 'support_point':[250,y+240] if i%2==0 else None,
                'notes':'Body reference and support checked', 'component_decisions':[]})

    def test_single_source_scale_canvas_expansion_and_flight(self):
        frames,meta,source=g.calibrate_frames(self.sheet,self.source,self.contract,self.report)
        self.assertEqual(meta['scale'],.5)
        self.assertGreater(frames[0].width,64)
        self.assertEqual(len(set(f.size for f in frames)),1)
        self.assertEqual(frames[0].getbbox()[3]-frames[1].getbbox()[3],20)
        self.assertEqual(source.size,frames[0].size)
        for f in meta['frames']:
            self.assertAlmostEqual(f['output_body_anchor'][0],meta['pivot_top_left_px'][0])

    def test_source_anatomy_changes_scale_even_same_canvas(self):
        c=copy.deepcopy(self.contract)
        c['anchors']['head']['points'][1]=[19,20]
        c['anchors']['torso']['points'][1]=[30,25]
        _,meta,_=g.calibrate_frames(self.sheet,self.source,c,self.report)
        self.assertEqual(meta['scale'],.25)

    def test_bad_head_or_torso_cannot_be_fixed_by_per_frame_resize(self):
        for name,end in [('head',[210,200]),('torso',[240,280])]:
            r=copy.deepcopy(self.report)
            r['frames'][7]['anchors'][name]['points'][1]=end
            with self.assertRaisesRegex(WorkflowError,'ANATOMICAL_SCALE_MISMATCH'):
                g.calibrate_frames(self.sheet,self.source,self.contract,r)

    def test_stale_source_and_missing_frame_rejected(self):
        c=copy.deepcopy(self.contract);c['source_sha256']='0'*64
        with self.assertRaisesRegex(WorkflowError,'SOURCE_GEOMETRY_STALE'):
            g.validate_source_geometry(self.source,c)
        r=copy.deepcopy(self.report);r['frames'].pop()
        with self.assertRaises(WorkflowError):g.calibrate_frames(self.sheet,self.source,self.contract,r)

    def test_fragment_requires_decision_before_output(self):
        ImageDraw.Draw(self.sheet).rectangle((240,80,245,85),fill=(90,90,90,255))
        with self.assertRaisesRegex(WorkflowError,'COMPONENT_DECISIONS_REQUIRED'):
            g.calibrate_frames(self.sheet,self.source,self.contract,self.report)
        diag=analyze_components(self.sheet.crop((0,0,512,512)))
        self.report['frames'][0]['component_decisions']=[{'id':c['id'],
            'decision':'remove_as_background','notes':'Detached square above pose'} for c in diag['components'] if not c['primary']]
        frames,meta,_=g.calibrate_frames(self.sheet,self.source,self.contract,self.report)
        self.assertEqual(meta['frames'][0]['removed_component_pixels'],36)

    def test_anchors_must_touch_visible_art(self):
        r=copy.deepcopy(self.report);r['frames'][0]['body_anchor']=[0,0]
        with self.assertRaisesRegex(WorkflowError,'GEOMETRY_POINT_NOT_ON_ART'):
            g.calibrate_frames(self.sheet,self.source,self.contract,r)

    def test_source_anchor_cannot_be_removed_by_component_decision(self):
        im=Image.open(self.source).convert('RGBA')
        ImageDraw.Draw(im).rectangle((2,2,8,8),fill=(60,60,60,255));im.save(self.source)
        c=copy.deepcopy(self.contract);c['source_sha256']=sha_file(self.source)
        c['anchors']['head']['points']=[[2,2],[8,2]]
        c['component_decisions']=[{'id':v['id'],'decision':'remove_as_background',
            'notes':'Synthetic detached artifact to remove'} for v in analyze_components(im)['components'] if not v['primary']]
        with self.assertRaisesRegex(WorkflowError,'GEOMETRY_POINT_NOT_ON_ART'):
            g.validate_source_geometry(self.source,c)

    def test_source_area_and_unknown_primary_rejected_before_generation(self):
        im=Image.new('RGBA',(1024,1024))
        ImageDraw.Draw(im).rectangle((12,12,50,50),fill=(60,60,60,255));im.save(self.source)
        c=copy.deepcopy(self.contract);c['source_sha256']=sha_file(self.source)
        with self.assertRaisesRegex(WorkflowError,'SOURCE_CANVAS_LIMIT'):
            g.validate_source_geometry(self.source,c)
        im.crop((0,0,64,64)).save(self.source);c['source_sha256']=sha_file(self.source)
        c['primary_anchor']='missing'
        with self.assertRaisesRegex(WorkflowError,'PRIMARY_ANCHOR_INVALID'):
            g.validate_source_geometry(self.source,c)


if __name__=='__main__':unittest.main()
