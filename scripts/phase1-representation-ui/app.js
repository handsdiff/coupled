'use strict';
const $ = x => document.getElementById(x);
let index, cases, selected, mode = 'time', generation = 0, median = false;
const names = {cleaned:'Cleaned READs',ocr:'Full-window OCR',images:'Full-window screenshots'};
const fmt = x => Number(x).toLocaleString(undefined,{maximumFractionDigits:0});
const stamp = x => x ? new Date(x).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : 'Time in source record';
function el(tag, text, cls) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
async function get(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error(`${r.status}: ${await r.text()}`);return r.json();}
function parse(s){try{return JSON.parse(s);}catch{return null;}}
function eventInfo(item,p){const d=parse(p.parts[item.partStart]?.text||'')||{};return {kind:d.kind||item.kind,time:item.at||d.capturedAt||d.availableAt||d.timestamp,app:d.source?.application||d.destination?.application||'',title:d.source?.resourceTitle||''};}
function textForPart(part){return part.type==='input_text'?part.text:`[FULL-WINDOW IMAGE: ${part.sha256}; detail=${part.detail}]`;}
function readable(part){
 const d=parse(part.text);if(!d)return part.text;
 if(typeof d.content==='string')return d.content;
 if(d.privacy)return '[Sensitive observation redacted]';
 if(d.kind==='read_observation')return `${d.source?.application||''}\n${d.source?.resourceTitle||''}`.trim();
 return JSON.stringify(d,null,2);
}
function renderPanel(p){
 const panel=el('article',undefined,'panel'),head=el('div',undefined,'panel-head');panel.append(head);
 head.append(el('h2',names[p.arm.split('_')[1]]));
 const reused=!!p.reuse,blocked=p.limits.length>0;
 head.append(el('span',reused?'Already sampled — reuse exact result':blocked?'Prepared, but blocked by size limits':p.execution?'Sampled — result saved':'Prepared — not sampled','badge '+(blocked?'blocked':reused||p.execution?'':'new')));
 const stats=el('div',undefined,'stats');
 stats.append(el('div',`${p.tokenAccounting.startsWith('previous_')?'Recorded':'Estimated'} input: ${fmt(p.estimatedInputTokens)} tokens`));
 stats.append(el('div',`History starts ${stamp(p.intervalStartAt)}`));
 stats.append(el('div',`Ends before ${stamp(p.cutoffExclusive)} · ${p.historySpanMinutes.toFixed(1)} min`));
 stats.append(el('div',`${fmt(p.historyItems)} records · ${fmt(p.historicalWriteCount)} prior WRITEs · ${fmt(p.frameCount||p.cleanedReadCount)} ${p.arm.endsWith('cleaned')?'cleaned READs':'raw observations'}`));
 if(p.imageCount) stats.append(el('div',`${fmt(p.imageCount)} full-window images · ${(p.wirePayloadBytes/1024/1024).toFixed(1)} MiB request estimate`));
 if(p.privacyRedactedFrames)stats.append(el('div',`${p.privacyRedactedFrames} sensitive observations replaced with a redaction notice`));
 if(blocked)stats.append(el('div',p.limits.join('; ').replaceAll('_',' ')+'. Full interval is still shown; nothing was silently removed.','limits'));
 head.append(stats);
 if(p.packing?.allAvailableHistoryUsed)stats.append(el('div','All available earlier history is included. There is not enough history to fill the shared budget. No padding.','unmeasured'));
 if(p.execution){
   head.append(el('div',`Saved result · ${fmt(p.execution.usage.input_tokens)} actual input tokens · ${p.execution.timing.dispatchToCompletionSeconds.toFixed(1)} s`,'badge'));
   const grade=p.execution.grade;
   if(grade)head.append(el('div',`${grade.decision.toUpperCase()} · ${grade.reason}`,'grade '+grade.decision));
   const d=el('details');d.open=!!grade;d.append(el('summary',grade?`Model completion — ${grade.decision}`:'Model completion — not yet holistically graded'),el('pre',p.execution.prediction));head.append(d);
 }
 const link=el('a','Open exact input parts (JSON)','input-link');link.href=`/api/input/${p.case}/${p.arm}`;link.target='_blank';link.rel='noopener';head.append(link);
 const controls=el('div',undefined,'event-controls'),select=el('select',undefined,'event-select');select.setAttribute('aria-label',`${names[p.arm.split('_')[1]]} history record`);
 p.items.forEach((item,i)=>{const x=eventInfo(item,p);const opt=el('option',`${i+1}/${p.items.length} · ${stamp(x.time)} · ${x.kind} ${x.app}`);opt.value=i;select.append(opt);});
 const nav=el('div',undefined,'event-nav'),prev=el('button','← Earlier'),next=el('button','Later →'),first=el('button','First'),last=el('button','Last');nav.append(prev,next,first,last);controls.append(select,nav);panel.append(controls);
 const evidence=el('div',undefined,'evidence');panel.append(evidence);
 function show(i){
   select.value=i;evidence.replaceChildren();const item=p.items[i];if(!item){evidence.append(el('p','No prior history records.'));return;}
   const info=eventInfo(item,p);evidence.append(el('div',`${i+1} of ${p.items.length} · ${stamp(info.time)} · ${info.kind} ${info.app} ${info.title}`,'event-caption'));
   for(const part of p.parts.slice(item.partStart,item.partEnd)){
     if(part.type==='input_text')evidence.append(el('pre',readable(part)));
     else {const a=el('a');a.href=`/image/${part.sha256}`;a.target='_blank';a.rel='noopener';const img=el('img');img.src=a.href;img.alt='Full screenshot supplied at this point in the context';a.append(img);evidence.append(a);evidence.append(el('p','Click image to inspect at original resolution.','event-caption'));}
   }
   prev.disabled=i===0;next.disabled=i===p.items.length-1;evidence.scrollTop=0;
 }
 select.onchange=()=>show(+select.value);prev.onclick=()=>show(Math.max(0,+select.value-1));next.onclick=()=>show(Math.min(p.items.length-1,+select.value+1));first.onclick=()=>show(0);last.onclick=()=>show(p.items.length-1);
 const details=el('details',undefined,'full-input');details.append(el('summary','Inspect the complete input in chronological order'));const all=el('pre');details.append(all);details.ontoggle=()=>{all.textContent=details.open?p.parts.map(textForPart).join('\n'):'';};panel.append(details);
 show(Math.max(0,p.items.length-1));return panel;
}
function renderScoring(){
 const container=$('scoring-summary'),s=index.scoring;container.replaceChildren();
 if(!s){container.append(el('p','Holistic scoring pending. Saved predictions are not yet final grades.'));return;}
 container.append(el('h2','Holistic results'),el('p','Pass means the prediction captures the intended thought, including useful compatible elaboration. Latency and construction quality are assessed separately.'));
 const table=el('table'),head=el('tr');for(const x of ['Condition','Passes','Accuracy','Median latency','Mean latency','Mean input tokens','Mean API-equivalent cost'])head.append(el('th',x));table.append(head);
 for(const a of ['time_cleaned','time_ocr','time_images','budget_cleaned','budget_ocr']){
  const r=s.arms[a],tr=el('tr');for(const x of [r.label,`${r.passes}/${r.n}`,`${(100*r.passRate).toFixed(1)}%`,`${r.latencySeconds.median.toFixed(1)} s`,`${r.latencySeconds.mean.toFixed(1)} s`,fmt(r.inputTokens.mean),`$${r.apiEquivalentUSD.mean.toFixed(2)}`])tr.append(el('td',x));table.append(tr);
 }container.append(table,el('p','Screenshots: 68 available cases; case 41 is unscored because its intact input exceeds capacity. Other conditions: 69 cases. Paired comparisons below use common cases only.'));
 const details=el('details');details.append(el('summary','Paired improvements and regressions'));
 for(const [key,p] of Object.entries(s.paired)){
  const [a,b]=key.split('__');details.append(el('p',`${s.arms[a].label} → ${s.arms[b].label}: ${p.gains.length} gains, ${p.losses.length} losses, net ${p.netPassDifference>=0?'+':''}${p.netPassDifference} (${p.n} shared cases).`));
 }container.append(details);
}
async function render(){
 const ticket=++generation;$('error').textContent='';$('panels').replaceChildren(el('p','Loading exact prepared inputs…'));
 $('case').value=selected;$('mode').value=mode;history.replaceState(null,'',`?case=${selected}&mode=${mode}`);
 try{
 const arms=median && mode==='budget'?['budget_cleaned','budget_ocr','time_images']:['cleaned','ocr','images'].map(a=>`${mode}_${a}`);
 const ps=await Promise.all(arms.map(a=>get(`/api/payload/${selected}/${a}`)));if(ticket!==generation)return;
 $('instruction').textContent=ps[0].parts[0].text;$('query').textContent=ps[0].query;$('target').textContent=ps[0].target;$('case-meta').textContent=ps[0].application;
 $('scope').textContent=mode==='budget'?`Expanded cleaned and OCR each use the same ${fmt(index.plan.sharedBudgetInputTokens)}-token ceiling, derived from the median complete screenshot input across all 69 cases. The screenshot panel reuses the same time-matched condition; it is NOT a new screenshot run and its size varies by case.`:mode==='token'?'Historical preparation: per-example budgets from the earlier screenshot experiment. Superseded by the shared-median design.':'Same time interval: original cleaned 32K baseline plus full-window OCR and screenshots throughout its complete historical interval. Exactly the same historical WRITEs. No five-minute cutoff, cleaned background, or frame subsampling.';
 $('case-boundary').textContent=mode==='budget'?`One shared budget for every example: ${fmt(index.plan.sharedBudgetInputTokens)} input tokens. Different representations can cover different amounts of earlier history.`:mode==='token'?`Historical per-example ceiling: ${fmt(ps[0].assignedInputTokens)} tokens.`:`Shared interval: ${stamp(ps[0].fixed32KStartAt)} → before ${stamp(ps[0].cutoffExclusive)}. “32K” names the frozen reference-tokenizer budget, not exactly 32K Astra tokens.`;
 $('panels').replaceChildren(...ps.map(renderPanel));$('previous').disabled=cases.indexOf(selected)===0;$('next').disabled=cases.indexOf(selected)===cases.length-1;
 }catch(e){$('error').textContent=e.message;}
}
async function start(){
 index=await get('/api/index');median=index.plan.version==='phase1-median-context-controls-v1';cases=[...new Set(index.rows.map(x=>x.case))].sort((a,b)=>a-b);const params=new URLSearchParams(location.search);selected=cases.includes(+params.get('case'))?+params.get('case'):cases[0];mode=median?(params.get('mode')==='budget'?'budget':'time'):(params.get('mode')==='time'?'time':'token');
 for(const [value,label] of median?[['time','Same historical interval'],['budget','Shared median token budget']]:[['token','Historical per-example budget'],['time','Same historical interval']]){const o=el('option',label);o.value=value;$('mode').append(o);}
 for(const c of cases){const r=index.rows.find(x=>x.case===c);const o=el('option',`#${c} · ${r.application}`);o.value=c;$('case').append(o);}
 $('intro').textContent=median?'Original 32K cleaned baseline plus four additional conditions: full-interval OCR, full-interval screenshots, expanded cleaned, and expanded OCR. This read-only page shows five distinct inputs.':'Historical prepared-input comparison.';
 function totals(){const p=index.progress;$('totals').textContent=median?`${cases.length} cases · 69 original baseline results reused · ${index.summary.reusedAdditionalRequests} additional exact reuses · ${index.summary.feasibleNewRequests} new requests · case 41 screenshots capacity-blocked.${p?` Saved new results: ${p.newCompleted}/${p.newPlanned}.`:' Execution has not started.'}${p?.executionPause?` Paused: ${p.executionPause.reason}`:''}`:`${cases.length} examples in historical preparation.`;}
 totals();renderScoring();setInterval(async()=>{try{const hadScores=!!index.scoring;index=await get('/api/index');totals();renderScoring();if(!hadScores&&index.scoring)render();}catch(e){$('error').textContent=e.message;}},30000);
 $('case').onchange=()=>{selected=+$('case').value;render();};$('mode').onchange=()=>{mode=$('mode').value;render();};$('previous').onclick=()=>{selected=cases[cases.indexOf(selected)-1];render();};$('next').onclick=()=>{selected=cases[cases.indexOf(selected)+1];render();};await render();
}
start().catch(e=>$('error').textContent=e.message);
