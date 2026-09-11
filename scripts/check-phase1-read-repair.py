#!/usr/bin/env python3
"""No-network regression checks for accepting only lossless OCR reordering."""
from pathlib import Path
import runpy

ROOT=Path(__file__).resolve().parent
permutation=runpy.run_path(str(ROOT/'materialize-phase1-native-order.py'))['permute']
native=runpy.run_path(str(ROOT/'build-phase1-read-surface-evidence.py'))['has_native_order']
lines=[{'text':'tail','boundingBox':{'x':1}}, {'text':'head','boundingBox':{'x':2}},
       {'text':'tail','boundingBox':{'x':3}}]
assert permutation(lines,[{'text':'head'},{'text':'tail'},{'text':'tail'}])==[1,0,2]
assert permutation(lines,[{'text':'head'},{'text':'invented'},{'text':'tail'}]) is None
assert permutation(lines,[{'text':'head'},{'text':'tail'}]) is None
assert lines[0]=={'text':'tail','boundingBox':{'x':1}}
assert not native({}) and not native({'orderingVersion':'legacy_xy_sort'})
assert native({'orderingVersion':'vision-native-order-v1'})
assert native({'ocrOrderingVersion':'vision-native-order-v1'})
print('PASS: lossless native-order permutations, duplicate-line preservation, recognition rejection, legacy-cache rejection')
