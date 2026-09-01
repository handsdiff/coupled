#!/usr/bin/env python3
"""Serve the local Phase 1 WRITE identity review UI."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HTML = r'''<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coupled · WRITE identity review</title>
<style>
:root{color-scheme:light dark;font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:Canvas;color:CanvasText}*{box-sizing:border-box}body{margin:0}.shell{display:grid;grid-template-columns:310px minmax(0,1fr);min-height:100vh}.side{border-right:1px solid color-mix(in srgb,CanvasText 18%,transparent);padding:18px;position:sticky;top:0;height:100vh;overflow:auto}.main{padding:24px;min-width:0}.muted{opacity:.65}.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:14px 0}.stat{padding:10px;background:color-mix(in srgb,CanvasText 6%,Canvas);border-radius:8px}.stat b{display:block;font-size:20px}.filters{display:grid;gap:8px;margin:14px 0}select,input,button{font:inherit;padding:8px;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);border-radius:7px;background:Canvas;color:CanvasText}.items{display:grid;gap:5px}.item{width:100%;text-align:left;background:transparent}.item.active{background:color-mix(in srgb,Highlight 18%,Canvas);border-color:Highlight}.item small{display:block;opacity:.65;margin-top:3px}.top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.top h1{font-size:22px;margin:0 auto 0 0}.badge{display:inline-block;padding:3px 7px;border-radius:999px;background:color-mix(in srgb,CanvasText 9%,Canvas);font-size:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}.panel{border:1px solid color-mix(in srgb,CanvasText 16%,transparent);border-radius:10px;padding:14px;min-width:0}.panel h2{font-size:15px;margin:0 0 10px}.wide{grid-column:1/-1}pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:color-mix(in srgb,CanvasText 5%,Canvas);padding:12px;border-radius:7px}.target{font-size:17px;white-space:pre-wrap;overflow-wrap:anywhere}.key{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}.review-actions{margin-left:auto}.ok{color:#16834a}.warn{color:#b86a00}.empty{padding:60px;text-align:center;opacity:.65}@media(max-width:800px){.shell{display:block}.side{position:static;height:auto;border-right:0;border-bottom:1px solid color-mix(in srgb,CanvasText 18%,transparent)}.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style>
<div class="shell"><aside class="side"><strong>WRITE identity review</strong><div id="meta" class="muted"></div><div class="stats" id="stats"></div><div class="filters"><select id="scope"><option value="examples">Episode-level model input</option><option value="terminal">Every VS Code terminal WRITE</option></select><select id="app"><option value="">All applications</option></select><select id="mode"><option value="">All modes</option></select><input id="search" placeholder="Search target or destination"></div><div class="items" id="items"></div><details><summary>Raw vs normalized identity disagreements</summary><pre id="boundaries"></pre></details></aside><main class="main" id="main"></main></div>
<script>
const state={data:null,rows:[],index:0}; const $=id=>document.getElementById(id); const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function pretty(v){return JSON.stringify(v,null,2)}
function activeRows(){return $('scope').value==='terminal'?state.data.terminalWrites:state.data.examples}
function rowTarget(r){return r.target??r.targetValidationOnly??''}
function apply(){const a=$('app').value,m=$('mode').value,q=$('search').value.toLowerCase();state.rows=activeRows().filter(r=>{const d=r.modelFacingDestination||{};return(!a||d.application===a)&&(!m||d.interactionMode===m)&&(!q||(rowTarget(r)+' '+pretty(d)).toLowerCase().includes(q))});state.index=Math.min(state.index,Math.max(0,state.rows.length-1));drawList();draw()}
function drawList(){$('items').innerHTML=state.rows.map((r,i)=>{const d=r.modelFacingDestination||{};return`<button class="item ${i===state.index?'active':''}" data-i="${i}"><b>${r.reviewOrdinal}. ${esc(d.application)} · ${esc(d.surfaceKind)}</b><small>${esc(d.interactionMode||d.surfaceLabel||d.resourceTitle||'')}</small></button>`}).join('');document.querySelectorAll('.item').forEach(b=>b.onclick=()=>{state.index=+b.dataset.i;drawList();draw()})}
function draw(){const r=state.rows[state.index];if(!r){$('main').innerHTML='<div class="empty">No matching examples</div>';return}const d=r.modelFacingDestination||{},terminal=!!r.sourceEventID;const identity=r.exampleID??r.sourceEventID;const saved=localStorage.getItem('write-review:'+identity)||'';const title=terminal?`Terminal WRITE ${r.reviewOrdinal}`:`Example ${r.reviewOrdinal} <span class="muted">· corpus #${r.chronologicalOrdinal+1}</span>`;const episode=terminal?'':`<section class="panel"><h2>Episode lineage</h2><pre>${esc(pretty(r.episode))}</pre></section><section class="panel"><h2>Packing</h2><pre>${esc(pretty({modelInputTokenCount:r.modelInputTokenCount,modelInputTokenCountBeforePacking:r.modelInputTokenCountBeforePacking,droppedContextEventCount:r.droppedContextEventCount,semanticModelInputSHA256:r.semanticModelInputSHA256}))}</pre></section><section class="panel wide"><h2>Actual episode-level semantic input sent to tokenization</h2><pre>${esc(r.exactSemanticModelInput)}</pre></section>`;$('main').innerHTML=`<div class="top"><h1>${title}</h1><span class="badge">${esc(d.application)}</span><span class="badge">${esc(d.surfaceKind)}</span><span class="badge ${d.interactionMode==='unknown'?'warn':'ok'}">${esc(d.interactionMode||'no mode')}</span><div class="review-actions"><select id="judgment"><option value="">Unreviewed</option><option value="correct">Correct</option><option value="flag">Flag</option></select></div></div><div class="grid"><section class="panel"><h2>Before</h2><pre>${esc(pretty(r.rawDestination))}</pre></section><section class="panel"><h2>After</h2><pre>${esc(pretty(d))}</pre><p class="key">${esc(r.logicalDestinationKey)}</p></section><section class="panel"><h2>Classification</h2><pre>${esc(pretty(r.classification))}</pre></section><section class="panel wide"><h2>${terminal?'Micro-WRITE content · validation only':'Closed substantive target · validation only'}</h2><div class="target">${esc(rowTarget(r))}</div></section>${episode}</div>`;const j=$('judgment');j.value=saved;j.onchange=()=>localStorage.setItem('write-review:'+identity,j.value)}
fetch('/review.json').then(r=>r.json()).then(data=>{state.data=data;const mappings=Object.entries(data.configuredTerminalAgentProgramMappings||{}).map(([title,agent])=>`${title}→${agent}`).join(', ');$('meta').textContent=`${data.episodeVersion} · ${data.normalizerVersion}${mappings?' · '+mappings:''}`;$('stats').innerHTML=`<div class="stat"><b>${data.counts.reviewExamples}</b><span>episode cases</span></div><div class="stat"><b>${data.counts.terminalWriteAudit}</b><span>terminal writes</span></div><div class="stat"><b>${data.counts.changedIdentityBoundaries}</b><span>identity disagreements</span></div>`;$('boundaries').textContent=pretty(data.changedIdentityBoundaries);const all=[...data.examples,...data.terminalWrites];const apps=[...new Set(all.map(r=>r.modelFacingDestination?.application).filter(Boolean))];apps.forEach(v=>$('app').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));const modes=[...new Set(all.map(r=>r.modelFacingDestination?.interactionMode).filter(Boolean))];modes.forEach(v=>$('mode').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));$('scope').onchange=$('app').onchange=$('mode').onchange=$('search').oninput=()=>{state.index=0;apply()};apply()}).catch(e=>$('main').textContent=e)
</script>'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    review = args.review.resolve()
    payload = review.read_bytes()
    json.loads(payload)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/review.json":
                body, content_type = payload, "application/json; charset=utf-8"
            elif self.path in {"/", "/index.html"}:
                body, content_type = HTML.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *values: object) -> None:
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"WRITE identity review: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
