"""Same-frame evidence gate for a narrowly wider AX ancestor. No OCR guessing."""
from difflib import SequenceMatcher

from phase1_read_surface_v6 import normalized_node_rectangle
from phase1_read_surface_v2 import normalized_top_to_vision_region

PANE_VERSION = 'ax-pane-edge-validation-v4'
COMPLETE_PANE_VERSION = 'ax-pane-edge-selection-v5'
FUSED_PANE_VERSION = 'ax-pane-edge-selection-v6'
MIN_WITNESSES = 2


def complete_observations_usable(full, comparison):
    """Both required projections must actually contain recognized text."""
    return all(not observation.get('error') and any(
        line.get('text', '').strip() for line in observation.get('lines', [])
    ) for observation in (full, comparison))


def normalized(text):
    return ' '.join(text.casefold().split())


def edge_lines(pane):
    return [(i, l) for i, l in enumerate(pane['lines'])
            if len(l['text'].strip()) >= 8 and l['boundingBox']['x'] <= .002]


def prefix_match(old, new):
    a, b = normalized(old), normalized(new)
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match()
    return match if match.a <= 5 and match.b > match.a and match.size >= max(8, .65 * len(a)) else None


def screening_witnesses(pane, raw):
    # Cheap prefilter only. Final acceptance additionally requires box geometry
    # from OCR of the very same screenshot, never a prior/future document.
    full_lines = (raw.get('content') or '').splitlines()
    return [i for i, l in edge_lines(pane) if any(prefix_match(l['text'], t) for t in full_lines)]


def contains(outer, inner, epsilon=1e-6):
    return (outer['x'] <= inner['x']+epsilon and outer['y'] <= inner['y']+epsilon and
            outer['x']+outer['width'] >= inner['x']+inner['width']-epsilon and
            outer['y']+outer['height'] >= inner['y']+inner['height']-epsilon)


def ancestors(pane, raw, clipped_edge_only=False):
    selection = pane.get('surfaceSelection', {})
    depth = selection.get('selectedDepth')
    if not isinstance(depth, int) or selection.get('isV1Fallback'):
        return []
    nodes = (raw.get('accessibilitySurface') or {}).get('ancestors', [])
    base = pane['regionOfInterest']
    # A remembered/canonical pane may not be the currently probed subtree.
    # Only ascend a node whose actual current frame matches this rectangle.
    def region(node):
        rect = normalized_node_rectangle(raw, node)
        if rect is None:
            return None
        try:
            r = normalized_top_to_vision_region(rect)
        except ValueError:
            return None
        return r if r['x'] >= 0 and r['y'] >= 0 and r['x']+r['width'] <= 1.000001 and r['y']+r['height'] <= 1.000001 else None
    selected = next((n for n in nodes if n.get('depth') == depth), None)
    matched = region(selected) if selected else None
    if matched is None or any(abs(matched[k]-base[k]) > .002 for k in base):
        return []
    result = []
    seen = set()
    for node in sorted(nodes, key=lambda n: n.get('depth', -1)):
        if node.get('depth', -1) <= depth:
            continue
        if node.get('role') not in {'AXGroup', 'AXScrollArea'}:
            break  # Do not cross into another named document/window root.
        r = region(node)
        if r is None:
            continue
        if not contains(r, base) or r['height'] > base['height'] * 1.06:
            break
        if r['width'] > base['width'] * 1.5 or (r['width'] > .98 and r['height'] > .98):
            break
        if max(base['x']-r['x'], r['x']+r['width']-base['x']-base['width']) > base['width'] * .18:
            break
        key = tuple(round(r[k], 7) for k in ('x', 'y', 'width', 'height'))
        if key in seen or r['width'] <= base['width']+1e-5:
            continue
        seen.add(key)
        # The ancestor may include a header/footer; use its horizontal extent
        # only. The original selected vertical interval remains unchanged.
        # A cut LEFT edge is not permission to expand the right edge too. A
        # parent AX frame can extend over another composited app at the right.
        roi = dict(r, y=base['y'], height=base['height'])
        if clipped_edge_only:
            roi['width'] = base['x']+base['width']-r['x']
            if roi['x'] >= base['x']-1e-5:
                continue
        result.append({'node': node, 'regionOfInterest': roi,
                       'ancestorRegionOfInterest': r})
        if len(result) == 3:
            break
    return result


