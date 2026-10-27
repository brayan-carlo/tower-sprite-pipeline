"""Pixel-bound diagnostics for manually observed held-prop landmarks.

No natural-motion classifier or universal acceptance threshold is implied.
Coordinates must mark visible art; occluded landmarks require visual review
instead of fabricated coordinates. All cyclic transitions use actual timing.
"""
import hashlib
import math
import statistics

def _point(value, image):
    if not isinstance(value,(list,tuple)) or len(value)!=2 or any(
        type(v) not in (int,float) or not math.isfinite(v) for v in value):
        raise ValueError('Visible finite landmark required')
    x,y=value
    if not (0<=x<image.width and 0<=y<image.height):raise ValueError('Landmark outside frame')
    x,y=round(x),round(y)
    if x>=image.width or y>=image.height or image.getpixel((x,y))[3]<16:
        raise ValueError('Rounded landmark pixel not on visible art')
    return list(value)

def _delta(a,b):return (b-a+180)%360-180

def rigid_prop_diagnostics(frames, annotations, frame_ms):
    if type(frame_ms) is not int or frame_ms<=0:raise ValueError('Positive integer timing required')
    if len(frames)<3 or len(frames)!=len(annotations):raise ValueError('Complete cycle required')
    if [a.get('frame') for a in annotations]!=list(range(1,len(frames)+1)):
        raise ValueError('Unique ordered frame ordinals required')
    if any(im.mode!='RGBA' or im.size!=frames[0].size for im in frames):
        raise ValueError('RGBA frames with common canvas required')
    records=[]
    for i,(im,a) in enumerate(zip(frames,annotations)):
        if not isinstance(a.get('notes'),str) or len(a['notes'].strip())<10:
            raise ValueError('Observed landmark notes required')
        p={name:_point(a.get(name),im) for name in ('body','hand','grip','guard','tip')}
        dx=p['tip'][0]-p['guard'][0];dy=p['tip'][1]-p['guard'][1]
        length=math.hypot(dx,dy)
        if length<4:raise ValueError('Blade axis too short to measure')
        digest=hashlib.sha256(im.width.to_bytes(4,'big')+im.height.to_bytes(4,'big')+im.tobytes()).hexdigest()
        records.append({'frame':i+1,'pixel_sha256':digest,'landmarks':p,'notes':a['notes'],
            'blade_length_pixels':length,'blade_angle_degrees':math.degrees(math.atan2(dy,dx)),
            'hand_grip_distance_pixels':math.dist(p['hand'],p['grip']),
            'hand_relative_body':[p['hand'][j]-p['body'][j] for j in (0,1)],
            'grip_relative_body':[p['grip'][j]-p['body'][j] for j in (0,1)],
            'tip_relative_grip':[p['tip'][j]-p['grip'][j] for j in (0,1)]})
    transitions=[];seconds=frame_ms/1000
    for i,a in enumerate(records):
        b=records[(i+1)%len(records)];angle=_delta(a['blade_angle_degrees'],b['blade_angle_degrees'])
        transitions.append({'frames':[a['frame'],b['frame']],'angle_delta_degrees':angle,
            'half_turn_direction_ambiguous':math.isclose(abs(angle),180,abs_tol=1e-9),
            'angle_velocity_degrees_per_second':angle/seconds,
            'hand_relative_body_distance_pixels':math.dist(a['hand_relative_body'],b['hand_relative_body']),
            'grip_relative_body_distance_pixels':math.dist(a['grip_relative_body'],b['grip_relative_body']),
            'tip_relative_grip_distance_pixels':math.dist(a['tip_relative_grip'],b['tip_relative_grip'])})
    for i,t in enumerate(transitions):
        previous=transitions[i-1]['angle_velocity_degrees_per_second']
        t['angle_acceleration_degrees_per_second_squared']=(t['angle_velocity_degrees_per_second']-previous)/seconds
    lengths=[r['blade_length_pixels'] for r in records]
    return {'schema':'native_rigid_prop_diagnostics_v1','frame_ms':frame_ms,
        'frames':records,'transitions':transitions,'median_blade_length_pixels':statistics.median(lengths),
        'length_range_pixels':max(lengths)-min(lengths),'visual_review_required':True,
        'angle_assumption':'Shortest-arc discrete differences; half-turn direction is ambiguous. Sparse poses do not reveal physical motion between frames.',
        'derivative_method':'Finite differences using the requested frame interval, not measured continuous physical velocity or acceleration.',
        'measurement_status':'manual_landmarks_not_automatic_anatomical_recognition'}
