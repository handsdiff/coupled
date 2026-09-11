'use strict';
const variants=['cleaned_recent','raw_ocr_recent','screenshots_recent'];
const names={'cleaned_recent':'Cleaned READs','raw_ocr_recent':'Full-window OCR','screenshots_recent':'Screenshots'};
const $=id=>document.getElementById(id);
let all=[],visible=[],current=null,frame=0,loadID=0,contextID=0;
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
async function get(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error(`Could not load data (${r.status}).`);return r.json();}
function error(e){$('error').textContent=e.message;}
function filter(){
 const mode=$('filter').value,q=$('search').value.trim().toLowerCase();
 visible=all.filter(c=>{const [a,b,d]=variants.map(v=>c.passes[v]);return (!q||String(c.case)===q||c.target.toLowerCase().includes(q))&&({all:true,differ:a!==b||a!==d,vision_gain:!a&&d,vision_loss:a&&!d,ocr_gain:!a&&b,all_pass:a&&b&&d,all_fail:!a&&!b&&!d}[mode]);});
 $('cases').replaceChildren(...visible.map(c=>{const o=el('option',`#${c.case} · ${c.application} · ${c.target.slice(0,65)}`);o.value=c.case;return o;}));
 if(!visible.length){$('case').hidden=true;$('position').textContent='No matching cases';$('prev').disabled=$('next').disabled=true;return;}
 const selected=visible.some(c=>c.case===current?.case)?current.case:visible[0].case;
 $('cases').value=selected;show(selected).catch(error);
}
async function show(n){
 const id=++loadID;++contextID;$('error').textContent='';$('context').hidden=true;$('case').hidden=true;
 const c=await get(`/api/case/${n}`);if(id!==loadID)return;current=c;frame=0;$('case').hidden=false;
 history.replaceState(null,'',`#${c.case}`);$('heading').textContent=`Case ${c.case}`;
 $('meta').textContent=`${c.query.destination.application} · ${c.query.destination.resourceTitle||c.query.destination.surfaceKind||''} · ${c.beganAt}`;
 $('target').textContent=c.target;$('query').textContent=JSON.stringify(c.query,null,2);
 $('answers').replaceChildren(...variants.map(v=>{const r=c.answers[v],card=el('article',undefined,'answer'),head=el('div',undefined,'sectionhead');head.append(el('h3',names[v]),el('span',r.semanticPass?'PASS':'FAIL',`grade ${r.semanticPass?'pass':'fail'}`));card.append(head,el('pre',r.prediction),el('p',r.reason,'reason'),el('p',`${r.timing.dispatchToCompletionSeconds.toFixed(1)}s · $${r.apiEquivalentCostUSD.toFixed(3)} API-equivalent · ${r.inputTokens.toLocaleString()} input tokens`,'metrics'));const b=el('button','Inspect exact supplied context');b.addEventListener('click',()=>context(c.case,v).catch(error));card.append(b);return card;}));
 $('frame-title').textContent=`${c.frames.length} original screenshots · all captured before this WRITE`;
 $('interval').textContent=`Recent interval: ${c.interval.startAt} → ${c.interval.latestFrameAt}. ${c.interval.sizeLimitShortenedInterval?'Shortened by request-size limits.':'No request-size shortening.'} Older history remains cleaned. Full-window OCR used these same frames.`;
 $('frame-index').max=Math.max(0,c.frames.length-1);$('frame-index').value=0;$('frame-image').removeAttribute('src');
 if($('screenshots').open)showFrame();
 const pos=visible.findIndex(x=>x.case===c.case);$('position').textContent=`${pos+1} of ${visible.length}`;$('prev').disabled=pos<=0;$('next').disabled=pos>=visible.length-1;
}
async function context(n,v){const id=++contextID;const p=await get(`/api/context/${n}/${v}`);if(id!==contextID||current.case!==n)return;$('context-title').textContent=`${names[v]} · exact supplied context`;$('context-text').textContent=p.parts.map(t=>t.type==='image'?`\n[ORIGINAL SCREENSHOT ${t.imageIndex+1} — see frame viewer below]\n`:t.text).join('\n\n');$('context').hidden=false;$('context').scrollIntoView({behavior:'smooth',block:'start'});}
function showFrame(){if(!current?.frames.length)return;frame=Math.max(0,Math.min(frame,current.frames.length-1));const f=current.frames[frame];$('frame-index').value=frame;$('frame-meta').textContent=`${frame+1} / ${current.frames.length} · ${f.capturedAt||''} · ${f.source?.application||''}`;$('frame-image').src=f.url;$('frame-full').href=f.url;$('frame-prev').disabled=frame===0;$('frame-next').disabled=frame===current.frames.length-1;}
function move(step){const i=visible.findIndex(c=>c.case===current?.case)+step;if(i>=0&&i<visible.length){$('cases').value=visible[i].case;show(visible[i].case).catch(error);}}
$('filter').addEventListener('change',filter);$('search').addEventListener('input',filter);$('cases').addEventListener('change',()=>show(Number($('cases').value)).catch(error));$('prev').onclick=()=>move(-1);$('next').onclick=()=>move(1);$('close-context').onclick=()=>{$('context').hidden=true;++contextID;};$('screenshots').addEventListener('toggle',()=>{if($('screenshots').open)showFrame();});$('frame-prev').onclick=()=>{frame--;showFrame();};$('frame-next').onclick=()=>{frame++;showFrame();};$('frame-index').oninput=()=>{frame=Number($('frame-index').value);showFrame();};
get('/api/overview').then(d=>{all=d.cases;$('summary').replaceChildren(...variants.map(v=>{const a=d.summary.arms[v],s=el('article',undefined,'stat');s.append(el('h3',names[v]),el('strong',`${a.passes} / 69 · ${(100*a.passRate).toFixed(1)}%`),el('p',`Latency: ${a.latencySeconds.median.toFixed(1)}s median / ${a.latencySeconds.mean.toFixed(1)}s mean`),el('p',`API-equivalent: $${a.apiEquivalentUSD.mean.toFixed(3)} per query · $${a.apiEquivalentUSD.total.toFixed(2)} total`));return s;}));$('authority').textContent=`${d.summary.authority} Cost figures are usage-based API equivalents, not subscription charges.`;const fromHash=Number(location.hash.slice(1));if(all.some(c=>c.case===fromHash))current={case:fromHash};filter();}).catch(error);
