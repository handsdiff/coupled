"""Exact read-only record views for legacy consumers that repeatedly scan rows.

Retain metadata once, defer expanded history strings until explicitly read.
This changes storage/access cost only; Mapping reads and expansion return the
original record, verified by tests. Never supplies new semantic evidence.
"""
from collections.abc import Mapping, Sequence
from phase1_jsonl import JSONLSequence


class DeferredHistoryRecord(Mapping):
    def __init__(self, source, index, record):
        self.source, self.index = source, index
        self.keys_in_order = tuple(record)
        self.metadata = {k:v for k,v in record.items() if k not in {"context", "modelInput"}}

    def __getitem__(self, key):
        if key in self.metadata:
            return self.metadata[key]
        if key in self.keys_in_order:
            return self.source[self.index][key]
        raise KeyError(key)

    def __len__(self): return len(self.keys_in_order)
    def __iter__(self): return iter(self.keys_in_order)


class HistoricalExamples(Sequence):
    def __init__(self, path):
        self.source = JSONLSequence(path)
        self.records = [DeferredHistoryRecord(self.source, i, row) for i,row in enumerate(self.source)]

    def __len__(self): return len(self.records)
    def __getitem__(self, index): return self.records[index]
    def __iter__(self): return iter(self.records)
