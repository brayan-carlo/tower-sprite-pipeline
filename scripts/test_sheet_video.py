import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from PIL import Image, ImageDraw
from sheet_video import prepare_source, sample_video, resolve_ffmpeg, VideoError
try:
    FFMPEG=Path(resolve_ffmpeg())
except VideoError:
    FFMPEG=None
class VideoTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def video(self, flat=False):
        if FFMPEG is None:self.skipTest('FFmpeg unavailable; configure TOWER_SPRITE_FFMPEG_EXE')
        for i in range(96):
            im=Image.new('RGB',(128,128),(110,110,110));d=ImageDraw.Draw(im);k=0 if flat else i%24
            d.rectangle((35,20,85,70),fill=(180,60,70));d.rectangle((25+k,75,38+k,110),fill=(40,150,210));d.rectangle((85-k,75,98-k,110),fill=(210,150,30));im.save(self.root/f'input-{i:03d}.png')
        raw=self.root/'fixture.mp4';r=subprocess.run([str(FFMPEG),'-nostdin','-v','error','-framerate','24','-i',str(self.root/'input-%03d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(raw)],capture_output=True,timeout=30);self.assertEqual(r.returncode,0);return raw
    def test_prepare_preserves_original_and_gray_transform(self):
        p=self.root/'original.png';im=Image.new('RGBA',(300,600));ImageDraw.Draw(im).rectangle((20,20,280,580),fill=(200,40,60,255));im.save(p);before=p.read_bytes();r=prepare_source(p,self.root/'prepared.png',pixel_art=True);self.assertEqual(before,p.read_bytes());self.assertEqual(r['original_sha256'],hashlib.sha256(before).hexdigest())
        with Image.open(r['path']) as out:self.assertEqual(out.size,(1024,1024));self.assertEqual(out.getpixel((0,0)),(110,110,110))
    def test_real_loop_period_distinct_full_frames_and_recovery(self):
        raw=self.video();r=sample_video(raw,self.root/'sample',ffmpeg_exe=str(FFMPEG),frame_count=8,loop=True);self.assertEqual(r['selection']['period_frames'],24);self.assertEqual(r['selection']['analysis_frame_indices'],[24,27,30,33,36,39,42,45]);self.assertEqual(len(set(r['selection']['sample_times_seconds'])),8);self.assertEqual(len(r['frame_paths']),8);self.assertEqual(sample_video(raw,self.root/'sample',ffmpeg_exe=str(FFMPEG),frame_count=8,loop=True),r)
    def test_nonloop_and_explicit_cycle(self):
        raw=self.video();r=sample_video(raw,self.root/'nonloop',ffmpeg_exe=str(FFMPEG),frame_count=8,loop=False);self.assertIsNone(r['selection']['period_frames']);self.assertEqual(r['selection']['analysis_frame_indices'][0],0);self.assertEqual(r['selection']['analysis_frame_indices'][-1],95)
        r=sample_video(raw,self.root/'manual',ffmpeg_exe=str(FFMPEG),frame_count=8,loop=True,cycle_start_seconds=1,cycle_duration_seconds=1);self.assertEqual(r['selection']['analysis_frame_indices'],[24,27,30,33,36,39,42,45])
    def test_flat_video_rejected(self):
        with self.assertRaisesRegex(VideoError,'CYCLE_UNRELIABLE'):sample_video(self.video(flat=True),self.root/'flat',ffmpeg_exe=str(FFMPEG),frame_count=8,loop=True)
if __name__=='__main__':unittest.main()
