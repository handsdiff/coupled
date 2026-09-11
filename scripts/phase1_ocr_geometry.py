"""Versioned, lossless reading order for unambiguous single-flow OCR.

Works on retained boxes, not characters or model guesses. Ambiguous columns,
tables and merged multi-line boxes retain their original order for review.
"""
from collections import Counter
import math
from statistics import median

ORDER_VERSION = 'ocr-box-row-order-v1'


def valid_box(box):
    return isinstance(box, dict) and all(isinstance(box.get(k), (int, float)) and
        math.isfinite(box[k]) for k in ('x', 'y', 'width', 'height')) and box['width'] > 0 and box['height'] > 0


def row_order(lines):
    audit = {'version': ORDER_VERSION, 'changed': False, 'reason': 'already_in_geometric_order'}
    if not lines or not all(valid_box(l.get('boundingBox')) for l in lines):
        return lines, dict(audit, reason='missing_or_invalid_geometry')
    boxes = [l['boundingBox'] for l in lines]
    heights = [b['height'] for b in boxes]
    # A tall observation can contain multiple lines whose internal order cannot
    # be recovered by permuting boxes. Do not place other lines inside it.
    if any(h > 1.8 * median(heights) for h in heights):
        return lines, dict(audit, reason='ambiguous_multiline_box_preserve_order')
    # Multiple side-by-side rows can be a table or independent text columns.
    # This slice deliberately does not guess their block reading order.
    parallel_rows = set()
    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            overlap = min(a['y']+a['height'], b['y']+b['height']) - max(a['y'], b['y'])
            separated = a['x']+a['width'] <= b['x'] or b['x']+b['width'] <= a['x']
            if overlap >= .5 * min(a['height'], b['height']) and separated and \
                    min(len(lines[i]['text'].strip()), len(lines[j]['text'].strip())) >= 8:
                parallel_rows.update((i, j))
    if len(parallel_rows) >= 4:
        return lines, dict(audit, reason='ambiguous_parallel_columns_preserve_order')
    # Anchor each row to one box rather than chaining approximate pairwise
    # comparisons: the old 2%-distance comparator was not transitive.
    pending = sorted(range(len(lines)), key=lambda i: (-boxes[i]['y']-boxes[i]['height']/2, boxes[i]['x'], i))
    rows = []
    for i in pending:
        b = boxes[i]
        for row in rows:
            a = boxes[row[0]]
            overlap = min(a['y']+a['height'], b['y']+b['height']) - max(a['y'], b['y'])
            center_distance = abs(a['y']+a['height']/2-b['y']-b['height']/2)
            if overlap >= .6 * min(a['height'], b['height']) and center_distance <= .35 * min(a['height'], b['height']):
                row.append(i)
                break
        else:
            rows.append([i])
    permutation = [i for row in rows for i in sorted(row, key=lambda i: (boxes[i]['x'], i))]
    ordered = [lines[i] for i in permutation]
    assert Counter(l['text'] for l in ordered) == Counter(l['text'] for l in lines)
    changed = permutation != list(range(len(lines)))
    return ordered, dict(audit, changed=changed, reason='geometric_rows' if changed else audit['reason'],
                         permutation=permutation, rowCount=len(rows))
