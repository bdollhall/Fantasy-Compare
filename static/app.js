(() => {
  const $ = (s) => document.querySelector(s);
  const a = $('#playerA'), b = $('#playerB'), suggestA = $('#suggestA'), suggestB = $('#suggestB');
  const btn = $('#compareBtn'), results = $('#results'), loading = $('#loading'), msg = $('#formMessage');
  const playerAImage = $("#playerAImage"), playerBImage = $("#playerBImage");
  let scoring = 'half-ppr';

  document.querySelectorAll('[data-score]').forEach(x => x.addEventListener('click', () => {
    document.querySelectorAll('[data-score]').forEach(y => y.classList.remove('active'));
    x.classList.add('active'); scoring = x.dataset.score;
  }));

  function escapeHtml(s='') { return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
  function impactClass(n) { return n > .15 ? 'pos' : n < -.15 ? 'neg' : 'neutral'; }
  function impactLabel(n) { return n > .15 ? 'ADVANTAGE' : n < -.15 ? 'CONCERN' : 'NEUTRAL'; }

  async function search(input, box) {
    const q = input.value.trim();
    if (q.length < 2) { box.classList.add('hidden'); return; }
    try {
      const r = await fetch('/api/players?q=' + encodeURIComponent(q));
      const data = await r.json();
      box.innerHTML = '';
      (data.players || []).slice(0, 8).forEach(p => {
        const el = document.createElement('button'); el.type = 'button'; el.className = 'suggestion';
        el.innerHTML = `<span>${escapeHtml(p.name)}</span><small>${escapeHtml(p.position)} · ${escapeHtml(p.team || '—')}</small>`;
        el.addEventListener('click', () => { input.value = p.name; box.classList.add('hidden'); });
        box.appendChild(el);
      });
      box.classList.toggle('hidden', !box.children.length);
    } catch { box.classList.add('hidden'); }
  }

  let timers = new Map();
  [[a,suggestA],[b,suggestB]].forEach(([input,box]) => input.addEventListener('input', () => {
    clearTimeout(timers.get(input)); timers.set(input, setTimeout(() => search(input, box), 180));
  }));
  document.addEventListener('click', e => { if (!e.target.closest('.search-wrap')) { suggestA.classList.add('hidden'); suggestB.classList.add('hidden'); } });

  function newsCards(player) {
    if (!player.news?.length) return `<div class="source-card"><div class="meta">No recent headline results were returned for ${escapeHtml(player.player.name)}.</div></div>`;
    return player.news.slice(0,3).map(n => `<div class="source-card"><a href="${escapeHtml(n.url)}" target="_blank" rel="noopener">${escapeHtml(n.title)}</a><div class="source-meta"><span>${escapeHtml(n.source)}</span><span>${escapeHtml(n.published)}</span></div></div>`).join('');
  }

  function render(data) {
    const win = data.winner === 'a' ? data.a : data.b;
    const lose = data.winner === 'a' ? data.b : data.a;
    const wp = data.winner === 'a' ? data.probability_a : data.probability_b;
    const lp = 100 - wp;
    const reasons = win.reasons.map(r => `<div class="analysis-card"><div class="analysis-head"><strong>${escapeHtml(r.factor)}</strong><span class="impact ${impactClass(r.impact)}">${impactLabel(r.impact)}</span></div><p>${escapeHtml(r.detail)}</p></div>`).join('');
    const sourceMap = new Map();
    [...data.a.sources,...data.b.sources].forEach(s => sourceMap.set(s.url, s));
    const sources = [...sourceMap.values()].map(s => `<div class="source-card"><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.label)}</a><div class="source-meta"><span>Data source</span><span>${escapeHtml(s.updated)}</span></div></div>`).join('');
    results.innerHTML = `
      <div class="result-card">
        <div class="topline"><div><div class="kicker">More likely to score more · Week ${data.week}</div><div class="winner-name">${escapeHtml(win.player.name)}</div><div class="meta">${escapeHtml(win.player.position)} · ${escapeHtml(win.player.team || '')} vs ${escapeHtml(win.opponent || 'TBD')} · ${escapeHtml(data.scoring.toUpperCase())}</div></div><div class="prob"><strong>${wp.toFixed(0)}%</strong><span>chance to outscore ${escapeHtml(lose.player.name)}</span></div></div>
        <div class="bar"><div style="width:${wp}%"></div></div><div class="versus-prob"><span>${escapeHtml(win.player.name)} ${wp.toFixed(1)}%</span><span>${escapeHtml(lose.player.name)} ${lp.toFixed(1)}%</span></div>
        <div class="projection-grid"><div class="statbox"><span>Floor</span><strong>${win.floor.toFixed(1)}</strong></div><div class="statbox"><span>Median</span><strong>${win.median.toFixed(1)}</strong></div><div class="statbox"><span>Ceiling</span><strong>${win.ceiling.toFixed(1)}</strong></div></div>
        <div class="player-compare"><div class="mini-player"><b>${escapeHtml(data.a.player.name)}</b><strong>${data.a.projection.toFixed(1)}</strong><span>projected points</span></div><div class="mini-player"><b>${escapeHtml(data.b.player.name)}</b><strong>${data.b.projection.toFixed(1)}</strong><span>projected points</span></div></div>
        <div class="projection-line"><span>Model confidence</span><strong class="confidence">${escapeHtml(data.confidence)}</strong></div>
        <div class="timestamp">Generated ${new Date(data.generated_at).toLocaleString()} · Refreshes on every comparison</div>
      </div>
      <h2 class="section-title">Why ${escapeHtml(win.player.name)} leads</h2><div class="analysis-list">${reasons}</div>
      <h2 class="section-title">Fresh context</h2><div class="news-list">${newsCards(win)}${newsCards(lose)}</div>
      <h2 class="section-title">Sources used</h2><div class="sources-list">${sources}</div>
      <p class="method"><strong>How the percentage works:</strong> ${escapeHtml(data.method)} ${escapeHtml(data.disclaimer)}</p>`;
    results.classList.remove('hidden');
  }

  btn.addEventListener('click', async () => {
    msg.textContent = ''; results.classList.add('hidden');
    if (!a.value.trim() || !b.value.trim()) { msg.textContent = 'Choose two players first.'; return; }
    btn.disabled = true; loading.classList.remove('hidden');
    try {
      const r = await fetch('/api/compare', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player_a:a.value.trim(),player_b:b.value.trim(),scoring})});
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Comparison failed');
      render(data);
    } catch (e) { msg.textContent = e.message || 'Something went wrong.'; }
    finally { loading.classList.add('hidden'); btn.disabled = false; }
  });
})();
