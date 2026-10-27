"""Offline behavioral contract: real journals, Decimal arithmetic and OS processes."""
import json
import hashlib
import copy
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
import sys
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import sprite_task_ledger as ledger
except ImportError:
    ledger = None

H = 'a' * 64
Q = 'b' * 64
C = 'c' * 64


def contender(manifest, run, strategy, operation, gate, result):
    import sprite_task_ledger as child
    gate.wait(15)
    try:
        child.reserve_plan(manifest, run, 'a1' if run == 'r1' else 'a2', strategy, [operation])
        result.put('reserved')
    except child.WorkflowError as exc:
        result.put(exc.code)


def reprice_contender(manifest, run, operation, gate, result):
    import sprite_task_ledger as child
    gate.wait(15)
    try:
        child.reprice_plan(manifest, run, 'a1' if run == 'r1' else 'a2', [operation])
        result.put('repriced')
    except child.WorkflowError as exc:
        result.put(exc.code)


def marker_contender(manifest, expected, gate, result):
    import sprite_task_ledger as child
    gate.wait(15)
    try:
        child.mark_submission(manifest, 'op1', expected=expected)
        result.put('marked')
    except child.WorkflowError as exc:
        result.put(exc.code)


def release_pair_contender(manifest, gate, result):
    import sprite_task_ledger as child
    gate.wait(15)
    try:
        child.release_unsubmitted(manifest, 'op1', 'cancelled before the primary marker')
        child.release_unsubmitted(manifest, 'cleanup', 'parent released without starting')
        result.put('released')
    except child.WorkflowError as exc:
        result.put(exc.code)


