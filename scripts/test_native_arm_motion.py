import copy,math,unittest
from PIL import Image,ImageDraw
from native_arm_motion import arm_prop_diagnostics

def fixture():
    frames=[];annotations=[]
    for i,degrees in enumerate([-4,-2,0,2,4,2,0,-2]):
        a=math.radians(degrees)
        def end(length):return [80+length*math.sin(a),100+length*math.cos(a)]
        points={'shoulder':[80,60],'elbow':[80,100],'body':[115,95],
                'hand':end(35),'grip':end(42),'guard':end(48),'tip':end(75)}
        im=Image.new('RGBA',(220,220));draw=ImageDraw.Draw(im)
        for p in points.values():
            x,y=map(round,p);draw.ellipse((x-2,y-2,x+2,y+2),fill=(180,140,80,255))
        frames.append(im);annotations.append({'frame':i+1,**points,'notes':'Synthetic visible landmarks for an articulated arm and rigid prop.'})
    return frames,annotations

class ArmMotionTests(unittest.TestCase):
    def measure(self,f,a,method='construction_transformed',uncertainty=0):
        return arm_prop_diagnostics(f,a,100,annotation_method=method,uncertainty_pixels=uncertainty)
    def test_elbow_flex_is_measured_separately_from_rigid_blade(self):
        f,a=fixture();r=self.measure(f,a)
        self.assertAlmostEqual(r['arm']['upper_segment_range_pixels'],0)
        self.assertAlmostEqual(r['arm']['forearm_segment_range_pixels'],0)
        self.assertAlmostEqual(r['arm']['elbow_angle_range_degrees'],8)
        self.assertAlmostEqual(r['prop']['length_range_pixels'],0)
        self.assertIs(r['delivery_allowed'],False)
        self.assertIs(r['visual_review_required'],True)
    def test_eight_to_one_is_included(self):
        f,a=fixture();r=self.measure(f,a)
        self.assertEqual(r['arm']['transitions'][-1]['frames'],[8,1])
        self.assertAlmostEqual(r['arm']['transitions'][-1]['elbow_angle_delta_degrees'],2)
        self.assertEqual(len(r['arm']['transitions']),8)
    def test_absent_elbow_is_not_invented(self):
        f,a=fixture();a[2].pop('elbow')
        with self.assertRaises(ValueError):self.measure(f,a)
    def test_transparent_joint_pixel_is_rejected(self):
        f,a=fixture();f[3].putpixel((80,100),(0,0,0,0))
        with self.assertRaises(ValueError):self.measure(f,a)
    def test_method_and_uncertainty_are_required_and_not_quality_claims(self):
        f,a=fixture()
        with self.assertRaises(ValueError):self.measure(f,a,'automatically_approved')
        for bad in [float('nan'),-1,True]:
            with self.assertRaises(ValueError):self.measure(f,a,uncertainty=bad)
        r=self.measure(f,a,'manual_visible_landmarks',3)
        self.assertEqual(r['uncertainty_pixels'],3)
    def test_zero_length_arm_axis_is_rejected(self):
        f,a=fixture();a[0]['shoulder']=a[0]['elbow']
        with self.assertRaises(ValueError):self.measure(f,a)
    def test_same_arm_angles_are_reported_without_automatic_rejection(self):
        f,a=fixture();fixed=copy.deepcopy(a[2]);a=[dict(fixed,frame=i+1) for i in range(8)];f=[f[2].copy() for _ in range(8)]
        r=self.measure(f,a)
        self.assertEqual(r['arm']['elbow_angle_range_degrees'],0)
        self.assertFalse(r['delivery_allowed'])
        self.assertNotIn('artistic_decision',r)

if __name__=='__main__':unittest.main()
