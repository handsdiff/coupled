#!/usr/bin/env python3
"""Serve the semantic-v16 shadow READ projection for manual review."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
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


def source_ids(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(value) for value in row.get("sourceRecordIDs", []) if value)


class ReviewStore:
    def __init__(
        self, baseline: Path, candidate: Path, surfaces: Path, source: Path,
        compiled: Path | None = None, packed: Path | None = None,
    ):
        self.baseline = baseline.resolve()
        self.candidate = candidate.resolve()
        self.surfaces = surfaces.resolve()
        self.source = source.resolve()
        self.compiled = compiled.resolve() if compiled else None
        self.packed = packed.resolve() if packed else None
        if (self.compiled is None) != (self.packed is None):
            raise ReviewError("--compiled and --packed must be supplied together")
        base_manifest = load_json(self.baseline / "reduction.json")
        candidate_manifest = load_json(self.candidate / "reduction.json")
        if base_manifest.get("reducerVersion") != "phase1-semantic-v14":
            raise ReviewError("baseline must be phase1-semantic-v14")
        if candidate_manifest.get("reducerVersion") != "phase1-semantic-v16":
            raise ReviewError("candidate must be phase1-semantic-v16")
        for directory, manifest in (
            (self.baseline, base_manifest), (self.candidate, candidate_manifest)
        ):
            expected = manifest.get("artifacts", {}).get("digestsSHA256", {})
            for name in ("events.jsonl", "unresolved.jsonl"):
                if sha256(directory / name) != expected.get(name):
                    raise ReviewError(f"artifact digest differs: {directory}/{name}")
        base_raw = base_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        candidate_raw = candidate_manifest.get("source", {}).get("digestsSHA256", {}).get("raw.jsonl")
        if not base_raw or base_raw != candidate_raw or sha256(self.source / "raw.jsonl") != base_raw:
            raise ReviewError("baseline, candidate, and source raw journal differ")

        surface_manifest = load_json(self.surfaces / "read-surface-evidence.json")
        evidence_path = self.surfaces / "read-surfaces.jsonl"
        surface_hash = surface_manifest.get("artifacts", {}).get("digestsSHA256", {}).get(
            "read-surfaces.jsonl"
        )
        if sha256(evidence_path) != surface_hash:
            raise ReviewError("READ-surface evidence digest differs")
        if candidate_manifest.get("source", {}).get("readSurfaceEvidence", {}).get(
            "readSurfacesSHA256"
        ) != surface_hash:
            raise ReviewError("candidate is not bound to supplied surface evidence")
        evidence = {
            str(row["sourceRecordID"]): row for row in load_jsonl(evidence_path)
        }
        raw_rows = load_jsonl(self.source / "raw.jsonl")
        raw_by_id = {
            str(row["recordID"]): row for row in raw_rows if row.get("recordID")
        }
        frame_by_ocr = {
            str(row["recordID"]): str(row["sourceFrameRecordID"])
            for row in raw_rows
            if row.get("recordType") == "visual_ocr_observation"
            and row.get("recordID") and row.get("sourceFrameRecordID")
        }

        base_reads = {
            source_ids(row): row
            for row in load_jsonl(self.baseline / "events.jsonl")
            if row.get("kind") == "read"
        }
        candidate_reads = [
            row for row in load_jsonl(self.candidate / "events.jsonl")
            if row.get("kind") == "read"
        ]
        candidate_by_id = {str(row["eventID"]): row for row in candidate_reads}
        model_occurrences: dict[str, list[dict[str, Any]]] = {}
        packing_summary: dict[str, Any] | None = None
        if self.compiled is not None and self.packed is not None:
            packing_manifest = load_json(self.packed / "packing.json")
            if packing_manifest.get("packerVersion") != "phase1-token-pack-v8":
                raise ReviewError("packed review requires phase1-token-pack-v8")
            if packing_manifest.get("packing", {}).get(
                "readNoveltyRendering", {}
            ).get("status") != "shadow_opt_in":
                raise ReviewError("packed review requires the shadow READ renderer")
            compiled_events_path = self.compiled / "events.jsonl"
            expected_compiled_hash = packing_manifest.get("source", {}).get(
                "digestsSHA256", {}
            ).get("events.jsonl")
            if sha256(compiled_events_path) != expected_compiled_hash:
                raise ReviewError("packed artifact is not bound to supplied compiled events")
            compiled_events = {
                str(item["sourceEventID"]): item
                for item in load_jsonl(compiled_events_path)
            }
            plan_path = self.packed / "context-plans.jsonl"
            expected_plan_hash = packing_manifest.get(
                "artifactDigestsSHA256", {}
            ).get("context-plans.jsonl")
            if sha256(plan_path) != expected_plan_hash:
                raise ReviewError("packed context-plan digest differs")
            for plan in load_jsonl(plan_path):
                for block in plan.get("retainedContextBlocks", []):
                    rendering = block.get("readRendering")
                    if not isinstance(rendering, dict):
                        continue
                    event_id = str(block.get("contextBlockID", ""))
                    compiled_event = compiled_events.get(event_id)
                    if compiled_event is None:
                        raise ReviewError(f"packed block lacks compiled event: {event_id}")
                    serialized = block.get("serializedOverride")
                    if serialized is None:
                        serialized = compiled_event.get("serialized")
                    try:
                        model_payload = json.loads(serialized)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ReviewError(f"invalid packed READ projection: {event_id}") from error
                    model_occurrences.setdefault(event_id, []).append({
                        "exampleID": plan.get("exampleID"),
                        "targetEventID": plan.get("targetEventID"),
                        "decision": rendering.get("decision"),
                        "dependencyAvailable": rendering.get("dependencyAvailable"),
                        "dependsOnEventID": rendering.get("dependsOnEventID"),
                        "contentTruncated": block.get("contentTruncated"),
                        "modelFacingContent": model_payload.get("content"),
                        "serializedSHA256": block.get("serializedSHA256"),
                    })
            packing_summary = {
                "packerVersion": packing_manifest.get("packerVersion"),
                "counts": packing_manifest.get("counts", {}).get(
                    "readRendering", {}
                ),
                "tokensRemoved": packing_manifest.get("counts", {}).get(
                    "modelInputTokensRemovedByReadNoveltyRendering", 0
                ),
            }
        rows: list[dict[str, Any]] = []
        self.images: dict[str, tuple[Path, str]] = {}
        for row in candidate_reads:
            ids = source_ids(row)
            baseline_row = base_reads.get(ids)
            surface = next((evidence[item] for item in reversed(ids) if item in evidence), {})
            observed = str(surface.get("content", row.get("content", "")))
            semantic = str(row.get("content", ""))
            details = row.get("reduction", {}).get("semanticReadContent", {})
            removed = details.get("removedLines", [])
            novelty = row.get("readNovelty", {})
            predecessor = candidate_by_id.get(str(novelty.get("dependsOnEventID", "")))
            image_key = ""
            for source_id in ids:
                raw_id = frame_by_ocr.get(source_id, source_id)
                raw = raw_by_id.get(raw_id, {})
                relative = raw.get("screenshotRelativePath")
                expected_hash = raw.get("screenshotSHA256")
                if isinstance(relative, str) and isinstance(expected_hash, str):
                    path = (self.source / relative).resolve()
                    if self.source in path.parents and path.is_file():
                        image_key = str(row["eventID"])
                        self.images[image_key] = (path, expected_hash)
                        break
            scaffolding_changed = bool(removed)
            novelty_decision = str(novelty.get("decision", "missing"))
            changed = scaffolding_changed or novelty_decision in {
                "emit_new_content", "suppress_no_new_content",
                "suppress_nonsemantic_microglyph",
            }
            occurrences = model_occurrences.get(str(row["eventID"]), [])
            rendering_counts = Counter(
                str(item.get("decision", "missing")) for item in occurrences
            )
            model_changed = any(
                item.get("decision") in {
                    "render_novel_content", "render_empty_adjacent_repeat"
                }
                for item in occurrences
            )
            model_fallback = any(
                item.get("decision") in {
                    "render_complete_dependency_unavailable",
                    "render_complete_uncertain_microglyph",
                    "retain_existing_truncated_state",
                    "retain_nonread_model_projection",
                }
                for item in occurrences
            )
            rows.append({
                "id": str(row["eventID"]),
                "changed": changed,
                "capturedAt": row.get("capturedAt") or row.get("availableAt"),
                "application": row.get("appName") or row.get("bundleIdentifier"),
                "windowTitle": row.get("windowTitle"),
                "sequence": row.get("sequence"),
                "baselineSequence": baseline_row.get("sequence") if baseline_row else None,
                "sourceRecordIDs": list(ids),
                "priorCompleteSemantic": predecessor.get("content", "") if predecessor else "",
                "observedOCR": observed,
                "removedScaffolding": removed,
                "completeSemantic": semantic,
                "novelContent": novelty.get("content", ""),
                "novelty": novelty,
                "semanticDetails": details,
                "baselineContent": baseline_row.get("content", "") if baseline_row else "",
                "imageKey": image_key,
                "modelOccurrences": occurrences,
                "modelRenderingCounts": dict(sorted(rendering_counts.items())),
                "modelChanged": model_changed,
                "modelFallback": model_fallback,
            })
        rows.sort(key=lambda row: (str(row.get("capturedAt") or ""), row["id"]))
        self.rows = rows
        self.by_id = {row["id"]: row for row in rows}
        decisions = Counter(str(row["novelty"].get("decision", "missing")) for row in rows)
        self.summary = {
            "status": "semantic_v16_shadow_review_only_not_training_authority",
            "baselineVersion": "phase1-semantic-v14",
            "candidateVersion": "phase1-semantic-v16",
            "sourceRawSHA256": base_raw,
            "readCount": len(rows),
            "changedReadCount": sum(bool(row["changed"]) for row in rows),
            "scaffoldingChangedReadCount": sum(bool(row["removedScaffolding"]) for row in rows),
            "noveltyDecisions": dict(sorted(decisions.items())),
            "packing": packing_summary,
        }

    def index(self) -> list[dict[str, Any]]:
        keys = (
            "id", "changed", "capturedAt", "application", "windowTitle",
            "sequence", "modelChanged", "modelFallback", "modelRenderingCounts",
        )
        result = []
        for row in self.rows:
            value = {key: row.get(key) for key in keys}
            removed = row.get("removedScaffolding", [])
            value["scaffoldingCount"] = len(removed)
            value["genericScaffoldingCount"] = sum(
                item.get("reason") == "stable_peripheral_interface_text"
                for item in removed
            )
            value["noveltyDecision"] = row.get("novelty", {}).get("decision", "missing")
            result.append(value)
        return result


HTML = r'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Coupled · semantic and model-facing READ review</title><style>
:root{color-scheme:light dark;font:13px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:Canvas;color:CanvasText}*{box-sizing:border-box}body{margin:0}.shell{display:grid;grid-template-columns:330px minmax(0,1fr);min-height:100vh}.side{border-right:1px solid color-mix(in srgb,CanvasText 18%,transparent);padding:16px;position:sticky;top:0;height:100vh;overflow:auto}.main{padding:22px;min-width:0}.muted{opacity:.62}.stats{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.tag{padding:3px 7px;border-radius:999px;background:color-mix(in srgb,CanvasText 8%,Canvas)}select,input,button{font:inherit;color:CanvasText;background:Canvas;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);border-radius:7px;padding:7px}.filters{display:grid;gap:7px;margin:12px 0}.items{display:grid;gap:4px}.item{text-align:left;background:transparent}.item.active{border-color:Highlight;background:color-mix(in srgb,Highlight 18%,Canvas)}.item small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.65}.top{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.top h1{font-size:21px;margin:0 auto 0 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}.panel{border:1px solid color-mix(in srgb,CanvasText 16%,transparent);border-radius:9px;min-width:0;overflow:hidden}.panel h2{font-size:14px;margin:0;padding:10px 12px;border-bottom:1px solid color-mix(in srgb,CanvasText 14%,transparent)}pre{margin:0;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:430px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:color-mix(in srgb,CanvasText 4%,Canvas)}img{display:block;max-width:100%;max-height:520px;margin:auto}.wide{grid-column:1/-1}.changed{color:#c06400}.empty{padding:70px;text-align:center;opacity:.6}@media(max-width:900px){.shell{display:block}.side{position:static;height:auto;border-right:0;border-bottom:1px solid color-mix(in srgb,CanvasText 18%,transparent)}.grid{grid-template-columns:1fr}.wide{grid-column:auto}}</style>
<div class="shell"><aside class="side"><b>Semantic → model-facing READ review</b><div id="meta" class="muted"></div><div id="stats" class="stats"></div><div class="filters"><select id="scope"><option value="model-changed">Model-facing text changed</option><option value="fallback">Full/truncated fallback</option><option value="changed">Any semantic change</option><option value="scaffolding">Any scaffolding removal</option><option value="generic">Generic scaffolding only</option><option value="novel">Emitted novelty only</option><option value="suppressed">Suppressed repeats/artifacts</option><option value="">All READs</option></select><select id="app"><option value="">All applications</option></select><input id="search" placeholder="Search #, window, or app"></div><div id="items" class="items"></div></aside><main id="main" class="main"></main></div>
<script>const $=x=>document.getElementById(x),esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));let rows=[],filtered=[],index=0;function inScope(r,s){if(!s)return true;if(s==='model-changed')return r.modelChanged;if(s==='fallback')return r.modelFallback;if(s==='changed')return r.changed;if(s==='scaffolding')return r.scaffoldingCount>0;if(s==='generic')return r.genericScaffoldingCount>0;if(s==='novel')return r.noveltyDecision==='emit_new_content';if(s==='suppressed')return r.noveltyDecision.startsWith('suppress_');return false}function apply(){const scope=$('scope').value,a=$('app').value,q=$('search').value.toLowerCase();filtered=rows.filter(r=>inScope(r,scope)&&(!a||r.application===a)&&(!q||(r.sequence+' '+r.windowTitle+' '+r.application).toLowerCase().includes(q)));index=Math.min(index,Math.max(0,filtered.length-1));list();show()}function list(){$('items').innerHTML=filtered.map((r,i)=>`<button class="item ${i===index?'active':''}" data-i="${i}"><b class="${r.changed||r.modelChanged?'changed':''}">#${r.sequence} ${r.modelChanged?'model text shortened':r.modelFallback?'full text retained':r.changed?'cleaned':'unchanged'}</b><small>${esc(r.application)} · ${esc(r.windowTitle)}</small><small>${esc(r.capturedAt)}</small></button>`).join('');document.querySelectorAll('.item').forEach(b=>b.onclick=()=>{index=+b.dataset.i;list();show()})}function renderingLabel(g){if(g.decision==='render_empty_adjacent_repeat')return['EXACT REPEAT REMOVED',`The cleaned READ in panel 4 exactly repeats panel 1. It contributes no new text in ${g.count} later training context${g.count===1?'':'s'}.`,'[No new READ text]'];if(g.decision==='render_novel_content')return['NEW TEXT ONLY',`The immediately preceding READ is present in all ${g.count} training context${g.count===1?'':'s'}, so only the non-repeated text below is included.`,g.modelFacingContent||'[No new READ text]'];if(g.decision==='render_complete_dependency_unavailable')return['FULL CURRENT READ RETAINED',`The preceding READ is outside ${g.count} packed training context${g.count===1?'':'s'}, so removing overlap would lose information.`,g.modelFacingContent||'[No READ text]'];if(g.decision==='render_complete_uncertain_microglyph')return['FULL CURRENT READ RETAINED',`A tiny OCR difference was uncertain in ${g.count} training context${g.count===1?'':'s'}, so nothing was removed.`,g.modelFacingContent||'[No READ text]'];if(g.decision==='retain_existing_truncated_state')return['NORMAL 32K CONTEXT TRUNCATION',`This was the oldest retained READ in ${g.count} training context${g.count===1?'':'s'} and was shortened only to fit the 32K limit—not by overlap removal.`,g.modelFacingContent||'[No retained READ text]'];return['FULL CURRENT READ',`The complete cleaned READ is included in ${g.count} training context${g.count===1?'':'s'}.`,g.modelFacingContent||'[No READ text]']}function renderings(items){if(!items.length)return 'This READ is not retained in any reviewed training example.';const groups=new Map();for(const x of items){const key=JSON.stringify([x.decision,x.dependencyAvailable,x.contentTruncated,x.modelFacingContent]);if(!groups.has(key))groups.set(key,{...x,count:0});groups.get(key).count++}return [...groups.values()].map((g,i)=>{const [title,why,text]=renderingLabel(g);return `${groups.size>1?`CASE ${i+1}\n`:''}${title}\n${why}\n\nTEXT GIVEN TO THE MODEL:\n${text}`}).join('\n\n────────────────────────────────────────\n\n')}async function show(){const s=filtered[index];if(!s){$('main').innerHTML='<div class="empty">No matching READs</div>';return}const r=await(await fetch('/api/read?id='+encodeURIComponent(s.id))).json();const removed=(r.removedScaffolding||[]).map(x=>`[${x.reason}] ${x.text}`).join('\n');$('main').innerHTML=`<div class="top"><h1>#${r.sequence} ${esc(r.application)} · ${esc(r.windowTitle)}</h1><span class="tag">${esc(r.novelty.decision)}</span><span class="tag">removed ${r.removedScaffolding.length} UI lines</span></div><div class="muted">${esc(r.capturedAt)} · ${esc(r.sourceRecordIDs.join(', '))}</div><div class="grid">${r.imageKey?`<section class="panel wide"><h2>Captured screen evidence</h2><img src="/api/image?id=${encodeURIComponent(r.imageKey)}"></section>`:''}<section class="panel"><h2>1 · Previous cleaned READ</h2><pre>${esc(r.priorCompleteSemantic||'—')}</pre></section><section class="panel"><h2>2 · Raw OCR from the screen</h2><pre>${esc(r.observedOCR||'—')}</pre></section><section class="panel"><h2>3 · Interface text removed</h2><pre>${esc(removed||'—')}</pre></section><section class="panel"><h2>4 · Current cleaned READ (always preserved)</h2><pre>${esc(r.completeSemantic||'—')}</pre></section><section class="panel wide"><h2>5 · New text after removing adjacent overlap</h2><pre>${esc(r.novelContent||'[No new READ text]')}</pre></section><section class="panel wide"><h2>6 · What the model receives</h2><pre>${esc(renderings(r.modelOccurrences||[]))}</pre></section><section class="panel wide"><h2>Technical audit details</h2><pre>${esc(JSON.stringify({novelty:r.novelty,semantic:r.semanticDetails,modelRenderingCounts:r.modelRenderingCounts},null,2))}</pre></section></div>`}Promise.all([fetch('/api/summary').then(r=>r.json()),fetch('/api/index').then(r=>r.json())]).then(([s,x])=>{rows=x;const p=s.packing;$('meta').textContent=`${s.baselineVersion} → ${s.candidateVersion}${p?' → '+p.packerVersion:''} · shadow only`;$('stats').innerHTML=`<span class="tag">READs ${s.readCount}</span><span class="tag changed">changed ${s.changedReadCount}</span><span class="tag">scaffolding ${s.scaffoldingChangedReadCount}</span>${p?`<span class="tag">model tokens removed ${p.tokensRemoved.toLocaleString()}</span>`:''}`;[...new Set(rows.map(r=>r.application).filter(Boolean))].sort().forEach(v=>$('app').insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));$('scope').onchange=$('app').onchange=$('search').oninput=()=>{index=0;apply()};apply()}).catch(e=>$('main').textContent=e.stack||e)</script>'''


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
            elif parsed.path == "/api/image":
                item = store.images.get(query.get("id", [""])[0])
                if item is None:
                    self.send_error(404)
                elif sha256(item[0]) != item[1]:
                    self.send_error(409, "screenshot digest differs")
                else:
                    self.send_body(
                        item[0].read_bytes(),
                        mimetypes.guess_type(item[0].name)[0] or "image/png",
                    )
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
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--compiled", type=Path)
    parser.add_argument("--packed", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8772, type=int)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    store = ReviewStore(
        arguments.baseline, arguments.candidate,
        arguments.read_surface_evidence, arguments.source,
        arguments.compiled, arguments.packed,
    )
    if arguments.check:
        print(json.dumps(store.summary, sort_keys=True))
        return 0
    server = ThreadingHTTPServer((arguments.host, arguments.port), handler(store))
    print(f"Semantic READ v16 review: http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
