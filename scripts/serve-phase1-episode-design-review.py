#!/usr/bin/env python3
"""Serve a read-only local UI for Phase 1 episode-design shadow artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ReviewUIError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewUIError(f"expected object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewUIError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def index(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value or value in result:
            raise ReviewUIError(f"invalid or duplicate {key}: {value!r}")
        result[value] = row
    return result


class ReviewStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.manifest = load_json(self.path / "episode-review.json")
        if self.manifest.get("status") != "shadow_review_only_not_training_authority":
            raise ReviewUIError("review artifact is not explicitly shadow-only")
        digests = self.manifest.get("artifactDigestsSHA256", {})
        for name, expected in digests.items():
            artifact = self.path / name
            if not artifact.is_file() or sha256(artifact) != expected:
                raise ReviewUIError(f"artifact digest disagrees: {name}")
        self.candidates = load_jsonl(self.path / "episode-candidates.jsonl")
        self.model_inputs = index(
            load_jsonl(self.path / "model-facing-inputs.jsonl"), "exampleID"
        )
        proposals_path = self.path / "proposed-annotations.jsonl"
        self.proposals = (
            index(load_jsonl(proposals_path), "label")
            if proposals_path.is_file()
            else {}
        )
        self.candidate_by_label = index(self.candidates, "label")
        if set(self.proposals) - set(self.candidate_by_label):
            raise ReviewUIError("proposal labels do not match candidates")
        for candidate in self.candidates:
            example_id = candidate.get("predictionOpportunity", {}).get(
                "modelFacingExampleID"
            )
            if example_id is not None and example_id not in self.model_inputs:
                raise ReviewUIError(
                    f"candidate has no model-facing input: {candidate['label']}"
                )

    def summaries(self) -> list[dict[str, Any]]:
        rows = []
        for candidate in self.candidates:
            proposal = self.proposals.get(candidate["label"], {})
            visibility = proposal.get("visibilityAssessment", {})
            rows.append(
                {
                    "label": candidate["label"],
                    "category": candidate.get("selectionCategory"),
                    "range": candidate.get("oneBasedExampleRange"),
                    "mode": candidate.get("selectionMode"),
                    "decision": proposal.get("decision", "unreviewed"),
                    "visibility": visibility.get("status", "not_assessed"),
                    "gatesPassed": candidate.get("mechanicalGates", {}).get("passed"),
                }
            )
        return rows

    def detail(self, label: str) -> dict[str, Any]:
        candidate = self.candidate_by_label.get(label)
        if candidate is None:
            raise KeyError(label)
        proposal = self.proposals.get(label)
        opportunity = candidate["predictionOpportunity"]
        example_ids = {
            value
            for value in (
                opportunity.get("modelFacingExampleID"),
                opportunity.get("nearestLaterPackedExampleID"),
            )
            if isinstance(value, str)
        }
        if proposal:
            example_ids.update(
                partition["modelFacingExampleID"]
                for partition in proposal.get("partitions", [])
            )
        return {
            "candidate": candidate,
            "proposal": proposal,
            "modelFacingInputs": {
                value: self.model_inputs[value] for value in sorted(example_ids)
            },
        }


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phase 1 Episode Design Review</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#12161b;--line:#27303a;--muted:#91a0af;--text:#edf2f7;--accent:#7dd3fc;--warn:#fbbf24;--bad:#fb7185;--good:#86efac}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}header{position:sticky;top:0;z-index:2;padding:12px 18px;border-bottom:1px solid var(--line);background:#0b0d10ee;backdrop-filter:blur(10px)}h1,h2,h3{margin:0 0 10px;font-family:ui-sans-serif,system-ui,sans-serif}h1{font-size:18px}h2{font-size:16px}h3{font-size:13px;color:var(--accent)}.app{display:grid;grid-template-columns:280px minmax(0,1fr);min-height:calc(100vh - 58px)}nav{border-right:1px solid var(--line);padding:10px;overflow:auto;height:calc(100vh - 58px);position:sticky;top:58px}.item{display:block;width:100%;text-align:left;color:var(--text);background:transparent;border:1px solid transparent;border-radius:8px;padding:9px;margin:2px 0;cursor:pointer}.item:hover,.item.active{background:#17202a;border-color:#334155}.item small{display:block;color:var(--muted);margin-top:3px}.main{padding:14px;min-width:0}.warning{border:1px solid #7c5c16;background:#2b220e;padding:12px;border-radius:10px;color:#fde68a;margin-bottom:14px}.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;min-width:0}.head{padding:12px;border-bottom:1px solid var(--line)}.body{padding:12px}.card{border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0;background:#0e1217}.meta{color:var(--muted);font-size:12px}.bad{color:var(--bad)}.good{color:var(--good)}.warn{color:var(--warn)}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0;padding:10px;background:#090b0e;border:1px solid #202833;border-radius:7px;max-height:420px;overflow:auto}details{margin:8px 0}summary{cursor:pointer;color:var(--accent)}.history{max-height:620px;overflow:auto}.pill{display:inline-block;padding:2px 6px;border:1px solid #3b4755;border-radius:99px;color:var(--muted);margin:2px}.partition{border-left:3px solid var(--accent)}@media(max-width:980px){.app{grid-template-columns:1fr}nav{position:relative;top:0;height:auto;border-right:0;border-bottom:1px solid var(--line);max-height:260px}.grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Phase 1 episode design review <span class="pill">shadow only</span></h1><div class="meta">Actual historical sampling input → editing trajectory → proposed closed substantive outcome</div></header>
<div class="app"><nav id="nav"></nav><main class="main" id="main">Loading…</main></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty=v=>JSON.stringify(v,null,2); const pre=v=>`<pre>${esc(typeof v==='string'?v:pretty(v))}</pre>`;
const marker=t=>(t?.segments||[]).map(s=>s.type==='paste'?'&lt;|paste|&gt;':esc(s.content||'')).join('');
let summaries=[]; let selected='';
async function loadIndex(){summaries=await (await fetch('/api/index')).json();document.getElementById('nav').innerHTML=summaries.map(x=>`<button class="item" data-label="${esc(x.label)}"><b>${esc(x.label)}</b><small>${x.range.first}–${x.range.last} · ${esc(x.decision)}</small><small class="${x.visibility.includes('missing')?'bad':''}">${esc(x.visibility)}</small></button>`).join('');document.querySelectorAll('.item').forEach(b=>b.onclick=()=>loadDetail(b.dataset.label));await loadDetail(summaries[0].label)}
function modelPanel(m,title){const history=m.retainedHistory.map(e=>`<div class="card"><div class="meta">${e.ordinal} · ${esc(e.projection.kind)} · ${esc(e.projection.application)} · ${esc(e.projection.window)}${e.contentTruncated?' · TRUNCATED':''}</div>${pre(e.projection.kind==='read'?e.projection.content:e.projection.authorshipSegments)}</div>`).join('');return `<div class="card"><h3>${esc(title)}</h3><div class="meta">${m.modelInputTokenCount} tokens · ${m.retainedContextEventCount}/${m.sourceContextEventCount} events retained · ${m.droppedContextEventCount} dropped · ${esc(m.semanticModelInputSHA256)}</div><h3>Conditioning query</h3>${pre(m.query)}<details><summary>Exact retained model history</summary><div class="history">${history}</div></details><details><summary>Exact serialized model input</summary>${pre(m.exactSemanticModelInput)}</details></div>`}
function proposalPanel(p){if(!p)return '<div class="card">No proposal.</div>';const vis=p.visibilityAssessment||{};const target=p.finalizedTarget?`<h3>Proposed target</h3><div class="card">${marker(p.finalizedTarget)}</div>`:'';const parts=(p.partitions||[]).map(x=>`<div class="card partition"><b>${x.firstOneBasedExampleOrdinal}–${x.lastOneBasedExampleOrdinal}: ${esc(x.decision)}</b><div class="meta">${esc(x.targetPolicy)} · input ${esc(x.modelFacingExampleID)}</div>${x.finalizedTarget?`<div>${marker(x.finalizedTarget)}</div>`:'<div class="warn">No loss target</div>'}<p>${esc(x.notes)}</p></div>`).join('');return `<div class="card"><h3>Assistant proposal — pending human adjudication</h3><b>${esc(p.decision)}</b><p>${esc(p.notes)}</p><div class="${vis.status?.includes('missing')?'bad':'meta'}"><b>${esc(vis.status)}</b><br>${esc(vis.missingInformation||vis.note||'')}</div></div>${target}${parts?`<h3>Explicit partitions</h3>${parts}`:''}`}
function trajectory(c){return c.members.map(m=>`<div class="card"><b>Example ${m.oneBasedExampleOrdinal??'history-only'} · ${esc(m.application)} · ${esc(m.boundaryReason)}</b><div class="meta">${esc(m.writeEventID)} · selected ${esc(m.selectedTerminalObservationSource)}</div><h3>Old loss target</h3>${pre(m.currentLossTarget||'[no independent loss target]')}<details><summary>Raw logical BEFORE</summary>${pre(m.beforeLogicalValue)}</details><details><summary>Reducer-selected terminal</summary>${pre(m.selectedTerminalLogicalValue)}</details></div>`).join('')}
async function loadDetail(label){selected=label;document.querySelectorAll('.item').forEach(b=>b.classList.toggle('active',b.dataset.label===label));const d=await (await fetch('/api/candidate?label='+encodeURIComponent(label))).json();const c=d.candidate,p=d.proposal,onsetID=c.predictionOpportunity.modelFacingExampleID,first=onsetID?d.modelFacingInputs[onsetID]:null,all=Object.values(d.modelFacingInputs);const inputPanel=first?modelPanel(first,'Candidate-onset input'):`<div class="card bad"><h3>No frozen packed input existed at episode onset</h3><p>The first member was history-only in the old corpus. A future episode compiler must build a new query from this recorded conditioning state; the later packed example is not a substitute.</p>${pre(c.initialConditioningState)}</div>`;document.getElementById('main').innerHTML=`<div class="warning"><b>Sampling proxy, not focus-time evidence.</b> ${esc(c.predictionOpportunity.limitation)}</div><h2>${esc(c.label)}</h2><div class="meta">Examples ${c.oneBasedExampleRange.first}–${c.oneBasedExampleRange.last} · ${esc(c.selectionCategory)} · ${esc(c.selectionMode)} · gates ${c.mechanicalGates.passed?'passed':'failed: '+c.mechanicalGates.failures.join(', ')}</div><p>${esc(c.selectionRationale)}</p><div class="grid"><section class="panel"><div class="head"><h2>What the model actually received</h2></div><div class="body">${inputPanel}${all.filter(x=>!first||x.exampleID!==first.exampleID).map(x=>modelPanel(x,first?'Partition input':'Nearest later packed input (not episode onset)')).join('')}</div></section><section class="panel"><div class="head"><h2>What the human produced</h2></div><div class="body">${proposalPanel(p)}<h3>Complete micro-WRITE trajectory</h3>${trajectory(c)}</div></section></div>`}
loadIndex().catch(e=>document.getElementById('main').textContent=e.stack||e);
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
            try:
                if parsed.path == "/":
                    self.send(HTTPStatus.OK, "text/html; charset=utf-8", HTML.encode())
                elif parsed.path == "/api/index":
                    self.send(
                        HTTPStatus.OK,
                        "application/json",
                        json.dumps(store.summaries(), ensure_ascii=False).encode(),
                    )
                elif parsed.path == "/api/candidate":
                    label = urllib.parse.parse_qs(parsed.query).get("label", [""])[0]
                    self.send(
                        HTTPStatus.OK,
                        "application/json",
                        json.dumps(store.detail(label), ensure_ascii=False).encode(),
                    )
                else:
                    self.send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")
            except KeyError:
                self.send(HTTPStatus.NOT_FOUND, "text/plain", b"unknown candidate")

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    store = ReviewStore(arguments.review.expanduser())
    if arguments.check:
        print(
            f"Episode design review verified: {len(store.candidates)} candidates, "
            f"{len(store.model_inputs)} exact model-facing inputs."
        )
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), handler(store))
    url = f"http://127.0.0.1:{arguments.port}/"
    print(f"Episode design review: {url}")
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
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, ReviewUIError) as error:
        raise SystemExit(f"serve-phase1-episode-design-review: {error}") from error
