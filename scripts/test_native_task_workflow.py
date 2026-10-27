"""Managed native/alignment protocol with real pixels, proofs and task replay.

Synthetic reviews exercise contracts only; they never establish artistic quality.
The generic fixture imports C's actual Node/fixed-bridge network-only fake harness.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image
import generic_sprite_workflow as generic
import native_articulated_workflow as articulated
import native_sprite_workflow as native
import sprite_alignment_workflow as alignment
import sprite_task_ledger as ledger
from sheet_workflow_state import WorkflowError, encoded, sha_file
from test_native_articulated_workflow import fixture, pass_report, read, write, NOTE


def task_options(root, source, action='walk', *, allowed=None, requirements=None):
    task=ledger.create_task(Path(root)/'tasks',dict(task_id='fixture-'+uuid.uuid4().hex,
        cap_usd='1',budget_basis='fal_operational_reservations',
        allowed_operations=list(ledger.OPERATION_KINDS) if allowed is None else allowed,
        evidence_ref='Synthetic isolated authorization; no service invocation'),
        [dict(target_id='character',source_sha256=sha_file(source),action=action,
            orientation='left',requirements={'frames':8} if requirements is None else requirements)])
    return dict(task_manifest=task['task_manifest'],target_id='character')


class NativeTaskTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='sprite-D-');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.recipe,self.geometry=fixture(self.root/'inputs')
        recipe=read(self.recipe)
        for i,frame in enumerate(recipe['frames']):frame['layers'][0]['target'][1]=i%2
        write(self.recipe,recipe)
        self.context=task_options(self.root,self.recipe.parent/'source.png')

    def build(self, **kwargs):
        return articulated.build(self.recipe,self.geometry,self.root/'runs',**(self.context|kwargs))

    def state(self):return ledger.inspect_task(self.context['task_manifest'])

    def parent(self):
        result=self.build()
        return articulated.review(result['run_manifest'],self.native_report(result))

    def native_report(self,result):
        report=pass_report(result,self.geometry)
        for i,frame in enumerate(report['geometry']['frames']):
            frame['body_anchor'][1]+=i%2
            for anchor in frame['anchors']:
                for point in anchor['points']:point[1]+=i%2
            for support in frame['supports'].values():
                if support['point'] is not None:support['point'][1]+=i%2
        return report

    def derive(self,result,**kwargs):
        annotation={'schema_version':1,'artifact_set_sha256':result['artifact_set_sha256'],
            'reference':{'name':'torso midpoint','description':NOTE,'source_point':[30,30]},
            'frames':[{'frame':i+1,'sha256':sha_file(p),'point':[30+i,30+i%2]}
                for i,p in enumerate(result['artifact_paths']['frames'])]}
        path=self.root/('anchors-'+uuid.uuid4().hex+'.json');write(path,annotation)
        return alignment.align_manifest(result['run_manifest'],path,output_root=self.root/'derived',**kwargs)

    def child_review(self,result,failed=None):
        from test_sheet_review import VisualReviewTests
        from test_sprite_alignment_workflow import body_review
        report=VisualReviewTests.report(self,result,failed);report['body_alignment']=body_review(result)
        path=self.root/'child-review.json';write(path,report)
        return generic.record_visual_review(result['run_manifest'],path)

    def test_public_context_and_permission_are_required_before_render(self):
        with self.assertRaisesRegex(WorkflowError,'TASK_CONTEXT_REQUIRED'):
            articulated.build(self.recipe,self.geometry,self.root/'missing')
        context=task_options(self.root,self.recipe.parent/'source.png',allowed=['body_x_alignment'])
        with self.assertRaisesRegex(WorkflowError,'OPERATION_NOT_AUTHORIZED'):
            self.build(**context)
        self.assertFalse((self.root/'runs').exists());self.assertFalse((self.root/'missing').exists())

    def test_history_native_build_blocks_before_snapshot_or_render(self):
        from test_sheet_task_workflow import history_fixture
        source=self.recipe.parent/'source.png'
        descriptor=dict(route='native_motion_construction',model='articulated_biped_walk',conditioning='local',
                        inputs=[dict(role='source',sha256=sha_file(source))])
        for kind,code in [('pending','TASK_HISTORY_REQUIRED'),('closed','TASK_HISTORY_MECHANISM_CLOSED'),('unresolved','TASK_HISTORY_UNRESOLVED')]:
            with self.subTest(kind=kind):
                task=history_fixture(self.root/kind,source,descriptor=descriptor,pending=kind=='pending',unresolved=kind=='unresolved')
                with self.assertRaisesRegex(WorkflowError,code):self.build(task_manifest=task['task_manifest'])
                self.assertFalse((self.root/'runs').exists())
                self.assertEqual(ledger.inspect_task(task['task_manifest'])['runs'],{})

    def test_history_quadruped_prepare_blocks_before_prompt(self):
        from test_native_geometry_workflow import GeometryWorkflowTests
        from test_sheet_task_workflow import history_fixture
        helper=GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        descriptor=dict(route='native_motion_construction',model='quadruped_two_stride',conditioning='local',
            inputs=[dict(role='source',sha256=sha_file(helper.source))])
        for pending,code in [(True,'TASK_HISTORY_REQUIRED'),(False,'TASK_HISTORY_MECHANISM_CLOSED')]:
            task=history_fixture(helper.root/str(pending),helper.source,action='run',descriptor=descriptor,pending=pending)
            with self.assertRaisesRegex(WorkflowError,code):
                native.prepare(helper.source,helper.root/'runs','left',source_geometry=helper.contract(),task_manifest=task['task_manifest'],target_id='character')
            self.assertFalse((helper.root/'runs').exists())

    def test_history_budget_keeps_null_and_status_only_for_sealed_task(self):
        import native_alignment_source as managed
        from test_sheet_task_workflow import history_fixture
        task=history_fixture(self.root/'pending',self.recipe.parent/'source.png',pending=True)
        result=managed.native_budget({'request':{'task_ref':{'task_manifest':task['task_manifest']}}})
        self.assertEqual(result['history_status'],'pending');self.assertIsNone(result['task_budget']['reserved_total_usd'])
        self.assertIsNone(result['task_budget']['available_estimate_usd'])
        self.assertNotIn('history_status',self.build())

    def test_completed_native_resume_validates_without_rebeginning(self):
        from test_sheet_task_workflow import history_fixture
        task=history_fixture(self.root/'local-history',self.recipe.parent/'source.png',local_unknown=True)
        self.context=dict(task_manifest=task['task_manifest'],target_id='character')
        result=self.build();before=self.state()['sequence']
        with patch.object(ledger,'begin_attempt',side_effect=AssertionError('completed read must not begin')):
            resumed=articulated.resume(result['run_manifest'])
        self.assertEqual(resumed['state'],articulated.READY);self.assertEqual(self.state()['sequence'],before)

    def test_history_building_resume_checks_actual_descriptor_before_render(self):
        from test_sheet_task_workflow import history_fixture
        task=history_fixture(self.root/'local-history',self.recipe.parent/'source.png',local_unknown=True)
        self.context=dict(task_manifest=task['task_manifest'],target_id='character')
        class Interrupted(BaseException):pass
        with patch.object(articulated,'_finish',side_effect=Interrupted):
            with self.assertRaises(Interrupted):self.build()
        path=next((self.root/'runs').glob('*/run-manifest.json'));actual=ledger.check_history_context
        render=articulated._render
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            def checked_render(*args,**kwargs):
                self.assertTrue(any(len(c.args)==3 and c.args[2]['route']=='native_motion_construction' for c in guarded.call_args_list))
                return render(*args,**kwargs)
            with patch.object(articulated,'_render',side_effect=checked_render):resumed=articulated.resume(path)
        self.assertEqual(resumed['state'],articulated.READY)

    def test_new_alignment_and_missing_setup_use_target_only_history_guard(self):
        from test_sheet_task_workflow import history_fixture
        task=history_fixture(self.root/'local-history',self.recipe.parent/'source.png',local_unknown=True)
        self.context=dict(task_manifest=task['task_manifest'],target_id='character');parent=self.parent()
        actual=ledger.check_history_context
        class Interrupted(BaseException):pass
        with patch.object(ledger,'bind_run',side_effect=Interrupted):
            with self.assertRaises(Interrupted):self.derive(parent)
        path=next((self.root/'derived').glob('*/run-manifest.json'))
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:
            resumed=alignment.align_manifest(path,path.parent/'derivation/annotations.json',output_root=self.root/'derived')
        self.assertEqual(resumed['state'],generic.READY,resumed)
        self.assertIn((task['task_manifest'],'character'),[c.args for c in guarded.call_args_list])
        with patch.object(ledger,'check_history_context',wraps=actual) as guarded:child=self.derive(parent)
        self.assertEqual(child['state'],generic.READY,child)
        self.assertIn((task['task_manifest'],'character'),[c.args for c in guarded.call_args_list])

    def test_native_scope_bind_attempt_budget_and_no_manifest_downgrade(self):
        result=self.build();m=read(result['run_manifest']);state=self.state()
        self.assertEqual(m['request']['task_max_attempts'],2)
        self.assertEqual(read(Path(result['run_manifest']).parent/'scope.json')['request'],m['request'])
        attempt=m['attempts'][0];self.assertEqual(attempt['attempt_id'],m['run_id']+':attempt-01')
        self.assertEqual(state['runs'][m['run_id']]['scope_sha256'],m['scope_sha256'])
        self.assertEqual(result['task_budget']['reserved_total_usd'],'0');self.assertEqual(state['operations'],{})
        m['request'].pop('task_ref');write(result['run_manifest'],m)
        with self.assertRaises(WorkflowError):articulated.inspect(result['run_manifest'])

    def test_native_attempt_correction_metadata_must_match_actual_ledger(self):
        result=self.build();m=read(result['run_manifest'])
        m['attempts'][0]['correction']={'defect':'motion:invented','plan':'Invented mutable metadata','evidence':NOTE}
        write(result['run_manifest'],m)
        with self.assertRaisesRegex(WorkflowError,'TASK_STRATEGY_MISMATCH'):articulated.inspect(result['run_manifest'])

    def test_quadruped_prepare_exhaustion_never_materializes_unaccepted_prompt(self):
        from test_native_geometry_workflow import GeometryWorkflowTests
        helper=GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        context=task_options(helper.root,helper.source,'run')
        result=native.prepare(helper.source,helper.root/'runs','left',max_attempts=1,
            source_geometry=helper.contract(),**context)
        prompts=set((helper.root/'runs').glob('*/attempt-01/prompt.txt'))
        self.assertEqual(len(prompts),1);self.assertTrue(Path(result['prompt_file']).read_bytes())
        with self.assertRaisesRegex(WorkflowError,'TASK_STRATEGY_EXHAUSTED'):
            native.prepare(helper.source,helper.root/'runs','left',max_attempts=1,
                source_geometry=helper.contract(),**context)
        self.assertEqual(set((helper.root/'runs').glob('*/attempt-01/prompt.txt')),prompts)
        state=ledger.inspect_task(context['task_manifest']);self.assertEqual(len(state['attempts']),1)

    def test_quadruped_prepare_interruption_recovers_same_id_only_after_acceptance(self):
        from test_native_geometry_workflow import GeometryWorkflowTests
        class Interrupted(BaseException):pass
        for boundary in ('before_bind','after_bind','after_begin'):
            with self.subTest(boundary=boundary):
                helper=GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
                context=task_options(helper.root,helper.source,'run')
                bind=ledger.bind_run;begin=ledger.begin_attempt
                def bind_fault(*args,**kwargs):
                    if boundary=='before_bind':raise Interrupted()
                    result=bind(*args,**kwargs)
                    if boundary=='after_bind':raise Interrupted()
                    return result
                def begin_fault(*args,**kwargs):
                    result=begin(*args,**kwargs)
                    if boundary=='after_begin':raise Interrupted()
                    return result
                with patch.object(ledger,'bind_run',side_effect=bind_fault),patch.object(ledger,'begin_attempt',side_effect=begin_fault):
                    with self.assertRaises(Interrupted):
                        native.prepare(helper.source,helper.root/'runs','left',max_attempts=1,
                            source_geometry=helper.contract(),**context)
                path=next((helper.root/'runs').glob('*/run-manifest.json'));manifest=read(path)
                self.assertFalse(Path(manifest['attempts'][0]['prompt_file']).exists())
                self.assertTrue((path.parent/'scope.json').is_file())
                before={str(p):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
                try:self.assertEqual(native.inspect(path)['state'],'PREPARATION_PENDING')
                except WorkflowError as error:self.assertIn(error.code,('TASK_SCOPE_MISMATCH','TASK_STRATEGY_MISMATCH'))
                self.assertEqual(before,{str(p):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()})
                recovered=native.begin(path)
                self.assertEqual(recovered['state'],'NATIVE_GENERATION_STARTED')
                self.assertEqual(recovered['run_manifest'],str(path));self.assertEqual(recovered['attempt'],1)
                self.assertEqual(read(path)['attempts'][0]['attempt_id'],manifest['attempts'][0]['attempt_id'])
                self.assertEqual(sha_file(recovered['prompt_file']),manifest['attempts'][0]['prompt_sha256'])
                state=ledger.inspect_task(context['task_manifest'])
                self.assertEqual(len(state['attempts']),1);self.assertEqual(len(state['runs']),1)
                with self.assertRaisesRegex(WorkflowError,'GENERATION_ALREADY_CLAIMED'):native.begin(path)

    def test_alignment_setup_missing_mutable_policy_and_proof_cannot_register(self):
        parent=self.parent()
        class Interrupted(BaseException):pass
        with patch.object(ledger,'bind_run',side_effect=Interrupted):
            with self.assertRaises(Interrupted):self.derive(parent)
        path=next((self.root/'derived').glob('*/run-manifest.json'));manifest=read(path)
        before=self.state();manifest['request'].pop('alignment_policy');write(path,manifest)
        proof=path.parent/'derivation/proof.json';proof.unlink()
        result=alignment.align_manifest(path,path.parent/'derivation/annotations.json')
        self.assertEqual(result['block_reason'],'ALIGNMENT_PROOF_INVALID')
        after=self.state()
        self.assertEqual(dict(state=result['state'],attempts=len(after['attempts']),bound=manifest['run_id'] in after['runs'],proof=proof.exists()),
            dict(state='BLOCKED',attempts=len(before['attempts']),bound=False,proof=False))
        self.assertEqual(after['sequence'],before['sequence'])

    def test_actual_native_child_review_export_and_live_rejection(self):
        parent=self.parent();self.assertEqual(parent['state'],'READY_FOR_ALIGNMENT')
        child=self.derive(parent);self.assertEqual(child['state'],generic.READY,child)
        cm=read(child['run_manifest']);pm=read(parent['run_manifest'])
        source={'run_id':pm['run_id'],'attempt_id':pm['attempts'][-1]['attempt_id']}
        self.assertEqual(cm['request']['task_ref'],pm['request']['task_ref'])
        self.assertEqual(cm['derivation']['source_attempt'],source)
        self.assertEqual(cm['derivation']['alignment_strategy']['source_group'],source)
        self.assertFalse(child['delivery_allowed']);self.assertEqual(child['task_budget']['reserved_total_usd'],'0')
        reviewed=self.child_review(child);self.assertTrue(reviewed['delivery_allowed'],reviewed)
        exported=generic.export_artifacts(child['run_manifest'],self.root/'final')
        self.assertTrue(exported['delivery_allowed'],exported)
        report=self.native_report(parent);report['decision']='reject';report['checks']['motion']['status']='fail'
        rejected=articulated.review(parent['run_manifest'],report)
        self.assertEqual(rejected['state'],articulated.REJECT)
        self.assertFalse(generic.export_artifacts(child['run_manifest'],self.root/'blocked')['delivery_allowed'])
        attempt=next(a for a in self.state()['attempts'].values() if a['run_id']==pm['run_id'])
        self.assertEqual([r['decision'] for r in attempt['review_history']],['pass','reject'])

    def test_native_structured_correction_and_shared_profile_limit(self):
        result=self.build();report=self.native_report(result)
        report['decision']='reject';report['checks']['motion']['status']='fail'
        articulated.review(result['run_manifest'],report)
        with self.assertRaisesRegex(WorkflowError,'TASK_CORRECTION_REQUIRED'):
            articulated.revise(result['run_manifest'],self.recipe,NOTE)
        defect=next(a for a in self.state()['attempts'].values())['review']['defects'][0]
        corrected=articulated.revise(result['run_manifest'],self.recipe,NOTE,
            correction={'defect':defect,'plan':'Change actual body trajectory, fixture rejects unchanged pixels','evidence':NOTE})
        self.assertEqual(corrected['attempt'],2)
        with self.assertRaisesRegex(WorkflowError,'TASK_STRATEGY_EXHAUSTED'):self.build()

    def test_alignment_quota_by_source_and_permission(self):
        parent=self.parent();first=self.derive(parent);second=self.derive(parent)
        self.assertEqual(first['state'],generic.READY,first);self.assertEqual(second['state'],generic.READY,second)
        third=self.derive(parent);self.assertEqual(third['block_reason'],'TASK_STRATEGY_EXHAUSTED',third)
        other=self.parent();fresh=self.derive(other);self.assertEqual(fresh['state'],generic.READY,fresh)
        for child in (first,second,fresh):
            strategy=read(child['run_manifest'])['derivation']['alignment_strategy']
            self.assertEqual(strategy['strategy_scope'],'alignment_source_v1');self.assertEqual(strategy['max_attempts'],2)
        self.assertNotEqual(read(first['run_manifest'])['derivation']['alignment_strategy']['source_group'],
            read(fresh['run_manifest'])['derivation']['alignment_strategy']['source_group'])

    def test_orphan_native_reject_and_pass_reconcile_only_complete_reports(self):
        result=self.build();manifest=result['run_manifest'];report=self.native_report(result)
        receipt=Path(manifest).parent/'attempt-01'/'visual-review-001.json';receipt.write_bytes(encoded(report))
        before=Path(manifest).read_bytes();articulated.inspect(manifest);self.assertEqual(Path(manifest).read_bytes(),before)
        resumed=articulated.resume(manifest);self.assertEqual(resumed['state'],'READY_FOR_ALIGNMENT')
        report['decision']='reject';report['checks']['identity']['status']='fail'
        (receipt.parent/'visual-review-002.json').write_bytes(encoded(report))
        resumed=articulated.resume(manifest);self.assertEqual(resumed['state'],articulated.REJECT)
        with self.assertRaisesRegex(WorkflowError,'REJECTED_ARTIFACTS_REQUIRE_REVISION'):
            articulated.review(manifest,self.native_report(result))

    def test_alignment_setup_recovery_exact_bytes_and_no_inspect_repair(self):
        parent=self.parent()
        class Interrupted(BaseException):pass
        for boundary in ('before_bind','after_bind','after_begin'):
            with self.subTest(boundary=boundary):
                original_bind=ledger.bind_run;original_begin=ledger.begin_attempt
                def bind(*args,**kwargs):
                    if boundary=='before_bind':raise Interrupted()
                    value=original_bind(*args,**kwargs)
                    if boundary=='after_bind':raise Interrupted()
                    return value
                def begin(*args,**kwargs):
                    value=original_begin(*args,**kwargs)
                    if boundary=='after_begin':raise Interrupted()
                    return value
                # Each boundary uses a separate permitted native output/target.
                if boundary!='before_bind':
                    self.context=task_options(self.root,self.recipe.parent/'source.png');parent=self.parent()
                old=set((self.root/'derived').glob('*/run-manifest.json')) if (self.root/'derived').exists() else set()
                with patch.object(ledger,'bind_run',side_effect=bind),patch.object(ledger,'begin_attempt',side_effect=begin):
                    with self.assertRaises(Interrupted):self.derive(parent)
                path=(set((self.root/'derived').glob('*/run-manifest.json'))-old).pop();m=read(path)
                anchors=path.parent/'derivation/annotations.json'
                before={str(p):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
                generic.inspect(path)
                self.assertEqual(before,{str(p):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()})
                if boundary=='after_begin':
                    self.assertEqual(generic.inspect(path)['state'],generic.READY)
                    continue
                bad=self.root/'changed-anchors.json';bad.write_bytes(anchors.read_bytes()+b' ')
                denied=alignment.align_manifest(path,bad);self.assertEqual(denied['block_reason'],'ALIGNMENT_ANNOTATIONS_INVALID')
                denied=alignment.align_manifest(path,anchors,output_root=self.root/'elsewhere')
                self.assertEqual(denied['block_reason'],'WORKSPACE_NOT_ALLOWED')
                denied=alignment.align_manifest(path,anchors,alignment_max_attempts=3)
                self.assertEqual(denied['block_reason'],'TASK_STRATEGY_COLLISION')
                proof=path.parent/'derivation/proof.json';proof_bytes=proof.read_bytes();proof.unlink()
                denied=alignment.align_manifest(path,anchors)
                self.assertEqual(denied['block_reason'],'ALIGNMENT_PROOF_INVALID')
                proof.write_bytes(proof_bytes)
                recovered=alignment.align_manifest(path,anchors)
                self.assertEqual(recovered['state'],generic.READY,recovered);self.assertEqual(recovered['run_manifest'],str(path))
                self.assertEqual(m['run_id'],read(path)['run_id']);self.assertEqual(len(self.state()['attempts']),2)
                immutable={p:v for p,v in before.items() if Path(p).name!='run-manifest.json'}
                self.assertEqual(immutable,{p:Path(p).read_bytes() for p in immutable})

    def test_orphan_reject_reconciles_before_native_export_helpers(self):
        parent=self.parent();p=Path(parent['run_manifest']);report=self.native_report(parent)
        report['decision']='reject';report['checks']['identity']['status']='fail'
        receipt=p.parent/'attempt-01/visual-review-002.json';receipt.write_bytes(encoded(report))
        before=p.read_bytes()
        with self.assertRaisesRegex(WorkflowError,'NATIVE_REVIEW_RECONCILIATION_REQUIRED'):articulated.inspect(p)
        self.assertEqual(p.read_bytes(),before)
        with self.assertRaisesRegex(WorkflowError,'ALIGNMENT_REQUIRED'):
            articulated.export(p,self.root/'forbidden')
        attempt=next(a for a in self.state()['attempts'].values())
        self.assertEqual(attempt['review']['decision'],'reject')
        self.assertEqual(read(p)['state'],articulated.REJECT)

    def test_managed_planted_attack_uses_strict_review_and_child_one_shot_export(self):
        recipe=read(self.recipe);recipe['action']='attack';write(self.recipe,recipe)
        self.context=task_options(self.root,self.recipe.parent/'source.png','attack')
        result=self.build();report=self.native_report(result)
        report['geometry'].pop('first_support_limb')
        for frame in report['geometry']['frames']:
            frame['supports']={'near':{'role':'stance','point':[30,45]},'far':{'role':'stance','point':[34,44]}}
        phases=['ready','anticipation','load','strike','follow_through','recovery','recovery','settled']
        for frame,phase in zip(report['motion']['frames'],phases):frame['phase']=phase
        incomplete=copy.deepcopy(report);incomplete.pop('motion')
        with self.assertRaises(WorkflowError):articulated.review(result['run_manifest'],incomplete)
        parent=articulated.review(result['run_manifest'],report)
        self.assertEqual(parent['request']['profile'],'articulated_biped_planted_attack')
        child=self.derive(parent);reviewed=self.child_review(child)
        self.assertTrue(reviewed['delivery_allowed'],reviewed)
        exported=generic.export_artifacts(child['run_manifest'],self.root/'attack-final')
        self.assertTrue(exported['delivery_allowed'],exported)
        with Image.open(exported['artifact_paths']['preview_gif']) as gif:
            self.assertNotIn('loop',gif.info);self.assertEqual(gif.n_frames,8)

    def test_managed_quadruped_real_processing_geometry_child_export(self):
        from test_native_geometry_workflow import GeometryWorkflowTests
        helper=GeometryWorkflowTests();helper.setUp();self.addCleanup(helper.tearDown)
        result=helper.ingested();result=native.calibrate(result['run_manifest'],helper.geometry_report(result))
        parent=native.review(result['run_manifest'],helper.report(result))
        geometry=read(parent['artifact_paths']['geometry'])
        annotation={'schema_version':1,'artifact_set_sha256':parent['artifact_set_sha256'],
            'reference':{'name':'torso midpoint','description':NOTE,'source_point':helper.contract()['body_anchor']},
            'frames':[{'frame':i+1,'sha256':sha_file(p),'point':row['output_body_anchor']}
                for i,(p,row) in enumerate(zip(parent['artifact_paths']['frames'],geometry['frames']))]}
        path=self.root/'quadruped-anchors.json';write(path,annotation)
        child=alignment.align_manifest(parent['run_manifest'],path,output_root=self.root/'derived')
        self.assertEqual(child['state'],generic.READY,child);self.assertTrue(self.child_review(child)['delivery_allowed'])
        self.assertTrue(generic.export_artifacts(child['run_manifest'],self.root/'quadruped-final')['delivery_allowed'])
        state=ledger.inspect_task(parent['request']['task_ref']['task_manifest'])
        self.assertEqual(state['operations'],{});self.assertEqual(child['task_budget']['reserved_total_usd'],'0')

    def test_native_permission_does_not_imply_alignment_and_new_pass_keeps_canonical(self):
        self.context=task_options(self.root,self.recipe.parent/'source.png',allowed=['native_motion_construction'])
        parent=self.parent();before=copy.deepcopy(next(a for a in self.state()['attempts'].values())['review'])
        report=self.native_report(parent);report['checks']['identity']['notes']='Different complete synthetic observation of the same native artifacts.'
        articulated.review(parent['run_manifest'],report)
        self.assertEqual(next(a for a in self.state()['attempts'].values())['review'],before)
        denied=self.derive(parent);self.assertEqual(denied['block_reason'],'OPERATION_NOT_AUTHORIZED')
        self.assertFalse((self.root/'derived').exists())

    def test_public_native_and_alignment_cli_use_actual_context_and_quota(self):
        geometry=self.root/'geometry.json';geometry.write_bytes(encoded(self.geometry))
        env={key:os.environ[key] for key in ('SystemRoot','WINDIR','USERPROFILE','HOME','TEMP','TMP') if key in os.environ}
        env['PYTHONDONTWRITEBYTECODE']='1'
        args=[sys.executable,'-B',str(Path(articulated.__file__)),'build','--recipe',str(self.recipe),
            '--source-geometry',str(geometry),'--workspace-root',str(self.root/'cli-runs'),
            '--task-manifest',self.context['task_manifest'],'--target-id',self.context['target_id'],'--max-attempts','2']
        process=subprocess.run(args,capture_output=True,env=env,timeout=30)
        self.assertEqual(process.returncode,0,process.stderr.decode(errors='replace'))
        result=json.loads(process.stdout);parent=articulated.review(result['run_manifest'],self.native_report(result))
        child=self.derive(parent);anchors=Path(child['run_manifest']).parent/'derivation/annotations.json'
        process=subprocess.run([sys.executable,'-B',str(Path(alignment.__file__)),'align',
            '--run-manifest',parent['run_manifest'],'--anchors-file',str(anchors),
            '--output-root',str(self.root/'cli-derived'),'--alignment-max-attempts','2'],capture_output=True,env=env,timeout=30)
        self.assertEqual(process.returncode,0,process.stdout.decode(errors='replace'))
        result=json.loads(process.stdout);self.assertEqual(result['state'],generic.READY)
        self.assertEqual(read(result['run_manifest'])['derivation']['alignment_strategy']['max_attempts'],2)


class GenericManagedAlignmentTests(unittest.TestCase):
    def setUp(self):
        from test_sheet_task_workflow import ManagedWorkflowTests
        self.h=ManagedWorkflowTests(methodName='runTest');self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.root=self.h.root

    def derive(self,result,**kwargs):
        from test_sprite_alignment_workflow import annotate
        path=self.root/('anchors-'+uuid.uuid4().hex+'.json');annotate(result,path)
        return alignment.align_manifest(result['run_manifest'],path,output_root=self.root/'derived',**kwargs)

    def review(self,result,failed=None,classification=False):
        from test_sheet_review import VisualReviewTests
        from test_sprite_alignment_workflow import body_review
        report=VisualReviewTests.report(self,result,failed)
        if read(result['run_manifest']).get('derivation'):report['body_alignment']=body_review(result)
        if classification:
            report['repairable_defects']=[dict(check='framing',kind='position_x',frame_findings=[dict(frame=1,notes=NOTE)],
                attestations=dict(all_frames_inspected=True,only_horizontal_body_translation=True,
                    no_clipping_or_missing_pixels=True,no_y_scale_pose_anatomy_change=True))]
        path=self.root/'review.json';write(path,report)
        return generic.record_visual_review(result['run_manifest'],path)

    def test_actual_node_managed_alignment_pass_export_and_budget(self):
        parent=self.h.generate();before=self.h.state()['reserved_total_usd'];calls=len(self.h.calls())
        child=self.derive(parent);self.assertEqual(child['state'],generic.READY,child)
        self.assertTrue(self.review(child)['delivery_allowed'])
        exported=generic.export_artifacts(child['run_manifest'],self.root/'final')
        self.assertTrue(exported['delivery_allowed'],exported)
        self.assertEqual(exported['task_budget']['reserved_total_usd'],before);self.assertEqual(len(self.h.calls()),calls)
        self.assertEqual(len(self.h.posts()),1)

    def test_live_v3_parent_reject_blocks_already_passed_child(self):
        parent=self.h.generate();child=self.derive(parent);self.assertTrue(self.review(child)['delivery_allowed'])
        self.assertEqual(self.review(parent,'identity')['state'],generic.VISUAL_REJECTED)
        blocked=generic.export_artifacts(child['run_manifest'],self.root/'blocked')
        self.assertFalse(blocked['delivery_allowed']);self.assertEqual(blocked['block_reason'],'TASK_SOURCE_DEFECT_UNRESOLVED')

    def test_x_classification_is_repairable_but_generic_framing_is_not(self):
        first=self.h.generate();self.assertEqual(self.review(first,'framing',True)['state'],generic.VISUAL_REJECTED)
        child=self.derive(first);self.assertTrue(self.review(child)['delivery_allowed'])
        defect=next(a for a in self.h.state()['attempts'].values() if a['run_id']==read(first['run_manifest'])['run_id'])['review']['defects'][0]
        second=generic.revise(first['run_manifest'],NOTE,correction=dict(defect=defect,plan='Revise source motion with a new observed provider attempt',evidence=NOTE),transport=self.h.transport,poll_seconds=0)
        self.assertEqual(second['state'],generic.READY,second);self.review(second,'framing')
        other=self.derive(second);blocked=self.review(other)
        self.assertFalse(blocked['delivery_allowed']);self.assertEqual(blocked['block_reason'],'TASK_SOURCE_DEFECT_UNRESOLVED')

    def test_managed_v3_snapshot_replays_without_parent_and_chain_keeps_source_quota(self):
        parent=self.h.generate();child=self.derive(parent);self.assertTrue(self.review(child)['delivery_allowed'])
        grandchild=self.derive(child);self.assertEqual(grandchild['state'],generic.READY,grandchild)
        a=read(child['run_manifest'])['derivation']['alignment_strategy']
        self.assertEqual(a,read(grandchild['run_manifest'])['derivation']['alignment_strategy'])
        third=self.derive(parent);self.assertEqual(third['block_reason'],'TASK_STRATEGY_EXHAUSTED',third)
        Path(parent['run_manifest']).rename(Path(parent['run_manifest']).with_suffix('.moved'))
        self.assertTrue(generic.export_artifacts(child['run_manifest'],self.root/'snapshot-export')['delivery_allowed'])

    def test_rejected_source_A_cannot_borrow_actual_corrected_attempt_B(self):
        first=self.h.generate();self.review(first,'identity');a=self.derive(first)
        self.assertFalse(self.review(a)['delivery_allowed'])
        state=self.h.state();source=read(first['run_manifest'])
        defect=next(value for value in state['attempts'].values() if value['attempt_id']==source['attempts'][0]['attempt_id'])['review']['defects'][0]
        with Image.open(self.h.primary) as image:
            image.putpixel((30,30),(195,65,95,255));image.save(self.h.primary)
        second=generic.revise(first['run_manifest'],NOTE,transport=self.h.transport,poll_seconds=0,
            correction=dict(defect=defect,plan='Restore original visible identity in actual replacement provider output',evidence=NOTE))
        self.assertEqual(second['state'],generic.READY,second);b=self.derive(second)
        self.assertTrue(self.review(b)['delivery_allowed'])
        self.assertFalse(generic.export_artifacts(a['run_manifest'],self.root/'wrong-source')['delivery_allowed'])
        self.assertTrue(generic.export_artifacts(b['run_manifest'],self.root/'corrected-source')['delivery_allowed'])
        self.assertNotEqual(read(a['run_manifest'])['derivation']['source_attempt'],read(b['run_manifest'])['derivation']['source_attempt'])
        self.assertEqual(len(self.h.posts()),2)


if __name__=='__main__':unittest.main()
