"""Attack profile contracts; synthetic fixtures never certify animation quality."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
import native_articulation as renderer
import native_articulated_workflow as workflow
from native_articulated_review import validate_review
from generic_sprite_workflow import _review_target
from sheet_workflow_state import WorkflowError
from test_native_articulated_workflow import fixture, pass_report, read, write
from test_native_articulated_review import review_fixture
from test_native_task_workflow import task_options

PHASES = ['ready', 'anticipation', 'load', 'strike', 'follow_through', 'recovery', 'recovery', 'settled']


def attack_report(report):
    report = copy.deepcopy(report)
    report['geometry'].pop('first_support_limb', None)
    for row in report['geometry']['frames']:
        row['supports'] = {'near': {'role': 'stance', 'point': [12,27]},
                           'far': {'role': 'stance', 'point': [20,27]}}
    for row, phase in zip(report['motion']['frames'], PHASES):
        row['phase'] = phase
    report['motion']['playback'][0]['frame_ms'] = 150
    return report


class AttackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recipe, self.geometry = fixture(self.root/'inputs')
        data = read(self.recipe)
        data.update(action='attack', frame_ms=150)
        body = self.recipe.parent/'body.png'
        with Image.open(body) as image:
            image.putpixel((20,10), (240,240,240,20))
            image.save(body)
        data['assets']['body']['sha256'] = hashlib.sha256(body.read_bytes()).hexdigest()
        write(self.recipe, data)

    def test_attack_is_nonlooping_review_required_and_integrity_checked(self):
        result = workflow.build(self.recipe, self.geometry, self.root/'runs',
            **task_options(self.root,self.recipe.parent/'source.png','attack'))
        self.assertEqual(result['state'], 'READY_FOR_VISUAL_REVIEW')
        self.assertEqual(result['request']['profile'], 'articulated_biped_planted_attack')
        self.assertFalse(result['request']['loop'])
        self.assertFalse(result['delivery_allowed'])
        with self.assertRaises(WorkflowError):
            workflow.export(result['run_manifest'], self.root/'delivery')
        with Image.open(result['artifact_paths']['preview']) as gif:
            self.assertEqual(gif.n_frames, 8)
            self.assertNotIn('loop', gif.info)
            self.assertEqual(gif.convert('RGBA').getpixel((20,10))[3], 0)
            for i in range(8):
                gif.seek(i)
                self.assertEqual(gif.info['duration'], 150)
        self.assertEqual(workflow.inspect(result['run_manifest'])['state'], 'READY_FOR_VISUAL_REVIEW')

    def test_attack_pass_exports_only_after_complete_attack_evidence(self):
        # Genuine legacy policy-absent scope retains its historical direct export.
        result = workflow._build(self.recipe, self.geometry, self.root/'runs')
        self.assertEqual(result['state'], 'READY_FOR_VISUAL_REVIEW')
        report = pass_report(result, self.geometry)
        report['geometry'].pop('first_support_limb')
        for i,row in enumerate(report['geometry']['frames']):
            row['supports'] = {name: {'role':'stance','point':[x,y]}
                               for name,x,y in [('near',30,45),('far',34,44)]}
        for row,phase in zip(report['motion']['frames'], PHASES): row['phase'] = phase
        report['motion']['playback'][0]['frame_ms'] = 150
        checked = workflow.review(result['run_manifest'], report)
        self.assertTrue(checked['delivery_allowed'])
        workflow.export(result['run_manifest'], self.root/'delivery')
        receipt = read(self.root/'delivery/delivery.json')
        self.assertFalse(receipt['request']['loop'])
        self.assertFalse(receipt['generation_claimed'])
        self.assertEqual(workflow.inspect(result['run_manifest'])['state'], 'READY_FOR_DELIVERY')

    def test_joint_overlap_can_be_hidden_without_removing_instances(self):
        data = read(self.recipe)
        overlap = self.recipe.parent/'overlap.png'
        with Image.open(self.recipe.parent/'body.png') as image:
            image.putpixel((25,20), (20,30,220,255))
            image.save(overlap)
        data['assets']['overlap'] = {'path':overlap.name, 'sha256':hashlib.sha256(overlap.read_bytes()).hexdigest()}
        for row in data['frames']:
            layer = copy.deepcopy(row['layers'][0])
            layer.update(id='joint', asset='overlap', role='joint_overlap', visible=False)
            row['layers'].append(layer)
        write(self.recipe, data)
        frames, _ = renderer.render_recipe(self.recipe)
        self.assertEqual(frames[0].getpixel((25,20)), (200,30,20,255))
        data['frames'][0]['layers'][0]['visible'] = False
        write(self.recipe, data)
        with self.assertRaisesRegex(renderer.ArticulationError, 'VISIBILITY'):
            renderer.render_recipe(self.recipe)

    def test_joint_role_and_visibility_must_be_valid_and_fixed(self):
        data = read(self.recipe)
        data['frames'][0]['layers'][0]['visible'] = 'false'
        write(self.recipe, data)
        with self.assertRaisesRegex(renderer.ArticulationError, 'VISIBILITY'):
            renderer.render_recipe(self.recipe)
        data['frames'][0]['layers'][0].update(visible=True, role='joint_overlap')
        for row in data['frames']:
            base = copy.deepcopy(row['layers'][0])
            base.pop('role', None)
            base.update(id='base', visible=True)
            row['layers'].append(base)
        write(self.recipe, data)
        with self.assertRaisesRegex(renderer.ArticulationError, 'ROLE'):
            renderer.render_recipe(self.recipe)


class AttackReviewTests(unittest.TestCase):
    def setup_attack(self):
        context, report, source, geometry, frames, components = review_fixture()
        context['request'].update(action='attack', frame_ms=150, loop=False)
        report = attack_report(report)
        report['artifact_set_sha256'] = _review_target(context)
        return context, report, source, geometry, frames, components

    def test_single_strike_with_planted_supports_is_validated(self):
        args = self.setup_attack()
        self.assertEqual(validate_review(*args)['decision'], 'pass')

    def test_repeated_strike_walk_phases_or_lifted_support_cannot_approve(self):
        for defect in ('second_strike', 'walk', 'lifted'):
            args = list(self.setup_attack())
            if defect == 'second_strike': args[1]['motion']['frames'][6]['phase'] = 'strike'
            elif defect == 'walk': args[1]['motion']['frames'][0]['phase'] = 'contact'
            else: args[1]['geometry']['frames'][4]['supports']['near'] = {'role':'swing','point':None}
            with self.subTest(defect=defect), self.assertRaises(WorkflowError): validate_review(*args)

    def test_attack_loop_or_wrong_playback_cannot_approve(self):
        for defect in ('loop', 'playback'):
            args = list(self.setup_attack())
            if defect == 'loop':
                args[0]['request']['loop'] = True
                args[1]['artifact_set_sha256'] = _review_target(args[0])
            else: args[1]['motion']['playback'][0]['frame_ms'] = 100
            with self.subTest(defect=defect), self.assertRaises(WorkflowError): validate_review(*args)

    def test_planted_attack_rejects_sliding_support_annotations(self):
        args = list(self.setup_attack())
        args[1]['geometry']['frames'][4]['supports']['near']['point'] = [19,27]
        with self.assertRaisesRegex(WorkflowError, 'SUPPORT'):
            validate_review(*args)


if __name__ == '__main__': unittest.main()
