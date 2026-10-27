"""Behavior tests for the bounded, local native sheet processor."""
import importlib
import hashlib
import inspect
import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw
from sheet_workflow_state import WorkflowError


class NativeImagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def api(self):
        try:
            return importlib.import_module('native_sheet_images')
        except ModuleNotFoundError:
            self.fail('Native image processing API has not been implemented')

    def source(self, color=(80, 80, 80, 255)):
        image = Image.new('RGBA', (128, 128))
        ImageDraw.Draw(image).rectangle((20, 20, 100, 110), fill=color)
        path = self.root / 'source.png'
        image.save(path)
        return path

    def raw(self, background=(244, 6, 242, 255)):
        image = Image.new('RGBA', (1200, 600), background)
        draw = ImageDraw.Draw(image)
        for row in range(2):
            for col, bottom in enumerate((230, 190, 230, 170)):
                # Unequal silhouettes ensure the assembly detects actual phases.
                draw.rectangle((col*300+75, row*300+80, col*300+180+col*3,
                                row*300+bottom), fill=(40+col*20, 80+row*5, 60, 255))
        path = self.root / 'raw.png'
        image.save(path)
        return path

    def reject(self, code, operation):
        with self.assertRaises(WorkflowError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def strict_process(self, path, output):
        process = self.api().process_sheet
        self.assertIn('strict_components', inspect.signature(process).parameters,
                      'Strict processing API is missing')
        return process(path, output, strict_components=True)

    def matte_diagnostic(self):
        diagnose = getattr(self.api(), 'diagnose_matte', None)
        self.assertTrue(callable(diagnose), 'Matte diagnostic API is missing')
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0, 0), (80, 80, 80, 32))
        image.save(path)
        before = path.read_bytes()
        result = diagnose(path, self.root/'diagnosis')
        self.assertEqual(path.read_bytes(), before)
        report = json.loads(Path(result['paths']['components_report']).read_text('utf-8'))
        return path, result, report

    def matte_decisions(self, report):
        return {'image_pixel_sha256': report['image_pixel_sha256'], 'decisions': [
            {'id': component['id'],
             'decision': 'remove_as_background' if component['bbox'] == [0, 0, 1, 1] else 'preserve_as_art',
             'notes': 'Reviewed exact corner background.' if component['bbox'] == [0, 0, 1, 1] else 'Preserve this distinct stride pose.'}
            for component in report['components'] if not component['primary']]}

    def test_matte_diagnosis_preserves_input_and_refuses_duplicate_outputs(self):
        path, diagnosis, report = self.matte_diagnostic()
        self.assertEqual(len(report['components']), 9)
        with Image.open(diagnosis['paths']['alpha']) as alpha:
            self.assertEqual(alpha.getpixel((0, 0)), (80, 80, 80, 32))
        before = {key: Path(value).read_bytes() for key, value in diagnosis['paths'].items()}
        self.reject('OUTPUT_CONFLICT', lambda: self.api().diagnose_matte(path, self.root/'diagnosis'))
        self.assertEqual(before, {key: Path(value).read_bytes() for key, value in diagnosis['paths'].items()})

    def test_matte_corner_removal_is_explicit_bound_and_audited(self):
        path, diagnosis, report = self.matte_diagnostic()
        before = path.read_bytes()
        review = self.matte_decisions(report)
        result = self.api().process_sheet(path, self.root/'out', strict_components=True, matte_review=review)
        self.assertEqual(path.read_bytes(), before)
        with Image.open(result['paths']['alpha_before_review']) as original, Image.open(result['paths']['alpha']) as reviewed:
            self.assertEqual(original.tobytes()[4:], reviewed.tobytes()[4:])
            self.assertEqual(original.getpixel((0, 0)), (80, 80, 80, 32))
            self.assertEqual(reviewed.getpixel((0, 0)), (0, 0, 0, 0))
        receipt = json.loads(Path(result['paths']['matte_review']).read_text('utf-8'))
        self.assertEqual(receipt['components_report'], report)
        self.assertEqual(receipt['decisions'], review['decisions'])
        for key in ('alpha_before_review', 'matte_review', 'alpha'):
            self.assertEqual(result['details']['matte_review']['artifact_sha256'][key],
                             hashlib.sha256(Path(result['paths'][key]).read_bytes()).hexdigest())
        saved = {key: Path(value).read_bytes() for key, value in result['paths'].items()}
        self.reject('OUTPUT_CONFLICT', lambda: self.api().process_sheet(path, self.root/'out', strict_components=True, matte_review=review))
        self.assertEqual(saved, {key: Path(value).read_bytes() for key, value in result['paths'].items()})

    def test_matte_stale_hash_rejected_before_writing_outputs(self):
        path, diagnosis, report = self.matte_diagnostic()
        review = self.matte_decisions(report)
        review['image_pixel_sha256'] = '0'*64
        self.reject('COMPONENT_REPORT_STALE', lambda: self.api().process_sheet(path, self.root/'out', strict_components=True, matte_review=review))
        self.assertFalse((self.root/'out').exists())

    def test_matte_review_for_changed_raw_pixels_is_rejected(self):
        path, diagnosis, report = self.matte_diagnostic()
        with Image.open(path) as original:
            changed = original.convert('RGBA')
        changed.putpixel((80, 100), (41, 80, 60, 255))
        changed.save(path)
        self.reject('COMPONENT_REPORT_STALE', lambda: self.api().process_sheet(path, self.root/'out', strict_components=True, matte_review=self.matte_decisions(report)))
        self.assertFalse((self.root/'out').exists())

    def test_matte_review_requires_decisions_for_other_poses_and_protects_primary(self):
        path, diagnosis, report = self.matte_diagnostic()
        review = self.matte_decisions(report)
        review['decisions'] = [d for d in review['decisions'] if d['decision'] == 'remove_as_background']
        self.reject('COMPONENT_DECISIONS_REQUIRED', lambda: self.api().process_sheet(path, self.root/'missing', strict_components=True, matte_review=review))
        review = self.matte_decisions(report)
        primary = next(c for c in report['components'] if c['primary'])
        review['decisions'].append({'id': primary['id'], 'decision': 'remove_as_background', 'notes': 'Invalid removal of primary pose.'})
        self.reject('COMPONENT_PRIMARY_PROTECTED', lambda: self.api().process_sheet(path, self.root/'primary', strict_components=True, matte_review=review))

    def test_matte_review_cannot_authorize_legacy_or_implicitly_delete_corner(self):
        path, diagnosis, report = self.matte_diagnostic()
        review = self.matte_decisions(report)
        self.reject('MATTE_REVIEW_REQUIRES_STRICT', lambda: self.api().process_sheet(path, self.root/'legacy', matte_review=review))
        for decision in review['decisions']:
            decision['decision'] = 'preserve_as_art'
        self.reject('UNREVIEWED_ALPHA_LOSS', lambda: self.api().process_sheet(path, self.root/'preserved', strict_components=True, matte_review=review))
        self.assertFalse((self.root/'preserved').exists())

    def test_strict_preserves_and_reports_faint_vertical_six_by_six_fragment(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        ImageDraw.Draw(image).rectangle((100, 50, 105, 55), fill=(80, 80, 80, 8))
        image.save(path)
        result = self.strict_process(path, self.root/'out')
        components = json.loads(Path(result['paths']['components_report']).read_text('utf-8'))
        secondary = [c for c in components['frames'][0]['components'] if not c['primary']]
        self.assertEqual([(c['pixels'], c['visible_pixels']) for c in secondary], [(36, 0)])
        with Image.open(result['paths']['alpha']) as alpha, Image.open(result['paths']['registered']) as registered:
            self.assertEqual(Counter(p for p in alpha.get_flattened_data() if p[3]),
                             Counter(p for p in registered.get_flattened_data() if p[3]))
        self.assertTrue(result['details']['registration']['strict_components'])

    def test_strict_preserves_faint_halo_beyond_old_crop_margin(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        ImageDraw.Draw(image).rectangle((68, 100, 74, 110), fill=(70, 80, 90, 1))
        image.save(path)
        result = self.strict_process(path, self.root/'out')
        with Image.open(result['paths']['registered']) as registered:
            self.assertEqual(sum(p == (70, 80, 90, 1) for p in registered.get_flattened_data()), 77)
        report = json.loads(Path(result['paths']['components_report']).read_text('utf-8'))
        self.assertEqual(len(report['frames'][0]['components']), 1)

    def test_legacy_corner_exception_is_rejected_by_strict_processing(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0, 0), (80, 80, 80, 32))
        image.save(path)
        legacy = self.api().process_sheet(path, self.root/'legacy')
        self.assertEqual(legacy['details']['registration']['ignored_far_edge_background'],
                         [{'row': 1, 'bounds': [0, 1], 'pixels': 1}])
        self.reject('UNREVIEWED_ALPHA_LOSS', lambda: self.strict_process(path, self.root/'strict'))
        self.assertFalse((self.root/'strict').exists())

    def test_strict_rejects_unassignable_faint_region_instead_of_dropping_it(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        ImageDraw.Draw(image).rectangle((240, 100, 245, 105), fill=(80, 80, 80, 8))
        image.save(path)
        self.reject('UNREVIEWED_ALPHA_LOSS', lambda: self.strict_process(path, self.root/'out'))
        self.assertFalse((self.root/'out').exists())

    def test_strict_rejects_all_alpha_that_cannot_fit_registered_canvas(self):
        image = Image.new('RGBA', (1200, 1000), (244, 6, 242, 255))
        draw = ImageDraw.Draw(image)
        for row in range(2):
            for col in range(4):
                draw.rectangle((col*300+75, row*500+20, col*300+180, row*500+490), fill=(40, 80, 60, 255))
        path = self.root/'tall.png'
        image.save(path)
        self.reject('UNREVIEWED_ALPHA_LOSS', lambda: self.strict_process(path, self.root/'out'))
        self.assertFalse((self.root/'out').exists())

    def test_accepts_isolated_png(self):
        source = self.source()
        before = source.read_bytes()
        self.assertEqual(self.api().validate_source(source), (128, 128))
        self.assertEqual(source.read_bytes(), before)

    def test_pixel_digest_ignores_png_compression_and_detects_pixel_change(self):
        path = self.source()
        compact = self.root/'compact.png'
        uncompressed = self.root/'uncompressed.png'
        changed = self.root/'changed.png'
        with Image.open(path) as image:
            image.save(compact,compress_level=9)
            image.save(uncompressed,compress_level=0)
            image.putpixel((50,50),(79,80,80,255))
            image.save(changed,compress_level=9)
        self.assertNotEqual(compact.read_bytes(),uncompressed.read_bytes())
        api=self.api()
        self.assertTrue(callable(getattr(api,'pixel_sha256',None)), 'pixel_sha256 API is missing')
        self.assertEqual(api.pixel_sha256(compact),api.pixel_sha256(uncompressed))
        self.assertNotEqual(api.pixel_sha256(compact),api.pixel_sha256(changed))

    def test_rejects_opaque_source(self):
        path = self.root/'opaque.png'
        Image.new('RGBA', (128,128), 'gray').save(path)
        self.reject('SOURCE_ALPHA_REQUIRED', lambda:self.api().validate_source(path))

    def test_rejects_single_visible_magenta_detail(self):
        path = self.source()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((50,50), (200,50,200,255))
        image.save(path)
        self.reject('SOURCE_KEY_COLLISION', lambda:self.api().validate_source(path))

    def test_rejects_small_color_shift_that_matte_would_modify(self):
        self.reject('SOURCE_KEY_COLLISION', lambda:self.api().validate_source(self.source((70,60,70,255))))

    def test_ignores_rgb_in_fully_transparent_source_pixels(self):
        path = self.source()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0,0), (255,0,255,0))
        image.save(path)
        self.assertEqual(self.api().validate_source(path), (128,128))

    def test_rejects_non_png_and_empty_source(self):
        path = self.root/'not-png.png'
        Image.new('RGB',(128,128),'gray').save(path,format='JPEG')
        self.reject('PNG_REQUIRED',lambda:self.api().validate_source(path))
        Image.new('RGBA',(128,128)).save(path)
        self.reject('SOURCE_EMPTY',lambda:self.api().validate_source(path))

    def test_rejects_oversized_source(self):
        path = self.root/'wide.png'
        Image.new('RGBA',(4097,16)).save(path)
        self.reject('IMAGE_LIMIT_EXCEEDED',lambda:self.api().validate_source(path))

    def test_rejects_wrong_key_without_outputs(self):
        self.reject('KEY_MISMATCH',lambda:self.api().process_sheet(self.raw((0,255,0,255)),self.root/'out'))
        self.assertFalse((self.root/'out'/'registered.png').exists())

    def test_accepts_small_magenta_border_variations(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        for x in range(image.width):
            for y in (0,image.height-1):
                image.putpixel((x,y),(244-(x%3),6+(x%4),242,255))
        image.save(path)
        result=self.api().process_sheet(path,self.root/'out')
        self.assertTrue(Path(result['paths']['registered']).is_file())

    def test_rejects_ambiguous_detached_content(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        ImageDraw.Draw(image).rectangle((240,100,245,110),fill='gray')
        image.save(path)
        self.reject('POSE_SEPARATION_AMBIGUOUS',lambda:self.api().process_sheet(path,self.root/'out'))

    def test_rejects_pose_clipped_at_stride_boundary(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        ImageDraw.Draw(image).rectangle((75,0,180,80),fill='gray')
        image.save(path)
        self.reject('POSE_CLIPPED',lambda:self.api().process_sheet(path,self.root/'out'))

    def test_logs_only_tiny_translucent_corner_residue(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0,0),(80,80,80,32))
        image.save(path)
        result = self.api().process_sheet(path,self.root/'out')
        report = json.loads(Path(result['paths']['processing_report']).read_text('utf-8'))
        self.assertEqual(report['registration']['ignored_far_edge_background'],[{'row':1,'bounds':[0,1],'pixels':1}])

    def test_rejects_opaque_corner_residue(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0,0),(80,80,80,255))
        image.save(path)
        self.reject('POSE_SEPARATION_AMBIGUOUS',lambda:self.api().process_sheet(path,self.root/'out'))

    def test_rejects_translucent_mid_border_residue(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0,150),(80,80,80,32))
        image.save(path)
        self.reject('POSE_SEPARATION_AMBIGUOUS',lambda:self.api().process_sheet(path,self.root/'out'))

    def test_rejects_translucent_stride_corner_inside_complete_image(self):
        path = self.raw()
        with Image.open(path) as original:
            image = original.convert('RGBA')
        image.putpixel((0,300),(80,80,80,32))
        image.save(path)
        self.reject('POSE_SEPARATION_AMBIGUOUS',lambda:self.api().process_sheet(path,self.root/'out'))

    def test_registration_preserves_pixels_and_flight_offsets(self):
        path = self.raw()
        before = path.read_bytes()
        result = self.api().process_sheet(path,self.root/'out')
        with Image.open(result['paths']['registered']) as registered:
            self.assertEqual(registered.size,(2048,1024))
            self.assertEqual(registered.mode,'RGBA')
            bottoms = []
            for col in range(4):
                cell=registered.crop((col*512,0,(col+1)*512,512))
                box=cell.getchannel('A').getbbox()
                bottoms.append(box[3])
                self.assertEqual(cell.getpixel((box[0],box[1])),(40+col*20,80,60,255))
                self.assertEqual(box[2]-box[0],106+col*3)
            self.assertEqual(bottoms,[440,400,440,380])
        self.assertEqual(path.read_bytes(),before)

    def test_refuses_output_collision(self):
        path = self.raw()
        target=self.root/'out'
        target.mkdir()
        sentinel=target/'registered.png'
        sentinel.write_bytes(b'preserve me')
        self.reject('OUTPUT_CONFLICT',lambda:self.api().process_sheet(path,target))
        self.assertEqual(sentinel.read_bytes(),b'preserve me')

    def test_common_scale_assembly_preserves_flight(self):
        from sheet_images import assemble_sheet
        result=self.api().process_sheet(self.raw(),self.root/'out')
        assembled=assemble_sheet(result['paths']['registered'],self.root/'assembly',
                                 frame_count=8,columns=4,rows=2,canvas=(256,256),
                                 frame_ms=100,loop=True,pixel_art=True,grounded=False)
        paths=assembled['artifact_paths']
        with Image.open(paths['sheet']) as image:
            boxes=[image.crop((col*256,0,(col+1)*256,256)).getchannel('A').getbbox() for col in range(4)]
        self.assertGreater(boxes[0][3],boxes[1][3])
        self.assertGreater(boxes[2][3],boxes[3][3])


if __name__=='__main__':
    unittest.main()
