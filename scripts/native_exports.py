"""Durable local export ownership and revocation; caller holds the run lock.

Intent is committed before destination writes. Recovery never overwrites a
receipt unless its bytes match a recorded original or revoked receipt.
"""
import hashlib
import os
from pathlib import Path

from generic_sprite_workflow import _review_target, _validate_visual_report
from sheet_workflow_state import (WorkflowError, atomic_json, encoded,
                                 new_or_identical, read_json, sha_file)


def safe_path(value):
    """Reject links, including ancestors, before resolving the destination."""
    path = Path(os.path.abspath(Path(value).expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise WorkflowError('OUTPUT_CONFLICT')
    return path


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _context(m, number):
    a = m['attempts'][number-1]
    return {'run_id':m['run_id'], 'attempts':m['attempts'][:number],
            'request':m['request'], 'artifact_hashes':a['artifact_hashes'],
            'consistency':a['consistency']}


def export_summary(m):
    records = m.get('exports', [])
    return {'export_revocation_pending':any(r['status']=='REVOCATION_PENDING' for r in records),
            'export_pending':any(r['status']=='EXPORT_PENDING' for r in records),
            'export_destinations':[{'destination':r['destination'], 'status':r['status'],
                                    'errors':r.get('errors', {})} for r in records]}


def alignment_required(m):
    request=m.get('request',{})
    if 'alignment_policy' not in request:return False
    if request['alignment_policy']!='body_x_preserve_y_v1':
        raise WorkflowError('ALIGNMENT_POLICY_UNSUPPORTED')
    return True


def require_direct_delivery(m):
    if alignment_required(m):raise WorkflowError('ALIGNMENT_REQUIRED')


def delivery_allowed(m):
    summary = export_summary(m)
    return (not alignment_required(m) and m['state']=='READY_FOR_DELIVERY' and not summary['export_pending']
            and not summary['export_revocation_pending'])


def register_export(path, m, destination, payloads, artifact_set):
    require_direct_delivery(m)
    destination = safe_path(destination)
    hashes = {Path(p).name:_hash(data) for p, data in payloads.items()}
    for record in m.get('exports', []):
        if record['destination'] == str(destination):
            if (record['hashes'] != hashes or record['artifact_set_sha256'] != artifact_set
                    or record['status'] in ('REVOCATION_PENDING', 'REVOKED')):
                raise WorkflowError('OUTPUT_CONFLICT')
            return record
    # Check the entire payload before creating even the first destination file.
    for name, digest in hashes.items():
        target = safe_path(destination/name)
        if target.exists() and (not target.is_file() or sha_file(target) != digest):
            raise WorkflowError('OUTPUT_CONFLICT')
    record = {'destination':str(destination), 'attempt':len(m['attempts']),
              'artifact_set_sha256':artifact_set, 'hashes':hashes,
              'receipts':{Path(p).name:data.decode('utf-8') for p, data in payloads.items()
                          if Path(p).name in ('delivery.json', 'visual-review.json')},
              'status':'EXPORT_PENDING', 'origin':'registered_before_write'}
    m.setdefault('exports', []).append(record)
    atomic_json(path, m)
    return record


def write_export(path, m, record, payloads):
    require_direct_delivery(m)
    destination = safe_path(record['destination'])
    # Export recovery must not publish any receipt until all artifacts are present.
    for target, payload in payloads.items():
        target = safe_path(target)
        if target.parent != destination or _hash(payload) != record['hashes'].get(target.name):
            raise WorkflowError('OUTPUT_CONFLICT')
        if target.exists() and (not target.is_file() or sha_file(target) != _hash(payload)):
            raise WorkflowError('OUTPUT_CONFLICT')
    destination.mkdir(parents=True, exist_ok=True)
    names = sorted(payloads, key=lambda p: (Path(p).name in record['receipts'], str(p)))
    for target in names:
        safe_path(target)
        new_or_identical(target, payloads[target])
        if sha_file(target) != record['hashes'][Path(target).name]:
            raise WorkflowError('OUTPUT_CONFLICT')
    record['status'] = 'EXPORTED'
    record.pop('errors', None)
    atomic_json(path, m)


def mark_revoked(m):
    """Stage revocation in the same manifest transaction as the rejected review."""
    import json
    for record in m.get('exports', []):
        if record['status'] == 'REVOKED':
            continue
        record['status'] = 'REVOCATION_PENDING'
        if 'revoked_receipts' in record:
            continue
        delivery = json.loads(record['receipts']['delivery.json'])
        delivery.update(state='VISUAL_REJECTED', delivery_allowed=False, revoked=True,
                        revocation_reason='approval_withdrawn')
        visual = {'decision':'reject', 'revoked':True, 'delivery_allowed':False,
                  'artifact_set_sha256':record['artifact_set_sha256'],
                  'revocation_reason':'approval_withdrawn',
                  'previous_review':json.loads(record['receipts']['visual-review.json'])}
        record['revoked_receipts'] = {'delivery.json':delivery, 'visual-review.json':visual}


def revoke_exports(path, m):
    for record in m.get('exports', []):
        if record['status'] != 'REVOCATION_PENDING':
            continue
        errors = {}
        for name, value in record['revoked_receipts'].items():
            try:
                target = safe_path(Path(record['destination'])/name)
                if not target.parent.is_dir():
                    raise WorkflowError('EXPORT_DESTINATION_UNAVAILABLE')
                revoked_hash = _hash(encoded(value))
                if target.exists():
                    digest = sha_file(target)
                    if digest == revoked_hash:
                        continue
                    if digest != record['hashes'][name]:
                        raise WorkflowError('OUTPUT_CONFLICT')
                    atomic_json(target, value)
                else:
                    # A partial export can lack a receipt. Create only exclusively.
                    new_or_identical(target, encoded(value))
                if sha_file(target) != revoked_hash:
                    raise WorkflowError('OUTPUT_CONFLICT')
            except (OSError, WorkflowError) as exc:
                errors[name] = getattr(exc, 'code', 'EXPORT_DESTINATION_UNAVAILABLE')
        record['errors'] = errors
        record['status'] = 'REVOCATION_PENDING' if errors else 'REVOKED'
        atomic_json(path, m)
    return export_summary(m)


def reconcile_exports(path, m):
    if alignment_required(m) or m['state'] != 'READY_FOR_DELIVERY':
        mark_revoked(m)
        atomic_json(path, m)
    revoke_exports(path, m)
    for record in m.get('exports', []):
        if record['status'] != 'EXPORT_PENDING':
            continue
        try:
            a = m['attempts'][record['attempt']-1]
            if (_review_target(_context(m, record['attempt'])) != record['artifact_set_sha256']
                    or a.get('visual_review', {}).get('decision') != 'pass'):
                raise WorkflowError('VISUAL_REVIEW_REQUIRED')
            destination = safe_path(record['destination'])
            payloads = {destination/Path(p).name:Path(p).read_bytes() for p in a['artifact_hashes']}
            payloads.update({destination/name:value.encode('utf-8')
                             for name, value in record['receipts'].items()})
            write_export(path, m, record, payloads)
        except (OSError, WorkflowError) as exc:
            record['errors'] = {'export':getattr(exc, 'code', 'EXPORT_DESTINATION_UNAVAILABLE')}
            atomic_json(path, m)
    return export_summary(m)


def adopt_export(path, m, destination):
    """Register a verified existing export, without claiming a historical write."""
    require_direct_delivery(m)
    destination = safe_path(destination)
    existing = next((r for r in m.get('exports', []) if r['destination']==str(destination)), None)
    if existing:
        revoke_exports(path, m)
        return existing
    try:
        delivery = read_json(safe_path(destination/'delivery.json'))
        visual = read_json(safe_path(destination/'visual-review.json'))
        if (delivery.get('schema') != m['schema'] or delivery.get('run_manifest') != str(path)
                or delivery.get('source_sha256') != m['source']['sha256']
                or delivery.get('request') != m['request']
                or delivery.get('state') != 'READY_FOR_DELIVERY' or visual.get('decision') != 'pass'):
            raise WorkflowError('EXPORT_ADOPTION_INVALID')
        number = next((i for i, a in enumerate(m['attempts'], 1)
                       if 'artifact_hashes' in a and _review_target(_context(m, i)) == delivery.get('artifact_set_sha256')), None)
        if number is None:
            raise WorkflowError('EXPORT_ADOPTION_INVALID')
        a = m['attempts'][number-1]
        if delivery.get('raw_sha256') != a['raw']['sha256']:
            raise WorkflowError('EXPORT_ADOPTION_INVALID')
        _validate_visual_report(_context(m, number), visual)
        hashes = {}
        for source, digest in a['artifact_hashes'].items():
            name = Path(source).name
            if name in hashes or sha_file(safe_path(destination/name)) != digest:
                raise WorkflowError('EXPORT_ADOPTION_INVALID')
            hashes[name] = digest
        receipts = {name:safe_path(destination/name).read_text('utf-8')
                    for name in ('delivery.json', 'visual-review.json')}
        hashes.update({name:sha_file(destination/name) for name in receipts})
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        raise WorkflowError('EXPORT_ADOPTION_INVALID') from exc
    record = {'destination':str(destination), 'attempt':number,
              'artifact_set_sha256':delivery['artifact_set_sha256'], 'hashes':hashes,
              'receipts':receipts, 'status':'EXPORTED', 'origin':'adopted_existing_export'}
    m.setdefault('exports', []).append(record)
    if m['state'] != 'READY_FOR_DELIVERY' or a.get('visual_review', {}).get('decision') != 'pass':
        mark_revoked(m)
    atomic_json(path, m)
    revoke_exports(path, m)
    return record
