from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from sheet_images import SheetImageError, assemble_sheet


def _bounds(index: int, count: int, dimension: int) -> tuple[int, int]:
    return (index * dimension // count, (index + 1) * dimension // count)


def make_grid(
    folder: Path,
    *,
    count: int,
    columns: int,
    rows: int,
    size: tuple[int, int] | None = None,
) -> Path:
    width, height = size or (columns * 32, rows * 32)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(count):
        column = index % columns
        row = index // columns
        left, right = _bounds(column, columns, width)
        top, bottom = _bounds(row, rows, height)
        cell_width = right - left
        cell_height = bottom - top
        body_left = left + max(4, cell_width // 3)
        body_top = top + max(4, cell_height // 4)
        body_right = min(right - 5, body_left + max(4, cell_width // 4))
        body_bottom = min(bottom - 5, body_top + max(7, cell_height // 2))
        color = (40 + index * 17, 80 + index * 11, 120 + index * 7, 255)
        draw.rectangle((body_left, body_top, body_right, body_bottom), fill=color)
        limb_y = min(bottom - 4, body_top + 2 + (index % 5))
        if index % 2:
            draw.line((body_left - 3, limb_y, body_left, limb_y), fill=color, width=2)
        else:
            draw.line((body_right, limb_y, body_right + 2 + index % 3, limb_y), fill=color, width=2)
    path = folder / "raw.png"
    image.save(path, format="PNG")
    return path


def visible_bbox(path: str | Path) -> tuple[int, int, int, int]:
    with Image.open(path) as image:
        alpha = image.convert("RGBA").getchannel("A")
        bbox = alpha.point(lambda value: 255 if value >= 16 else 0).getbbox()
    if bbox is None:
        raise AssertionError(f"no visible pixels in {path}")
    return bbox


def file_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def color_bbox(image: Image.Image, color: tuple[int, int, int]) -> tuple[int, int, int, int]:
    rgba = image.convert("RGBA")
    mask = Image.new("L", rgba.size, 0)
    mask.putdata([255 if pixel[:3] == color and pixel[3] else 0 for pixel in rgba.get_flattened_data()])
    bbox = mask.getbbox()
    if bbox is None:
        raise AssertionError(f"color {color} is absent")
    return bbox


def checkerboard(size: tuple[int, int], tile: int = 8) -> Image.Image:
    image = Image.new("RGBA", size, (200, 200, 200, 255))
    draw = ImageDraw.Draw(image)
    for top in range(0, size[1], tile):
        for left in range(0, size[0], tile):
            if (left // tile + top // tile) % 2:
                draw.rectangle(
                    (left, top, min(size[0] - 1, left + tile - 1), min(size[1] - 1, top + tile - 1)),
                    fill=(235, 235, 235, 255),
                )
    return image


class SheetImagesTests(unittest.TestCase):
    def assert_code(self, expected: str, callable_, *args, **kwargs) -> SheetImageError:
        with self.assertRaises(SheetImageError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, expected)
        return raised.exception

    def test_assembles_requested_artifacts_and_reopens_exact_gif_timing(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=8, columns=4, rows=2)

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=8,
                columns=4,
                rows=2,
                canvas=(1254, 1254),
            )

            self.assertEqual(result["state"], "READY_FOR_VISUAL_REVIEW")
            self.assertEqual(len(result["artifact_paths"]["frames"]), 8)
            for artifact in (
                result["artifact_paths"]["sheet"],
                result["artifact_paths"]["preview_gif"],
                result["artifact_paths"]["contact"],
                result["artifact_paths"]["qa_report"],
                *result["artifact_paths"]["frames"],
            ):
                self.assertTrue(Path(artifact).is_absolute())
                self.assertTrue(Path(artifact).is_file())
            with Image.open(result["artifact_paths"]["sheet"]) as sheet:
                self.assertEqual((sheet.mode, sheet.size), ("RGBA", (10032, 1254)))
            with Image.open(result["artifact_paths"]["preview_gif"]) as gif:
                self.assertEqual(gif.n_frames, 8)
                self.assertEqual(gif.info["loop"], 0)
                for index in range(8):
                    gif.seek(index)
                    self.assertEqual(gif.info["duration"], 100)
            qa = json.loads(Path(result["artifact_paths"]["qa_report"]).read_text("utf-8"))
            self.assertEqual(qa["state"], "READY_FOR_VISUAL_REVIEW")
            self.assertEqual(qa["frame_count"], 8)
            self.assertEqual(qa["frame_ms"], 100)
            self.assertTrue(qa["semantic_review_required"])
            self.assertNotIn("gait_pass", qa)
            self.assertEqual(len(qa["lower_half_mask_iou"]), 28)
            self.assertEqual(result["source_sha256"], file_digest(raw))
            for name, digest in qa["sha256"].items():
                if name == "frames":
                    self.assertEqual(digest, [file_digest(path) for path in result["artifact_paths"]["frames"]])
                else:
                    self.assertEqual(digest, file_digest(result["artifact_paths"][name]))

    def test_rejects_empty_requested_cell(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=7, columns=4, rows=2)
            self.assert_code(
                "CELL_EMPTY",
                assemble_sheet,
                raw,
                folder / "out",
                frame_count=8,
                columns=4,
                rows=2,
                canvas=(64, 64),
            )

    def test_rejects_content_in_unused_grid_slot(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=9, columns=5, rows=2)
            self.assert_code(
                "GRID_INVALID",
                assemble_sheet,
                raw,
                folder / "out",
                frame_count=8,
                columns=5,
                rows=2,
                canvas=(64, 64),
            )

    def test_rejects_alpha_1_through_15_in_unused_grid_slot(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=2, columns=3, rows=1)
            with Image.open(raw) as opened:
                base = opened.copy()
            unused_x = 2 * base.width // 3 + 8
            for alpha in range(1, 16):
                with self.subTest(alpha=alpha):
                    image = base.copy()
                    image.putpixel((unused_x, 8), (30, 60, 90, alpha))
                    image.save(raw, format="PNG")
                    self.assert_code(
                        "GRID_INVALID",
                        assemble_sheet,
                        raw,
                        folder / f"out-alpha-{alpha}",
                        frame_count=2,
                        columns=3,
                        rows=1,
                        canvas=(64, 64),
                    )

    def test_rejects_visible_content_touching_a_cell_seam(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=2, columns=2, rows=1)
            with Image.open(raw) as opened:
                image = opened.copy()
            draw = ImageDraw.Draw(image)
            seam = image.width // 2
            draw.rectangle((seam - 2, 8, seam + 2, 12), fill=(255, 20, 20, 255))
            image.save(raw, format="PNG")

            self.assert_code(
                "CELL_CLIPPED",
                assemble_sheet,
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(64, 64),
            )

    def test_rejects_opaque_checkerboard(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGB", (64, 32), (210, 210, 210))
            draw = ImageDraw.Draw(image)
            for y in range(0, 32, 8):
                for x in range(0, 64, 8):
                    if (x // 8 + y // 8) % 2:
                        draw.rectangle((x, y, x + 7, y + 7), fill=(170, 170, 170))
            raw = folder / "opaque.png"
            image.save(raw, format="PNG")

            self.assert_code(
                "ALPHA_REQUIRED",
                assemble_sheet,
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(64, 64),
            )

    def test_rejects_duplicate_poses_that_only_differ_in_hidden_rgb(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
            pixels = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    pixels[x, y] = ((31 if x < 32 else 231), (x * 3) % 256, (y * 7) % 256, 0)
            draw = ImageDraw.Draw(image)
            draw.rectangle((10, 7, 20, 25), fill=(20, 90, 160, 255))
            draw.rectangle((42, 7, 52, 25), fill=(20, 90, 160, 255))
            raw = folder / "duplicates.png"
            image.save(raw, format="PNG")

            self.assert_code(
                "DUPLICATE_FRAME",
                assemble_sheet,
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(64, 64),
            )

    def test_partitions_nondivisible_source_dimensions_without_losing_cells(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=8, columns=4, rows=2, size=(131, 67))

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=8,
                columns=4,
                rows=2,
                canvas=(64, 48),
            )

            self.assertEqual(len(result["artifact_paths"]["frames"]), 8)
            self.assertEqual(result["qa"]["source_dimensions"], [131, 67])
            self.assertEqual(result["qa"]["cell_bounds"][3], [98, 0, 131, 33])
            self.assertEqual(result["qa"]["cell_bounds"][7], [98, 33, 131, 67])

    def test_uses_one_scale_and_grounds_visible_bottoms(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (120, 70), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((15, 12, 44, 51), fill=(220, 40, 50, 255))
            draw.rectangle((80, 20, 94, 39), fill=(40, 100, 220, 255))
            raw = folder / "scale.png"
            image.save(raw, format="PNG")

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(100, 100),
            )

            first = visible_bbox(result["artifact_paths"]["frames"][0])
            second = visible_bbox(result["artifact_paths"]["frames"][1])
            self.assertEqual(first[3], 90)
            self.assertEqual(second[3], 90)
            self.assertEqual(first[3] - first[1], 80)
            self.assertEqual(second[3] - second[1], 40)
            self.assertAlmostEqual(result["qa"]["normalization"]["scale"], 2.0)

    def test_contact_preserves_aspect_ratio_and_centers_non_square_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((15, 18, 24, 32), fill=(220, 40, 50, 255))
            draw.rectangle((65, 10, 74, 39), fill=(40, 100, 220, 255))
            raw = folder / "contact-aspect.png"
            image.save(raw, format="PNG")

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(320, 160),
            )

            with Image.open(result["artifact_paths"]["frames"][0]) as opened:
                frame = opened.convert("RGBA")
            with Image.open(result["artifact_paths"]["contact"]) as opened:
                contact_cell = opened.convert("RGBA").crop((0, 0, 256, 160))
            frame_bbox = color_bbox(frame, (220, 40, 50))
            contact_bbox = color_bbox(contact_cell, (220, 40, 50))
            scale_x = (contact_bbox[2] - contact_bbox[0]) / (frame_bbox[2] - frame_bbox[0])
            scale_y = (contact_bbox[3] - contact_bbox[1]) / (frame_bbox[3] - frame_bbox[1])
            self.assertAlmostEqual(scale_x, scale_y, delta=0.03)
            frame_center_y = (frame_bbox[1] + frame_bbox[3]) / 2
            contact_center_y = (contact_bbox[1] + contact_bbox[3]) / 2
            self.assertAlmostEqual(contact_center_y, 16 + frame_center_y * 0.8, delta=1.0)

    def test_contact_uses_resampling_selected_by_pixel_art(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=2, columns=2, rows=1)
            for pixel_art, resampling in (
                (True, Image.Resampling.NEAREST),
                (False, Image.Resampling.LANCZOS),
            ):
                with self.subTest(pixel_art=pixel_art):
                    result = assemble_sheet(
                        raw,
                        folder / f"out-{pixel_art}",
                        frame_count=2,
                        columns=2,
                        rows=1,
                        canvas=(320, 320),
                        pixel_art=pixel_art,
                    )
                    with Image.open(result["artifact_paths"]["frames"][0]) as opened:
                        thumbnail = opened.convert("RGBA").resize((256, 256), resample=resampling)
                    expected = checkerboard((256, 256))
                    expected.alpha_composite(thumbnail)
                    with Image.open(result["artifact_paths"]["contact"]) as opened:
                        actual = opened.convert("RGBA").crop((0, 0, 256, 256))
                    self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_non_grounded_preserves_cell_displacement_and_nonloop_omits_loop_extension(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((16, 10, 25, 29), fill=(220, 40, 50, 255))
            draw.rectangle((78, 10, 87, 29), fill=(40, 100, 220, 255))
            raw = folder / "displacement.png"
            image.save(raw, format="PNG")

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(100, 100),
                frame_ms=70,
                loop=False,
                grounded=False,
            )

            first = visible_bbox(result["artifact_paths"]["frames"][0])
            second = visible_bbox(result["artifact_paths"]["frames"][1])
            self.assertAlmostEqual(result["qa"]["normalization"]["scale"], 10 / 3)
            self.assertEqual(second[0] - first[0], 40)
            self.assertEqual(first[1], second[1])
            with Image.open(result["artifact_paths"]["preview_gif"]) as gif:
                self.assertEqual(gif.n_frames, 2)
                self.assertNotIn("loop", gif.info)
                for index in range(2):
                    gif.seek(index)
                    self.assertEqual(gif.info["duration"], 70)

    def test_non_grounded_zero_crop_origin_stays_within_raster_rounding(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (100, 50), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((1, 10, 10, 29), fill=(220, 40, 50, 255))
            draw.rectangle((63, 10, 72, 29), fill=(40, 100, 220, 255))
            raw = folder / "zero-origin.png"
            image.save(raw, format="PNG")

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(100, 100),
                grounded=False,
            )

            first = visible_bbox(result["artifact_paths"]["frames"][0])
            second = visible_bbox(result["artifact_paths"]["frames"][1])
            scale = result["qa"]["normalization"]["scale"]
            self.assertAlmostEqual(scale, 10 / 3)
            self.assertLessEqual(abs((second[0] - first[0]) - round(12 * scale)), 1)

    def test_non_grounded_fits_shifted_horizontal_union_without_losing_artwork(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((20, 30, 29, 39), fill=(255, 0, 0, 255))
            draw.rectangle((128, 30, 137, 39), fill=(0, 255, 0, 255))
            raw = folder / "reviewer-horizontal.png"
            image.save(raw, format="PNG")

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=2,
                columns=2,
                rows=1,
                canvas=(100, 100),
                grounded=False,
                pixel_art=True,
            )

            measurements = []
            for path in result["artifact_paths"]["frames"]:
                with Image.open(path) as frame:
                    alpha = frame.getchannel("A")
                    measurements.append((alpha.getbbox(), sum(alpha.histogram()[16:])))
            self.assertAlmostEqual(result["qa"]["normalization"]["scale"], 45 / 11)
            self.assertEqual(measurements, [((13, 29, 54, 70), 1681), ((45, 29, 86, 70), 1681)])
            self.assertEqual(measurements[1][0][0] - measurements[0][0][0], 32)

    def test_non_grounded_fits_shifted_vertical_union_on_non_square_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            image = Image.new("RGBA", (100, 200), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 35, 39, 44), fill=(255, 0, 0, 255))
            draw.rectangle((30, 155, 39, 164), fill=(0, 255, 0, 255))
            raw = folder / "reviewer-vertical.png"
            image.save(raw, format="PNG")

            result = assemble_sheet(
                raw,
                folder / "out",
                frame_count=2,
                columns=1,
                rows=2,
                canvas=(80, 120),
                grounded=False,
                pixel_art=True,
            )

            measurements = []
            for path in result["artifact_paths"]["frames"]:
                with Image.open(path) as frame:
                    alpha = frame.getchannel("A")
                    measurements.append((alpha.getbbox(), sum(alpha.histogram()[16:])))
            self.assertAlmostEqual(result["qa"]["normalization"]["scale"], 48 / 17)
            self.assertEqual(measurements, [((26, 17, 54, 45), 784), ((26, 74, 54, 102), 784)])
            self.assertEqual(measurements[1][0][1] - measurements[0][0][1], 57)

    def test_reentry_is_byte_identical_and_conflict_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=4, columns=2, rows=2)
            output = folder / "out"
            kwargs = {
                "frame_count": 4,
                "columns": 2,
                "rows": 2,
                "canvas": (64, 64),
            }
            first = assemble_sheet(raw, output, **kwargs)
            paths = [
                *first["artifact_paths"]["frames"],
                first["artifact_paths"]["sheet"],
                first["artifact_paths"]["preview_gif"],
                first["artifact_paths"]["contact"],
                first["artifact_paths"]["qa_report"],
            ]
            before = {path: file_digest(path) for path in paths}

            assemble_sheet(raw, output, **kwargs)
            self.assertEqual(before, {path: file_digest(path) for path in paths})

            conflict = Path(first["artifact_paths"]["frames"][0])
            conflict.write_bytes(b"preserve-this-conflict")
            self.assert_code("OUTPUT_CONFLICT", assemble_sheet, raw, output, **kwargs)
            self.assertEqual(conflict.read_bytes(), b"preserve-this-conflict")

    def test_rejects_boolean_integer_and_out_of_range_contract_values(self) -> None:
        with tempfile.TemporaryDirectory() as folder_name:
            folder = Path(folder_name)
            raw = make_grid(folder, count=2, columns=2, rows=1)
            cases = (
                {"frame_count": True, "columns": 2, "rows": 1, "canvas": (64, 64)},
                {"frame_count": 2, "columns": 2, "rows": 1, "canvas": (15, 64)},
                {"frame_count": 2, "columns": 2, "rows": 1, "canvas": (64, 64), "frame_ms": 25},
            )
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    self.assert_code("INPUT_INVALID", assemble_sheet, raw, folder / "out", **kwargs)


if __name__ == "__main__":
    unittest.main()
