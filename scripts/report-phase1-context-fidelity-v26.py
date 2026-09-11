#!/usr/bin/env python3
"""Render the independent v26 fidelity audit, with explicit judgment limits."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAUSES = ROOT/'coupled-data/sep02-10-fidelity-causes-20260911'

# Earliest presently demonstrated failure, not the last matcher to reject it.
EARLIEST = {
37: ('OCR recognition', 'Confirmed against the pre-mutation field: AI becomes Al. This is a letter-recognition mismatch; the downstream single-token novelty rejection turns it into a false thought boundary.'),
240: ('Visible viewport edge → OCR', 'The bottom line is only partially visible in the actual screenshot. Garbage survives into READ. Earlier complete wording exists; this is not lost full-window capture.'),
288: ('OCR recognition/segmentation', 'The visible URL and initial field say j_upward/status/2092317212371759269. OCR produces i_upward/status/20923, puts the remaining digits on another line, and interposes nusomi. The selected screenshot contains the full URL. Separate residuals _ 15 / 85 are status UI.'),
445: ('OCR recognition/segmentation', 'Readable terminal text incorrect source identity is fused into identity.e panes. in a multi-line recognition box. Pane selection did not remove the characters.'),
452: ('Episode construction, after repaired OCR order', 'Native ordering fixes the demonstrated data that line displacement. The boundary is now fully explainable, but the frozen corpus still keeps cases 245 and 246 separate.'),
459: ('Episode construction / prior-draft matching', 'The boundary already has a complete non-novelty proof, but the corpus has not been rebuilt to join the thought.'),
484: ('OCR recognition → order-sensitive episode matching', 'AI is recognized as Al. The old scrambled ordering nevertheless allowed an exact non-novelty proof; native ordering changes the alignment and leaves one al residue. This is a newly failing matcher gate, not newly invented letters.'),
487: ('OCR recognition → order-sensitive episode matching', 'Same al mismatch and newly failing non-novelty gate as case 262; original field says AI. The READ order improves while the matcher gets less reliable.'),
498: ('Visible viewport edge; additional OCR recognition', 'A cut-off bottom line becomes rasenning aur…; fully visible RLHF is also recognized as RHF. Native order reduces residuals but fixes neither cause.'),
505: ('Visible viewport edge; additional OCR recognition', 'Same family of partial-bottom glyphs and RLHF→RHF errors. Native order reduces, but does not eliminate, the residual.'),
622: ('OCR text representation / episode matching', 'The screen correctly shows events.jsonl. OCR splits this as events. json and misreads UI as Ul. The current READ is understandable; the matcher fails on json and, after reordering, complete. An exact minimal matching repair is not established.'),
641: ('Prior-view coverage / episode matching — partly unresolved', 'The remainder includes whole response phrases and queued user text, not just OCR errors. One selected prior does not prove that every phrase was available at onset. Human grouping recommends repair, but a safe complete causal proof remains pending.'),
645: ('No episode defect: new inbound answer', 'Retain the split: the later screen contains a new answer, not merely a re-rendered old view. Native ordering did not collapse this control.'),
648: ('Prior-view coverage / episode matching', 'The regression-test passage exists before onset. The current matcher now recognizes it, leaving app y and icon-like 9 3 residue. The old 35-repair/4-control accounting was corrected to 36/3; this is not a new v26 failure.'),
660: ('Pane selection', 'The selected pane starts inside visible lines, cutting For explicit to r explicit and many other prefixes. Complete screenshots survive. Native ordering was not adopted for these primary panes because recognition also changed.'),
685: ('OCR recognition/segmentation', 'The screenshot table says Some unfinished prompts receive loss / before the user finishes composing / them. OCR merges it into Store the ser fishes compose oss. Reordering existing lines cannot repair that recognition box.'),
687: ('Visible UI overlay → OCR', 'A floating down-arrow covers training run and OCR emits trainin Ji. The source image itself is occluded; use another existing view if a reconstruction is needed, not guessed spelling.'),
712: ('Episode construction / prior-draft matching', 'The existing matcher explains the READ; the reviewed composition remains fragmented in the frozen corpus.'),
737: ('Episode construction / prior-draft matching', 'The boundary is proved non-novel; the following old-text island in 400 is a separate consequence of the wrong episode start.'),
776: ('Episode construction, after repaired OCR order', 'The old s stages residue is gone in the current proof. This does not itself merge 421 and 422; their target grouping remains frozen.'),
832: ('No episode defect: new inbound answer', 'Actual new numeric results and explanation arrived after onset. All three current READs remain blocking, as intended.'),
834: ('Episode construction / prior-draft matching', 'Already fully explained. A punctuation-only retained island is not independently counted as a material defect.'),
836: ('Episode policy for asynchronous UI status', 'Updated the / 1 file changed +22 -18 really appeared. Treating that status change as a boundary of the ongoing thought is the problem; OCR did not invent it.'),
868: ('OCR recognition; residual draft matching partly unresolved', 'The screenshot and held field have gradypb and oneill_c; OCR has grady.pb and oneill c. The remaining similarity token belongs to the human wording. These are separable recognition and draft-alignment issues.'),
929: ('OCR recognition / draft alignment', 'phase1 becomes phasel in tool paths; i remains from draft alignment. Case 499 additionally has the earlier, more serious false-empty WRITE onset.'),
931: ('OCR recognition/layout', 'Readable having and overfocusing become havine and soviversinnient. The current renderer still exposes the corrupt draft text; reordering is not a spelling repair.'),
1003: ('Episode matching / icon filtering after repaired order', 'Native ordering removes the visual relationships and reference residuals. Only 3 remains; the screen location of that final token is not fully isolated, so do not call it proved new content or silently merge.'),
1052: ('Pane selection', 'The narrower ROI clips Images/Content/Audio/Immediate into nages/ontent/ludio/nmediate. The later wider view is now fully explainable, but the first clipped READ still blocks the pair.'),
1064: ('Pane selection elsewhere; repaired OCR-order blocker', 'The just post ordering error is fixed and the boundary is now explainable. Independent clipped-prefix witnesses elsewhere in that pane remain; do not call the entire READ perfect.'),
1068: ('Pane selection', 'The Obsidian ROI begins about 361 pixels into a 1179-pixel screenshot, inside visible bullet text. mention/emphasis become ation/ohasis. The wider full image survives.'),
1076: ('UI filtering / episode policy', 'word comes from the changed selection count 1 word 6 characters, not new document information. This is an interface-status boundary, not bad spelling.'),
1079: ('Pane selection; correct independent episode split', 'The Obsidian READ cuts visible prefixes, but the ChatGPT READ genuinely adds What was reassuring. Repairing the pane must not merge this control.'),
1083: ('Episode construction / prior-draft matching', 'All intervening content is already explained; the frozen target still ends too early.'),
1148: ('UI/view scope / episode matching — partly unresolved', 'Discord GIF/sidebar/welcome/status content is real interface text. The GitHub READ is repeated. A residual user name also needs source-coverage validation; this is not all OCR corruption.'),
1149: ('UI filtering / episode policy', 'The remaining BIUS 6 is a formatting toolbar. Other READs are fully explained. A toolbar cannot by itself justify a new human thought.'),
1152: ('OCR recognition plus UI filtering', 'An existing introduction has I/l confusion in Ive, while 品 and air come from toolbar/icon material. Old-text islands in this target need separate episode-onset reconstruction.'),
1176: ('Table/text order → episode matching', 'The table is readable and native order is improved. comparisons, token and effectively time remain unmatched. A matcher failure is not proof of novel information; no complete minimal repair has been proved.'),
1212: ('Episode construction / prior-draft matching', 'Complete non-novelty proof exists; frozen grouping has not applied it.'),
1234: ('Episode construction / prior-draft matching', 'Complete proof exists for one continuing thought. Case 687 also contains old-text islands because its onset is too late.'),
}


def read(p):return json.loads(p.read_text())
def save(p,v):
    with p.open('x') as f:json.dump(v,f,ensure_ascii=False,indent=2,sort_keys=True);f.write('\n')
def link(p,label,line=None):return f'[{label}](<{p}{":"+str(line) if line else ""}>)'


def main():
    p=argparse.ArgumentParser();p.add_argument('--audit',type=Path,required=True);p.add_argument('--packed',type=Path,required=True)
    p.add_argument('--output',type=Path)
    a=p.parse_args();audit=a.audit.resolve();root=a.output.resolve() if a.output else audit
    if a.output:root.mkdir(parents=True,exist_ok=False)
    summary=read(audit/'summary.json');boundaries=read(audit/'boundaries.json');cases=read(audit/'cases.json')
    packed=read(a.packed/'summary.json');packed_cases={r['case']:r for r in read(a.packed/'cases.json')}
    frozen=read(CAUSES/'stage-evidence.json');oldb={b['candidateLine']:b for b in frozen['boundaries']}
    by_case={r['case']:r for r in cases}
    output_cases=[]
    for n,c in by_case.items():
        findings=[]
        for b in boundaries:
            if n not in b['cases']:continue
            stage,explanation=EARLIEST[b['candidateLine']]
            findings.append({'boundary':b['candidateLine'],'earliestDemonstratedStage':stage,'explanation':explanation,
                'currentProofComplete':b['proofNowComplete'],'sourceEvidence':[
                    {'eventID':r['eventID'],'sourceRecordIDs':r['sourceRecordIDs'],'content':r['content']}
                    for r in b['currentReads']]})
        if c['prefixFinding']:
            findings.insert(0,{'earliestDemonstratedStage': 'Raw AX snapshot / semantic terminal selection' if n==523 else 'Raw AX empty-field observation → episode onset trust',
                'explanation':c['prefixFinding'],'currentProofComplete':False})
        if c['otherFinding']:
            findings.insert(0,{'earliestDemonstratedStage':'Raw AX field value → semantic target trust','explanation':c['otherFinding']['finding'],'currentProofComplete':False})
        if c['oldTextIsland']:
            findings.append({'earliestDemonstratedStage':'Episode onset/grouping → contiguous net diff',
                'explanation':'The exact local transitions preserve old initial-field spans between new edits. The contiguous diff faithfully includes those spans. The error is choosing an incomplete composition as the training unit, not permission to delete every unchanged substring.',
                'currentProofComplete':False})
        output_cases.append(dict(c,findings=findings,packedReadDefects=packed_cases[n]['after']['verifiedReadDefects']))
    save(root/'detailed-case-ledger.json',output_cases)
    before=packed['before'];after=packed['after']
    lines=['# Current context fidelity: v26 independent audit','',
       'September 11, 2026. Numbers refer to the frozen **687-example Sep 2–10 cohort**, not the older READ-review UI. '
       'This is a diagnostic report; raw captures, production rules, episode membership, and training authorizations are unchanged.','',
       '## Answer: separate rates, not a misleading single percentage','',
       f'- **{summary["knownUnrepairedExamples"]}/687 ({summary["knownUnrepairedPercent"]:.2f}%) have reviewed grouping/target problems still awaiting repair.** '
       'That is the old 67 plus cases 360–361, whose classification was corrected before this READ-only pass. It is not 69 newly introduced errors. Case 529 is separately unresolved.',
       f'- **At least {after["verifiedReadDefectExposureCount"]}/687 ({after["percent"]:.2f}%) regenerated 32K contexts contain a specific screenshot-verified READ defect from this audit.** '
       f'The same witness scan finds {before["verifiedReadDefectExposureCount"]}/687 ({before["percent"]:.2f}%) before the fixes. '
       'This is a measured lower bound, not a complete population error rate. Small OCR errors and a severe missing paragraph are both counted once at the example level. '
       'The witness inventory focuses on remaining defects, not every ordering error repaired by native OCR. Do not interpret 84→83 as the total effectiveness of the fixes. '
       'Four cases (603–606) lose exposure because more retained content moves the bad old frame outside 32K, not because its pane was repaired; three (499–501) gain their first listed defect through the new code-order regression.',
       '- The two rates overlap and must not be added. Thought-boundary quality, malformed READ content, and historical-context exposure are different denominators/claims.',
       '- The remaining unflagged examples are **not certified clean**. Every input was packed and mechanically checked; not every pixel of every historical pane was manually adjudicated. The previous 54/687 shadow count was not a global context-error rate.', '',
       '## Biggest offenders','',
       '### Direct target/grouping impact','',
       '| Failure | Reviewed examples | Earliest responsible step |',
       '|---|---:|---|',
       f'| An unfinished thought split on a non-novel READ | {len(summary["boundaryExamples"])} | Mixed: pane/OCR defects or episode novelty/status policy; see each boundary below |',
       f'| Missing beginning / wrong empty composer query | {len(summary["prefixCases"])} | AX observation or terminal-state selection, then trusting empty AX as a fresh onset |',
       f'| Old text inside a partial-thought target | {len(summary["oldTextIslandCases"])} | Episode onset/grouping, followed by an otherwise faithful contiguous diff |',
       '| Wrong sentence order in the target itself | 1 (case 9) | Raw AX field value disagrees with the screenshot; reducer trusts it |','',
       'Counts overlap. The eight old-text cases are review/repair cases, not proof that every old word should be removed. Some partial compositions require onset reconstruction rather than string cleanup.','',
       '### Known READ defects surviving actual 32K rendering','',
       '| Earliest step | Context examples exposed |', '|---|---:|']
    for stage,ns in sorted(after['byEarliestStage'].items(),key=lambda item:-len(item[1])):
        lines.append(f'| {stage} | {len(ns)} |')
    lines += ['', 'These counts overlap across stages. The scope is the explicit witness inventory in '
       +link(a.packed.resolve()/'verified-read-witnesses.json','verified-read-witnesses.json')+
       '. Each occurrence must survive adjacent-repeat rendering and oldest-event truncation; merely retaining an event ID does not count.', '',
       '## Are the two fixes strict improvements without regressions?', '',
       '**No. They have clear benefits, but strict monotonic improvement is disproved.**', '',
       '### Blacklist removal: restores evidence, also restores control noise','',
       'It recovers research notes and source code that recurrence never justified deleting. The motivating Obsidian READ returns from a stray glyph and word counts to 1,932 characters. '
       'But the same change restores controls missed by the narrower filters, including `Do anything`, `gpt-5.6-sol xhigh fast • ~/coupled`, and `Approve for me` variants.',
       f'The narrow verified-footer scan finds those reinstated controls in **{len(after["restoredUIFooterExposureCases"])}** of the regenerated 32K contexts (before: {len(before["restoredUIFooterExposureCases"])}). '
       'This is noise exposure, not an assertion that all those examples have a materially wrong target. Scored/Pass/Fail dashboard text is not automatically classified as noise; it can be task-relevant evidence.', '',
       '### Native OCR order: better prose, one demonstrated code-order regression','',
       'In READ `evt_73900df9ac3f11dd760f6d5cc45d5f1ff57b8ab282146eadbe46009f15ac4452`, the source screenshot has `rg --files ...` followed by `sed`. '
       'The adopted native order instead emits:', '', '```text', '• Ran rg', 'sed', '-files -g AGENTS.md ...', '```', '',
       'The old order kept the flags with `rg`. This is a real new ordering error, although it affects a displayed diagnostic command rather than a human WRITE target. '
       'Source: '+link(ROOT/'coupled-data/phase1-ordinary-work-2026-09-07-1/screenshots/visual-67D79D17-9323-4252-A66E-9977028C60D0.png','screenshot')+'.', '',
       'Cases 262 and 263 also lose a previously complete non-novelty proof: an existing `AI`→`Al` OCR mismatch becomes an unmatched `al` after reordering. '
       'The prose ordering is better, but the matcher is not invariant to an evidence-preserving permutation. Those are downstream proof regressions, not evidence of new external information.', '',
       '### Gates that held','',
       '- All 1,633 underlying WRITEs and all 687 target/query/onset/mask contracts are unchanged.',
       '- All three genuine-new-information boundary controls still block merging.',
       '- Cases 245–246, 421–422, and 580–581 gain complete non-novelty proofs. Two earlier proofs are lost, leaving **10/36** reviewed non-novel boundaries mechanically explained, versus **9/36** before. None of those proof changes has yet changed frozen episode membership.',
       '- Legacy-v25 replay and repeated native-evidence materialization were byte-identical in the prior audit. That establishes reproducibility, not semantic perfection.',
       '- Only 156/11,579 pane records adopted the native evidence. Most historical panes remain in the legacy order. The rejected fresh recognition changes were not silently admitted.', '',
       '## Every reviewed boundary: earliest step and current result','',
       '| Cases | Earliest demonstrated problem | Before → after unexplained words | Current conclusion |',
       '|---|---|---|---|']
    for b in boundaries:
        number=b['candidateLine'];stage,explanation=EARLIEST[number]
        label=' → '.join(str(n) if n is not None else 'continuation' for n in b['cases'])
        old='/'.join(str(r['boundaryEvidence']['unexplainedWords']) for r in b['oldAssessments'])
        new='/'.join(str(r['boundaryEvidence']['unexplainedWords']) for r in b['newAssessments'])
        disposition='Retain genuine split' if b['annotation']['disposition']=='retain_boundary' else 'Proof passes; target grouping still unrepaired' if b['proofNowComplete'] else 'Still requires repair/review'
        lines.append(f'| {label} | {stage} | {old} → {new} | {disposition} |')
    lines += ['', 'Residual counts are debugging measurements, not error severity or proof of novelty.', '', '## Case-by-case ledger','']
    for c in output_cases:
        n=c['case'];status='repair pending' if c['knownIssueStillPresent'] else 'hold: onset unresolved' if c['unresolvedHold'] else 'control / separate READ issue'
        lines += [f'### Case {n} — {status}', '', '**Current target:** '+json.dumps(c['targetText'],ensure_ascii=False), '']
        for finding in c['findings']:
            lines += ['**Earliest step:** '+finding['earliestDemonstratedStage']+'. '+finding['explanation'], '']
        links=[link(ROOT/m['path'],f'raw {m["line"]}',m['line']) for m in c['rawMembers']]
        lines += ['WRITE evidence: '+ '; '.join(links), '']
        for number in c['boundaryGroups']:
            original=oldb[number]
            for r in original['reads']:
                for rid in r['paneRecordIDs'][-1:]:
                    raw=frozen['raw'][rid]
                    image=ROOT/Path(raw['auditSourcePath']).parent/raw['screenshotRelativePath']
                    lines += ['READ evidence: '+link(image,'screenshot')+'; '+link(ROOT/raw['auditSourcePath'],f'raw {raw["auditSourceLine"]}',raw['auditSourceLine'])+'.','']
    lines += ['## All 687 cases: actual context exposure', '',
       'The machine-readable '+link(a.packed.resolve()/'cases.json','packed case ledger')+' has an entry for every example, including exact source IDs and surviving bad substrings. '
       'An empty witness list means not flagged by this scan, not manually certified clean.', '',
       '### Every context with a confirmed retained READ defect', '',
       'These are target example numbers; the source-neighborhood numbers identify where the bad READ was captured. A defect can enter several later contexts.', '',
       '| Target example | Earliest defective step | Source neighborhood | Exact retained witness |',
       '|---|---|---|---|']
    for n,row in sorted(packed_cases.items()):
        for w in row['after']['verifiedReadDefects']:
            source='/'.join(str(c) for c in w['reviewCases'] if c is not None) or 'native-order code regression'
            snippets='; '.join(w['phrases']).replace('|','\\|').replace('\n',' → ')
            lines.append(f'| {n} | {w["stage"]} | {source} | `{snippets}` |')
    lines += ['',
       '## Practical priority', '',
       '1. Repair false-empty onset/terminal selection and the reviewed thought grouping, using preserved trajectories. These directly distort loss targets.',
       '2. Repair verified pane edges and the most damaging OCR region/line-order failures; do not relabel all 26 remaining boundary problems as OCR.',
       '3. Keep the blacklist deletion, but do not advertise it as noise-free. Handle demonstrated interface controls separately from recurring substantive evidence.',
       '4. Protect the native-order code regression and the two lost novelty proofs before promoting this candidate. Do not undo good prose ordering merely to placate the matcher.',
       '5. Rebuild episodes and pack under a new reviewed version before claiming a lower training-data error rate. Current v26 is not yet wired into the standard packer version allowlist; this audit explicitly invokes the existing dependency-aware renderer without pretending a production pack was frozen.', '',
       'No provider calls, training, capture changes, or source edits were made by this audit.']
    (root/'REPORT.md').write_text('\n'.join(lines)+'\n')
    save(root/'report-manifest.json',{'scriptSHA256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         'reportSHA256':hashlib.sha256((root/'REPORT.md').read_bytes()).hexdigest(),
         'reviewedCases':len(output_cases),'knownTargetGroupingCases':summary['knownUnrepairedExamples'],
         'packedAudit':str(a.packed.resolve()),'providerCalls':0,'scope':'diagnostic only'})
    print(root/'REPORT.md')


if __name__=='__main__':main()
