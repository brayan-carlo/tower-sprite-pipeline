"""Pixel-bound arm articulation and rigid-prop diagnostics, never art approval.

Observed landmarks and transformed construction points are different evidence.
The selected annotation method is retained explicitly. A visible pixel does not
prove that an occluded anatomical joint was identified correctly.
"""
import math
from native_motion import rigid_prop_diagnostics, _point, _delta

def _angle(a,b):
    return math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))

def arm_prop_diagnostics(frames, annotations, frame_ms, *, annotation_method, uncertainty_pixels):
    if annotation_method not in ('manual_visible_landmarks','construction_transformed'):
        raise ValueError('Explicit annotation method required')
    if (type(uncertainty_pixels) not in (int,float) or not math.isfinite(uncertainty_pixels)
            or uncertainty_pixels<0):
        raise ValueError('Finite nonnegative landmark uncertainty required')
    prop=rigid_prop_diagnostics(frames,annotations,frame_ms)
    records=[]
    for im,annotation,p in zip(frames,annotations,prop['frames']):
        shoulder=_point(annotation.get('shoulder'),im)
        elbow=_point(annotation.get('elbow'),im)
        hand=p['landmarks']['hand']
        upper=math.dist(shoulder,elbow);forearm=math.dist(elbow,hand)
        if min(upper,forearm)<4:raise ValueError('Arm segment too short to measure')
        upper_angle=_angle(shoulder,elbow);forearm_angle=_angle(elbow,hand)
        records.append({'frame':p['frame'],'pixel_sha256':p['pixel_sha256'],
            'landmarks':{'shoulder':shoulder,'elbow':elbow,'hand':hand},
            'upper_segment_pixels':upper,'forearm_segment_pixels':forearm,
            'upper_segment_angle_degrees':upper_angle,'forearm_segment_angle_degrees':forearm_angle,
            'elbow_angle_degrees':_delta(upper_angle,forearm_angle),
            'prop_relative_forearm_degrees':_delta(forearm_angle,p['blade_angle_degrees'])})
    transitions=[]
    for i,a in enumerate(records):
        b=records[(i+1)%len(records)]
        diffs={name:_delta(a[key],b[key]) for name,key in [
            ('shoulder_angle_delta_degrees','upper_segment_angle_degrees'),
            ('forearm_angle_delta_degrees','forearm_segment_angle_degrees'),
            ('elbow_angle_delta_degrees','elbow_angle_degrees')]}
        transitions.append({'frames':[a['frame'],b['frame']],**diffs,
            'elbow_velocity_degrees_per_second':diffs['elbow_angle_delta_degrees']/(frame_ms/1000),
            'half_turn_direction_ambiguous':any(math.isclose(abs(v),180,abs_tol=1e-9) for v in diffs.values())})
    def span(key):
        values=[r[key] for r in records];return max(values)-min(values)
    # Unwrap around the first recorded pose to avoid a spurious 358-degree span.
    relative=[_delta(records[0]['elbow_angle_degrees'],r['elbow_angle_degrees']) for r in records]
    return {'schema':'native_arm_prop_diagnostics_v1','frame_ms':frame_ms,
        'annotation_method':annotation_method,'uncertainty_pixels':uncertainty_pixels,
        'prop':prop,'arm':{'frames':records,'transitions':transitions,
            'upper_segment_range_pixels':span('upper_segment_pixels'),
            'forearm_segment_range_pixels':span('forearm_segment_pixels'),
            'elbow_angle_range_degrees':max(relative)-min(relative)},
        'visual_review_required':True,'delivery_allowed':False,
        'limitations':[
            'Arm points describe annotated segments, not automatic identification of bones or joints.',
            'Construction-transformed coordinates verify the rig, not independent perceived fluency.',
            'A rigid prop may accompany either a flexing or a braced arm. Zero elbow variation is not automatically wrong.',
            'Angles use shortest-arc discrete differences. Sparse poses do not identify motion between frames.',
            'A range around the first pose does not establish cumulative rotations exceeding a half-turn.',
            'Natural support transfer, grip attachment and rhythm still require visual review of the actual cycle.']}