def release_cleanup_contender(manifest, gate, result):
    import sprite_task_ledger as child
    gate.wait(15)
    try:
        child.release_unsubmitted(manifest, 'cleanup', 'competing cleanup release')
        result.put('cleanup-released')
    except child.WorkflowError as exc:
        result.put(exc.code)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ledger, 'sprite_task_ledger implementation is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='sprite-ledger-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.create()

    def create(self, cap='1', allowed=None, basis='fal_operational_reservations'):
        auth = dict(task_id='human-task', cap_usd=cap, budget_basis=basis,
                    allowed_operations=allowed or list(ledger.OPERATION_KINDS), evidence_ref='user:message:7')
        targets = [dict(target_id=x, source_sha256=H, action='run', orientation='left', requirements={'frames': 8})
                   for x in ('wolf', 'golem')]
        return ledger.create_task(self.root, auth, targets)['task_manifest']

    def run_bind(self, name='r1', target='wolf', parent=None):
        value = dict(run_id=name, target_id=target, scope_sha256=C, manifest_path=f'/data/{name}/run.json')
        if parent:
            value['parent_run_id'] = parent
        return ledger.bind_run(self.manifest, value)

    def descriptor(self, route='sheet', **extras):
        value = dict(route=route, model='minimax/h3-max/image-to-video' if route == 'video' else 'fal-ai/nano-banana-2/edit', conditioning='single_source',
                     inputs=[dict(role='source', sha256=H)])
        if route == 'auto':
            value['resolved_route'] = 'sheet'
        if route in ('body_x_alignment', 'native_motion_construction', 'alpha_edge_cleanup'):
            models = dict(body_x_alignment='body_x_preserve_y_v1', native_motion_construction='quadruped_two_stride',
                          alpha_edge_cleanup='alpha_edge_cleanup')
            value = dict(route=route, model=models[route], conditioning='local',
                         inputs=[dict(role='source', sha256=H)])
        return dict(value, **extras)

    def attempt(self, run='r1', attempt='a1', descriptor=None, limit=2, correction=None, source_attempt=None,
                strategy_scope=None):
        kwargs = {} if source_attempt is None else dict(source_attempt=source_attempt)
        if strategy_scope is not None:
            kwargs['strategy_scope'] = strategy_scope
        return ledger.begin_attempt(self.manifest, run, attempt, descriptor or self.descriptor(), limit, correction, **kwargs)

    def op(self, name='op1', amount='.3', **extras):
        return dict(dict(operation_id=name, kind='provider_animation', config_sha256=C,
                         quote_sha256=Q, amount_usd=amount), **extras)

    def reserve(self, operations=None, run='r1', strategy=None):
        attempt_id = 'a1' if run == 'r1' else ('b1' if run == 'r2' else 'a1')
        return ledger.reserve_plan(self.manifest, run, attempt_id, strategy or self.strategy, operations or [self.op()])

    def prepare(self):
        self.run_bind()
        self.strategy = self.attempt()

    def blocked(self, code, func, *args, **kwargs):
        with self.assertRaises(ledger.WorkflowError) as context:
            func(*args, **kwargs)
        self.assertEqual(code, context.exception.code)

    def test_missing_module_is_real_red(self):
        self.assertEqual('0', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_inspection_reports_replayed_attempt_count(self):
        self.prepare()
        self.assertEqual(1, ledger.inspect_task(self.manifest)['strategies'][self.strategy]['attempts_used'])

    def test_shared_cap_across_runs_and_targets(self):
        self.prepare()
        self.reserve([self.op(amount='.6')])
        self.run_bind('r2', 'golem')
        other = self.attempt('r2', 'b1')
        self.blocked('TASK_BUDGET_EXCEEDED', self.reserve, [self.op('op2', '.5')], 'r2', other)
        self.assertEqual('0.6', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_full_plan_is_atomic_and_exact_decimal_boundary(self):
        self.prepare()
        self.blocked('TASK_BUDGET_EXCEEDED', self.reserve, [self.op('a', '.7'), self.op('b', '.300000000000000001', kind='alpha_edge_cleanup')])
        self.assertEqual({}, ledger.inspect_task(self.manifest)['operations'])
        self.reserve([self.op('a', '.7'), self.op('b', '.3', kind='alpha_edge_cleanup')])
        self.assertEqual('0', ledger.inspect_task(self.manifest)['available_estimate_usd'])

    def test_decimal_objects_are_canonical_and_higher_precision_is_exact(self):
        self.manifest = self.create(Decimal('1.00000000000000000000000000000000000001'))
        self.prepare()
        self.reserve([self.op('exact', Decimal('1.00000000000000000000000000000000000001'))])
        self.assertEqual('0', ledger.inspect_task(self.manifest)['available_estimate_usd'])

    def test_every_money_value_in_event_files_is_a_canonical_string(self):
        self.prepare()
        self.reserve([self.op(amount='.3000')])
        event = json.loads(sorted((Path(self.manifest).parent / 'events').glob('*.json'))[-1].read_text())
        self.assertEqual('0.3', event['payload']['operations'][0]['amount_usd'])

    def test_large_canonical_decimal_replays_and_missing_context_is_stable(self):
        self.manifest = self.create('9' * 120 + 'e100')
        self.assertEqual('9' * 120 + '0' * 100, ledger.inspect_task(self.manifest)['cap_usd'])
        self.blocked('TASK_CONTEXT_REQUIRED', ledger.inspect_task, None)

    def test_malformed_input_has_stable_schema_error_without_writes(self):
        for allowed in ([{}], [True], 'provider_animation'):
            with self.subTest(allowed=allowed):
                self.blocked('TASK_SCHEMA_INVALID', self.create, allowed=allowed)
        self.prepare()
        before = ledger.inspect_task(self.manifest)['sequence']
        self.blocked('TASK_SCHEMA_INVALID', ledger.reserve_plan, self.manifest, [], 'a1', self.strategy, [self.op()])
        self.assertEqual(before, ledger.inspect_task(self.manifest)['sequence'])

    def test_one_frozen_full_plan_per_attempt_cannot_reset_operation_ids(self):
        self.prepare()
        self.reserve([self.op('generation'), self.op('cleanup', '.1', kind='alpha_edge_cleanup')])
        self.blocked('TASK_PLAN_COLLISION', self.reserve, [self.op('renamed-generation')])
        self.blocked('TASK_PLAN_COLLISION', self.reserve, [self.op('generation')])

    def test_reprice_delta_is_atomic_and_never_refunds(self):
        self.assertTrue(callable(getattr(ledger, 'reprice_plan', None)), 'explicit pre-submit repricing is missing')
        self.prepare()
        self.reserve([self.op('generation', '.6'), self.op('cleanup', '.2', kind='alpha_edge_cleanup')])
        self.blocked('TASK_BUDGET_EXCEEDED', ledger.reprice_plan, self.manifest, 'r1', 'a1',
                     [self.op('generation', '.9', quote_sha256='d' * 64), self.op('cleanup', '.2', kind='alpha_edge_cleanup')])
        self.assertEqual('0.8', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        revised = [self.op('generation', '.7', quote_sha256='d' * 64), self.op('cleanup', '.2', kind='alpha_edge_cleanup')]
        ledger.reprice_plan(self.manifest, 'r1', 'a1', revised)
        ledger.reprice_plan(self.manifest, 'r1', 'a1', revised)
        self.assertEqual('0.9', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        lower = [self.op('generation', '.1', quote_sha256='e' * 64), self.op('cleanup', '.2', kind='alpha_edge_cleanup')]
        ledger.reprice_plan(self.manifest, 'r1', 'a1', lower)
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('0.9', state['reserved_total_usd'])
        self.assertEqual(3, len(state['operations']['generation']['quote_history']))
        self.assertEqual('0.1', state['operations']['generation']['current_quote']['amount_usd'])

    def test_reprice_cannot_change_identity_config_kind_or_submitted_price(self):
        self.prepare()
        self.reserve()
        for changed in (self.op('renamed'), self.op(config_sha256=Q), self.op(kind='alpha_edge_cleanup')):
            self.blocked('TASK_PLAN_COLLISION', ledger.reprice_plan, self.manifest, 'r1', 'a1', [changed])
        ledger.mark_submission(self.manifest, 'op1')
        self.blocked('TASK_PLAN_SUBMITTED', ledger.reprice_plan, self.manifest, 'r1', 'a1',
                     [self.op(amount='.4', quote_sha256='d' * 64)])

    def test_reprice_cleanup_held_after_primary_known_id_preserves_primary(self):
        self.prepare()
        primary, cleanup = self.op('primary', '.6'), self.op('cleanup', '.2', kind='alpha_edge_cleanup')
        self.reserve([primary, cleanup])
        ledger.mark_submission(self.manifest, 'primary')
        ledger.record_outcome(self.manifest, 'primary', dict(status='completed', provider_request_id='primary-id', evidence='receipt'))
        ledger.reprice_plan(self.manifest, 'r1', 'a1',
                            [primary, self.op('cleanup', '.3', kind='alpha_edge_cleanup', quote_sha256='d' * 64)])
        self.assertEqual('0.9', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.blocked('TASK_BUDGET_EXCEEDED', ledger.reprice_plan, self.manifest, 'r1', 'a1',
                     [primary, self.op('cleanup', '.5', kind='alpha_edge_cleanup', quote_sha256='e' * 64)])
        self.blocked('TASK_PLAN_SUBMITTED', ledger.reprice_plan, self.manifest, 'r1', 'a1',
                     [self.op('primary', '.5', quote_sha256='f' * 64), self.op('cleanup', '.3', kind='alpha_edge_cleanup', quote_sha256='d' * 64)])

    def test_models_cannot_be_strategy_labels_and_native_profile_must_match(self):
        self.prepare()
        self.blocked('TASK_POLICY_UNSUPPORTED', self.attempt, 'r1', 'fake', self.descriptor(model='new-name'))
        self.blocked('TASK_POLICY_UNSUPPORTED', self.attempt, 'r1', 'fake', self.descriptor('video', model='fal-ai/nano-banana-2/edit'))
        self.blocked('TASK_POLICY_UNSUPPORTED', self.attempt, 'r1', 'fake', self.descriptor('native_motion_construction', model='articulated_biped_walk'))

    def test_third_attempt_requires_new_targeted_plan_not_reworded_evidence(self):
        self.run_bind()
        self.attempt(limit=3)
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['motion: pauses'], 'frame1')
        first = dict(defect='motion: pauses', plan='continuous gait with distinct phases', evidence='frame1')
        self.attempt('r1', 'a2', limit=3, correction=first)
        ledger.record_review(self.manifest, 'r1', 'a2', 'reject', ['motion: pauses'], 'frame2')
        repeated = dict(defect='motion: pauses', plan='CONTINUOUS  gait with distinct phases', evidence='different evidence')
        self.blocked('TASK_CORRECTION_REQUIRED', self.attempt, 'r1', 'a3', limit=3, correction=repeated)
        self.attempt('r1', 'a3', limit=3, correction=dict(defect='motion: pauses', plan='remove idle key pose and use stride transitions', evidence='frame2'))
        self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, 'r1', 'a4', limit=3, correction=first)

    def test_attempt_id_cannot_be_rebound_to_another_run(self):
        self.prepare()
        self.run_bind('r2')
        self.blocked('TASK_STRATEGY_COLLISION', self.attempt, 'r2', 'a1')

    def test_concurrent_reprice_deltas_share_final_balance(self):
        self.prepare()
        self.reserve([self.op('race1', '.3')])
        self.run_bind('r2', 'golem')
        strategy = self.attempt('r2', 'a2')
        ledger.reserve_plan(self.manifest, 'r2', 'a2', strategy, [self.op('race2', '.3')])
        context = multiprocessing.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        children = [context.Process(target=reprice_contender,
                    args=(self.manifest, run, self.op(name, '.6', quote_sha256='d' * 64), gate, queue))
                    for run, name in [('r1', 'race1'), ('r2', 'race2')]]
        for child in children:
            child.start()
        gate.set()
        results = [queue.get(timeout=25) for _ in children]
        for child in children:
            child.join(25)
            self.assertEqual(0, child.exitcode)
        self.assertCountEqual(['repriced', 'TASK_BUDGET_EXCEEDED'], results)
        self.assertEqual('0.9', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def deferred(self, **changes):
        plan = dict(schema='sprite_deferred_cleanup_v1', primary_operation_id='op1', primary_config_sha256=C,
                    model='fal-ai/ideogram/remove-background', input=dict(sync_mode=False), reserved_units=1,
                    source_from='primary_raw_png', source_limits=dict(mime='image/png', max_bytes=10485760,
                                                                       max_width=4096, max_height=4096))
        return dict(plan, **changes)

    def deferred_op(self, plan=None):
        return dict(operation_id='cleanup', kind='alpha_edge_cleanup', deferred_config=plan or self.deferred(),
                    quote_sha256=Q, amount_usd='.1')

    def binding(self, deferred=None, **changes):
        value = dict(schema='sprite_execution_binding_v1', config_sha256=C if deferred is None else 'd' * 64,
                     config_file_sha256='e' * 64, source_sha256=H, provenance_sha256='f' * 64,
                     deferred_plan_sha256=deferred)
        return dict(value, **changes)

    def test_deferred_plan_reserves_without_inventing_config_digest(self):
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('0.4', state['reserved_total_usd'])
        self.assertNotIn('config_sha256', state['operations']['cleanup']['reservation'])
        self.assertEqual(self.deferred(), state['operations']['cleanup']['deferred_config'])
        self.blocked('TASK_OPERATION_NOT_MATERIALIZED', ledger.mark_submission, self.manifest, 'cleanup')

    def test_deferred_plan_rejects_wrong_model_parent_and_source_route(self):
        self.prepare()
        for plan in (self.deferred(model='new-provider'), self.deferred(primary_operation_id='missing'),
                     self.deferred(primary_config_sha256=Q), self.deferred(source_from='video_sample_grid_v1'),
                     self.deferred(reserved_units=True), self.deferred(input=dict(sync_mode=True))):
            with self.subTest(plan=plan):
                self.blocked('TASK_PLAN_INVALID', self.reserve, [self.op(), self.deferred_op(plan)])
        for change in (dict(config_sha256=C), dict(provider_request_id='import-id')):
            self.blocked('TASK_SCHEMA_INVALID', self.reserve, [self.op(), dict(self.deferred_op(), **change)])
        self.assertEqual('0', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_materialization_is_immutable_and_cleanup_requires_completed_known_parent(self):
        self.assertTrue(callable(getattr(ledger, 'bind_operation_config', None)), 'materialization API missing')
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        plan_sha = hashlib.sha256(ledger.encoded(self.deferred())).hexdigest()
        cleanup_binding = self.binding(plan_sha)
        self.blocked('TASK_PARENT_OPERATION_INCOMPLETE', ledger.bind_operation_config, self.manifest, 'cleanup', cleanup_binding)
        bound = ledger.bind_operation_config(self.manifest, 'op1', self.binding())
        ledger.bind_operation_config(self.manifest, 'op1', self.binding())
        self.blocked('TASK_OPERATION_COLLISION', ledger.bind_operation_config, self.manifest, 'op1', self.binding(source_sha256=Q))
        self.assertEqual(64, len(bound['binding_sha256']))
        ledger.mark_submission(self.manifest, 'op1')
        ledger.record_outcome(self.manifest, 'op1', dict(status='accepted', provider_request_id='p1', evidence='receipt'))
        self.blocked('TASK_PARENT_OPERATION_INCOMPLETE', ledger.bind_operation_config, self.manifest, 'cleanup', cleanup_binding)
        ledger.record_outcome(self.manifest, 'op1', dict(status='completed', provider_request_id='p1', evidence='raw'))
        ledger.bind_operation_config(self.manifest, 'cleanup', cleanup_binding)
        self.blocked('TASK_OPERATION_COLLISION', ledger.bind_operation_config, self.manifest, 'cleanup', self.binding(plan_sha, config_sha256=Q))
        self.assertEqual('0.4', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_expected_marker_binds_exact_config_quote_amount_atomically(self):
        self.assertTrue(callable(getattr(ledger, 'bind_operation_config', None)))
        self.prepare()
        self.reserve()
        before = ledger.inspect_task(self.manifest)['sequence']
        expected = dict(config_sha256=C, binding_sha256=H, quote_sha256=Q, amount_usd='.3')
        self.blocked('TASK_OPERATION_NOT_MATERIALIZED', ledger.mark_submission, self.manifest, 'op1', expected=expected)
        bound = ledger.bind_operation_config(self.manifest, 'op1', self.binding())
        expected['binding_sha256'] = bound['binding_sha256']
        for changed in (dict(config_sha256=Q), dict(binding_sha256=H), dict(quote_sha256=H), dict(amount_usd='.4')):
            self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH', ledger.mark_submission, self.manifest, 'op1', expected=dict(expected, **changed))
        self.assertFalse(ledger.inspect_task(self.manifest)['operations']['op1']['submission_started'])
        self.assertTrue(ledger.mark_submission(self.manifest, 'op1', expected=expected)['submit_allowed'])
        ledger.record_outcome(self.manifest, 'op1', dict(status='completed', provider_request_id='p1', evidence='receipt'))
        stale = dict(expected, quote_sha256=H, amount_usd='.01')
        self.assertFalse(ledger.mark_submission(self.manifest, 'op1', expected=stale)['submit_allowed'])
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH', ledger.mark_submission, self.manifest, 'op1',
                     expected=dict(stale, config_sha256=Q))

    def test_deferred_reprice_keeps_commitment_after_materializing(self):
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        original = ledger.inspect_task(self.manifest)['operations']['cleanup']['reservation']
        revised = dict(self.deferred_op(), quote_sha256='d' * 64, amount_usd='.2')
        ledger.reprice_plan(self.manifest, 'r1', 'a1', [self.op(), revised])
        self.assertEqual(original, ledger.inspect_task(self.manifest)['operations']['cleanup']['reservation'])
        self.assertEqual('0.5', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_reprice_racing_expected_marker_has_one_atomic_winner(self):
        self.assertTrue(callable(getattr(ledger, 'bind_operation_config', None)))
        self.prepare()
        self.reserve()
        bound = ledger.bind_operation_config(self.manifest, 'op1', self.binding())
        expected = dict(config_sha256=C, binding_sha256=bound['binding_sha256'], quote_sha256=Q, amount_usd='.3')
        context = multiprocessing.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        children = [context.Process(target=marker_contender, args=(self.manifest, expected, gate, queue)),
                    context.Process(target=reprice_contender, args=(self.manifest, 'r1', self.op(amount='.4', quote_sha256='d' * 64), gate, queue))]
        for child in children:
            child.start()
        gate.set()
        results = [queue.get(timeout=25) for _ in children]
        for child in children:
            child.join(25)
            self.assertEqual(0, child.exitcode)
        self.assertTrue(sorted(results) in (sorted(['marked', 'TASK_PLAN_SUBMITTED']),
                                           sorted(['repriced', 'TASK_SUBMISSION_CONTEXT_MISMATCH'])), results)

    def test_deferred_cleanup_release_cannot_remove_held_or_inflight_primary_coverage(self):
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        self.blocked('TASK_OPERATION_NOT_RELEASABLE', ledger.release_unsubmitted,
                     self.manifest, 'cleanup', 'would remove required coverage before primary')
        ledger.mark_submission(self.manifest, 'op1')
        self.blocked('TASK_OPERATION_NOT_RELEASABLE', ledger.release_unsubmitted,
                     self.manifest, 'cleanup', 'primary marker is in flight')
        ledger.record_outcome(self.manifest, 'op1', dict(status='pending', provider_request_id='known-pending', evidence='queued'))
        self.blocked('TASK_OPERATION_NOT_RELEASABLE', ledger.release_unsubmitted,
                     self.manifest, 'cleanup', 'known pending is not terminal')
        self.assertEqual('0.4', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_deferred_pair_cancelled_before_marker_cannot_submit_primary(self):
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        ledger.release_unsubmitted(self.manifest, 'op1', 'never started')
        ledger.release_unsubmitted(self.manifest, 'cleanup', 'parent released without starting')
        self.blocked('TASK_SUBMISSION_UNRESOLVED', ledger.mark_submission, self.manifest, 'op1')
        self.assertEqual('0', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_deferred_cleanup_release_requires_known_terminal_and_retains_parent_cost(self):
        for terminal in ('completed', 'failed', 'cancelled', 'rejected'):
            with self.subTest(terminal=terminal):
                self.manifest = self.create()
                self.prepare()
                self.reserve([self.op(), self.deferred_op()])
                ledger.mark_submission(self.manifest, 'op1')
                ledger.record_outcome(self.manifest, 'op1', dict(status=terminal, evidence='terminal without ID remains uncertain'))
                self.blocked('TASK_OPERATION_NOT_RELEASABLE', ledger.release_unsubmitted,
                             self.manifest, 'cleanup', 'absence of ID is not terminal certainty')
                ledger.record_outcome(self.manifest, 'op1', dict(status=terminal, provider_request_id=terminal, evidence='known terminal receipt'))
                ledger.release_unsubmitted(self.manifest, 'cleanup', 'validated assembly/alpha or terminal failure evidence')
                self.assertEqual('0.3', ledger.inspect_task(self.manifest)['reserved_total_usd'])
                self.assertFalse(ledger.mark_submission(self.manifest, 'op1')['submit_allowed'])

    def test_real_process_marker_and_pair_release_cannot_both_succeed(self):
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        bound = ledger.bind_operation_config(self.manifest, 'op1', self.binding())
        expected = dict(config_sha256=C, binding_sha256=bound['binding_sha256'], quote_sha256=Q, amount_usd='.3')
        context = multiprocessing.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        children = [context.Process(target=marker_contender, args=(self.manifest, expected, gate, queue)),
                    context.Process(target=release_pair_contender, args=(self.manifest, gate, queue))]
        for child in children:
            child.start()
        gate.set()
        results = [queue.get(timeout=25) for _ in children]
        for child in children:
            child.join(25)
            self.assertEqual(0, child.exitcode)
        self.assertTrue(sorted(results) in (sorted(['marked', 'TASK_OPERATION_NOT_RELEASABLE']),
                                           sorted(['released', 'TASK_SUBMISSION_UNRESOLVED'])), results)
        state = ledger.inspect_task(self.manifest)
        if 'marked' in results:
            self.assertEqual('held', state['operations']['cleanup']['status'])
            self.assertEqual('0.4', state['reserved_total_usd'])
        else:
            self.assertEqual('released', state['operations']['cleanup']['status'])
            self.assertEqual('0', state['reserved_total_usd'])

    def test_real_process_cleanup_release_cannot_race_past_primary_marker(self):
        self.prepare()
        self.reserve([self.op(), self.deferred_op()])
        bound = ledger.bind_operation_config(self.manifest, 'op1', self.binding())
        expected = dict(config_sha256=C, binding_sha256=bound['binding_sha256'], quote_sha256=Q, amount_usd='.3')
        context = multiprocessing.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        children = [context.Process(target=marker_contender, args=(self.manifest, expected, gate, queue)),
                    context.Process(target=release_cleanup_contender, args=(self.manifest, gate, queue))]
        for child in children:
            child.start()
        gate.set()
        results = [queue.get(timeout=25) for _ in children]
        for child in children:
            child.join(25)
            self.assertEqual(0, child.exitcode)
        self.assertCountEqual(['marked', 'TASK_OPERATION_NOT_RELEASABLE'], results)
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('held', state['operations']['cleanup']['status'])
        self.assertEqual('0.4', state['reserved_total_usd'])

    def test_money_rejects_nonfinite_bool_float_negative(self):
        for value in (True, False, 'NaN', 'Infinity', '-.1', 0.1):
            with self.subTest(value=value):
                self.blocked('TASK_SCHEMA_INVALID', self.create, value)

    def test_idempotence_and_operation_collision(self):
        self.prepare()
        self.reserve()
        self.reserve()
        self.assertEqual('0.3', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.blocked('TASK_OPERATION_COLLISION', self.reserve, [self.op(amount='.4')])
        self.blocked('TASK_SCOPE_COLLISION', ledger.bind_run, self.manifest,
                     dict(run_id='r1', target_id='wolf', scope_sha256=Q, manifest_path='/elsewhere'))

    def test_unknown_submission_holds_money_and_blocks_replacement(self):
        self.prepare()
        self.reserve()
        ledger.mark_submission(self.manifest, 'op1')
        self.blocked('TASK_SUBMISSION_UNRESOLVED', ledger.mark_submission, self.manifest, 'op1')
        self.run_bind('r2')
        self.blocked('TASK_SUBMISSION_UNRESOLVED', self.attempt, 'r2', 'next')
        self.blocked('TASK_OPERATION_NOT_RELEASABLE', ledger.release_unsubmitted, self.manifest, 'op1', 'no file')
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('0.3', state['reserved_total_usd'])
        self.assertTrue(state['uncertainty']['unresolved_submissions'])

    def test_known_request_id_continues_without_submit_or_reserve(self):
        self.prepare()
        self.reserve()
        ledger.mark_submission(self.manifest, 'op1')
        outcome = dict(status='accepted', provider_request_id='req-1', evidence='provider receipt')
        ledger.record_outcome(self.manifest, 'op1', outcome)
        ledger.record_outcome(self.manifest, 'op1', outcome)
        result = ledger.mark_submission(self.manifest, 'op1')
        self.assertFalse(result['submit_allowed'])
        self.assertEqual('req-1', result['provider_request_id'])
        self.assertEqual('0.3', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_release_only_never_started_and_failed_cancelled_retained(self):
        self.prepare()
        self.reserve([self.op('free', '.1', kind='alpha_edge_cleanup'), self.op('failed', '.2')])
        ledger.release_unsubmitted(self.manifest, 'free', 'transport never invoked')
        ledger.mark_submission(self.manifest, 'failed')
        ledger.record_outcome(self.manifest, 'failed', dict(status='failed', provider_request_id='failed', evidence='receipt'))
        self.run_bind('r2', 'golem')
        strategy = self.attempt('r2', 'b1')
        self.reserve([self.op('cancelled', '.3')], 'r2', strategy)
        ledger.mark_submission(self.manifest, 'cancelled')
        ledger.record_outcome(self.manifest, 'cancelled', dict(status='cancelled', provider_request_id='cancelled', evidence='receipt'))
        for name in ('failed', 'cancelled'):
            self.blocked('TASK_OPERATION_NOT_RELEASABLE', ledger.release_unsubmitted, self.manifest, name, 'failed')
        self.assertEqual('0.5', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_duplicate_request_ids_and_conflicting_outcomes_block(self):
        self.prepare()
        self.reserve([self.op('first'), self.op('second', kind='alpha_edge_cleanup')])
        ledger.mark_submission(self.manifest, 'first')
        ledger.record_outcome(self.manifest, 'first', dict(status='completed', provider_request_id='same', evidence='done'))
        ledger.mark_submission(self.manifest, 'second')
        self.blocked('TASK_OPERATION_COLLISION', ledger.record_outcome, self.manifest, 'second',
                     dict(status='completed', provider_request_id='same', evidence='duplicate'))
        self.blocked('TASK_OUTCOME_CONFLICT', ledger.record_outcome, self.manifest, 'first',
                     dict(status='failed', provider_request_id='same', evidence='changed'))

    def test_strategy_normalizes_auto_prompt_seed_paths_and_uuid(self):
        self.prepare()
        self.run_bind('new-uuid')
        self.assertEqual(self.strategy, self.attempt('new-uuid', 'other',
                         self.descriptor('auto', prompt='different', seed=42, path='/different', label='novel')))
        self.blocked('TASK_STRATEGY_COLLISION', self.attempt, limit=3)

    def test_retry_limit_and_semantic_correction_persist_across_runs(self):
        self.prepare()
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['motion: returns to rest'], 'frames:2-5')
        self.run_bind('r2')
        self.blocked('TASK_CORRECTION_REQUIRED', self.attempt, 'r2', 'a2')
        correction = dict(defect='motion: returns to rest', plan='enforce continuous forward gait', evidence='frames:2-5')
        self.attempt('r2', 'a2', correction=correction)
        ledger.record_review(self.manifest, 'r2', 'a2', 'reject', ['motion: returns to rest'], 'new-frames')
        self.run_bind('r3')
        self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, 'r3', 'a3', correction=correction)
        self.blocked('TASK_REVIEW_CONFLICT', ledger.record_review, self.manifest, 'r1', 'a1', 'pass', [], 'changed mind')

    def test_rejected_attempt_cannot_start_unsubmitted_remainder(self):
        self.prepare()
        self.reserve()
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['motion: pauses'], 'frame3')
        self.blocked('TASK_ATTEMPT_CLOSED', ledger.mark_submission, self.manifest, 'op1')
        ledger.release_unsubmitted(self.manifest, 'op1', 'journal proves no transport marker')
        self.assertEqual('0', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_identical_correction_cannot_buy_third_attempt(self):
        self.prepare()
        # The predeclared count is immutable, including across runs.
        self.blocked('TASK_STRATEGY_COLLISION', self.attempt, 'r1', 'a2', limit=3)
        correction = dict(defect='bad motion', plan='fix motion', evidence='frame3')
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['bad motion'], 'frame3')
        self.attempt('r1', 'a2', correction=correction)
        ledger.record_review(self.manifest, 'r1', 'a2', 'reject', ['bad motion'], 'frame3')
        self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, 'r1', 'a3', correction=correction)

    def test_only_x_derivation_costs_zero_and_preserves_source_defects(self):
        self.prepare()
        self.reserve()
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['identity: wrong head', 'scale: shrinks'], 'sheet1')
        self.run_bind('child', parent='r1')
        self.attempt('child', 'align', self.descriptor('body_x_alignment'))
        self.blocked('TASK_SOURCE_DEFECT_UNRESOLVED', ledger.record_review, self.manifest,
                     'child', 'align', 'pass', [], 'X proof')
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('0.3', state['reserved_total_usd'])
        self.assertEqual('r1', state['runs']['child']['parent_run_id'])
        self.assertEqual(H, state['targets']['wolf']['source_sha256'])

    def test_unauthorized_local_motion_and_unsupported_conditioning_fail_closed(self):
        self.manifest = self.create(allowed=['alpha_edge_cleanup'])
        self.run_bind()
        self.blocked('OPERATION_NOT_AUTHORIZED', self.attempt,
                     descriptor=self.descriptor('native_motion_construction'))
        self.manifest = self.create()
        self.run_bind()
        self.blocked('TASK_POLICY_UNSUPPORTED', self.attempt, descriptor=self.descriptor(conditioning='A_to_B'))
        self.blocked('TASK_SOURCE_MISMATCH', self.attempt,
                     descriptor=self.descriptor(inputs=[dict(role='source', sha256=Q)]))

    def test_local_strategy_cannot_reserve_provider_animation_even_when_task_allows_it(self):
        self.run_bind()
        for route in ('body_x_alignment', 'alpha_edge_cleanup', 'native_motion_construction'):
            with self.subTest(route=route):
                strategy = self.attempt('r1', route, self.descriptor(route))
                self.blocked('TASK_PLAN_INVALID', ledger.reserve_plan, self.manifest, 'r1', route, strategy, [self.op(route)])
        self.assertEqual({}, ledger.inspect_task(self.manifest)['operations'])

    def test_single_attempt_cannot_submit_two_generation_primaries_or_duplicate_cleanup(self):
        self.run_bind()
        strategy = self.attempt(limit=1)
        for operations in ([self.op('primary1'), self.op('primary2')],
                           [self.op('primary'), self.op('clean1', '.1', kind='alpha_edge_cleanup'),
                            self.op('clean2', '.1', kind='alpha_edge_cleanup')],
                           [self.op('cleanup', '.1', kind='alpha_edge_cleanup')]):
            with self.subTest(operations=operations):
                self.blocked('TASK_PLAN_INVALID', ledger.reserve_plan, self.manifest, 'r1', 'a1', strategy, operations)
        self.assertEqual('0', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.assertEqual(1, ledger.inspect_task(self.manifest)['strategies'][strategy]['attempts_used'])

    def test_import_is_atomic_deduplicated_and_conservatively_uncertain(self):
        def entry(name, amount='.3', request=None):
            return dict(import_id=name, target_id='wolf', kind='provider_animation', source_sha256=H,
                        config_sha256=C, evidence_sha256=Q, amount_usd=amount,
                        provider_request_id=request, uncertain=request is None)
        values = [entry('old1'), entry('old2', '.2', 'request2')]
        ledger.import_reservations(self.manifest, values)
        ledger.import_reservations(self.manifest, values)
        self.assertEqual('0.5', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.blocked('TASK_OPERATION_COLLISION', ledger.import_reservations, self.manifest, [entry('other', '.2', 'request2')])
        self.blocked('TASK_OPERATION_COLLISION', ledger.import_reservations, self.manifest, [entry('old1', '.4')])
        self.blocked('TASK_BUDGET_EXCEEDED', ledger.import_reservations, self.manifest, [entry('new', '.6'), entry('tiny', '.1')])
        self.assertEqual(2, len(ledger.inspect_task(self.manifest)['imports']))
        self.run_bind()
        self.blocked('TASK_SUBMISSION_UNRESOLVED', self.attempt)

    def test_uncertain_import_can_resolve_by_id_without_new_cost_or_submit(self):
        entry = dict(import_id='lost', target_id='wolf', kind='provider_animation', source_sha256=H,
                     config_sha256=C, evidence_sha256=Q, amount_usd='.4', provider_request_id=None, uncertain=True)
        ledger.import_reservations(self.manifest, [entry])
        ledger.record_outcome(self.manifest, 'lost', dict(status='completed', provider_request_id='recovered-id', evidence='transport history'))
        self.assertFalse(ledger.mark_submission(self.manifest, 'lost')['submit_allowed'])
        ledger.import_reservations(self.manifest, [entry])
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('0.4', state['reserved_total_usd'])
        self.assertEqual({}, state['uncertainty']['unresolved_submissions'])
        self.run_bind()
        self.attempt()

    def test_inspect_does_not_recreate_or_fill_a_damaged_lock_file(self):
        lock = Path(self.manifest).parent / '.run.lock'
        lock.write_bytes(b'')
        self.blocked('TASK_JOURNAL_CORRUPT', ledger.inspect_task, self.manifest)
        self.assertEqual(b'', lock.read_bytes())
        lock.unlink()
        self.blocked('TASK_JOURNAL_CORRUPT', ledger.inspect_task, self.manifest)
        self.assertFalse(lock.exists())

    def test_body_only_review_cannot_clear_source_defects_in_same_run(self):
        self.prepare()
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['anatomy: missing leg'], 'frame4')
        self.attempt('r1', 'align', self.descriptor('body_x_alignment'))
        self.blocked('TASK_SOURCE_DEFECT_UNRESOLVED', ledger.record_review, self.manifest,
                     'r1', 'align', 'pass', [], 'X proof')

    def corrected_generation(self):
        self.prepare()
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['identity: wrong head'], 'raw-a')
        correction = dict(defect='identity: wrong head', plan='restore source head shape in corrected generation', evidence='raw-a')
        self.attempt('r1', 'a2', correction=correction)
        ledger.record_review(self.manifest, 'r1', 'a2', 'pass', [], 'corrected-raw-b')

    def test_local_review_of_exact_corrected_generation_can_pass_without_erasing_reject(self):
        self.corrected_generation()
        source = dict(run_id='r1', attempt_id='a2')
        self.attempt('r1', 'align', self.descriptor('body_x_alignment'), source_attempt=source)
        ledger.record_review(self.manifest, 'r1', 'align', 'pass', [], 'aligned-b')
        attempts = {value['attempt_id']: value for value in ledger.inspect_task(self.manifest)['attempts'].values()}
        self.assertEqual('reject', attempts['a1']['review']['decision'])
        self.assertEqual(source, attempts['align']['source_attempt'])
        self.assertEqual('pass', attempts['align']['review']['decision'])

    def test_children_and_local_chains_use_exact_source_not_unrelated_newer_pass(self):
        self.corrected_generation()
        for child, original in [('bad-child', 'a1'), ('good-child', 'a2')]:
            self.run_bind(child, parent='r1')
            self.attempt(child, child, self.descriptor('body_x_alignment'), limit=3,
                         source_attempt=dict(run_id='r1', attempt_id=original))
        self.blocked('TASK_SOURCE_DEFECT_UNRESOLVED', ledger.record_review, self.manifest,
                     'bad-child', 'bad-child', 'pass', [], 'still raw-a')
        ledger.record_review(self.manifest, 'good-child', 'good-child', 'pass', [], 'aligned raw-b')
        self.run_bind('grandchild', parent='good-child')
        self.attempt('grandchild', 'grandchild', self.descriptor('body_x_alignment'), limit=3,
                     source_attempt=dict(run_id='good-child', attempt_id='good-child'))
        ledger.record_review(self.manifest, 'grandchild', 'grandchild', 'pass', [], 'aligned good child')
        self.run_bind('bad-grandchild', parent='bad-child')
        self.attempt('bad-grandchild', 'bad-grandchild', self.descriptor('alpha_edge_cleanup'),
                     source_attempt=dict(run_id='bad-child', attempt_id='bad-child'))
        self.blocked('TASK_SOURCE_DEFECT_UNRESOLVED', ledger.record_review, self.manifest,
                     'bad-grandchild', 'bad-grandchild', 'pass', [], 'edge cleanup cannot fix identity')

    def test_local_source_binding_is_immutable_and_requires_existing_same_lineage_attempt(self):
        self.corrected_generation()
        self.run_bind('other')
        self.attempt('other', 'other', self.descriptor('video'))
        self.run_bind('child', parent='r1')
        for source in (dict(run_id='r1', attempt_id='missing'), dict(run_id='other', attempt_id='other')):
            self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH', self.attempt, 'child', 'align',
                         self.descriptor('body_x_alignment'), source_attempt=source)
        self.attempt('child', 'align', self.descriptor('body_x_alignment'), source_attempt=dict(run_id='r1', attempt_id='a1'))
        self.blocked('TASK_STRATEGY_COLLISION', self.attempt, 'child', 'align',
                     self.descriptor('body_x_alignment'), source_attempt=dict(run_id='r1', attempt_id='a2'))
        self.blocked('TASK_POLICY_UNSUPPORTED', self.attempt, 'child', 'provider-new',
                     source_attempt=dict(run_id='r1', attempt_id='a2'))

    def test_lineage_review_revocation_preserves_history_and_exact_repeat(self):
        self.prepare()
        passed = dict(decision='pass', defects=[], evidence='original receipt')
        rejected = dict(decision='reject', defects=['identity: wrong head'], evidence='later receipt')
        self.assertEqual(passed, ledger.record_review(self.manifest, 'r1', 'a1', **passed))
        directory = Path(self.manifest).parent
        original_events = {p.name: p.read_bytes() for p in (directory / 'events').iterdir()}
        sequence = ledger.inspect_task(self.manifest)['sequence']
        self.assertEqual(passed, ledger.record_review(self.manifest, 'r1', 'a1', **passed))
        self.assertEqual(sequence, ledger.inspect_task(self.manifest)['sequence'])
        self.blocked('TASK_REVIEW_CONFLICT', ledger.record_review, self.manifest, 'r1', 'a1', 'pass', [], 'new notes')
        self.assertEqual(rejected, ledger.record_review(self.manifest, 'r1', 'a1',
                         'reject', [' identity: wrong head '], ' later receipt '))
        state = ledger.inspect_task(self.manifest)
        attempt = next(iter(state['attempts'].values()))
        self.assertEqual([passed, rejected], attempt['review_history'])
        self.assertEqual(rejected, attempt['review'])
        self.assertEqual(['identity: wrong head'], attempt['effective_defects'])
        for name, content in original_events.items():
            self.assertEqual(content, (directory / 'events' / name).read_bytes())
        self.assertEqual(rejected, ledger.record_review(self.manifest, 'r1', 'a1', **rejected))
        for decision, defects, evidence in [('pass', [], 'original receipt'),
                                             ('reject', ['scale: shrinks'], 'third receipt')]:
            self.blocked('TASK_REVIEW_CONFLICT', ledger.record_review, self.manifest,
                         'r1', 'a1', decision, defects, evidence)
        self.assertEqual(sequence + 1, ledger.inspect_task(self.manifest)['sequence'])

    def test_lineage_historical_single_review_replays_without_writes(self):
        self.prepare()
        review = dict(decision='reject', defects=['motion: pauses'], evidence='legacy receipt')
        ledger.record_review(self.manifest, 'r1', 'a1', **review)
        directory = Path(self.manifest).parent
        before = {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        attempt = next(iter(ledger.inspect_task(self.manifest)['attempts'].values()))
        self.assertEqual([review], attempt['review_history'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in directory.rglob('*') if p.is_file()})

    def test_lineage_empty_defects_without_review_do_not_create_a_pass(self):
        self.prepare()
        attempt = next(iter(ledger.inspect_task(self.manifest)['attempts'].values()))
        self.assertEqual([], attempt['effective_defects'])
        self.assertEqual([], attempt['review_history'])
        self.assertNotIn('review', attempt)
        self.assertFalse(ledger.inspect_task(self.manifest)['delivery_allowed'])

    def test_lineage_revocation_orphan_recovers_head_without_duplicate_history(self):
        self.prepare()
        ledger.record_review(self.manifest, 'r1', 'a1', 'pass', [], 'pass receipt')
        head = Path(self.manifest).parent / 'head.json'
        old_head = head.read_bytes()
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['motion: pauses'], 'reject receipt')
        final_head = head.read_bytes()
        head.write_bytes(old_head)
        state = ledger.inspect_task(self.manifest)
        self.assertTrue(state['recovery_required'])
        self.assertEqual('reject', next(iter(state['attempts'].values()))['review']['decision'])
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['motion: pauses'], 'reject receipt')
        self.assertEqual(final_head, head.read_bytes())
        self.assertEqual(2, len(next(iter(ledger.inspect_task(self.manifest)['attempts'].values()))['review_history']))

    def test_lineage_live_defects_project_exact_source_and_local_repairs(self):
        self.prepare()
        ledger.record_review(self.manifest, 'r1', 'a1', 'pass', [], 'source pass')
        self.attempt('r1', 'alignment', self.descriptor('body_x_alignment'),
                     source_attempt=dict(run_id='r1', attempt_id='a1'))
        ledger.record_review(self.manifest, 'r1', 'alignment', 'pass', [], 'alignment pass')
        self.attempt('r1', 'cleanup', self.descriptor('alpha_edge_cleanup'),
                     source_attempt=dict(run_id='r1', attempt_id='alignment'))
        ledger.record_review(self.manifest, 'r1', 'cleanup', 'pass', [], 'cleanup pass')
        self.attempt('r1', 'independent')
        ledger.record_review(self.manifest, 'r1', 'independent', 'pass', [], 'other generation')
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject',
                             ['position_x: drift', 'alpha_edge: halo', 'identity: wrong head'], 'late source reject')
        attempts = {a['attempt_id']: a for a in ledger.inspect_task(self.manifest)['attempts'].values()}
        self.assertEqual(['alpha_edge: halo', 'identity: wrong head'], attempts['alignment']['effective_defects'])
        self.assertEqual(['identity: wrong head'], attempts['cleanup']['effective_defects'])
        self.assertEqual([], attempts['independent']['effective_defects'])
        self.assertEqual('pass', attempts['cleanup']['review']['decision'])
        ledger.record_review(self.manifest, 'r1', 'alignment', 'reject', ['position_x: own drift'], 'own rejection')
        attempts = {a['attempt_id']: a for a in ledger.inspect_task(self.manifest)['attempts'].values()}
        self.assertEqual(['position_x: own drift', 'position_x: drift', 'alpha_edge: halo', 'identity: wrong head'],
                         attempts['alignment']['effective_defects'])
        self.assertEqual(['position_x: own drift', 'position_x: drift', 'identity: wrong head'],
                         attempts['cleanup']['effective_defects'])

    def test_alignment_scope_legacy_event_and_fingerprint_remain_unchanged(self):
        self.run_bind()
        descriptor = self.descriptor('body_x_alignment')
        strategy = self.attempt(descriptor=descriptor)
        self.assertEqual('5e4af02eee259e0317f5a2d3f469b2f2881105e90c005a195cfbaf6dc3f457a8', strategy)
        event_path = sorted((Path(self.manifest).parent / 'events').iterdir())[-1]
        before = event_path.read_bytes()
        payload = json.loads(before)['payload']
        self.assertEqual(dict(run_id='r1', attempt_id='a1', effective_descriptor=descriptor, max_attempts=2,
                              correction=None, source_attempt=None), payload)
        self.assertEqual(strategy, self.attempt(descriptor=descriptor, strategy_scope='target'))
        self.assertEqual(before, event_path.read_bytes())
        state = ledger.inspect_task(self.manifest)
        self.assertEqual(2, state['sequence'])
        self.assertNotIn('strategy_scope', state['strategies'][strategy])
        self.assertNotIn('source_group', state['strategies'][strategy])

    def test_alignment_scope_shared_chain_quota_and_correction_keep_immediate_source(self):
        self.prepare()
        original = dict(run_id='r1', attempt_id='a1')
        self.run_bind('child', parent='r1')
        strategy = self.attempt('child', 'align1', self.descriptor('body_x_alignment'),
                                source_attempt=original, strategy_scope='alignment_source_v1')
        ledger.record_review(self.manifest, 'child', 'align1', 'reject', ['position_x: drift'], 'frame2')
        self.run_bind('grandchild', parent='child')
        immediate = dict(run_id='child', attempt_id='align1')
        self.blocked('TASK_CORRECTION_REQUIRED', self.attempt, 'grandchild', 'align2',
                     self.descriptor('body_x_alignment'), source_attempt=immediate, strategy_scope='alignment_source_v1')
        correction = dict(defect='position_x: drift', plan='use torso anchor rather than weapon', evidence='frame2')
        self.assertEqual(strategy, self.attempt('grandchild', 'align2', self.descriptor('body_x_alignment'),
                         correction=correction, source_attempt=immediate, strategy_scope='alignment_source_v1'))
        self.run_bind('third-folder', parent='grandchild')
        self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, 'third-folder', 'align3',
                     self.descriptor('body_x_alignment'), correction=correction,
                     source_attempt=dict(run_id='grandchild', attempt_id='align2'), strategy_scope='alignment_source_v1')
        state = ledger.inspect_task(self.manifest)
        self.assertEqual(original, state['strategies'][strategy]['source_group'])
        self.assertEqual('alignment_source_v1', state['strategies'][strategy]['strategy_scope'])
        self.assertEqual(2, state['strategies'][strategy]['attempts_used'])
        attempts = {a['attempt_id']: a for a in state['attempts'].values()}
        self.assertEqual(immediate, attempts['align2']['source_attempt'])
        self.assertEqual(['position_x: drift'], attempts['align2']['effective_defects'])
        self.assertEqual('0', state['reserved_total_usd'])
        self.assertEqual({}, state['operations'])

    def test_alignment_scope_real_generation_attempts_get_independent_groups(self):
        self.prepare()
        self.attempt(attempt='a2')
        strategies = []
        for source_id in ('a1', 'a2'):
            source = dict(run_id='r1', attempt_id=source_id)
            strategy = self.attempt(attempt='align-' + source_id, descriptor=self.descriptor('body_x_alignment'),
                                    limit=1, source_attempt=source, strategy_scope='alignment_source_v1')
            expected = hashlib.sha256((json.dumps(dict(target_id='wolf', effective_descriptor=self.descriptor('body_x_alignment'),
                       strategy_scope='alignment_source_v1', source_group=source), sort_keys=True,
                       ensure_ascii=False, indent=2) + '\n').encode('utf-8')).hexdigest()
            self.assertEqual(expected, strategy)
            self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, attempt='again-' + source_id,
                         descriptor=self.descriptor('body_x_alignment'), limit=1, source_attempt=source,
                         strategy_scope='alignment_source_v1')
            strategies.append(strategy)
        self.assertNotEqual(*strategies)
        state = ledger.inspect_task(self.manifest)
        self.assertEqual(2, state['strategies'][self.strategy]['attempts_used'])
        self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, attempt='a3')

    def test_alignment_scope_stops_at_first_non_alignment_origin(self):
        self.prepare()
        original = dict(run_id='r1', attempt_id='a1')
        self.attempt(attempt='cleanup-origin', descriptor=self.descriptor('alpha_edge_cleanup'), source_attempt=original)
        self.attempt(attempt='native-origin', descriptor=self.descriptor('native_motion_construction'))
        for source_id in ('cleanup-origin', 'native-origin'):
            source = dict(run_id='r1', attempt_id=source_id)
            strategy = self.attempt(attempt='align-' + source_id, descriptor=self.descriptor('body_x_alignment'),
                                    source_attempt=source, strategy_scope='alignment_source_v1')
            self.assertEqual(source, ledger.inspect_task(self.manifest)['strategies'][strategy]['source_group'])

    def test_alignment_scope_mode_cannot_change_for_same_normalized_descriptor(self):
        for first, second in [('target', 'alignment_source_v1'), ('alignment_source_v1', 'target')]:
            with self.subTest(first=first):
                self.manifest = self.create()
                self.prepare()
                source = dict(run_id='r1', attempt_id='a1')
                self.attempt(attempt='align1', descriptor=self.descriptor('body_x_alignment'),
                             source_attempt=source, strategy_scope=first)
                before = ledger.inspect_task(self.manifest)['sequence']
                self.blocked('TASK_STRATEGY_COLLISION', self.attempt, attempt='align2',
                             descriptor=self.descriptor('body_x_alignment', prompt='different', seed=7, path='/new'),
                             source_attempt=source, strategy_scope=second)
                self.assertEqual(before, ledger.inspect_task(self.manifest)['sequence'])

    def test_alignment_scope_limit_identity_and_orphan_recovery_are_immutable(self):
        self.prepare()
        source = dict(run_id='r1', attempt_id='a1')
        self.attempt(attempt='a2')
        head = Path(self.manifest).parent / 'head.json'
        old_head = head.read_bytes()
        strategy = self.attempt(attempt='align', descriptor=self.descriptor('body_x_alignment'),
                                source_attempt=source, strategy_scope='alignment_source_v1')
        final_head = head.read_bytes()
        head.write_bytes(old_head)
        self.assertTrue(ledger.inspect_task(self.manifest)['recovery_required'])
        self.assertEqual(strategy, self.attempt(attempt='align', descriptor=self.descriptor('body_x_alignment'),
                         source_attempt=source, strategy_scope='alignment_source_v1'))
        self.assertEqual(final_head, head.read_bytes())
        self.blocked('TASK_STRATEGY_COLLISION', self.attempt, attempt='align-other-limit',
                     descriptor=self.descriptor('body_x_alignment'), limit=3, source_attempt=source,
                     strategy_scope='alignment_source_v1')
        self.blocked('TASK_STRATEGY_COLLISION', self.attempt, attempt='align',
                     descriptor=self.descriptor('body_x_alignment'), source_attempt=dict(run_id='r1', attempt_id='a2'),
                     strategy_scope='alignment_source_v1')
        self.assertEqual(1, ledger.inspect_task(self.manifest)['strategies'][strategy]['attempts_used'])

    def test_alignment_scope_rejects_unsupported_modes_routes_and_invalid_origins(self):
        self.prepare()
        source = dict(run_id='r1', attempt_id='a1')
        for scope in ('unsupported', '', 1, [], None):
            self.blocked('TASK_POLICY_UNSUPPORTED', ledger.begin_attempt, self.manifest, 'r1', 'bad',
                         self.descriptor('body_x_alignment'), source_attempt=source, strategy_scope=scope)
        for route in ('sheet', 'video', 'alpha_edge_cleanup', 'native_motion_construction'):
            self.blocked('TASK_POLICY_UNSUPPORTED', self.attempt, attempt='bad-route',
                         descriptor=self.descriptor(route), source_attempt=source, strategy_scope='alignment_source_v1')
        self.run_bind('other')
        self.attempt('other', 'other-generation', self.descriptor('video'))
        self.run_bind('other-target', target='golem')
        self.attempt('other-target', 'golem-generation')
        for invalid in (None, {}, dict(run_id='r1', attempt_id='missing'),
                        dict(run_id='other', attempt_id='other-generation'),
                        dict(run_id='other-target', attempt_id='golem-generation'),
                        dict(run_id='r1', attempt_id='bad-source'),
                        dict(run_id='r1', attempt_id='a1', extra='caller-supplied-group')):
            self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH', ledger.begin_attempt, self.manifest, 'r1', 'bad-source',
                         self.descriptor('body_x_alignment'), source_attempt=invalid, strategy_scope='alignment_source_v1')

    def test_alignment_scope_incomplete_legacy_chain_cannot_invent_a_root(self):
        self.run_bind()
        self.attempt(descriptor=self.descriptor('body_x_alignment'))
        self.run_bind('child', parent='r1')
        before = ledger.inspect_task(self.manifest)['sequence']
        self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH', self.attempt, 'child', 'align',
                     self.descriptor('body_x_alignment'), source_attempt=dict(run_id='r1', attempt_id='a1'),
                     strategy_scope='alignment_source_v1')
        self.assertEqual(before, ledger.inspect_task(self.manifest)['sequence'])

    def history_encoded(self, value):
        return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')

    def history_ref(self, name='inventory.json'):
        return dict(path=str(self.root / name), sha256=Q)

    def history_record(self, name='old-generation', *, kind='provider_generation', descriptor=None):
        descriptor = descriptor or self.descriptor()
        return dict(history_id=name, target_id='wolf', source_sha256=H, run_id='old-run', attempt_id=None,
                    attempt_index=4, source_history_id=None, kind=kind,
                    mechanism=dict(origin='fal', route=descriptor['route'], model=descriptor['model'],
                                   conditioning=descriptor['conditioning'], resolution='current_descriptor',
                                   current_descriptor=descriptor),
                    local_attempt_limit=7, global_attempt_limit=None, feedback=' Original feedback. ',
                    cost_basis='fal_operational_reservations', reservation_ids=['old-reservation'],
                    reviews=[dict(stage=stage, decision=decision, original_decision=original, artifact_set_sha256=None, sequence=None,
                                  defects=defects, evidence=[self.history_ref(stage + '.json')])
                             for stage, decision, original, defects in [('raw', 'pass', 'RAW PASS', []),
                                  ('other', 'hold', 'inconclusive', []), ('artifacts', 'reject', 'VISUAL_REJECTED', ['wrong size'])]],
                    evidence=[self.history_ref(name + '-job-state.json')])

    def history_bundle(self):
        return dict(schema='sprite_task_history_v1', migration_id='migration-fixture',
                    eligibility_policy='historical_generation_closed_local_source_v1',
                    coverage=dict(target_ids=['wolf', 'golem'], evidence=[self.history_ref()], complete=True),
                    reservations=[dict(import_id='old-reservation', target_id='wolf', kind='provider_animation',
                        source_sha256=H, config_sha256=C, evidence_sha256=Q, amount_usd='0.4',
                        provider_request_id='known-old-id', uncertain=False)], attempts=[self.history_record()])

    def history_task(self, bundle, cap='1'):
        authorization = dict(task_id='history-task', cap_usd=cap, budget_basis='fal_operational_reservations',
                             allowed_operations=list(ledger.OPERATION_KINDS), evidence_ref='fixture authorization')
        targets = [dict(target_id=x, source_sha256=H, action='run', orientation='left', requirements={'frames': 8})
                   for x in ('wolf', 'golem')]
        requirement = dict(schema='sprite_history_requirement_v1', migration_id=bundle['migration_id'],
                           bundle_sha256=hashlib.sha256(self.history_encoded(bundle)).hexdigest())
        self.manifest = ledger.create_task(self.root, authorization, targets, historical_import=requirement)['task_manifest']
        return requirement

    def history_files(self):
        return {str(p): p.read_bytes() for p in Path(self.manifest).parent.rglob('*') if p.is_file()}

    def test_history_pending_seal_null_budget_and_every_mutator_fail_before_writes(self):
        bundle = self.history_bundle()
        requirement = self.history_task(bundle)
        before = self.history_files()
        state = ledger.inspect_task(self.manifest)
        self.assertEqual(requirement, state['historical_import'])
        self.assertEqual('pending', state['history_status'])
        self.assertIsNone(state['reserved_total_usd'])
        self.assertIsNone(state['available_estimate_usd'])
        self.assertEqual(0, state['historical_attempt_count'])
        self.assertEqual(['golem', 'wolf'], state['history_blocked_targets'])
        manifest = json.loads(Path(self.manifest).read_text())
        sealed = {key: manifest[key] for key in ('authorization', 'targets', 'historical_import')}
        self.assertEqual(hashlib.sha256(self.history_encoded(sealed)).hexdigest(), manifest['authorization_sha256'])
        calls = [(ledger.bind_run, [dict(run_id='r1')]),
                 (ledger.begin_attempt, ['r1', 'a1', self.descriptor()]),
                 (ledger.reserve_plan, ['r1', 'a1', C, []]), (ledger.mark_submission, ['op']),
                 (ledger.bind_operation_config, ['op', {}]), (ledger.reprice_plan, ['r1', 'a1', []]),
                 (ledger.record_outcome, ['op', {}]), (ledger.release_unsubmitted, ['op', 'proof']),
                 (ledger.record_review, ['r1', 'a1', 'pass', [], 'proof']),
                 (ledger.import_reservations, [bundle['reservations']]),
                 (ledger.check_history_context, ['wolf', self.descriptor()])]
        for function, args in calls:
            with self.subTest(function=function.__name__):
                self.blocked('TASK_HISTORY_REQUIRED', function, self.manifest, *args)
        self.assertEqual(before, self.history_files())

    def test_history_atomic_import_keeps_original_reviews_limits_and_six_external_costs_unknown(self):
        bundle = self.history_bundle()
        for number in range(6):
            record = self.history_record('external-' + str(number), kind='external_image')
            record.update(run_id=None, attempt_index=None, local_attempt_limit=None, reviews=[], feedback=None,
                          cost_basis='external_unverified', reservation_ids=[])
            record['mechanism'] = dict(origin='image_gen', route=None, model=None, conditioning=None,
                                       resolution='legacy_unsupported', current_descriptor=None)
            bundle['attempts'].append(record)
        requirement = self.history_task(bundle)
        result = ledger.import_task_history(self.manifest, bundle)
        self.assertEqual(dict(migration_id='migration-fixture', bundle_sha256=requirement['bundle_sha256'],
                              history_status='complete'), result)
        state = ledger.inspect_task(self.manifest)
        self.assertEqual(1, state['sequence'])
        self.assertEqual('0.4', state['reserved_total_usd'])
        self.assertEqual('0.6', state['available_estimate_usd'])
        self.assertEqual(7, state['historical_attempt_count'])
        self.assertEqual([], state['history_blocked_targets'])
        self.assertEqual({record['history_id']: record for record in bundle['attempts']}, state['historical_attempts'])
        for name in ('runs', 'attempts', 'strategies', 'operations'):
            self.assertEqual({}, state[name])
        self.assertFalse(state['delivery_allowed'])
        before = self.history_files()
        self.assertEqual(result, ledger.import_task_history(self.manifest, copy.deepcopy(bundle)))
        self.assertEqual(before, self.history_files())
        self.blocked('TASK_HISTORY_REQUIRED', ledger.import_reservations, self.manifest, bundle['reservations'])
        event = json.loads(next((Path(self.manifest).parent / 'events').iterdir()).read_text())
        self.assertEqual('import_task_history', event['kind'])
        self.assertEqual(dict(bundle=bundle), event['payload'])

    def test_history_review_sequence_preserves_mixed_partial_raw_hold_then_pass(self):
        bundle = self.history_bundle()
        reviews = bundle['attempts'][0]['reviews']
        raw_hold = dict(sequence=2, stage='raw', decision='hold', original_decision='HOLD',
                        artifact_set_sha256=None, defects=[], evidence=[self.history_ref('raw-hold.json')])
        raw_pass = dict(sequence=7, stage='raw', decision='pass', original_decision='PASS',
                        artifact_set_sha256=H, defects=[], evidence=[self.history_ref('raw-pass.json')])
        unpositioned = copy.deepcopy(reviews[-1])
        unpositioned.update(sequence=None, original_decision=' joint alternative rejection ',
                            evidence=[self.history_ref('joint-alternative-observation.json')])
        reviews[:] = [raw_hold, unpositioned, raw_pass]
        local = self.history_record('other-observations', kind='local_derivation', descriptor=self.descriptor('body_x_alignment'))
        local.update(run_id='other-observation-run', source_history_id='old-generation',
                     cost_basis='local_no_new_reservation', reservation_ids=[], reviews=copy.deepcopy([raw_hold, raw_pass]))
        local['mechanism']['origin'] = 'local'
        bundle['attempts'].append(local)  # Numbering is per attempt, never a global timeline.
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        before = self.history_files()
        state = ledger.inspect_task(self.manifest)
        self.assertEqual([raw_hold, unpositioned, raw_pass], state['historical_attempts']['old-generation']['reviews'])
        self.assertEqual([raw_hold, raw_pass], state['historical_attempts']['other-observations']['reviews'])
        self.assertEqual({}, state['attempts'])
        self.assertFalse(state['delivery_allowed'])
        self.blocked('TASK_HISTORY_MECHANISM_CLOSED', ledger.check_history_context, self.manifest, 'wolf', self.descriptor())
        self.assertEqual(before, self.history_files())

    def test_history_review_sequence_all_null_retains_observations_without_last_verdict_authority(self):
        bundle = self.history_bundle()
        reviews = bundle['attempts'][0]['reviews']
        reviews.append(dict(sequence=None, stage='other', decision='pass', original_decision='partial selection only',
                            artifact_set_sha256=None, defects=[], evidence=[self.history_ref('narrow-pass.json')]))
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        state = ledger.inspect_task(self.manifest)
        self.assertEqual(reviews, state['historical_attempts']['old-generation']['reviews'])
        self.assertEqual([None] * 4, [review['sequence'] for review in state['historical_attempts']['old-generation']['reviews']])
        self.assertEqual({}, state['attempts'])
        self.assertFalse(state['delivery_allowed'])

    def test_history_review_sequence_invalid_or_missing_is_atomic(self):
        cases = [[], [0], [-1], [True], [False], [1.0], ['1'], [[]], [{}], [2, 2], [2, None, 2], [7, None, 3]]
        for values in cases:
            with self.subTest(values=values):
                bundle = self.history_bundle()
                template = bundle['attempts'][0]['reviews'][0]
                reviews = []
                for value in values or ['missing']:
                    review = copy.deepcopy(template)
                    if value == 'missing':review.pop('sequence')
                    else:review['sequence'] = value
                    reviews.append(review)
                bundle['attempts'][0]['reviews'] = reviews
                self.history_task(bundle)
                before = self.history_files()
                self.blocked('TASK_SCHEMA_INVALID', ledger.import_task_history, self.manifest, bundle)
                self.assertEqual(before, self.history_files())

    def test_history_owner_evidence_rejects_missing_coverage_only_and_other_owner_only_atomically(self):
        for location in ('missing', 'coverage-only', 'other-owner-only'):
            with self.subTest(location=location):
                bundle = self.history_bundle()
                bundle['reservations'][0]['evidence_sha256'] = H
                physical = dict(path=str(self.root / 'referenced-job-state.json'), sha256=H)
                if location == 'coverage-only':
                    bundle['coverage']['evidence'].append(physical)
                elif location == 'other-owner-only':
                    other = self.history_record('other-generation', descriptor=self.descriptor('video'))
                    other.update(run_id='other-run', reservation_ids=['other-reservation'], evidence=[physical])
                    reservation = copy.deepcopy(bundle['reservations'][0])
                    reservation.update(import_id='other-reservation', amount_usd='0.1', provider_request_id='other-known-id')
                    bundle['attempts'].append(other)
                    bundle['reservations'].append(reservation)
                self.history_task(bundle)
                before = self.history_files()
                self.blocked('TASK_SCHEMA_INVALID', ledger.import_task_history, self.manifest, bundle)
                state = ledger.inspect_task(self.manifest)
                self.assertEqual('pending', state['history_status'])
                self.assertEqual(0, state['sequence'])
                self.assertEqual({}, state['imports'])
                self.assertEqual({}, state['historical_attempts'])
                self.assertIsNone(state['reserved_total_usd'])
                self.assertEqual(before, self.history_files())

    def test_history_owner_evidence_accepts_physical_ref_distinct_from_semantic_config(self):
        bundle = self.history_bundle()
        physical = dict(path=str(self.root / 'owned-job-state.json'), sha256=H)
        bundle['reservations'][0]['evidence_sha256'] = H
        bundle['attempts'][0]['evidence'].append(physical)
        self.assertNotEqual(bundle['reservations'][0]['config_sha256'], physical['sha256'])
        self.assertNotIn(H, [ref['sha256'] for ref in bundle['coverage']['evidence']])
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('complete', state['history_status'])
        self.assertEqual(1, state['sequence'])
        self.assertEqual('0.4', state['reserved_total_usd'])
        self.assertEqual(bundle['attempts'][0]['evidence'], state['historical_attempts']['old-generation']['evidence'])

    def test_history_import_conflict_unsealed_and_changed_bundle_never_mutate(self):
        bundle = self.history_bundle()
        self.blocked('TASK_HISTORY_CONFLICT', ledger.import_task_history, self.manifest, bundle)
        self.history_task(bundle)
        before = self.history_files()
        changed = copy.deepcopy(bundle)
        changed['migration_id'] = 'another'
        self.blocked('TASK_HISTORY_CONFLICT', ledger.import_task_history, self.manifest, changed)
        changed = copy.deepcopy(bundle)
        changed['attempts'][0]['feedback'] = 'other data'
        self.blocked('TASK_HISTORY_CONFLICT', ledger.import_task_history, self.manifest, changed)
        self.assertEqual(before, self.history_files())

    def test_history_durable_import_with_old_head_replays_both_finance_and_history(self):
        bundle = self.history_bundle()
        self.history_task(bundle)
        head = Path(self.manifest).parent / 'head.json'
        before = head.read_bytes()
        ledger.import_task_history(self.manifest, bundle)
        final = head.read_bytes()
        head.write_bytes(before)
        files = self.history_files()
        state = ledger.inspect_task(self.manifest)
        self.assertTrue(state['recovery_required'])
        self.assertEqual('complete', state['history_status'])
        self.assertEqual('0.4', state['reserved_total_usd'])
        self.assertEqual(1, state['historical_attempt_count'])
        self.assertEqual(files, self.history_files())
        ledger.import_task_history(self.manifest, bundle)
        self.assertEqual(final, head.read_bytes())
        self.assertEqual(1, ledger.inspect_task(self.manifest)['sequence'])

    def test_history_malformed_complete_bundle_is_atomic(self):
        def mutate(bundle, case):
            record = bundle['attempts'][0]
            if case == 'coverage': bundle['coverage']['complete'] = False
            elif case == 'targets': bundle['coverage']['target_ids'] = ['wolf']
            elif case == 'duplicate': bundle['attempts'].append(copy.deepcopy(record))
            elif case == 'orphan': record['reservation_ids'] = []
            elif case == 'double-owner':
                other = copy.deepcopy(record);other.update(history_id='alias', run_id='other')
                bundle['attempts'].append(other)
            elif case == 'source': record['source_sha256'] = Q
            elif case == 'target': record['target_id'] = 'golem'
            elif case == 'missing-parent': record['source_history_id'] = 'missing'
            elif case == 'cycle': record['source_history_id'] = record['history_id']
            elif case == 'ref': record['evidence'][0]['path'] = 'relative.json'
            elif case == 'ref-conflict': record['evidence'].append(dict(path=record['evidence'][0]['path'], sha256=C))
            elif case == 'no-evidence': record['reviews'][0]['evidence'] = []
            elif case == 'bool-limit': record['local_attempt_limit'] = True
            elif case == 'bad-review': record['reviews'][0]['decision'] = 'approved'
            elif case == 'model-mismatch': record['mechanism']['model'] = 'another-model'
            elif case == 'fake-local': record['kind'] = 'local_derivation'
            elif case == 'legacy-supported': record['mechanism'].update(resolution='legacy_unsupported', current_descriptor=None)
            elif case == 'extra': record['made_up_authority'] = True
        for case in ('coverage', 'targets', 'duplicate', 'orphan', 'double-owner', 'source', 'target',
                     'missing-parent', 'cycle', 'ref', 'ref-conflict', 'no-evidence', 'bool-limit',
                     'bad-review', 'model-mismatch', 'fake-local', 'legacy-supported', 'extra'):
            with self.subTest(case=case):
                bundle = self.history_bundle();mutate(bundle, case)
                self.history_task(bundle)
                before = self.history_files()
                with self.assertRaises(ledger.WorkflowError):
                    ledger.import_task_history(self.manifest, bundle)
                self.assertEqual(before, self.history_files())

    def test_history_canonical_amounts_and_cap_are_checked_without_partial_import(self):
        for value in ('0.40', '.4', 0.4, 1, True, Decimal('0.4')):
            with self.subTest(value=str(value)):
                bundle = self.history_bundle()
                self.history_task(bundle)
                bundle['reservations'][0]['amount_usd'] = value
                before = self.history_files()
                with self.assertRaises(ledger.WorkflowError):
                    ledger.import_task_history(self.manifest, bundle)
                self.assertEqual(before, self.history_files())
        bundle = self.history_bundle()
        self.history_task(bundle, cap='0.3')
        before = self.history_files()
        self.blocked('TASK_BUDGET_EXCEEDED', ledger.import_task_history, self.manifest, bundle)
        self.assertEqual(before, self.history_files())

    def test_history_identifiers_and_supported_model_labels_cannot_hide_equivalence(self):
        for case in ('padded-target', 'padded-import-id', 'padded-model'):
            with self.subTest(case=case):
                bundle = self.history_bundle()
                if case == 'padded-target':
                    bundle['attempts'][0]['target_id'] = ' wolf '
                    bundle['reservations'][0]['target_id'] = ' wolf '
                elif case == 'padded-import-id':
                    bundle['reservations'][0]['import_id'] = ' old-reservation '
                else:
                    bundle['attempts'][0]['mechanism'].update(model=' fal-ai/nano-banana-2/edit ',
                        resolution='legacy_unsupported', current_descriptor=None)
                self.history_task(bundle)
                before = self.history_files()
                with self.assertRaises(ledger.WorkflowError):
                    ledger.import_task_history(self.manifest, bundle)
                self.assertEqual(before, self.history_files())

    def test_history_new_post_guard_does_not_depend_on_a_caller_preflight(self):
        bundle = self.history_bundle()
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        self.run_bind()
        strategy = self.attempt(descriptor=self.descriptor('video'))
        ledger.reserve_plan(self.manifest, 'r1', 'a1', strategy, [self.op(amount='0.1')])
        state = ledger.inspect_task(self.manifest)
        # Exercise the replay transition itself against a closed-mechanism
        # snapshot. No amended history or fabricated event is persisted.
        closed = self.history_record('closed-video', descriptor=self.descriptor('video'))
        state['historical_attempts']['closed-video'] = closed
        self.blocked('TASK_HISTORY_MECHANISM_CLOSED', ledger._apply, state, 'mark_submission', dict(operation_id='op1'))
        self.assertFalse(state['operations']['op1']['submission_started'])
        closed['mechanism'].update(resolution='unresolved', current_descriptor=None)
        self.blocked('TASK_HISTORY_UNRESOLVED', ledger._apply, state, 'mark_submission', dict(operation_id='op1'))
        self.assertFalse(state['operations']['op1']['submission_started'])

    def test_history_generation_closure_ignores_annotations_but_distinct_mechanism_keeps_limits(self):
        bundle = self.history_bundle()
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        self.run_bind()
        for descriptor in (self.descriptor(), self.descriptor('auto', prompt='new', seed=22, path='/new', uuid='new')):
            self.blocked('TASK_HISTORY_MECHANISM_CLOSED', self.attempt, descriptor=descriptor)
            self.blocked('TASK_HISTORY_MECHANISM_CLOSED', ledger.check_history_context, self.manifest, 'wolf', descriptor)
        self.assertIsNone(ledger.check_history_context(self.manifest, 'wolf', self.descriptor('video')))
        strategy = self.attempt(descriptor=self.descriptor('video'), limit=1)
        self.blocked('TASK_STRATEGY_EXHAUSTED', self.attempt, attempt='a2', descriptor=self.descriptor('video'), limit=1)
        self.blocked('TASK_BUDGET_EXCEEDED', ledger.reserve_plan, self.manifest, 'r1', 'a1', strategy, [self.op(amount='0.7')])
        ledger.reserve_plan(self.manifest, 'r1', 'a1', strategy, [self.op(amount='0.5')])
        self.assertEqual('0.9', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_history_native_construction_is_generation_even_without_a_fal_reservation(self):
        bundle = self.history_bundle()
        record = self.history_record(kind='native_construction', descriptor=self.descriptor('native_motion_construction'))
        record.update(cost_basis='local_no_new_reservation', reservation_ids=[])
        record['mechanism']['origin'] = 'local'
        bundle.update(attempts=[record], reservations=[])
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        self.run_bind()
        self.blocked('TASK_HISTORY_MECHANISM_CLOSED', self.attempt, descriptor=self.descriptor('native_motion_construction'))
        self.attempt(descriptor=self.descriptor('video'))
        self.assertEqual('0', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_history_local_uncertainty_and_paid_cleanup_do_not_close_new_output_alignment(self):
        bundle = self.history_bundle()
        original = bundle['attempts'][0]
        local = self.history_record('old-local', kind='local_derivation', descriptor=self.descriptor('body_x_alignment'))
        local.update(run_id='old-local-run', source_history_id=original['history_id'], reservation_ids=[],
                     cost_basis='local_no_new_reservation')
        local['mechanism'].update(origin='local', resolution='unresolved', current_descriptor=None)
        paid = copy.deepcopy(local)
        paid.update(history_id='paid-cleanup', run_id='old-cleanup-run', reservation_ids=['paid-cleanup-reservation'],
                    cost_basis='fal_operational_reservations')
        paid['mechanism'] = dict(origin='fal', route='alpha_edge_cleanup', model='fal-ai/ideogram/remove-background',
                                 conditioning='single_source', resolution='legacy_unsupported', current_descriptor=None)
        reservation = copy.deepcopy(bundle['reservations'][0])
        reservation.update(import_id='paid-cleanup-reservation', kind='alpha_edge_cleanup', amount_usd='0.1',
                           provider_request_id='cleanup-known-id')
        bundle['reservations'].append(reservation)
        bundle['attempts'].extend([local, paid])
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        self.assertEqual([], ledger.inspect_task(self.manifest)['history_blocked_targets'])
        self.run_bind()
        self.attempt(descriptor=self.descriptor('video'))
        source = dict(run_id='r1', attempt_id='a1')
        self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH', self.attempt, attempt='unbound-local',
                     descriptor=self.descriptor('body_x_alignment'))
        self.attempt(attempt='align', descriptor=self.descriptor('body_x_alignment'), source_attempt=source,
                     strategy_scope='alignment_source_v1')
        self.attempt(attempt='alpha', descriptor=self.descriptor('alpha_edge_cleanup'), source_attempt=source)
        self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH', self.attempt, attempt='historical-copy',
                     descriptor=self.descriptor('body_x_alignment'), strategy_scope='alignment_source_v1',
                     source_attempt=dict(run_id='old-run', attempt_id='old-generation'))
        ledger.record_review(self.manifest, 'r1', 'a1', 'reject', ['identity: wrong head'], 'new source proof')
        self.blocked('TASK_SOURCE_DEFECT_UNRESOLVED', ledger.record_review, self.manifest,
                     'r1', 'align', 'pass', [], 'X only')
        state = ledger.inspect_task(self.manifest)
        self.assertEqual('0.5', state['reserved_total_usd'])
        alignment = next(a for a in state['attempts'].values() if a['attempt_id'] == 'align')
        self.assertEqual(['identity: wrong head'], alignment['effective_defects'])

    def test_history_transport_bounds_cross_target_parent_and_duplicate_request_id_are_atomic(self):
        for case in ('attempt-limit', 'reservation-limit', 'cross-target-parent', 'duplicate-request'):
            with self.subTest(case=case):
                bundle = self.history_bundle()
                if case == 'attempt-limit':
                    bundle['attempts'] *= 513
                elif case == 'reservation-limit':
                    bundle['reservations'] *= 257
                else:
                    other = self.history_record('other', descriptor=self.descriptor('video'))
                    other.update(target_id='golem', run_id='other-run', reservation_ids=['other-reservation'])
                    reservation = copy.deepcopy(bundle['reservations'][0])
                    reservation.update(import_id='other-reservation', target_id='golem', amount_usd='0.1')
                    if case == 'cross-target-parent':
                        reservation['provider_request_id'] = 'other-known-id'
                        bundle['attempts'][0]['source_history_id'] = 'other'
                    bundle['attempts'].append(other)
                    bundle['reservations'].append(reservation)
                self.history_task(bundle)
                before = self.history_files()
                with self.assertRaises(ledger.WorkflowError):
                    ledger.import_task_history(self.manifest, bundle)
                self.assertEqual(before, self.history_files())

    def test_history_unresolved_target_blocks_new_work_but_known_ids_recover_without_new_post(self):
        for kind in ('provider_generation', 'unresolved'):
            with self.subTest(kind=kind):
                bundle = self.history_bundle()
                bundle['attempts'][0]['kind'] = kind
                bundle['attempts'][0]['mechanism'].update(resolution='unresolved', current_descriptor=None)
                self.history_task(bundle)
                ledger.import_task_history(self.manifest, bundle)
                self.run_bind()
                self.blocked('TASK_HISTORY_UNRESOLVED', self.attempt, descriptor=self.descriptor('video'))
                self.blocked('TASK_HISTORY_UNRESOLVED', ledger.check_history_context, self.manifest, 'wolf')
                self.assertEqual(['wolf'], ledger.inspect_task(self.manifest)['history_blocked_targets'])
                self.assertFalse(ledger.mark_submission(self.manifest, 'old-reservation')['submit_allowed'])
                ledger.record_outcome(self.manifest, 'old-reservation', dict(status='completed',
                                     evidence='verified old transport', provider_request_id='known-old-id'))
                self.assertEqual('0.4', ledger.inspect_task(self.manifest)['reserved_total_usd'])
                self.run_bind('other', target='golem')
                self.attempt('other', 'other-attempt')

    def test_history_unknown_submission_keeps_money_and_never_reposts(self):
        bundle = self.history_bundle()
        bundle['reservations'][0].update(provider_request_id=None, uncertain=True)
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        self.blocked('TASK_SUBMISSION_UNRESOLVED', ledger.mark_submission, self.manifest, 'old-reservation')
        self.run_bind()
        self.blocked('TASK_SUBMISSION_UNRESOLVED', self.attempt, descriptor=self.descriptor('video'))
        ledger.record_outcome(self.manifest, 'old-reservation', dict(status='completed', provider_request_id='recovered-id',
                             evidence='real archived transport evidence'))
        self.assertFalse(ledger.mark_submission(self.manifest, 'old-reservation')['submit_allowed'])
        self.assertEqual('0.4', ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_history_whole_event_limit_rejects_even_when_bundle_itself_fits(self):
        bundle = self.history_bundle()
        ref = self.history_ref('x' * 2800 + '.json')
        bundle['coverage']['evidence'] = [ref]
        def event_size():
            return len(self.history_encoded(dict(sequence=1, previous_sha256=H, kind='import_task_history',
                        payload=dict(bundle=bundle), sha256=Q)))
        one = event_size()
        bundle['coverage']['evidence'].append(ref)
        step = event_size() - one
        bundle['coverage']['evidence'] = [ref] * ((5 * 1024 * 1024 - one) // step + 2)
        self.assertLessEqual(len(self.history_encoded(bundle)), 5 * 1024 * 1024)
        self.assertGreater(event_size(), 5 * 1024 * 1024)
        self.history_task(bundle)
        before = self.history_files()
        with self.assertRaises(ledger.WorkflowError):
            ledger.import_task_history(self.manifest, bundle)
        self.assertEqual(before, self.history_files())

    def test_history_event_at_exact_limit_is_replayable(self):
        bundle = self.history_bundle()
        long_ref, short_ref = self.history_ref('x' * 2800 + '.json'), self.history_ref('pad.json')
        def size():
            return len(self.history_encoded(dict(sequence=1, previous_sha256=H, kind='import_task_history',
                        payload=dict(bundle=bundle), sha256=Q)))
        bundle['coverage']['evidence'] = [short_ref]
        base = size()
        bundle['coverage']['evidence'].insert(0, long_ref)
        step = size() - base
        count = (5 * 1024 * 1024 - base) // step
        bundle['coverage']['evidence'] = [long_ref] * count + [short_ref]
        padding = 5 * 1024 * 1024 - size()
        short_ref['path'] = str(self.root / ('pad' + 'x' * padding + '.json'))
        self.assertEqual(5 * 1024 * 1024, size())
        self.history_task(bundle)
        ledger.import_task_history(self.manifest, bundle)
        event = next((Path(self.manifest).parent / 'events').iterdir())
        self.assertEqual(5 * 1024 * 1024, event.stat().st_size)
        self.assertEqual('complete', ledger.inspect_task(self.manifest)['history_status'])

    def test_history_requirement_tamper_and_missing_seal_are_not_legacy_downgrades(self):
        for case in ('changed', 'removed'):
            with self.subTest(case=case):
                self.history_task(self.history_bundle())
                path = Path(self.manifest)
                value = json.loads(path.read_text())
                if case == 'changed':value['historical_import']['bundle_sha256'] = H
                else:value.pop('historical_import')
                path.write_bytes(self.history_encoded(value))
                self.blocked('TASK_JOURNAL_CORRUPT', ledger.inspect_task, self.manifest)

    def test_history_unsealed_manifest_and_inspection_keep_legacy_shape_and_import_behavior(self):
        before = Path(self.manifest).read_bytes()
        expected = dict(schema='sprite_task_ledger_v1', authorization=dict(task_id='human-task', cap_usd='1',
            budget_basis='fal_operational_reservations', allowed_operations=sorted(ledger.OPERATION_KINDS), evidence_ref='user:message:7'),
            targets={x:dict(target_id=x, source_sha256=H, action='run', orientation='left', requirements={'frames':8})
                     for x in ('wolf', 'golem')})
        sealed = dict(authorization=expected['authorization'], targets=expected['targets'])
        expected['authorization_sha256'] = hashlib.sha256(self.history_encoded(sealed)).hexdigest()
        self.assertEqual(self.history_encoded(expected), before)
        state = ledger.inspect_task(self.manifest)
        for key in ('historical_import', 'history_status', 'historical_attempts', 'historical_attempt_count', 'history_blocked_targets'):
            self.assertNotIn(key, state)
        self.assertIsNone(ledger.check_history_context(self.manifest, 'wolf', self.descriptor()))
        reservation = self.history_bundle()['reservations'][0]
        reservation['amount_usd'] = '0.40'
        ledger.import_reservations(self.manifest, [reservation])
        self.assertEqual('0.4', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.assertEqual(before, Path(self.manifest).read_bytes())

    def test_journal_orphan_event_counts_and_mutation_recovers_head(self):
        head = Path(self.manifest).parent / 'head.json'
        before = head.read_bytes()
        self.prepare()
        self.reserve()
        head.write_bytes(before)  # durable event, interrupted head advance
        snapshot = {p.name: p.read_bytes() for p in head.parent.rglob('*') if p.is_file()}
        self.assertEqual('0.3', ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.assertEqual(snapshot, {p.name: p.read_bytes() for p in head.parent.rglob('*') if p.is_file()})
        self.run_bind('r2', 'golem')
        self.assertGreater(json.loads(head.read_text())['sequence'], 0)

    def test_missing_truncated_changed_event_and_bad_head_fail_closed(self):
        for mode in ('missing', 'truncated', 'changed', 'head'):
            with self.subTest(mode=mode):
                self.manifest = self.create()
                self.prepare()
                events = sorted((Path(self.manifest).parent / 'events').glob('*.json'))
                if mode == 'missing':
                    events[0].unlink()
                elif mode == 'truncated':
                    events[0].write_text('{')
                elif mode == 'changed':
                    data = json.loads(events[0].read_text())
                    data['payload']['run']['scope_sha256'] = Q
                    events[0].write_text(json.dumps(data))
                else:
                    (Path(self.manifest).parent / 'head.json').write_text('{}')
                self.blocked('TASK_JOURNAL_CORRUPT', ledger.inspect_task, self.manifest)

    def test_concurrent_processes_cannot_overdraw_final_balance(self):
        self.prepare()
        self.run_bind('r2', 'golem')
        other = self.attempt('r2', 'a2')
        context = multiprocessing.get_context('spawn')
        gate, queue = context.Event(), context.Queue()
        children = [context.Process(target=contender, args=(self.manifest, run, strategy, self.op(name, '.7'), gate, queue))
                    for run, strategy, name in [('r1', self.strategy, 'race1'), ('r2', other, 'race2')]]
        for child in children:
            child.start()
        gate.set()
        results = [queue.get(timeout=25) for _ in children]
        for child in children:
            child.join(25)
            self.assertEqual(0, child.exitcode)
        self.assertCountEqual(['reserved', 'TASK_BUDGET_EXCEEDED'], results)
        self.assertEqual('0.7', ledger.inspect_task(self.manifest)['reserved_total_usd'])



def endpoint_race_action(manifest, action, args, start, result):
    import sprite_task_ledger as child
    start.wait(15)
    try:
        if action == 'mark': child.mark_submission(manifest, args['operation_id'], expected=args['expected'])
        elif action == 'reprice': child.reprice_plan(manifest, args['run_id'], args['attempt_id'], args['operations'], endpoint_plan=args['endpoint_plan'])
        elif action == 'gate': child.record_endpoint_gate(manifest, args['run_id'], args['attempt_id'], args['gate'])
        elif action == 'release': child.release_unsubmitted(manifest, args['operation_id'], 'race abandon')
        result.put((action, 'ok'))
    except child.WorkflowError as exc:
        result.put((action, exc.code))


class EndpointLedgerTests(unittest.TestCase):
    setUp = LedgerTests.setUp
    create = LedgerTests.create
    run_bind = LedgerTests.run_bind
    blocked = LedgerTests.blocked
    history_ref = LedgerTests.history_ref
    history_record = LedgerTests.history_record
    history_bundle = LedgerTests.history_bundle
    history_task = LedgerTests.history_task
    history_encoded = LedgerTests.history_encoded
    descriptor = LedgerTests.descriptor

    def digest(self, value):
        return hashlib.sha256(self.history_encoded(value)).hexdigest()

    def endpoint_descriptor(self):
        return dict(schema='sprite_strategy_descriptor_v2', route='endpoint_cycle_v1',
                    family='nano2_wan_flf2v_cycle_v1', model='fal-ai/wan-flf2v',
                    conditioning='pose_edit_then_opposite_endpoints', inputs=[dict(role='source', sha256=H)])

    def setup_endpoint(self, run='e1', correction=None, reserve=True, config=C):
        self.run_bind(run)
        aid = run + ':attempt-01'
        strategy = ledger.begin_attempt(self.manifest, run, aid, self.endpoint_descriptor(), correction=correction)
        plan = dict(schema='sprite_endpoint_plan_v1', attempt_id=aid, source_sha256=H,
                    pose_config_sha256=config, pose_inputs=[dict(role='edit', sha256=Q), dict(role='identity', sha256=H)],
                    endpoint_a_sha256='d'*64, preparation_policy='pose_canvas_2048_v1',
                    video_policy=dict(frames=81, fps=24, resolution='720p', aspect_ratio='1:1', steps=30,
                                      acceleration='none', prompt_expansion=False),
                    sampling_policy='endpoint_full_range_81_four_phases_v1',
                    cleanup_policy=dict(model='fal-ai/ideogram/remove-background', sync_mode=False,
                                        max_bytes=10485760, max_width=4096, max_height=4096),
                    output_policy=dict(frames=8, frame_ms=100, loop=True, canvas=[2048,2048],
                                       geometry_policy='common_nearest_2048_v1', alignment_policy='body_x_preserve_y_v1'))
        ops=[]
        for role, amount in [('pose_edit','0.12'),('video_ab','0.4'),('video_ba','0.4'),('cleanup','0.01')]:
            op=dict(operation_id=aid+':'+role, kind='alpha_edge_cleanup' if role=='cleanup' else 'provider_animation',
                    quote_sha256=Q, amount_usd=amount)
            if role=='pose_edit': op['config_sha256']=config
            else: op['deferred_config']=dict(schema='sprite_endpoint_deferred_v1',endpoint_plan_sha256=self.digest(plan),role=role)
            ops.append(op)
        ctx=dict(run=run, aid=aid, strategy=strategy, plan=plan, ops=ops)
        if reserve: self.reserve_endpoint(ctx)
        return ctx

    def reserve_endpoint(self, ctx, ops=None, plan=None):
        return ledger.reserve_plan(self.manifest, ctx['run'], ctx['aid'], ctx['strategy'],
                                   ops or ctx['ops'], endpoint_plan=plan or ctx['plan'])

    def state_attempt(self, ctx):
        return next(a for a in ledger.inspect_task(self.manifest)['attempts'].values() if a['attempt_id']==ctx['aid'])

    def gate(self, ctx, stage, decision='pass', subject=None):
        if subject is None:
            ph=self.digest(ctx['plan'])
            subjects=dict(input_ready=dict(endpoint_plan_sha256=ph),
                pair_ready=dict(endpoint_plan_sha256=ph,pose_request_id=ctx['aid']+':pose_edit:request',
                                pose_raw_sha256='e'*64,endpoint_a_sha256='d'*64,endpoint_b_sha256='e'*64))
            if stage in ('ab_ready','cycle_ready'):
                pair=self.state_attempt(ctx)['endpoint_gates']['pair_ready']
                subjects['ab_ready']=dict(pair_gate_sha256=self.digest(pair),ab_request_id=ctx['aid']+':video_ab:request',
                                         ab_raw_sha256='f'*64,probe_sha256=C)
                if stage=='cycle_ready':
                    ab=self.state_attempt(ctx)['endpoint_gates']['ab_ready']
                    subjects['cycle_ready']=dict(pair_gate_sha256=self.digest(pair),ab_gate_sha256=self.digest(ab),
                        ab_request_id=ctx['aid']+':video_ab:request',ab_raw_sha256='f'*64,
                        ba_request_id=ctx['aid']+':video_ba:request',ba_raw_sha256='1'*64,selection_sha256=C,grid_sha256='2'*64)
            subject=subjects[stage]
        gate=dict(schema='sprite_endpoint_gate_v1', stage=stage, decision=decision,subject=subject,
                  defects=['material: observed mismatch'] if decision=='reject' else [], evidence=[self.history_ref(stage+'.json')])
        ledger.record_endpoint_gate(self.manifest,ctx['run'],ctx['aid'],gate)
        return gate

    def bind(self,ctx,role,inputs=None):
        aid=ctx['aid']; ph=self.digest(ctx['plan'])
        actual=dict(pose_edit=ctx['plan']['pose_inputs'],
                    video_ab=[dict(role='start',sha256='d'*64),dict(role='end',sha256='e'*64)],
                    video_ba=[dict(role='start',sha256='e'*64),dict(role='end',sha256='d'*64)],
                    cleanup=[dict(role='grid',sha256='2'*64)])[role]
        binding=dict(schema='sprite_execution_binding_v2',config_sha256=ctx['plan']['pose_config_sha256'] if role=='pose_edit' else ('4'*64 if role=='video_ba' else C),
            config_file_sha256=C,input_set_sha256=self.digest(actual if inputs is None else inputs),provenance_sha256=C,
            deferred_plan_sha256=None if role=='pose_edit' else self.digest(next(o['deferred_config'] for o in ctx['ops'] if o['operation_id']==aid+':'+role)))
        return ledger.bind_operation_config(self.manifest,aid+':'+role,binding)

    def expected(self,ctx,role):
        op=ledger.inspect_task(self.manifest)['operations'][ctx['aid']+':'+role]
        return dict(schema='sprite_endpoint_submission_expected_v1',config_sha256=op['execution_binding']['config_sha256'],
            binding_sha256=op['binding_sha256'],quote_sha256=op['current_quote']['quote_sha256'],amount_usd=op['current_quote']['amount_usd'],
            experiment_state_sha256=self.state_attempt(ctx)['experiment_state_sha256'])

    def submit(self,ctx,role,status='completed'):
        self.bind(ctx,role)
        result=ledger.mark_submission(self.manifest,ctx['aid']+':'+role,expected=self.expected(ctx,role))
        self.assertTrue(result['submit_allowed'])
        self.assertEqual(self.state_attempt(ctx)['experiment_state_sha256'],result['experiment_state_sha256'])
        ledger.record_outcome(self.manifest,ctx['aid']+':'+role,dict(status=status,evidence='provider fixture',provider_request_id=ctx['aid']+':'+role+':request'))

    def advance_pair(self,ctx):
        self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit');self.gate(ctx,'pair_ready')

    def test_full_plan_atomic_cap_and_exact_four_roles(self):
        self.manifest=self.create('0.92');ctx=self.setup_endpoint(reserve=False)
        self.blocked('TASK_BUDGET_EXCEEDED',self.reserve_endpoint,ctx)
        self.assertEqual({},ledger.inspect_task(self.manifest)['operations'])
        self.blocked('TASK_PLAN_INVALID',self.reserve_endpoint,ctx,ctx['ops'][:3])
        self.manifest=self.create('1.14');ctx=self.setup_endpoint()
        self.assertEqual('0.21',ledger.inspect_task(self.manifest)['available_estimate_usd'])
        self.assertEqual('0',self.reserve_endpoint(ctx)['newly_reserved_usd'])
        self.assertEqual(4,len(ledger.inspect_task(self.manifest)['operations']))

    def test_plan_closed_fields_commitments_and_materialization(self):
        ctx=self.setup_endpoint(reserve=False)
        for key,value in [('video_policy',dict(ctx['plan']['video_policy'],frames=82)),('extra',True),('pose_inputs',list(reversed(ctx['plan']['pose_inputs'])))]:
            altered=dict(ctx['plan']);altered[key]=value
            self.blocked('TASK_PLAN_INVALID',self.reserve_endpoint,ctx,plan=altered)
        self.reserve_endpoint(ctx)
        bad=copy.deepcopy(ctx['ops']);bad[1]['deferred_config']['role']='video_ba'
        self.blocked('TASK_PLAN_INVALID',self.reserve_endpoint,ctx,bad)
        self.blocked('TASK_OPERATION_COLLISION',self.bind,ctx,'pose_edit',list(reversed(ctx['plan']['pose_inputs'])))
        self.bind(ctx,'pose_edit');self.bind(ctx,'pose_edit')
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',self.bind,ctx,'video_ab')

    def test_full_cycle_gates_bind_order_and_delivery_review_remains_separate(self):
        ctx=self.setup_endpoint();self.bind(ctx,'pose_edit')
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        self.advance_pair(ctx)
        self.blocked('TASK_OPERATION_COLLISION',self.bind,ctx,'video_ab',[dict(role='start',sha256='e'*64),dict(role='end',sha256='d'*64)])
        self.submit(ctx,'video_ab');self.gate(ctx,'ab_ready');self.submit(ctx,'video_ba');self.gate(ctx,'cycle_ready');self.submit(ctx,'cleanup')
        self.assertNotIn('review',self.state_attempt(ctx))
        self.assertEqual(4,len(self.state_attempt(ctx)['endpoint_gates']))
        for role in ('pose_edit','video_ab','video_ba','cleanup'):
            result=ledger.mark_submission(self.manifest,ctx['aid']+':'+role,expected=self.expected(ctx,role))
            self.assertFalse(result['submit_allowed'])

    def test_hold_pass_revocation_reject_atomic_and_subject_is_immutable(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready','hold');self.bind(ctx,'pose_edit')
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        self.advance_pair(ctx);self.bind(ctx,'video_ab')
        self.gate(ctx,'pair_ready','hold')
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':video_ab',expected=self.expected(ctx,'video_ab'))
        self.gate(ctx,'pair_ready');self.gate(ctx,'pair_ready','reject')
        self.assertIn('material: observed mismatch',self.state_attempt(ctx)['effective_defects'])
        self.assertEqual('rejected',self.state_attempt(ctx)['endpoint_status'])
        self.blocked('TASK_ATTEMPT_CLOSED',self.gate,ctx,'pair_ready')
        self.blocked('TASK_ATTEMPT_CLOSED',ledger.mark_submission,self.manifest,ctx['aid']+':video_ab',expected=self.expected(ctx,'video_ab'))

    def test_gate_wrong_ids_raw_subject_prior_pass_and_evidence_rejected_without_event(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit')
        valid=dict(endpoint_plan_sha256=self.digest(ctx['plan']),pose_request_id=ctx['aid']+':pose_edit:request',pose_raw_sha256='e'*64,endpoint_a_sha256='d'*64,endpoint_b_sha256='e'*64)
        before=ledger.inspect_task(self.manifest)['sequence']
        for key,value in [('pose_request_id','wrong'),('endpoint_b_sha256',C),('endpoint_a_sha256',C)]:
            bad=dict(valid);bad[key]=value
            with self.assertRaises(ledger.WorkflowError): self.gate(ctx,'pair_ready',subject=bad)
        self.gate(ctx,'pair_ready',subject=valid)
        changed=dict(valid);changed['pose_raw_sha256']=C;changed['endpoint_b_sha256']=C
        self.blocked('TASK_ENDPOINT_GATE_CONFLICT',self.gate,ctx,'pair_ready',subject=changed)
        self.assertEqual(before+1,ledger.inspect_task(self.manifest)['sequence'])

    def test_state_token_covers_reprice_gate_release_and_marker(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit');expected=self.expected(ctx,'pose_edit')
        ops=copy.deepcopy(ctx['ops']);ops[-1].update(quote_sha256=C,amount_usd='.02')
        ledger.reprice_plan(self.manifest,ctx['run'],ctx['aid'],ops,endpoint_plan=ctx['plan'])
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=expected)
        fresh=self.expected(ctx,'pose_edit');self.gate(ctx,'input_ready','hold')
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=fresh)
        self.gate(ctx,'input_ready');fresh=self.expected(ctx,'pose_edit')
        ledger.release_unsubmitted(self.manifest,ctx['aid']+':pose_edit','abandoned')
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=fresh)

    def test_ordered_release_marker_both_orders_unknown_retains_exposure(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit')
        self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':video_ab','abandon')
        ledger.mark_submission(self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':video_ab','abandon')
        self.blocked('TASK_SUBMISSION_UNRESOLVED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        ledger.record_outcome(self.manifest,ctx['aid']+':pose_edit',dict(status='failed',evidence='no id known'))
        self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':video_ab','abandon')
        self.assertEqual('0.93',ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.blocked('TASK_SUBMISSION_UNRESOLVED',self.setup_endpoint,'e2',dict(defect='failure',plan='different pose correction',evidence='proof'))

    def test_pose_failure_release_second_corrected_full_plan_and_third_forbidden(self):
        self.manifest=self.create('1.14');ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit','failed')
        correction=dict(defect='material mismatch',plan='restore source material in edit payload',evidence='failure and changed payload review')
        self.blocked('TASK_CORRECTION_REQUIRED',self.setup_endpoint,'e2',correction)
        for role in ('video_ab','video_ba','cleanup'): ledger.release_unsubmitted(self.manifest,ctx['aid']+':'+role,'known failed pose')
        self.assertEqual('1.02',ledger.inspect_task(self.manifest)['available_estimate_usd'])
        self.blocked('TASK_CORRECTION_REQUIRED',self.setup_endpoint,'e2')
        second=self.setup_endpoint('e2',correction,reserve=False)
        self.blocked('TASK_CORRECTION_REQUIRED',self.reserve_endpoint,second)
        second['plan']['pose_config_sha256']='3'*64;second['ops'][0]['config_sha256']='3'*64
        for op in second['ops'][1:]:op['deferred_config']['endpoint_plan_sha256']=self.digest(second['plan'])
        self.reserve_endpoint(second)
        self.assertEqual('0.09',ledger.inspect_task(self.manifest)['available_estimate_usd'])
        self.blocked('TASK_STRATEGY_EXHAUSTED',self.setup_endpoint,'e3',correction)
        self.blocked('TASK_POLICY_UNSUPPORTED',ledger.begin_attempt,self.manifest,'e2','e2:attempt-01',self.endpoint_descriptor(),3,correction)

    def test_ab_retained_point52_leaves_point62_and_new_plan_does_not_fit(self):
        self.manifest=self.create('1.14');ctx=self.setup_endpoint();self.advance_pair(ctx);self.submit(ctx,'video_ab','failed')
        for role in ('video_ba','cleanup'): ledger.release_unsubmitted(self.manifest,ctx['aid']+':'+role,'known failed AB')
        self.assertEqual('0.62',ledger.inspect_task(self.manifest)['available_estimate_usd'])
        second=self.setup_endpoint('e2',dict(defect='anatomy',plan='specific new pose edit direction',evidence='technical failure proof'),reserve=False,config='3'*64)
        self.blocked('TASK_BUDGET_EXCEEDED',self.reserve_endpoint,second)

    def test_reprice_whole_plan_no_refund_and_submitted_quote_frozen(self):
        ctx=self.setup_endpoint();self.advance_pair(ctx)
        changed=copy.deepcopy(ctx['ops']);changed[1].update(quote_sha256=C,amount_usd='.42')
        result=ledger.reprice_plan(self.manifest,ctx['run'],ctx['aid'],changed,endpoint_plan=ctx['plan'])
        self.assertEqual('0.02',result['newly_reserved_usd'])
        changed[1].update(quote_sha256=H,amount_usd='.39')
        ledger.reprice_plan(self.manifest,ctx['run'],ctx['aid'],changed,endpoint_plan=ctx['plan'])
        self.assertEqual('0.95',ledger.inspect_task(self.manifest)['reserved_total_usd'])
        changed[0].update(quote_sha256=C,amount_usd='.13')
        self.blocked('TASK_PLAN_SUBMITTED',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],changed,endpoint_plan=ctx['plan'])

    def test_attempt_active_hold_or_success_cannot_open_second_or_reuse_roles(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready','hold')
        self.blocked('TASK_CORRECTION_REQUIRED',self.setup_endpoint,'e2',dict(defect='material',plan='rewrite prompt',evidence='none'))
        for role in ('pose_edit','video_ab','video_ba','cleanup'):ledger.release_unsubmitted(self.manifest,ctx['aid']+':'+role,'aborted')
        self.blocked('TASK_CORRECTION_REQUIRED',self.setup_endpoint,'e2',dict(defect='material',plan='rewrite prompt',evidence='none'))

    def test_history_closed_equivalence_unknown_blocks_and_v1_sheet_distinct(self):
        for model,conditioning,want in [('fal-ai/nano-banana-2/edit','single_source',None),
            ('fal-ai/wan-flf2v','pose_edit_then_opposite_endpoints','TASK_HISTORY_MECHANISM_CLOSED'),
            ('fal-ai/wan-flf2v','opposite_endpoints','TASK_HISTORY_UNRESOLVED'),
            ('fal-ai/wan-flf2v',None,'TASK_HISTORY_UNRESOLVED')]:
            with self.subTest(model=model,conditioning=conditioning):
                bundle=self.history_bundle();record=bundle['attempts'][0]
                if conditioning!='single_source':record['mechanism'].update(route='video',model=model,conditioning=conditioning,resolution='legacy_unsupported',current_descriptor=None)
                self.history_task(bundle);ledger.import_task_history(self.manifest,bundle)
                if want:self.blocked(want,ledger.check_history_context,self.manifest,'wolf',self.endpoint_descriptor())
                else:self.assertIsNone(ledger.check_history_context(self.manifest,'wolf',self.endpoint_descriptor()))


    def test_endpoint_one_attempt_per_run_even_when_mixing_v1(self):
        ctx=self.setup_endpoint()
        self.blocked('TASK_STRATEGY_COLLISION',ledger.begin_attempt,self.manifest,'e1','ordinary',self.descriptor())
        self.run_bind('mixed');ledger.begin_attempt(self.manifest,'mixed','ordinary',self.descriptor())
        self.blocked('TASK_STRATEGY_COLLISION',ledger.begin_attempt,self.manifest,'mixed','mixed:attempt-01',self.endpoint_descriptor())

    def test_endpoint_requires_sealed_orientation_and_exact_frame_count(self):
        for field,value in [('action','walk'),('orientation','front'),('requirements',dict(frames=8.0))]:
            target=dict(target_id='wolf',source_sha256=H,action='run',orientation='left',requirements=dict(frames=8))
            target[field]=value
            auth=dict(task_id='strict-target',cap_usd='1',budget_basis='fal_operational_reservations',allowed_operations=list(ledger.OPERATION_KINDS),evidence_ref='fixture')
            self.manifest=ledger.create_task(self.root,auth,[target])['task_manifest'];self.run_bind('e1')
            self.blocked('TASK_POLICY_UNSUPPORTED',ledger.begin_attempt,self.manifest,'e1','e1:attempt-01',self.endpoint_descriptor())

    def test_known_id_resume_ignores_old_state_token_but_not_binding_identity(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit');old=self.expected(ctx,'pose_edit')
        self.submit(ctx,'pose_edit');self.gate(ctx,'input_ready','reject')
        result=ledger.mark_submission(self.manifest,ctx['aid']+':pose_edit',expected=old)
        self.assertFalse(result['submit_allowed'])
        bad=dict(old,binding_sha256=H)
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=bad)

    def test_reprice_raise_cap_failure_atomic_and_marker_first_freezes_quote(self):
        self.manifest=self.create('0.94');ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit')
        changed=copy.deepcopy(ctx['ops']);changed[-1].update(amount_usd='.03',quote_sha256=C)
        before=ledger.inspect_task(self.manifest)['sequence']
        self.blocked('TASK_BUDGET_EXCEEDED',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],changed,endpoint_plan=ctx['plan'])
        self.assertEqual(before,ledger.inspect_task(self.manifest)['sequence'])
        ledger.mark_submission(self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        changed=copy.deepcopy(ctx['ops']);changed[0].update(amount_usd='.13',quote_sha256=C)
        self.blocked('TASK_PLAN_SUBMITTED',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],changed,endpoint_plan=ctx['plan'])

    def test_os_concurrency_marker_vs_reprice_gate_release(self):
        for action in ('reprice','gate','release'):
            with self.subTest(action=action):
                self.manifest=self.create('1.14');ctx=self.setup_endpoint();gate=self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit')
                marker=dict(operation_id=ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
                if action=='reprice':
                    changed=copy.deepcopy(ctx['ops']);changed[0].update(amount_usd='.13',quote_sha256=C)
                    args=dict(run_id=ctx['run'],attempt_id=ctx['aid'],operations=changed,endpoint_plan=ctx['plan'])
                elif action=='gate':args=dict(run_id=ctx['run'],attempt_id=ctx['aid'],gate=dict(gate,decision='hold'))
                else:args=dict(operation_id=ctx['aid']+':pose_edit')
                mp=multiprocessing.get_context('spawn');start=mp.Event();results=mp.Queue()
                children=[mp.Process(target=endpoint_race_action,args=(self.manifest,name,payload,start,results)) for name,payload in [('mark',marker),(action,args)]]
                for child in children:child.start()
                start.set();outcomes=dict(results.get(timeout=30) for _ in children)
                for child in children:
                    child.join(30);self.assertEqual(0,child.exitcode)
                self.assertIn(outcomes['mark'],('ok','TASK_SUBMISSION_CONTEXT_MISMATCH'))
                if outcomes['mark']=='ok':self.assertIn(outcomes[action],('ok','TASK_PLAN_SUBMITTED','TASK_OPERATION_NOT_RELEASABLE'))
                else:self.assertEqual('ok',outcomes[action])
                op=ledger.inspect_task(self.manifest)['operations'][ctx['aid']+':pose_edit']
                self.assertFalse(op['submission_started'] and op['status']=='released')

    def test_frozen_final_history_bundle_replay_exact_finances_and_idempotence(self):
        # Read-only historical evidence; fresh journal lives exclusively in TemporaryDirectory.
        configured=os.environ.get('TOWER_SPRITE_PRIVATE_HISTORY_DIR')
        if not configured:self.skipTest('Private history is not distributed; set TOWER_SPRITE_PRIVATE_HISTORY_DIR to opt in')
        frozen=Path(configured)
        manifest=json.loads((frozen/'task.json').read_text('utf-8'))
        event=json.loads((frozen/'events'/'00000001.json').read_text('utf-8'));bundle=event['payload']['bundle']
        self.assertEqual('58f3d3a6d5d85d5746ecdd28ef1b6516097d0d53ba726d3b10ac0e6ff140b132',self.digest(bundle))
        task=ledger.create_task(self.root,manifest['authorization'],list(manifest['targets'].values()),historical_import=manifest['historical_import'])['task_manifest']
        ledger.import_task_history(task,bundle);first=ledger.inspect_task(task)
        self.assertEqual(115,first['historical_attempt_count']);self.assertEqual(81,len(first['imports']))
        self.assertEqual('28.092',first['reserved_total_usd']);self.assertEqual('29.232',first['cap_usd']);self.assertEqual('1.14',first['available_estimate_usd'])
        ledger.import_task_history(task,bundle);self.assertEqual(first['sequence'],ledger.inspect_task(task)['sequence'])
        descriptor=self.endpoint_descriptor();descriptor['inputs'][0]['sha256']=manifest['targets']['stone_golem-run']['source_sha256']
        self.assertIsNone(ledger.check_history_context(task,'stone_golem-run',descriptor))
        self.assertEqual(bundle,json.loads((Path(task).parent/'events'/'00000001.json').read_text('utf-8'))['payload']['bundle'])


    def test_gate_revocation_current_pass_dependency_and_terminal_final_review(self):
        ctx=self.setup_endpoint();self.advance_pair(ctx);self.submit(ctx,'video_ab');self.gate(ctx,'ab_ready')
        self.submit(ctx,'video_ba');cycle=self.gate(ctx,'cycle_ready');self.bind(ctx,'cleanup')
        self.gate(ctx,'ab_ready','hold')
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':cleanup',expected=self.expected(ctx,'cleanup'))
        self.gate(ctx,'ab_ready');self.submit(ctx,'cleanup')
        ledger.record_review(self.manifest,ctx['run'],ctx['aid'],'pass',[],'full physical review fixture')
        self.gate(ctx,'pair_ready','reject')
        a=self.state_attempt(ctx)
        self.assertEqual('pass',a['review']['decision'])
        self.assertEqual('rejected',a['endpoint_status'])
        self.assertIn('material: observed mismatch',a['effective_defects'])
        self.blocked('TASK_ATTEMPT_CLOSED',ledger.record_review,self.manifest,ctx['run'],ctx['aid'],'pass',[],'old pass cannot erase gate')
        # Same PASS subject with different evidence receives a distinct content hash.
        self.assertEqual('pass',cycle['decision'])

    def test_all_four_unknown_no_id_markers_are_nonrepeatable_and_not_releasable(self):
        for index,role in enumerate(('pose_edit','video_ab','video_ba','cleanup')):
            with self.subTest(role=role):
                self.manifest=self.create('1.14');ctx=self.setup_endpoint();self.gate(ctx,'input_ready')
                for earlier,stage in list(zip(('pose_edit','video_ab','video_ba'),('pair_ready','ab_ready','cycle_ready')))[:index]:
                    self.submit(ctx,earlier);self.gate(ctx,stage)
                self.bind(ctx,role);oid=ctx['aid']+':'+role
                ledger.mark_submission(self.manifest,oid,expected=self.expected(ctx,role))
                self.blocked('TASK_SUBMISSION_UNRESOLVED',ledger.mark_submission,self.manifest,oid,expected=self.expected(ctx,role))
                ledger.record_outcome(self.manifest,oid,dict(status='uncertain',evidence='marker without known ID'))
                self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,oid,'no proof of refund')
                if index<3:self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':'+('pose_edit','video_ab','video_ba','cleanup')[index+1],'ancestor unknown')
                self.assertEqual('0.93',ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_gate_marker_both_orders_preserve_atomic_boundary_and_release_closes_plan(self):
        # Gate first: stale snapshot fails; fresh snapshot sees HOLD.
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit');old=self.expected(ctx,'pose_edit')
        self.gate(ctx,'input_ready','hold')
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=old)
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        # Marker first: later HOLD cannot erase a marker/exposure, recovery stays GET-only.
        self.gate(ctx,'input_ready');ledger.mark_submission(self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        self.gate(ctx,'input_ready','hold')
        self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':video_ab','ancestor in flight')
        self.assertTrue(ledger.inspect_task(self.manifest)['operations'][ctx['aid']+':pose_edit']['submission_started'])
        self.manifest=self.create('1.14');ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit')
        for role in ('pose_edit','video_ab','video_ba','cleanup'):ledger.release_unsubmitted(self.manifest,ctx['aid']+':'+role,'aborted in order')
        self.blocked('TASK_ATTEMPT_CLOSED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        self.assertEqual('0',ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_gate_subject_hash_tracks_effective_pass_and_old_role_ids_do_not_form_new_plan(self):
        ctx=self.setup_endpoint();self.advance_pair(ctx);self.submit(ctx,'video_ab');ab=self.gate(ctx,'ab_ready')
        pair=self.state_attempt(ctx)['endpoint_gates']['pair_ready']
        new_pair=dict(pair,evidence=[self.history_ref('pair-new-review.json')]);ledger.record_endpoint_gate(self.manifest,ctx['run'],ctx['aid'],new_pair)
        self.bind(ctx,'video_ba')
        self.blocked('TASK_ENDPOINT_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':video_ba',expected=self.expected(ctx,'video_ba'))
        self.blocked('TASK_ENDPOINT_GATE_CONFLICT',self.gate,ctx,'ab_ready',subject=dict(ab['subject'],pair_gate_sha256=self.digest(new_pair)))
        self.gate(ctx,'pair_ready','reject')
        for role in ('video_ba','cleanup'):ledger.release_unsubmitted(self.manifest,ctx['aid']+':'+role,'quality rejected')
        second=self.setup_endpoint('e2',dict(defect='material: observed mismatch',plan='change material using source-specific constraints',evidence='review of changed payload'),reserve=False,config='3'*64)
        old_ids=copy.deepcopy(second['ops']);old_ids[1]['operation_id']=ctx['ops'][1]['operation_id']
        self.blocked('TASK_PLAN_INVALID',self.reserve_endpoint,second,old_ids)

    def test_closed_history_equivalence_exact_models_and_unknown_endpoint_blocks(self):
        for model in ('fal-ai/wan-motion','fal-ai/veo3.1/first-last-frame-to-video','fal-ai/vidu/q2/reference-to-video','fal-ai/kling-video/v3/pro/image-to-video','fal-ai/unreconciled'):
            with self.subTest(model=model):
                bundle=self.history_bundle();bundle['attempts'][0]['mechanism'].update(route='video',model=model,conditioning=None,resolution='legacy_unsupported',current_descriptor=None)
                self.history_task(bundle);ledger.import_task_history(self.manifest,bundle)
                if model=='fal-ai/unreconciled':self.blocked('TASK_HISTORY_UNRESOLVED',ledger.check_history_context,self.manifest,'wolf',self.endpoint_descriptor())
                else:self.assertIsNone(ledger.check_history_context(self.manifest,'wolf',self.endpoint_descriptor()))


    def test_opposite_endpoints_cannot_bind_same_raw_or_same_video_config(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit')
        subject=dict(endpoint_plan_sha256=self.digest(ctx['plan']),pose_request_id=ctx['aid']+':pose_edit:request',pose_raw_sha256='d'*64,endpoint_a_sha256='d'*64,endpoint_b_sha256='d'*64)
        self.blocked('TASK_ENDPOINT_GATE_CONFLICT',self.gate,ctx,'pair_ready',subject=subject)
        self.gate(ctx,'pair_ready');self.submit(ctx,'video_ab');self.gate(ctx,'ab_ready')
        # An input-set digest cannot excuse reusing AB's config digest for BA.
        binding=dict(schema='sprite_execution_binding_v2',config_sha256=C,config_file_sha256=C,
            input_set_sha256=self.digest([dict(role='start',sha256='e'*64),dict(role='end',sha256='d'*64)]),
            provenance_sha256=C,deferred_plan_sha256=self.digest(ctx['ops'][2]['deferred_config']))
        self.blocked('TASK_OPERATION_COLLISION',ledger.bind_operation_config,self.manifest,ctx['aid']+':video_ba',binding)


    def test_endpoint_gate_oversize_event_is_rejected_before_durable_write(self):
        ctx=self.setup_endpoint();gate=self.gate(ctx,'input_ready','hold')
        ref=dict(path=str(self.root/('x'*2800+'.json')),sha256=Q)
        huge=dict(gate,evidence=[ref]*1900)
        before=ledger.inspect_task(self.manifest)['sequence']
        self.blocked('TASK_SCHEMA_INVALID',ledger.record_endpoint_gate,self.manifest,ctx['run'],ctx['aid'],huge)
        self.assertEqual(before,ledger.inspect_task(self.manifest)['sequence'])

    def test_rejected_gate_orphan_event_recovers_closed_attempt_and_defects_together(self):
        ctx=self.setup_endpoint();self.gate(ctx,'input_ready')
        head=Path(self.manifest).parent/'head.json';before=head.read_bytes()
        self.gate(ctx,'input_ready','reject');head.write_bytes(before)
        result=self.state_attempt(ctx)
        self.assertEqual('rejected',result['endpoint_status']);self.assertIn('material: observed mismatch',result['effective_defects'])
        sequence=ledger.inspect_task(self.manifest)['sequence']
        self.gate(ctx,'input_ready','reject')
        state=ledger.inspect_task(self.manifest)
        self.assertFalse(state['recovery_required']);self.assertEqual(sequence,state['sequence'])


class SeedreamLedgerTests(unittest.TestCase):
    setUp = LedgerTests.setUp
    create = LedgerTests.create
    run_bind = LedgerTests.run_bind
    blocked = LedgerTests.blocked
    history_ref = LedgerTests.history_ref
    history_record = LedgerTests.history_record
    history_bundle = LedgerTests.history_bundle
    history_task = LedgerTests.history_task
    history_encoded = LedgerTests.history_encoded
    descriptor = LedgerTests.descriptor
    digest = EndpointLedgerTests.digest
    reserve_endpoint = EndpointLedgerTests.reserve_endpoint
    state_attempt = EndpointLedgerTests.state_attempt
    expected = EndpointLedgerTests.expected
    submit = EndpointLedgerTests.submit

    def seed_descriptor(self):
        return dict(schema='sprite_strategy_descriptor_v2', route='endpoint_cycle_v1',
                    family='seedream45_wan_flf2v_cycle_v1', model='fal-ai/wan-flf2v',
                    conditioning='valid_pose_edit_then_opposite_endpoints', inputs=[dict(role='source', sha256=H)])

    def seed_context(self, run='s1'):
        aid = run + ':attempt-01'
        plan = dict(schema='sprite_endpoint_plan_v2', variant='seedream45_a_identity_v1',
            pose_output_policy='seedream_rgb8_png_jpeg_to_png_v1',
            pose_decoder=dict(pillow_version='fixture-pillow', jpeg_codec_version='fixture-jpeg'),
            attempt_id=aid, source_sha256=H, pose_config_sha256=C,
            pose_inputs=[dict(role='edit', sha256='d'*64), dict(role='identity', sha256=H)],
            endpoint_a_sha256='d'*64, preparation_policy='pose_canvas_2048_v1',
            video_policy=dict(frames=81, fps=24, resolution='720p', aspect_ratio='1:1', steps=30,
                              acceleration='none', prompt_expansion=False),
            sampling_policy='endpoint_full_range_81_four_phases_v1',
            cleanup_policy=dict(model='fal-ai/ideogram/remove-background', sync_mode=False,
                                max_bytes=10485760, max_width=4096, max_height=4096),
            output_policy=dict(frames=8, frame_ms=100, loop=True, canvas=[2048,2048],
                               geometry_policy='common_nearest_2048_v1', alignment_policy='body_x_preserve_y_v1'))
        ops=[]
        for role, amount in [('pose_edit','0.04'),('video_ab','0.4'),('video_ba','0.4'),('cleanup','0.01')]:
            value=dict(operation_id=aid+':'+role, kind='alpha_edge_cleanup' if role=='cleanup' else 'provider_animation',
                       quote_sha256=Q, amount_usd=amount)
            if role == 'pose_edit': value['config_sha256']=C
            else: value['deferred_config']=dict(schema='sprite_endpoint_deferred_v1',endpoint_plan_sha256=self.digest(plan),role=role)
            ops.append(value)
        return dict(run=run, aid=aid, plan=plan, ops=ops)

    def begin_seed(self, ctx):
        self.run_bind(ctx['run'])
        ctx['strategy']=ledger.begin_attempt(self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),1,
                                            endpoint_plan_sha256=self.digest(ctx['plan']))
        return ctx

    def setup_seed(self, run='s1', reserve=True):
        ctx=self.begin_seed(self.seed_context(run))
        if reserve: self.reserve_endpoint(ctx)
        return ctx

    def gate(self, ctx, stage, decision='pass', subject=None):
        if subject is None:
            if stage == 'input_ready': subject=dict(endpoint_plan_sha256=self.digest(ctx['plan']))
            elif stage == 'pair_ready':
                subject=dict(endpoint_plan_sha256=self.digest(ctx['plan']),pose_request_id=ctx['aid']+':pose_edit:request',
                    pose_raw_sha256='e'*64,pose_output_proof_sha256='9'*64,
                    endpoint_a_sha256='d'*64,endpoint_b_sha256='8'*64)
            else:
                pair=self.state_attempt(ctx)['endpoint_gates']['pair_ready']
                subject=dict(pair_gate_sha256=self.digest(pair),ab_request_id=ctx['aid']+':video_ab:request',
                             ab_raw_sha256='f'*64,probe_sha256=C)
                if stage == 'cycle_ready':
                    ab=self.state_attempt(ctx)['endpoint_gates']['ab_ready']
                    subject=dict(pair_gate_sha256=self.digest(pair),ab_gate_sha256=self.digest(ab),
                        ab_request_id=ctx['aid']+':video_ab:request',ab_raw_sha256='f'*64,
                        ba_request_id=ctx['aid']+':video_ba:request',ba_raw_sha256='1'*64,selection_sha256=C,grid_sha256='2'*64)
        gate=dict(schema='sprite_endpoint_gate_v2',stage=stage,decision=decision,subject=subject,
                  defects=['material: observed mismatch'] if decision=='reject' else [],evidence=[self.history_ref(stage+'.json')])
        return ledger.record_endpoint_gate(self.manifest,ctx['run'],ctx['aid'],gate)

    def bind(self, ctx, role, inputs=None):
        values=dict(pose_edit=ctx['plan']['pose_inputs'],
                    video_ab=[dict(role='start',sha256='d'*64),dict(role='end',sha256='8'*64)],
                    video_ba=[dict(role='start',sha256='8'*64),dict(role='end',sha256='d'*64)],
                    cleanup=[dict(role='grid',sha256='2'*64)])
        return EndpointLedgerTests.bind(self,ctx,role,values[role] if inputs is None else inputs)

    def submit(self, ctx, role, status='completed'):
        self.bind(ctx,role)
        result=ledger.mark_submission(self.manifest,ctx['aid']+':'+role,expected=self.expected(ctx,role))
        self.assertTrue(result['submit_allowed'])
        evidence='pose-output-proof.json#sha256='+'9'*64 if role=='pose_edit' and status=='completed' else 'provider fixture'
        ledger.record_outcome(self.manifest,ctx['aid']+':'+role,
            dict(status=status,evidence=evidence,provider_request_id=ctx['aid']+':'+role+':request'))

    def test_seedream_atomic_085_plan_fits_090_but_not_009(self):
        self.manifest=self.create('0.09');ctx=self.setup_seed(reserve=False)
        self.blocked('TASK_BUDGET_EXCEEDED',self.reserve_endpoint,ctx)
        self.assertFalse(ledger.inspect_task(self.manifest)['operations'])
        self.manifest=self.create('0.90');ctx=self.setup_seed(reserve=False)
        self.blocked('TASK_PLAN_INVALID',self.reserve_endpoint,ctx,ctx['ops'][:3])
        self.assertFalse(ledger.inspect_task(self.manifest)['operations'])
        self.assertEqual('0.85',self.reserve_endpoint(ctx)['newly_reserved_usd'])
        self.assertEqual('0',self.reserve_endpoint(ctx)['newly_reserved_usd'])
        self.assertEqual('0.05',ledger.inspect_task(self.manifest)['available_estimate_usd'])

    def test_seedream_full_cycle_raw_and_png_hashes_are_distinct(self):
        ctx=self.setup_seed();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit');self.gate(ctx,'pair_ready')
        self.submit(ctx,'video_ab');self.gate(ctx,'ab_ready');self.submit(ctx,'video_ba');self.gate(ctx,'cycle_ready');self.submit(ctx,'cleanup')
        self.assertEqual(4,len(self.state_attempt(ctx)['endpoint_gates']))
        self.assertFalse(ledger.mark_submission(self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))['submit_allowed'])

    def test_seedream_max1_closed_family_and_no_second_uuid(self):
        ctx=self.setup_seed(reserve=False)
        self.run_bind('s2')
        self.blocked('TASK_STRATEGY_EXHAUSTED',ledger.begin_attempt,self.manifest,'s2','s2:attempt-01',self.seed_descriptor(),1,
                     endpoint_plan_sha256=self.digest(self.seed_context('s2')['plan']))
        for family,model,conditioning in [('seedream4_wan_flf2v_cycle_v1','fal-ai/wan-flf2v','valid_pose_edit_then_opposite_endpoints'),
                ('seedream45_wan_flf2v_cycle_v1','fal-ai/bytedance/seedream/v4.5/edit','valid_pose_edit_then_opposite_endpoints'),
                ('seedream45_wan_flf2v_cycle_v1','fal-ai/wan-flf2v','pose_edit_then_opposite_endpoints')]:
            descriptor=dict(self.seed_descriptor(),family=family,model=model,conditioning=conditioning)
            self.blocked('TASK_POLICY_UNSUPPORTED',ledger.begin_attempt,self.manifest,'s2','s2:attempt-01',descriptor,1)
        self.blocked('TASK_POLICY_UNSUPPORTED',ledger.begin_attempt,self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),2)

    def test_seedream_plan_rejects_alias_schema_edit_mismatch_and_bad_decoder(self):
        ctx=self.setup_seed(reserve=False)
        for changes in [dict(schema='sprite_endpoint_plan_v1'),dict(variant='seedream4_a_identity_v1'),
                dict(pose_output_policy='arbitrary'),dict(pose_decoder=dict(pillow_version='',jpeg_codec_version='x')),
                dict(pose_decoder=dict(pillow_version='x',jpeg_codec_version='x',interpreter='free')),
                dict(endpoint_a_sha256=Q)]:
            with self.subTest(changes=changes):
                self.blocked('TASK_PLAN_INVALID',self.reserve_endpoint,ctx,plan=dict(ctx['plan'],**changes))

    def test_seedream_v2_pair_requires_proof_and_b_different_from_a(self):
        ctx=self.setup_seed();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit')
        valid=dict(endpoint_plan_sha256=self.digest(ctx['plan']),pose_request_id=ctx['aid']+':pose_edit:request',
                   pose_raw_sha256='e'*64,pose_output_proof_sha256='9'*64,endpoint_a_sha256='d'*64,endpoint_b_sha256='8'*64)
        for key,value in [('pose_request_id','foreign'),('endpoint_a_sha256',Q),('endpoint_b_sha256','d'*64)]:
            self.blocked('TASK_ENDPOINT_GATE_CONFLICT',self.gate,ctx,'pair_ready',subject=dict(valid,**{key:value}))
        missing=dict(valid);missing.pop('pose_output_proof_sha256')
        self.blocked('TASK_SCHEMA_INVALID',self.gate,ctx,'pair_ready',subject=missing)
        self.gate(ctx,'pair_ready',subject=valid)
        self.blocked('TASK_OPERATION_COLLISION',self.bind,ctx,'video_ab',
            [dict(role='start',sha256='d'*64),dict(role='end',sha256='e'*64)])

    def test_seedream_history_seedream4_and_45_and_wan_aliases_block(self):
        for model,conditioning in [('fal-ai/bytedance/seedream/v4/edit',None),
                ('fal-ai/bytedance/seedream/v4.5/edit',None),('fal-ai/wan-flf2v','opposite_endpoints')]:
            with self.subTest(model=model):
                bundle=self.history_bundle();bundle['attempts'][0]['mechanism'].update(route='video',model=model,
                    conditioning=conditioning,resolution='legacy_unsupported',current_descriptor=None)
                self.history_task(bundle);ledger.import_task_history(self.manifest,bundle)
                self.blocked('TASK_HISTORY_MECHANISM_CLOSED',ledger.check_history_context,self.manifest,'wolf',self.seed_descriptor())

    def test_seedream_begin_replay_before_reserve_or_binding(self):
        ctx=self.setup_seed(reserve=False)
        self.assertEqual(ctx['strategy'],ledger.begin_attempt(self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),1,
                         endpoint_plan_sha256=self.digest(ctx['plan'])))
        self.reserve_endpoint(ctx)
        self.assertEqual(ctx['strategy'],ledger.begin_attempt(self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),1,
                         endpoint_plan_sha256=self.digest(ctx['plan'])))

    def nano_fixture(self):
        fixture=EndpointLedgerTests()
        fixture.root=self.root
        fixture.manifest=self.manifest
        return fixture

    def test_seedream_requires_nano_terminal_with_no_future_reservations(self):
        self.manifest=self.create('3')
        nano=self.nano_fixture();old=nano.setup_endpoint()
        self.blocked('TASK_SUBMISSION_UNRESOLVED',self.setup_seed,reserve=False)
        nano.gate(old,'input_ready');nano.submit(old,'pose_edit','failed')
        self.blocked('TASK_SUBMISSION_UNRESOLVED',self.setup_seed,reserve=False)
        for role in ('video_ab','video_ba','cleanup'):
            ledger.release_unsubmitted(self.manifest,old['aid']+':'+role,'fixture terminal release')
        ctx=self.setup_seed()
        self.assertEqual('failed',nano.state_attempt(old)['endpoint_status'])
        self.assertEqual(4,len(self.state_attempt(ctx)['plan_operation_ids']))
        self.assertEqual('0.97',ledger.inspect_task(self.manifest)['reserved_total_usd'])

    def test_seedream_foreign_wan_marker_without_id_closes_even_after_failed(self):
        for role in ('video_ab','video_ba'):
            with self.subTest(role=role):
                self.manifest=self.create('3');nano=self.nano_fixture();old=nano.setup_endpoint()
                nano.advance_pair(old)
                if role=='video_ba': nano.submit(old,'video_ab');nano.gate(old,'ab_ready')
                nano.bind(old,role);ledger.mark_submission(self.manifest,old['aid']+':'+role,expected=nano.expected(old,role))
                self.blocked('TASK_HISTORY_MECHANISM_CLOSED',self.setup_seed,reserve=False)
                ledger.record_outcome(self.manifest,old['aid']+':'+role,
                    dict(status='failed',evidence='fixture terminal WAN',provider_request_id='foreign-'+role))
                for future in (('video_ba','cleanup') if role=='video_ab' else ('cleanup',)):
                    ledger.release_unsubmitted(self.manifest,old['aid']+':'+future,'fixture terminal release')
                self.blocked('TASK_HISTORY_MECHANISM_CLOSED',self.setup_seed,reserve=False)

    def test_seedream_marker_rechecks_foreign_wan_after_begin_and_reserve(self):
        self.manifest=self.create('3');ctx=self.setup_seed();self.gate(ctx,'input_ready');self.bind(ctx,'pose_edit')
        nano=self.nano_fixture();other=nano.setup_endpoint();nano.advance_pair(other)
        nano.bind(other,'video_ab');ledger.mark_submission(self.manifest,other['aid']+':video_ab',expected=nano.expected(other,'video_ab'))
        self.blocked('TASK_SUBMISSION_UNRESOLVED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        ledger.record_outcome(self.manifest,other['aid']+':video_ab',dict(status='pending',evidence='foreign known WAN',provider_request_id='foreign-wan'))
        self.blocked('TASK_HISTORY_MECHANISM_CLOSED',ledger.mark_submission,self.manifest,ctx['aid']+':pose_edit',expected=self.expected(ctx,'pose_edit'))
        self.assertFalse(ledger.inspect_task(self.manifest)['operations'][ctx['aid']+':pose_edit']['submission_started'])

    def test_seedream_own_wan_begin_replay_and_known_id_recovery_never_resubmits(self):
        ctx=self.setup_seed();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit');self.gate(ctx,'pair_ready')
        self.submit(ctx,'video_ab');self.gate(ctx,'ab_ready')
        self.assertEqual(ctx['strategy'],ledger.begin_attempt(self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),1,
                         endpoint_plan_sha256=self.digest(ctx['plan'])))
        self.submit(ctx,'video_ba')
        result=ledger.mark_submission(self.manifest,ctx['aid']+':video_ab',expected=self.expected(ctx,'video_ab'))
        self.assertFalse(result['submit_allowed']);self.assertEqual(ctx['aid']+':video_ab:request',result['provider_request_id'])

    def test_seedream_failed_pose_consumes_attempt_and_releases_only_future_in_order(self):
        ctx=self.setup_seed();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit','failed')
        self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':video_ba','out of order')
        for role in ('video_ab','video_ba','cleanup'):
            ledger.release_unsubmitted(self.manifest,ctx['aid']+':'+role,'never submitted')
        self.assertEqual('0.04',ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.blocked('TASK_STRATEGY_EXHAUSTED',self.setup_seed,'another')

    def test_seedream_pair_rejects_unlinked_output_proof_and_gate_schema(self):
        ctx=self.setup_seed();self.gate(ctx,'input_ready');self.submit(ctx,'pose_edit')
        subject=dict(endpoint_plan_sha256=self.digest(ctx['plan']),pose_request_id=ctx['aid']+':pose_edit:request',
                     pose_raw_sha256='e'*64,pose_output_proof_sha256=Q,endpoint_a_sha256='d'*64,endpoint_b_sha256='8'*64)
        self.blocked('TASK_ENDPOINT_GATE_CONFLICT',self.gate,ctx,'pair_ready',subject=subject)
        gate=dict(schema='sprite_endpoint_gate_v1',stage='input_ready',decision='pass',
                  subject=dict(endpoint_plan_sha256=self.digest(ctx['plan'])),defects=[],evidence=[self.history_ref()])
        self.blocked('TASK_SCHEMA_INVALID',ledger.record_endpoint_gate,self.manifest,ctx['run'],ctx['aid'],gate)

    def test_seedream_begin_requires_immutable_acyclic_scope_plan_commitment(self):
        ctx=self.seed_context();self.run_bind(ctx['run'])
        for value in (None,'invalid','A'*64):
            self.blocked('TASK_PLAN_INVALID',ledger.begin_attempt,self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),1,
                         endpoint_plan_sha256=value)
        ctx=self.begin_seed(ctx)
        self.blocked('TASK_STRATEGY_COLLISION',ledger.begin_attempt,self.manifest,ctx['run'],ctx['aid'],self.seed_descriptor(),1,
                     endpoint_plan_sha256=Q)
        altered=dict(ctx['plan'],pose_config_sha256=Q)
        self.blocked('TASK_PLAN_COLLISION',self.reserve_endpoint,ctx,plan=altered)
        changed=dict(ctx['plan'],pose_decoder=dict(pillow_version='another',jpeg_codec_version='another'))
        self.blocked('TASK_PLAN_COLLISION',self.reserve_endpoint,ctx,plan=changed)
        self.assertFalse(ledger.inspect_task(self.manifest)['operations'])
        self.reserve_endpoint(ctx)
        self.blocked('TASK_PLAN_COLLISION',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],ctx['ops'],endpoint_plan=changed)
        self.blocked('TASK_SCOPE_COLLISION',ledger.bind_run,self.manifest,
                     dict(run_id=ctx['run'],target_id='wolf',scope_sha256=Q,manifest_path='/data/s1/run.json'))

    def test_seedream_commitment_cannot_be_added_to_nano_or_v1(self):
        self.run_bind('plain')
        for descriptor in (self.descriptor(),EndpointLedgerTests.endpoint_descriptor(self)):
            self.blocked('TASK_POLICY_UNSUPPORTED',ledger.begin_attempt,self.manifest,'plain','plain:attempt-01',descriptor,2,
                         endpoint_plan_sha256=Q)



