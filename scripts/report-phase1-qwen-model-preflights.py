#!/usr/bin/env python3
"""Local comparison, complete final-answer review, and memorization curves."""
import argparse
import json
from pathlib import Path

LABELS={'qwen38_off':'Qwen3.8-27B · reasoning off','qwen36_hybrid':'Qwen3.6-35B-A3B · reasoning off',
        'qwen35_base':'Qwen3.5-35B-A3B-Base','qwen38_reasoning_low':'Qwen3.8-27B · reasoning low'}


def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--plot',action='store_true');a=p.parse_args();d=a.directory
    audit=json.loads((d/'audit.json').read_text());run=json.loads((d/'run.json').read_text())
    oldaudit=json.loads((a.reference/'audit.json').read_text());oldrun=json.loads((a.reference/'preflight.json').read_text())
    ref=json.loads((a.reference/'preparation.json').read_text())
    grades=json.loads((d/'capability-review.json').read_text());oldgrades=json.loads((a.reference/'capability-review.json').read_text())
    models={'qwen38_off':oldrun,**run['models']}
    ordered=['qwen38_off','qwen36_hybrid','qwen35_base','qwen38_reasoning_low']
    if a.plot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,axes=plt.subplots(1,2,figsize=(11,4.4))
        colors=['#3267a8','#c97e23','#27937e']
        for key,color in zip(ordered[:3],colors):
            points=oldaudit['epochTrainingNLL'] if key=='qwen38_off' else audit['models'][key]['epochTrainingNLL']
            axes[0].plot([x['epoch'] for x in points],[x['trainingNLL'] for x in points],'-o',color=color,label=LABELS[key])
            o=models[key]['overfit'];snaps=o['snapshots']
            axes[1].plot([0]+[x['epoch'] for x in snaps],[snaps[0]['baselineMeanNLL']]+[x['meanNLL'] for x in snaps],'-o',color=color)
        for ax in axes:
            ax.set_yscale('log');ax.set_xlabel('Passes over the same ten examples');ax.set_ylabel('Target-token NLL (log scale)')
            ax.grid(alpha=.2);ax.spines[['top','right']].set_visible(False)
        axes[0].set_title('Training loss: token-weighted epoch mean');axes[1].set_title('Known-target likelihood at saved checkpoints')
        fig.suptitle('Ten-example memorization test — not future-write learning',fontsize=13)
        handles,labels=axes[0].get_legend_handles_labels();fig.legend(handles,labels,loc='lower center',ncol=3,fontsize=8)
        fig.tight_layout(rect=(0,.10,1,.92));fig.savefig(d/'memorization-curves.png',dpi=160);plt.close(fig)
        print(d/'memorization-curves.png');return
    table=['| Model | Useful frozen answers | Cases with ≥1/4 useful | Overfit exact | Known-target NLL | Frozen median / mean latency | Mean query token-cost estimate |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for key in ordered:
        if key=='qwen38_off':
            cap=oldaudit['capability'];passes=cap['passedAnswers'];any_count=cap['anyOfFour'];lat=oldaudit['latency']['frozenCapability']
            price=ref['pricesPerMillionTokens'];answers=[a for rows in oldrun['capability']['scores'].values() for a in rows]
            cost=sum((a['inputTokens']*price['prefill']+a['outputTokens']*price['sample'])/1e6 for a in answers)/len(answers)
        else:
            cap=audit['models'][key]['intentGrades'];passes=cap['passingAnswers'];any_count=cap['anyOfFourCases'];lat=audit['models'][key]['frozenLatency']
            cost=audit['models'][key]['frozenMeanEstimatedQueryUSD']
        o=models[key].get('overfit');last=o['snapshots'][-1] if o else None
        fit=f"{last['normalizedExactMatches']}/10 at epoch {last['epoch']}" if o else 'Not trained'
        loss=f"{last['baselineMeanNLL']:.3f} → {last['meanNLL']:.4f}" if o else '—'
        table.append(f"| {LABELS[key]} | {passes}/80 | {any_count}/20 | {fit} | {loss} | {lat['medianSeconds']:.2f}s / {lat['meanSeconds']:.2f}s | ${cost:.4f} |")
    text=['# Bounded Qwen model comparison — September 12, 2026','',
          'The larger old/new experiment has **not** started. These are mechanical, memorization and frozen-capability checks—not a chronological personalization result.','',*table,'',
          'Grades are implementer judgments using the intended-thought rubric, not exact-match scoring or independent adjudication. Useful elaboration is allowed. These twenty substantive, application-balanced cases are not a random corpus estimate. Four answers per case are not four independent human decisions.','',
          'Latency is SDK request-to-result time over all answers, including failures. Four seeds reuse each prompt, so caching and output length affect these timings; this is not a cold-prefill or useful-suggestion latency guarantee. Passing-only latency is recorded separately in `audit.json`.','',
          'Per-query cost uses saved actual input/output counts at the full execution-time token rates, including reasoning tokens. It excludes cache discounts and storage, is not invoice-verified, and does not include training. The authorization bounds below instead reserve each request’s maximum output allowance.','',
          '## Cost and preservation','',
          f"- Original 27B-off authorization: dispatched-token upper bound ${oldaudit['maximumTokenCostUSD']:.4f}, plus its $0.25 storage reserve.",
          f"- Additional shared $20 authorization, including both prior attempts and all {len(audit['restorationControls'])} diagnostics: dispatched-token upper bound **${audit['totalAdditionalAuthorizationTokenBoundUSD']:.4f}**, plus $0.50 storage reserve.",
          '- These are conservative token bounds, not invoice-verified charges; cached-prefill discounts may lower actual billing.',
          '- Earlier 27B-xhigh probes remain separate: 3/4 exhausted 8,192 tokens without a final answer. They were not replaced or included as additional low-effort samples.',
          '- The ten previously completed baseline operations were reused with source hashes. Every dispatched operation, raw response, token sequence, timing and returned loss remains saved.','',
          '## Training and generation contract','',
          '| Setting | Three trained configurations | Reasoning-low configuration |','|---|---|---|',
          '| Training data | Same ten new-pipeline examples; separate disposable old/new three-example checks | None |',
          '| Initialization | Fresh adapter for each test | Frozen base model |',
          '| LoRA | Rank 32; attention, MLP and unembedding; seed 17 | None |',
          '| Optimizer | Adam, LR 2e-4, betas 0.9/0.95, epsilon 1e-12, weight decay 0, clipping 1.0 | None |',
          '| Batch / order | One example; deterministic seed-17 epoch order | Not applicable |',
          '| Overfit stopping | Evaluate epochs 5 and 10; stop at ≥8/10 normalized exact and NLL ≤0.25 and ≤25% of baseline | Not applicable |',
          '| Loss | Exact authored completion and the literal paste-action marker plus native EOS; prompt/query masked; correct one-token causal shift | No training or invented reasoning traces |',
          '| Generation | Temperature 0.6, seeds 17–20, maximum 512 output tokens | Temperature 0.6, seeds 17–20, low reasoning, maximum 8,192 total output tokens |',
          '| Native termination | Base: 248044; chat/hybrid: 248046 | 248046 after native reasoning/final structure |',
          '| Context | Identical frozen semantic inputs; native model formatting, no extra history selected | Same inputs; native reasoning-low instruction |',
          '| Tokenizers | Full SDK/local vocabulary and selected-sequence decode checks; unchanged vocabulary | Same checks |',
          '| Checkpoints | Separate sampler and full optimizer checkpoints, one-hour TTL | None |','',
          '## Checkpoint diagnostic — do not silently treat it as passed','',
          'The 35B tests separately measure saved/restored likelihood agreement and next-update continuation. Matching probe likelihoods do not prove that every weight is identical. Qwen3.6 fails next-update equivalence on both old and new probes; Qwen3.5 Base passes old and fails new. Three repeated no-update forwards on each original trainer agree exactly, so ordinary observed forward variability does not explain away the mismatch. These gates remain failed even when memorization passes.',
          'For Qwen3.6, an additional longer sampler probe also agrees exactly before and after reload. An explicitly seeded restore still does not reproduce the continuous training branch. The same-client control was rejected by the provider with HTTP 400 (`LoadWeights is not permitted with seq_id 8`); no post-load result is claimed. The cause remains unresolved. Fresh-adapter overfit results do not depend on those diagnostic branches.',
          '', '## Curves','', '![Memorization curves](memorization-curves.png)','',
          'The left panel averages training loss over each pass. The right evaluates the same known targets at saved checkpoints. Neither measures generalization.','']
    quick=['## Three-update old/new mechanical probes','',
           'Each arm uses a separate fresh adapter, trains three examples once, then tests the same five probes. Two probes were not trained in that arm; two examples are far too few to estimate generalization. The after-update results use the restored branch, whose equivalence failures are reported above.','',
           '| Model | Pipeline | Trained three: NLL before → after | Other two: NLL before → after | After-update length-capped generations |',
           '|---|---|---:|---:|---:|']
    for key in ordered[:3]:
        arms=oldrun['arms'] if key=='qwen38_off' else models[key]['originalChecks']
        for arm,data in arms.items():
            transitions=[]
            for trained in (True,False):
                entries=[v for eid,v in data['scores'].items() if (eid in ref['arms'][arm]['trainIDs'])==trained]
                sides=[[v.get('base',v)['nll'] for v in entries],[v['after']['nll'] for v in entries]]
                means=[sum(v['weightedNLLSum'] for v in side)/sum(v['lossBearingTokens'] for v in side) for side in sides]
                transitions.append(f'{means[0]:.3f} → {means[1]:.3f}')
            capped=sum(v['after']['generation']['stopReason']=='length' for v in data['scores'].values())
            quick.append(f"| {LABELS[key]} | {arm} | {transitions[0]} | {transitions[1]} | {capped}/5 |")
    text+=quick+['','All native-format, loss-shift and token-identity checks passed. Successful loss reduction does not imply that every post-update generation is useful. Detailed probe outputs and returned likelihoods remain in `preflight.json` (original 27B) and `run.json` (additional models).','']
    text+=['## Review the trained memorization outputs','',
           'These are the same ten examples used for repeated training. Exact-match labels below describe string reproduction, not an independent future-prediction score.','']
    cases={c['exampleID']:c for c in ref['additionalTests']['cases']}
    for eid in ref['additionalTests']['overfitIDs']:
        c=cases[eid]
        text += [f"### Trained case {c['cohortNumber']} · {c['application']}",'','Human target:','','```text',c['targetText'],'```','']
        for key in ordered[:3]:
            last=models[key]['overfit']['snapshots'][-1];v=last['scores'][eid]
            text += [f"**{LABELS[key]}** · epoch {last['epoch']} · {'normalized exact' if v['normalizedExactMatch'] else 'not exact'}",'',
                     '```text',v['generation']['prediction'],'```','']
    text += ['## Review every frozen answer','', 'All four seeds are shown, including failures and empty final answers. Raw reasoning remains in the operational evidence, not the scored completion.','']
    newer={key:{g['cohortNumber']:g for g in entries} for key,entries in grades['models'].items()}
    older={g['cohortNumber']:g for g in oldgrades['cases']}
    for c in ref['additionalTests']['cases']:
        n=c['cohortNumber'];text += [f"### Case {n} · {c['application']}",'','Human target:','', '```text',c['targetText'],'```','']
        for key in ordered:
            g=older[n] if key=='qwen38_off' else newer[key][n]
            text += [f"#### {LABELS[key]}",'',g['reason'],'']
            answers=models[key]['capability']['scores'][c['exampleID']]
            for passed,answer in zip(g['passBySeed'],answers):
                text += [f"Seed {answer['seed']} · {'PASS' if passed else 'FAIL'} · {answer['latencySeconds']:.2f}s",'',
                         '```text',answer['prediction'] or '[No final answer produced]','```','']
    with (d/'REVIEW.md').open('x') as f:f.write('\n'.join(text))
    print('\n'.join(table))


if __name__=='__main__':main()
