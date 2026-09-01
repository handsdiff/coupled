#!/usr/bin/env python3
"""Serve the read-only Phase 1 READ-surface-v2 rectangle review."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ReviewError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


class ReviewStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest = load_json(self.directory / "review.json")
        if self.manifest.get("schemaVersion") != 2:
            raise ReviewError("review artifact does not contain the v1/v2 OCR comparison")
        if self.manifest.get("status") != "shadow_review_only_not_training_authority":
            raise ReviewError("artifact is not explicitly shadow-only")
        observations_path = self.directory / "observations.jsonl"
        expected = self.manifest.get("artifactDigestsSHA256", {}).get("observations.jsonl")
        if not observations_path.is_file() or sha256(observations_path) != expected:
            raise ReviewError("observations digest differs")
        jobs_path = self.directory / "ocr-jobs.jsonl"
        expected_jobs = self.manifest.get("artifactDigestsSHA256", {}).get("ocr-jobs.jsonl")
        if not jobs_path.is_file() or sha256(jobs_path) != expected_jobs:
            raise ReviewError("OCR jobs digest differs")
        source = self.manifest.get("source", {})
        raw_path = Path(str(source.get("sessionDirectory"))) / "raw.jsonl"
        if not raw_path.is_file() or sha256(raw_path) != source.get("rawJSONLSHA256"):
            raise ReviewError("source raw journal differs")
        self.rows = load_jsonl(observations_path)
        self.by_id = {}
        for row in self.rows:
            record_id = row.get("recordID")
            if not isinstance(record_id, str) or not record_id or record_id in self.by_id:
                raise ReviewError(f"invalid or duplicate record ID: {record_id!r}")
            screenshot = Path(str(row.get("screenshotPath")))
            if not screenshot.is_file() or sha256(screenshot) != row.get("screenshotSHA256"):
                raise ReviewError(f"screenshot digest differs: {screenshot}")
            comparison = row.get("ocrComparison")
            if not isinstance(comparison, dict) or not all(
                isinstance(comparison.get(variant, {}).get("content"), str)
                for variant in ("v1", "v2")
            ):
                raise ReviewError(f"observation lacks v1/v2 OCR: {record_id}")
            self.by_id[record_id] = row

    def summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "ordinal": row["ordinal"],
                "recordID": row["recordID"],
                "application": row["application"],
                "windowTitle": row["windowTitle"],
                "capturedAt": row["capturedAt"],
                "triggerTypes": row["triggerTypes"],
                "proposal": row["selection"]["proposal"],
            }
            for row in self.rows
        ]


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>READ Surface v2 Review</title>
<style>
:root{color-scheme:dark;--bg:#090b0e;--panel:#11151a;--panel2:#171c22;--line:#2a333d;--text:#edf2f7;--muted:#96a3b2;--v1:#fb7185;--v2:#4ade80;--ax:#60a5fa;--pointer:#facc15}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}header{height:62px;padding:10px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:20px;background:#090b0ef2;position:sticky;top:0;z-index:20}h1,h2,h3{font-family:ui-sans-serif,system-ui,sans-serif;margin:0}h1{font-size:17px}h2{font-size:16px}h3{font-size:13px}.muted{color:var(--muted)}.layout{display:grid;grid-template-columns:310px minmax(0,1fr);min-height:calc(100vh - 62px)}nav{border-right:1px solid var(--line);height:calc(100vh - 62px);overflow:auto;position:sticky;top:62px;padding:8px}.item{display:block;width:100%;padding:9px 10px;margin:2px 0;border:1px solid transparent;border-radius:7px;text-align:left;color:var(--text);background:transparent;cursor:pointer}.item:hover,.item.active{background:var(--panel2);border-color:#3b4652}.item .line{display:flex;justify-content:space-between;gap:8px}.item small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.method-v1_fallback{color:var(--v1)}.method-ax_semantic_container{color:var(--v2)}.method-ax_repeated_vertical_pane{color:#a7f3d0}.main{padding:14px;min-width:0}.toolbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.toolbar button,.toolbar label{color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:6px 9px}.toolbar button{cursor:pointer}.toolbar input{accent-color:var(--ax)}.summary{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px}.tag{border:1px solid var(--line);border-radius:99px;padding:3px 7px;color:var(--muted)}.stage{position:relative;width:100%;line-height:0;background:#050607;border:1px solid var(--line);overflow:hidden}.stage img{display:block;width:100%;height:auto}.overlay{position:absolute;pointer-events:none}.overlay span{position:absolute;top:-17px;left:-1px;padding:1px 4px;font:10px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap;background:#050607dd}.ancestor{border:1px solid color-mix(in srgb,var(--ax) 72%,transparent);background:color-mix(in srgb,var(--ax) 5%,transparent)}.ancestor span{color:var(--ax)}.v1{border:3px dashed var(--v1);z-index:8}.v1 span{color:var(--v1)}.v2{border:3px solid var(--v2);z-index:9}.v2 span{color:var(--v2)}.pointer{position:absolute;width:12px;height:12px;border:2px solid #111;border-radius:50%;background:var(--pointer);transform:translate(-50%,-50%);z-index:12;pointer-events:none;box-shadow:0 0 0 2px var(--pointer)}.details{display:grid;grid-template-columns:minmax(280px,.75fr) minmax(420px,1.25fr);gap:12px;margin-top:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden}.panelhead{padding:10px 12px;border-bottom:1px solid var(--line)}.panelbody{padding:10px 12px}.proposal{font-size:15px;margin-bottom:6px}.reason{color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:11px}th,td{text-align:left;border-bottom:1px solid #222a32;padding:6px 5px;vertical-align:top}th{color:var(--muted);position:sticky;top:0;background:var(--panel)}tr.selected{background:#15301f}tr:hover{background:#17202a}.scroll{max-height:360px;overflow:auto}.legend{display:flex;gap:14px;color:var(--muted);font-size:11px}.swatch{display:inline-block;width:15px;height:9px;margin-right:5px;vertical-align:middle}.swatch.v1s{border:2px dashed var(--v1)}.swatch.v2s{border:2px solid var(--v2)}.swatch.axs{border:1px solid var(--ax)}.ocr-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.ocr-panel{min-width:0}.ocr-panel.v1-ocr{border-top:3px solid var(--v1)}.ocr-panel.v2-ocr{border-top:3px solid var(--v2)}.ocr-meta{font-size:11px;color:var(--muted);margin-top:3px}.ocr-text{margin:0;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;max-height:420px;min-height:160px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text);background:#0b0e12}.warn{color:#fbbf24}.ok{color:#86efac}@media(max-width:1000px){.layout{grid-template-columns:1fr}nav{position:relative;top:0;height:auto;max-height:240px;border-right:0;border-bottom:1px}.details,.ocr-grid{grid-template-columns:1fr}}
</style></head><body>
<header><div><h1>READ surface v2 shadow review</h1><div class="muted">v1/v2 OCR · recorded AX ancestry · conservative v2 proposal</div></div><div class="toolbar"><button id="prev">← Previous</button><button id="next">Next →</button><label><input id="toggle-v1" type="checkbox" checked> v1</label><label><input id="toggle-ax" type="checkbox" checked> all AX</label><label><input id="toggle-v2" type="checkbox" checked> proposed v2</label></div></header>
<div class="layout"><nav id="nav"></nav><main class="main"><div id="summary" class="summary"></div><div class="legend"><span><i class="swatch v1s"></i>v1 pointer crop</span><span><i class="swatch axs"></i>AX ancestor</span><span><i class="swatch v2s"></i>proposed v2</span></div><div id="stage" class="stage"></div><div class="ocr-grid"><section class="panel ocr-panel v1-ocr"><div class="panelhead"><h2>v1 OCR · recorded pointer crop</h2><div id="v1-meta" class="ocr-meta"></div></div><pre id="v1-ocr" class="ocr-text"></pre></section><section class="panel ocr-panel v2-ocr"><div class="panelhead"><h2>v2 OCR · proposed AX rectangle</h2><div id="v2-meta" class="ocr-meta"></div></div><pre id="v2-ocr" class="ocr-text"></pre></section></div><div class="details"><section class="panel"><div class="panelhead"><h2>Proposal</h2></div><div class="panelbody" id="proposal"></div></section><section class="panel"><div class="panelhead"><h2>Recorded AX ancestor path</h2></div><div class="scroll"><table><thead><tr><th>Depth</th><th>Role / subrole</th><th>Label</th><th>Area</th><th>Repeated</th></tr></thead><tbody id="ancestors"></tbody></table></div></section></div></main></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let rows=[],index=0,current=null;
const pct=v=>(100*Number(v)).toFixed(4)+'%';
function overlay(rect,kind,label,extra=''){if(!rect)return'';return `<div class="overlay ${kind}" ${extra} style="left:${pct(rect.x)};top:${pct(rect.y)};width:${pct(rect.width)};height:${pct(rect.height)}"><span>${esc(label)}</span></div>`}
function renderNav(){document.getElementById('nav').innerHTML=rows.map((x,i)=>`<button class="item ${i===index?'active':''}" data-index="${i}"><div class="line"><b>${x.ordinal}. ${esc(x.application)}</b><span class="method-${esc(x.proposal.method)}">${x.proposal.isV1Fallback?'v1 fallback':'v2'}</span></div><small>${esc(x.windowTitle)}</small><small>${esc(x.proposal.reason)}</small></button>`).join('');document.querySelectorAll('.item').forEach(b=>b.onclick=()=>load(Number(b.dataset.index)))}
function render(){const s=current.selection,p=s.proposal,comparison=current.ocrComparison,recorded=current.recordedV1OCR;document.getElementById('summary').innerHTML=`<span class="tag">#${current.ordinal}/${rows.length}</span><span class="tag">${esc(current.application)}</span><span class="tag">${esc(current.windowTitle)}</span><span class="tag">${esc(current.triggerTypes.join(', '))}</span><span class="tag">${esc(current.capturedAt)}</span>`;let boxes='';if(document.getElementById('toggle-ax').checked){for(const a of s.ancestors){if(a.normalizedTopRectangle)boxes+=overlay(a.normalizedTopRectangle,'ancestor',`AX d${a.depth} ${a.subrole||a.role||''}`,`data-depth="${a.depth}"`)}}if(document.getElementById('toggle-v1').checked)boxes+=overlay(s.v1.normalizedTopRectangle,'v1',`v1 · ${s.v1.method}`);if(document.getElementById('toggle-v2').checked)boxes+=overlay(p.normalizedTopRectangle,'v2',p.isV1Fallback?'proposal · v1 fallback':`proposal · AX d${p.selectedDepth}`);const pt=s.pointer.screen;boxes+=`<div class="pointer" style="left:${pct(pt.x/s.image.width)};top:${pct(pt.y/s.image.height)}"></div>`;document.getElementById('stage').innerHTML=`<img src="/image?id=${encodeURIComponent(current.recordID)}" alt="Captured application window">${boxes}`;document.getElementById('v1-ocr').textContent=recorded.content||'';document.getElementById('v2-ocr').textContent=comparison.v2.content||'';document.getElementById('v1-meta').textContent=`${recorded.content.length.toLocaleString()} characters · ${recorded.recognizedLineCount??'—'} lines · actual live-pipeline output`;document.getElementById('v2-meta').textContent=`${comparison.v2.content.length.toLocaleString()} characters · ${comparison.v2.recognizedLineCount} lines · re-OCR from the same retained screenshot`;document.getElementById('proposal').innerHTML=`<div class="proposal method-${esc(p.method)}"><b>${esc(p.method)}</b> · ${esc(p.confidence)}</div><div class="reason">${esc(p.reason)}</div><p>${p.isV1Fallback?'No AX pane met the conservative generic gate. The proposed rectangle intentionally equals v1.':`Selected AX depth ${p.selectedDepth}: ${esc(p.selectedSubrole||p.selectedRole||'unlabeled container')}.`}</p><div class="muted">v1: ${esc(s.v1.method)} · ${esc(s.v1.confidence)}<br>record: ${esc(current.recordID)} · raw line ${current.sourceRawLine}</div>`;document.getElementById('ancestors').innerHTML=s.ancestors.map(a=>`<tr data-depth="${a.depth}" class="${a.depth===p.selectedDepth?'selected':''}"><td>${a.depth}</td><td>${esc([a.role,a.subrole].filter(Boolean).join(' / '))}</td><td>${esc(a.title||a.elementDescription||a.identifier||'—')}</td><td>${a.areaFraction==null?'—':(100*a.areaFraction).toFixed(1)+'%'}</td><td>${a.nearFrameCount}</td></tr>`).join('');renderNav()}
async function load(i){index=(i+rows.length)%rows.length;current=await (await fetch('/api/observation?id='+encodeURIComponent(rows[index].recordID))).json();render()}
async function init(){rows=await (await fetch('/api/index')).json();await load(0);document.getElementById('prev').onclick=()=>load(index-1);document.getElementById('next').onclick=()=>load(index+1);for(const id of ['toggle-v1','toggle-ax','toggle-v2'])document.getElementById(id).onchange=render;document.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='j')load(index+1);if(e.key==='ArrowLeft'||e.key==='k')load(index-1)})}
init().catch(e=>document.querySelector('.main').textContent=e.stack||e);
</script></body></html>'''


def handler(store: ReviewStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    self.send(HTTPStatus.OK, "text/html; charset=utf-8", HTML.encode())
                elif parsed.path == "/api/index":
                    self.send(HTTPStatus.OK, "application/json", json.dumps(store.summaries()).encode())
                elif parsed.path == "/api/observation":
                    record_id = query.get("id", [""])[0]
                    self.send(HTTPStatus.OK, "application/json", json.dumps(store.by_id[record_id]).encode())
                elif parsed.path == "/image":
                    record_id = query.get("id", [""])[0]
                    row = store.by_id[record_id]
                    image = Path(row["screenshotPath"])
                    self.send(HTTPStatus.OK, mimetypes.guess_type(image.name)[0] or "image/png", image.read_bytes())
                else:
                    self.send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")
            except KeyError:
                self.send(HTTPStatus.NOT_FOUND, "text/plain", b"unknown observation")

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    store = ReviewStore(arguments.review.expanduser())
    if arguments.check:
        print(f"READ-surface-v2 review verified: {len(store.rows)} observations.")
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), handler(store))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"READ-surface-v2 review: {url}")
    print("Read-only localhost UI; press Ctrl-C to stop.")
    if not arguments.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
