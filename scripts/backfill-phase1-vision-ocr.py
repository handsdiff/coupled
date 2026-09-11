#!/usr/bin/env python3
"""Bounded, local-only OCR of pilot frames without raw OCR; never edits sessions."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import resource
import subprocess
import tempfile
import time

from phase1_read_model_comparison import canonical, dump, dump_rows, file_hash, fingerprint, rows


def require(value,message):
    if not value:raise ValueError(message)


def run(inventory,output):
    inventory=inventory.resolve();output=output.resolve()
    require(not output.exists(),'Use a fresh sidecar directory')
    hashes=json.loads((inventory/'artifact-hashes.json').read_text())
    for name,digest in hashes.items():require(file_hash(inventory/name)==digest,'Inventory changed')
    frames=[r for r in rows(inventory/'frames.jsonl') if r['ocr'] is None]
    output.mkdir(parents=True,mode=0o700)
    records=output/'records';records.mkdir()
    source=Path(__file__).with_name('ocr-phase1-surface-regions.m').resolve()
    source_hash=file_hash(source)
    roi={'x':0,'y':0,'width':1,'height':1}
    observations=[]
    with tempfile.TemporaryDirectory(prefix='coupled-vision-ocr-') as temp:
        binary=Path(temp)/'ocr'
        command=['xcrun','clang','-fobjc-arc','-fblocks','-framework','Foundation','-framework','Vision','-framework','ImageIO','-framework','CoreGraphics',str(source),'-o',str(binary)]
        compiled=subprocess.run(command,capture_output=True,text=True)
        require(compiled.returncode==0,'OCR helper build failed: '+compiled.stderr[:4000])
        binary_hash=file_hash(binary)
        for index,frame in enumerate(frames,1):
            require(file_hash(frame['screenshotPath'])==frame['screenshotSHA256'],'Screenshot changed before OCR')
            job={'jobID':frame['recordID'],'imagePath':frame['screenshotPath'],'regionOfInterest':roi}
            start=time.monotonic()
            # One child per image caps lifetime allocations in Vision. No pool,
            # no screenshot directory scan, no concurrent personal-data jobs.
            process=subprocess.run([str(binary)],input=canonical(job)+'\n',text=True,capture_output=True,timeout=90,check=True)
            result=json.loads(process.stdout)
            require(not result.get('error') and result.get('jobID')==frame['recordID'] and result.get('regionOfInterest')==roi,'OCR failed; no silent missing observation')
            require(isinstance(result.get('content'),str),'OCR omitted content')
            require(file_hash(frame['screenshotPath'])==frame['screenshotSHA256'],'Screenshot changed during OCR')
            observation={'recordID':'ocr_backfill_'+fingerprint([frame['recordID'],frame['screenshotSHA256'],source_hash]),
                'sourceFrameRecordID':frame['recordID'],'sessionID':frame['sessionID'],
                'sourceRawPath':frame['rawPath'],'sourceRawLine':frame['rawLine'],
                'capturedAt':frame['capturedAt'],'processedAt':datetime.now(timezone.utc).isoformat(),
                'screenshotSHA256':frame['screenshotSHA256'],'origin':'local_full_window_ocr_backfill',
                'content':result['content'],'contentWasTruncated':False,'lines':result['lines'],
                'captureScope':'canonical_full_window','elapsedSeconds':time.monotonic()-start,
                'cropFractions':{'viewportSideCropFraction':0,'viewportTopCropFraction':0,'viewportBottomCropFraction':0}}
            dump(records/(observation['recordID']+'.json'),observation)
            observations.append(observation)
            print(f'Full-window OCR {index}/{len(frames)}; characters={len(result["content"])}',flush=True)
    require(file_hash(source)==source_hash,'OCR implementation changed during replay')
    dump_rows(output/'observations.jsonl',observations)
    manifest={'version':'phase1-vision-ocr-backfill-v1','status':'complete','completedFrames':len(observations),
        'inventory':str(inventory),'inventoryArtifactsSHA256':file_hash(inventory/'artifact-hashes.json'),
        'sourceSHA256':source_hash,'binarySHA256':binary_hash,'macOS':platform.mac_ver()[0],
        'implementationSHA256':file_hash(__file__),
        'settings':{'recognitionLevel':'accurate','usesLanguageCorrection':True,'automaticallyDetectsLanguage':True,'regionOfInterest':roi,'maxCharacters':None},
        'captureTimePolicy':'Original screenshot time is causal availability; processedAt is local replay time, never backdated.',
        'resourcePolicy':'Sequential single-image child processes, 90 second timeout per image',
        'peakChildMiB':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024**2,
        'providerCalls':0,'rawSessionMutations':0,
        'artifactSHA256':{str(p.relative_to(output)):file_hash(p) for p in sorted(output.rglob('*.json'))},
    }
    manifest['artifactSHA256']['observations.jsonl']=file_hash(output/'observations.jsonl')
    dump(output/'manifest.json',manifest)
    print(json.dumps({k:manifest[k] for k in ('status','completedFrames','peakChildMiB','providerCalls','rawSessionMutations')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args();run(args.inventory,args.output)