def global_box(line, region):
    b = line['boundingBox']
    return {'x': region['x']+b['x']*region['width'], 'y': region['y']+b['y']*region['height'],
            'width': b['width']*region['width'], 'height': b['height']*region['height']}


def prefix_extension(old_text, wide_text, minimum_anchor=8, maximum_leading=1):
    """An image-grounded prefix only; never substitute later OCR wording.

    The shared anchor must start at the first or second old character. A later
    similarity match cannot authorize replacement of a whole line. Preserve
    original suffix bytes, including numbers, names and punctuation.
    """
    # A shared intact start is evidence against a missing prefix. Searching
    # later repeated phrases would manufacture an extension of already intact
    # text (e.g. "Supported Providers ... Supported Providers ...").
    if len(old_text) >= minimum_anchor and wide_text.startswith(old_text[:minimum_anchor]):
        return None
    candidates = []
    for a in range(min(maximum_leading+1, len(old_text))):
        if a and not old_text[:a].isalpha():
            continue  # Do not rewrite an existing leading number or sign.
        for b in range(a, min(len(wide_text), a+40)):
            if old_text[:a] == wide_text[:b]:
                continue
            length = 0
            while a+length < len(old_text) and b+length < len(wide_text) and old_text[a+length] == wide_text[b+length]:
                length += 1
            if length >= minimum_anchor:
                candidates.append((length, -a, -b, a, b))
    if not candidates:
        return None
    _, _, _, a, b = max(candidates)
    return {'content': wide_text[:b]+old_text[a:], 'oldAnchorStart': a, 'wideAnchorStart': b,
            'preservedSuffix': old_text[a:], 'recoveredPrefix': wide_text[:b]}


def extend_edges(old, wider):
    """Fuse boundary observations, retaining every original non-edge line.

    The wider ROI is OCR context, not permission to import neighboring panes.
    Returned content contains the original lines with only proven prefixes
    extended. Full wider OCR is separately retained by the caller.
    """
    from copy import deepcopy
    output = deepcopy(old['lines'])
    witnesses = []
    used = set()
    for i, line in edge_lines(old):
        a = global_box(line, old['regionOfInterest'])
        possible = []
        for j, new in enumerate(wider['lines']):
            if j in used:
                continue
            b = global_box(new, wider['regionOfInterest'])
            overlap = min(a['y']+a['height'], b['y']+b['height'])-max(a['y'], b['y'])
            if overlap < .5*min(a['height'], b['height']):
                continue
            if not (b['x'] < old['regionOfInterest']['x']-.0005 < b['x']+b['width']):
                continue
            patch = prefix_extension(line['text'], new['text'])
            if patch is None:
                continue
            possible.append((j, b, new, patch))
        # An edge can align with a merged box or an adjacent column. Never
        # resolve competing matches by whichever OCR observation came first.
        if len(possible) == 1:
            j, b, new, patch = possible[0]
            output[i]['text'] = patch['content']
            # Keep the old vertical/end geometry; extend only the recovered edge.
            widened = dict(a, x=b['x'], width=a['x']+a['width']-b['x'])
            witnesses.append(dict(patch, oldIndex=i, widerIndex=j, before=line['text'],
                                  widerText=new['text'], globalBox=widened))
            used.add(j)
    accepted = len(witnesses) >= MIN_WITNESSES
    if not accepted:
        return old['lines'], {'version': PANE_VERSION, 'accepted': False, 'witnesses': witnesses,
                             'reason': 'insufficient_same_frame_prefix_witnesses'}
    # Short labels may have fewer than eight intact characters. Only consider
    # them after two long lines independently establish this same-frame edge.
    strong_indices = {w['oldIndex'] for w in witnesses}
    for i, line in enumerate(old['lines']):
        if i in strong_indices or not 4 <= len(line['text'].strip()) <= 16 or line['boundingBox']['x'] > .002:
            continue
        a = global_box(line, old['regionOfInterest'])
        possible = []
        for j, new in enumerate(wider['lines']):
            if j in used:
                continue
            b = global_box(new, wider['regionOfInterest'])
            overlap = min(a['y']+a['height'], b['y']+b['height'])-max(a['y'], b['y'])
            if overlap < .5*min(a['height'], b['height']) or not (b['x'] < old['regionOfInterest']['x']-.0005 < b['x']+b['width']):
                continue
            patch = prefix_extension(line['text'], new['text'], minimum_anchor=3, maximum_leading=1)
            if patch:
                possible.append((j, new, b, patch))
        if len(possible) == 1:
            j, new, b, patch = possible[0]
            output[i]['text'] = patch['content']
            witnesses.append(dict(patch, oldIndex=i, widerIndex=j, before=line['text'],
                                  widerText=new['text'], shortLabelAfterLongWitnesses=True,
                                  globalBox=dict(a, x=b['x'], width=a['x']+a['width']-b['x'])))
            used.add(j)
    by_index = {w['oldIndex']: w for w in witnesses}
    roi = wider['regionOfInterest']
    for i, item in enumerate(output):
        b = by_index[i]['globalBox'] if i in by_index else global_box(old['lines'][i], old['regionOfInterest'])
        item['boundingBox'] = {'x': (b['x']-roi['x'])/roi['width'], 'y': (b['y']-roi['y'])/roi['height'],
                              'width': b['width']/roi['width'], 'height': b['height']/roi['height']}
    assert len(output) == len(old['lines'])
    assert all(output[i]['text'] == old['lines'][i]['text'] for i in range(len(output)) if i not in by_index)
    return output, {'version': PANE_VERSION, 'accepted': True, 'witnesses': witnesses,
                    'nonEdgeTextUnchanged': True, 'lineCountUnchanged': True,
                    'reason': 'verified_same_frame_prefix_extensions',
                    'contentConstruction': 'original_lines_plus_verified_prefixes_not_entire_parent_ocr'}


