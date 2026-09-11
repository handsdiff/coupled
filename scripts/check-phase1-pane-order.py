#!/usr/bin/env python3
"""Offline regression gates for row ordering and evidence-gated pane widening."""
from copy import deepcopy
import json

from phase1_ocr_geometry import row_order
from phase1_pane_edge import ancestors, screening_witnesses, extend_edges, prefix_extension, complete_observations_usable, fuse_exposed_lines
from phase1_order_invariance import preserve_order_invariant_proof


def line(text, x, y, w=.6, h=.02):
    return {'text': text, 'confidence': 1, 'boundingBox': {'x': x, 'y': y, 'width': w, 'height': h}}


def main():
    source = [line('lower complete sentence', .1, .50), line('upper complete sentence', .2, .525)]
    saved = deepcopy(source)
    ordered, proof = row_order(source)
    assert [l['text'] for l in ordered] == ['upper complete sentence', 'lower complete sentence']
    assert source == saved and proof['changed']
    assert row_order(ordered)[0] == ordered
    # Inline fragments belong on one actual baseline, not one window-% band.
    inline = [line('right', .6, .4, .2), line('left', .1, .4, .2)]
    assert row_order(inline)[0] == inline[::-1]
    table = [line('left cell one', .1, .5, .2), line('right cell one', .6, .5, .2),
             line('left cell two', .1, .4, .2), line('right cell two', .6, .4, .2)]
    assert row_order(table)[0] == table
    columns = [table[0], table[2], table[1], table[3]]
    assert row_order(columns)[0] == columns
    tall = source + [line('multiple merged lines', .1, .2, .6, .08)]
    assert row_order(tall)[0] == tall
    bad = [dict(source[0], boundingBox={})]
    assert row_order(bad)[0] == bad

    old = {'regionOfInterest': {'x': .2, 'y': .1, 'width': .6, 'height': .8},
           'lines': [line('irst complete sentence', 0, .5), line('econd complete sentence', 0, .6),
                     line('a protected third sentence', .05, .7)]}
    # Global y/height are equal despite differing local ROI x coordinates.
    new = {'regionOfInterest': {'x': .15, 'y': .1, 'width': .65, 'height': .8},
           'lines': [line('First complete sentence', 0, .5), line('Second complete sentence', 0, .6),
                     line('a protected third sentence', .12, .7)]}
    assert extend_edges(old, new)[1]['accepted']
    assert complete_observations_usable(new, old)
    assert not complete_observations_usable(new, {'lines': []})
    assert not complete_observations_usable({'lines': []}, old)
    assert not complete_observations_usable(new, {'lines': [{'text': '  '}]})
    assert not complete_observations_usable(new, dict(old, error='recognition_failed'))
    ambiguous = deepcopy(new); ambiguous['lines'].append(deepcopy(new['lines'][0]))
    assert not extend_edges(old, ambiguous)[1]['accepted']
    assert len(screening_witnesses(old, {'content': '\n'.join(l['text'] for l in new['lines'])})) == 2
    # Actual viewport clipping (no visible prefix outside pane) cannot be repaired.
    assert not extend_edges(old, old)[1]['accepted']
    changed = deepcopy(new); changed['lines'][2]['text'] = 'a fabricated third sentence'
    fused, proof = extend_edges(old, changed)
    assert proof['accepted'] and fused[2]['text'] == old['lines'][2]['text']
    exposed = deepcopy(changed)
    exposed['lines'].append(line('best', .005, .65, .04))
    fused, proof = fuse_exposed_lines(old, exposed)
    assert proof['accepted'] and not proof['addedLines']  # Nonmonotonic order is ambiguous.
    assert [l['text'] for l in fused] == ['First complete sentence', 'Second complete sentence', 'a protected third sentence']
    # Use monotonic Vision order for the uniquely exposed short-line case.
    ordered_old = dict(old, lines=list(reversed(old['lines'])))
    ordered_new = dict(exposed, lines=list(reversed(exposed['lines'])))
    fused, proof = fuse_exposed_lines(ordered_old, ordered_new)
    assert [l['text'] for l in fused] == ['a protected third sentence', 'best', 'Second complete sentence', 'First complete sentence']
    assert proof['originalSuffixBytesPreserved'] and proof['originalLineOrderPreserved']
    clipped = deepcopy(ordered_new); clipped['lines'][0]['boundingBox']['x'] = 0
    fused, proof = fuse_exposed_lines(ordered_old, clipped)
    assert not proof['addedLines']  # 'best' still touching the new cut is not complete.
    assert prefix_extension("ociated GTM foundations after this vector", "associated GT foundations after this vector")['content'] == "associated GTM foundations after this vector"
    assert prefix_extension('outdated analysis of those examples', 'its ourdated analysis of those examples') is None
    assert prefix_extension('1,000 examples were scored', '- 2,000 examples were scored') is None
    assert prefix_extension('-1,000 examples were scored', '+1,000 examples were scored') is None
    assert prefix_extension('your analysis of', 'your analysis of my feedback to ensure your analysis of') is None
    assert prefix_extension('Supported Providers (Website Supported Models | Docs)',
                            'Supported Providers (Website Supported Providers (Website Supported Models | Docs)') is None
    short_old = deepcopy(old); short_old['lines'].append(line('ludio', 0, .3, .1))
    short_new = deepcopy(new); short_new['lines'].append(line('Audio', 0, .3, .1))
    assert extend_edges(short_old, short_new)[0][-1]['text'] == 'Audio'
    assert not extend_edges(dict(short_old, lines=[short_old['lines'][-1]]), short_new)[1]['accepted']
    wrong_y = deepcopy(new)
    for l in wrong_y['lines']:
        l['boundingBox']['y'] -= .1
    assert not extend_edges(old, wrong_y)[1]['accepted']

    pane = dict(old, surfaceSelection={'selectedDepth': 0})
    raw = {'windowBounds': {'x': 0, 'y': 0, 'width': 1000, 'height': 1000},
           'accessibilitySurface': {'ancestors': [
               {'depth': 0, 'role': 'AXTextArea', 'frame': {'x': 200, 'y': 100, 'width': 600, 'height': 800}},
               {'depth': 1, 'role': 'AXGroup', 'frame': {'x': 150, 'y': 100, 'width': 700, 'height': 800}},
               {'depth': 2, 'role': 'AXWindow', 'frame': {'x': 0, 'y': 0, 'width': 1000, 'height': 1000}},
           ]}}
    assert len(ancestors(pane, raw)) == 1
    edge_only = ancestors(pane, raw, clipped_edge_only=True)[0]['regionOfInterest']
    assert edge_only['x'] == .15 and abs(edge_only['width']-.65) < 1e-12
    assert edge_only['x']+edge_only['width'] == old['regionOfInterest']['x']+old['regionOfInterest']['width']
    raw['accessibilitySurface']['ancestors'][1]['frame']['height'] = 820
    expanded = ancestors(pane, raw)[0]
    assert expanded['regionOfInterest']['height'] == old['regionOfInterest']['height']
    assert expanded['regionOfInterest']['y'] == old['regionOfInterest']['y']
    assert expanded['ancestorRegionOfInterest']['height'] > old['regionOfInterest']['height']
    raw['accessibilitySurface']['ancestors'][1]['role'] = 'AXWebArea'
    assert not ancestors(pane, raw)
    raw['accessibilitySurface']['ancestors'][1]['role'] = 'AXGroup'
    raw['accessibilitySurface']['ancestors'][1]['frame']['height'] = 950
    assert not ancestors(pane, raw)
    pane['regionOfInterest'] = dict(old['regionOfInterest'], x=.3)
    assert not ancestors(pane, raw)
    event = {'sourceEventID':'e', 'sessionID':'s', 'sourceRecordIDs':['r'], 'availableAt':'T2',
             'serialized': json.dumps({'kind':'read', 'content':'alpha line\nbeta line'})}
    changed_order = dict(event, serialized=json.dumps({'kind':'read', 'content':'beta line\nalpha line'}))
    before_decision = {'status':'repeated', 'boundaryEvidence': {'unexplainedWords':0, 'comparedAtOnset':'T1', 'draftSHA256':'d'}}
    after_decision = {'status':'unresolved', 'boundaryEvidence': {'unexplainedWords':1, 'comparedAtOnset':'T1', 'draftSHA256':'d'}}
    carried = preserve_order_invariant_proof(event, changed_order, before_decision, after_decision)
    assert carried['boundaryEvidence']['unexplainedWords'] == 0
    for extra in ['\nnot alpha', '\n42', '\nalpha line']:
        novel = dict(changed_order, serialized=json.dumps({'kind':'read','content':'beta line\nalpha line'+extra}))
        assert preserve_order_invariant_proof(event, novel, before_decision, after_decision) == after_decision
    assert preserve_order_invariant_proof(event, dict(changed_order, availableAt='T3'), before_decision, after_decision) == after_decision
    changed_field = deepcopy(after_decision); changed_field['boundaryEvidence']['initialFieldSHA256'] = 'different'
    assert preserve_order_invariant_proof(event, changed_order, before_decision, changed_field) == changed_field
    print('PASS: single-flow ordering; table/column/merged-box preservation; same-frame prefix proof; protected content; ancestor/root/window guards')


if __name__ == '__main__':
    main()
