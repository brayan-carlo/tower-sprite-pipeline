"""Pixel-bound diagnostics and explicit review of disconnected native art.

Core connectivity is 8-neighbor at alpha >=16. A faint (alpha 1..15)
connected region belongs to a core only if it touches exactly one core. This
retains antialiasing when preserving and removes its exact halo when deleting.
Shared faint bridges never merge cores and prohibit deleting either neighbor.
Isolated faint regions are diagnosed too; transparent RGB is never modified.
No classification of anatomy/accessories or automatic deletion is performed.
"""
from array import array
import hashlib

from PIL import Image
from sheet_workflow_state import WorkflowError


def _validate_image(image):
    if not isinstance(image, Image.Image) or image.mode != 'RGBA':
        raise WorkflowError('COMPONENT_IMAGE_INVALID')
    width, height = image.size
    if not (1 <= width <= 4096 and 1 <= height <= 4096) or width * height > 8_000_000:
        raise WorkflowError('IMAGE_LIMIT_EXCEEDED')


def _neighbors(index, width, height):
    x, y = index % width, index // width
    for ny in range(max(0, y-1), min(height, y+2)):
        for nx in range(max(0, x-1), min(width, x+2)):
            if nx != x or ny != y:
                yield ny*width+nx


def _analyze(image):
    _validate_image(image)
    width, height = image.size
    alpha = image.getchannel('A').tobytes()
    # Fixed-width arrays avoid one Python integer object per image pixel.
    labels = array('I', [0]) * len(alpha)
    regions = []
    for seed, opacity in enumerate(alpha):
        if opacity < 16 or labels[seed]:
            continue
        label = len(regions)+1
        members = array('I', [seed])
        labels[seed] = label
        for index in members:
            for adjacent in _neighbors(index, width, height):
                if alpha[adjacent] >= 16 and labels[adjacent] == 0:
                    labels[adjacent] = label
                    members.append(adjacent)
        regions.append({'seed_index': seed, 'members': members,
                        'visible_pixels': len(members), 'shared_halo_pixels': 0})

    faint_seen = bytearray(len(alpha))
    shared_total = 0
    for seed, opacity in enumerate(alpha):
        if not 0 < opacity < 16 or faint_seen[seed]:
            continue
        members = array('I', [seed])
        faint_seen[seed] = 1
        touching = set()
        for index in members:
            for adjacent in _neighbors(index, width, height):
                if alpha[adjacent] >= 16:
                    touching.add(labels[adjacent])
                elif alpha[adjacent] and not faint_seen[adjacent]:
                    faint_seen[adjacent] = 1
                    members.append(adjacent)
        if len(touching) == 1:
            regions[next(iter(touching))-1]['members'].extend(members)
        elif not touching:
            regions.append({'seed_index': seed, 'members': members,
                            'visible_pixels': 0, 'shared_halo_pixels': 0})
        else:
            shared_total += len(members)
            for label in touching:
                regions[label-1]['shared_halo_pixels'] += len(members)

    regions.sort(key=lambda region: region['seed_index'])
    primary = max(range(len(regions)),
                  key=lambda i: (regions[i]['visible_pixels'], len(regions[i]['members'])),
                  default=None)
    components = []
    for index, region in enumerate(regions):
        members = region['members']
        left, top, right, bottom = width, height, 0, 0
        for member in members:
            x, y = member % width, member // width
            left, top = min(left, x), min(top, y)
            right, bottom = max(right, x+1), max(bottom, y+1)
        components.append({'id': f'component-{index+1:04d}',
                           'seed': [region['seed_index'] % width, region['seed_index'] // width],
                           'bbox': [left, top, right, bottom], 'pixels': len(members),
                           'visible_pixels': region['visible_pixels'],
                           'shared_halo_pixels': region['shared_halo_pixels'],
                           'primary': index == primary, 'requires_review': index != primary})
    digest = hashlib.sha256(width.to_bytes(4, 'big') + height.to_bytes(4, 'big') + image.tobytes()).hexdigest()
    report = {'schema': 'native_components_v1', 'image_pixel_sha256': digest,
              'size': [width, height], 'connectivity': 8, 'core_alpha_threshold': 16,
              'halo_policy': 'unique_core_only_shared_halo_blocks_removal',
              'shared_halo_pixels': shared_total, 'components': components,
              'review_required': len(components) > 1}
    return report, regions


def analyze_components(image):
    """Diagnose all nontransparent regions without altering or resizing pixels.

    Primary means greatest visible core area, with stable scan-order tie breaks;
    it is a conservative protected region, not anatomical recognition.
    """
    return _analyze(image)[0]


def apply_component_review(image, report, decisions):
    """Return an RGBA copy after validating every secondary decision.

    decisions is a list of {id, decision, notes}; notes must contain at least
    ten non-padding characters. Recompute reports/masks, so altered reports or
    image bytes cannot retarget an earlier decision to different artwork.
    """
    current, regions = _analyze(image)
    if report != current:
        raise WorkflowError('COMPONENT_REPORT_STALE')
    if not isinstance(decisions, list):
        raise WorkflowError('COMPONENT_DECISION_INVALID')
    components = {c['id']: c for c in current['components']}
    selected = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            raise WorkflowError('COMPONENT_DECISION_INVALID')
        identifier = decision.get('id')
        action, notes = decision.get('decision'), decision.get('notes')
        if (not isinstance(identifier, str) or identifier not in components or identifier in selected
                or action not in ('preserve_as_art', 'remove_as_background')
                or not isinstance(notes, str) or len(notes.strip()) < 10):
            raise WorkflowError('COMPONENT_DECISION_INVALID')
        component = components[identifier]
        if component['primary'] and action == 'remove_as_background':
            raise WorkflowError('COMPONENT_PRIMARY_PROTECTED')
        if component['shared_halo_pixels'] and action == 'remove_as_background':
            raise WorkflowError('COMPONENT_HALO_AMBIGUOUS')
        selected[identifier] = action
    if any(c['requires_review'] and c['id'] not in selected for c in current['components']):
        raise WorkflowError('COMPONENT_DECISIONS_REQUIRED')
    result = image.copy()
    pixels = result.load()
    for component, region in zip(current['components'], regions):
        if selected.get(component['id']) == 'remove_as_background':
            for member in region['members']:
                pixels[member % image.width, member // image.width] = (0, 0, 0, 0)
    return result