def regional_race_action(manifest, action, args, start, result):
    import sprite_task_ledger as child
    start.wait(15)
    try:
        if action == 'begin':
            child.begin_attempt(manifest,args['run'],args['aid'],args['descriptor'],1,regional_plan_sha256=args['plan_sha256'])
        elif action == 'mark':
            child.mark_submission(manifest,args['operation_id'],expected=args['expected'])
        elif action == 'reprice':
            child.reprice_plan(manifest,args['run'],args['aid'],args['ops'],regional_plan=args['plan'])
        elif action == 'gate':
            child.record_regional_gate(manifest,args['run'],args['aid'],args['gate'])
        elif action == 'release':
            child.release_unsubmitted(manifest,args['operation_id'],'race abandon')
        result.put((action,'ok'))
    except child.WorkflowError as exc:
        result.put((action,exc.code))


class RegionalLedgerTests(unittest.TestCase):
    setUp = LedgerTests.setUp
    create = LedgerTests.create
    run_bind = LedgerTests.run_bind
    blocked = LedgerTests.blocked
    history_ref = LedgerTests.history_ref
    history_record = LedgerTests.history_record
    history_bundle = LedgerTests.history_bundle
    history_task = LedgerTests.history_task
    history_encoded = LedgerTests.history_encoded
    descriptor = LedgerTests.descriptor
    digest = EndpointLedgerTests.digest

    def regional_descriptor(self):
        return dict(schema='sprite_regional_strategy_v1', route='regional_video_inpaint_v1',
                    family='spatial_video_mask_reference_v1', model='fal-ai/wan-vace-14b/inpainting',
                    conditioning='video_mask_reference', inputs=[dict(role='source', sha256=H)])

    def regional_history(self, cap='1.26', model=None, conditioning=None):
        bundle = self.history_bundle()
        if model:
            bundle['attempts'][0]['mechanism'].update(route='video', model=model, conditioning=conditioning,
                resolution='legacy_unsupported', current_descriptor=None)
        self.history_task(bundle, cap)
        ledger.import_task_history(self.manifest, bundle)
        return bundle

    def regional_context(self, run='region1'):
        self.run_bind(run)
        aid = run + ':attempt-01'
        geometry = dict(policy='inverse_parent_preparation_v1', resize_canvas=[1880,1880],
                        crop=[0,189,1880,1500], output_canvas=[1880,1500],
                        scale_numerator=1880, scale_denominator=1440)
        plan = dict(schema='sprite_regional_plan_v1', attempt_id=aid, source_sha256=H,
            historical_source=dict(history_bundle_sha256=ledger.inspect_task(self.manifest)['historical_import']['bundle_sha256'],history_id='old-generation'),
            provenance=self.history_ref('provenance.json'), media=self.history_ref('media.json'),
            preflight=self.history_ref('preflight.json'), primary_config_sha256=C,
            primary_inputs=[dict(role=role,sha256=sha) for role,sha in [('video','d'*64),('mask','e'*64),('identity',H)]],
            profile='vace81_24_720_v1', cleanup_policy=dict(model='fal-ai/ideogram/remove-background',
                sync_mode=False,max_bytes=10485760,max_width=4096,max_height=4096),
            output_policy=dict(frames=8,frame_ms=100,loop=True,grounded=False,canvas=[1880,1500],
                selection=[0,3,6,9,13,16,19,22],geometry=geometry,alignment_policy='body_x_preserve_y_v1'))
        ops=[dict(operation_id=aid+':primary',kind='provider_animation',quote_sha256=Q,amount_usd='0.56',config_sha256=C),
             dict(operation_id=aid+':cleanup',kind='alpha_edge_cleanup',quote_sha256=Q,amount_usd='0.01',
                  deferred_config=dict(schema='sprite_regional_deferred_v1',regional_plan_sha256=self.digest(plan),role='cleanup'))]
        return dict(run=run,aid=aid,plan=plan,ops=ops)

    def begin_regional(self, ctx):
        ctx['strategy']=ledger.begin_attempt(self.manifest,ctx['run'],ctx['aid'],self.regional_descriptor(),1,
                                            regional_plan_sha256=self.digest(ctx['plan']))
        return ctx['strategy']

    def reserve_regional(self, ctx, ops=None, plan=None):
        return ledger.reserve_plan(self.manifest,ctx['run'],ctx['aid'],ctx['strategy'],
                                   ctx['ops'] if ops is None else ops,regional_plan=ctx['plan'] if plan is None else plan)

    def attempt_state(self, ctx):
        return next(a for a in ledger.inspect_task(self.manifest)['attempts'].values() if a['attempt_id']==ctx['aid'])

    def gate(self, ctx, stage='input_ready', decision='pass', subject=None):
        if subject is None:
            subject=dict(regional_plan_sha256=self.digest(ctx['plan']))
            if stage=='input_ready': subject.update(preflight_sha256=Q,media_sha256=Q)
            else: subject.update(request_id='regional-request',raw_sha256='1'*64,probe_sha256='2'*64,
                selection_sha256='6'*64,grid_sha256='3'*64,result_transform_sha256='4'*64)
        checks=['provenance','media','preflight'] if stage=='input_ready' else [
            'identity','anatomy','scale','material','orientation','framewise_motion','mask_scope','exterior','crop_background','loop_selection']
        gate=dict(schema='sprite_regional_gate_v1',stage=stage,decision=decision,subject=subject,
            evidence=self.history_ref(stage+'.json'),findings=[dict(check=check,passed=decision=='pass',notes='review fixture') for check in checks])
        ledger.record_regional_gate(self.manifest,ctx['run'],ctx['aid'],gate)
        return gate

    def bind(self, ctx, role='primary', **changes):
        op=ctx['ops'][0 if role=='primary' else 1]
        inputs=ctx['plan']['primary_inputs'] if role=='primary' else [dict(role='grid',sha256='3'*64)]
        binding=dict(schema='sprite_regional_execution_binding_v1',config_sha256=C,config_file_sha256=C,
            input_set_sha256=self.digest(inputs),provenance_sha256=Q,
            deferred_plan_sha256=None if role=='primary' else self.digest(op['deferred_config']))
        binding.update(changes)
        return ledger.bind_operation_config(self.manifest,op['operation_id'],binding)

    def expected(self, ctx, role='primary'):
        state=ledger.inspect_task(self.manifest); op=state['operations'][ctx['aid']+':'+role]
        return dict(schema='sprite_regional_submission_expected_v1',config_sha256=C,binding_sha256=op['binding_sha256'],
            quote_sha256=op['current_quote']['quote_sha256'],amount_usd=op['current_quote']['amount_usd'],
            regional_state_sha256=ledger.regional_state_token(state,ctx['run'],ctx['aid']))

    def ready(self, cap='1.26'):
        self.regional_history(cap);ctx=self.regional_context();self.begin_regional(ctx);self.reserve_regional(ctx)
        self.gate(ctx);self.bind(ctx)
        return ctx

    def test_regional_quota_across_runs(self):
        self.regional_history();first=self.regional_context();self.begin_regional(first)
        self.reserve_regional(first)
        ledger.release_unsubmitted(self.manifest,first['aid']+':primary','cancelled before marker')
        ledger.release_unsubmitted(self.manifest,first['aid']+':cleanup','parent released')
        second=self.regional_context('region2')
        self.blocked('TASK_STRATEGY_EXHAUSTED',self.begin_regional,second)
        self.assertEqual(1,len(ledger.inspect_task(self.manifest)['attempts']))

    def test_regional_reserve_atomic_cap(self):
        for cap,allowed in [('1.26',True),('0.96',False)]:
            self.regional_history(cap);ctx=self.regional_context();self.begin_regional(ctx)
            if allowed:
                self.assertEqual('0.57',self.reserve_regional(ctx)['newly_reserved_usd'])
                self.assertEqual('0.29',ledger.inspect_task(self.manifest)['available_estimate_usd'])
                self.assertEqual('0',self.reserve_regional(ctx)['newly_reserved_usd'])
            else:
                before=ledger.inspect_task(self.manifest)['sequence']
                self.blocked('TASK_BUDGET_EXCEEDED',self.reserve_regional,ctx)
                self.assertEqual({},ledger.inspect_task(self.manifest)['operations'])
                self.assertEqual(before,ledger.inspect_task(self.manifest)['sequence'])

    def test_regional_stale_token(self):
        ctx=self.ready();old=self.expected(ctx)
        self.gate(ctx,decision='hold')
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':primary',expected=old)
        self.gate(ctx);fresh=self.expected(ctx)
        result=ledger.mark_submission(self.manifest,ctx['aid']+':primary',expected=fresh)
        self.assertTrue(result['submit_allowed']);self.assertNotEqual(fresh['regional_state_sha256'],result['regional_state_sha256'])
        self.blocked('TASK_SUBMISSION_UNRESOLVED',ledger.mark_submission,self.manifest,ctx['aid']+':primary',expected=self.expected(ctx))
        ledger.record_outcome(self.manifest,ctx['aid']+':primary',dict(status='completed',provider_request_id='regional-request',evidence='fixture'))
        self.assertFalse(ledger.mark_submission(self.manifest,ctx['aid']+':primary',expected=fresh)['submit_allowed'])

    def test_regional_history_binding(self):
        for model,conditioning,want in [('fal-ai/wan-vace-14b/inpainting','video_mask_reference','TASK_HISTORY_MECHANISM_CLOSED'),
            ('fal-ai/unreconciled',None,'TASK_HISTORY_UNRESOLVED')]:
            self.regional_history(model=model,conditioning=conditioning);ctx=self.regional_context()
            self.blocked(want,self.begin_regional,ctx)
        self.regional_history();ctx=self.regional_context();self.begin_regional(ctx);self.reserve_regional(ctx)
        self.blocked('TASK_OPERATION_COLLISION',self.bind,ctx,provenance_sha256=C)
        self.blocked('TASK_OPERATION_COLLISION',self.bind,ctx,input_set_sha256=C)
        self.blocked('TASK_GATE_REQUIRED',self.bind,ctx,'cleanup')

    def test_regional_cleanup_requires_completed_primary_and_exact_raw_gate(self):
        ctx=self.ready();self.blocked('TASK_PARENT_OPERATION_INCOMPLETE',self.gate,ctx,'raw_ready')
        ledger.mark_submission(self.manifest,ctx['aid']+':primary',expected=self.expected(ctx))
        ledger.record_outcome(self.manifest,ctx['aid']+':primary',dict(status='completed',provider_request_id='regional-request',evidence='fixture'))
        self.gate(ctx,'raw_ready');self.bind(ctx,'cleanup')
        self.assertTrue(ledger.mark_submission(self.manifest,ctx['aid']+':cleanup',expected=self.expected(ctx,'cleanup'))['submit_allowed'])
        ledger.record_outcome(self.manifest,ctx['aid']+':cleanup',dict(status='completed',provider_request_id='cleanup-request',evidence='fixture'))
        ledger.record_review(self.manifest,ctx['run'],ctx['aid'],'pass',[],'full review fixture')
        self.assertEqual('pass',self.attempt_state(ctx)['review']['decision'])

    def test_regional_reprice_no_refund_and_plan_immutable(self):
        ctx=self.ready();old=self.expected(ctx);ops=copy.deepcopy(ctx['ops']);ops[0].update(amount_usd='0.2',quote_sha256='5'*64)
        self.assertEqual('0',ledger.reprice_plan(self.manifest,ctx['run'],ctx['aid'],ops,regional_plan=ctx['plan'])['newly_reserved_usd'])
        self.assertEqual('0.97',ledger.inspect_task(self.manifest)['reserved_total_usd'])
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':primary',expected=old)
        changed=copy.deepcopy(ctx['plan']);changed['primary_config_sha256']='6'*64
        self.blocked('TASK_PLAN_COLLISION',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],ops,regional_plan=changed)

    def test_regional_historical_source_exact_seal_target_and_kind(self):
        for change in [dict(history_bundle_sha256=C),dict(history_id='missing')]:
            self.regional_history();ctx=self.regional_context();ctx['plan']['historical_source'].update(change)
            ctx['ops'][1]['deferred_config']['regional_plan_sha256']=self.digest(ctx['plan'])
            self.begin_regional(ctx)
            self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH',self.reserve_regional,ctx)
        self.regional_history();ctx=self.regional_context();ctx['plan']['historical_source']['extra']=True
        self.begin_regional(ctx);self.blocked('TASK_PLAN_INVALID',self.reserve_regional,ctx)
        self.regional_history();ctx=self.regional_context();self.run_bind('other-target','golem')
        ctx['run']='other-target';ctx['aid']='other-target:attempt-01';ctx['plan']['attempt_id']=ctx['aid']
        self.begin_regional(ctx);self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH',self.reserve_regional,ctx)

    def test_regional_begin_needs_complete_sealed_history(self):
        self.run_bind('r1')
        self.blocked('TASK_HISTORY_REQUIRED',ledger.begin_attempt,self.manifest,'r1','r1:attempt-01',self.regional_descriptor(),1,regional_plan_sha256=Q)

    def test_regional_closed_policy_rejects_boolean_numeric_alias(self):
        self.regional_history();ctx=self.regional_context();ctx['plan']['cleanup_policy']['sync_mode']=0
        ctx['ops'][1]['deferred_config']['regional_plan_sha256']=self.digest(ctx['plan']);self.begin_regional(ctx)
        self.blocked('TASK_PLAN_INVALID',self.reserve_regional,ctx)

    def test_regional_reject_findings_survive_replay_as_defects(self):
        ctx=self.ready();self.gate(ctx,decision='reject')
        attempt=self.attempt_state(ctx)
        self.assertEqual('rejected',attempt['regional_status'])
        self.assertIn('preflight: review fixture',attempt['effective_defects'])
        before=ledger.inspect_task(self.manifest)['sequence']
        self.blocked('TASK_ATTEMPT_CLOSED',self.gate,ctx)
        self.assertEqual(before,ledger.inspect_task(self.manifest)['sequence'])

    def test_regional_unknown_marker_retained_and_orphan_head_recovers(self):
        ctx=self.ready();head=Path(self.manifest).parent/'head.json';previous=head.read_bytes()
        ledger.mark_submission(self.manifest,ctx['aid']+':primary',expected=self.expected(ctx))
        durable=head.read_bytes();head.write_bytes(previous)
        state=ledger.inspect_task(self.manifest);self.assertTrue(state['recovery_required'])
        self.blocked('TASK_OPERATION_NOT_RELEASABLE',ledger.release_unsubmitted,self.manifest,ctx['aid']+':primary','unknown')
        ledger.record_outcome(self.manifest,ctx['aid']+':primary',dict(status='uncertain',evidence='lost provider response'))
        state=ledger.inspect_task(self.manifest)
        self.assertFalse(state['recovery_required']);self.assertNotEqual(previous,head.read_bytes())
        self.assertEqual('0.97',state['reserved_total_usd'])
        self.assertEqual('uncertain',state['operations'][ctx['aid']+':primary']['status'])
        self.blocked('TASK_SUBMISSION_UNRESOLVED',ledger.mark_submission,self.manifest,ctx['aid']+':primary',expected=self.expected(ctx))

    def test_regional_gate_subjects_findings_and_pass_revocation(self):
        ctx=self.ready();gate=self.gate(ctx);sequence=ledger.inspect_task(self.manifest)['sequence']
        for changes in [dict(subject=dict(gate['subject'],media_sha256=C)),dict(evidence=dict(path='relative',sha256=Q)),
                        dict(findings=[dict(check='preflight',passed=False,notes='failure')]),dict(extra=True)]:
            with self.assertRaises(ledger.WorkflowError):
                ledger.record_regional_gate(self.manifest,ctx['run'],ctx['aid'],dict(gate,**changes))
            self.assertEqual(sequence,ledger.inspect_task(self.manifest)['sequence'])
        ledger.mark_submission(self.manifest,ctx['aid']+':primary',expected=self.expected(ctx))
        ledger.record_outcome(self.manifest,ctx['aid']+':primary',dict(status='completed',provider_request_id='regional-request',evidence='fixture'))
        raw=self.gate(ctx,'raw_ready');self.bind(ctx,'cleanup');expected=self.expected(ctx,'cleanup')
        self.gate(ctx,decision='hold')
        self.blocked('TASK_SUBMISSION_CONTEXT_MISMATCH',ledger.mark_submission,self.manifest,ctx['aid']+':cleanup',expected=expected)
        self.blocked('TASK_GATE_REQUIRED',ledger.mark_submission,self.manifest,ctx['aid']+':cleanup',expected=self.expected(ctx,'cleanup'))
        self.gate(ctx);bad=dict(raw,findings=raw['findings'][:-1])
        self.blocked('TASK_SCHEMA_INVALID',ledger.record_regional_gate,self.manifest,ctx['run'],ctx['aid'],bad)
        bad=dict(raw,subject=dict(raw['subject'],request_id='foreign'))
        self.blocked('TASK_GATE_SUBJECT_MISMATCH',ledger.record_regional_gate,self.manifest,ctx['run'],ctx['aid'],bad)

    def test_regional_commitment_scope_alias_and_released_quota_are_closed(self):
        self.regional_history();ctx=self.regional_context();self.begin_regional(ctx)
        for plan_change in [dict(primary_config_sha256=Q),dict(media=self.history_ref('new-mask.json'))]:
            altered=dict(ctx['plan'],**plan_change)
            self.blocked('TASK_STRATEGY_COLLISION',ledger.begin_attempt,self.manifest,ctx['run'],ctx['aid'],self.regional_descriptor(),1,
                         regional_plan_sha256=self.digest(altered))
        descriptor=self.regional_descriptor();descriptor['family']='renamed-family'
        self.blocked('TASK_POLICY_UNSUPPORTED',ledger.begin_attempt,self.manifest,ctx['run'],ctx['aid'],descriptor,1,regional_plan_sha256=self.digest(ctx['plan']))
        self.blocked('TASK_SCHEMA_INVALID',ledger.reserve_plan,self.manifest,ctx['run'],ctx['aid'],ctx['strategy'],ctx['ops'],
                     regional_plan=ctx['plan'],endpoint_plan={})
        self.assertEqual(1,len(ledger.inspect_task(self.manifest)['attempts']))

    def test_regional_os_quota_and_marker_races(self):
        mp=multiprocessing.get_context('spawn')
        self.regional_history();contexts=[self.regional_context(run) for run in ('race1','race2')]
        start=mp.Event();results=mp.Queue()
        children=[mp.Process(target=regional_race_action,args=(self.manifest,'begin',dict(run=c['run'],aid=c['aid'],
            descriptor=self.regional_descriptor(),plan_sha256=self.digest(c['plan'])),start,results)) for c in contexts]
        for child in children:child.start()
        start.set();outcomes=[results.get(timeout=20) for _ in children]
        for child in children:child.join(20);self.assertEqual(0,child.exitcode)
        self.assertEqual(['TASK_STRATEGY_EXHAUSTED','ok'],sorted(result for _,result in outcomes))
        self.assertEqual(1,len(ledger.inspect_task(self.manifest)['attempts']))
        for action in ('reprice','gate','release'):
            ctx=self.ready();expected=self.expected(ctx);ops=copy.deepcopy(ctx['ops'])
            ops[0].update(quote_sha256='9'*64,amount_usd='0.57')
            held=self.gate(ctx);held=dict(held,decision='hold')
            args=dict(run=ctx['run'],aid=ctx['aid'],ops=ops,plan=ctx['plan'],gate=held,operation_id=ctx['aid']+':primary')
            marker=dict(operation_id=ctx['aid']+':primary',expected=expected)
            start=mp.Event();results=mp.Queue()
            children=[mp.Process(target=regional_race_action,args=(self.manifest,name,payload,start,results))
                      for name,payload in [('mark',marker),(action,args)]]
            for child in children:child.start()
            start.set();outcomes=dict(results.get(timeout=20) for _ in children)
            for child in children:child.join(20);self.assertEqual(0,child.exitcode)
            state=ledger.inspect_task(self.manifest);primary=state['operations'][ctx['aid']+':primary']
            if outcomes['mark']=='ok':
                self.assertTrue(primary['submission_started']);self.assertEqual('0.56',primary['current_quote']['amount_usd'])
                if action!='gate': self.assertNotEqual('ok',outcomes[action])
            else:
                self.assertEqual('ok',outcomes[action]);self.assertFalse(primary['submission_started'])
            self.assertIn(outcomes['mark'],('ok','TASK_SUBMISSION_CONTEXT_MISMATCH','TASK_ATTEMPT_CLOSED'))

    def test_regional_spatial_history_alias_cannot_hide_as_local_derivation(self):
        bundle=self.history_bundle()
        local=self.history_record('old-spatial',kind='local_derivation')
        local.update(run_id='old-local',reservation_ids=[],cost_basis='local_no_new_reservation')
        local['mechanism']=dict(origin='local',route='regional_video_inpaint_v1',model='historic-alias',
            conditioning='video_mask_reference',resolution='legacy_unsupported',current_descriptor=None)
        bundle['attempts'].append(local);self.history_task(bundle);ledger.import_task_history(self.manifest,bundle)
        ctx=self.regional_context();self.blocked('TASK_HISTORY_MECHANISM_CLOSED',self.begin_regional,ctx)

    def test_regional_nonprovider_history_cannot_be_the_raw_parent(self):
        bundle=self.history_bundle();local=self.history_record('local-parent',kind='local_derivation')
        local.update(run_id='old-local',reservation_ids=[],cost_basis='local_no_new_reservation')
        local['mechanism']=dict(origin='local',route='body_x_alignment',model='body_x_preserve_y_v1',
            conditioning='local',resolution='current_descriptor',current_descriptor=self.descriptor('body_x_alignment'))
        bundle['attempts'].append(local);self.history_task(bundle);ledger.import_task_history(self.manifest,bundle)
        ctx=self.regional_context();ctx['plan']['historical_source']['history_id']='local-parent';self.begin_regional(ctx)
        self.blocked('TASK_SOURCE_ATTEMPT_MISMATCH',self.reserve_regional,ctx)

    def test_regional_reprice_budget_failure_atomic_and_marker_freezes_price(self):
        ctx=self.ready('0.98');ops=copy.deepcopy(ctx['ops']);ops[0].update(quote_sha256='7'*64,amount_usd='0.58')
        before=ledger.inspect_task(self.manifest)
        self.blocked('TASK_BUDGET_EXCEEDED',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],ops,regional_plan=ctx['plan'])
        after=ledger.inspect_task(self.manifest);self.assertEqual(before['sequence'],after['sequence'])
        self.assertEqual(before['operations'],after['operations'])
        ledger.mark_submission(self.manifest,ctx['aid']+':primary',expected=self.expected(ctx))
        ops[0]['amount_usd']='0.55'
        self.blocked('TASK_PLAN_SUBMITTED',ledger.reprice_plan,self.manifest,ctx['run'],ctx['aid'],ops,regional_plan=ctx['plan'])

    def test_regional_geometry_accepts_profile_width_positive_integers(self):
        for width in (1440,1024):
            with self.subTest(width=width):
                self.regional_history();ctx=self.regional_context()
                ctx['plan']['output_policy']['geometry']['scale_denominator']=width
                ctx['ops'][1]['deferred_config']['regional_plan_sha256']=self.digest(ctx['plan'])
                self.begin_regional(ctx)
                self.assertEqual('0.57',self.reserve_regional(ctx)['newly_reserved_usd'])
                self.assertEqual(width,self.attempt_state(ctx)['regional_plan']['output_policy']['geometry']['scale_denominator'])

    def test_regional_geometry_rejects_nonpositive_or_noninteger_denominators(self):
        for width in (0,-1,True,False,1024.0,1024.5,'1024',None):
            with self.subTest(width=width):
                self.regional_history();ctx=self.regional_context()
                ctx['plan']['output_policy']['geometry']['scale_denominator']=width
                ctx['ops'][1]['deferred_config']['regional_plan_sha256']=self.digest(ctx['plan'])
                self.begin_regional(ctx)
                before=ledger.inspect_task(self.manifest)['sequence']
                self.blocked('TASK_PLAN_INVALID',self.reserve_regional,ctx)
                after=ledger.inspect_task(self.manifest)
                self.assertEqual({},after['operations']);self.assertEqual(before,after['sequence'])

    def test_regional_geometry_shapes_reject_numeric_aliases(self):
        for field,value in [('resize_canvas',[1880.0,1880]),('output_canvas',[1880,1500.0])]:
            self.regional_history();ctx=self.regional_context();ctx['plan']['output_policy']['geometry'][field]=value
            ctx['ops'][1]['deferred_config']['regional_plan_sha256']=self.digest(ctx['plan']);self.begin_regional(ctx)
            self.blocked('TASK_PLAN_INVALID',self.reserve_regional,ctx)

    def test_regional_closed_contract_and_commitment(self):
        self.regional_history();ctx=self.regional_context()
        self.blocked('TASK_PLAN_INVALID',ledger.begin_attempt,self.manifest,ctx['run'],ctx['aid'],self.regional_descriptor(),1)
        self.begin_regional(ctx)
        for path,value in [('profile','unknown'),('source_sha256',Q),('extra',True)]:
            altered=copy.deepcopy(ctx['plan']);altered[path]=value
            self.blocked('TASK_PLAN_INVALID' if path!='source_sha256' else 'TASK_PLAN_INVALID',self.reserve_regional,ctx,plan=altered)
        self.blocked('TASK_PLAN_INVALID',self.reserve_regional,ctx,ctx['ops'][:1])
        self.reserve_regional(ctx)
        old=self.attempt_state(ctx)['regional_plan_sha256']
        self.assertEqual(self.digest(ctx['plan']),old)
        self.assertEqual(ctx['strategy'],self.begin_regional(ctx))


if __name__ == '__main__':
    unittest.main()
