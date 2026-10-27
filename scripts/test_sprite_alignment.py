"""Behavioral tests for whole-frame body X alignment; no remote calls or fixtures."""

import json
import math
import unittest
from fractions import Fraction
from unittest.mock import patch

from PIL import Image

try:
    import sprite_alignment
except ModuleNotFoundError as error:
    if error.name != "sprite_alignment":
        raise
    sprite_alignment = None


class BodyAlignmentTests(unittest.TestCase):
    def align(self, *args, **kwargs):
        self.assertIsNotNone(sprite_alignment, "align_body_x implementation is missing")
        return sprite_alignment.align_body_x(*args, **kwargs)

    def assert_error(self, code, frames, anchors, **kwargs):
        self.assertIsNotNone(sprite_alignment, "align_body_x implementation is missing")
        with self.assertRaises(sprite_alignment.AlignmentError) as caught:
            self.align(frames, anchors, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertIsInstance(caught.exception, ValueError)

    def assert_visible_translation(self, source, output, dx):
        expected = {
            (x + dx, y): source.getpixel((x, y))
            for y in range(source.height)
            for x in range(source.width)
            if source.getpixel((x, y))[3]
        }
        actual = {
            (x, y): output.getpixel((x, y))
            for y in range(output.height)
            for x in range(output.width)
            if output.getpixel((x, y))[3]
        }
        self.assertEqual(actual, expected)

    def test_centers_drifting_body_without_erasing_y_bounce_or_accessories(self):
        # Bounding-box centering would move these bodies as accessories vary.
        frames = []
        anchors = [(2, 2), (5, 4), (8, 1)]
        for index, (body_x, body_y) in enumerate(anchors):
            frame = Image.new("RGBA", (11, 7), (19, 29, 39, 0))
            frame.putpixel((body_x, body_y), (180, 90, index, 255))
            frame.putpixel((body_x, body_y + 1), (2, 3, 4, 81))
            edge = [10, 0, 10][index]
            frame.putpixel((edge, 6), (201, 40, 71, 1))
            frames.append(frame)
        snapshots = [frame.tobytes() for frame in frames]
        output, report = self.align(frames, anchors)
        self.assertEqual([frame.size for frame in output], [(17, 7)] * 3)
        self.assertEqual(report["policy"], "body_x_preserve_y_v1")
        self.assertEqual(report["input_canvas"], {"width": 11, "height": 7})
        self.assertEqual(report["output_canvas"], {"width": 17, "height": 7})
        self.assertEqual(report["target_x"], 8)
        self.assertEqual(report["anchors_before"], [[2, 2], [5, 4], [8, 1]])
        self.assertEqual(report["anchors_after"], [[8, 2], [8, 4], [8, 1]])
        self.assertEqual([item["dx"] for item in report["transforms"]], [6, 3, 0])
        for index, item in enumerate(report["transforms"]):
            self.assertEqual(item["frame_index"], index)
            self.assertEqual((item["dy"], item["scale"]), (0, 1))
            self.assert_visible_translation(frames[index], output[index], item["dx"])
            self.assertIsNot(output[index], frames[index])
        self.assertEqual([frame.tobytes() for frame in frames], snapshots)
        self.assertEqual(anchors, [(2, 2), (5, 4), (8, 1)])
        json.dumps(report, allow_nan=False)

    def test_reference_in_transparent_gap_is_valid(self):
        frame = Image.new("RGBA", (5, 3))
        frame.putpixel((0, 0), (1, 2, 3, 50))
        frame.putpixel((4, 2), (9, 8, 7, 255))
        output, report = self.align([frame], [(2, 1)])
        self.assertEqual(output[0].size, (5, 3))
        self.assertEqual(report["anchors_after"], [[2, 1]])
        self.assert_visible_translation(frame, output[0], 0)

    def test_canvas_width_is_minimum_and_expands_to_preserve_edges(self):
        frame = Image.new("RGBA", (5, 2))
        frame.putpixel((4, 0), (4, 5, 6, 128))
        output, report = self.align([frame], [(0, 1)], canvas_width=6)
        self.assertEqual(output[0].size, (9, 2))
        self.assertEqual(report["transforms"][0]["dx"], 4)
        self.assert_visible_translation(frame, output[0], 4)
        output, _ = self.align([frame], [(0, 1)], canvas_width=1)
        self.assertEqual(output[0].size, (9, 2))

    def test_negative_translation_and_fractional_anchor_preserve_pixels(self):
        frame = Image.new("RGBA", (8, 3))
        frame.putpixel((7, 2), (20, 40, 60, 129))
        output, report = self.align([frame], [(6.25, 1.5)])
        self.assertEqual(output[0].size, (8, 3))
        self.assertEqual(report["transforms"][0]["dx"], -3)
        self.assertEqual(report["anchors_after"], [[3.25, 1.5]])
        self.assert_visible_translation(frame, output[0], -3)

    def test_half_pixel_ties_round_toward_positive_x_deterministically(self):
        frame = Image.new("RGBA", (6, 2))
        frame.putpixel((2, 0), (1, 2, 3, 255))
        first, report = self.align([frame], [(2, 0)], canvas_width=10)
        second, repeated = self.align([frame], [(2, 0)], canvas_width=10)
        self.assertEqual(report["transforms"][0]["dx"], 3)
        self.assertEqual(report["anchors_after"], [[5, 0]])
        self.assertEqual(report, repeated)
        self.assertEqual(first[0].tobytes(), second[0].tobytes())

    def test_fully_transparent_frame_is_allowed_without_mutation(self):
        frame = Image.new("RGBA", (4, 2), (12, 34, 56, 0))
        before = frame.tobytes()
        output, report = self.align([frame], [(0.25, 0.75)])
        self.assertIsNone(output[0].getchannel("A").getbbox())
        self.assertLessEqual(abs(report["anchors_after"][0][0] - 1.5), 0.5)
        self.assertEqual(frame.tobytes(), before)

    def test_rejects_empty_and_malformed_frame_collections(self):
        for frames in (None, 7, "abc", {}, []):
            with self.subTest(frames=frames):
                self.assert_error("INVALID_FRAMES", frames, [])

    def test_rejects_non_images_non_rgba_zero_and_mixed_sizes(self):
        good = Image.new("RGBA", (3, 2))
        for frame in (None, object(), Image.new("RGB", (3, 2)), Image.new("RGBA", (0, 2))):
            with self.subTest(frame=type(frame).__name__):
                self.assert_error("INVALID_FRAME", [frame], [(0, 0)])
        self.assert_error("MIXED_CANVAS", [good, Image.new("RGBA", (4, 2))], [(0, 0)] * 2)

    def test_rejects_malformed_anchor_collections_and_counts(self):
        frame = Image.new("RGBA", (3, 2))
        for anchors in (None, 7, "ab", {}, [], [(0, 0), (0, 0)]):
            with self.subTest(anchors=anchors):
                self.assert_error("INVALID_ANCHORS", [frame], anchors)

    def test_rejects_malformed_nonfinite_bool_and_outside_anchor_points(self):
        frame = Image.new("RGBA", (3, 2))
        for point in (None, 3, "00", {}, (), (0,), (0, 0, 0),
                      (True, 0), (0, False), ("1", 0), (None, 0),
                      (math.nan, 0), (0, math.inf), (-math.inf, 0),
                      (-0.001, 0), (2.001, 0), (0, -0.1), (0, 1.1), (10**1000, 0)):
            with self.subTest(point=point):
                self.assert_error("INVALID_ANCHOR", [frame], [point])

    def test_rejects_invalid_minimum_canvas_widths(self):
        frame = Image.new("RGBA", (3, 2))
        for width in (False, True, 0, -1, 1.5, "3", math.inf, 4097):
            with self.subTest(width=width):
                self.assert_error("INVALID_CANVAS_WIDTH", [frame], [(0, 0)], canvas_width=width)

    def test_rejects_exact_outside_coordinates_before_float_rounding(self):
        frame = Image.new("RGBA", (3, 2))
        points = (
            (Fraction(20000000000000001, 10000000000000000), 0),
            (Fraction(-1, 10**400), 0),
            (0, Fraction(10000000000000001, 10000000000000000)),
            (0, Fraction(-1, 10**400)),
        )
        for point in points:
            with self.subTest(point=point):
                self.assert_error("INVALID_ANCHOR", [frame], [point])

    def test_dimension_limit_rejected_before_output_allocation(self):
        for size in ((4097, 1), (1, 4097)):
            frame = Image.new("RGBA", size)
            with patch("PIL.Image.new", side_effect=AssertionError("allocated output")):
                self.assert_error("DIMENSION_LIMIT", [frame], [(0, 0)])

    def test_required_width_limit_rejected_before_output_allocation(self):
        frame = Image.new("RGBA", (4096, 1))
        frame.putpixel((4095, 0), (1, 2, 3, 255))
        with patch("PIL.Image.new", side_effect=AssertionError("allocated output")):
            self.assert_error("DIMENSION_LIMIT", [frame], [(0, 0)])

    def test_total_pixel_limit_rejected_before_output_allocation(self):
        frame = Image.new("RGBA", (1, 4096))
        frames = [frame] * 15626
        with patch("PIL.Image.new", side_effect=AssertionError("allocated output")):
            self.assert_error("PIXEL_LIMIT", frames, [(0, 0)] * len(frames))

    def test_expansion_pixel_limit_rejected_before_output_allocation(self):
        frame = Image.new("RGBA", (2000, 1000))
        frame.putpixel((1999, 0), (1, 2, 3, 255))
        with patch("PIL.Image.new", side_effect=AssertionError("allocated output")):
            self.assert_error("PIXEL_LIMIT", [frame] * 32, [(0, 0)] * 32)

    def test_maximum_dimension_and_single_pixel_canvas_are_supported(self):
        for width, anchor in ((1, (0, 0)), (4096, (2047.5, 0))):
            frame = Image.new("RGBA", (width, 1), (1, 2, 3, 255))
            output, _ = self.align([frame], [anchor])
            self.assertEqual(output[0].size, frame.size)
            self.assertEqual(output[0].tobytes(), frame.tobytes())


if __name__ == "__main__":
    unittest.main()
