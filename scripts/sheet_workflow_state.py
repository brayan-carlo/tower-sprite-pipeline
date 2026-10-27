"""Durable local scope/state and crash-released OS advisory locking."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

class WorkflowError(ValueError):
    def __init__(self, code): self.code=code;super().__init__(code)

def sha_file(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file():raise WorkflowError('FILE_INVALID')
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def encoded(value):return (json.dumps(value,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
def scope_sha(value):return hashlib.sha256(encoded(value)).hexdigest()
def read_json(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>5*1024*1024:raise WorkflowError('STATE_INVALID')
    try:return json.loads(p.read_text('utf-8'))
    except (ValueError,UnicodeError):raise WorkflowError('STATE_INVALID') from None

def atomic_json(path,value):
    p=Path(path);payload=encoded(value)
    if p.is_symlink():raise WorkflowError('OUTPUT_CONFLICT')
    temp=p.with_name('.'+p.name+'.'+uuid.uuid4().hex+'.partial')
    with temp.open('xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
    os.replace(temp,p)

def new_or_identical(path,payload):
    p=Path(path)
    if p.exists():
        if p.is_symlink() or p.read_bytes()!=payload:raise WorkflowError('OUTPUT_CONFLICT')
        return
    with p.open('xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())

@contextmanager
def run_lock(directory):
    """Lock held by the OS, never a stale-file heuristic or transport claim deletion."""
    p=Path(directory)/'.run.lock'
    if p.is_symlink():raise WorkflowError('RUN_LOCKED')
    with p.open('a+b') as f:
        f.seek(0,2)
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise WorkflowError('RUN_LOCKED') from None
        try:yield
        finally:
            f.seek(0)
            if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)