def fuse_exposed_lines(old, wider, *, full_pane_proof=None):
    """Preserve the original OCR; add only proven prefixes and exposed lines.

    A second OCR pass can garble previously legible paragraphs. Its interior
    text therefore cannot replace the first observation. Entirely missing
    lines may be added only from the newly exposed left strip, at an unambiguous
    vertical insertion point. Existing line order and suffix bytes survive.
    """
    from copy import deepcopy
    output, prefix = extend_edges(old, wider)
    if not prefix['accepted'] and not (full_pane_proof or {}).get('accepted'):
        return old['lines'], {'version': FUSED_PANE_VERSION, 'accepted': False,
                              'reason': 'no_same_frame_edge_proof'}
    if not prefix['accepted']:
        output = deepcopy(old['lines'])
        for line, before in zip(output, old['lines']):
            b = global_box(before, old['regionOfInterest']); roi = wider['regionOfInterest']
            line['boundingBox'] = {'x': (b['x']-roi['x'])/roi['width'],
                'y': (b['y']-roi['y'])/roi['height'], 'width': b['width']/roi['width'],
                'height': b['height']/roi['height']}
    added = []
    original_boxes = [global_box(line, old['regionOfInterest']) for line in old['lines']]
    roi = old['regionOfInterest']
    for index, line in enumerate(wider['lines']):
        box = global_box(line, wider['regionOfInterest'])
        if not line['text'].strip() or not (
            # A line still touching the NEW cut edge is not a complete newly
            # exposed line (e.g. only the final 'e' of a clipped 'State').
            box['x'] > wider['regionOfInterest']['x']+.002*wider['regionOfInterest']['width']
            and box['x']+box['width'] <= roi['x']+1e-6
            and box['y'] >= roi['y']-1e-6
            and box['y']+box['height'] <= roi['y']+roi['height']+1e-6
        ):
            continue
        def same_row(other):
            overlap = min(box['y']+box['height'], other['y']+other['height'])-max(box['y'], other['y'])
            return overlap > .2*min(box['height'], other['height'])
        if any(same_row(b) for b in original_boxes):
            continue  # Not permission to repeat part of an existing line.
        center = box['y']+box['height']/2
        centers = [b['y']+b['height']/2 for b in
                   (global_box(l, wider['regionOfInterest']) for l in output)]
        slots = [i for i in range(len(output)+1)
                 if (i == 0 or centers[i-1] > center)
                 and (i == len(output) or centers[i] < center)]
        if len(slots) != 1:
            continue  # Ambiguous column/reading order: retain evidence, don't guess.
        output.insert(slots[0], deepcopy(line))
        original_boxes.append(box)
        added.append({'widerIndex': index, 'text': line['text'], 'globalBox': box,
                      'reason': 'wholly_in_newly_exposed_strip_with_unique_line_slot'})
    return output, {'version': FUSED_PANE_VERSION, 'accepted': True,
        'prefixProof': prefix, 'addedLines': added,
        'originalLineOrderPreserved': True, 'originalSuffixBytesPreserved': True,
        'reason': 'same_frame_preserved_ocr_with_proven_exposed_text'}
