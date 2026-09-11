"""Carry a proven non-novelty decision across lossless line reordering only.

Information did not change when only whole-line order changed. Keep the old
comparison proof, not old model-facing text. No fuzzy matching, added words,
case IDs, or targets are used to establish equivalence.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json

VERSION = 'read-boundary-lossless-order-invariance-v1'


def preserve_order_invariant_proof(before_event, after_event, before_decision, after_decision):
    if before_decision['boundaryEvidence']['unexplainedWords'] != 0 or after_decision['boundaryEvidence']['unexplainedWords'] == 0:
        return after_decision
    for key in ['sourceEventID', 'sessionID', 'sourceRecordIDs', 'availableAt']:
        if before_event.get(key) != after_event.get(key):
            return after_decision
    before, after = json.loads(before_event['serialized']), json.loads(after_event['serialized'])
    a, b = before.pop('content'), after.pop('content')
    if before != after or Counter(a.splitlines()) != Counter(b.splitlines()):
        return after_decision
    if before_decision['boundaryEvidence']['comparedAtOnset'] != after_decision['boundaryEvidence']['comparedAtOnset'] or \
       before_decision['boundaryEvidence']['draftSHA256'] != after_decision['boundaryEvidence']['draftSHA256'] or \
       before_decision['boundaryEvidence'].get('initialFieldSHA256') != after_decision['boundaryEvidence'].get('initialFieldSHA256'):
        return after_decision
    # Callers must compute both decisions with the same onset, field and draft;
    # their input bindings accompany the enclosing replay manifest.
    result = deepcopy(before_decision)
    result['boundaryEvidence']['orderInvariance'] = {
        'version': VERSION, 'rule': 'exact_same_line_multiset_same_observation_and_cutoff',
        'beforeContentSHA256': hashlib.sha256(a.encode()).hexdigest(),
        'afterContentSHA256': hashlib.sha256(b.encode()).hexdigest(),
        'modelContentUnchangedByThisRule': True,
        'refinedOrderDiagnosticBeforeCarry': after_decision['boundaryEvidence']}
    return result
