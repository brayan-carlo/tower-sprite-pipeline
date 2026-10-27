"""Bounded H3 source preparation and video-to-grid technical sampling."""
import io
import math
import os
from pathlib import Path
import shutil
import subprocess
from PIL import Image, ImageChops, ImageStat
from sheet_workflow_state import WorkflowError, sha_file, new_or_identical, encoded

class VideoError(WorkflowError):pass

def resolve_ffmpeg(explicit=None):
    candidate=explicit or os.environ.get('TOWER_SPRITE_FFMPEG_EXE') or shutil.which('ffmpeg')
    if not candidate:
        roots=[Path.home()/'.codex/work/tower-sprite-pipeline',Path.home()/'.cache/codex-runtimes']
        for root in roots:
            matches=sorted(root.glob('*/runtimes/video-analysis-*/imageio_ffmpeg/binaries/ffmpeg*')) if root.exists() else []
            if matches:candidate=str(matches[-1]);break
    if not candidate:raise VideoError('RUNTIME_REQUIRED')
    p=Path(candidate).expanduser().resolve()
    if not p.is_file():raise VideoError('RUNTIME_REQUIRED')
    try:r=subprocess.run([str(p),'-version'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired):raise VideoError('RUNTIME_REQUIRED') from None
    if r.returncode or len(r.stdout)>65536 or not r.stdout.startswith(b'ffmpeg version'):raise VideoError('RUNTIME_REQUIRED')
    return str(p)

def png_bytes(image):
    b=io.BytesIO();image.save(b,format='PNG');return b.getvalue()

def prepare_source(source, output_path, *, pixel_art=True):
    source=Path(source);dest=Path(output_path)
    with Image.open(source) as opened:im=opened.convert('RGBA')
    scale=min(1024/im.width,1024/im.height);size=(max(1,round(im.width*scale)),max(1,round(im.height*scale)))
    im=im.resize(size,Image.Resampling.NEAREST if pixel_art else Image.Resampling.LANCZOS)
    out=Image.new('RGB',(1024,1024),(110,110,110));offset=((1024-size[0])//2,(1024-size[1])//2);out.paste(im,offset,im.getchannel('A'));new_or_identical(dest,png_bytes(out))
    return {'path':str(dest),'sha256':sha_file(dest),'original_sha256':sha_file(source),'transform':{'scale':scale,'size':list(size),'offset':list(offset),'canvas':[1024,1024],'background_rgb':[110,110,110]}}

def _ffmpeg(exe,args,maximum,timeout=45):
    try:r=subprocess.run([exe,'-nostdin','-v','error',*args],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired):raise VideoError('VIDEO_DECODE_FAILED') from None
    if r.returncode or len(r.stdout)>maximum or len(r.stderr)>65536:raise VideoError('VIDEO_DECODE_FAILED')
    return r.stdout

def sample_video(raw_path, output_dir, *, ffmpeg_exe, frame_count, loop, cycle_start_seconds=None, cycle_duration_seconds=None):
    raw=Path(raw_path);out=Path(output_dir);exe=resolve_ffmpeg(ffmpeg_exe)
    if not raw.is_file() or raw.stat().st_size>100*1024*1024 or raw.stat().st_size==0:raise VideoError('VIDEO_INVALID')
    if not isinstance(frame_count,int) or isinstance(frame_count,bool) or not 2<=frame_count<=16:raise VideoError('INPUT_INVALID')
    if (cycle_start_seconds is None)!=(cycle_duration_seconds is None):raise VideoError('CYCLE_OPTIONS_INVALID')
    data=_ffmpeg(exe,['-i',str(raw),'-t','30','-an','-vf','fps=24,scale=96:96','-frames:v','721','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],721*96*96*3)
    unit=96*96*3
    if len(data)%unit or not frame_count<=len(data)//unit<=720:raise VideoError('VIDEO_ANALYSIS_INVALID')
    count=len(data)//unit;fps=24;small=[]
    for offset in range(0,len(data),unit):
        im=Image.frombytes('RGB',(96,96),data[offset:offset+unit]);small.append(im.crop((10,38,86,91)).resize((32,32),Image.Resampling.BILINEAR))
    del data
    period=None;candidates=[];limitation='Image repetition is a technical heuristic; it does not prove gait, contact alternation, identity, or artistic quality.'
    if cycle_start_seconds is not None:
        if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (cycle_start_seconds,cycle_duration_seconds)) or cycle_start_seconds<0 or cycle_duration_seconds<=0:raise VideoError('CYCLE_OPTIONS_INVALID')
        start=round(cycle_start_seconds*fps);period=round(cycle_duration_seconds*fps)
        if start+period>count or period<frame_count:raise VideoError('CYCLE_OPTIONS_INVALID')
        indices=[start+round(i*period/frame_count) for i in range(frame_count)]
    elif loop:
        def distance(a,b):return sum(ImageStat.Stat(ImageChops.difference(a,b)).mean)/3
        motion=sum(distance(a,b) for a,b in zip(small,small[1:]))/(count-1)
        if motion<.1:raise VideoError('CYCLE_UNRELIABLE_ZERO_MOTION')
        for lag in range(18,min(60,count//2)+1):
            pairs=[distance(small[i],small[i+lag]) for i in range(8,count-lag-6)]
            if pairs:candidates.append({'period_frames':lag,'mean_absolute_difference':sum(pairs)/len(pairs)})
        if not candidates:raise VideoError('CYCLE_UNRELIABLE_TOO_SHORT')
        candidates.sort(key=lambda c:(c['mean_absolute_difference'],c['period_frames']));best=candidates[0];period=best['period_frames']
        median=sorted(c['mean_absolute_difference'] for c in candidates)[len(candidates)//2]
        if median<.1 or best['mean_absolute_difference']>median*.8:raise VideoError('CYCLE_UNRELIABLE_FLAT_SCORES')
        start=period if period*2<=count else (count-period)//2
        indices=[start+round(i*period/frame_count) for i in range(frame_count)]
    else:indices=[round(i*(count-1)/(frame_count-1)) for i in range(frame_count)]
    if len(set(indices))!=frame_count or min(indices)<0 or max(indices)>=count:raise VideoError('CYCLE_OPTIONS_INVALID')
    out.mkdir(parents=True,exist_ok=True);paths=[];frames=[]
    for i,index in enumerate(indices):
        payload=_ffmpeg(exe,['-ss',format(index/fps,'.9f'),'-i',str(raw),'-an','-vf','scale=960:960:force_original_aspect_ratio=decrease','-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],10*1024*1024)
        try:
            with Image.open(io.BytesIO(payload)) as image:
                if image.width>960 or image.height>960:raise VideoError('VIDEO_FRAME_INVALID')
                frame=image.convert('RGB')
        except (OSError,ValueError):raise VideoError('VIDEO_FRAME_INVALID') from None
        dest=out/f'frame-{i+1:02d}.png';new_or_identical(dest,png_bytes(frame));paths.append(str(dest));frames.append(frame)
    if len({f.size for f in frames})!=1:raise VideoError('VIDEO_FRAME_INVALID')
    cols=min(4,frame_count);rows=math.ceil(frame_count/cols);w,h=frames[0].size
    if cols*w*rows*h>64_000_000:raise VideoError('VIDEO_GRID_TOO_LARGE')
    grid=Image.new('RGB',(cols*w,rows*h),(110,110,110))
    for i,frame in enumerate(frames):grid.paste(frame,((i%cols)*w,(i//cols)*h))
    grid_path=out/'sample-grid.png';payload=png_bytes(grid)
    if len(payload)>10*1024*1024:raise VideoError('VIDEO_GRID_TOO_LARGE')
    new_or_identical(grid_path,payload)
    selection={'schema_version':3,'raw_sha256':sha_file(raw),'analysis_fps':24,'analysis_frame_count':count,'analysis_frame_indices':indices,'sample_times_seconds':[i/fps for i in indices],'period_frames':period,'period_candidates':candidates,'heuristic_limitation':limitation,'explicit_cycle':cycle_start_seconds is not None,'grid_sha256':sha_file(grid_path),'frame_sha256':[sha_file(p) for p in paths]}
    selection_path=out/'selection.json';new_or_identical(selection_path,encoded(selection))
    return {'grid_path':str(grid_path),'frame_paths':paths,'selection_path':str(selection_path),'selection':selection}
