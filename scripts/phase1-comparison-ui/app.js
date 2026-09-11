'use strict';

function filterExamples(examples, f) {
  const search = (f.search || '').trim().toLowerCase().replace(/^#/, '');
  return examples.filter(r => {
    if (f.app && r.application !== f.app) return false;
    if (f.day && r.day !== f.day) return false;
    if (f.eligibility === 'substantive' && !r.substantive) return false;
    if (f.eligibility === 'excluded' && r.substantive) return false;
    if (search && (/^\d+$/.test(search) ? r.case !== Number(search) : !r.searchText.includes(search))) return false;
    const p = r.passes;
    const astra = p.astra_old || p.astra_new, sol = p.sol_old || p.sol_new;
    switch (f.outcome) {
      case 'repeated': return !!r.repeatabilitySelected;
      case 'rescored': return !!r.scoringChanged;
      case 'any': return astra || sol;
      case 'none': return r.substantive && !astra && !sol;
      case 'astra_only': return astra && !sol;
      case 'sol_only': return sol && !astra;
      case 'pipeline': return p.astra_old !== p.astra_new || p.sol_old !== p.sol_new;
      case 'astra_pipeline': return p.astra_old !== p.astra_new;
      case 'sol_pipeline': return p.sol_old !== p.sol_new;
      default: return true;
    }
  });
}

function adjacentCase(filtered, selected, delta) {
  const index = filtered.findIndex(r => r.case === selected);
  return filtered[index + delta]?.case ?? null;
}

// Pure functions are also exercised by the no-browser regression check.
if (typeof module !== 'undefined') module.exports = {filterExamples, adjacentCase};

if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  const state = {overview: null, filtered: [], selected: null, detail: null, view: 'predictions', showScores: true, revision: 0};
  const names = {astra: 'GPT-6 Astra', sol: 'GPT-5.6 Sol'};
  const order = ['astra_old', 'sol_old', 'astra_new', 'sol_new'];
  const sec = n => n == null ? '—' : `${n.toFixed(2)} s`;
  const money = n => n == null ? 'Unknown' : `$${n.toFixed(4)}`;
  const time = value => new Date(value).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit'});
  const count = n => n.toLocaleString();

  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function textBlock(text) { return el('pre', text === '' ? '∅ Empty text' : text, 'text'); }
  function card(title, text, cls = '') {
    const root = el('section', null, `card ${cls}`), head = el('div', null, 'card-head'), body = el('div', null, 'card-body');
    head.append(el('h2', title)); body.append(textBlock(text)); root.append(head, body); return root;
  }
  function disclosure(title, node, cls = '') {
    const root = el('details', null, cls), body = el('div'); root.append(el('summary', title)); body.append(node); root.append(body); return root;
  }
  async function getJSON(url) {
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    return response.json();
  }
  function error(message) { $('error').hidden = false; $('error').textContent = message; }
  function setHash() { history.replaceState(null, '', `#case=${state.selected || ''}&view=${state.view}`); }
  function controls() { return Object.fromEntries(['search', 'app', 'day', 'eligibility', 'outcome'].map(k => [k, $(k).value])); }

  function renderList() {
    const list = $('examples'); list.replaceChildren();
    $('count').textContent = `${count(state.filtered.length)} of ${count(state.overview.examples.length)} examples · J / K to navigate`;
    for (const row of state.filtered) {
      const button = el('button', null, `example${row.case === state.selected ? ' selected' : ''}`);
      button.dataset.case = row.case;
      if (row.case === state.selected) button.setAttribute('aria-current', 'true');
      const head = el('div', null, 'example-head'); head.append(el('span', `#${row.case} · ${row.application.replace('Visual Studio ', '').replace('Google ', '')}`), el('span', row.day.slice(5)));
      button.append(head, el('div', row.target, 'preview'));
      const pills = el('div', null, 'pills');
      pills.append(el('span', row.substantive ? 'Scored' : 'Not in score', 'pill'));
      if (state.showScores) {
        if (row.passes.astra_old || row.passes.astra_new) pills.append(el('span', state.view === 'repeatability' ? 'Astra original pass' : 'Astra pass', 'pill astra'));
        if (row.passes.sol_old || row.passes.sol_new) pills.append(el('span', state.view === 'repeatability' ? 'Sol original pass' : 'Sol pass', 'pill sol'));
      }
      button.append(pills); button.addEventListener('click', () => loadCase(row.case)); list.append(button);
    }
    $('previous').disabled = adjacentCase(state.filtered, state.selected, -1) == null;
    $('next').disabled = adjacentCase(state.filtered, state.selected, 1) == null;
    $('random').disabled = !state.filtered.length;
  }

  function applyFilters(preferred = null) {
    state.filtered = filterExamples(state.overview.examples, controls());
    const chosen = state.filtered.find(r => r.case === (preferred || state.selected)) || state.filtered[0];
    renderList();
    if (!chosen) {
      state.revision++; state.selected = null; state.detail = null;
      $('case-title').textContent = 'No matching examples'; $('case-meta').textContent = '';
      $('detail').replaceChildren(el('div', 'Try another filter or reset filters.', 'empty')); return;
    }
    if (chosen.case !== state.selected || !state.detail) loadCase(chosen.case);
  }

  async function loadCase(number) {
    const revision = ++state.revision; state.selected = number; state.detail = null;
    setHash(); renderList(); $('error').hidden = true;
    $('case-title').textContent = `Example #${number}`; $('case-meta').textContent = 'Loading…';
    $('detail').replaceChildren(el('div', 'Loading this example…', 'spinner'));
    try {
      const row = await getJSON(`/api/cases/${number}`);
      if (revision !== state.revision) return;
      state.detail = row;
      $('case-title').textContent = `#${number} · ${row.destination.application}`;
      const destination = row.destination.resourceTitle || row.destination.surfaceLabel || row.destination.surfaceKind;
      $('case-meta').textContent = `${time(row.beganAt)} · ${destination} · Original corpus #${row.originalTargetNumber} · ${row.reused ? 'Original sample prediction' : 'Added full-cohort example'}`;
      renderDetail();
    } catch (e) { if (revision === state.revision) error(`Could not load this example. ${e.message}`); }
  }

  function targetAndQuery(row) {
    const holder = el('div');
    holder.append(card('What you actually wrote', row.target, 'target'));
    if (row.scoringChange) {
      const change = row.scoringChange;
      const items = el('div');
      if (change.classification) items.append(el('p', `Scoring eligibility: ${change.classification.before ? 'included' : 'excluded'} → ${change.classification.after ? 'included' : 'excluded'}. ${change.classification.reason}`));
      for (const g of change.judgments || []) {
        const [model, variant] = g.arm.split('_');
        items.append(el('p', `${names[model]} · ${variant}: ${g.before} → ${g.after}. ${g.reason}`));
      }
      holder.append(disclosure('Scoring revised · see what changed', items, 'card'));
    }
    if (!row.substantive) holder.append(el('div', `Excluded from holistic scoring, not from the dataset. ${row.eligibilityReason}`, 'notice'));
    const q = row.query, cursor = q.cursorContext || {}, contents = el('div');
    if ((cursor.leftContext || '').trim() && ['chat_prompt', 'integrated_terminal'].includes(row.destination.surfaceKind)) {
      holder.append(el('p', 'This prediction starts within an existing draft. The prefix is already supplied in cursor context; a pass does not mean predicting that entire draft from scratch.', 'notice'));
    }
    const grid = el('div', null, 'query-grid');
    for (const [label, value] of [['Before cursor', cursor.leftContext || ''], ['After cursor', cursor.rightContext || '']]) {
      const part = el('div'); part.append(el('h3', label), textBlock(value)); grid.append(part);
    }
    contents.append(grid);
    if (cursor.selectedText) contents.append(el('h3', 'Selected text'), el('pre', cursor.selectedText, 'text selection'));
    if (cursor.surfacePrompt) contents.append(el('p', `Empty-field prompt: ${cursor.surfacePrompt}`, 'muted'));
    if (q.clipboard) contents.append(disclosure('Clipboard available at this write', textBlock(q.clipboard.content ?? 'No text exposed'), 'field'));
    contents.append(disclosure('Exact conditioning JSON', textBlock(JSON.stringify(q, null, 2)), 'field'));
    holder.append(disclosure('Cursor and destination · same for every arm and attempt', contents, 'card'));
    return holder;
  }

  function renderPredictions(row) {
    const fragment = document.createDocumentFragment(); fragment.append(targetAndQuery(row));
    const bar = el('div', null, 'toolbar'); bar.append(el('span', 'Columns: Astra / Sol · Rows: Old / New', 'muted'));
    const label = el('label'), toggle = el('input'); toggle.type = 'checkbox'; toggle.checked = state.showScores;
    label.append(toggle, document.createTextNode('Show judgments'));
    toggle.addEventListener('change', () => { state.showScores = toggle.checked; renderList(); renderDetail(); }); bar.append(label); fragment.append(bar);
    const grid = el('div', null, 'grid');
    for (const arm of order) {
      const [model, variant] = arm.split('_'), prediction = row.predictions[arm];
      const root = card(`${names[model]} · ${variant === 'old' ? 'Old' : 'New'} pipeline`, prediction.prediction, `prediction ${model}`);
      if (state.showScores) {
        root.firstChild.append(el('span', !row.substantive ? 'Not scored' : prediction.semanticPass ? 'Useful · pass' : 'Not useful · fail', `pill${prediction.semanticPass ? ' pass' : ''}`));
        if (prediction.reviewOrigin === 'human_adjudication') root.firstChild.append(el('span', 'Your judgment', 'pill'));
        root.append(disclosure('Why this judgment?', el('p', prediction.reason, 'reason')));
      }
      const metrics = el('div', null, 'metrics');
      metrics.append(el('span', `${sec(prediction.generationLatencySeconds)} latency`), el('span', `${money(prediction.apiEquivalentCostUSD)} API-equivalent`));
      root.append(metrics); grid.append(root);
    }
    fragment.append(grid, el('p', 'Full output shown verbatim. Scores ask whether this would be a desirable prediction of the whole intended write—not merely a related topic. Your explicit judgments override assistant/reviewer grades. Costs are estimates, not subscription charges.', 'muted'));
    fragment.append(disclosure('Target lineage and eligibility', textBlock(JSON.stringify({exampleID: row.exampleID, episode: row.episode, substantive: row.substantive, reason: row.eligibilityReason}, null, 2)), 'card'));
    return fragment;
  }

  function eventContent(event) {
    if (typeof event.content === 'string') return event.content;
    if (Array.isArray(event.authorshipSegments)) return event.authorshipSegments.map(s => s.content || '').join('');
    return JSON.stringify(event, null, 2);
  }

  async function renderContext(row, revision) {
    const root = $('detail'); root.append(targetAndQuery(row));
    root.append(el('p', 'These are the exact retained READ/WRITE blocks, in the order used for each prompt. Each column is shared by Astra and Sol. Differences in available time and retained history are part of the pipeline comparison.', 'notice'));
    const grid = el('div', null, 'grid context-grid'); root.append(grid);
    for (const variant of ['old', 'new']) {
      const column = el('section', null, 'context-column'), meta = row.context[variant];
      column.append(el('h2', `${variant === 'old' ? 'Old' : 'New'} pipeline`), el('p', `${count(meta.referenceInputTokens)} reference tokens · ${meta.retainedEvents} events · ${meta.eventCounts.read || 0} READ / ${meta.eventCounts.write || 0} WRITE`, 'muted'));
      const loading = el('p', 'Loading retained context…', 'spinner'); column.append(loading); grid.append(column);
      getJSON(`/api/cases/${row.case}/context/${variant}`).then(data => {
        if (revision !== state.revision || state.view !== 'context' || state.selected !== row.case) return;
        loading.remove();
        column.append(el('p', `${variant === 'old' ? 'Pane v2 · semantic v14' : 'Pane v7 · semantic v25'} · frozen for this experiment`, 'muted'));
        for (const [i, block] of data.blocks.entries()) {
          const event = block.event, identity = event.destination || event.source || {};
          const title = `${i + 1} · ${block.kind.toUpperCase()} · ${identity.application || ''} · ${time(block.availableAt)}${block.contentTruncated ? ' · truncated by packing' : ''}`;
          const body = el('div');
          body.append(textBlock(eventContent(event)), disclosure('Exact serialized event and provenance', textBlock(block.serialized + '\n\n' + JSON.stringify({eventID: block.eventID, availableAt: block.availableAt, contentTruncated: block.contentTruncated, serializedSHA256: block.serializedSHA256}, null, 2))));
          const eventNode = disclosure(title, body, `event ${block.kind}`); eventNode.open = i >= data.blocks.length - 6; column.append(eventNode);
        }
        const exact = textBlock(data.modelInput); exact.classList.add('exact');
        column.append(disclosure('Exact semantic prompt · instruction + history + conditioning', exact, 'card'), el('p', 'The shared native Codex preamble was added separately by the frozen runner. This view does not reconstruct or alter the prompt.', 'muted'), el('p', `Prompt SHA256: ${data.modelInputSHA256}`, 'hash'));
      }).catch(e => { if (revision === state.revision && state.view === 'context') loading.textContent = `Could not load context: ${e.message}`; });
    }
  }

  async function renderRepeats(row, revision) {
    const root = $('detail'); root.append(targetAndQuery(row));
    const repeated = state.overview.repeatability;
    if (!repeated?.cases.includes(row.case)) {
      root.append(el('p', 'This case is not in the 70-case repeatability check. Choose “Repeatability · 70 selected cases” in the outcome filter.', 'notice'));
      return;
    }
    const loading = el('p', 'Loading repeated answers…', 'spinner'); root.append(loading);
    try {
      const data = await getJSON(`/api/cases/${row.case}/repeats`);
      if (revision !== state.revision || state.view !== 'repeatability') return;
      loading.remove();
      const p = data.progress;
      const bar = el('div', null, 'toolbar');
      bar.append(el('span', `${p.completedFreshAnswers} / ${p.plannedFreshAnswers} fresh answers saved · ${p.scoringComplete ? p.scoringLabel : 'new answers not yet scored'} · ${p.status.replaceAll('_', ' ')}`, 'muted'));
      const refresh = el('button', 'Refresh answers'); refresh.addEventListener('click', () => loadCase(row.case)); bar.append(refresh); root.append(bar);
      root.append(el('p', 'Does the old → new difference persist? Compare successful answers out of three, then inspect the answers and changed READ context. These selected cases are not an overall accuracy estimate.', 'notice'));
      root.append(disclosure('Why this case was selected', el('p', data.selection.reason)));
      for (const model of ['astra', 'sol']) {
        const before = data.arms[model + '_old'], after = data.arms[model + '_new'];
        const countLabel = n => n == null ? 'awaiting scoring' : `${n}/3`;
        root.append(el('h2', `${names[model]} · Old ${countLabel(before.successes)} → New ${countLabel(after.successes)}`, 'section-heading'));
        if (p.scoringComplete) root.append(el('p', `Two fresh answers only: old ${before.freshSuccesses}/2 → new ${after.freshSuccesses}/2.`, 'muted'));
        const grid = el('div', null, 'grid context-grid');
        for (const variant of ['old', 'new']) {
          const column = el('section', null, 'repeat-column'); column.append(el('h3', `${variant === 'old' ? 'Old' : 'New'} pipeline`));
          for (const answer of data.arms[model + '_' + variant].answers) {
            const title = answer.replicate === 1 ? 'Original answer' : `Fresh answer ${answer.replicate - 1}`;
            const cardNode = card(title, answer.status === 'pending' ? 'Not collected yet.' : answer.prediction, `prediction ${model}`);
            cardNode.firstChild.append(el('span', answer.status === 'pending' ? 'Pending' : answer.semanticPass == null ? 'Not yet scored' : answer.semanticPass ? 'Pass' : 'Fail', `pill${answer.semanticPass ? ' pass' : ''}`));
            if (answer.reviewStatus === 'human_adjudication') {
              cardNode.firstChild.append(el('span', 'Your judgment', 'pill'));
              if (answer.previousScoring && answer.previousScoring.semanticPass !== answer.semanticPass)
                cardNode.append(el('p', `Previously ${answer.previousScoring.semanticPass ? 'pass' : 'fail'}; updated from your review. The earlier scoring artifact is preserved.`, 'muted'));
            }
            if (answer.reason) cardNode.append(disclosure('Why this judgment?', el('p', answer.reason, 'reason')));
            if (answer.status !== 'pending') {
              const metrics = el('div', null, 'metrics');
              metrics.append(el('span', `${sec(answer.generationLatencySeconds)} latency`), el('span', `${money(answer.apiEquivalentCostUSD)} API-equivalent`));
              cardNode.append(metrics);
            }
            column.append(cardNode);
          }
          grid.append(column);
        }
        root.append(grid);
      }
    } catch (e) { if (revision === state.revision) loading.textContent = `Could not load repeated answers: ${e.message}`; }
  }

  function renderSummary() {
    const root = document.createDocumentFragment(), summary = state.overview.summary;
    const repeats = state.overview.repeatability;
    if (state.overview.currentJudgmentsApplied) root.append(el('p', 'Up to date with reviewer reconciliation and your judgments. The same answer has the same grade in every view.', 'notice'));
    if (repeats?.scoringComplete) {
      const r = repeats.summary;
      root.append(el('h2', `Repeated answers · ${r.cases} selected cases`, 'section-heading'), el('p', 'One original + two fresh answers per case. “All 3 pass” measures consistency; “Any of 3 pass” means at least one succeeded. This selected set is not full-dataset accuracy.', 'muted'));
      const wrap = el('div', null, 'card table-wrap'), table = el('table'), head = el('thead'), hr = el('tr');
      ['Model / pipeline', 'All 3 pass', 'Any of 3 pass', 'Individual answers pass'].forEach(v => hr.append(el('th', v))); head.append(hr); table.append(head);
      const body = el('tbody');
      for (const model of ['astra', 'sol']) for (const variant of ['old', 'new']) {
        const a = r.arms[state.overview.models[model] + ' / ' + variant], tr = el('tr');
        [`${names[model]} · ${variant}`, `${a.countAllThree}/${a.cases} (${(100*a.allThreeSuccessRate).toFixed(1)}%)`, `${a.countAnyOne}/${a.cases} (${(100*a.anyOfThreeSuccessRate).toFixed(1)}%)`, `${(100*a.singleAnswerPassRate).toFixed(1)}%`].forEach(v => tr.append(el('td', v)));
        body.append(tr);
      }
      table.append(body); wrap.append(table); root.append(wrap);
    }
    root.append(el('h2', 'Original answers · full dataset', 'section-heading'));
    root.append(el('div', `${summary.cases} matched targets × 4 arms = ${summary.predictions} predictions. Holistic scoring uses ${summary.substantiveExamples} substantive targets; ${summary.excludedExamples} other targets remain inspectable. Predictions are unchanged.`, 'notice'));
    if (repeats?.scoringComplete) root.append(el('p', 'These totals use the latest grades for the original answers only. The two fresh answers do not change the full-dataset denominator.', 'muted'));
    const revision = state.overview.scoringRevision;
    if (revision) root.append(el('p', revision.explanation, 'notice'));
    const wrap = el('div', null, 'card table-wrap'), table = el('table'), head = el('thead'), hr = el('tr');
    ['Model / pipeline', 'Useful predictions', 'Pass rate', 'Median latency', 'Mean latency', 'API-equivalent / query'].forEach(v => hr.append(el('th', v))); head.append(hr); table.append(head);
    const body = el('tbody');
    for (const model of ['astra', 'sol']) for (const variant of ['old', 'new']) {
      const m = summary.models[state.overview.models[model] + ' / ' + variant], tr = el('tr');
      [`${names[model]} · ${variant}`, `${m.semanticPasses} / ${m.substantiveExamples}`, `${(100 * m.semanticPassRate).toFixed(1)}%`, sec(m.medianLatencySecondsAllExamples), sec(m.meanLatencySecondsAllExamples), money(m.apiEquivalentCostUSDAllExamples / m.operationalMeasurementExamples)].forEach(v => tr.append(el('td', v))); body.append(tr);
    }
    table.append(body); wrap.append(table); root.append(wrap);
    root.append(el('h2', 'How to read these results', 'section-heading'), el('p', summary.passBar), el('p', 'Latency and cost use all 377 successful queries per arm. Holistic judgments ignore speed. No human-time or real-time usefulness score has been computed for this comparison. API-equivalent dollars are not subscription charges.', 'muted'));
    root.append(el('h2', 'What stayed fixed', 'section-heading'), el('p', 'Same targets, cursor/destination queries, closed WRITE history, 32K reference-token budget, and xhigh effort. Only the READ pipeline and model vary. The new arm is the frozen pane-v7 / semantic-v25 snapshot—not subsequent fixes.'));
    root.append(el('p', 'The original 100 examples were reused; 277 more were sampled. All 1,508 outputs passed integrity checks. Four failed provider attempts were preserved separately; their unreported costs are unknown, not zero.', 'muted'));
    const link = el('a', 'Open the complete report ↗'); link.href = '/report'; link.target = '_blank'; link.rel = 'noreferrer'; root.append(link);
    if (revision) {
      const old = el('a', 'Previous scoring report (preserved) ↗'); old.href = '/previous-report'; old.target = '_blank'; old.rel = 'noreferrer';
      const p = el('p'); p.append(old); root.append(p);
    }
    return root;
  }

  function renderDetail() {
    $('detail').replaceChildren();
    document.querySelectorAll('[data-tab]').forEach(n => n.setAttribute('aria-selected', String(n.dataset.tab === state.view)));
    setHash();
    if (state.view === 'summary') { $('detail').append(renderSummary()); return; }
    if (!state.detail) return;
    if (state.view === 'context') renderContext(state.detail, state.revision);
    else if (state.view === 'repeatability') renderRepeats(state.detail, state.revision);
    else $('detail').append(renderPredictions(state.detail));
  }

  async function init() {
    try {
      state.overview = await getJSON('/api/overview');
      if (state.overview.repeatability) {
        document.querySelector('[data-tab="repeatability"]').hidden = false;
        const cases = new Set(state.overview.repeatability.cases);
        state.overview.examples.forEach(r => r.repeatabilitySelected = cases.has(r.case));
        const option = el('option', `Repeatability · ${cases.size} selected cases`); option.value = 'repeated'; $('outcome').append(option);
      }
      $('run-label').textContent = `Sep 2–4 · 377 writes · 4 arms · ${state.overview.currentJudgmentsApplied ? 'Current reviewed scores' : state.overview.scoringRevision ? 'Reconciled scoring v3' : 'Scoring v2'} · read-only`;
      if (state.overview.scoringRevision) {
        const option = el('option', 'Scoring revised'); option.value = 'rescored'; $('outcome').append(option);
      }
      for (const [id, key] of [['app', 'application'], ['day', 'day']]) {
        for (const value of [...new Set(state.overview.examples.map(r => r[key]))].sort()) {
          const option = el('option', value); option.value = value; $(id).append(option);
        }
      }
      const params = new URLSearchParams(location.hash.slice(1));
      if (['predictions', 'context', 'summary', 'repeatability'].includes(params.get('view'))) state.view = params.get('view');
      if (state.view === 'repeatability' && state.overview.repeatability) $('outcome').value = 'repeated';
      applyFilters(Number(params.get('case')) || null);
    } catch (e) { error(`Unable to open the frozen results: ${e.message}`); }
  }
  for (const id of ['search', 'app', 'day', 'eligibility', 'outcome']) $(id).addEventListener(id === 'search' ? 'input' : 'change', () => applyFilters());
  $('reset').addEventListener('click', () => { ['search', 'app', 'day'].forEach(k => $(k).value = ''); ['eligibility', 'outcome'].forEach(k => $(k).value = 'all'); applyFilters(); });
  $('random').addEventListener('click', () => { if (state.filtered.length) loadCase(state.filtered[Math.floor(Math.random() * state.filtered.length)].case); });
  for (const [id, delta] of [['previous', -1], ['next', 1]]) $(id).addEventListener('click', () => { const n = adjacentCase(state.filtered, state.selected, delta); if (n != null) loadCase(n); });
  document.querySelectorAll('[data-tab]').forEach(node => node.addEventListener('click', () => { state.view = node.dataset.tab; renderDetail(); }));
  document.addEventListener('keydown', e => { if (['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'SUMMARY'].includes(document.activeElement?.tagName) || e.ctrlKey || e.metaKey || e.altKey) return; if (e.key.toLowerCase() === 'j') $('next').click(); if (e.key.toLowerCase() === 'k') $('previous').click(); });
  window.addEventListener('hashchange', () => { const p = new URLSearchParams(location.hash.slice(1)); const number = Number(p.get('case')); if (['predictions', 'context', 'summary', 'repeatability'].includes(p.get('view'))) state.view = p.get('view'); if (state.overview?.examples.some(r => r.case === number)) { if (!state.filtered.some(r => r.case === number)) $('reset').click(); loadCase(number); } });
  init();
}
