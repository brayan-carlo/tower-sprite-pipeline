"""Offline, append-only authorization/reservation journal for one human task.

This unit controls local reservations, never invoices or artistic delivery. The
caller remains responsible for human authorization, real adapter descriptors,
quote validity and recording the marker BEFORE invoking a provider transport.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from sheet_workflow_state import WorkflowError, atomic_json, encoded, new_or_identical, run_lock

OPERATION_KINDS = ('provider_animation', 'alpha_edge_cleanup', 'body_x_alignment', 'native_motion_construction')
BUDGET_BASES = ('fal_operational_reservations', 'total_provider_cap')
_TERMINAL = ('completed', 'failed', 'cancelled', 'rejected')
_OUTCOMES = ('accepted', 'pending', 'uncertain') + _TERMINAL
_HEX = re.compile(r'^[0-9a-f]{64}$')
_EVENT = re.compile(r'^[0-9]{8}\.json$')
_DESCRIPTOR_MODELS_V1 = dict(sheet=('fal-ai/nano-banana-2/edit',), video=('minimax/h3-max/image-to-video',),
                         alpha_edge_cleanup=('alpha_edge_cleanup',), body_x_alignment=('body_x_preserve_y_v1',),
                         native_motion_construction=('quadruped_two_stride', 'articulated_biped_walk', 'articulated_biped_planted_attack'))


def _fail(code='TASK_SCHEMA_INVALID'):
    raise WorkflowError(code)


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        _fail()
    return value.strip()


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        _fail()


def _sha(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        _fail()
    return value


def _digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _money(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)) or len(str(value)) > 256:
        _fail()
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail()
    if not number.is_finite() or number < 0 or (positive and number == 0) or abs(number.as_tuple().exponent) > 100:
        _fail()
    result = format(number, 'f')
    if '.' in result:
        result = result.rstrip('0').rstrip('.')
    if len(result) > 256:
        _fail()
    return '0' if number == 0 else result


def _sum(values):
    # Bounded input size/exponents make this context exact even beyond the default
    # Decimal precision. No float conversion or per-operation rounding occurs.
    with localcontext() as context:
        context.prec = 512
        return sum((Decimal(value) for value in values), Decimal(0))


def _authorization(value):
    _keys(value, ('task_id', 'cap_usd', 'budget_basis', 'allowed_operations', 'evidence_ref'))
    basis = _text(value['budget_basis'])
    if basis not in BUDGET_BASES:
        _fail('TASK_POLICY_UNSUPPORTED')
    allowed = value['allowed_operations']
    if not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) for item in allowed):
        _fail()
    if len(allowed) != len(set(allowed)):
        _fail()
    if any(kind not in OPERATION_KINDS for kind in allowed):
        _fail('OPERATION_NOT_AUTHORIZED')
    return dict(task_id=_text(value['task_id']), cap_usd=_money(value['cap_usd']), budget_basis=basis,
                allowed_operations=sorted(allowed), evidence_ref=_text(value['evidence_ref']))


def _targets(values):
    if not isinstance(values, list) or not values or len(values) > 256:
        _fail()
    result = {}
    for target in values:
        _keys(target, ('target_id', 'source_sha256', 'action', 'orientation', 'requirements'))
        target_id = _text(target['target_id'])
        requirements = target['requirements']
        if not isinstance(requirements, dict):
            _fail()
        try:
            # JSON canonicalization rejects nonfinite values and non-JSON data.
            requirements = json.loads(encoded(requirements))
        except (TypeError, ValueError):
            _fail()
        normalized = dict(target_id=target_id, source_sha256=_sha(target['source_sha256']),
                          action=_text(target['action']).lower(), orientation=_text(target['orientation']).lower(),
                          requirements=requirements)
        if target_id in result:
            _fail('TASK_SOURCE_MISMATCH')
        result[target_id] = normalized
    return result


def _permit(state, kind):
    if kind not in OPERATION_KINDS or kind not in state['authorization']['allowed_operations']:
        _fail('OPERATION_NOT_AUTHORIZED')


def _run(state, run_id):
    _text(run_id)
    if run_id not in state['runs']:
        _fail('TASK_SCOPE_MISMATCH')
    return state['runs'][run_id]


def _unresolved(state, target_id):
    return [key for key, op in {**state['operations'], **state['imports']}.items()
            if op['target_id'] == target_id and op['status'] in ('submission_pending', 'uncertain')
            and not op.get('provider_request_id')]


def _guard_submission(state, target_id):
    if _unresolved(state, target_id):
        _fail('TASK_SUBMISSION_UNRESOLVED')


def _attempt_open(state, attempt_key):
    if (state['attempts'][attempt_key].get('review', {}).get('decision') == 'reject'
            or state['attempts'][attempt_key].get('endpoint_status', 'open') != 'open'
            or state['attempts'][attempt_key].get('regional_status', 'open') != 'open'):
        _fail('TASK_ATTEMPT_CLOSED')


def _descriptor_v1(value, target):
    if not isinstance(value, dict):
        _fail()
    # These ignored annotations have no effect on the mechanism fingerprint.
    _keys(value, ('route', 'model', 'conditioning', 'inputs'),
          ('resolved_route', 'prompt', 'seed', 'path', 'label', 'duration', 'uuid'))
    route = _text(value['route']).lower()
    if route == 'auto':
        route = _text(value.get('resolved_route')).lower()
    elif 'resolved_route' in value and value['resolved_route'] != route:
        _fail('TASK_POLICY_UNSUPPORTED')
    if route not in ('sheet', 'video', 'alpha_edge_cleanup', 'body_x_alignment', 'native_motion_construction'):
        _fail('TASK_POLICY_UNSUPPORTED')
    conditioning = value['conditioning']
    if conditioning != ('single_source' if route in ('sheet', 'video') else 'local'):
        _fail('TASK_POLICY_UNSUPPORTED')
    inputs = value['inputs']
    if not isinstance(inputs, list) or len(inputs) != 1:
        _fail('TASK_POLICY_UNSUPPORTED')
    _keys(inputs[0], ('role', 'sha256'))
    if inputs[0]['role'] != 'source':
        _fail('TASK_POLICY_UNSUPPORTED')
    source = _sha(inputs[0]['sha256'])
    if source != target['source_sha256']:
        _fail('TASK_SOURCE_MISMATCH')
    model = _text(value['model']).lower()
    if model not in _DESCRIPTOR_MODELS_V1[route]:
        _fail('TASK_POLICY_UNSUPPORTED')
    if route == 'native_motion_construction':
        actions = dict(quadruped_two_stride='run', articulated_biped_walk='walk', articulated_biped_planted_attack='attack')
        if target['action'] != actions[model] or target['orientation'] not in ('left', 'right'):
            _fail('TASK_POLICY_UNSUPPORTED')
    return dict(route=route, model=model, conditioning=conditioning,
                inputs=[dict(role='source', sha256=source)])


_ENDPOINT_ROLES = ('pose_edit', 'video_ab', 'video_ba', 'cleanup')
_ENDPOINT_STAGES = ('input_ready', 'pair_ready', 'ab_ready', 'cycle_ready')
_SEEDREAM_FAMILY = 'seedream45_wan_flf2v_cycle_v1'
_SEEDREAM_VARIANT = 'seedream45_a_identity_v1'


def _seedream_descriptor(descriptor):
    return descriptor.get('family') == _SEEDREAM_FAMILY


def _descriptor(value, target):
    if not isinstance(value, dict) or 'schema' not in value:
        return _descriptor_v1(value, target)
    _keys(value, ('schema', 'route', 'family', 'model', 'conditioning', 'inputs'))
    if value.get('schema') == 'sprite_regional_strategy_v1':
        expected = dict(schema='sprite_regional_strategy_v1', route='regional_video_inpaint_v1',
            family='spatial_video_mask_reference_v1', model='fal-ai/wan-vace-14b/inpainting',
            conditioning='video_mask_reference', inputs=[dict(role='source', sha256=target['source_sha256'])])
        if (value != expected or type(target['requirements'].get('frames')) is not int
                or target['requirements']['frames'] != 8):
            _fail('TASK_POLICY_UNSUPPORTED')
        return copy.deepcopy(expected)
    expected = dict(schema='sprite_strategy_descriptor_v2', route='endpoint_cycle_v1',
                    family='nano2_wan_flf2v_cycle_v1', model='fal-ai/wan-flf2v',
                    conditioning='pose_edit_then_opposite_endpoints',
                    inputs=[dict(role='source', sha256=target['source_sha256'])])
    if value.get('family') == _SEEDREAM_FAMILY:
        expected.update(family=_SEEDREAM_FAMILY, conditioning='valid_pose_edit_then_opposite_endpoints')
    if (value != expected or target['action'] != 'run' or target['orientation'] not in ('left', 'right')
            or type(target['requirements'].get('frames')) is not int or target['requirements']['frames'] != 8):
        _fail('TASK_POLICY_UNSUPPORTED')
    return copy.deepcopy(expected)


def _endpoint_attempt(state, attempt):
    return state['strategies'][attempt['strategy_sha256']]['effective_descriptor']['route'] == 'endpoint_cycle_v1'


def _endpoint_plan(value, attempt, target, *, seedream=False):
    try:
        _keys(value, ('schema', 'attempt_id', 'source_sha256', 'pose_config_sha256', 'pose_inputs',
                      'endpoint_a_sha256', 'preparation_policy', 'video_policy', 'sampling_policy',
                      'cleanup_policy', 'output_policy') +
              (('variant', 'pose_output_policy', 'pose_decoder') if seedream else ()))
        _sha(value['pose_config_sha256']); _sha(value['endpoint_a_sha256'])
        inputs = value['pose_inputs']
        if not isinstance(inputs, list) or len(inputs) != 2:
            _fail()
        for entry, role in zip(inputs, ('edit', 'identity')):
            _keys(entry, ('role', 'sha256'))
            if entry['role'] != role:
                _fail()
            _sha(entry['sha256'])
        if seedream:
            _keys(value['pose_decoder'], ('pillow_version', 'jpeg_codec_version'))
            for version in value['pose_decoder'].values():
                if _text(version) != version:
                    _fail()
            if (value['variant'] != _SEEDREAM_VARIANT
                    or value['pose_output_policy'] != 'seedream_rgb8_png_jpeg_to_png_v1'
                    or inputs[0]['sha256'] != value['endpoint_a_sha256']):
                _fail()
        if (value['schema'] != ('sprite_endpoint_plan_v2' if seedream else 'sprite_endpoint_plan_v1') or value['attempt_id'] != attempt['attempt_id']
                or value['source_sha256'] != target['source_sha256']
                or value['preparation_policy'] != 'pose_canvas_2048_v1'
                or value['sampling_policy'] != 'endpoint_full_range_81_four_phases_v1'
                or encoded(value['video_policy']) != encoded(dict(frames=81, fps=24, resolution='720p', aspect_ratio='1:1',
                    steps=30, acceleration='none', prompt_expansion=False))
                or encoded(value['cleanup_policy']) != encoded(dict(model='fal-ai/ideogram/remove-background', sync_mode=False,
                    max_bytes=10485760, max_width=4096, max_height=4096))
                or encoded(value['output_policy']) != encoded(dict(frames=8, frame_ms=100, loop=True, canvas=[2048,2048],
                    geometry_policy='common_nearest_2048_v1', alignment_policy='body_x_preserve_y_v1'))):
            _fail()
        return copy.deepcopy(value)
    except (WorkflowError, TypeError, ValueError):
        _fail('TASK_PLAN_INVALID')


def _endpoint_operations(state, attempt):
    return [state['operations'][name] for name in attempt.get('plan_operation_ids', [])]


def _endpoint_token(state, attempt):
    # Projection excludes inspect-only derived data and journal metadata.
    return _digest(dict(endpoint_plan=attempt.get('endpoint_plan'),
        operations=[{key: value for key, value in op.items()} for op in _endpoint_operations(state, attempt)],
        gates=attempt.get('endpoint_gates', {}), gate_history=attempt.get('endpoint_gate_history', []),
        status=attempt.get('endpoint_status', 'open'), review=attempt.get('review')))


def _endpoint_complete(operation):
    return bool(operation.get('provider_request_id')) and any(o['status'] == 'completed' for o in operation['outcomes'])


def _endpoint_terminal(operation):
    return ((operation['status'] == 'released' and not operation['submission_started'])
            or (bool(operation.get('provider_request_id')) and any(o['status'] in _TERMINAL for o in operation['outcomes'])))


def _seedream_guard(state, target_id, current=None, *, marker=False):
    """Share WAN history and Seedream quota; exclude only this exact attempt."""
    if current is not None:
        key = _digest(dict(run_id=current['run_id'], attempt_id=current['attempt_id']))
        if (state['attempts'].get(key) != current
                or state['runs'][current['run_id']]['target_id'] != target_id
                or current.get('scope_sha256') != state['runs'][current['run_id']]['scope_sha256']
                or not _seedream_descriptor(state['strategies'][current['strategy_sha256']]['effective_descriptor'])
                or state['strategies'][current['strategy_sha256']]['max_attempts'] != 1):
            _fail('TASK_STRATEGY_COLLISION')
        if marker and (not current.get('endpoint_plan') or len(_endpoint_operations(state, current)) != 4
                or current.get('endpoint_plan_sha256') != _digest(current['endpoint_plan'])):
            _fail('TASK_PLAN_INVALID')
    for other in state['attempts'].values():
        if state['runs'][other['run_id']]['target_id'] != target_id or other is current:
            continue
        descriptor = state['strategies'][other['strategy_sha256']]['effective_descriptor']
        if _seedream_descriptor(descriptor):
            _fail('TASK_STRATEGY_EXHAUSTED')
        if descriptor['route'] != 'endpoint_cycle_v1':
            continue
        operations = _endpoint_operations(state, other)
        if any((op['submission_started'] or op.get('provider_request_id')
                or op['status'] in ('submission_pending', 'uncertain'))
               and op['operation_id'].rsplit(':', 1)[1] in ('video_ab', 'video_ba') for op in operations):
            _fail('TASK_HISTORY_MECHANISM_CLOSED')
        if (other.get('endpoint_status') not in ('failed', 'rejected', 'released')
                or len(operations) != 4 or not all(_endpoint_terminal(op) for op in operations)):
            _fail('TASK_SUBMISSION_UNRESOLVED')
    if 'historical_import' not in state and any(op['target_id'] == target_id and op['kind'] == 'provider_animation'
                                               for op in state['imports'].values()):
        _fail('TASK_HISTORY_UNRESOLVED')


def _endpoint_live_gate(attempt, stage):
    gate = attempt.get('endpoint_gates', {}).get(stage)
    if not gate or gate['decision'] != 'pass':
        _fail('TASK_ENDPOINT_GATE_REQUIRED')
    if stage != 'input_ready':
        _endpoint_live_gate(attempt, 'input_ready')
    if stage in ('ab_ready', 'cycle_ready'):
        pair = _endpoint_live_gate(attempt, 'pair_ready')
        if gate['subject']['pair_gate_sha256'] != _digest(pair):
            _fail('TASK_ENDPOINT_GATE_REQUIRED')
    if stage == 'cycle_ready':
        ab = _endpoint_live_gate(attempt, 'ab_ready')
        if gate['subject']['ab_gate_sha256'] != _digest(ab):
            _fail('TASK_ENDPOINT_GATE_REQUIRED')
    return gate


def _endpoint_inputs(state, attempt, role):
    plan = attempt['endpoint_plan']
    if role == 'pose_edit':
        return plan['pose_inputs']
    if role in ('video_ab', 'video_ba'):
        pair = _endpoint_live_gate(attempt, 'pair_ready')['subject']
        a, b = pair['endpoint_a_sha256'], pair['endpoint_b_sha256']
        return [dict(role='start', sha256=a if role == 'video_ab' else b),
                dict(role='end', sha256=b if role == 'video_ab' else a)]
    gate = _endpoint_live_gate(attempt, 'cycle_ready')
    return [dict(role='grid', sha256=gate['subject']['grid_sha256'])]


def _endpoint_values(values, attempt, plan):
    if not isinstance(values, list) or len(values) != 4:
        _fail('TASK_PLAN_INVALID')
    result = []
    for role, value in zip(_ENDPOINT_ROLES, values):
        try:
            _keys(value, ('operation_id', 'kind', 'quote_sha256', 'amount_usd',
                          'config_sha256' if role == 'pose_edit' else 'deferred_config'))
            if value['operation_id'] != attempt['attempt_id'] + ':' + role or value['kind'] != (
                    'alpha_edge_cleanup' if role == 'cleanup' else 'provider_animation'):
                _fail()
            commitment = dict(config_sha256=_sha(value['config_sha256'])) if role == 'pose_edit' else dict(
                deferred_config=dict(schema='sprite_endpoint_deferred_v1', endpoint_plan_sha256=_digest(plan), role=role))
            if role == 'pose_edit':
                if commitment['config_sha256'] != plan['pose_config_sha256']:
                    _fail()
            elif value['deferred_config'] != commitment['deferred_config']:
                _fail()
            result.append(dict(operation_id=value['operation_id'], kind=value['kind'], **commitment,
                               quote_sha256=_sha(value['quote_sha256']), amount_usd=_money(value['amount_usd'], True)))
        except WorkflowError:
            _fail('TASK_PLAN_INVALID')
    return result


def _endpoint_gate(state, attempt, gate):
    _keys(gate, ('schema', 'stage', 'decision', 'subject', 'defects', 'evidence'))
    stage, decision = gate['stage'], gate['decision']
    seedream = _seedream_descriptor(state['strategies'][attempt['strategy_sha256']]['effective_descriptor'])
    if (gate['schema'] != ('sprite_endpoint_gate_v2' if seedream else 'sprite_endpoint_gate_v1') or stage not in _ENDPOINT_STAGES or decision not in ('pass', 'hold', 'reject')
            or not isinstance(gate['defects'], list) or (decision == 'reject') != bool(gate['defects'])):
        _fail()
    for defect in gate['defects']:
        _text(defect)
    _history_refs(gate['evidence'], {})
    old = attempt.get('endpoint_gates', {}).get(stage)
    if old == gate:
        return gate
    _attempt_open(state, _digest(dict(run_id=attempt['run_id'], attempt_id=attempt['attempt_id'])))
    if old and old['subject'] != gate['subject']:
        _fail('TASK_ENDPOINT_GATE_CONFLICT')
    subject = gate['subject']
    ops = dict(zip(_ENDPOINT_ROLES, _endpoint_operations(state, attempt)))
    if len(ops) != 4:
        _fail('TASK_PLAN_INVALID')
    plan = attempt['endpoint_plan']
    if stage == 'input_ready':
        expected = dict(endpoint_plan_sha256=_digest(plan))
    elif stage == 'pair_ready':
        _keys(subject, ('endpoint_plan_sha256', 'pose_request_id', 'pose_raw_sha256', 'endpoint_a_sha256', 'endpoint_b_sha256') +
              (('pose_output_proof_sha256',) if seedream else ()))
        _endpoint_live_gate(attempt, 'input_ready')
        if not _endpoint_complete(ops['pose_edit']):
            _fail('TASK_PARENT_OPERATION_INCOMPLETE')
        raw = _sha(subject['pose_raw_sha256'])
        b = _sha(subject['endpoint_b_sha256']) if seedream else raw
        if b == plan['endpoint_a_sha256']:
            _fail('TASK_ENDPOINT_GATE_CONFLICT')
        expected = dict(endpoint_plan_sha256=_digest(plan), pose_request_id=ops['pose_edit']['provider_request_id'],
                        pose_raw_sha256=raw, endpoint_a_sha256=plan['endpoint_a_sha256'], endpoint_b_sha256=b)
        if seedream:
            proof_sha = _sha(subject['pose_output_proof_sha256'])
            if ops['pose_edit']['outcomes'][-1]['evidence'] != 'pose-output-proof.json#sha256=' + proof_sha:
                _fail('TASK_ENDPOINT_GATE_CONFLICT')
            expected['pose_output_proof_sha256'] = proof_sha
    elif stage == 'ab_ready':
        _keys(subject, ('pair_gate_sha256', 'ab_request_id', 'ab_raw_sha256', 'probe_sha256'))
        pair = _endpoint_live_gate(attempt, 'pair_ready')
        if not _endpoint_complete(ops['video_ab']):
            _fail('TASK_PARENT_OPERATION_INCOMPLETE')
        expected = dict(pair_gate_sha256=_digest(pair), ab_request_id=ops['video_ab']['provider_request_id'],
                        ab_raw_sha256=_sha(subject['ab_raw_sha256']), probe_sha256=_sha(subject['probe_sha256']))
    else:
        _keys(subject, ('pair_gate_sha256', 'ab_gate_sha256', 'ab_request_id', 'ab_raw_sha256',
                        'ba_request_id', 'ba_raw_sha256', 'selection_sha256', 'grid_sha256'))
        pair, ab = _endpoint_live_gate(attempt, 'pair_ready'), _endpoint_live_gate(attempt, 'ab_ready')
        if not _endpoint_complete(ops['video_ab']) or not _endpoint_complete(ops['video_ba']):
            _fail('TASK_PARENT_OPERATION_INCOMPLETE')
        expected = dict(pair_gate_sha256=_digest(pair), ab_gate_sha256=_digest(ab),
                        ab_request_id=ops['video_ab']['provider_request_id'], ab_raw_sha256=ab['subject']['ab_raw_sha256'],
                        ba_request_id=ops['video_ba']['provider_request_id'], ba_raw_sha256=_sha(subject['ba_raw_sha256']),
                        selection_sha256=_sha(subject['selection_sha256']), grid_sha256=_sha(subject['grid_sha256']))
    if subject != expected:
        _fail('TASK_ENDPOINT_GATE_CONFLICT')
    attempt.setdefault('endpoint_gates', {})[stage] = copy.deepcopy(gate)
    attempt.setdefault('endpoint_gate_history', []).append(copy.deepcopy(gate))
    if decision == 'reject':
        attempt['endpoint_status'] = 'rejected'
    return gate


def _endpoint_apply(state, kind, payload, attempt):
    key = _digest(dict(run_id=attempt['run_id'], attempt_id=attempt['attempt_id']))
    run = _run(state, attempt['run_id'])
    descriptor = state['strategies'][attempt['strategy_sha256']]['effective_descriptor']
    seedream = _seedream_descriptor(descriptor)
    if kind in ('reserve_plan', 'reprice_plan'):
        _keys(payload, ('run_id', 'attempt_id', 'operations', 'endpoint_plan') + (('strategy_sha256',) if kind == 'reserve_plan' else ()))
        plan = _endpoint_plan(payload['endpoint_plan'], attempt, state['targets'][run['target_id']], seedream=seedream)
        if seedream and attempt.get('endpoint_plan_sha256') != _digest(plan):
            _fail('TASK_PLAN_COLLISION')
        values = _endpoint_values(payload['operations'], attempt, plan)
        if 'endpoint_plan' in attempt and attempt['endpoint_plan'] != plan:
            _fail('TASK_PLAN_COLLISION')
        if kind == 'reserve_plan':
            if payload['strategy_sha256'] != attempt['strategy_sha256']:
                _fail('TASK_STRATEGY_MISMATCH')
            if attempt.get('plan_operation_ids'):
                for value, operation in zip(values, _endpoint_operations(state, attempt)):
                    original = {name: operation['reservation'][name] for name in value}
                    if original != value:
                        _fail('TASK_OPERATION_COLLISION')
                return dict(operation_ids=attempt['plan_operation_ids'], newly_reserved_usd='0')
            _attempt_open(state, key)
            _history_guard(state, run['target_id'], state['strategies'][attempt['strategy_sha256']]['effective_descriptor'])
            if seedream:
                _seedream_guard(state, run['target_id'], attempt)
            _guard_submission(state, run['target_id'])
            strategy = state['strategies'][attempt['strategy_sha256']]
            previous = [state['attempts'][a] for a in strategy['attempt_keys'] if a != key]
            if previous and previous[-1].get('endpoint_plan', {}).get('pose_config_sha256') == plan['pose_config_sha256']:
                _fail('TASK_CORRECTION_REQUIRED')
            for value in values:
                _permit(state, value['kind'])
                if value['operation_id'] in state['operations'] or value['operation_id'] in state['imports']:
                    _fail('TASK_OPERATION_COLLISION')
            _cap(state, [value['amount_usd'] for value in values])
            for value in values:
                reservation = dict(**value, run_id=attempt['run_id'], target_id=run['target_id'], attempt_key=key,
                                   strategy_sha256=attempt['strategy_sha256'], provider_request_id=None)
                quote = dict(quote_sha256=value['quote_sha256'], amount_usd=value['amount_usd'])
                state['operations'][value['operation_id']] = dict(**reservation, reservation=reservation, status='held',
                    submission_started=False, outcomes=[], current_quote=quote, quote_history=[quote])
            attempt.update(endpoint_plan=plan, endpoint_plan_sha256=_digest(plan),
                           plan_operation_ids=[v['operation_id'] for v in values], endpoint_status='open',
                           endpoint_gates={}, endpoint_gate_history=[])
            return dict(operation_ids=attempt['plan_operation_ids'], newly_reserved_usd=_money(_sum(v['amount_usd'] for v in values)))
        if not attempt.get('endpoint_plan'):
            _fail('TASK_PLAN_INVALID')
        changed = {}
        for value, operation in zip(values, _endpoint_operations(state, attempt)):
            for name in ('config_sha256', 'deferred_config'):
                if value.get(name) != operation['reservation'].get(name):
                    _fail('TASK_PLAN_COLLISION')
            quote = dict(quote_sha256=value['quote_sha256'], amount_usd=value['amount_usd'])
            if quote != operation['current_quote']:
                if operation['submission_started'] or operation['status'] != 'held':
                    _fail('TASK_PLAN_SUBMITTED')
                if any(q['quote_sha256'] == quote['quote_sha256'] and q != quote for q in operation['quote_history']):
                    _fail('TASK_OPERATION_COLLISION')
                changed[value['operation_id']] = quote
        if not changed:
            return dict(newly_reserved_usd='0')
        _attempt_open(state, key); _guard_submission(state, run['target_id'])
        with localcontext() as context:
            context.prec = 512
            delta = _sum(max(Decimal(q['amount_usd']) - Decimal(state['operations'][name]['amount_usd']), Decimal(0)) for name, q in changed.items())
        _cap(state, [str(delta)])
        for name, quote in changed.items():
            op = state['operations'][name]
            op['amount_usd'] = _money(max(Decimal(op['amount_usd']), Decimal(quote['amount_usd'])))
            op['quote_sha256'], op['current_quote'] = quote['quote_sha256'], quote
            if quote not in op['quote_history']:
                op['quote_history'].append(quote)
        return dict(newly_reserved_usd=_money(delta))
    if kind == 'record_endpoint_gate':
        _keys(payload, ('run_id', 'attempt_id', 'gate'))
        return _endpoint_gate(state, attempt, payload['gate'])
    operation = _operation(state, payload['operation_id']) if 'operation_id' in payload else None
    if kind == 'bind_operation_config':
        _keys(payload, ('operation_id', 'binding'))
        binding = payload['binding']
        _keys(binding, ('schema', 'config_sha256', 'config_file_sha256', 'input_set_sha256', 'provenance_sha256', 'deferred_plan_sha256'))
        if binding['schema'] != 'sprite_execution_binding_v2':
            _fail()
        for name in ('config_sha256', 'config_file_sha256', 'input_set_sha256', 'provenance_sha256'):
            _sha(binding[name])
        if operation.get('execution_binding') is not None:
            if operation['execution_binding'] != binding:
                _fail('TASK_OPERATION_COLLISION')
            return dict(binding_sha256=operation['binding_sha256'])
        _attempt_open(state, key)
        if operation['submission_started'] or operation['status'] != 'held':
            _fail('TASK_PLAN_SUBMITTED')
        role = operation['operation_id'].rsplit(':', 1)[1]
        deferred = operation.get('deferred_config')
        if role in ('video_ab', 'video_ba'):
            sibling = state['operations'][attempt['attempt_id'] + (':video_ba' if role == 'video_ab' else ':video_ab')]
            if sibling.get('execution_binding', {}).get('config_sha256') == binding['config_sha256']:
                _fail('TASK_OPERATION_COLLISION')
        if (binding['input_set_sha256'] != _digest(_endpoint_inputs(state, attempt, role))
                or binding['deferred_plan_sha256'] != (_digest(deferred) if deferred else None)
                or (not deferred and binding['config_sha256'] != operation['config_sha256'])):
            _fail('TASK_OPERATION_COLLISION')
        operation['execution_binding'], operation['binding_sha256'] = copy.deepcopy(binding), _digest(binding)
        return dict(binding_sha256=operation['binding_sha256'])
    if kind == 'mark_submission':
        _keys(payload, ('operation_id', 'expected'))
        expected = payload['expected']
        _keys(expected, ('schema', 'config_sha256', 'binding_sha256', 'quote_sha256', 'amount_usd', 'experiment_state_sha256'))
        if expected['schema'] != 'sprite_endpoint_submission_expected_v1':
            _fail()
        token = _sha(expected['experiment_state_sha256'])
        if not operation.get('provider_request_id') and token != _endpoint_token(state, attempt):
            _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        _expected(operation, {name: expected[name] for name in ('config_sha256', 'binding_sha256', 'quote_sha256', 'amount_usd')})
        if operation.get('provider_request_id'):
            return dict(submit_allowed=False, provider_request_id=operation['provider_request_id'], experiment_state_sha256=_endpoint_token(state, attempt))
        _attempt_open(state, key)
        if operation['status'] != 'held' or operation['submission_started']:
            _fail('TASK_SUBMISSION_UNRESOLVED')
        _guard_submission(state, run['target_id'])
        _history_guard(state, run['target_id'], state['strategies'][attempt['strategy_sha256']]['effective_descriptor'])
        if seedream:
            _seedream_guard(state, run['target_id'], attempt, marker=True)
        role = operation['operation_id'].rsplit(':', 1)[1]
        index = _ENDPOINT_ROLES.index(role)
        _endpoint_live_gate(attempt, _ENDPOINT_STAGES[index])
        operations = _endpoint_operations(state, attempt)
        if (len(operations) != 4 or any(not _endpoint_complete(op) for op in operations[:index])
                or any(op['status'] != 'held' or op['submission_started'] for op in operations[index:])):
            _fail('TASK_PLAN_INVALID')
        for op in operations:
            _permit(state, op['kind'])
        operation['status'], operation['submission_started'] = 'submission_pending', True
        return dict(submit_allowed=True, provider_request_id=None, experiment_state_sha256=_endpoint_token(state, attempt))
    if kind == 'release_unsubmitted':
        _keys(payload, ('operation_id', 'evidence'))
        if operation['status'] == 'released' and operation.get('release_evidence') == payload['evidence']:
            return operation
        index = _ENDPOINT_ROLES.index(operation['operation_id'].rsplit(':', 1)[1])
        if not all(_endpoint_terminal(op) for op in _endpoint_operations(state, attempt)[:index]):
            _fail('TASK_OPERATION_NOT_RELEASABLE')
        if operation['submission_started'] or operation['status'] != 'held':
            _fail('TASK_OPERATION_NOT_RELEASABLE')
        operation['status'], operation['release_evidence'] = 'released', _text(payload['evidence'])
        if attempt['endpoint_status'] == 'open':
            attempt['endpoint_status'] = 'released'
        return operation
    if kind == 'record_review':
        if payload['decision'] == 'pass':
            _attempt_open(state, key)
            if not all(_endpoint_complete(op) for op in _endpoint_operations(state, attempt)):
                _fail('TASK_PARENT_OPERATION_INCOMPLETE')
            _endpoint_live_gate(attempt, 'cycle_ready')
        result = _apply_core(state, kind, payload)
        if payload['decision'] == 'reject':
            attempt['endpoint_status'] = 'rejected'
        return result
    result = _apply_core(state, kind, payload)
    if kind == 'record_outcome' and operation.get('provider_request_id'):
        if payload['outcome']['status'] in ('failed', 'cancelled', 'rejected') and attempt['endpoint_status'] != 'rejected':
            attempt['endpoint_status'] = 'failed'
    return result


_REGIONAL_ROUTE = 'regional_video_inpaint_v1'
_REGIONAL_ROLES = ('primary', 'cleanup')
_REGIONAL_RAW_CHECKS = ('identity', 'anatomy', 'scale', 'material', 'orientation',
                        'framewise_motion', 'mask_scope', 'exterior', 'crop_background', 'loop_selection')


def _regional_attempt(state, attempt):
    return state['strategies'][attempt['strategy_sha256']]['effective_descriptor']['route'] == _REGIONAL_ROUTE


def _regional_ref(value):
    # Only the closed logical Ref is checked here. Physical files are revalidated
    # by the consumer before calling the ledger; replay never opens provider data.
    references = {}
    _history_refs([value], references)
    return copy.deepcopy(value)


def _regional_plan(value, attempt, target):
    try:
        _keys(value, ('schema', 'attempt_id', 'source_sha256', 'provenance', 'media', 'preflight',
                      'primary_config_sha256', 'primary_inputs', 'profile', 'cleanup_policy', 'output_policy', 'historical_source'))
        if (value['schema'] != 'sprite_regional_plan_v1' or value['attempt_id'] != attempt['attempt_id']
                or value['source_sha256'] != target['source_sha256'] or value['profile'] != 'vace81_24_720_v1'):
            _fail()
        _keys(value['historical_source'], ('history_bundle_sha256', 'history_id'))
        _sha(value['historical_source']['history_bundle_sha256'])
        _history_id(value['historical_source']['history_id'])
        for name in ('provenance', 'media', 'preflight'):
            _regional_ref(value[name])
        _sha(value['primary_config_sha256'])
        inputs = value['primary_inputs']
        if not isinstance(inputs, list) or len(inputs) != 3:
            _fail()
        for role, item in zip(('video', 'mask', 'identity'), inputs):
            _keys(item, ('role', 'sha256'))
            if item['role'] != role:
                _fail()
            _sha(item['sha256'])
        if inputs[-1]['sha256'] != target['source_sha256']:
            _fail()
        if encoded(value['cleanup_policy']) != encoded(dict(model='fal-ai/ideogram/remove-background', sync_mode=False,
                                           max_bytes=10485760, max_width=4096, max_height=4096)):
            _fail()
        output = value['output_policy']
        _keys(output, ('frames', 'frame_ms', 'loop', 'grounded', 'canvas', 'selection', 'geometry', 'alignment_policy'))
        if (type(output['frames']) is not int or output['frames'] != 8 or type(output['frame_ms']) is not int
                or output['frame_ms'] != 100 or output['loop'] is not True or output['grounded'] is not False
                or output['alignment_policy'] != 'body_x_preserve_y_v1'):
            _fail()
        canvas, selection, geometry = output['canvas'], output['selection'], output['geometry']
        if (not isinstance(canvas, list) or len(canvas) != 2 or any(type(x) is not int or x <= 0 for x in canvas)
                or not isinstance(selection, list) or len(selection) != 8
                or any(type(x) is not int or not 0 <= x < 81 for x in selection)
                or selection != sorted(set(selection))):
            _fail()
        _keys(geometry, ('policy', 'resize_canvas', 'crop', 'output_canvas', 'scale_numerator', 'scale_denominator'))
        size, crop = max(canvas), geometry['crop']
        if (geometry['policy'] != 'inverse_parent_preparation_v1' or encoded(geometry['output_canvas']) != encoded(canvas)
                or encoded(geometry['resize_canvas']) != encoded([size, size]) or geometry['scale_numerator'] != size
                or type(geometry['scale_numerator']) is not int or type(geometry['scale_denominator']) is not int
                or geometry['scale_denominator'] <= 0 or not isinstance(crop, list) or len(crop) != 4
                or any(type(x) is not int or x < 0 for x in crop) or crop[2:] != canvas
                or crop[0] + crop[2] > size or crop[1] + crop[3] > size):
            _fail()
        if 'canvas' in target['requirements'] and target['requirements']['canvas'] != canvas:
            _fail()
        return copy.deepcopy(value)
    except WorkflowError:
        _fail('TASK_PLAN_INVALID')


def _regional_history_source(state, plan, target_id):
    if 'historical_import' not in state or state.get('history_status') != 'complete':
        _fail('TASK_HISTORY_REQUIRED')
    source = plan['historical_source']
    record = state['historical_attempts'].get(source['history_id'])
    if (source['history_bundle_sha256'] != state['historical_import']['bundle_sha256'] or record is None
            or record['target_id'] != target_id or record['source_sha256'] != plan['source_sha256']
            or record['kind'] != 'provider_generation' or record['mechanism']['origin'] != 'fal'
            or record['mechanism']['resolution'] == 'unresolved'):
        _fail('TASK_SOURCE_ATTEMPT_MISMATCH')


def _regional_operations(state, attempt):
    return [state['operations'][name] for name in attempt.get('plan_operation_ids', [])]


def regional_state_token(state, run_id, attempt_id):
    """Canonical logical snapshot; excludes inspection-only projections."""
    key = _digest(dict(run_id=run_id, attempt_id=attempt_id))
    attempt = state['attempts'].get(key)
    if attempt is None or not _regional_attempt(state, attempt):
        _fail('TASK_STRATEGY_MISMATCH')
    clean = {name: value for name, value in attempt.items()
             if name not in ('regional_state_sha256', 'effective_defects')}
    return _digest(dict(attempt=clean, run=state['runs'][run_id],
        target=state['targets'][state['runs'][run_id]['target_id']], authorization=state['authorization'],
        operations=_regional_operations(state, attempt), historical_import=state.get('historical_import'),
        history_status=state.get('history_status'), historical_attempts=state.get('historical_attempts', {})))


def _regional_values(values, attempt, plan):
    if not isinstance(values, list) or len(values) != 2:
        _fail('TASK_PLAN_INVALID')
    result = []
    for role, value in zip(_REGIONAL_ROLES, values):
        try:
            _keys(value, ('operation_id', 'kind', 'quote_sha256', 'amount_usd',
                          'config_sha256' if role == 'primary' else 'deferred_config'))
            if value['operation_id'] != attempt['attempt_id'] + ':' + role or value['kind'] != (
                    'provider_animation' if role == 'primary' else 'alpha_edge_cleanup'):
                _fail()
            commitment = dict(config_sha256=plan['primary_config_sha256']) if role == 'primary' else dict(
                deferred_config=dict(schema='sprite_regional_deferred_v1', regional_plan_sha256=_digest(plan), role='cleanup'))
            if any(value[name] != item for name, item in commitment.items()):
                _fail()
            result.append(dict(operation_id=value['operation_id'], kind=value['kind'], **commitment,
                               quote_sha256=_sha(value['quote_sha256']), amount_usd=_money(value['amount_usd'], True)))
        except WorkflowError:
            _fail('TASK_PLAN_INVALID')
    return result


def _regional_live_gate(attempt, stage):
    gate = attempt.get('regional_gates', {}).get(stage)
    if not gate or gate['decision'] != 'pass':
        _fail('TASK_GATE_REQUIRED')
    if stage == 'raw_ready':
        _regional_live_gate(attempt, 'input_ready')
    return gate


def _regional_gate(state, attempt, gate):
    _keys(gate, ('schema', 'stage', 'decision', 'subject', 'evidence', 'findings'))
    stage, decision = gate['stage'], gate['decision']
    if (gate['schema'] != 'sprite_regional_gate_v1' or stage not in ('input_ready', 'raw_ready')
            or decision not in ('pass', 'hold', 'reject')):
        _fail()
    _regional_ref(gate['evidence'])
    findings = gate['findings']
    if not isinstance(findings, list) or not findings:
        _fail()
    checks = []
    for finding in findings:
        _keys(finding, ('check', 'passed', 'notes'))
        checks.append(_text(finding['check']))
        if type(finding['passed']) is not bool or (decision == 'pass' and finding['passed'] is not True):
            _fail()
        _text(finding['notes'])
    if len(checks) != len(set(checks)) or (stage == 'raw_ready' and set(checks) != set(_REGIONAL_RAW_CHECKS)):
        _fail()
    if not attempt.get('regional_plan'):
        _fail('TASK_PLAN_INVALID')
    plan = attempt['regional_plan']
    subject = gate['subject']
    if stage == 'input_ready':
        expected = dict(regional_plan_sha256=_digest(plan), preflight_sha256=plan['preflight']['sha256'], media_sha256=plan['media']['sha256'])
        _keys(subject, expected)
    else:
        _keys(subject, ('regional_plan_sha256', 'request_id', 'raw_sha256', 'probe_sha256',
                        'selection_sha256', 'grid_sha256', 'result_transform_sha256'))
        _regional_live_gate(attempt, 'input_ready')
        primary = _regional_operations(state, attempt)[0]
        if not _endpoint_complete(primary):
            _fail('TASK_PARENT_OPERATION_INCOMPLETE')
        # These are physical artifact hashes; the consumer validates their RAW,
        # selection and transform content before recording this immutable gate.
        expected = dict(subject, regional_plan_sha256=_digest(plan), request_id=primary['provider_request_id'])
        for name in ('raw_sha256', 'probe_sha256', 'selection_sha256', 'grid_sha256', 'result_transform_sha256'):
            _sha(subject[name])
    if subject != expected:
        _fail('TASK_GATE_SUBJECT_MISMATCH')
    old = attempt.get('regional_gates', {}).get(stage)
    if old == gate:
        return gate
    if old and old['subject'] != subject:
        _fail('TASK_GATE_SUBJECT_MISMATCH')
    if old and old['decision'] == 'reject':
        _fail('TASK_ATTEMPT_CLOSED')
    if attempt.get('regional_status', 'open') != 'open' and decision != 'reject':
        _fail('TASK_ATTEMPT_CLOSED')
    attempt.setdefault('regional_gates', {})[stage] = copy.deepcopy(gate)
    attempt.setdefault('regional_gate_history', []).append(copy.deepcopy(gate))
    if decision == 'reject':
        attempt['regional_status'] = 'rejected'
    return gate


def _regional_apply(state, kind, payload, attempt):
    key = _digest(dict(run_id=attempt['run_id'], attempt_id=attempt['attempt_id']))
    run = _run(state, attempt['run_id'])
    descriptor = state['strategies'][attempt['strategy_sha256']]['effective_descriptor']
    if kind in ('reserve_plan', 'reprice_plan'):
        _keys(payload, ('run_id', 'attempt_id', 'operations', 'regional_plan') + (('strategy_sha256',) if kind == 'reserve_plan' else ()))
        plan = _regional_plan(payload['regional_plan'], attempt, state['targets'][run['target_id']])
        if attempt['regional_plan_sha256'] != _digest(plan):
            _fail('TASK_PLAN_COLLISION')
        _regional_history_source(state, plan, run['target_id'])
        values = _regional_values(payload['operations'], attempt, plan)
        _history_guard(state, run['target_id'], descriptor)
        if kind == 'reserve_plan':
            if payload['strategy_sha256'] != attempt['strategy_sha256']:
                _fail('TASK_STRATEGY_MISMATCH')
            if attempt.get('plan_operation_ids'):
                for value, operation in zip(values, _regional_operations(state, attempt)):
                    if {name: operation['reservation'][name] for name in value} != value:
                        _fail('TASK_OPERATION_COLLISION')
                return dict(operation_ids=attempt['plan_operation_ids'], newly_reserved_usd='0')
            _attempt_open(state, key); _guard_submission(state, run['target_id'])
            for value in values:
                _permit(state, value['kind'])
                if value['operation_id'] in state['operations'] or value['operation_id'] in state['imports']:
                    _fail('TASK_OPERATION_COLLISION')
            _cap(state, [value['amount_usd'] for value in values])
            for value in values:
                reservation = dict(**value, run_id=attempt['run_id'], target_id=run['target_id'], attempt_key=key,
                                   strategy_sha256=attempt['strategy_sha256'], provider_request_id=None)
                quote = dict(quote_sha256=value['quote_sha256'], amount_usd=value['amount_usd'])
                state['operations'][value['operation_id']] = dict(**reservation, reservation=reservation,
                    status='held', submission_started=False, outcomes=[], current_quote=quote, quote_history=[quote])
            attempt.update(regional_plan=plan, plan_operation_ids=[v['operation_id'] for v in values],
                           regional_status='open', regional_gates={}, regional_gate_history=[])
            return dict(operation_ids=attempt['plan_operation_ids'], newly_reserved_usd=_money(_sum(v['amount_usd'] for v in values)))
        if attempt.get('regional_plan') != plan:
            _fail('TASK_PLAN_COLLISION')
        changed = {}
        for value, operation in zip(values, _regional_operations(state, attempt)):
            if any(value.get(name) != operation['reservation'].get(name) for name in ('config_sha256', 'deferred_config')):
                _fail('TASK_PLAN_COLLISION')
            quote = dict(quote_sha256=value['quote_sha256'], amount_usd=value['amount_usd'])
            if quote != operation['current_quote']:
                if operation['submission_started'] or operation['status'] != 'held':
                    _fail('TASK_PLAN_SUBMITTED')
                if any(q['quote_sha256'] == quote['quote_sha256'] and q != quote for q in operation['quote_history']):
                    _fail('TASK_OPERATION_COLLISION')
                changed[value['operation_id']] = quote
        if not changed:
            return dict(newly_reserved_usd='0')
        _attempt_open(state, key); _guard_submission(state, run['target_id'])
        with localcontext() as context:
            context.prec = 512
            delta = _sum(max(Decimal(q['amount_usd']) - Decimal(state['operations'][name]['amount_usd']), Decimal(0)) for name, q in changed.items())
        _cap(state, [str(delta)])
        for name, quote in changed.items():
            op = state['operations'][name]
            op['amount_usd'] = _money(max(Decimal(op['amount_usd']), Decimal(quote['amount_usd'])))
            op['quote_sha256'], op['current_quote'] = quote['quote_sha256'], quote
            if quote not in op['quote_history']:
                op['quote_history'].append(quote)
        return dict(newly_reserved_usd=_money(delta))
    if kind == 'record_regional_gate':
        _keys(payload, ('run_id', 'attempt_id', 'gate'))
        return _regional_gate(state, attempt, payload['gate'])
    operation = _operation(state, payload['operation_id']) if 'operation_id' in payload else None
    if kind == 'bind_operation_config':
        _keys(payload, ('operation_id', 'binding'))
        binding = payload['binding']
        _keys(binding, ('schema', 'config_sha256', 'config_file_sha256', 'input_set_sha256', 'provenance_sha256', 'deferred_plan_sha256'))
        if binding['schema'] != 'sprite_regional_execution_binding_v1':
            _fail()
        for name in ('config_sha256', 'config_file_sha256', 'input_set_sha256', 'provenance_sha256'):
            _sha(binding[name])
        if operation.get('execution_binding') is not None:
            if operation['execution_binding'] != binding:
                _fail('TASK_OPERATION_COLLISION')
            return dict(binding_sha256=operation['binding_sha256'])
        _attempt_open(state, key)
        if operation['submission_started'] or operation['status'] != 'held':
            _fail('TASK_PLAN_SUBMITTED')
        plan = attempt['regional_plan']
        role = operation['operation_id'].rsplit(':', 1)[1]
        inputs = plan['primary_inputs'] if role == 'primary' else [dict(role='grid', sha256=_regional_live_gate(attempt, 'raw_ready')['subject']['grid_sha256'])]
        deferred = operation.get('deferred_config')
        if (binding['input_set_sha256'] != _digest(inputs) or binding['provenance_sha256'] != plan['provenance']['sha256']
                or binding['deferred_plan_sha256'] != (_digest(deferred) if deferred else None)
                or (not deferred and binding['config_sha256'] != plan['primary_config_sha256'])):
            _fail('TASK_OPERATION_COLLISION')
        operation['execution_binding'], operation['binding_sha256'] = copy.deepcopy(binding), _digest(binding)
        return dict(binding_sha256=operation['binding_sha256'])
    if kind == 'mark_submission':
        _keys(payload, ('operation_id', 'expected'))
        expected = payload['expected']
        _keys(expected, ('schema', 'config_sha256', 'binding_sha256', 'quote_sha256', 'amount_usd', 'regional_state_sha256'))
        if expected['schema'] != 'sprite_regional_submission_expected_v1':
            _fail()
        token = _sha(expected['regional_state_sha256'])
        if not operation.get('provider_request_id') and token != regional_state_token(state, attempt['run_id'], attempt['attempt_id']):
            _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
        _expected(operation, {name: expected[name] for name in ('config_sha256', 'binding_sha256', 'quote_sha256', 'amount_usd')})
        if operation.get('provider_request_id'):
            return dict(submit_allowed=False, provider_request_id=operation['provider_request_id'],
                        regional_state_sha256=regional_state_token(state, attempt['run_id'], attempt['attempt_id']))
        _attempt_open(state, key)
        if operation['status'] != 'held' or operation['submission_started']:
            _fail('TASK_SUBMISSION_UNRESOLVED')
        _guard_submission(state, run['target_id']); _history_guard(state, run['target_id'], descriptor)
        _regional_history_source(state, attempt['regional_plan'], run['target_id'])
        operations = _regional_operations(state, attempt)
        index = _REGIONAL_ROLES.index(operation['operation_id'].rsplit(':', 1)[1])
        _regional_live_gate(attempt, 'input_ready' if index == 0 else 'raw_ready')
        if (len(operations) != 2 or attempt['regional_plan_sha256'] != _digest(attempt['regional_plan'])
                or any(not _endpoint_complete(op) for op in operations[:index])
                or any(op['status'] != 'held' or op['submission_started'] for op in operations[index:])):
            _fail('TASK_PLAN_INVALID')
        for op in operations:
            _permit(state, op['kind'])
        operation['status'], operation['submission_started'] = 'submission_pending', True
        return dict(submit_allowed=True, provider_request_id=None,
                    regional_state_sha256=regional_state_token(state, attempt['run_id'], attempt['attempt_id']))
    if kind == 'release_unsubmitted':
        _keys(payload, ('operation_id', 'evidence'))
        if operation['status'] == 'released' and operation.get('release_evidence') == payload['evidence']:
            return operation
        index = _REGIONAL_ROLES.index(operation['operation_id'].rsplit(':', 1)[1])
        if any(not _endpoint_terminal(op) for op in _regional_operations(state, attempt)[:index]):
            _fail('TASK_OPERATION_NOT_RELEASABLE')
        if operation['submission_started'] or operation['status'] != 'held':
            _fail('TASK_OPERATION_NOT_RELEASABLE')
        operation['status'], operation['release_evidence'] = 'released', _text(payload['evidence'])
        if attempt['regional_status'] == 'open':
            attempt['regional_status'] = 'released'
        return operation
    if kind == 'record_review':
        if payload['decision'] == 'pass':
            _attempt_open(state, key)
            if not all(_endpoint_complete(op) for op in _regional_operations(state, attempt)):
                _fail('TASK_PARENT_OPERATION_INCOMPLETE')
            _regional_live_gate(attempt, 'raw_ready')
        result = _apply_core(state, kind, payload)
        if payload['decision'] == 'reject':
            attempt['regional_status'] = 'rejected'
        return result
    result = _apply_core(state, kind, payload)
    if (kind == 'record_outcome' and payload['outcome']['status'] in ('failed', 'cancelled', 'rejected')
            and attempt['regional_status'] != 'rejected'):
        attempt['regional_status'] = 'failed'
    return result


def _feedback(correction):
    return tuple(' '.join(correction[key].casefold().split()) for key in ('defect', 'plan'))


def _lineage_runs(state, run_id):
    result = []
    while run_id:
        result.append(run_id)
        run_id = state['runs'][run_id].get('parent_run_id')
    return result


def _historical_defects(state, run_id):
    lineage = _lineage_runs(state, run_id)
    return [defect for attempt in state['attempts'].values()
            if attempt['run_id'] in lineage and attempt.get('review', {}).get('decision') == 'reject'
            for defect in attempt['review']['defects']]


def _remaining_defects(route, defects):
    prefix = 'position_x:' if route == 'body_x_alignment' else 'alpha_edge:'
    return [defect for defect in defects if not defect.lower().startswith(prefix)]


def _source_defects(state, attempt_key, visited=None):
    """Follow exact local provenance; a different generation is a new output.

    Binding identifies a logical attempt only. The consumer must independently
    prove that its actual source files/artifact set belong to that attempt.
    """
    visited = set() if visited is None else visited
    if attempt_key in visited:
        _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
    visited.add(attempt_key)
    attempt = state['attempts'][attempt_key]
    review = attempt.get('review', {})
    defects = list(review['defects']) if review.get('decision') == 'reject' else []
    defects.extend(defect for gate in attempt.get('endpoint_gate_history', []) if gate['decision'] == 'reject' for defect in gate['defects'])
    defects.extend(finding['check'] + ': ' + finding['notes']
                   for gate in attempt.get('regional_gate_history', []) if gate['decision'] == 'reject'
                   for finding in gate['findings'] if not finding['passed'])
    route = state['strategies'][attempt['strategy_sha256']]['effective_descriptor']['route']
    if route in ('body_x_alignment', 'alpha_edge_cleanup'):
        source = attempt.get('source_attempt')
        inherited = (_source_defects(state, _digest(source), visited) if source
                     else _historical_defects(state, attempt['run_id']))
        if review.get('decision') == 'pass':
            inherited = _remaining_defects(route, inherited)
        defects.extend(inherited)
    return defects


def _alignment_source_group(state, source_attempt):
    """Resolve only exact body-X ancestors, never caller labels or file paths."""
    visited = set()
    while True:
        key = _digest(source_attempt)
        if key in visited or key not in state['attempts']:
            _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
        visited.add(key)
        attempt = state['attempts'][key]
        route = state['strategies'][attempt['strategy_sha256']]['effective_descriptor']['route']
        if route != 'body_x_alignment':
            return source_attempt
        parent = attempt.get('source_attempt')
        if (parent is None or parent['run_id'] not in _lineage_runs(state, attempt['run_id'])
                or state['runs'][parent['run_id']]['target_id'] != state['runs'][attempt['run_id']]['target_id']):
            _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
        source_attempt = parent


def _exposure(state):
    return _sum(op['amount_usd'] for op in (*state['operations'].values(), *state['imports'].values())
                if op['status'] != 'released')


def _cap(state, additional):
    if _sum([str(_exposure(state)), *additional]) > Decimal(state['authorization']['cap_usd']):
        _fail('TASK_BUDGET_EXCEEDED')


def _request_unique(state, request_id, identity):
    if request_id is None:
        return
    for name, record in (*state['operations'].items(), *state['imports'].items()):
        if name != identity and record.get('provider_request_id') == request_id:
            _fail('TASK_OPERATION_COLLISION')


def _operation(state, operation_id):
    if operation_id in state['operations']:
        return state['operations'][operation_id]
    if operation_id in state['imports']:
        return state['imports'][operation_id]
    else:
        _fail('TASK_OPERATION_MISMATCH')


def _deferred_config(value):
    try:
        _keys(value, ('schema', 'primary_operation_id', 'primary_config_sha256', 'model', 'input',
                      'reserved_units', 'source_from', 'source_limits'))
        _keys(value['input'], ('sync_mode',))
        _keys(value['source_limits'], ('mime', 'max_bytes', 'max_width', 'max_height'))
        limits = dict(mime='image/png', max_bytes=10485760, max_width=4096, max_height=4096)
        if (value['schema'] != 'sprite_deferred_cleanup_v1' or value['model'] != 'fal-ai/ideogram/remove-background'
                or value['input']['sync_mode'] is not False or type(value['reserved_units']) is not int
                or value['reserved_units'] != 1 or value['source_from'] not in ('primary_raw_png', 'video_sample_grid_v1')
                or value['source_limits'] != limits
                or any(type(value['source_limits'][key]) is not int for key in ('max_bytes', 'max_width', 'max_height'))):
            _fail()
        return dict(schema=value['schema'], primary_operation_id=_text(value['primary_operation_id']),
                    primary_config_sha256=_sha(value['primary_config_sha256']), model=value['model'],
                    input=dict(sync_mode=False), reserved_units=1, source_from=value['source_from'], source_limits=limits)
    except WorkflowError:
        _fail('TASK_PLAN_INVALID')


def _commitment(value):
    _keys(value, ('operation_id', 'kind', 'quote_sha256', 'amount_usd'),
          ('config_sha256', 'deferred_config', 'provider_request_id'))
    if ('config_sha256' in value) == ('deferred_config' in value):
        _fail()
    if 'deferred_config' in value:
        if value['kind'] != 'alpha_edge_cleanup' or 'provider_request_id' in value:
            _fail()
        return dict(deferred_config=_deferred_config(value['deferred_config']))
    return dict(config_sha256=_sha(value['config_sha256']))


def _binding(value):
    _keys(value, ('schema', 'config_sha256', 'config_file_sha256', 'source_sha256', 'provenance_sha256', 'deferred_plan_sha256'))
    if value['schema'] != 'sprite_execution_binding_v1':
        _fail()
    result = {key: _sha(item) for key, item in value.items() if key not in ('schema', 'deferred_plan_sha256')}
    result.update(schema=value['schema'], deferred_plan_sha256=None if value['deferred_plan_sha256'] is None else _sha(value['deferred_plan_sha256']))
    return result


def _expected(operation, expected):
    _keys(expected, ('config_sha256', 'binding_sha256', 'quote_sha256', 'amount_usd'))
    expected = {key: _money(value, True) if key == 'amount_usd' else _sha(value) for key, value in expected.items()}
    binding = operation.get('execution_binding')
    if binding is None:
        _fail('TASK_OPERATION_NOT_MATERIALIZED')
    if expected['config_sha256'] != binding['config_sha256'] or expected['binding_sha256'] != operation['binding_sha256']:
        _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')
    if not operation.get('provider_request_id') and (
            expected['quote_sha256'] != operation['current_quote']['quote_sha256']
            or expected['amount_usd'] != operation['current_quote']['amount_usd']
            or Decimal(expected['amount_usd']) > Decimal(operation['amount_usd'])):
        _fail('TASK_SUBMISSION_CONTEXT_MISMATCH')


def _history_requirement(value):
    _keys(value, ('schema', 'migration_id', 'bundle_sha256'))
    if value['schema'] != 'sprite_history_requirement_v1':
        _fail()
    return dict(schema=value['schema'], migration_id=_text(value['migration_id']), bundle_sha256=_sha(value['bundle_sha256']))


def _history_id(value):
    if _text(value) != value:
        _fail()
    return value


def _history_refs(values, references):
    if not isinstance(values, list) or not values:
        _fail()
    for ref in values:
        _keys(ref, ('path', 'sha256'))
        path, sha = _history_id(ref['path']), _sha(ref['sha256'])
        if (not Path(path).is_absolute() or not Path(path).name or '\x00' in path
                or path.endswith(('/', '\\')) or (path in references and references[path] != sha)):
            _fail()
        references[path] = sha


def _history_mechanism(record, target, reservations):
    mechanism = record['mechanism']
    _keys(mechanism, ('origin', 'route', 'model', 'conditioning', 'resolution', 'current_descriptor'))
    origin, resolution, kind, basis = mechanism['origin'], mechanism['resolution'], record['kind'], record['cost_basis']
    if origin not in ('fal', 'image_gen', 'local') or resolution not in ('current_descriptor', 'legacy_unsupported', 'unresolved'):
        _fail()
    for key in ('route', 'model', 'conditioning'):
        if mechanism[key] is not None:
            _text(mechanism[key])
    if basis not in ('fal_operational_reservations', 'external_unverified', 'local_no_new_reservation'):
        _fail()
    if basis == 'fal_operational_reservations':
        if origin != 'fal' or not reservations:
            _fail()
    elif reservations or (basis == 'external_unverified' and origin != 'image_gen') or (basis == 'local_no_new_reservation' and origin != 'local'):
        _fail()
    if kind == 'provider_generation':
        if origin != 'fal' or basis != 'fal_operational_reservations' or not any(r['kind'] == 'provider_animation' for r in reservations):
            _fail()
    elif kind == 'native_construction':
        if origin != 'local' or basis != 'local_no_new_reservation':
            _fail()
    elif kind == 'external_image':
        if origin != 'image_gen' or basis != 'external_unverified':
            _fail()
    elif kind == 'local_derivation':
        if (origin not in ('local', 'fal') or any(r['kind'] != 'alpha_edge_cleanup' for r in reservations)
                or (origin == 'fal' and basis != 'fal_operational_reservations')):
            _fail()
    elif kind != 'unresolved':
        _fail()
    descriptor = mechanism['current_descriptor']
    model = _text(mechanism['model']).lower() if mechanism['model'] is not None else None
    known_route = next((route for route, models in _DESCRIPTOR_MODELS_V1.items() if model in models), None)
    if resolution == 'legacy_unsupported' and known_route is not None:
        _fail()
    if resolution == 'legacy_unsupported' and kind in ('provider_generation', 'native_construction') and model is None:
        _fail()
    if kind == 'local_derivation' and known_route in ('sheet', 'video', 'native_motion_construction'):
        _fail()
    if resolution == 'current_descriptor':
        normalized = _descriptor_v1(descriptor, target)
        if (descriptor != normalized or any(mechanism[key] != normalized[key] for key in ('route', 'model', 'conditioning'))
                or (kind == 'provider_generation' and normalized['route'] not in ('sheet', 'video'))
                or (kind == 'native_construction' and normalized['route'] != 'native_motion_construction')
                or (kind == 'local_derivation' and (origin != 'local' or normalized['route'] not in ('body_x_alignment', 'alpha_edge_cleanup')))
                or kind == 'external_image'):
            _fail()
    elif descriptor is not None:
        _fail()


def _history_bundle(state, bundle):
    _keys(bundle, ('schema', 'migration_id', 'eligibility_policy', 'coverage', 'reservations', 'attempts'))
    if bundle['schema'] != 'sprite_task_history_v1' or bundle['eligibility_policy'] != 'historical_generation_closed_local_source_v1':
        _fail('TASK_POLICY_UNSUPPORTED')
    _text(bundle['migration_id'])
    coverage, references = bundle['coverage'], {}
    _keys(coverage, ('target_ids', 'evidence', 'complete'))
    ids = coverage['target_ids']
    if (coverage['complete'] is not True or not isinstance(ids, list) or any(not isinstance(value, str) for value in ids)
            or len(ids) != len(set(ids)) or set(ids) != set(state['targets'])):
        _fail()
    _history_refs(coverage['evidence'], references)
    reservations, attempts = bundle['reservations'], bundle['attempts']
    if not isinstance(reservations, list) or len(reservations) > 256 or not isinstance(attempts, list) or len(attempts) > 512:
        _fail()
    by_reservation = {}
    for reservation in reservations:
        _keys(reservation, ('import_id', 'target_id', 'kind', 'source_sha256', 'config_sha256', 'evidence_sha256',
                            'amount_usd', 'provider_request_id', 'uncertain'))
        if not isinstance(reservation['amount_usd'], str) or reservation['amount_usd'] != _money(reservation['amount_usd'], True):
            _fail()
        name = _history_id(reservation['import_id'])
        _history_id(reservation['target_id'])
        if reservation['provider_request_id'] is not None:
            _history_id(reservation['provider_request_id'])
        if reservation['kind'] not in ('provider_animation', 'alpha_edge_cleanup'):
            _fail()
        if name in by_reservation:
            _fail('TASK_OPERATION_COLLISION')
        by_reservation[name] = reservation
    records, owners, identities = {}, set(), set()
    for record in attempts:
        _keys(record, ('history_id', 'target_id', 'source_sha256', 'run_id', 'attempt_id', 'attempt_index', 'source_history_id',
                      'kind', 'mechanism', 'local_attempt_limit', 'global_attempt_limit', 'feedback', 'cost_basis',
                      'reservation_ids', 'reviews', 'evidence'))
        name, target_id = _history_id(record['history_id']), _history_id(record['target_id'])
        if name in records:
            _fail()
        target = state['targets'].get(target_id)
        if not target or _sha(record['source_sha256']) != target['source_sha256']:
            _fail('TASK_SOURCE_MISMATCH')
        for key in ('run_id', 'attempt_id', 'source_history_id', 'feedback'):
            if record[key] is not None:
                if key == 'feedback':
                    _text(record[key])  # Keep the original feedback verbatim.
                else:
                    _history_id(record[key])
        for key in ('attempt_index', 'local_attempt_limit', 'global_attempt_limit'):
            if record[key] is not None and (type(record[key]) is not int or record[key] <= 0):
                _fail()
        if record['run_id'] is not None:
            for key in ('attempt_id', 'attempt_index'):
                if record[key] is not None:
                    identity = (target_id, record['run_id'], key, record[key])
                    if identity in identities:
                        _fail()
                    identities.add(identity)
        claims = record['reservation_ids']
        if not isinstance(claims, list):
            _fail()
        owned = []
        for claim in claims:
            _history_id(claim)
            if claim not in by_reservation or claim in owners:
                _fail()
            reservation = by_reservation[claim]
            if reservation['target_id'] != target_id or reservation['source_sha256'] != record['source_sha256']:
                _fail('TASK_SOURCE_MISMATCH')
            owners.add(claim)
            owned.append(reservation)
        _history_mechanism(record, target, owned)
        _history_refs(record['evidence'], references)
        owner_evidence = {ref['sha256'] for ref in record['evidence']}
        if any(_sha(reservation['evidence_sha256']) not in owner_evidence for reservation in owned):
            _fail()
        if not isinstance(record['reviews'], list):
            _fail()
        previous_sequence = 0
        for review in record['reviews']:
            _keys(review, ('stage', 'decision', 'original_decision', 'artifact_set_sha256', 'defects', 'evidence', 'sequence'))
            sequence = review['sequence']
            if sequence is not None:
                if type(sequence) is not int or sequence <= previous_sequence:
                    _fail()
                previous_sequence = sequence
            if review['stage'] not in ('raw', 'artifacts', 'technical', 'other') or review['decision'] not in ('pass', 'reject', 'hold', 'unresolved'):
                _fail()
            _text(review['original_decision'])
            if review['artifact_set_sha256'] is not None:
                _sha(review['artifact_set_sha256'])
            if not isinstance(review['defects'], list):
                _fail()
            for defect in review['defects']:
                _text(defect)
            _history_refs(review['evidence'], references)
        records[name] = record
    if owners != set(by_reservation):
        _fail()
    for name, record in records.items():
        visited, parent = {name}, record['source_history_id']
        while parent is not None:
            if parent not in records or parent in visited or records[parent]['target_id'] != record['target_id']:
                _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
            visited.add(parent)
            parent = records[parent]['source_history_id']
    return records


def _history_blocked_targets(state):
    if state['history_status'] == 'pending':
        return sorted(state['targets'])
    return sorted({record['target_id'] for record in state['historical_attempts'].values()
                   if record['kind'] == 'unresolved' or (record['kind'] != 'local_derivation'
                       and record['mechanism']['resolution'] == 'unresolved')})


def _history_guard(state, target_id, descriptor=None):
    if 'historical_import' not in state:
        return
    if state['history_status'] == 'pending':
        _fail('TASK_HISTORY_REQUIRED')
    if target_id in _history_blocked_targets(state):
        _fail('TASK_HISTORY_UNRESOLVED')
    if descriptor is not None and descriptor['route'] == _REGIONAL_ROUTE:
        for record in state['historical_attempts'].values():
            if record['target_id'] != target_id:
                continue
            mechanism = record['mechanism']
            model, conditioning, route = mechanism['model'], mechanism['conditioning'], mechanism['route']
            if (model == 'fal-ai/wan-vace-14b/inpainting' or conditioning == 'video_mask_reference'
                    or route in (_REGIONAL_ROUTE, 'spatial_video_mask_reference_v1')):
                _fail('TASK_HISTORY_MECHANISM_CLOSED')
            if record['kind'] not in ('provider_generation', 'unresolved'):
                continue
            if (model in ('fal-ai/nano-banana-2/edit', 'minimax/h3-max/image-to-video') and conditioning == 'single_source'):
                continue
            if model in ('fal-ai/wan-motion', 'fal-ai/veo3.1/first-last-frame-to-video',
                         'fal-ai/vidu/q2/reference-to-video', 'fal-ai/kling-video/v3/pro/image-to-video',
                         'fal-ai/wan-flf2v', 'fal-ai/bytedance/seedream/v4/edit', 'fal-ai/bytedance/seedream/v4.5/edit'):
                continue
            _fail('TASK_HISTORY_UNRESOLVED')
    if descriptor is not None and descriptor['route'] == 'endpoint_cycle_v1':
        for record in state['historical_attempts'].values():
            if record['target_id'] != target_id or record['kind'] not in ('provider_generation', 'unresolved'):
                continue
            mechanism = record['mechanism']
            model, conditioning = mechanism['model'], mechanism['conditioning']
            if _seedream_descriptor(descriptor):
                if model in ('fal-ai/bytedance/seedream/v4/edit', 'fal-ai/bytedance/seedream/v4.5/edit', 'fal-ai/wan-flf2v'):
                    _fail('TASK_HISTORY_MECHANISM_CLOSED')
                if model == 'fal-ai/nano-banana-2/edit' and conditioning != 'single_source':
                    _fail('TASK_HISTORY_UNRESOLVED')
            if model == 'fal-ai/nano-banana-2/edit':
                if conditioning == 'single_source':
                    continue
                if conditioning == 'pose_edit_then_opposite_endpoints':
                    _fail('TASK_HISTORY_MECHANISM_CLOSED')
                _fail('TASK_HISTORY_UNRESOLVED')
            if model == 'fal-ai/wan-flf2v':
                if conditioning == 'pose_edit_then_opposite_endpoints':
                    _fail('TASK_HISTORY_MECHANISM_CLOSED')
                _fail('TASK_HISTORY_UNRESOLVED')
            if model == 'minimax/h3-max/image-to-video' and conditioning == 'single_source':
                continue
            # Closed reconciliation of the frozen inventory (root's preflight ruling).
            # These are distinct endpoints, never aliases for FLF2V. The historical
            # parser/catalog remains frozen and may retain conditioning=None.
            if model in ('fal-ai/wan-motion', 'fal-ai/veo3.1/first-last-frame-to-video',
                         'fal-ai/vidu/q2/reference-to-video', 'fal-ai/kling-video/v3/pro/image-to-video'):
                continue
            # Uncatalogued provider generations cannot prove disjointness.
            _fail('TASK_HISTORY_UNRESOLVED')
    if descriptor is not None and descriptor['route'] in ('sheet', 'video', 'native_motion_construction'):
        if any(record['target_id'] == target_id and record['kind'] in ('provider_generation', 'native_construction', 'external_image')
               and record['mechanism']['current_descriptor'] == descriptor for record in state['historical_attempts'].values()):
            _fail('TASK_HISTORY_MECHANISM_CLOSED')


def _import_reservations(state, entries):
    if not isinstance(entries, list) or not entries or len(entries) > 256:
        _fail()
    pending = {}
    for value in entries:
        _keys(value, ('import_id', 'target_id', 'kind', 'source_sha256', 'config_sha256', 'evidence_sha256',
                      'amount_usd', 'provider_request_id', 'uncertain'))
        import_id, target_id = _text(value['import_id']), _text(value['target_id'])
        if target_id not in state['targets'] or _sha(value['source_sha256']) != state['targets'][target_id]['source_sha256']:
            _fail('TASK_SOURCE_MISMATCH')
        _permit(state, value['kind'])
        request_id = value['provider_request_id']
        if request_id is not None:
            request_id = _text(request_id)
        if type(value['uncertain']) is not bool or (not request_id and not value['uncertain']):
            _fail()
        entry = dict(import_id=import_id, target_id=target_id, kind=value['kind'], source_sha256=_sha(value['source_sha256']),
                     config_sha256=_sha(value['config_sha256']), evidence_sha256=_sha(value['evidence_sha256']),
                     amount_usd=_money(value['amount_usd'], True), provider_request_id=request_id, uncertain=value['uncertain'])
        old = state['imports'].get(import_id) or pending.get(import_id)
        if import_id in state['operations'] or (old and old['reservation'] != entry):
            _fail('TASK_OPERATION_COLLISION')
        _request_unique(state, request_id, import_id)
        if not old:
            pending[import_id] = dict(**entry, reservation=entry, status='uncertain' if entry['uncertain'] else 'committed',
                                     submission_started=True, outcomes=[])
    request_ids = [value['provider_request_id'] for value in pending.values() if value['provider_request_id']]
    if len(request_ids) != len(set(request_ids)):
        _fail('TASK_OPERATION_COLLISION')
    _cap(state, [value['amount_usd'] for value in pending.values()])
    state['imports'].update(pending)
    return dict(import_ids=[value['import_id'] for value in entries])


def _apply(state, kind, payload):
    attempt = None
    if isinstance(payload, dict):
        if 'run_id' in payload and 'attempt_id' in payload and isinstance(payload['run_id'], str) and isinstance(payload['attempt_id'], str):
            attempt = state['attempts'].get(_digest(dict(run_id=payload['run_id'], attempt_id=payload['attempt_id'])))
        elif isinstance(payload.get('operation_id'), str):
            operation = state['operations'].get(payload['operation_id'])
            if operation:
                attempt = state['attempts'].get(operation['attempt_key'])
    if kind != 'begin_attempt' and attempt is not None and _regional_attempt(state, attempt):
        return _regional_apply(state, kind, payload, attempt)
    if kind != 'begin_attempt' and attempt is not None and _endpoint_attempt(state, attempt):
        return _endpoint_apply(state, kind, payload, attempt)
    if kind in ('record_endpoint_gate', 'record_regional_gate') or (isinstance(payload, dict)
            and ('endpoint_plan' in payload or 'regional_plan' in payload)):
        _fail('TASK_PLAN_INVALID')
    return _apply_core(state, kind, payload)


def _apply_core(state, kind, payload):
    """Validate all transitions during live calls AND replay (no cached totals)."""
    if 'historical_import' in state and kind != 'import_task_history':
        if state['history_status'] == 'pending' or kind == 'import_reservations':
            _fail('TASK_HISTORY_REQUIRED')
    if kind == 'import_task_history':
        requirement = state.get('historical_import')
        if requirement is None:
            _fail('TASK_HISTORY_CONFLICT')
        _keys(payload, ('bundle',))
        bundle = payload['bundle']
        if (not isinstance(bundle, dict) or bundle.get('migration_id') != requirement['migration_id']
                or _digest(bundle) != requirement['bundle_sha256']):
            _fail('TASK_HISTORY_CONFLICT')
        if state['history_status'] != 'complete':
            records = _history_bundle(state, bundle)
            if bundle['reservations']:
                _import_reservations(state, bundle['reservations'])
            state['historical_attempts'], state['history_status'] = records, 'complete'
        return dict(migration_id=requirement['migration_id'], bundle_sha256=requirement['bundle_sha256'], history_status='complete')
    if kind == 'bind_run':
        _keys(payload, ('run',))
        run = payload['run']
        _keys(run, ('run_id', 'target_id', 'scope_sha256', 'manifest_path'), ('parent_run_id',))
        run = {key: (_sha(value) if key == 'scope_sha256' else _text(value)) for key, value in run.items()}
        if run['target_id'] not in state['targets']:
            _fail('TASK_SOURCE_MISMATCH')
        parent = run.get('parent_run_id')
        if parent and (_run(state, parent)['target_id'] != run['target_id'] or parent == run['run_id']):
            _fail('TASK_SOURCE_MISMATCH')
        old = state['runs'].get(run['run_id'])
        if old and old != run:
            _fail('TASK_SCOPE_COLLISION')
        state['runs'][run['run_id']] = run
        return run
    if kind == 'begin_attempt':
        _keys(payload, ('run_id', 'attempt_id', 'effective_descriptor', 'max_attempts', 'correction'),
              ('source_attempt', 'strategy_scope', 'endpoint_plan_sha256', 'regional_plan_sha256'))
        run_id, attempt_id = _text(payload['run_id']), _text(payload['attempt_id'])
        run = _run(state, run_id)
        descriptor = _descriptor(payload['effective_descriptor'], state['targets'][run['target_id']])
        for existing in state['attempts'].values():
            if existing['run_id'] == run_id and (descriptor['route'] == 'endpoint_cycle_v1' or _endpoint_attempt(state, existing)):
                if existing['attempt_id'] != attempt_id or state['strategies'][existing['strategy_sha256']]['effective_descriptor'] != descriptor:
                    _fail('TASK_STRATEGY_COLLISION')
        _history_guard(state, run['target_id'], descriptor)
        operation_kind = 'provider_animation' if descriptor['route'] in ('sheet', 'video', 'endpoint_cycle_v1', 'regional_video_inpaint_v1') else descriptor['route']
        _permit(state, operation_kind)
        limit = payload['max_attempts']
        if type(limit) is not int or not 1 <= limit <= 3:
            _fail()
        if descriptor['route'] == 'endpoint_cycle_v1' and (limit != (1 if _seedream_descriptor(descriptor) else 2)
                or attempt_id != run_id + ':attempt-01'):
            _fail('TASK_POLICY_UNSUPPORTED')
        if descriptor['route'] == 'regional_video_inpaint_v1':
            if 'historical_import' not in state or state.get('history_status') != 'complete':
                _fail('TASK_HISTORY_REQUIRED')
            if (limit != 1 or attempt_id != run_id + ':attempt-01' or payload['correction'] is not None
                    or payload.get('source_attempt') is not None or payload.get('strategy_scope', 'target') != 'target'
                    or 'endpoint_plan_sha256' in payload):
                _fail('TASK_POLICY_UNSUPPORTED')
            try:
                regional_commitment = _sha(payload.get('regional_plan_sha256'))
            except WorkflowError:
                _fail('TASK_PLAN_INVALID')
        elif 'regional_plan_sha256' in payload:
            _fail('TASK_POLICY_UNSUPPORTED')
        if _seedream_descriptor(descriptor):
            try:
                plan_commitment = _sha(payload.get('endpoint_plan_sha256'))
            except WorkflowError:
                _fail('TASK_PLAN_INVALID')
        elif 'endpoint_plan_sha256' in payload:
            _fail('TASK_POLICY_UNSUPPORTED')
        strategy_scope = payload.get('strategy_scope', 'target')
        if (strategy_scope not in ('target', 'alignment_source_v1')
                or (strategy_scope == 'alignment_source_v1' and descriptor['route'] != 'body_x_alignment')):
            _fail('TASK_POLICY_UNSUPPORTED')
        correction = payload['correction']
        if correction is not None:
            _keys(correction, ('defect', 'plan', 'evidence'))
            correction = {key: _text(value) for key, value in correction.items()}
        key = _digest(dict(run_id=run_id, attempt_id=attempt_id))
        source_attempt = payload.get('source_attempt')
        if ('historical_import' in state and descriptor['route'] in ('body_x_alignment', 'alpha_edge_cleanup')
                and source_attempt is None):
            _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
        if strategy_scope == 'alignment_source_v1' and source_attempt is None:
            _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
        if source_attempt is not None:
            if descriptor['route'] in ('sheet', 'video', 'endpoint_cycle_v1'):
                _fail('TASK_POLICY_UNSUPPORTED')
            try:
                _keys(source_attempt, ('run_id', 'attempt_id'))
                source_attempt = {name: _text(value) for name, value in source_attempt.items()}
            except WorkflowError:
                if strategy_scope == 'alignment_source_v1':
                    _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
                raise
            source_key = _digest(source_attempt)
            if (source_attempt['run_id'] not in _lineage_runs(state, run_id)
                    or source_key not in state['attempts'] or source_key == key
                    or state['runs'][source_attempt['run_id']]['target_id'] != run['target_id']):
                _fail('TASK_SOURCE_ATTEMPT_MISMATCH')
        fingerprint = dict(target_id=run['target_id'], effective_descriptor=descriptor)
        if strategy_scope == 'alignment_source_v1':
            fingerprint.update(strategy_scope=strategy_scope, source_group=_alignment_source_group(state, source_attempt))
        if any(value['target_id'] == run['target_id'] and value['effective_descriptor'] == descriptor
               and value.get('strategy_scope', 'target') != strategy_scope for value in state['strategies'].values()):
            _fail('TASK_STRATEGY_COLLISION')
        strategy_sha = _digest(fingerprint)
        strategy = state['strategies'].get(strategy_sha)
        if strategy and strategy['max_attempts'] != limit:
            _fail('TASK_STRATEGY_COLLISION')
        if any(value['attempt_id'] == attempt_id and value['run_id'] != run_id for value in state['attempts'].values()):
            _fail('TASK_STRATEGY_COLLISION')
        attempt = dict(run_id=run_id, attempt_id=attempt_id, strategy_sha256=strategy_sha,
                       correction=correction, source_attempt=source_attempt)
        if descriptor['route'] == 'regional_video_inpaint_v1':
            attempt.update(regional_plan_sha256=regional_commitment, scope_sha256=run['scope_sha256'])
        if _seedream_descriptor(descriptor):
            attempt.update(endpoint_plan_sha256=plan_commitment, scope_sha256=run['scope_sha256'])
        old = state['attempts'].get(key)
        if old:
            if {key: old[key] for key in attempt} != attempt:
                _fail('TASK_STRATEGY_COLLISION')
            if _seedream_descriptor(descriptor):
                _seedream_guard(state, run['target_id'], old)
            return strategy_sha
        if _seedream_descriptor(descriptor):
            _seedream_guard(state, run['target_id'])
        _guard_submission(state, run['target_id'])
        if strategy:
            if len(strategy['attempt_keys']) >= limit:
                _fail('TASK_STRATEGY_EXHAUSTED')
            prior = [state['attempts'][key] for key in strategy['attempt_keys']]
            if descriptor['route'] == 'endpoint_cycle_v1':
                previous = prior[-1]
                if (previous.get('endpoint_status') not in ('failed', 'rejected') or correction is None
                        or not previous.get('plan_operation_ids')
                        or not all(_endpoint_terminal(op) for op in _endpoint_operations(state, previous))
                        or _feedback(correction)[0] == _feedback(correction)[1]
                        or any(item['correction'] is not None and _feedback(item['correction']) == _feedback(correction) for item in prior)):
                    _fail('TASK_CORRECTION_REQUIRED')
                defects = _source_defects(state, _digest(dict(run_id=previous['run_id'], attempt_id=previous['attempt_id'])))
                if defects and correction['defect'] not in defects:
                    _fail('TASK_CORRECTION_REQUIRED')
            rejected = [item for item in prior if item.get('review', {}).get('decision') == 'reject']
            if rejected:
                latest = rejected[-1]['review']
                if correction is None or correction['defect'] not in latest['defects']:
                    _fail('TASK_CORRECTION_REQUIRED')
                if correction['plan'].casefold() == correction['defect'].casefold():
                    _fail('TASK_CORRECTION_REQUIRED')
                if any(item['correction'] is not None and _feedback(item['correction']) == _feedback(correction) for item in prior):
                    _fail('TASK_CORRECTION_REQUIRED')
        else:
            strategy = dict(**fingerprint, max_attempts=limit, attempt_keys=[])
            state['strategies'][strategy_sha] = strategy
        strategy['attempt_keys'].append(key)
        attempt['review_history'] = []
        state['attempts'][key] = attempt
        return strategy_sha
    if kind == 'reserve_plan':
        _keys(payload, ('run_id', 'attempt_id', 'strategy_sha256', 'operations'))
        run_id, strategy_sha = _text(payload['run_id']), _sha(payload['strategy_sha256'])
        run = _run(state, run_id)
        strategy = state['strategies'].get(strategy_sha)
        if not strategy or strategy['target_id'] != run['target_id'] or not any(
                state['attempts'][key]['run_id'] == run_id for key in strategy['attempt_keys']):
            _fail('TASK_STRATEGY_MISMATCH')
        _history_guard(state, run['target_id'], strategy['effective_descriptor'])
        matching = [key for key in strategy['attempt_keys'] if state['attempts'][key]['run_id'] == run_id]
        attempt_key = _digest(dict(run_id=run_id, attempt_id=_text(payload['attempt_id'])))
        if attempt_key not in matching:
            _fail('TASK_STRATEGY_MISMATCH')
        attempt = state['attempts'][attempt_key]
        if strategy['effective_descriptor']['route'] not in ('sheet', 'video'):
            _fail('TASK_PLAN_INVALID')
        operations = payload['operations']
        if not isinstance(operations, list) or not operations or len(operations) > 64:
            _fail()
        pending = {}
        for value in operations:
            commitment = _commitment(value)
            operation_id = _text(value['operation_id'])
            _permit(state, value['kind'])
            if value['kind'] in ('body_x_alignment', 'native_motion_construction'):
                _fail('TASK_POLICY_UNSUPPORTED')  # local-only operations have no economic reservation
            operation = dict(operation_id=operation_id, run_id=run_id, target_id=run['target_id'], attempt_key=attempt_key,
                             strategy_sha256=strategy_sha, kind=value['kind'], **commitment,
                             quote_sha256=_sha(value['quote_sha256']), amount_usd=_money(value['amount_usd'], True),
                             provider_request_id=value.get('provider_request_id'))
            if operation['provider_request_id'] is not None:
                operation['provider_request_id'] = _text(operation['provider_request_id'])
                _request_unique(state, operation['provider_request_id'], operation_id)
            old = state['operations'].get(operation_id) or pending.get(operation_id)
            if operation_id in state['imports']:
                _fail('TASK_OPERATION_COLLISION')
            if old:
                if old['reservation'] != operation:
                    _fail('TASK_OPERATION_COLLISION')
            else:
                pending[operation_id] = dict(**operation, reservation=operation, status='committed' if operation['provider_request_id'] else 'held',
                                             submission_started=bool(operation['provider_request_id']), outcomes=[],
                                             current_quote=dict(quote_sha256=operation['quote_sha256'], amount_usd=operation['amount_usd']),
                                             quote_history=[dict(quote_sha256=operation['quote_sha256'], amount_usd=operation['amount_usd'])])
        plan_kinds = {_text(value['operation_id']): value['kind'] for value in operations}
        if (list(plan_kinds.values()).count('provider_animation') != 1
                or list(plan_kinds.values()).count('alpha_edge_cleanup') > 1):
            _fail('TASK_PLAN_INVALID')
        full_plan = {name: pending.get(name) or state['operations'][name] for name in plan_kinds}
        for operation in full_plan.values():
            if 'deferred_config' in operation:
                deferred = operation['deferred_config']
                parent = full_plan.get(deferred['primary_operation_id'])
                source_from = 'primary_raw_png' if strategy['effective_descriptor']['route'] == 'sheet' else 'video_sample_grid_v1'
                if (not parent or parent['kind'] != 'provider_animation' or parent['attempt_key'] != attempt_key
                        or parent.get('config_sha256') != deferred['primary_config_sha256'] or deferred['source_from'] != source_from):
                    _fail('TASK_PLAN_INVALID')
        plan = sorted(plan_kinds)
        if attempt.get('plan_operation_ids') is not None and attempt['plan_operation_ids'] != plan:
            _fail('TASK_PLAN_COLLISION')
        if pending:
            _attempt_open(state, attempt_key)
            _guard_submission(state, run['target_id'])
        _cap(state, [value['amount_usd'] for value in pending.values()])
        request_ids = [value['provider_request_id'] for value in pending.values() if value['provider_request_id']]
        if len(request_ids) != len(set(request_ids)):
            _fail('TASK_OPERATION_COLLISION')
        state['operations'].update(pending)
        attempt['plan_operation_ids'] = plan
        return dict(operation_ids=[value['operation_id'] for value in operations], newly_reserved_usd=_money(_sum(value['amount_usd'] for value in pending.values())))
    if kind == 'reprice_plan':
        _keys(payload, ('run_id', 'attempt_id', 'operations'))
        run_id, attempt_id = _text(payload['run_id']), _text(payload['attempt_id'])
        run = _run(state, run_id)
        attempt_key = _digest(dict(run_id=run_id, attempt_id=attempt_id))
        if attempt_key not in state['attempts']:
            _fail('TASK_STRATEGY_MISMATCH')
        attempt = state['attempts'][attempt_key]
        values = payload['operations']
        if not isinstance(values, list) or not values or len(values) > 64:
            _fail()
        revised = {}
        for value in values:
            commitment = _commitment(value)
            operation_id = _text(value['operation_id'])
            if operation_id not in attempt.get('plan_operation_ids', []):
                _fail('TASK_PLAN_COLLISION')
            operation = _operation(state, operation_id)
            original_commitment = {key: operation['reservation'][key] for key in ('config_sha256', 'deferred_config') if key in operation['reservation']}
            if (value['kind'] != operation['kind'] or commitment != original_commitment
                    or value.get('provider_request_id') != operation['reservation']['provider_request_id']):
                _fail('TASK_PLAN_COLLISION')
            quote = dict(quote_sha256=_sha(value['quote_sha256']), amount_usd=_money(value['amount_usd'], True))
            if operation_id in revised and revised[operation_id] != quote:
                _fail('TASK_OPERATION_COLLISION')
            if any(old['quote_sha256'] == quote['quote_sha256'] and old != quote for old in operation['quote_history']):
                _fail('TASK_OPERATION_COLLISION')
            revised[operation_id] = quote
        if sorted(revised) != attempt.get('plan_operation_ids'):
            _fail('TASK_PLAN_COLLISION')
        changed = {name: quote for name, quote in revised.items() if state['operations'][name]['current_quote'] != quote}
        if not changed:
            return dict(newly_reserved_usd='0')
        _attempt_open(state, attempt_key)
        if any(state['operations'][name]['submission_started'] or state['operations'][name]['status'] != 'held' for name in changed):
            _fail('TASK_PLAN_SUBMITTED')
        _guard_submission(state, run['target_id'])
        with localcontext() as context:
            context.prec = 512
            delta = _sum(max(Decimal(quote['amount_usd']) - Decimal(state['operations'][name]['amount_usd']), Decimal(0))
                         for name, quote in changed.items())
        _cap(state, [str(delta)])
        for name, quote in changed.items():
            operation = state['operations'][name]
            operation['amount_usd'] = _money(max(Decimal(operation['amount_usd']), Decimal(quote['amount_usd'])))
            operation['quote_sha256'], operation['current_quote'] = quote['quote_sha256'], quote
            if quote not in operation['quote_history']:
                operation['quote_history'].append(quote)
        return dict(newly_reserved_usd=_money(delta))
    if kind == 'bind_operation_config':
        _keys(payload, ('operation_id', 'binding'))
        operation = _operation(state, _text(payload['operation_id']))
        binding = _binding(payload['binding'])
        if operation.get('execution_binding') is not None:
            if operation['execution_binding'] != binding:
                _fail('TASK_OPERATION_COLLISION')
            return dict(binding_sha256=operation['binding_sha256'])
        if operation['status'] != 'held' or operation['submission_started']:
            _fail('TASK_PLAN_SUBMITTED')
        _attempt_open(state, operation['attempt_key'])
        deferred = operation.get('deferred_config')
        if deferred:
            if binding['deferred_plan_sha256'] != _digest(deferred):
                _fail('TASK_OPERATION_COLLISION')
            parent = _operation(state, deferred['primary_operation_id'])
            if not parent.get('provider_request_id') or not any(outcome['status'] == 'completed' for outcome in parent['outcomes']):
                _fail('TASK_PARENT_OPERATION_INCOMPLETE')
        elif binding['deferred_plan_sha256'] is not None or binding['config_sha256'] != operation['config_sha256']:
            _fail('TASK_OPERATION_COLLISION')
        operation['execution_binding'], operation['binding_sha256'] = binding, _digest(binding)
        return dict(binding_sha256=operation['binding_sha256'])
    if kind == 'mark_submission':
        _keys(payload, ('operation_id',), ('expected',))
        operation = _operation(state, _text(payload['operation_id']))
        if payload.get('expected') is not None:
            _expected(operation, payload['expected'])
        if 'deferred_config' in operation and operation.get('execution_binding') is None:
            _fail('TASK_OPERATION_NOT_MATERIALIZED')
        if operation.get('provider_request_id'):
            return dict(submit_allowed=False, provider_request_id=operation['provider_request_id'])
        if operation['status'] != 'held' or operation['submission_started']:
            _fail('TASK_SUBMISSION_UNRESOLVED')
        descriptor = (state['strategies'][operation['strategy_sha256']]['effective_descriptor']
                      if operation['kind'] == 'provider_animation' else None)
        _history_guard(state, operation['target_id'], descriptor)
        if operation['kind'] == 'provider_animation':
            attempt = state['attempts'][operation['attempt_key']]
            for dependent_id in attempt.get('plan_operation_ids', []):
                dependent = state['operations'][dependent_id]
                deferred = dependent.get('deferred_config')
                if (deferred and deferred['primary_operation_id'] == operation['operation_id']
                        and (dependent['status'] != 'held' or dependent['submission_started'])):
                    _fail('TASK_PLAN_INVALID')
        _attempt_open(state, operation['attempt_key'])
        _guard_submission(state, operation['target_id'])
        operation['status'] = 'submission_pending'
        operation['submission_started'] = True
        return dict(submit_allowed=True, provider_request_id=None)
    if kind == 'record_outcome':
        _keys(payload, ('operation_id', 'outcome'))
        operation_id = _text(payload['operation_id'])
        operation = _operation(state, operation_id)
        value = payload['outcome']
        _keys(value, ('status', 'evidence'), ('provider_request_id',))
        status, evidence = _text(value['status']), _text(value['evidence'])
        if status not in _OUTCOMES or not operation['submission_started'] or operation['status'] == 'released':
            _fail('TASK_OUTCOME_CONFLICT')
        request_id = value.get('provider_request_id')
        if request_id is not None:
            request_id = _text(request_id)
        previous_id = operation.get('provider_request_id')
        if previous_id and request_id and previous_id != request_id:
            _fail('TASK_OPERATION_COLLISION')
        request_id = request_id or previous_id
        _request_unique(state, request_id, operation_id)
        outcome = dict(status=status, evidence=evidence, provider_request_id=request_id)
        if outcome in operation['outcomes']:
            return operation
        terminal = [item for item in operation['outcomes'] if item['status'] in _TERMINAL]
        if terminal and terminal[-1]['status'] != status:
            _fail('TASK_OUTCOME_CONFLICT')
        operation['outcomes'].append(outcome)
        operation['provider_request_id'] = request_id
        operation['status'] = 'committed' if request_id else 'uncertain'
        return operation
    if kind == 'release_unsubmitted':
        _keys(payload, ('operation_id', 'evidence'))
        operation = _operation(state, _text(payload['operation_id']))
        evidence = _text(payload['evidence'])
        if operation['status'] == 'released' and operation.get('release_evidence') == evidence:
            return operation
        if operation['submission_started'] or operation['status'] != 'held':
            _fail('TASK_OPERATION_NOT_RELEASABLE')
        deferred = operation.get('deferred_config')
        if deferred:
            parent = _operation(state, deferred['primary_operation_id'])
            abandoned_before_start = parent['status'] == 'released' and not parent['submission_started']
            known_terminal = parent.get('provider_request_id') and any(outcome['status'] in _TERMINAL for outcome in parent['outcomes'])
            if not abandoned_before_start and not known_terminal:
                _fail('TASK_OPERATION_NOT_RELEASABLE')
        operation['status'], operation['release_evidence'] = 'released', evidence
        return operation
    if kind == 'record_review':
        _keys(payload, ('run_id', 'attempt_id', 'decision', 'defects', 'evidence'))
        run_id, attempt_id = _text(payload['run_id']), _text(payload['attempt_id'])
        run = _run(state, run_id)
        key = _digest(dict(run_id=run_id, attempt_id=attempt_id))
        if key not in state['attempts']:
            _fail('TASK_STRATEGY_MISMATCH')
        attempt = state['attempts'][key]
        decision, evidence = _text(payload['decision']), _text(payload['evidence'])
        if decision not in ('pass', 'reject') or not isinstance(payload['defects'], list):
            _fail()
        defects = [_text(value) for value in payload['defects']]
        if (decision == 'reject' and not defects) or (decision == 'pass' and defects):
            _fail()
        review = dict(decision=decision, defects=defects, evidence=evidence)
        if 'review' in attempt:
            if attempt['review'] == review:
                return review
            if attempt['review']['decision'] != 'pass' or decision != 'reject':
                _fail('TASK_REVIEW_CONFLICT')
        route = state['strategies'][attempt['strategy_sha256']]['effective_descriptor']['route']
        if decision == 'pass' and route in ('body_x_alignment', 'alpha_edge_cleanup'):
            source = attempt.get('source_attempt')
            defects = (_source_defects(state, _digest(source)) if source else _historical_defects(state, run_id))
            if _remaining_defects(route, defects):
                _fail('TASK_SOURCE_DEFECT_UNRESOLVED')
        attempt['review'] = review
        attempt['review_history'].append(review)
        return review
    if kind == 'import_reservations':
        _keys(payload, ('entries',))
        return _import_reservations(state, payload['entries'])
    _fail('TASK_JOURNAL_CORRUPT')


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
        _fail('TASK_JOURNAL_CORRUPT')
    try:
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    _fail('TASK_JOURNAL_CORRUPT')
                value[key] = item
            return value
        return json.loads(path.read_text('utf-8'), object_pairs_hook=pairs)
    except (ValueError, UnicodeError, OSError):
        _fail('TASK_JOURNAL_CORRUPT')


def _replay(path):
    try:
        manifest = _read(path)
        _keys(manifest, ('schema', 'authorization', 'targets', 'authorization_sha256'), ('historical_import',))
        if manifest['schema'] != 'sprite_task_ledger_v1':
            _fail()
        authorization = _authorization(manifest['authorization'])
        targets = _targets(list(manifest['targets'].values()))
        sealed = dict(authorization=authorization, targets=targets)
        if 'historical_import' in manifest:
            requirement = _history_requirement(manifest['historical_import'])
            if requirement != manifest['historical_import']:
                _fail()
            sealed['historical_import'] = requirement
        seal = _digest(sealed)
        if manifest['authorization_sha256'] != seal or manifest['targets'] != targets or manifest['authorization'] != authorization:
            _fail()
        state = dict(task_id=authorization['task_id'], authorization=authorization, targets=targets,
                     authorization_sha256=seal, runs={}, strategies={}, attempts={}, operations={}, imports={})
        if 'historical_import' in manifest:
            state.update(historical_import=requirement, history_status='pending', historical_attempts={})
        head = _read(path.parent / 'head.json')
        _keys(head, ('sequence', 'event_sha256', 'authorization_sha256'))
        if type(head['sequence']) is not int or head['sequence'] < 0 or head['authorization_sha256'] != seal:
            _fail()
        events_dir = path.parent / 'events'
        if events_dir.is_symlink() or not events_dir.is_dir():
            _fail()
        files = sorted(events_dir.iterdir())
        if len(files) > 100000 or any(not _EVENT.fullmatch(item.name) for item in files):
            _fail()
        previous, checked_head = seal, head['sequence'] == 0 and head['event_sha256'] == seal
        for sequence, event_path in enumerate(files, 1):
            if event_path.name != f'{sequence:08d}.json':
                _fail()
            event = _read(event_path)
            _keys(event, ('sequence', 'previous_sha256', 'kind', 'payload', 'sha256'))
            content = {key: value for key, value in event.items() if key != 'sha256'}
            if event['sequence'] != sequence or event['previous_sha256'] != previous or _digest(content) != event['sha256']:
                _fail()
            _apply(state, event['kind'], event['payload'])
            previous = event['sha256']
            if sequence == head['sequence']:
                checked_head = head['event_sha256'] == previous
        if not checked_head or head['sequence'] > len(files):
            _fail()
        return state, dict(sequence=len(files), event_sha256=previous, authorization_sha256=seal), head
    except (WorkflowError, KeyError, TypeError, AttributeError, OSError, OverflowError):
        _fail('TASK_JOURNAL_CORRUPT')


@contextmanager
def _locked(task_manifest):
    if not isinstance(task_manifest, (str, os.PathLike)) or not str(task_manifest).strip():
        _fail('TASK_CONTEXT_REQUIRED')
    path = Path(task_manifest).absolute()
    lock_path = path.parent / '.run.lock'
    if (path.is_symlink() or not path.is_file() or lock_path.is_symlink() or not lock_path.is_file()
            or lock_path.stat().st_size != 1):
        _fail('TASK_JOURNAL_CORRUPT')
    deadline = time.monotonic() + 15
    while True:
        manager = run_lock(path.parent)
        try:
            manager.__enter__()
            break
        except WorkflowError as exc:
            if exc.code != 'RUN_LOCKED' or time.monotonic() >= deadline:
                _fail('TASK_LOCKED')
            time.sleep(.01)
    try:
        yield path
    finally:
        manager.__exit__(None, None, None)


def _mutate(task_manifest, kind, payload):
    try:
        def decimal_json(value):
            if isinstance(value, Decimal) and kind != 'import_task_history':
                return _money(value)
            _fail()
        payload = json.loads(json.dumps(payload, default=decimal_json, allow_nan=False))
        def money_fields(value):
            if isinstance(value, list):
                return [money_fields(item) for item in value]
            if isinstance(value, dict):
                return {key: _money(item) if key in ('amount_usd', 'cap_usd') else money_fields(item)
                        for key, item in value.items()}
            return value
        if kind != 'import_task_history':
            payload = money_fields(payload)
    except (TypeError, ValueError, OverflowError):
        _fail()
    with _locked(task_manifest) as path:
        state, tail, head = _replay(path)
        before = copy.deepcopy(state)
        result = _apply(state, kind, payload)
        bounded_endpoint = any(_endpoint_attempt(state, attempt) or _regional_attempt(state, attempt) for attempt in state['attempts'].values())
        if (kind == 'import_task_history' or bounded_endpoint) and state != before:
            content = dict(sequence=tail['sequence'] + 1, previous_sha256=tail['event_sha256'], kind=kind, payload=payload)
            if len(encoded(dict(**content, sha256=_digest(content)))) > 5 * 1024 * 1024:
                _fail()
        if tail != head:
            atomic_json(path.parent / 'head.json', tail)
        if state == before:
            return copy.deepcopy(result)
        content = dict(sequence=tail['sequence'] + 1, previous_sha256=tail['event_sha256'], kind=kind, payload=payload)
        event = dict(**content, sha256=_digest(content))
        new_or_identical(path.parent / 'events' / f"{content['sequence']:08d}.json", encoded(event))
        atomic_json(path.parent / 'head.json', dict(sequence=content['sequence'], event_sha256=event['sha256'],
                                                   authorization_sha256=tail['authorization_sha256']))
        return copy.deepcopy(result)


def create_task(output_root, authorization, targets, *, historical_import=None):
    authorization, targets = _authorization(authorization), _targets(targets)
    sealed = dict(authorization=authorization, targets=targets)
    if historical_import is not None:
        sealed['historical_import'] = _history_requirement(historical_import)
    root = Path(output_root).absolute()
    installed = Path(__file__).resolve().parents[1]
    if root.resolve().is_relative_to(installed):
        _fail('TASK_OUTPUT_INVALID')
    root.mkdir(parents=True, exist_ok=True)
    directory = root / ('sprite-task-' + uuid.uuid4().hex)
    directory.mkdir()
    (directory / 'events').mkdir()
    seal = _digest(sealed)
    manifest = dict(schema='sprite_task_ledger_v1', authorization=authorization, targets=targets, authorization_sha256=seal)
    if historical_import is not None:
        manifest['historical_import'] = sealed['historical_import']
    path = directory / 'task.json'
    new_or_identical(path, encoded(manifest))
    atomic_json(directory / 'head.json', dict(sequence=0, event_sha256=seal, authorization_sha256=seal))
    new_or_identical(directory / '.run.lock', b'0')
    return dict(task_manifest=str(path), task_id=authorization['task_id'], authorization_sha256=seal)


def inspect_task(task_manifest):
    """Read-only replay: never repairs head or uses cached budget numbers."""
    with _locked(task_manifest) as path:
        state, tail, head = _replay(path)
        for strategy in state['strategies'].values():
            strategy['attempts_used'] = len(strategy['attempt_keys'])
        for key, attempt in state['attempts'].items():
            attempt['effective_defects'] = _source_defects(state, key)
            if _regional_attempt(state, attempt):
                attempt['regional_state_sha256'] = regional_state_token(state, attempt['run_id'], attempt['attempt_id'])
            if _endpoint_attempt(state, attempt):
                attempt['experiment_state_sha256'] = _endpoint_token(state, attempt)
        exposure = _exposure(state)
        with localcontext() as context:
            context.prec = 512
            available = Decimal(state['authorization']['cap_usd']) - exposure
        state.update(task_manifest=str(path), sequence=tail['sequence'], recovery_required=tail != head,
                     cap_usd=state['authorization']['cap_usd'], reserved_total_usd=_money(exposure),
                     available_estimate_usd=_money(available), delivery_allowed=False,
                     uncertainty=dict(invoice_verified=False, external_provider_costs='unverified_outside_this_journal',
                                      budget_basis=state['authorization']['budget_basis'],
                                      uncertain_operations=[name for name, operation in {**state['operations'], **state['imports']}.items()
                                                            if operation['status'] in ('uncertain', 'submission_pending')],
                                      unresolved_submissions={target: _unresolved(state, target) for target in state['targets']
                                                              if _unresolved(state, target)}))
        if 'historical_import' in state:
            state.update(historical_attempt_count=len(state['historical_attempts']), history_blocked_targets=_history_blocked_targets(state))
            if state['history_status'] == 'pending':
                state.update(reserved_total_usd=None, available_estimate_usd=None)
        return state


def bind_run(task_manifest, run):
    return _mutate(task_manifest, 'bind_run', dict(run=run))


def begin_attempt(task_manifest, run_id, attempt_id, effective_descriptor, max_attempts=2, correction=None, *,
                  source_attempt=None, strategy_scope='target', endpoint_plan_sha256=None, regional_plan_sha256=None):
    payload = dict(run_id=run_id, attempt_id=attempt_id, effective_descriptor=effective_descriptor,
                   max_attempts=max_attempts, correction=correction, source_attempt=source_attempt)
    if strategy_scope != 'target':
        payload['strategy_scope'] = strategy_scope
    if endpoint_plan_sha256 is not None:
        payload['endpoint_plan_sha256'] = endpoint_plan_sha256
    if regional_plan_sha256 is not None:
        payload['regional_plan_sha256'] = regional_plan_sha256
    return _mutate(task_manifest, 'begin_attempt', payload)


def reserve_plan(task_manifest, run_id, attempt_id, strategy_sha256, operations, *, endpoint_plan=None, regional_plan=None):
    payload = dict(run_id=run_id, attempt_id=attempt_id, strategy_sha256=strategy_sha256, operations=operations)
    if endpoint_plan is not None:
        payload['endpoint_plan'] = endpoint_plan
    if regional_plan is not None:
        payload['regional_plan'] = regional_plan
    return _mutate(task_manifest, 'reserve_plan', payload)


def mark_submission(task_manifest, operation_id, *, expected=None):
    return _mutate(task_manifest, 'mark_submission', dict(operation_id=operation_id, expected=expected))


def bind_operation_config(task_manifest, operation_id, binding):
    return _mutate(task_manifest, 'bind_operation_config', dict(operation_id=operation_id, binding=binding))


def reprice_plan(task_manifest, run_id, attempt_id, operations, *, endpoint_plan=None, regional_plan=None):
    payload = dict(run_id=run_id, attempt_id=attempt_id, operations=operations)
    if endpoint_plan is not None:
        payload['endpoint_plan'] = endpoint_plan
    if regional_plan is not None:
        payload['regional_plan'] = regional_plan
    return _mutate(task_manifest, 'reprice_plan', payload)


def record_regional_gate(task_manifest, run_id, attempt_id, gate):
    return _mutate(task_manifest, 'record_regional_gate', dict(run_id=run_id, attempt_id=attempt_id, gate=gate))


def record_endpoint_gate(task_manifest, run_id, attempt_id, gate):
    return _mutate(task_manifest, 'record_endpoint_gate', dict(run_id=run_id, attempt_id=attempt_id, gate=gate))


def record_outcome(task_manifest, operation_id, outcome):
    return _mutate(task_manifest, 'record_outcome', dict(operation_id=operation_id, outcome=outcome))


def release_unsubmitted(task_manifest, operation_id, evidence):
    return _mutate(task_manifest, 'release_unsubmitted', dict(operation_id=operation_id, evidence=evidence))


def record_review(task_manifest, run_id, attempt_id, decision, defects, evidence):
    return _mutate(task_manifest, 'record_review', dict(run_id=run_id, attempt_id=attempt_id,
                   decision=decision, defects=defects, evidence=evidence))


def import_reservations(task_manifest, entries):
    return _mutate(task_manifest, 'import_reservations', dict(entries=entries))


def import_task_history(task_manifest, bundle):
    """Import only the complete, presealed logical history; never open evidence files."""
    return _mutate(task_manifest, 'import_task_history', dict(bundle=bundle))


def check_history_context(task_manifest, target_id, effective_descriptor=None):
    """Read-only execution gate, not artifact adoption or proof of actual pixels.

    Returns None when history permits this context. Callers must still validate
    task identity/target, authorization, live artifacts and actual provenance.
    Mutations independently repeat their authoritative guards under the lock.
    """
    with _locked(task_manifest) as path:
        state, _, _ = _replay(path)
        target_id = _text(target_id)
        if target_id not in state['targets']:
            _fail('TASK_SOURCE_MISMATCH')
        _history_guard(state, target_id)
        descriptor = (_descriptor(effective_descriptor, state['targets'][target_id])
                      if effective_descriptor is not None else None)
        _history_guard(state, target_id, descriptor)
