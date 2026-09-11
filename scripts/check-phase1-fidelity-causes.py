#!/usr/bin/env python3
"""Small no-network checks for diagnostic helpers, not semantic repairs."""
import runpy
from pathlib import Path


def main():
    helpers = runpy.run_path(str(Path(__file__).with_name('review-phase1-fidelity-causes.py')))
    order = helpers['ordered_lines']
    verify = helpers['verify_island']
    pane = {'lines': [
        {'text': 'second line', 'boundingBox': {'x': .1, 'y': .5, 'height': .01}},
        {'text': 'first line', 'boundingBox': {'x': .2, 'y': .53, 'height': .01}},
    ]}
    assert order('second line\nfirst line', pane) == 'first line\nsecond line'
    for text, bad in [('missing line', pane), ('first line', {'lines': pane['lines'] + [pane['lines'][1]]})]:
        try:
            order(text, bad)
        except ValueError:
            pass
        else:
            raise AssertionError('Must not guess an absent or duplicate line position')
    case = {'case': 1, 'islandEvidence': [{
        'before': 'abc', 'after': 'XabcY', 'netInserted': 'XabcY',
        'rawPath': 'synthetic/raw.jsonl',
        'localEdits': [
            {'offset': 0, 'removed': '', 'inserted': 'X', 'rawLine': 1},
            {'offset': 4, 'removed': '', 'inserted': 'Y', 'rawLine': 2}],
        'runs': [{'offsetInNetEdit': 1, 'length': 3, 'content': 'abc'}],
    }]}
    assert verify(case)['islandCharacters'] == 3
    case['islandEvidence'][0]['runs'] = [{'offsetInNetEdit': 0, 'length': 1, 'content': 'X'}]
    try:
        verify(case)
    except AssertionError:
        pass
    else:
        raise AssertionError('Newly inserted text must not be called an old island')
    case['islandEvidence'][0]['localEdits'][0]['removed'] = 'wrong'
    try:
        verify(case)
    except AssertionError:
        pass
    else:
        raise AssertionError('A non-replayable edit must fail')
    print('PASS: line permutation, missing/ambiguous geometry rejection, exact edit replay, and old-text provenance')


if __name__ == '__main__':
    main()
