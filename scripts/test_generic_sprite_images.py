import json
import io
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from PIL import Image
from PIL import ImageSequence

sys.path.insert(0, str(Path(__file__).parent))
import generic_sprite_images as images


class GenericSpriteImagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_transparent_source_is_grounded_without_background_rewrite(self):
        source = Image.new("RGBA", (9, 11), (0, 0, 0, 0))
        source.paste((10, 20, 30, 255), (1, 2, 5, 8))
        source.save(self.path / "transparent.png")
        normalized, report = images.normalize_source(self.path / "transparent.png", None)
        self.assertEqual((9, 11), normalized.size)
        self.assertEqual((2, 5, 6, 11), normalized.getchannel("A").getbbox())
        self.assertEqual("preserved-alpha", report["background_action"])

    def test_exact_uniform_border_removes_only_edge_connected_background(self):
        source = Image.new("RGB", (9, 9), (255, 0, 255))
        source.paste((20, 30, 40), (2, 2, 7, 8))
        source.putpixel((4, 4), (255, 0, 255))
        source.save(self.path / "uniform.png")
        normalized, report = images.normalize_source(self.path / "uniform.png", None)
        self.assertEqual(255, normalized.getpixel((4, 5))[3])
        self.assertEqual(0, normalized.getpixel((0, 0))[3])
        self.assertEqual("removed-exact-uniform-border", report["background_action"])

    def test_complex_opaque_border_fails_closed(self):
        source = Image.new("RGB", (7, 7), (255, 0, 255))
        source.putpixel((0, 3), (0, 255, 0))
        source.save(self.path / "complex.png")
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_source(self.path / "complex.png", None)
        self.assertEqual("INPUT_BACKGROUND_COMPLEX", str(caught.exception))

    def test_non_png_bytes_fail_closed(self):
        path = self.path / "source.png"
        path.write_bytes(b"not a png")
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_source(path, None)
        self.assertEqual("INPUT_NOT_PNG", str(caught.exception))

    def test_empty_alpha_fails_closed(self):
        Image.new("RGBA", (4, 4), (1, 2, 3, 0)).save(self.path / "empty.png")
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_source(self.path / "empty.png", None)
        self.assertEqual("INPUT_EMPTY_ALPHA", str(caught.exception))

    def test_invalid_output_size_fails_closed(self):
        Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(self.path / "valid.png")
        for size in ((0, 4), (4, 0), (-1, 2)):
            with self.subTest(size=size), self.assertRaises(images.GenericImageBlocked) as caught:
                images.normalize_source(self.path / "valid.png", size)
            self.assertEqual("OUTPUT_SIZE_INVALID", str(caught.exception))

    def test_unsupported_resampling_fails_closed(self):
        Image.new("RGBA", (2, 2), (1, 2, 3, 255)).save(self.path / "valid.png")
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_source(self.path / "valid.png", (4, 4), "bilinear")
        self.assertEqual("RESAMPLE_MODE_INVALID", str(caught.exception))

    def test_equal_output_size_preserves_subject_scale(self):
        source = Image.new("RGBA", (20, 12), (0, 0, 0, 0))
        source.paste((90, 80, 70, 255), (4, 2, 16, 10))
        source.save(self.path / "same-size.png")
        normalized, _ = images.normalize_source(self.path / "same-size.png", (20, 12))
        self.assertEqual((4, 4, 16, 12), normalized.getchannel("A").getbbox())

    def test_reduction_that_loses_all_alpha_fails_closed(self):
        source = Image.new("RGBA", (9, 9), (0, 0, 0, 0))
        source.putpixel((0, 8), (90, 80, 70, 255))
        source.putpixel((8, 0), (90, 80, 70, 255))
        source.save(self.path / "dispersed.png")
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_source(self.path / "dispersed.png", (1, 1))
        self.assertEqual("INPUT_EMPTY_ALPHA", str(caught.exception))

    def test_missing_path_is_translated_to_not_png(self):
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_source(self.path / "missing.png", None)
        self.assertEqual("INPUT_NOT_PNG", str(caught.exception))

    def test_subject_fits_output_without_cropping(self):
        source = Image.new("RGBA", (7, 9), (0, 0, 0, 0))
        source.paste((90, 80, 70, 255), (1, 1, 6, 8))
        source.save(self.path / "subject.png")
        normalized, report = images.normalize_source(self.path / "subject.png", (20, 12))
        self.assertEqual((20, 12), normalized.size)
        self.assertEqual((6, 0, 14, 12), normalized.getchannel("A").getbbox())
        self.assertEqual([20, 12], report["normalized_size"])
        json.dumps(report)

    def test_hash_and_report_are_json_native(self):
        path = self.path / "valid.png"
        solid = Image.new("RGB", (3, 3), (1, 2, 3))
        solid.putpixel((1, 1), (4, 5, 6))
        solid.save(path)
        normalized, report = images.normalize_source(path, None)
        self.assertEqual(images.sha256_file(path), report["source_sha256"])
        self.assertEqual("RGB", report["source_mode"])
        self.assertEqual([3, 3], report["source_size"])
        self.assertIsInstance(report["alpha_bbox"], list)
        json.dumps(report)

    @staticmethod
    def generated_manifest(width: int = 12, height: int = 14) -> dict:
        return {
            "output": {
                "width": width,
                "height": height,
                "background": "transparent",
                "anchor": "bottom-center",
                "resample_mode": "nearest",
            }
        }

    def test_generated_rgb_recovers_its_actual_uniform_edge_background(self):
        raw = Image.new("RGB", (9, 11), (231, 17, 199))
        raw.paste((20, 30, 40), (2, 2, 7, 10))
        raw.putpixel((4, 5), (231, 17, 199))
        path = self.path / "generated-rgb.png"
        raw.save(path)

        normalized, report = images.normalize_generated_frame(
            path,
            self.generated_manifest(9, 11),
        )

        self.assertEqual((9, 11), normalized.size)
        self.assertEqual(11, normalized.getchannel("A").getbbox()[3])
        self.assertEqual("removed-detected-exact-uniform-border", report["background_action"])
        self.assertEqual([231, 17, 199], report["detected_background_rgb"])
        self.assertEqual([2, 2, 7, 10], report["raw_alpha_bbox"])
        self.assertEqual([9, 11], report["normalized_size"])
        json.dumps(report)

    def test_generated_rgba_preserves_existing_alpha_without_inventing_chroma(self):
        raw = Image.new("RGBA", (9, 11), (77, 88, 99, 0))
        raw.paste((20, 30, 40, 128), (2, 2, 7, 10))
        path = self.path / "generated-rgba.png"
        raw.save(path)

        normalized, report = images.normalize_generated_frame(
            path,
            self.generated_manifest(9, 11),
        )

        self.assertEqual((0, 128), normalized.getchannel("A").getextrema())
        self.assertEqual("preserved-rgba-alpha", report["background_action"])
        self.assertIsNone(report["detected_background_rgb"])

    def test_generated_empty_and_nonuniform_prohibited_edge_fail_closed(self):
        empty = self.path / "empty-generated.png"
        Image.new("RGBA", (5, 6), (1, 2, 3, 0)).save(empty)
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_generated_frame(empty, self.generated_manifest())
        self.assertEqual("FRAME_EMPTY", str(caught.exception))

        complex_path = self.path / "complex-generated.png"
        complex_image = Image.new("RGB", (7, 7), (250, 0, 250))
        complex_image.putpixel((0, 3), (0, 250, 0))
        complex_image.save(complex_path)
        with self.assertRaises(images.GenericImageBlocked) as caught:
            images.normalize_generated_frame(complex_path, self.generated_manifest())
        self.assertEqual("FRAME_CLIPPED", str(caught.exception))

    def test_generated_opaque_rgba_recovers_background_but_preserves_enclosed_same_color(self):
        raw = Image.new("RGBA", (9, 11), (231, 17, 199, 255))
        raw.paste((20, 30, 40, 255), (2, 2, 7, 11))
        raw.putpixel((4, 5), (231, 17, 199, 255))
        path = self.path / "opaque-rgba.png"
        raw.save(path)

        normalized, report = images.normalize_generated_frame(
            path,
            self.generated_manifest(9, 11),
        )

        self.assertEqual("removed-detected-exact-uniform-border", report["background_action"])
        self.assertEqual([231, 17, 199], report["detected_background_rgb"])
        opaque_chroma = [
            pixel
            for pixel in normalized.get_flattened_data()
            if pixel[:3] == (231, 17, 199) and pixel[3] > 0
        ]
        self.assertEqual([(231, 17, 199, 255)], opaque_chroma)

    def test_generated_clipping_is_detected_on_raw_before_resize(self):
        cases = {
            "left": (0, 2, 4, 9),
            "right": (5, 2, 9, 9),
            "top": (2, 0, 7, 9),
        }
        for label, box in cases.items():
            with self.subTest(edge=label):
                raw = Image.new("RGBA", (9, 11), (0, 0, 0, 0))
                raw.paste((20, 30, 40, 255), box)
                path = self.path / f"clipped-{label}.png"
                raw.save(path)
                with self.assertRaises(images.GenericImageBlocked) as caught:
                    images.normalize_generated_frame(path, self.generated_manifest())
                self.assertEqual("FRAME_CLIPPED", str(caught.exception))

    def test_generated_resize_reserves_transparent_top_and_side_margins(self):
        raw = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        raw.paste((20, 30, 40, 255), (2, 1, 6, 8))
        path = self.path / "post-resize-margin.png"
        raw.save(path)

        normalized, report = images.normalize_generated_frame(
            path,
            self.generated_manifest(4, 8),
        )

        self.assertEqual((4, 8), normalized.size)
        self.assertEqual((1, 5, 3, 8), normalized.getchannel("A").getbbox())
        self.assertEqual([1, 5, 3, 8], report["alpha_bbox"])
        self.assertEqual(8, report["alpha_bbox"][3])

    def test_generated_resize_rejects_canvases_too_small_for_required_margins(self):
        raw = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        raw.paste((20, 30, 40, 255), (2, 1, 6, 8))
        path = self.path / "too-small-margin.png"
        raw.save(path)

        for size in ((2, 8), (4, 1)):
            with self.subTest(size=size), self.assertRaises(
                images.GenericImageBlocked
            ) as caught:
                images.normalize_generated_frame(path, self.generated_manifest(*size))
            self.assertEqual("FRAME_CLIPPED", str(caught.exception))

    def test_generated_output_dimension_override_is_bottom_centered(self):
        raw = Image.new("RGBA", (20, 12), (0, 0, 0, 0))
        raw.paste((20, 30, 40, 255), (6, 2, 14, 11))
        path = self.path / "dimension-override.png"
        raw.save(path)

        normalized, report = images.normalize_generated_frame(
            path,
            self.generated_manifest(20, 12),
        )

        bbox = normalized.getchannel("A").getbbox()
        self.assertEqual((20, 12), normalized.size)
        self.assertEqual(12, bbox[3])
        self.assertEqual((20 - (bbox[2] - bbox[0])) // 2, bbox[0])
        self.assertEqual([20, 12], report["normalized_size"])

    def test_generated_decode_only_translates_expected_image_failures(self):
        path = self.path / "decode-errors.png"
        Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(path)

        for exception in (OSError("decode"), ValueError("bad PNG chunk")):
            with self.subTest(exception=type(exception).__name__):
                with mock.patch.object(images.Image, "open", side_effect=exception):
                    with self.assertRaises(images.GenericImageBlocked) as caught:
                        images.normalize_generated_frame(path, self.generated_manifest())
                self.assertEqual("FRAME_NOT_PNG", str(caught.exception))

        for exception in (MemoryError("oom"), RuntimeError("bug"), AssertionError("bug")):
            with self.subTest(exception=type(exception).__name__):
                with mock.patch.object(images.Image, "open", side_effect=exception):
                    with self.assertRaises(type(exception)) as caught:
                        images.normalize_generated_frame(path, self.generated_manifest())
                self.assertEqual(str(exception), str(caught.exception))

        with mock.patch.object(
            images, "_recover_generated_uniform_border", side_effect=ValueError("normalization bug")
        ):
            with self.assertRaisesRegex(ValueError, "^normalization bug$"):
                images.normalize_generated_frame(path, self.generated_manifest())

    def test_generated_malformed_ancillary_chunks_are_decode_failures(self):
        buffer = io.BytesIO()
        source = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        source.paste((20, 30, 40, 255), (2, 1, 6, 8))
        source.save(buffer, format="PNG")
        valid = buffer.getvalue()
        for kind, payload in ((b"pHYs", b"\0"), (b"sRGB", b"")):
            with self.subTest(chunk=kind):
                chunk = (
                    struct.pack(">I", len(payload)) + kind + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
                )
                path = self.path / f"malformed-{kind.decode('ascii')}.png"
                path.write_bytes(valid[:33] + chunk + valid[33:])

                with self.assertRaises(images.GenericImageBlocked) as caught:
                    images.normalize_generated_frame(path, self.generated_manifest(8, 8))
                self.assertEqual("FRAME_NOT_PNG", str(caught.exception))
                self.assertIsInstance(caught.exception.__cause__, ValueError)
                self.assertEqual("PNG_DECODE_FAILED", caught.exception.evidence["advisory"]["measurement"]["reason"])

    def test_technical_report_itself_rejects_prohibited_edge_contact(self):
        manifest = self.generated_manifest(8, 8)
        master = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        master.paste((20, 30, 40, 255), (2, 3, 6, 8))
        cases = {
            "left-and-right": (0, 3, 8, 8),
            "top": (2, 0, 6, 8),
        }

        for label, box in cases.items():
            with self.subTest(edge=label):
                frame = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
                frame.paste((20, 30, 40, 255), box)
                report = images.technical_frame_report(
                    frame,
                    master,
                    None,
                    manifest,
                    1,
                )
                self.assertEqual("FAIL", report["technical_result"])
                self.assertEqual(["FRAME_CLIPPED"], report["failures"])

    def test_technical_report_exposes_advisory_drift_without_using_it_as_gate(self):
        master = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        master.paste((200, 10, 10, 255), (2, 3, 5, 8))
        previous = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        previous.paste((10, 10, 200, 255), (2, 3, 5, 8))
        frame = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        frame.paste((10, 200, 10, 255), (2, 2, 6, 8))

        report = images.technical_frame_report(
            frame,
            master,
            previous,
            self.generated_manifest(8, 8),
            2,
        )

        self.assertEqual(
            {
                "schema_version",
                "frame_index",
                "technical_result",
                "failures",
                "normalization",
                "advisory",
                "provenance",
            },
            set(report),
        )
        self.assertEqual("PASS", report["technical_result"])
        self.assertEqual([], report["failures"])
        self.assertEqual([2, 2, 6, 8], report["advisory"]["alpha_bbox"])
        self.assertEqual(24, report["advisory"]["opaque_pixels"])
        self.assertEqual(24, report["advisory"]["visible_pixels"])
        self.assertEqual(
            {
                "bbox_movement": "candidate_bbox_minus_reference_bbox_[left,top,right,bottom]",
                "normalized_pixel_difference_count": "exact_rgba_pixel_mismatches_on_equal_canvas",
                "silhouette": "alpha_gt_zero_intersection_and_union_pixel_counts",
                "dominant_color_histogram_distance": "integer_l1_distance_over_4bit_rgb_histograms_of_visible_pixels",
            },
            report["advisory"]["metric_definitions"],
        )
        for reference in ("master", "previous"):
            evidence = report["advisory"][reference]
            self.assertEqual(
                {
                    "bbox_movement",
                    "normalized_pixel_difference_count",
                    "silhouette_intersection",
                    "silhouette_union",
                    "dominant_color_histogram_distance",
                },
                set(evidence),
            )
            self.assertEqual([0, -1, 1, 0], evidence["bbox_movement"])
            self.assertEqual(24, evidence["normalized_pixel_difference_count"])
            self.assertEqual(15, evidence["silhouette_intersection"])
            self.assertEqual(24, evidence["silhouette_union"])
            self.assertEqual(39, evidence["dominant_color_histogram_distance"])

    def test_technical_failures_are_independent_of_advisory_similarity(self):
        manifest = self.generated_manifest(8, 8)
        master = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        master.paste((20, 30, 40, 255), (2, 3, 6, 8))
        ungrounded = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        ungrounded.paste((20, 30, 40, 255), (2, 2, 6, 7))

        report = images.technical_frame_report(
            ungrounded,
            master,
            None,
            manifest,
            1,
        )

        self.assertEqual("FAIL", report["technical_result"])
        self.assertEqual(["FRAME_NOT_GROUNDED"], report["failures"])
        self.assertIsNone(report["advisory"]["previous"])

    def test_advisory_counts_partial_alpha_as_visible_but_not_opaque(self):
        frame = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        frame.paste((20, 30, 40, 255), (2, 3, 6, 8))
        frame.putpixel((3, 4), (20, 30, 40, 128))

        report = images.technical_frame_report(
            frame,
            frame.copy(),
            None,
            self.generated_manifest(8, 8),
            1,
        )

        self.assertEqual("PASS", report["technical_result"])
        self.assertEqual(19, report["advisory"]["opaque_pixels"])
        self.assertEqual(20, report["advisory"]["visible_pixels"])

    def test_dimension_mismatch_is_a_failure_with_explicitly_unavailable_advisory(self):
        frame = Image.new("RGBA", (7, 8), (0, 0, 0, 0))
        frame.paste((20, 30, 40, 255), (2, 3, 5, 8))
        master = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        master.paste((20, 30, 40, 255), (2, 3, 5, 8))

        report = images.technical_frame_report(
            frame,
            master,
            None,
            self.generated_manifest(8, 8),
            1,
        )

        self.assertEqual("FAIL", report["technical_result"])
        self.assertEqual(["FRAME_DIMENSIONS_MISMATCH"], report["failures"])
        self.assertEqual(
            {
                "comparison_status": "UNAVAILABLE",
                "unavailable_reason": "DIMENSIONS_MISMATCH",
                "candidate_size": [7, 8],
                "reference_size": [8, 8],
                "bbox_movement": None,
                "normalized_pixel_difference_count": None,
                "silhouette_intersection": None,
                "silhouette_union": None,
                "dominant_color_histogram_distance": None,
            },
            report["advisory"]["master"],
        )

        matching_canvas = images.technical_frame_report(
            frame, master, None, self.generated_manifest(7, 8), 1
        )
        self.assertEqual("PASS", matching_canvas["technical_result"])
        self.assertEqual([], matching_canvas["failures"])
        self.assertEqual(report["advisory"]["master"], matching_canvas["advisory"]["master"])

    def test_assembly_preserves_exact_frame_pixels_and_order(self):
        frames = tuple(
            Image.new("RGBA", (12, 18), color)
            for color in (
                (220, 20, 30, 255),
                (20, 210, 40, 255),
                (30, 40, 200, 255),
            )
        )
        before = tuple(frame.tobytes() for frame in frames)

        sheet = images.assemble_sheet(frames)

        self.assertEqual("RGBA", sheet.mode)
        self.assertEqual((36, 18), sheet.size)
        for index, expected in enumerate(before):
            cell = sheet.crop((index * 12, 0, (index + 1) * 12, 18))
            self.assertEqual(expected, cell.tobytes())
        self.assertEqual(before, tuple(frame.tobytes() for frame in frames))

    def test_gif_and_contact_sheet_use_the_same_declared_order(self):
        frames = tuple(
            Image.new("RGBA", (12, 18), color)
            for color in (
                (220, 20, 30, 255),
                (20, 210, 40, 255),
                (30, 40, 200, 255),
            )
        )
        gif_path = self.path / "loop.gif"
        contact_path = self.path / "contact.png"

        images.save_loop_gif(frames, gif_path, 100)
        images.save_contact_sheet(frames, contact_path)

        with Image.open(gif_path) as opened:
            decoded = tuple(frame.convert("RGBA") for frame in ImageSequence.Iterator(opened))
            self.assertEqual(3, len(decoded))
            self.assertEqual(0, opened.info["loop"])
            self.assertEqual([100, 100, 100], [frame.info["duration"] for frame in decoded])
        self.assertEqual(
            [(220, 20, 30, 255), (20, 210, 40, 255), (30, 40, 200, 255)],
            [frame.getpixel((6, 9)) for frame in decoded],
        )
        with Image.open(contact_path) as contact:
            self.assertEqual("RGBA", contact.mode)
            self.assertEqual((36, 18), contact.size)
            self.assertEqual(
                [(220, 20, 30, 255), (20, 210, 40, 255), (30, 40, 200, 255)],
                [contact.getpixel((6 + index * 12, 9)) for index in range(3)],
            )

    def test_assembly_rejects_empty_mismatched_and_invalid_duration_without_output(self):
        destination = self.path / "loop.gif"
        with self.assertRaisesRegex(ValueError, "FRAMES_REQUIRED"):
            images.assemble_sheet(())
        with self.assertRaisesRegex(ValueError, "FRAME_DIMENSIONS_MISMATCH"):
            images.save_contact_sheet(
                (Image.new("RGBA", (12, 18)), Image.new("RGBA", (13, 18))),
                self.path / "contact.png",
            )
        with self.assertRaisesRegex(ValueError, "GIF_DURATION_INVALID"):
            images.save_loop_gif((Image.new("RGBA", (12, 18)),), destination, 0)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
