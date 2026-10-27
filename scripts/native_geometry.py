"""Source-referenced, single-scale native assembly with explicit visual landmarks.

Landmarks are agent annotations, not automated proof of anatomy. The numeric
gate allows max(4 source pixels, 20 percent) measurement/pose variation; visual
review must still reject visible proportion or perspective changes.
"""
import math
import statistics
from pathlib import Path
from PIL import Image
from native_components import analyze_components, apply_component_review
from sheet_workflow_state import WorkflowError, sha_file, encoded
from sheet_images import (_png_bytes, _gif_bytes, _contact_image, _reject_duplicates,
    _validate_encoded_artifacts, _write_new_or_identical, consistency_diagnostics)


def _notes(value):
    if not isinstance(value,str) or not 10 <= len(value.strip()) <= 4000:
        raise WorkflowError('GEOMETRY_NOTES_REQUIRED')


def _point(value, image, visible=True):
    if not isinstance(value,list) or len(value)!=2 or any(
            type(v) not in (int,float) or not math.isfinite(v) for v in value):
        raise WorkflowError('GEOMETRY_POINT_INVALID')
    x,y=value
    if not (0<=x<image.width and 0<=y<image.height):
        raise WorkflowError('GEOMETRY_POINT_INVALID')
    if visible:
        x,y=round(x),round(y)
        area=image.getchannel('A').crop((max(0,x-2),max(0,y-2),min(image.width,x+3),min(image.height,y+3)))
        if area.getextrema()[1]<16:
            raise WorkflowError('GEOMETRY_POINT_NOT_ON_ART')
    return value


def _anchors(value,image):
    if not isinstance(value,dict) or len(value)!=2 or any(not isinstance(k,str) or not k.strip() for k in value):
        raise WorkflowError('TWO_ANATOMICAL_ANCHORS_REQUIRED')
    lengths={}
    for name,anchor in value.items():
        _notes(anchor.get('notes'))
        points=anchor.get('points')
        if not isinstance(points,list) or len(points)!=2:
            raise WorkflowError('GEOMETRY_POINT_INVALID')
        a,b=(_point(p,image) for p in points)
        distance=math.dist(a,b)
        if distance<4:raise WorkflowError('GEOMETRY_ANCHOR_TOO_SHORT')
        lengths[name]=distance
    return lengths


def validate_source_geometry(source,contract):
    if not isinstance(contract,dict) or contract.get('source_sha256')!=sha_file(source):
        raise WorkflowError('SOURCE_GEOMETRY_STALE')
    with Image.open(source) as im:im=im.convert('RGBA')
    if max(im.size)>1024 or im.width*im.height>1_000_000:raise WorkflowError('SOURCE_CANVAS_LIMIT')
    _notes(contract.get('notes'))
    lengths=_anchors(contract.get('anchors'),im)
    if contract.get('primary_anchor',sorted(lengths)[0]) not in lengths:
        raise WorkflowError('PRIMARY_ANCHOR_INVALID')
    _point(contract.get('body_anchor'),im)
    pivot=_point(contract.get('pivot'),im,False)
    if pivot[0]!=contract['body_anchor'][0]:
        raise WorkflowError('SOURCE_PIVOT_MUST_PROJECT_BODY_ANCHOR')
    components=analyze_components(im)
    reviewed=apply_component_review(im,components,contract.get('component_decisions'))
    _anchors(contract['anchors'],reviewed)
    _point(contract['body_anchor'],reviewed)
    return {'lengths':lengths,'components':components,'reviewed_source':reviewed}


