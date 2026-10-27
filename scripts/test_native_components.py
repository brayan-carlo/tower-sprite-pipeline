"""Disconnected art is diagnosed, never deleted without pixel-bound review."""
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw
from sheet_workflow_state import WorkflowError


class NativeComponentsTests(unittest.TestCase):
    def api(self):
        try:
            return importlib.import_module('native_components')
        except ModuleNotFoundError:
            self.fail('native_components API is missing')

    def fixture(self):
        image = Image.new('RGBA', (64, 64))
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 25, 45, 55), fill=(60, 70, 80, 255))
        draw.rectangle((20, 8, 25, 13), fill=(120, 100, 90, 255))
        return image

    def decisions(self, report, action='preserve_as_art'):
        return [{'id': c['id'], 'decision': action, 'notes': 'Verified detached accessory.'}
                for c in report['components'] if not c['primary']]

    def reject(self, code, image, report, decisions):
        with self.assertRaises(WorkflowError) as caught:
            self.api().apply_component_review(image, report, decisions)
        self.assertEqual(caught.exception.code, code)

    def test_detects_vertical_six_by_six_fragment_inside_body_x_span(self):
        image = self.fixture()
        before = image.tobytes()
        report = self.api().analyze_components(image)
        secondary = [c for c in report['components'] if not c['primary']]
        self.assertEqual(len(secondary), 1)
        self.assertEqual(secondary[0]['bbox'], [20, 8, 26, 14])
        self.assertEqual(secondary[0]['pixels'], 36)
        self.assertTrue(secondary[0]['requires_review'])
        self.assertEqual(image.tobytes(), before)
        self.assertEqual(report, self.api().analyze_components(image.copy()))

    def test_every_secondary_is_flagged_including_isolated_faint_art(self):
        image = self.fixture()
        image.putpixel((55, 20), (10, 20, 30, 8))
        image.putpixel((55, 45), (10, 20, 30, 255))
        image.putpixel((56, 46), (10, 20, 30, 255))
        report = self.api().analyze_components(image)
        self.assertEqual(len(report['components']), 4)
        self.assertEqual(sum(c['requires_review'] for c in report['components']), 3)
        self.assertEqual(sorted(c['pixels'] for c in report['components']), [1, 2, 36, 1116])

    def test_preserve_keeps_accessory_and_every_original_rgba_byte(self):
        image = self.fixture()
        image.putpixel((0, 0), (250, 10, 30, 0))
        report = self.api().analyze_components(image)
        reviewed = self.api().apply_component_review(image, report, self.decisions(report))
        self.assertEqual(reviewed.size, image.size)
        self.assertEqual(reviewed.tobytes(), image.tobytes())
        self.assertIsNot(reviewed, image)

    def test_remove_uses_exact_component_mask_and_its_unique_faint_halo(self):
        image = self.fixture()
        image.putpixel((19, 8), (20, 30, 40, 7))
        # Main body includes disconnected bbox overlap without pixel overlap.
        ImageDraw.Draw(image).line((10, 25, 10, 4, 30, 4, 30, 25), fill=(60, 70, 80, 255))
        report = self.api().analyze_components(image)
        before = image.tobytes()
        reviewed = self.api().apply_component_review(image, report, self.decisions(report, 'remove_as_background'))
        for y in range(64):
            for x in range(64):
                removed = (20 <= x <= 25 and 8 <= y <= 13) or (x, y) == (19, 8)
                self.assertEqual(reviewed.getpixel((x, y)), (0, 0, 0, 0) if removed else image.getpixel((x, y)))
        self.assertEqual(image.tobytes(), before)

    def test_stale_pixels_or_modified_report_are_rejected(self):
        image = self.fixture()
        report = self.api().analyze_components(image)
        changed = image.copy()
        changed.putpixel((10, 25), (59, 70, 80, 255))
        self.reject('COMPONENT_REPORT_STALE', changed, report, self.decisions(report))
        tampered = copy.deepcopy(report)
        tampered['components'][0]['bbox'][0] += 1
        self.reject('COMPONENT_REPORT_STALE', image, tampered, self.decisions(report))

    def test_missing_unknown_duplicate_primary_and_bad_notes_are_rejected(self):
        image = self.fixture()
        report = self.api().analyze_components(image)
        good = self.decisions(report)
        primary = next(c for c in report['components'] if c['primary'])
        cases = [('COMPONENT_DECISIONS_REQUIRED', []),
                 ('COMPONENT_DECISION_INVALID', good + good),
                 ('COMPONENT_DECISION_INVALID', good + [{'id': 'unknown', 'decision': 'preserve_as_art', 'notes': 'Looks like an accessory.'}]),
                 ('COMPONENT_PRIMARY_PROTECTED', good + [{'id': primary['id'], 'decision': 'remove_as_background', 'notes': 'Incorrect deletion attempt.'}]),
                 ('COMPONENT_DECISION_INVALID', [{**good[0], 'notes': 'short'}]),
                 ('COMPONENT_DECISION_INVALID', [{**good[0], 'decision': 'auto'}])]
        for code, decisions in cases:
            with self.subTest(code=code, decisions=decisions):
                self.reject(code, image, report, decisions)

    def test_shared_faint_bridge_cannot_merge_opaque_components_or_be_erased(self):
        image = self.fixture()
        ImageDraw.Draw(image).line((22, 14, 22, 24), fill=(10, 20, 30, 8))
        report = self.api().analyze_components(image)
        self.assertEqual(len(report['components']), 2)
        self.reject('COMPONENT_HALO_AMBIGUOUS', image, report, self.decisions(report, 'remove_as_background'))
        reviewed = self.api().apply_component_review(image, report, self.decisions(report))
        self.assertEqual(reviewed.tobytes(), image.tobytes())

    def test_bounds_and_rgba_are_validated_before_analysis(self):
        for image, code in [(Image.new('RGBA', (4097, 1)), 'IMAGE_LIMIT_EXCEEDED'),
                            (Image.new('RGBA', (3000, 3000)), 'IMAGE_LIMIT_EXCEEDED'),
                            (Image.new('RGB', (64, 64)), 'COMPONENT_IMAGE_INVALID')]:
            with self.subTest(code=code):
                with self.assertRaises(WorkflowError) as caught:
                    self.api().analyze_components(image)
                self.assertEqual(caught.exception.code, code)

    def test_processing_writes_eight_pixel_bound_reports_without_deleting_fragment(self):
        from native_sheet_images import process_sheet
        with tempfile.TemporaryDirectory() as tmp:
            image = Image.new('RGBA', (1200, 600), (244, 6, 242, 255))
            draw = ImageDraw.Draw(image)
            for row in range(2):
                for col in range(4):
                    draw.rectangle((col*300+75, row*300+80, col*300+180, row*300+230), fill=(40, 80, 60, 255))
            draw.rectangle((100, 50, 105, 55), fill=(80, 80, 80, 255))
            raw = Path(tmp)/'raw.png'
            image.save(raw)
            result = process_sheet(raw, Path(tmp)/'out')
            self.assertIn('components_report', result['paths'])
            report = json.loads(Path(result['paths']['components_report']).read_text('utf-8'))
            self.assertEqual(len(report['frames']), 8)
            self.assertEqual([c['pixels'] for c in report['frames'][0]['components'] if not c['primary']], [36])
            with Image.open(result['paths']['registered']) as registered:
                for frame in report['frames']:
                    index = frame['frame'] - 1
                    x, y = (index % 4)*512, (index // 4)*512
                    expected = self.api().analyze_components(registered.crop((x, y, x+512, y+512)))
                    self.assertEqual({k: v for k, v in frame.items() if k != 'frame'}, expected)


if __name__ == '__main__':
    unittest.main()
