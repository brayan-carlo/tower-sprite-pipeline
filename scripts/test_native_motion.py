import unittest, math
from PIL import Image
from native_motion import rigid_prop_diagnostics

class RigidPropDiagnosticsTests(unittest.TestCase):
    def fixture(self):
        frames=[Image.new('RGBA',(256,256),(60,60,60,255)) for _ in range(8)]
        annotations=[]
        for i,angle in enumerate([120,125,130,125,120,115,110,115]):
            rad=math.radians(angle);grip=[120,120]
            annotations.append({'frame':i+1,'body':[140,90],'hand':grip,'grip':grip,
                'guard':[120,120],'tip':[120+70*math.cos(rad),120+70*math.sin(rad)],
                'notes':'Observed synthetic rigid prop used to test arithmetic only.'})
        return frames,annotations
    def test_measures_all_cyclic_steps_without_claiming_art_approval(self):
        frames,annotations=self.fixture()
        result=rigid_prop_diagnostics(frames,annotations,100)
        self.assertEqual(len(result['transitions']),8)
        self.assertEqual(result['transitions'][-1]['frames'],[8,1])
        self.assertAlmostEqual(result['transitions'][-1]['angle_delta_degrees'],5)
        self.assertAlmostEqual(result['length_range_pixels'],0)
        self.assertIs(result['visual_review_required'],True)
        self.assertNotIn('approved',result)
    def test_flags_are_measurements_not_invented_universal_thresholds(self):
        frames,annotations=self.fixture()
        annotations[7]['tip']=[220,120]
        annotations[7]['hand']=[80,120]
        result=rigid_prop_diagnostics(frames,annotations,100)
        self.assertAlmostEqual(result['frames'][7]['hand_grip_distance_pixels'],40)
        self.assertAlmostEqual(result['frames'][7]['blade_length_pixels'],100)
        self.assertAlmostEqual(result['transitions'][-1]['angle_delta_degrees'],120)
    def test_rejects_missing_off_art_nonfinite_and_duplicate_ordinals(self):
        frames,annotations=self.fixture()
        for bad in [None,[math.nan,1],[-1,10],[999,1]]:
            annotations[0]['tip']=bad
            with self.assertRaises(ValueError):rigid_prop_diagnostics(frames,annotations,100)
        frames,annotations=self.fixture();annotations[-1]['frame']=7
        with self.assertRaises(ValueError):rigid_prop_diagnostics(frames,annotations,100)
        frames,annotations=self.fixture();frames[0].putalpha(0)
        with self.assertRaises(ValueError):rigid_prop_diagnostics(frames,annotations,100)
    def test_hashes_bind_actual_pixels_and_timing_changes_velocity(self):
        frames,annotations=self.fixture();a=rigid_prop_diagnostics(frames,annotations,100)
        frames[0].putpixel((0,0),(10,10,10,255));b=rigid_prop_diagnostics(frames,annotations,200)
        self.assertNotEqual(a['frames'][0]['pixel_sha256'],b['frames'][0]['pixel_sha256'])
        self.assertAlmostEqual(a['transitions'][0]['angle_velocity_degrees_per_second']/2,
                               b['transitions'][0]['angle_velocity_degrees_per_second'])
    def test_transparent_landmark_is_rejected_even_next_to_visible_art(self):
        frames,annotations=self.fixture();frames[0].putpixel((120,120),(0,0,0,0))
        with self.assertRaises(ValueError):rigid_prop_diagnostics(frames,annotations,100)
    def test_half_turn_is_explicitly_ambiguous_and_hand_path_is_measured(self):
        frames,annotations=self.fixture()
        annotations[0]['tip']=[190,120];annotations[1]['tip']=[50,120]
        annotations[1]['hand']=[122,123]
        result=rigid_prop_diagnostics(frames,annotations,100)
        self.assertTrue(result['transitions'][0]['half_turn_direction_ambiguous'])
        self.assertAlmostEqual(result['transitions'][0]['hand_relative_body_distance_pixels'],math.sqrt(13))
if __name__=='__main__':unittest.main()
