"""Local processing for eight native quadruped poses in two four-phase strides.

The matte and translation formulas intentionally match the reviewed experiments.
This module does not generate images or establish visual acceptance. The caller
must validate the isolated source before requesting the magenta-key generation.
"""
from collections import Counter
import hashlib
import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from native_components import analyze_components, apply_component_review
from sheet_workflow_state import WorkflowError, encoded, sha_file


def _load_png(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise WorkflowError('FILE_INVALID')
    if path.stat().st_size > 64 * 1024 * 1024:
        raise WorkflowError('IMAGE_LIMIT_EXCEEDED')
    try:
        with Image.open(path) as image:
            if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1:
                raise WorkflowError('PNG_REQUIRED')
            width, height = image.size
            if not (16 <= width <= 4096 and 16 <= height <= 4096) or width * height > 8_000_000:
                raise WorkflowError('IMAGE_LIMIT_EXCEEDED')
            image.load()
            return image.convert('RGBA')
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise WorkflowError('PNG_INVALID') from None


def validate_source(path):
    """Return source dimensions; reject alpha absence and any visible key risk.

    At alpha >=16, even a single pixel with min(R,B)-G >8 collides with
    the difference matte. This deliberately conservative rule rejects colors
    that are only slightly magenta, instead of silently altering details.
    """
    image = _load_png(path)
    histogram = image.getchannel('A').histogram()
    if not sum(histogram[16:]):
        raise WorkflowError('SOURCE_EMPTY')
    if histogram[0] < image.width * image.height * .01:
        raise WorkflowError('SOURCE_ALPHA_REQUIRED')
    for red, green, blue, alpha in image.get_flattened_data():
        if alpha >= 16 and min(red, blue) - green > 8:
            raise WorkflowError('SOURCE_KEY_COLLISION')
    return image.size


def pixel_sha256(path):
    """Hash dimensions and decoded RGBA pixels, independent of PNG encoding."""
    image = _load_png(path)
    digest = hashlib.sha256()
    digest.update(image.width.to_bytes(4, 'big'))
    digest.update(image.height.to_bytes(4, 'big'))
    digest.update(image.tobytes())
    return digest.hexdigest()


def _difference_matte(image):
    pixels = list(image.get_flattened_data())
    width, height = image.size
    border = [pixels[x][:3] for x in range(width)]
    border += [pixels[(height-1)*width+x][:3] for x in range(width)]
    key, border_count = Counter(border).most_common(1)[0]
    key_signal = min(key[0], key[2]) - key[1]
    magenta_border_fraction = sum(min(red, blue)-green >= 150 for red, green, blue in border) / len(border)
    if key_signal < 150 or magenta_border_fraction < .80:
        raise WorkflowError('KEY_MISMATCH')
    result = []
    changed = transparent = 0
    for red, green, blue, alpha in pixels:
        signal = min(red, blue) - green
        if signal <= 8:
            result.append((red, green, blue, alpha))
            continue
        opacity = max(0, min(1, 1-signal/key_signal))
        if opacity < .10:
            result.append((0, 0, 0, 0))
            transparent += 1
            changed += 1
            continue
        color = tuple(round(max(0, min(255, (c-(1-opacity)*k)/opacity)))
                      for c, k in zip((red, green, blue), key))
        result.append((*color, round(alpha*opacity)))
        changed += 1
    if transparent / (width*height) < .20:
        raise WorkflowError('KEY_COVERAGE_LOW')
    alpha_image = Image.new('RGBA', (width, height))
    alpha_image.putdata(result)
    return alpha_image, {
        'key_rgb': list(key), 'key_border_fraction': border_count / len(border),
        'magenta_border_fraction': magenta_border_fraction,
        'transparent_fraction': transparent / (width*height), 'changed_pixels': changed,
        'method': 'magenta_difference_matte_and_unpremultiply',
        'core_with_signal_le_8_unchanged': True, 'size': list(image.size),
        'assumption': 'Validated source has no visible magenta detail; generation still requires visual review.'}


def _register_strides(image, strict_components=False, alignment_policy=None):
    if alignment_policy not in (None,'body_x_preserve_y_v1'):raise WorkflowError('ALIGNMENT_POLICY_UNSUPPORTED')
    # Strict crops account for every nontransparent post-matte pixel. A faint
    # detached region above/below an opaque pose therefore survives diagnosis.
    # If it cannot be assigned to one of four disjoint X spans, fail before any
    # output is written instead of silently assuming it is background.
    separation_error = 'UNREVIEWED_ALPHA_LOSS' if strict_components else 'POSE_SEPARATION_AMBIGUOUS'
    clipping_error = 'UNREVIEWED_ALPHA_LOSS' if strict_components else 'POSE_CLIPPED'
    rows = []
    row_floors = []
    ignored = []
    for row in range(2):
        strip = image.crop((0, row*image.height//2, image.width, (row+1)*image.height//2))
        threshold = 1 if strict_components else 16
        mask = strip.getchannel('A').point(lambda alpha: 255 if alpha >= threshold else 0)
        occupied = [mask.crop((x, 0, x+1, mask.height)).getbbox() is not None for x in range(mask.width)]
        spans = []
        start = None
        for x, visible in enumerate(occupied+[False]):
            if visible and start is None:
                start = x
            elif not visible and start is not None:
                if x-start >= 50:
                    spans.append((start, x))
                elif strict_components:
                    raise WorkflowError(separation_error)
                elif start == 0 or x == image.width:
                    count = sum(mask.crop((start, 0, x, mask.height)).histogram()[1:])
                    if count > 4:
                        raise WorkflowError('POSE_SEPARATION_AMBIGUOUS')
                    # Only faint residues at complete-image corners qualify.
                    # A stride boundary is not an outside corner. Coordinates
                    # and alpha bound the exception; visual review remains due.
                    for edge_x in range(start, x):
                        for edge_y in range(strip.height):
                            alpha = strip.getpixel((edge_x, edge_y))[3]
                            if alpha < 16:
                                continue
                            global_y = row*image.height//2+edge_y
                            at_corner = (edge_x <= 2 or edge_x >= image.width-3) and (global_y <= 2 or global_y >= image.height-3)
                            if alpha >= 64 or not at_corner:
                                raise WorkflowError('POSE_SEPARATION_AMBIGUOUS')
                    ignored.append({'row': row+1, 'bounds': [start, x], 'pixels': count})
                else:
                    raise WorkflowError('POSE_SEPARATION_AMBIGUOUS')
                start = None
        if len(spans) != 4:
            raise WorkflowError(separation_error)
        poses = []
        for left, right in spans:
            bbox = mask.crop((left, 0, right, mask.height)).getbbox()
            if left == 0 or right == image.width or bbox[1] == 0 or bbox[3] == strip.height:
                raise WorkflowError(clipping_error)
            # Full-alpha bounds already contain every halo. Extra padding in
            # strict mode could copy a neighboring span across a one-pixel gap.
            padding = 0 if strict_components else 2
            box = (max(0, left-padding), max(0, bbox[1]-padding),
                   min(image.width, right+padding), min(strip.height, bbox[3]+padding))
            poses.append((strip.crop(box), box, bbox[3]))
        rows.append(poses)
        row_floors.append(max(poses[0][2], poses[2][2]))
    origin_y=None
    if alignment_policy is not None:
        minimum=min(box[1] for poses in rows for _,box,_ in poses)
        maximum=max(box[3] for poses in rows for _,box,_ in poses)
        if maximum-minimum>496:raise WorkflowError(clipping_error)
        origin_y=max(8-minimum,min(440-max(row_floors),504-maximum))
    sheet = Image.new('RGBA', (2048, 1024))
    transforms = []
    for row, poses in enumerate(rows):
        for col, (pose, box, bottom) in enumerate(poses):
            x = (512-pose.width)//2
            y = (origin_y if origin_y is not None else 440-row_floors[row])+box[1]
            if not (8 <= x and x+pose.width <= 504 and 8 <= y and y+pose.height <= 504):
                raise WorkflowError(clipping_error)
            sheet.paste(pose, (col*512+x, row*512+y))
            transforms.append({'frame': row*4+col+1, 'source_crop': list(box), 'paste': [x, y],
                               'support_baseline_raw': row_floors[row],
                               'bottom_relative_to_support': bottom-row_floors[row]})
    if strict_components and image.getchannel('A').histogram()[1:] != sheet.getchannel('A').histogram()[1:]:
        raise WorkflowError('UNREVIEWED_ALPHA_LOSS')
    return sheet, {**({'alignment_policy':alignment_policy,'common_translation_y':origin_y} if alignment_policy else {}), 'method': 'translation_only_two_repeated_four_phase_strides',
                   'cell': [512,512], 'support_baseline': 440, 'poses': transforms,
                   'ignored_far_edge_background': ignored, 'pose_pixels_preserved': True, 'resizing': False,
                   'strict_components': strict_components, 'registration_alpha_threshold': threshold}


def _png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', compress_level=9)
    return buffer.getvalue()


def diagnose_matte(raw_path, output_dir):
    """Write a non-destructive post-matte alpha and global component report.

    This is diagnostic only: no review is inferred and no component is removed.
    Existing outputs, even identical ones, are never overwritten.
    """
    raw_path, output_dir = Path(raw_path), Path(output_dir)
    if output_dir.is_symlink() or any(parent.is_symlink() for parent in output_dir.parents):
        raise WorkflowError('OUTPUT_CONFLICT')
    paths = {'alpha': output_dir/'alpha.png', 'components_report': output_dir/'components-report.json'}
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise WorkflowError('OUTPUT_CONFLICT')
    alpha, matte_report = _difference_matte(_load_png(raw_path))
    payloads = {'alpha': _png_bytes(alpha), 'components_report': encoded(analyze_components(alpha))}
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        for key, path in paths.items():
            with path.open('xb') as destination:
                destination.write(payloads[key])
    except OSError:
        raise WorkflowError('OUTPUT_CONFLICT') from None
    return {'paths': {key: str(path.resolve()) for key, path in paths.items()},
            'details': {'schema': 'native_matte_diagnostic_v1', 'input_sha256': sha_file(raw_path),
                        'matte': matte_report, 'delivery_allowed': False}}


def process_sheet(raw_path, output_dir, strict_components=False, matte_review=None, alignment_policy=None):
    """Remove the key and translate poses; write new intermediates only.

    Returns paths for alpha, registered, processing_report and components_report. The report logs
    any permitted tiny outside-edge residue; ambiguous detached content fails.
    strict_components=True preserves all post-matte alpha>0 in disjoint crops
    or rejects UNREVIEWED_ALPHA_LOSS before writing outputs. False retains the
    legacy alpha>=16 crop and its narrow outside-corner residue exception.
    matte_review is optional in strict mode only: its image_pixel_sha256 binds
    explicit decisions for every global secondary to the complete post-matte
    image. The pre-review alpha and decision receipt are retained and hashed.
    Existing output files are never replaced, including an identical result.
    """
    raw_path = Path(raw_path)
    output_dir = Path(output_dir)
    if matte_review is not None and not strict_components:
        raise WorkflowError('MATTE_REVIEW_REQUIRES_STRICT')
    if output_dir.is_symlink() or any(parent.is_symlink() for parent in output_dir.parents):
        raise WorkflowError('OUTPUT_CONFLICT')
    paths = {key: output_dir/name for key, name in (
        ('alpha', 'alpha.png'), ('registered', 'registered.png'), ('processing_report', 'processing-report.json'),
        ('components_report', 'components-report.json'))}
    if matte_review is not None:
        paths.update({'alpha_before_review': output_dir/'alpha-before-review.png',
                      'matte_review': output_dir/'matte-review.json'})
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise WorkflowError('OUTPUT_CONFLICT')
    image = _load_png(raw_path)
    alpha, matte_report = _difference_matte(image)
    review_payloads = {}
    review_summary = None
    if matte_review is not None:
        if not isinstance(matte_review, dict):
            raise WorkflowError('COMPONENT_DECISION_INVALID')
        before_report = analyze_components(alpha)
        if matte_review.get('image_pixel_sha256') != before_report['image_pixel_sha256']:
            raise WorkflowError('COMPONENT_REPORT_STALE')
        reviewed = apply_component_review(alpha, before_report, matte_review.get('decisions'))
        after_report = analyze_components(reviewed)
        receipt = {'schema': 'native_matte_review_v1', 'input_sha256': sha_file(raw_path),
                   'image_pixel_sha256': before_report['image_pixel_sha256'],
                   'reviewed_image_pixel_sha256': after_report['image_pixel_sha256'],
                   'components_report': before_report, 'decisions': matte_review['decisions']}
        review_payloads = {'alpha_before_review': _png_bytes(alpha), 'matte_review': encoded(receipt)}
        alpha = reviewed
        review_summary = {'image_pixel_sha256': before_report['image_pixel_sha256'],
                          'reviewed_image_pixel_sha256': after_report['image_pixel_sha256']}
    registered, registration_report = _register_strides(alpha, strict_components=strict_components, alignment_policy=alignment_policy)
    components_report = {'schema': 'native_sheet_components_v1', 'frames': [
        {'frame': index+1, **analyze_components(registered.crop(
            ((index % 4)*512, (index // 4)*512, (index % 4+1)*512, (index // 4+1)*512)))}
        for index in range(8)]}
    report = {'schema': 'native_sheet_processing_v1', 'input_sha256': sha_file(raw_path),
              'matte': matte_report, 'registration': registration_report,
              'visual_review_required': True, 'delivery_allowed': False}
    payloads = {'alpha': _png_bytes(alpha), 'registered': _png_bytes(registered),
                'components_report': encoded(components_report), **review_payloads}
    if review_summary is not None:
        review_summary['artifact_sha256'] = {
            key: hashlib.sha256(payloads[key]).hexdigest()
            for key in ('alpha_before_review', 'matte_review', 'alpha')}
        report['matte_review'] = review_summary
    payloads['processing_report'] = encoded(report)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        for key, path in paths.items():
            with path.open('xb') as destination:
                destination.write(payloads[key])
    except OSError:
        raise WorkflowError('OUTPUT_CONFLICT') from None
    return {'paths': {key: str(path.resolve()) for key, path in paths.items()},
            'details': report}
