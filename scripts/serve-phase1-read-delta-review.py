#!/usr/bin/env python3
"""Serve a read-only semantic-v14 versus semantic-v15 READ review."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
from collections import Counter
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
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReviewError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def verified_reduction(directory: Path, version: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = load_json(directory / "reduction.json")
    if manifest.get("reducerVersion") != version:
        raise ReviewError(f"expected {version}: {directory}")
    digests = manifest.get("artifacts", {}).get("digestsSHA256", {})
    for name in ("events.jsonl", "unresolved.jsonl"):
        path = directory / name
        if not path.is_file() or sha256(path) != digests.get(name):
            raise ReviewError(f"{version} artifact digest differs: {name}")
    return (
        manifest,
        load_jsonl(directory / "events.jsonl"),
        load_jsonl(directory / "unresolved.jsonl"),
    )


def source_ids(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(value) for value in row.get("sourceRecordIDs", []) if value)


def full_observation(
    row: dict[str, Any] | None,
    evidence: dict[str, dict[str, Any]],
) -> str:
    if row is None:
        return ""
    for record_id in reversed(source_ids(row)):
        item = evidence.get(record_id)
        if item is not None and isinstance(item.get("content"), str):
            return str(item["content"])
    return str(row.get("content", ""))


class ReviewStore:
    def __init__(self, baseline: Path, candidate: Path, surfaces: Path):
        self.baseline = baseline.resolve()
        self.candidate = candidate.resolve()
        self.surfaces = surfaces.resolve()
        base_manifest, base_events, _ = verified_reduction(
            self.baseline, "phase1-semantic-v14"
        )
        candidate_manifest, candidate_events, candidate_unresolved = verified_reduction(
            self.candidate, "phase1-semantic-v15"
        )
        base_raw = base_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        candidate_raw = candidate_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        if not base_raw or base_raw != candidate_raw:
            raise ReviewError("baseline and candidate do not derive from the same raw journal")

        surface_manifest = load_json(self.surfaces / "read-surface-evidence.json")
        evidence_path = self.surfaces / "read-surfaces.jsonl"
        expected = surface_manifest.get("artifacts", {}).get("digestsSHA256", {}).get(
            "read-surfaces.jsonl"
        )
        if sha256(evidence_path) != expected:
            raise ReviewError("READ-surface evidence digest differs")
        candidate_surface_hash = (
            candidate_manifest.get("source", {})
            .get("readSurfaceEvidence", {})
            .get("readSurfacesSHA256")
        )
        if candidate_surface_hash != expected:
            raise ReviewError("candidate is not bound to the supplied READ-surface evidence")
        evidence_rows = load_jsonl(evidence_path)
        evidence = {str(row["sourceRecordID"]): row for row in evidence_rows}

        base_reads = {
            str(row["eventID"]): row
            for row in base_events
            if row.get("kind") == "read"
        }
        candidate_reads = {
            str(row["eventID"]): row
            for row in candidate_events
            if row.get("kind") == "read"
        }
        base_by_source = {source_ids(row): row for row in base_reads.values()}
        candidate_by_source = {source_ids(row): row for row in candidate_reads.values()}
        delta_dispositions = {
            source_ids(row): row
            for row in candidate_unresolved
            if row.get("reason") == "adjacent_causal_read_no_new_content"
        }

        all_sources = set(base_by_source) | set(candidate_by_source)
        rows: list[dict[str, Any]] = []
        for ids in all_sources:
            before = base_by_source.get(ids)
            after = candidate_by_source.get(ids)
            disposition = delta_dispositions.get(ids)
            metadata = (
                after.get("reduction", {}).get("adjacentReadDelta", {})
                if after is not None
                else disposition.get("details", {}) if disposition is not None else {}
            )
            if before is None:
                status = "restored_full"
            elif after is None:
                status = "suppressed" if disposition is not None else "removed_other"
            elif before.get("content") != after.get("content"):
                status = "delta"
            elif metadata.get("decision") == "retain_complete_current":
                status = "conservative_full_fallback"
            else:
                status = "unchanged"
            identity = after or before or disposition
            assert identity is not None
            prior_id = metadata.get("priorEventID")
            prior = candidate_reads.get(str(prior_id)) or base_reads.get(str(prior_id))
            current_full = full_observation(after or before or disposition, evidence)
            rows.append(
                {
                    "id": str((after or before or {}).get("eventID") or "raw:" + ",".join(ids)),
                    "status": status,
                    "capturedAt": identity.get("capturedAt") or identity.get("availableAt"),
                    "application": identity.get("appName") or identity.get("bundleIdentifier"),
                    "windowTitle": identity.get("windowTitle"),
                    "sourceRecordIDs": list(ids),
                    "priorEventID": prior_id,
                    "priorFullContent": full_observation(prior, evidence),
                    "currentFullContent": current_full,
                    "baselineContent": before.get("content", "") if before else "",
                    "candidateContent": after.get("content", "") if after else "",
                    "metadata": metadata,
                    "baselineSequence": before.get("sequence") if before else None,
                    "candidateSequence": after.get("sequence") if after else None,
                }
            )
        rows.sort(key=lambda row: (str(row.get("capturedAt") or ""), row["id"]))
        self.rows = rows
        self.by_id = {row["id"]: row for row in rows}
        counts = Counter(row["status"] for row in rows)
        self.summary = {
            "status": "shadow_review_only_not_training_authority",
            "baselineVersion": "phase1-semantic-v14",
            "candidateVersion": "phase1-semantic-v15",
            "baselineDirectory": str(self.baseline),
            "candidateDirectory": str(self.candidate),
            "sourceRawSHA256": base_raw,
            "counts": dict(sorted(counts.items())),
            "totalReadLineages": len(rows),
        }

    def index(self) -> list[dict[str, Any]]:
        return [
            {
                key: row.get(key)
                for key in (
                    "id", "status", "capturedAt", "application", "windowTitle",
                    "baselineSequence", "candidateSequence",
                )
            }
            for row in self.rows
        ]


HTML = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coupled · adjacent READ delta review</title><style>
:root{color-scheme:light dark;font:13px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:Canvas;color:CanvasText}*{box-sizing:border-box}body{margin:0}.shell{display:grid;grid-template-columns:330px minmax(0,1fr);min-height:100vh}.side{border-right:1px solid color-mix(in srgb,CanvasText 18%,transparent);padding:16px;position:sticky;top:0;height:100vh;overflow:auto}.main{padding:22px;min-width:0}.muted{opacity:.62}.stats{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.tag{padding:3px 7px;border-radius:999px;background:color-mix(in srgb,CanvasText 8%,Canvas)}select,input,button{font:inherit;color:CanvasText;background:Canvas;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);border-radius:7px;padding:7px}.filters{display:grid;gap:7px;margin:12px 0}.items{display:grid;gap:4px}.item{text-align:left;background:transparent}.item.active{border-color:Highlight;background:color-mix(in srgb,Highlight 18%,Canvas)}.item small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.65}.top{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.top h1{font-size:21px;margin:0 auto 0 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}.panel{border:1px solid color-mix(in srgb,CanvasText 16%,transparent);border-radius:9px;min-width:0;overflow:hidden}.panel h2{font-size:14px;margin:0;padding:10px 12px;border-bottom:1px solid color-mix(in srgb,CanvasText 14%,transparent)}pre{margin:0;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:color-mix(in srgb,CanvasText 4%,Canvas)}.wide{grid-column:1/-1}.delta{color:#16834a}.suppressed{color:#996100}.restored_full{color:#2563eb}.conservative_full_fallback{color:#7c3aed}.unchanged{opacity:.6}.empty{padding:70px;text-align:center;opacity:.6}@media(max-width:900px){.shell{display:block}.side{position:static;height:auto;border-right:0;border-bottom:1px solid color-mix(in srgb,CanvasText 18%,transparent)}.grid{grid-template-columns:1fr}.wide{grid-column:auto}}</style>
<div class="shell"><aside class="side"><b>Adjacent causal READ review</b><div id="meta" class="muted"></div><div id="stats" class="stats"></div><div class="filters"><select id="status"><option value="changed">Changed only</option><option value="">All</option><option>delta</option><option>suppressed</option><option>restored_full</option><option>conservative_full_fallback</option><option>unchanged</option></select><select id="app"><option value="">All applications</option></select><input id="search" placeholder="Search text or window"></div><div id="items" class="items"></div></aside><main id="main" class="main"></main></div>
<script>const $=x=>document.getElementById(x),esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));let rows=[],filtered=[],index=0,summary={};function changed(s){return s!=='unchanged'}function apply(){const st=$('status').value,a=$('app').value,q=$('search').value.toLowerCase();filtered=rows.filter(r=>(!st||(st==='changed'?changed(r.status):r.status===st))&&(!a||r.application===a)&&(!q||(r.windowTitle+' '+r.application).toLowerCase().includes(q)));index=Math.min(index,Math.max(0,filtered.length-1));list();show()}function list(){$('items').innerHTML=filtered.map((r,i)=>`<button class="item ${i===index?'active':''}" data-i="${i}"><b class="${r.status}">${esc(r.status)}</b><small>${esc(r.application)} · ${esc(r.windowTitle)}</small><small>${esc(r.capturedAt)}</small></button>`).join('');document.querySelectorAll('.item').forEach(b=>b.onclick=()=>{index=+b.dataset.i;list();show()})}async function show(){const s=filtered[index];if(!s){$('main').innerHTML='<div class="empty">No matching READs</div>';return}const r=await(await fetch('/api/read?id='+encodeURIComponent(s.id))).json();$('main').innerHTML=`<div class="top"><h1>${esc(r.application)} · ${esc(r.windowTitle)}</h1><span class="tag ${r.status}">${esc(r.status)}</span><span class="tag">v14 #${r.baselineSequence??'—'}</span><span class="tag">v15 #${r.candidateSequence??'—'}</span></div><div class="muted">${esc(r.capturedAt)} · ${esc(r.sourceRecordIDs.join(', '))}</div><div class="grid"><section class="panel"><h2>Previous full observed state</h2><pre>${esc(r.priorFullContent||'—')}</pre></section><section class="panel"><h2>Current full observed state</h2><pre>${esc(r.currentFullContent||'—')}</pre></section><section class="panel"><h2>Before · semantic v14</h2><pre>${esc(r.baselineContent||'—')}</pre></section><section class="panel"><h2>After · semantic v15 emitted READ</h2><pre>${esc(r.candidateContent||'—')}</pre></section><section class="panel wide"><h2>Alignment and surface evidence</h2><pre>${esc(JSON.stringify(r.metadata,null,2))}</pre></section></div>`}Promise.all([fetch('/api/summary').then(r=>r.json()),fetch('/api/index').then(r=>r.json())]).then(([s,x])=>{summary=s;rows=x;$('meta').textContent=`${s.baselineVersion} → ${s.candidateVersion} · shadow only`;$('stats').innerHTML=Object.entries(s.counts).map(([k,v])=>`<span class="tag ${k}">${k}: ${v}</span>`).join('');[...new Set(rows.map(r=>r.application).filter(Boolean))].sort().forEach(v=>$('app').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));$('status').onchange=$('app').onchange=$('search').oninput=()=>{index=0;apply()};apply()}).catch(e=>$('main').textContent=e.stack||e)</script>'''


def handler(store: ReviewStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def send_body(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path in {"/", "/index.html"}:
                self.send_body(HTML.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/summary":
                self.send_body(json.dumps(store.summary).encode(), "application/json")
            elif parsed.path == "/api/index":
                self.send_body(json.dumps(store.index()).encode(), "application/json")
            elif parsed.path == "/api/read":
                row = store.by_id.get(query.get("id", [""])[0])
                if row is None:
                    self.send_error(404)
                else:
                    self.send_body(json.dumps(row).encode(), "application/json")
            else:
                self.send_error(404)

        def log_message(self, format: str, *values: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--read-surface-evidence", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8769, type=int)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    store = ReviewStore(
        arguments.baseline, arguments.candidate, arguments.read_surface_evidence
    )
    if arguments.check:
        print(json.dumps(store.summary, sort_keys=True))
        return 0
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler(store))
    print(f"Adjacent causal READ review: http://{arguments.host}:{arguments.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
