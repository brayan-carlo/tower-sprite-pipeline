"""Offline export revocation and recovery regressions."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch
import unittest

import native_sprite_workflow as workflow
import test_native_workflow as fixture
from sheet_workflow_state import WorkflowError, sha_file


class NativeExportTests(unittest.TestCase):
    setUp = fixture.NativeWorkflowTests.setUp
    tearDown = fixture.NativeWorkflowTests.tearDown
    prepared = fixture.NativeWorkflowTests.prepared
    generated = fixture.NativeWorkflowTests.generated
    report = fixture.NativeWorkflowTests.report

    def approved(self):
        r = self.generated()
        return workflow.review(r['run_manifest'], self.report(r))

    def test_rejection_revokes_both_receipts_and_preserves_images(self):
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        original = {f.name:sha_file(f) for f in out.glob('*.png')}
        result = workflow.review(p, self.report(r, 'scale'))
        self.assertFalse(result['delivery_allowed'])
        self.assertEqual(json.loads((out/'delivery.json').read_text())['state'], 'VISUAL_REJECTED')
        self.assertEqual(json.loads((out/'visual-review.json').read_text())['decision'], 'reject')
        self.assertEqual(original, {f.name:sha_file(f) for f in out.glob('*.png')})

    def test_multi_destination_revocation_is_idempotent(self):
        r = self.approved(); p = r['run_manifest']
        for name in ('first', 'second'):
            workflow.export(p, self.root/name)
        workflow.review(p, self.report(r, 'motion'))
        workflow.recover_exports(p)
        workflow.recover_exports(p)
        for name in ('first', 'second'):
            self.assertFalse(json.loads((self.root/name/'delivery.json').read_text())['delivery_allowed'])

    def test_interrupted_export_registers_before_write_and_recovers(self):
        import native_exports
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        with patch.object(native_exports, 'new_or_identical', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                workflow.export(p, out)
        m = json.loads(Path(p).read_text())
        self.assertEqual(m['exports'][0]['status'], 'EXPORT_PENDING')
        self.assertFalse(workflow.inspect(p)['delivery_allowed'])
        workflow.recover_exports(p)
        self.assertEqual(json.loads((out/'delivery.json').read_text())['state'], 'READY_FOR_DELIVERY')

    def test_receipt_conflict_remains_pending_without_overwriting_third_party(self):
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        (out/'delivery.json').write_bytes(b'third party document')
        workflow.review(p, self.report(r, 'scale'))
        self.assertEqual((out/'delivery.json').read_bytes(), b'third party document')
        m = json.loads(Path(p).read_text())
        self.assertEqual(m['exports'][0]['status'], 'REVOCATION_PENDING')
        self.assertEqual(json.loads((out/'visual-review.json').read_text())['decision'], 'reject')
        self.assertTrue(workflow.recover_exports(p)['export_revocation_pending'])

    def test_revocation_interruption_recovers_remaining_receipt(self):
        import native_exports
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        real = native_exports.atomic_json
        def interrupted(path, value):
            if Path(path).name == 'visual-review.json':
                raise PermissionError('fixture inaccessible receipt')
            return real(path, value)
        with patch.object(native_exports, 'atomic_json', side_effect=interrupted):
            result = workflow.review(p, self.report(r, 'scale'))
        self.assertTrue(result['export_revocation_pending'])
        self.assertFalse(result['delivery_allowed'])
        self.assertEqual(json.loads((out/'delivery.json').read_text())['state'], 'VISUAL_REJECTED')
        result = workflow.recover_exports(p)
        self.assertFalse(result['export_revocation_pending'])
        self.assertEqual(json.loads((out/'visual-review.json').read_text())['decision'], 'reject')

    def test_legacy_export_can_be_adopted_then_revoked(self):
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        receipt = json.loads((out/'delivery.json').read_text())
        receipt.pop('delivery_allowed')  # Original native receipts lacked this field.
        (out/'delivery.json').write_text(json.dumps(receipt))
        m = json.loads(Path(p).read_text()); m.pop('exports')
        Path(p).write_text(json.dumps(m))
        workflow.review(p, self.report(r, 'scale'))
        result = workflow.adopt_export(p, out)
        self.assertFalse(result['delivery_allowed'])
        m = json.loads(Path(p).read_text())
        self.assertEqual(m['exports'][0]['origin'], 'adopted_existing_export')
        self.assertEqual(json.loads((out/'delivery.json').read_text())['state'], 'VISUAL_REJECTED')

    def test_legacy_adoption_rejects_wrong_manifest_and_changed_image(self):
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        m = json.loads(Path(p).read_text()); m.pop('exports'); Path(p).write_text(json.dumps(m))
        receipt = json.loads((out/'delivery.json').read_text())
        receipt['run_manifest'] = str(self.root/'somebody-else.json')
        (out/'delivery.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(WorkflowError, 'EXPORT_ADOPTION_INVALID'):
            workflow.adopt_export(p, out)
        receipt['run_manifest'] = p; (out/'delivery.json').write_text(json.dumps(receipt))
        (out/'sprite-sheet.png').write_bytes(b'third party')
        with self.assertRaisesRegex(WorkflowError, 'EXPORT_ADOPTION_INVALID'):
            workflow.adopt_export(p, out)

    def test_export_refuses_symlink_parent(self):
        r = self.approved(); p = r['run_manifest']
        real = self.root/'real'; real.mkdir()
        link = self.root/'alias'
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest('OS does not permit fixture symlinks: '+str(exc))
        with self.assertRaisesRegex(WorkflowError, 'OUTPUT_CONFLICT'):
            workflow.export(p, link/'final')
        self.assertFalse((real/'final').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_export_refuses_real_windows_junction_parent(self):
        r = self.approved(); p = r['run_manifest']
        real = self.root/'real'; real.mkdir(); link = self.root/'alias'
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)],
                                 capture_output=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr.decode(errors='replace'))
        self.assertTrue(link.is_junction())
        with self.assertRaisesRegex(WorkflowError, 'OUTPUT_CONFLICT'):
            workflow.export(p, link/'final')
        self.assertFalse((real/'final').exists())

    def test_revocation_refuses_symlink_receipt(self):
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        victim = self.root/'unrelated.json'; victim.write_bytes(b'keep this document')
        receipt = out/'delivery.json'; original = receipt.read_bytes(); receipt.unlink()
        try:
            receipt.symlink_to(victim)
        except OSError as exc:
            receipt.write_bytes(original)
            self.skipTest('OS does not permit fixture symlinks: '+str(exc))
        result = workflow.review(p, self.report(r, 'scale'))
        self.assertEqual(victim.read_bytes(), b'keep this document')
        self.assertTrue(result['export_revocation_pending'])

    def test_repeated_adoption_recovers_interrupted_revocation(self):
        import native_exports
        r = self.approved(); p = r['run_manifest']; out = self.root/'final'
        workflow.export(p, out)
        m = json.loads(Path(p).read_text()); m.pop('exports'); Path(p).write_text(json.dumps(m))
        workflow.review(p, self.report(r, 'scale'))
        real = native_exports.atomic_json
        def interrupted(path, value):
            if Path(path).name == 'visual-review.json':
                raise PermissionError('fixture disconnected destination')
            return real(path, value)
        with patch.object(native_exports, 'atomic_json', side_effect=interrupted):
            workflow.adopt_export(p, out)
        workflow.adopt_export(p, out)
        self.assertEqual(json.loads((out/'visual-review.json').read_text())['decision'], 'reject')


if __name__ == '__main__':
    unittest.main()
