"""Private bridge protocol against real sealed scopes, files and the OS ledger."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sprite_task_ledger as ledger
from sheet_workflow_state import encoded, new_or_identical, run_lock, scope_sha
try:
    import sprite_task_bridge as bridge
except ImportError:
    bridge = None


def digest(value):
    return hashlib.sha256(value).hexdigest()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.build_fixture()

    def test_endpoint_dispatch_remains_fixed_and_propagates_local_blocks(self):
        # Endpoint config5 uses the same stdin protocol and single authority;
        # no alternate callback/interpreter is accepted from a message.
        import endpoint_cycle_contract as endpoint
        message=dict(schema='sprite_transport_context_v2')
        with patch.object(endpoint,'bridge_run',side_effect=endpoint.WorkflowError('SEEDREAM_DECODER_CHANGED')) as fixed:
            with self.assertRaises(endpoint.WorkflowError) as raised:bridge.check_context(message)
            self.assertEqual(raised.exception.code,'SEEDREAM_DECODER_CHANGED')
            fixed.assert_called_once_with(message,False)

    def build_fixture(self, route='sheet'):
        self.assertIsNotNone(bridge, 'private managed bridge is missing')
        self.route = route
        self.temp = tempfile.TemporaryDirectory(prefix='sprite-bridge-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_root = self.root / 'run1'
        self.run_root.mkdir()
        self.source = self.run_root / 'source.png'
        Image.new('RGBA', (16, 16), (80, 110, 140, 255)).save(self.source)
        self.original = digest(self.source.read_bytes())
        self.task = ledger.create_task(self.root / 'tasks', dict(task_id='human1', cap_usd='1',
            budget_basis='fal_operational_reservations', allowed_operations=list(ledger.OPERATION_KINDS), evidence_ref='user:7'),
            [dict(target_id='wolf', source_sha256=self.original, action='run', orientation='left', requirements={'frames': 8})])
        self.ref = dict(schema='sprite_task_ref_v1', task_id='human1', task_manifest=self.task['task_manifest'],
                        authorization_sha256=self.task['authorization_sha256'], target_id='wolf')
        self.attempt_id = 'run1:attempt-01'
        self.primary_id = self.attempt_id + ':primary'
        self.cleanup_id = self.attempt_id + ':cleanup'
        self.attempt_root = self.run_root / 'attempt-01'
        self.attempt_root.mkdir()
        self.primary = self.attempt_root / 'primary'
        self.primary.mkdir()
        self.scope = dict(run_id='run1', source=dict(path=str(self.source), sha256=self.original, original_path=str(self.source)),
                          request=dict(action='run', orientation='left', frames=8, route=route, loop=True, pixel_art=True, task_ref=self.ref),
                          authorization=dict(request_authorized=True, max_cost_usd=1.0, max_generation_attempts=2))
        self.scope_hash = scope_sha(self.scope)
        self.manifest_path = self.run_root / 'run-manifest.json'
        self.write(self.run_root / 'scope.json', self.scope)
        (self.run_root / 'scope.sha256').write_text(self.scope_hash + '\n')
        ledger.bind_run(self.task['task_manifest'], dict(run_id='run1', target_id='wolf', scope_sha256=self.scope_hash,
                                                       manifest_path=str(self.manifest_path)))
        self.strategy = ledger.begin_attempt(self.task['task_manifest'], 'run1', self.attempt_id,
             dict(route=route, model='fal-ai/nano-banana-2/edit' if route == 'sheet' else 'minimax/h3-max/image-to-video', conditioning='single_source',
                  inputs=[dict(role='source', sha256=self.original)]))
        self.manifest = dict(schema_version=3, mode='whole_sheet', **self.scope, authorization_scope_sha256=self.scope_hash,
                             attempts=[dict(index=1, attempt_id=self.attempt_id, jobs={})], state='GENERATING')
        actual_source = self.source
        if route == 'video':
            from sheet_video import prepare_source
            prepared = prepare_source(self.source, self.attempt_root / 'prepared-source.png', pixel_art=True)
            self.manifest['attempts'][0]['prepared'] = prepared
            actual_source = Path(prepared['path'])
        self.primary_config, self.primary_node_sha = self.make_config(self.primary, 'primary', actual_source)
        self.primary_receipt = self.make_receipt(self.primary, self.primary_config, self.primary_node_sha, '.2', '.3')
        self.primary_quote = self.primary_receipt['sha256']
        self.plan = dict(schema='sprite_deferred_cleanup_v1', primary_operation_id=self.primary_id,
                         primary_config_sha256=self.primary_node_sha, model='fal-ai/ideogram/remove-background',
                         input=dict(sync_mode=False), reserved_units=1, source_from='primary_raw_png' if route == 'sheet' else 'video_sample_grid_v1',
                         source_limits=dict(mime='image/png', max_bytes=10485760, max_width=4096, max_height=4096))
        self.plan_dir = self.attempt_root / 'cleanup-plan'
        self.plan_dir.mkdir()
        self.write(self.plan_dir / 'plan.json', self.plan)
        self.plan_sha = digest(encoded(self.plan))
        self.ops = [dict(operation_id=self.primary_id, kind='provider_animation', config_sha256=self.primary_node_sha,
                         quote_sha256=self.primary_quote, amount_usd='.3'),
                    dict(operation_id=self.cleanup_id, kind='alpha_edge_cleanup', deferred_config=self.plan,
                         quote_sha256='b' * 64, amount_usd='.1')]
        ledger.reserve_plan(self.task['task_manifest'], 'run1', self.attempt_id, self.strategy, self.ops)
        self.provenance(self.primary, dict(schema='sprite_source_provenance_v1', kind='original_source' if route == 'sheet' else 'prepared_source_v1',
                    original_source_sha256=self.original, input_sha256=digest(actual_source.read_bytes()),
                    evidence={} if route == 'sheet' else dict(transform=prepared['transform'])))
        self.primary_binding = self.bind(self.primary, self.primary_node_sha, digest(actual_source.read_bytes()))
        self.write_manifest()

    def write(self, path, value):
        path.write_bytes(encoded(value))

    def write_manifest(self):
        self.write(self.manifest_path, self.manifest)

    def make_config(self, directory, role, source):
        context = dict(task_ref=self.ref, run_manifest=str(self.manifest_path), run_id='run1',
                       attempt_id=self.attempt_id, operation_id=self.attempt_id + ':' + role)
        inputs = (dict(prompt='eight continuous poses', resolution='2K', aspect_ratio='16:9', num_images=1,
                       output_format='png', limit_generations=True, enable_web_search=False) if role == 'primary'
                  else dict(sync_mode=False))
        if role == 'primary' and self.route == 'video':
            inputs = dict(prompt='continuous gait', duration=5, resolution='768P', prompt_expansion_mode='balanced',
                          enable_safety_checker=True, sync_mode=False, end_image_same_as_start=True)
        config = dict(schema_version=3, model=('fal-ai/nano-banana-2/edit' if self.route == 'sheet' else 'minimax/h3-max/image-to-video') if role == 'primary' else 'fal-ai/ideogram/remove-background',
                      source=dict(path=str(source), sha256=digest(source.read_bytes())), input=inputs,
                      authorization=dict(request_authorized=True, scope_sha256=self.scope_hash, task_context=context), max_cost_usd=1.0)
        self.write(directory / 'config.json', config)
        # Actual Node numeric/stringification semantics, not a Python imitation.
        node = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        code = "const fs=require('fs'),c=require('crypto');const s=x=>Array.isArray(x)?x.map(s):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,s(x[k])])):x;const bytes=JSON.stringify(s(JSON.parse(fs.readFileSync(0,'utf8'))),null,2)+'\\n';process.stdout.write(c.createHash('sha256').update(bytes).digest('hex'));"
        child = subprocess.run([str(node), '-e', code], input=encoded(config), capture_output=True, timeout=10)
        self.assertEqual(0, child.returncode, child.stderr)
        node_sha = child.stdout.decode()
        file_sha = digest((directory / 'config.json').read_bytes())
        (directory / 'config.sha256').write_text(scope_sha(config) + '\n')
        self.manifest['attempts'][0]['jobs'][role] = dict(path=str(directory), config_sha256=scope_sha(config),
            transport_config_sha256=node_sha, config_file_sha256=file_sha, operation_id=self.attempt_id + ':' + role,
            state='PRICED', request_id=None, reserved_usd=.3 if role == 'primary' else .1)
        return config, node_sha

    def make_receipt(self, directory, config, node_sha, base, estimate, expired=False):
        receipt_dir = directory / 'pricing-receipts'
        receipt_dir.mkdir(exist_ok=True)
        receipt_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).replace(microsecond=0) - (timedelta(hours=1) if expired else timedelta())
        iso = lambda value: value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        model = config['model']
        video = model == 'minimax/h3-max/image-to-video'
        if video:
            base = '.04'
        unit = 'seconds' if video else 'image'
        api_body = encoded(dict(prices=[dict(endpoint_id=model, currency='USD', unit=unit, unit_price=base)]))
        api_name = receipt_id + '.api.body'
        (receipt_dir / api_name).write_bytes(api_body)
        from urllib.parse import urlencode
        sources = [dict(source_url='https://api.fal.ai/v1/models/pricing?' + urlencode(dict(endpoint_id=model)),
            retrieved_at=iso(now), body_sha256=digest(api_body), archive_file=api_name,
            selected=dict(endpoint_id=model, currency='USD', unit=unit, unit_price_usd=float(base)))]
        nano = model == 'fal-ai/nano-banana-2/edit'
        if nano:
            public_name = receipt_id + '.public.body'
            public = b'<html>base 0.2; 2K resolution multiplier 1.5</html>'
            (receipt_dir / public_name).write_bytes(public)
            sources.append(dict(source_url='https://fal.ai/models/fal-ai/nano-banana-2/edit', retrieved_at=iso(now),
                body_sha256=digest(public), archive_file=public_name,
                selected=dict(base_unit_price_usd=float(base), resolution='2K', resolution_multiplier=1.5)))
        if video:
            public_name = receipt_id + '.public.body'
            public = b'<html>synthetic H3 pricing fixture: base .04 and 768P .05</html>'
            (receipt_dir / public_name).write_bytes(public)
            sources.append(dict(source_url='https://fal.ai/models/minimax/h3-max/image-to-video', retrieved_at=iso(now),
                body_sha256=digest(public), archive_file=public_name,
                selected=dict(base_unit_price_usd=float(base), resolution='768P', effective_unit_price_usd=float(Decimal(estimate) / 6))))
        receipt = dict(schema_version=3, receipt_id=receipt_id, config_sha256=node_sha, model=model,
            retrieved_at=iso(now), expires_at=iso(now + timedelta(minutes=15)), currency='USD', api_unit=unit,
            base_unit_price_usd=float(base), resolution_multiplier=1.5 if nano else None,
            effective_unit_price_usd=float(Decimal(estimate) / 6) if video else float(estimate), estimated_cost_usd=float(estimate), estimated_cost_usd_decimal=str(float(estimate)),
            planning_basis='configured_duration+1_reserved_output_second' if video else 'configured_output_count',
            input_duration_seconds=5 if video else None, reserved_units=6 if video else 1,
            driving_video_sha256=None, sources=sources)
        name = f'{int(now.timestamp()) * 1000:016d}-{receipt_id}.receipt.json'
        path = receipt_dir / name
        self.write(path, receipt)
        return dict(path=path, value=receipt, sha256=digest(path.read_bytes()))

    def provenance(self, directory, record):
        self.write(directory / 'source-provenance.json', record)

    def bind(self, directory, node_sha, source_sha, deferred=None):
        binding = dict(schema='sprite_execution_binding_v1', config_sha256=node_sha,
                       config_file_sha256=digest((directory / 'config.json').read_bytes()), source_sha256=source_sha,
                       provenance_sha256=digest((directory / 'source-provenance.json').read_bytes()), deferred_plan_sha256=deferred)
        operation = self.primary_id if directory.name == 'primary' else self.cleanup_id
        return ledger.bind_operation_config(self.task['task_manifest'], operation, binding)

    def message(self, directory=None, before=False):
        directory = directory or self.primary
        job = self.manifest['attempts'][0]['jobs'][directory.name]
        config = json.loads((directory / 'config.json').read_text())
        value = dict(schema='sprite_before_submit_v1' if before else 'sprite_transport_context_v1', job_dir=str(directory),
                     config_sha256=job['transport_config_sha256'], config_file_sha256=job['config_file_sha256'],
                     source_sha256=config['source']['sha256'])
        if before:
            op = ledger.inspect_task(self.task['task_manifest'])['operations'][job['operation_id']]
            value.update(quote_sha256=op['current_quote']['quote_sha256'], amount_usd=op['current_quote']['amount_usd'])
        return value

    def blocked(self, code, function, *args):
        with self.assertRaises(ledger.WorkflowError) as failure:
            function(*args)
        self.assertEqual(code, failure.exception.code)

    def cleanup(self):
        bridge.before_submit(self.message(before=True))
        ledger.record_outcome(self.task['task_manifest'], self.primary_id,
                              dict(status='completed', provider_request_id='known-primary', evidence='verified raw'))
        raw = self.primary / ('raw.png' if self.route == 'sheet' else 'raw.mp4')
        if self.route == 'sheet':
            Image.new('RGB', (16, 16), (80, 110, 140)).save(raw)
        else:
            # Byte-identity fixture only. The facade/Node own real MP4 decoding.
            raw.write_bytes(b'synthetic-video-identity-fixture')
        raw_sha = digest(raw.read_bytes())
        primary_job = self.manifest['attempts'][0]['jobs']['primary']
        primary_job.update(state='DOWNLOADED', request_id='known-primary', raw_path=str(raw), raw_sha256=raw_sha)
        self.write(self.primary / 'job-state.json', dict(schema_version=3, state='DOWNLOADED',
            config_sha256=self.primary_node_sha, request_id='known-primary', raw_path=str(raw), raw_sha256=raw_sha))
        directory = self.attempt_root / 'cleanup'
        directory.mkdir()
        cleanup_source = raw
        evidence = dict(parent_operation_id=self.primary_id, parent_request_id='known-primary', parent_raw_sha256=raw_sha)
        if self.route == 'video':
            sampling = self.attempt_root / 'sampling'
            sampling.mkdir()
            paths = []
            grid = Image.new('RGB', (64, 32))
            for index in range(8):
                frame = Image.new('RGB', (16, 16), (80 + index, 110, 140))
                dest = sampling / f'frame-{index+1:02d}.png'
                frame.save(dest)
                paths.append(str(dest))
                grid.paste(frame, ((index % 4) * 16, (index // 4) * 16))
            cleanup_source = sampling / 'sample-grid.png'
            grid.save(cleanup_source)
            selection = dict(schema_version=3, raw_sha256=raw_sha, grid_sha256=digest(cleanup_source.read_bytes()),
                             frame_sha256=[digest(Path(path).read_bytes()) for path in paths])
            selection_path = sampling / 'selection.json'
            self.write(selection_path, selection)
            self.manifest['attempts'][0]['sampling'] = dict(grid_path=str(cleanup_source), frame_paths=paths,
                                                            selection_path=str(selection_path), selection=selection)
            evidence.update(selection_sha256=digest(selection_path.read_bytes()), grid_sha256=digest(cleanup_source.read_bytes()))
        config, node_sha = self.make_config(directory, 'cleanup', cleanup_source)
        receipt = self.make_receipt(directory, config, node_sha, '.1', '.1')
        self.provenance(directory, dict(schema='sprite_source_provenance_v1', kind='primary_raw_png' if self.route == 'sheet' else 'video_sample_grid_v1',
            original_source_sha256=self.original, input_sha256=digest(cleanup_source.read_bytes()), evidence=evidence))
        self.bind(directory, node_sha, digest(cleanup_source.read_bytes()), self.plan_sha)
        revised = dict(self.ops[1], quote_sha256=receipt['sha256'])
        ledger.reprice_plan(self.task['task_manifest'], 'run1', self.attempt_id, [self.ops[0], revised])
        self.write_manifest()
        return directory

    def test_context_checks_real_scope_and_separates_node_and_file_digests(self):
        self.assertNotEqual(scope_sha(self.primary_config), self.primary_node_sha)
        before = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = bridge.check_context(self.message())
        self.assertEqual('CONTEXT_VERIFIED', result['state'])
        self.assertIsNone(result['outcome_status'])
        self.assertEqual(self.primary_binding['binding_sha256'], result['binding_sha256'])
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_pending_history_diagnoses_before_absent_executable_setup(self):
        from test_sheet_task_workflow import history_fixture
        self.task=history_fixture(self.root/'pending',self.source,action='run',target_id='wolf',pending=True)
        self.ref.update(task_id=self.task['task_id'],task_manifest=self.task['task_manifest'],
                        authorization_sha256=self.task['authorization_sha256'])
        self.scope_hash=scope_sha(self.scope);self.manifest.update(self.scope,authorization_scope_sha256=self.scope_hash)
        self.write(self.run_root/'scope.json',self.scope);(self.run_root/'scope.sha256').write_text(self.scope_hash+'\n')
        self.make_config(self.primary,'primary',self.source);self.write_manifest()
        before=ledger.inspect_task(self.task['task_manifest'])
        self.blocked('TASK_HISTORY_REQUIRED',bridge.check_context,self.message())
        after=ledger.inspect_task(self.task['task_manifest'])
        self.assertEqual(after['runs'],{});self.assertEqual(after['sequence'],before['sequence'])
        # Direct isolated quote read is allowed; it supplies no execution permission.
        from test_sheet_task_workflow import ManagedWorkflowTests
        harness=ManagedWorkflowTests(methodName='runTest');harness.setUp();self.addCleanup(harness.doCleanups)
        quoted=harness.transport('pricing',self.primary)
        self.assertEqual(quoted['state'],'PRICED');self.assertEqual(harness.posts(),[])
        self.blocked('TASK_HISTORY_REQUIRED',bridge.check_context,self.message())
        self.assertEqual(ledger.inspect_task(self.task['task_manifest'])['sequence'],before['sequence'])

    def test_fresh_operation_guard_uses_strategy_but_marker_recovery_does_not(self):
        actual=ledger.check_history_context
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            result=bridge.check_context(self.message())
        self.assertEqual(result['state'],'CONTEXT_VERIFIED')
        self.assertEqual([c.args for c in guarded.call_args_list],[(self.task['task_manifest'],'wolf',
            dict(route='sheet',model='fal-ai/nano-banana-2/edit',conditioning='single_source',inputs=[dict(role='source',sha256=self.original)]))])
        bridge.before_submit(self.message(before=True))
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            result=bridge.check_context(self.message())
        self.assertEqual(result['state'],'CONTEXT_VERIFIED');self.assertEqual(guarded.call_count,0)
        ledger.record_outcome(self.task['task_manifest'],self.primary_id,dict(status='accepted',provider_request_id='same-known-id',evidence='Synthetic known-ID evidence'))
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            result=bridge.check_context(self.message())
        self.assertEqual(result['provider_request_id'],'same-known-id');self.assertEqual(guarded.call_count,0)

    def test_before_submit_validates_exact_receipt_and_durable_marker(self):
        result = bridge.before_submit(self.message(before=True))
        self.assertTrue(result['submit_allowed'])
        self.assertTrue(ledger.inspect_task(self.task['task_manifest'])['operations'][self.primary_id]['submission_started'])
        self.blocked('TASK_SUBMISSION_UNRESOLVED', bridge.before_submit, self.message(before=True))

    def test_known_id_context_and_continuation_need_no_fresh_quote(self):
        bridge.before_submit(self.message(before=True))
        ledger.record_outcome(self.task['task_manifest'], self.primary_id,
                              dict(status='completed', provider_request_id='known-primary', evidence='receipt'))
        self.primary_receipt['path'].unlink()
        context = bridge.check_context(self.message())
        self.assertEqual('known-primary', context['provider_request_id'])
        self.assertEqual('committed', context['status'])
        self.assertEqual('completed', context['outcome_status'])
        self.assertFalse(bridge.before_submit(self.message(before=True))['submit_allowed'])

    def test_failed_outcome_is_distinct_from_committed_reservation_state(self):
        bridge.before_submit(self.message(before=True))
        ledger.record_outcome(self.task['task_manifest'], self.primary_id,
                              dict(status='failed', provider_request_id='known-failed', evidence='provider failure'))
        result = bridge.check_context(self.message())
        self.assertEqual('committed', result['status'])
        self.assertEqual('failed', result['outcome_status'])
        self.primary_receipt['path'].unlink()
        self.assertFalse(bridge.before_submit(self.message(before=True))['submit_allowed'])

    def test_competing_known_id_returns_one_coherent_post_marker_snapshot(self):
        for terminal in ('failed', 'completed'):
            with self.subTest(terminal=terminal):
                self.build_fixture()
                real_receipt = bridge._receipt
                def competing_outcome(context, message):
                    real_receipt(context, message)
                    expected = dict(config_sha256=message['config_sha256'], binding_sha256=context['operation']['binding_sha256'],
                                    quote_sha256=message['quote_sha256'], amount_usd=message['amount_usd'])
                    ledger.mark_submission(self.task['task_manifest'], self.primary_id, expected=expected)
                    ledger.record_outcome(self.task['task_manifest'], self.primary_id,
                        dict(status=terminal, provider_request_id='other-executor-known-id', evidence='real competing marker/outcome'))
                with patch.object(bridge, '_receipt', competing_outcome):
                    result = bridge.before_submit(self.message(before=True))
                self.assertFalse(result['submit_allowed'])
                self.assertEqual('other-executor-known-id', result['provider_request_id'])
                self.assertTrue(result['submission_started'])
                self.assertEqual('committed', result['status'])
                self.assertEqual(terminal, result['outcome_status'])

    def test_known_completed_primary_resumes_after_legitimate_cleanup_release_without_quote(self):
        bridge.before_submit(self.message(before=True))
        ledger.record_outcome(self.task['task_manifest'], self.primary_id,
                              dict(status='completed', provider_request_id='known-primary', evidence='verified raw'))
        ledger.release_unsubmitted(self.task['task_manifest'], self.cleanup_id, 'facade alpha/assembly confirms cleanup unnecessary')
        self.primary_receipt['path'].unlink()
        result = bridge.before_submit(self.message(before=True))
        self.assertFalse(result['submit_allowed'])
        self.assertEqual('known-primary', result['provider_request_id'])
        self.assertTrue(result['submission_started'])
        self.assertEqual('committed', result['status'])
        self.assertEqual('completed', result['outcome_status'])
        self.assertEqual('0.3', ledger.inspect_task(self.task['task_manifest'])['reserved_total_usd'])

    def test_missing_or_mutated_task_reference_and_bind_block(self):
        self.manifest['request'] = dict(self.manifest['request'])
        self.manifest['request'].pop('task_ref')
        self.write_manifest()
        self.blocked('TASK_SCOPE_MISMATCH', bridge.check_context, self.message())

    def test_source_config_and_provenance_mutation_block(self):
        for path in (self.source, self.primary / 'config.json', self.primary / 'source-provenance.json'):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                path.write_bytes(before + b' ')
                with self.assertRaises(ledger.WorkflowError):
                    bridge.check_context(self.message())
                path.write_bytes(before)

    def test_receipt_body_tamper_and_stale_receipt_never_mark(self):
        archived = next((self.primary / 'pricing-receipts').glob('*.api.body'))
        original = archived.read_bytes()
        archived.write_bytes(b'{}')
        self.blocked('PRICE_RECEIPT_INVALID', bridge.before_submit, self.message(before=True))
        self.assertFalse(ledger.inspect_task(self.task['task_manifest'])['operations'][self.primary_id]['submission_started'])
        archived.write_bytes(original)
        # A valid-but-old receipt can be selected/held, but cannot start a submit.
        old = self.make_receipt(self.primary, self.primary_config, self.primary_node_sha, '.2', '.3', expired=True)
        updated = dict(self.ops[0], quote_sha256=old['sha256'])
        ledger.reprice_plan(self.task['task_manifest'], 'run1', self.attempt_id, [updated, self.ops[1]])
        self.blocked('PRICE_STALE', bridge.before_submit, self.message(before=True))

    def test_cleanup_is_bound_to_exact_completed_primary_raw_and_plan(self):
        directory = self.cleanup()
        self.assertTrue(bridge.before_submit(self.message(directory, before=True))['submit_allowed'])
        plan_path = self.plan_dir / 'plan.json'
        value = json.loads(plan_path.read_text())
        value['primary_operation_id'] = 'other-primary'
        self.write(plan_path, value)
        self.blocked('TASK_PLAN_INVALID', bridge.check_context, self.message(directory))

    def test_cleanup_parent_request_and_raw_cannot_be_replaced(self):
        directory = self.cleanup()
        parent_state = self.primary / 'job-state.json'
        value = json.loads(parent_state.read_text())
        value['request_id'] = 'unrelated-id'
        self.write(parent_state, value)
        self.blocked('TASK_SOURCE_PROVENANCE_MISMATCH', bridge.check_context, self.message(directory))

    def test_cli_protocol_is_bounded_and_never_uses_run_lock(self):
        command = [sys.executable, '-B', str(Path(bridge.__file__)), 'check-context']
        with run_lock(self.run_root):
            child = subprocess.run(command, input=encoded(self.message()), capture_output=True, timeout=15)
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertEqual('CONTEXT_VERIFIED', json.loads(child.stdout)['state'])
        for payload in (b'{', b'x' * 65537, b'[]', b'{"schema":"anything","program":"cmd.exe"}'):
            child = subprocess.run(command, input=payload, capture_output=True, timeout=15)
            self.assertEqual(2, child.returncode)
            self.assertEqual('BLOCKED', json.loads(child.stdout)['state'])
            self.assertEqual(b'', child.stderr)

    def test_unexpected_fields_relative_paths_and_missing_runtime_context_block(self):
        for changes in (dict(job_dir='relative/path'), dict(program='cmd.exe'), dict(config_sha256='x'), dict(schema='wrong')):
            self.blocked('TASK_BRIDGE_INPUT_INVALID', bridge.check_context, dict(self.message(), **changes))

    def test_prepared_video_keeps_original_strategy_and_exact_transform_binding(self):
        self.build_fixture('video')
        self.assertNotEqual(self.original, self.message()['source_sha256'])
        state = ledger.inspect_task(self.task['task_manifest'])
        self.assertEqual(self.original, state['strategies'][self.strategy]['effective_descriptor']['inputs'][0]['sha256'])
        self.assertEqual('CONTEXT_VERIFIED', bridge.check_context(self.message())['state'])
        self.assertTrue(bridge.before_submit(self.message(before=True))['submit_allowed'])
        self.manifest['attempts'][0]['prepared']['transform']['offset'][0] += 1
        self.write_manifest()
        self.blocked('TASK_SOURCE_PROVENANCE_MISMATCH', bridge.check_context, self.message())

    def test_video_cleanup_grid_selection_frames_and_parent_are_bound(self):
        self.build_fixture('video')
        directory = self.cleanup()
        self.assertEqual('CONTEXT_VERIFIED', bridge.check_context(self.message(directory))['state'])
        self.assertTrue(bridge.before_submit(self.message(directory, before=True))['submit_allowed'])
        frame = Path(self.manifest['attempts'][0]['sampling']['frame_paths'][0])
        frame.write_bytes(frame.read_bytes() + b' ')
        self.blocked('TASK_SOURCE_PROVENANCE_MISMATCH', bridge.check_context, self.message(directory))

    def test_prepared_transform_boolean_is_not_an_integer_coordinate(self):
        self.build_fixture('video')
        self.manifest['attempts'][0]['prepared']['transform']['offset'][0] = False
        self.write_manifest()
        self.blocked('TASK_SOURCE_PROVENANCE_MISMATCH', bridge.check_context, self.message())

    def test_receipt_decimal_string_and_numeric_must_agree_before_marker(self):
        value = self.primary_receipt['value']
        value['estimated_cost_usd_decimal'] = '0.30000000000000000001'
        self.write(self.primary_receipt['path'], value)
        replacement = dict(self.ops[0], quote_sha256=digest(self.primary_receipt['path'].read_bytes()))
        ledger.reprice_plan(self.task['task_manifest'], 'run1', self.attempt_id, [replacement, self.ops[1]])
        self.blocked('PRICE_RECEIPT_INVALID', bridge.before_submit, self.message(before=True))
        self.assertFalse(ledger.inspect_task(self.task['task_manifest'])['operations'][self.primary_id]['submission_started'])

    def test_managed_primary_cannot_start_without_the_full_cleanup_plan(self):
        # Real low-level compatibility allows a one-primary reservation. The
        # managed boundary must still enforce the closed routes' complete plan.
        self.task = ledger.create_task(self.root / 'other-tasks', dict(task_id='human2', cap_usd='1',
            budget_basis='fal_operational_reservations', allowed_operations=list(ledger.OPERATION_KINDS), evidence_ref='user:8'),
            [dict(target_id='wolf', source_sha256=self.original, action='run', orientation='left', requirements={'frames': 8})])
        self.ref = dict(schema='sprite_task_ref_v1', task_id='human2', task_manifest=self.task['task_manifest'],
                        authorization_sha256=self.task['authorization_sha256'], target_id='wolf')
        self.scope['request']['task_ref'] = self.ref
        self.scope_hash = scope_sha(self.scope)
        self.write(self.run_root / 'scope.json', self.scope)
        (self.run_root / 'scope.sha256').write_text(self.scope_hash + '\n')
        self.manifest['authorization_scope_sha256'] = self.scope_hash
        ledger.bind_run(self.task['task_manifest'], dict(run_id='run1', target_id='wolf', scope_sha256=self.scope_hash,
                                                       manifest_path=str(self.manifest_path)))
        strategy = ledger.begin_attempt(self.task['task_manifest'], 'run1', self.attempt_id,
            dict(route='sheet', model='fal-ai/nano-banana-2/edit', conditioning='single_source', inputs=[dict(role='source', sha256=self.original)]))
        self.primary_config, self.primary_node_sha = self.make_config(self.primary, 'primary', self.source)
        self.primary_receipt = self.make_receipt(self.primary, self.primary_config, self.primary_node_sha, '.2', '.3')
        operation = dict(operation_id=self.primary_id, kind='provider_animation', config_sha256=self.primary_node_sha,
                         quote_sha256=self.primary_receipt['sha256'], amount_usd='.3')
        ledger.reserve_plan(self.task['task_manifest'], 'run1', self.attempt_id, strategy, [operation])
        self.bind(self.primary, self.primary_node_sha, self.original)
        self.write_manifest()
        self.blocked('TASK_PLAN_INVALID', bridge.before_submit, self.message(before=True))


class RegionalBridgeDispatchTests(unittest.TestCase):
    def test_regional_protocol_dispatches_exact_consumer_without_legacy_fallback(self):
        import regional_video_workflow
        for before,schema in [(False,'sprite_regional_transport_context_v1'),(True,'sprite_regional_before_submit_v1')]:
            message=dict(schema=schema)
            with patch.object(regional_video_workflow,'regional_bridge_run',return_value={'state':'sentinel'}) as dispatch:
                self.assertEqual({'state':'sentinel'},bridge._run(message,before))
                dispatch.assert_called_once_with(message,before)

    def test_regional_invalid_stdio_error_is_closed_without_submit_field(self):
        result=subprocess.run([sys.executable,'-B',str(Path(bridge.__file__)),'check-context'],input=encoded(dict(schema='sprite_regional_transport_context_v1')),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.assertEqual(2,result.returncode)
        self.assertEqual({'state':'BLOCKED','block_reason':'TASK_BRIDGE_INPUT_INVALID'},json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
