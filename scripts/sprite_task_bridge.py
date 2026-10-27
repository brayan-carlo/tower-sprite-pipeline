"""Fixed stdin-only managed transport boundary; never executes a program/provider.

The facade verifies actual preparation/sampling/artwork before materialization.
This bridge independently checks sealed identities, exact file/receipt hashes and
the ledger binding, then delegates compare-and-mark to its single task lock.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys
from urllib.parse import urlencode

import sprite_task_ledger as ledger
from sheet_workflow_state import WorkflowError, encoded, scope_sha

MAX_INPUT_BYTES = 65536
_HEX = re.compile(r'^[a-f0-9]{64}$')
_UUID = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$')
_MODELS = dict(sheet='fal-ai/nano-banana-2/edit', video='minimax/h3-max/image-to-video')
_CLEANUP = 'fal-ai/ideogram/remove-background'


def _fail(code):
    raise WorkflowError(code)


def _keys(value, required, optional=(), code='TASK_BRIDGE_INPUT_INVALID'):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        _fail(code)


def _sha(value, code='TASK_BRIDGE_INPUT_INVALID'):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        _fail(code)
    return value


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _path(value, code='TASK_BRIDGE_INPUT_INVALID'):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
        _fail(code)
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        _fail(code)
    if path.resolve() != path.absolute():
        _fail(code)
    return path.absolute()


def _bytes(path, maximum=5 * 1024 * 1024, code='TASK_CONTEXT_INVALID'):
    path = _path(str(path), code)
    if not path.is_file() or path.stat().st_size > maximum:
        _fail(code)
    return path.read_bytes()


def _json_bytes(data, code='TASK_CONTEXT_INVALID', decimals=False):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail(code)
            result[key] = value
        return result
    def nonfinite(_):
        _fail(code)
    try:
        kwargs = dict(object_pairs_hook=pairs, parse_constant=nonfinite)
        if decimals:
            kwargs['parse_float'] = Decimal
        return json.loads(data.decode('utf-8'), **kwargs)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        _fail(code)


def _json(path, code='TASK_CONTEXT_INVALID', decimals=False):
    return _json_bytes(_bytes(path, code=code), code, decimals)


def _png_size(path, maximum):
    data = _bytes(path, maximum, 'TASK_SOURCE_PROVENANCE_MISMATCH')
    if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    width, height = struct.unpack('>II', data[16:24])
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    return width, height


def _message(value, before):
    fields = ('schema', 'job_dir', 'config_sha256', 'config_file_sha256', 'source_sha256')
    _keys(value, fields + (('quote_sha256', 'amount_usd') if before else ()))
    if value['schema'] != ('sprite_before_submit_v1' if before else 'sprite_transport_context_v1'):
        _fail('TASK_BRIDGE_INPUT_INVALID')
    for field in ('config_sha256', 'config_file_sha256', 'source_sha256'):
        _sha(value[field])
    _path(value['job_dir'])
    if before:
        _sha(value['quote_sha256'])
        if not isinstance(value['amount_usd'], str):
            _fail('TASK_BRIDGE_INPUT_INVALID')
        try:
            ledger._money(value['amount_usd'], True)
        except WorkflowError:
            _fail('TASK_BRIDGE_INPUT_INVALID')


def _task_ref(value):
    _keys(value, ('schema', 'task_id', 'task_manifest', 'authorization_sha256', 'target_id'), code='TASK_SCOPE_MISMATCH')
    if value['schema'] != 'sprite_task_ref_v1':
        _fail('TASK_SCOPE_MISMATCH')
    _sha(value['authorization_sha256'], 'TASK_SCOPE_MISMATCH')
    _path(value['task_manifest'], 'TASK_SCOPE_MISMATCH')
    if any(not isinstance(value[key], str) or not value[key].strip() for key in ('task_id', 'target_id')):
        _fail('TASK_SCOPE_MISMATCH')


def _transform(value):
    _keys(value, ('scale', 'size', 'offset', 'canvas', 'background_rgb'), code='TASK_SOURCE_PROVENANCE_MISMATCH')
    scale = value['scale']
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    for name, length in (('size',2), ('offset',2), ('canvas',2), ('background_rgb',3)):
        if not isinstance(value[name], list) or len(value[name]) != length or any(type(item) is not int for item in value[name]):
            _fail('TASK_SOURCE_PROVENANCE_MISMATCH')


def _provenance(context):
    job, root, attempt = context['job_dir'], context['root'], context['attempt']
    source, original = context['source_path'], context['scope']['source']['sha256']
    binding = context['operation']['execution_binding']
    payload = _bytes(job / 'source-provenance.json', code='TASK_SOURCE_PROVENANCE_MISMATCH')
    if _hash(payload) != binding['provenance_sha256']:
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    record = _json_bytes(payload, 'TASK_SOURCE_PROVENANCE_MISMATCH')
    _keys(record, ('schema', 'kind', 'original_source_sha256', 'input_sha256', 'evidence'), code='TASK_SOURCE_PROVENANCE_MISMATCH')
    if (record['schema'] != 'sprite_source_provenance_v1' or record['original_source_sha256'] != original
            or record['input_sha256'] != binding['source_sha256']):
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    kind, evidence = record['kind'], record['evidence']
    if context['role'] == 'primary':
        if context['route'] == 'sheet':
            if kind != 'original_source' or evidence != {} or source != root / 'source.png' or record['input_sha256'] != original:
                _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        else:
            if kind != 'prepared_source_v1' or source != context['attempt_dir'] / 'prepared-source.png':
                _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
            _keys(evidence, ('transform',), code='TASK_SOURCE_PROVENANCE_MISMATCH')
            prepared = attempt.get('prepared', {})
            _keys(prepared, ('path', 'sha256', 'original_sha256', 'transform'), code='TASK_SOURCE_PROVENANCE_MISMATCH')
            _transform(prepared['transform'])
            _transform(evidence['transform'])
            width, height = _png_size(root / 'source.png', 20 * 1024 * 1024)
            scale = min(1024 / width, 1024 / height)
            size = [max(1, round(width * scale)), max(1, round(height * scale))]
            transform = dict(scale=scale, size=size, offset=[(1024-size[0])//2, (1024-size[1])//2],
                             canvas=[1024,1024], background_rgb=[110,110,110])
            if (prepared != dict(path=str(source), sha256=record['input_sha256'], original_sha256=original, transform=transform)
                    or evidence['transform'] != transform or _png_size(source, 20 * 1024 * 1024) != (1024,1024)):
                _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
        return
    deferred = context['operation'].get('deferred_config')
    if not deferred:
        _fail('TASK_PLAN_INVALID')
    plan_path = context['attempt_dir'] / 'cleanup-plan' / 'plan.json'
    plan_bytes = _bytes(plan_path, code='TASK_PLAN_INVALID')
    if (_json_bytes(plan_bytes, 'TASK_PLAN_INVALID') != deferred
            or _hash(plan_bytes) != binding['deferred_plan_sha256']):
        _fail('TASK_PLAN_INVALID')
    if kind != deferred['source_from']:
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    fields = ('parent_operation_id', 'parent_request_id', 'parent_raw_sha256')
    _keys(evidence, fields + (('selection_sha256', 'grid_sha256') if kind == 'video_sample_grid_v1' else ()),
          code='TASK_SOURCE_PROVENANCE_MISMATCH')
    parent_id = deferred['primary_operation_id']
    parent = context['ledger']['operations'][parent_id]
    if (evidence['parent_operation_id'] != parent_id or not parent.get('provider_request_id')
            or evidence['parent_request_id'] != parent['provider_request_id']
            or not any(item['status'] == 'completed' for item in parent['outcomes'])):
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    primary_dir = context['attempt_dir'] / 'primary'
    raw = primary_dir / ('raw.png' if kind == 'primary_raw_png' else 'raw.mp4')
    raw_sha = _hash(_bytes(raw, 50 * 1024 * 1024 if kind == 'primary_raw_png' else 100 * 1024 * 1024,
                           'TASK_SOURCE_PROVENANCE_MISMATCH'))
    parent_state = _json(primary_dir / 'job-state.json', 'TASK_SOURCE_PROVENANCE_MISMATCH')
    parent_job = attempt['jobs']['primary']
    parent_binding = parent.get('execution_binding', {})
    if (evidence['parent_raw_sha256'] != raw_sha or parent_state.get('request_id') != parent['provider_request_id']
            or parent_state.get('state') != 'DOWNLOADED' or parent_state.get('raw_sha256') != raw_sha
            or parent_state.get('raw_path') != str(raw) or parent_job.get('raw_sha256') != raw_sha
            or parent_job.get('raw_path') != str(raw) or parent_job.get('request_id') != parent['provider_request_id']
            or parent_state.get('config_sha256') != deferred['primary_config_sha256']
            or parent_binding.get('config_sha256') != deferred['primary_config_sha256']
            or _hash(_bytes(primary_dir / 'config.json')) != parent_binding.get('config_file_sha256')):
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    if kind == 'primary_raw_png':
        if source != raw or record['input_sha256'] != raw_sha:
            _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    else:
        sampling_dir = context['attempt_dir'] / 'sampling'
        selection_path, grid_path = sampling_dir / 'selection.json', sampling_dir / 'sample-grid.png'
        selection_bytes = _bytes(selection_path, code='TASK_SOURCE_PROVENANCE_MISMATCH')
        selection = _json_bytes(selection_bytes, 'TASK_SOURCE_PROVENANCE_MISMATCH')
        sampling = attempt.get('sampling', {})
        frame_paths = [sampling_dir / f'frame-{number:02d}.png' for number in range(1, context['scope']['request']['frames'] + 1)]
        hashes = [_hash(_bytes(path, 10 * 1024 * 1024, 'TASK_SOURCE_PROVENANCE_MISMATCH')) for path in frame_paths]
        if (source != grid_path or evidence['selection_sha256'] != _hash(selection_bytes)
                or evidence['grid_sha256'] != record['input_sha256'] or selection.get('raw_sha256') != raw_sha
                or selection.get('grid_sha256') != record['input_sha256'] or selection.get('frame_sha256') != hashes
                or sampling.get('selection_path') != str(selection_path) or sampling.get('grid_path') != str(grid_path)
                or sampling.get('frame_paths') != [str(path) for path in frame_paths] or sampling.get('selection') != selection):
            _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    _png_size(source, deferred['source_limits']['max_bytes'])


def _context(message):
    job_dir = _path(message['job_dir'])
    role, attempt_dir = job_dir.name, job_dir.parent
    if role not in ('primary', 'cleanup') or not re.fullmatch(r'attempt-0[1-3]', attempt_dir.name):
        _fail('TASK_SCOPE_MISMATCH')
    root = attempt_dir.parent
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        _fail('TASK_SCOPE_MISMATCH')
    scope_bytes = _bytes(root / 'scope.json')
    scope = _json_bytes(scope_bytes)
    scope_hash = _hash(scope_bytes)
    manifest = _json(root / 'run-manifest.json')
    if (_bytes(root / 'scope.sha256', 128).decode().strip() != scope_hash or scope_sha(scope) != scope_hash
            or any(manifest.get(key) != scope.get(key) for key in ('run_id', 'request', 'source', 'authorization'))
            or manifest.get('authorization_scope_sha256') != scope_hash or manifest.get('schema_version') != 3
            or manifest.get('mode') != 'whole_sheet' or scope['run_id'] != root.name):
        _fail('TASK_SCOPE_MISMATCH')
    request = scope['request']
    ref = request.get('task_ref')
    if ref is None:
        _fail('TASK_CONTEXT_REQUIRED')
    _task_ref(ref)
    original = _bytes(root / 'source.png', 20 * 1024 * 1024, 'TASK_SOURCE_PROVENANCE_MISMATCH')
    if scope['source']['path'] != str(root / 'source.png') or _hash(original) != scope['source']['sha256']:
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    config_bytes = _bytes(job_dir / 'config.json')
    if _hash(config_bytes) != message['config_file_sha256']:
        _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    config = _json_bytes(config_bytes)
    _keys(config, ('schema_version', 'model', 'source', 'input', 'authorization', 'max_cost_usd'), code='TASK_SCOPE_MISMATCH')
    auth = config['authorization']
    _keys(auth, ('request_authorized', 'scope_sha256', 'task_context'), code='TASK_SCOPE_MISMATCH')
    _keys(config['source'], ('path', 'sha256'), code='TASK_SCOPE_MISMATCH')
    transport_context = auth['task_context']
    _keys(transport_context, ('task_ref', 'run_manifest', 'run_id', 'attempt_id', 'operation_id'), code='TASK_SCOPE_MISMATCH')
    number = int(attempt_dir.name[-2:])
    attempt_id = scope['run_id'] + f':attempt-{number:02d}'
    operation_id = attempt_id + ':' + role
    expected_context = dict(task_ref=ref, run_manifest=str(root / 'run-manifest.json'), run_id=scope['run_id'],
                            attempt_id=attempt_id, operation_id=operation_id)
    if (transport_context != expected_context or auth['request_authorized'] is not True or auth['scope_sha256'] != scope_hash
            or type(config['schema_version']) is not int or config['schema_version'] != 3
            or isinstance(config['max_cost_usd'], bool) or not isinstance(config['max_cost_usd'], (int, float))
            or not math.isfinite(config['max_cost_usd']) or config['max_cost_usd'] != scope['authorization']['max_cost_usd']):
        _fail('TASK_SCOPE_MISMATCH')
    state = ledger.inspect_task(ref['task_manifest'])
    if state['task_id'] != ref['task_id'] or state['authorization_sha256'] != ref['authorization_sha256']:
        _fail('TASK_SCOPE_MISMATCH')
    target = state['targets'].get(ref['target_id'])
    if (target and target['source_sha256'] == scope['source']['sha256'] and target['action'] == request['action']
            and target['orientation'] == request['orientation']
            and all(key in request and request[key] == value for key,value in target['requirements'].items())
            and state.get('history_status') == 'pending'):
        ledger.check_history_context(ref['task_manifest'], ref['target_id'])
    bound_run = state['runs'].get(scope['run_id'], {})
    if (not target or bound_run.get('scope_sha256') != scope_hash or bound_run.get('target_id') != ref['target_id']
            or bound_run.get('manifest_path') != str(root / 'run-manifest.json')
            or target['source_sha256'] != scope['source']['sha256'] or target['action'] != request['action']
            or target['orientation'] != request['orientation']):
        _fail('TASK_SCOPE_MISMATCH')
    if any(key not in request or request[key] != value for key, value in target['requirements'].items()):
        _fail('TASK_SCOPE_MISMATCH')
    attempts = manifest['attempts']
    if not isinstance(attempts, list) or not 1 <= number <= len(attempts):
        _fail('TASK_SCOPE_MISMATCH')
    attempt = attempts[number-1]
    job = attempt['jobs'][role]
    if (attempt.get('index') != number or attempt.get('attempt_id') != attempt_id or job.get('path') != str(job_dir)
            or job.get('operation_id') != operation_id or job.get('config_sha256') != scope_sha(config)
            or job.get('transport_config_sha256') != message['config_sha256']
            or job.get('config_file_sha256') != message['config_file_sha256']):
        _fail('TASK_SCOPE_MISMATCH')
    operation = state['operations'].get(operation_id)
    if not operation or operation['run_id'] != scope['run_id'] or operation['target_id'] != ref['target_id']:
        _fail('TASK_SCOPE_MISMATCH')
    ledger_attempt = state['attempts'][operation['attempt_key']]
    if ledger_attempt['run_id'] != scope['run_id'] or ledger_attempt['attempt_id'] != attempt_id:
        _fail('TASK_SCOPE_MISMATCH')
    primary_id, cleanup_id = attempt_id + ':primary', attempt_id + ':cleanup'
    cleanup_plan = state['operations'].get(cleanup_id, {})
    if (ledger_attempt.get('plan_operation_ids') != sorted((primary_id, cleanup_id))
            or 'deferred_config' not in cleanup_plan or cleanup_plan['attempt_key'] != operation['attempt_key']):
        _fail('TASK_PLAN_INVALID')
    if role == 'primary' and not operation.get('provider_request_id'):
        plan_bytes = _bytes(attempt_dir / 'cleanup-plan' / 'plan.json', code='TASK_PLAN_INVALID')
        if (plan_bytes != encoded(cleanup_plan['deferred_config'])
                or cleanup_plan['deferred_config']['primary_operation_id'] != primary_id):
            _fail('TASK_PLAN_INVALID')
    route = request['route']
    strategy = state['strategies'][operation['strategy_sha256']]['effective_descriptor']
    if (route not in _MODELS or strategy != dict(route=route, model=_MODELS[route], conditioning='single_source',
                                                inputs=[dict(role='source', sha256=target['source_sha256'])])
            or config['model'] != (_MODELS[route] if role == 'primary' else _CLEANUP)
            or operation['kind'] != ('provider_animation' if role == 'primary' else 'alpha_edge_cleanup')):
        _fail('TASK_SCOPE_MISMATCH')
    binding = operation.get('execution_binding')
    if binding is None:
        _fail('TASK_OPERATION_NOT_MATERIALIZED')
    if (binding['config_sha256'] != message['config_sha256'] or binding['config_file_sha256'] != message['config_file_sha256']
            or binding['source_sha256'] != message['source_sha256'] or config['source']['sha256'] != message['source_sha256']):
        _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    source_path = _path(config['source']['path'], 'TASK_SOURCE_PROVENANCE_MISMATCH')
    if _hash(_bytes(source_path, 20 * 1024 * 1024, 'TASK_SOURCE_PROVENANCE_MISMATCH')) != message['source_sha256']:
        _fail('TASK_SOURCE_PROVENANCE_MISMATCH')
    result = dict(job_dir=job_dir, root=root, attempt_dir=attempt_dir, role=role, route=route, config=config, scope=scope,
                  attempt=attempt, operation=operation, ledger=state, ref=ref, source_path=source_path)
    _provenance(result)
    if not operation['submission_started'] and not operation.get('provider_request_id'):
        ledger.check_history_context(ref['task_manifest'], ref['target_id'], strategy if role == 'primary' else None)
    return result


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        _fail('PRICE_RECEIPT_INVALID')
    try:
        return Decimal(ledger._money(value, True))
    except WorkflowError:
        _fail('PRICE_RECEIPT_INVALID')


def _receipt(context, message):
    directory = _path(str(context['job_dir'] / 'pricing-receipts'), 'PRICE_RECEIPT_INVALID')
    if not directory.is_dir():
        _fail('PRICE_RECEIPT_INVALID')
    names = list(directory.iterdir())
    if len(names) > 4096:
        _fail('PRICE_RECEIPT_INVALID')
    matches = [(path, _bytes(path, code='PRICE_RECEIPT_INVALID')) for path in names if path.name.endswith('.receipt.json')]
    matches = [(path, payload) for path, payload in matches if _hash(payload) == message['quote_sha256']]
    if len(matches) != 1:
        _fail('PRICE_RECEIPT_INVALID')
    path, payload = matches[0]
    receipt = _json_bytes(payload, 'PRICE_RECEIPT_INVALID', decimals=True)
    fields = ('schema_version', 'receipt_id', 'config_sha256', 'model', 'retrieved_at', 'expires_at', 'currency', 'api_unit',
              'base_unit_price_usd', 'resolution_multiplier', 'effective_unit_price_usd', 'estimated_cost_usd', 'planning_basis',
              'input_duration_seconds', 'reserved_units', 'driving_video_sha256', 'sources')
    _keys(receipt, fields, ('estimated_cost_usd_decimal',), 'PRICE_RECEIPT_INVALID')
    model = context['config']['model']
    video, nano = model == _MODELS['video'], model == _MODELS['sheet']
    units, basis = (6, 'configured_duration+1_reserved_output_second') if video else (1, 'configured_output_count')
    if (type(receipt['schema_version']) is not int or receipt['schema_version'] != 3
            or receipt['config_sha256'] != message['config_sha256'] or receipt['model'] != model
            or receipt['currency'] != 'USD' or receipt['api_unit'] not in (('seconds',) if video else ('image', 'images'))
            or type(receipt['reserved_units']) is not int or receipt['reserved_units'] != units
            or receipt['planning_basis'] != basis or receipt['input_duration_seconds'] != (5 if video else None)
            or receipt['driving_video_sha256'] is not None or receipt['resolution_multiplier'] != (Decimal('1.5') if nano else None)
            or not isinstance(receipt['receipt_id'], str) or not _UUID.fullmatch(receipt['receipt_id'])):
        _fail('PRICE_RECEIPT_INVALID')
    try:
        retrieved = datetime.fromisoformat(receipt['retrieved_at'].replace('Z', '+00:00'))
        expires = datetime.fromisoformat(receipt['expires_at'].replace('Z', '+00:00'))
        if retrieved.utcoffset() != timedelta(0) or expires.utcoffset() != timedelta(0) or expires-retrieved != timedelta(minutes=15):
            _fail('PRICE_RECEIPT_INVALID')
        epoch = retrieved - datetime(1970,1,1,tzinfo=timezone.utc)
        millis = (epoch.days * 86400 + epoch.seconds) * 1000 + epoch.microseconds // 1000
        if path.name != f'{millis:016d}-{receipt["receipt_id"]}.receipt.json':
            _fail('PRICE_RECEIPT_INVALID')
    except (ValueError, TypeError, AttributeError):
        _fail('PRICE_RECEIPT_INVALID')
    sources = receipt['sources']
    if not isinstance(sources, list) or len(sources) != (2 if nano or video else 1):
        _fail('PRICE_RECEIPT_INVALID')
    for number, source in enumerate(sources):
        _keys(source, ('source_url', 'retrieved_at', 'body_sha256', 'archive_file', 'selected'), code='PRICE_RECEIPT_INVALID')
        archive_name = receipt['receipt_id'] + ('.api.body' if number == 0 else '.public.body')
        if source['archive_file'] != archive_name or source['retrieved_at'] != receipt['retrieved_at']:
            _fail('PRICE_RECEIPT_INVALID')
        archived = _bytes(directory / archive_name, code='PRICE_RECEIPT_INVALID')
        if _hash(archived) != source['body_sha256']:
            _fail('PRICE_RECEIPT_INVALID')
        if number == 0:
            expected_url = 'https://api.fal.ai/v1/models/pricing?' + urlencode(dict(endpoint_id=model))
            selected = source['selected']
            _keys(selected, ('endpoint_id', 'currency', 'unit', 'unit_price_usd'), code='PRICE_RECEIPT_INVALID')
            if (source['source_url'] != expected_url or selected['endpoint_id'] != model
                    or selected['currency'] != 'USD' or selected['unit'] != receipt['api_unit']):
                _fail('PRICE_RECEIPT_INVALID')
            api = _json_bytes(archived, 'PRICE_RECEIPT_INVALID', decimals=True)
            prices = api.get('prices')
            if not isinstance(prices, list):
                _fail('PRICE_RECEIPT_INVALID')
            chosen = [item for item in prices if isinstance(item, dict) and item.get('endpoint_id') == model]
            if (len(chosen) != 1 or chosen[0].get('currency') != 'USD' or chosen[0].get('unit') != receipt['api_unit']
                    or _decimal(chosen[0].get('unit_price')) != _decimal(receipt['base_unit_price_usd'])
                    or _decimal(selected['unit_price_usd']) != _decimal(receipt['base_unit_price_usd'])):
                _fail('PRICE_RECEIPT_INVALID')
        else:
            selected = source['selected']
            if source['source_url'] != 'https://fal.ai/models/' + model:
                _fail('PRICE_RECEIPT_INVALID')
            if nano:
                _keys(selected, ('base_unit_price_usd', 'resolution', 'resolution_multiplier'), code='PRICE_RECEIPT_INVALID')
                if selected['resolution'] != '2K' or selected['resolution_multiplier'] != Decimal('1.5'):
                    _fail('PRICE_RECEIPT_INVALID')
            else:
                _keys(selected, ('base_unit_price_usd', 'resolution', 'effective_unit_price_usd'), code='PRICE_RECEIPT_INVALID')
                if selected['resolution'] != '768P' or _decimal(selected['effective_unit_price_usd']) != _decimal(receipt['effective_unit_price_usd']):
                    _fail('PRICE_RECEIPT_INVALID')
            if _decimal(selected['base_unit_price_usd']) != _decimal(receipt['base_unit_price_usd']):
                _fail('PRICE_RECEIPT_INVALID')
    with localcontext() as decimal_context:
        decimal_context.prec = 512
        effective = _decimal(receipt['effective_unit_price_usd'])
        if not video and effective != _decimal(receipt['base_unit_price_usd']) * (Decimal('1.5') if nano else Decimal(1)):
            _fail('PRICE_RECEIPT_INVALID')
        estimate = _decimal(receipt['estimated_cost_usd'])
        if estimate != effective * units or estimate != _decimal(message['amount_usd']):
            _fail('PRICE_RECEIPT_INVALID')
        if 'estimated_cost_usd_decimal' in receipt and (not isinstance(receipt['estimated_cost_usd_decimal'], str)
                or _decimal(receipt['estimated_cost_usd_decimal']) != estimate):
            _fail('PRICE_RECEIPT_INVALID')
        if estimate > Decimal(str(context['config']['max_cost_usd'])):
            _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    age = datetime.now(timezone.utc) - retrieved
    if age < -timedelta(seconds=30) or age > timedelta(minutes=15):
        _fail('PRICE_STALE')


def _output(context, before=False, marked=None):
    operation = context['operation']
    result = dict(state='SUBMISSION_AUTHORIZED' if before else 'CONTEXT_VERIFIED', task_id=context['ref']['task_id'],
        target_id=context['ref']['target_id'], run_id=operation['run_id'],
        attempt_id=context['ledger']['attempts'][operation['attempt_key']]['attempt_id'], operation_id=operation['operation_id'],
        config_sha256=operation['execution_binding']['config_sha256'],
        config_file_sha256=operation['execution_binding']['config_file_sha256'], binding_sha256=operation['binding_sha256'],
        provider_request_id=operation.get('provider_request_id'), submission_started=operation['submission_started'], status=operation['status'],
        outcome_status=operation['outcomes'][-1]['status'] if operation['outcomes'] else None)
    if before:
        result['submit_allowed'] = marked['submit_allowed']
    return result


def _run(message, before):
    try:
        if isinstance(message,dict) and message.get('schema') in ('sprite_regional_transport_context_v1','sprite_regional_before_submit_v1'):
            from regional_video_workflow import regional_bridge_run
            return regional_bridge_run(message,before)
        if isinstance(message,dict) and message.get('schema') in ('sprite_transport_context_v2','sprite_before_submit_v2'):
            # The fixed endpoint consumer owns both closed config4/config5
            # branches. Recovery validates RAW-only Seedream context without
            # preparing PNG/proofs or granting a second submission marker.
            from endpoint_cycle_contract import bridge_run
            return bridge_run(message,before)
        _message(message, before)
        context = _context(message)
        if not before:
            return _output(context)
        if not context['operation'].get('provider_request_id'):
            _receipt(context, message)
        expected = dict(config_sha256=message['config_sha256'], binding_sha256=context['operation']['binding_sha256'],
                        quote_sha256=message['quote_sha256'], amount_usd=message['amount_usd'])
        marked = ledger.mark_submission(context['ref']['task_manifest'], context['operation']['operation_id'], expected=expected)
        # The marker transaction may have recovered another executor's ID. Read
        # one coherent post-transition snapshot instead of mixing that new ID
        # with the earlier held/no-outcome context.
        state = ledger.inspect_task(context['ref']['task_manifest'])
        operation = state['operations'][context['operation']['operation_id']]
        if (operation['attempt_key'] != context['operation']['attempt_key']
                or operation.get('binding_sha256') != context['operation']['binding_sha256']
                or (marked['provider_request_id'] is not None and operation.get('provider_request_id') != marked['provider_request_id'])):
            _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        if operation.get('provider_request_id'):
            marked = dict(submit_allowed=False, provider_request_id=operation['provider_request_id'])
        elif not marked['submit_allowed'] or operation['status'] != 'submission_pending' or not operation['submission_started']:
            _fail('TASK_SUBMISSION_UNRESOLVED')
        context.update(ledger=state, operation=operation)
        return _output(context, True, marked)
    except WorkflowError:
        raise
    except (OSError, KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError):
        _fail('TASK_CONTEXT_INVALID')


def check_context(message):
    return _run(message, False)


def before_submit(message):
    return _run(message, True)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    message = None
    try:
        if args not in (['check-context'], ['before-submit']):
            _fail('TASK_BRIDGE_INPUT_INVALID')
        payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(payload) > MAX_INPUT_BYTES:
            _fail('TASK_BRIDGE_INPUT_INVALID')
        message = _json_bytes(payload, 'TASK_BRIDGE_INPUT_INVALID')
        result = before_submit(message) if args[0] == 'before-submit' else check_context(message)
        exit_code = 0
    except (WorkflowError, OSError, ValueError, TypeError) as error:
        result = dict(state='BLOCKED', block_reason=getattr(error, 'code', 'TASK_BRIDGE_INPUT_INVALID'), submit_allowed=False)
        if isinstance(message,dict) and message.get('schema') in ('sprite_transport_context_v2','sprite_before_submit_v2','sprite_regional_transport_context_v1','sprite_regional_before_submit_v1'):
            result.pop('submit_allowed')
        exit_code = 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