def calibrate_frames(registered,source,contract,report,alignment_policy=None):
    if alignment_policy not in (None,'body_x_preserve_y_v1'):raise WorkflowError('ALIGNMENT_POLICY_UNSUPPORTED')
    reference=validate_source_geometry(source,contract)
    if registered.mode!='RGBA' or registered.size!=(2048,1024):
        raise WorkflowError('REGISTERED_SHEET_INVALID')
    annotations=report.get('frames')
    if not isinstance(annotations,list) or [a.get('frame') for a in annotations]!=list(range(1,9)):
        raise WorkflowError('GEOMETRY_FRAMES_INVALID')
    primary=contract.get('primary_anchor',sorted(contract['anchors'])[0])
    if primary not in contract['anchors']:raise WorkflowError('PRIMARY_ANCHOR_INVALID')
    names=[primary]+[n for n in contract['anchors'] if n!=primary]
    cells=[];lengths=[];diagnostics=[]
    for i,a in enumerate(annotations):
        cell=registered.crop(((i%4)*512,(i//4)*512,(i%4+1)*512,(i//4+1)*512))
        _notes(a.get('notes'))
        if set(a.get('anchors',{}))!=set(names):raise WorkflowError('GEOMETRY_ANCHOR_NAMES_MISMATCH')
        lengths.append(_anchors(a['anchors'],cell))
        _point(a.get('body_anchor'),cell)
        if i%2==0:_point(a.get('support_point'),cell)
        elif a.get('support_point') is not None:raise WorkflowError('SUSPENSION_MUST_NOT_BE_GROUNDED')
        diag=analyze_components(cell)
        cleaned=apply_component_review(cell,diag,a.get('component_decisions'))
        # Decisions must not remove the anatomical evidence used for alignment.
        _anchors(a['anchors'],cleaned);_point(a['body_anchor'],cleaned)
        if i%2==0:_point(a['support_point'],cleaned)
        cells.append(cleaned);diagnostics.append(diag)
    scale=reference['lengths'][names[0]]/statistics.median(l[names[0]] for l in lengths)
    if not .05<=scale<=8:raise WorkflowError('GEOMETRY_SCALE_INVALID')
    measurements=[]
    for i,values in enumerate(lengths):
        projected={name:values[name]*scale for name in names}
        for name,length in projected.items():
            if abs(length-reference['lengths'][name])>max(4,.2*reference['lengths'][name]):
                raise WorkflowError('ANATOMICAL_SCALE_MISMATCH')
        measurements.append(projected)
    row_grounds=[]
    for start in (0,4):
        ys=[annotations[i]['support_point'][1] for i in (start,start+2)]
        if abs(ys[0]-ys[1])*scale>4:raise WorkflowError('SUPPORT_PLANE_MISMATCH')
        row_grounds.append(statistics.mean(ys))
    origins=[row_grounds[0]]*8 if alignment_policy else [row_grounds[i//4] for i in range(8)]
    source_im=reference['reviewed_source'];sx,sy=contract['pivot']
    left,top,right,bottom=-sx,-sy,source_im.width-sx,source_im.height-sy
    poses=[]
    for i,cell in enumerate(cells):
        box=cell.getchannel('A').getbbox()
        if box is None:raise WorkflowError('EMPTY_FRAME')
        x,y,x2,y2=box
        crop=cell.crop(box)
        resized=crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))),Image.Resampling.NEAREST)
        dx=round((x-annotations[i]['body_anchor'][0])*scale)
        dy=round((y-origins[i])*scale)
        poses.append((resized,dx,dy,box))
        left=min(left,dx-8);top=min(top,dy-8)
        right=max(right,dx+resized.width+8);bottom=max(bottom,dy+resized.height+8)
    left=math.floor(left);top=math.floor(top)
    canvas=(math.ceil(right-left),math.ceil(bottom-top))
    if (max(canvas)>(4096 if alignment_policy else 1024)
            or canvas[0]*canvas[1]*(8 if alignment_policy else 1)>(64_000_000 if alignment_policy else 1_000_000)):
        raise WorkflowError('OUTPUT_CANVAS_LIMIT')
    pivot=[-left,-top];frames=[];frame_meta=[]
    for i,(pose,dx,dy,box) in enumerate(poses):
        frame=Image.new('RGBA',canvas);frame.alpha_composite(pose,(pivot[0]+dx,pivot[1]+dy));frames.append(frame)
        a=annotations[i];diag=diagnostics[i]
        removed={d['id'] for d in a['component_decisions'] if d['decision']=='remove_as_background'}
        frame_meta.append({'frame':i+1,'projected_anchor_lengths':measurements[i],
            'input_body_anchor':a['body_anchor'],
            'output_body_anchor':[pivot[0],pivot[1]+(a['body_anchor'][1]-origins[i])*scale],
            'support_point':a['support_point'],'registered_ground_y':origins[i],
            'crop_bbox':list(box),'paste':[pivot[0]+dx,pivot[1]+dy],
            'output_bbox':list(frame.getchannel('A').getbbox()),
            'component_report':diag,'component_decisions':a['component_decisions'],
            'removed_component_pixels':sum(c['pixels'] for c in diag['components'] if c['id'] in removed)})
    source_reference=Image.new('RGBA',canvas)
    source_reference.alpha_composite(source_im,(round(pivot[0]-sx),round(pivot[1]-sy)))
    metadata={'schema':'native_geometry_v1','source_sha256':sha_file(source),
        'source_lengths':reference['lengths'],'scale':scale,'primary_anchor':names[0],
        'policy':{'relative_tolerance':.2,'absolute_tolerance_source_px':4,'per_frame_scaling':False},
        'measurement_status':'agent_annotations_require_visual_review',
        'canvas':list(canvas),'pivot_top_left_px':pivot,
        'pivot_bottom_left_normalized':[pivot[0]/canvas[0],1-pivot[1]/canvas[1]],
        'source_canvas':list(source_im.size),'source_pivot':contract['pivot'],
        'source_component_report':reference['components'],
        'source_component_decisions':contract['component_decisions'],
        'frames':frame_meta}
    if alignment_policy:metadata['policy'].update(alignment_policy=alignment_policy,common_origin_y=origins[0],preserve_natural_y=True)
    return frames,metadata,source_reference


def write_artifacts(frames,metadata,source_reference,report,destination,frame_ms):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    _reject_duplicates(frames)
    w,h=frames[0].size
    sheet=Image.new('RGBA',(w*8,h))
    for i,frame in enumerate(frames):sheet.alpha_composite(frame,(w*i,0))
    fb=[_png_bytes(f) for f in frames];sb=_png_bytes(sheet)
    gb=_gif_bytes(frames,frame_ms,True);cb=_png_bytes(_contact_image(frames,pixel_art=True))
    dims,durations,contact_dims=_validate_encoded_artifacts(fb,sb,gb,cb,frame_count=8,
        canvas=(w,h),frame_ms=frame_ms,loop=True)
    qa={'state':'READY_FOR_VISUAL_REVIEW','delivery_allowed':False,'semantic_review_required':True,
        'consistency':consistency_diagnostics(frames),'canvas':[w,h],'frame_count':8,
        'frame_ms':frame_ms,'loop':True,'sheet_dimensions':dims,'contact_dimensions':contact_dims,
        'preview':{'frame_count':8,'durations_ms':durations,'loop':True},
        'normalization':{'scale':metadata['scale'],'source_referenced':True,'grounded':False}}
    paths={'frames':[str(destination/f'frame-{i+1:02d}.png') for i in range(8)],
        'sheet':str(destination/'sprite-sheet.png'),'preview_gif':str(destination/'preview.gif'),
        'contact':str(destination/'contact.png'),'qa_report':str(destination/'qa.json'),
        'geometry':str(destination/'geometry.json'),'geometry_review':str(destination/'geometry-review.json'),
        'source_reference':str(destination/'source-reference.png')}
    payloads={Path(p):b for p,b in zip(paths['frames'],fb)}
    payloads.update({Path(paths[k]):b for k,b in {'sheet':sb,'preview_gif':gb,'contact':cb,
        'qa_report':encoded(qa),'geometry':encoded(metadata),'geometry_review':encoded(report),
        'source_reference':_png_bytes(source_reference)}.items()})
    _write_new_or_identical(payloads)
    return {'artifact_paths':paths,'qa':qa,'geometry':metadata}
