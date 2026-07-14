"""Simple trade confirmation desk (HTML) — Stocks + Options tabs."""

DESK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Khabari Desk</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #1a222c;
      --text: #e8eef4;
      --muted: #8b9aab;
      --buy: #1f9d55;
      --sell: #d64545;
      --hold: #c4a035;
      --line: #2a3644;
      --accent: #3d7ea6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; font-family: "Segoe UI", system-ui, sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1c2a3a, var(--bg));
      color: var(--text); padding: 2rem 1rem;
    }
    main { max-width: 640px; margin: 0 auto; }
    h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: .02em; }
    .sub { color: var(--muted); margin-bottom: 1rem; }
    .tabs { display: flex; gap: .5rem; margin-bottom: 1.25rem; }
    .tab {
      flex: 1; text-align: center; padding: .7rem 1rem; border-radius: 10px;
      border: 1px solid var(--line); background: #121820; color: var(--muted);
      cursor: pointer; font-weight: 650; font-size: .95rem;
    }
    .tab.active { background: var(--accent); color: white; border-color: var(--accent); }
    .pane { display: none; }
    .pane.active { display: block; }
    .card {
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 14px; padding: 1.25rem 1.35rem; margin-bottom: 1rem;
    }
    .action { font-size: 1.35rem; font-weight: 700; }
    .action.BUY, .action.BUY_TO_OPEN { color: var(--buy); }
    .action.SELL, .action.SELL_TO_CLOSE { color: var(--sell); }
    .action.HOLD { color: var(--hold); }
    .meta { color: var(--muted); font-size: .95rem; margin-top: .35rem; }
    ul { margin: .75rem 0 0; padding-left: 1.1rem; }
    li { margin: .25rem 0; }
    .row { display: flex; gap: .75rem; flex-wrap: wrap; margin-top: 1.1rem; }
    button {
      border: 0; border-radius: 10px; padding: .75rem 1.1rem; font-weight: 650;
      cursor: pointer; font-size: .95rem;
    }
    .btn-yes { background: var(--buy); color: white; }
    .btn-no { background: #3a4656; color: var(--text); }
    .status { margin-top: 1rem; min-height: 1.4rem; color: var(--muted); }
    table { width: 100%; border-collapse: collapse; font-size: .92rem; }
    th, td { text-align: left; padding: .45rem 0; border-bottom: 1px solid var(--line); }
    th { color: var(--muted); font-weight: 600; }
    input[type=text], input[type=number] {
      width: 100%; max-width: 12rem; padding: .7rem .8rem; border-radius: 10px; border: 1px solid var(--line);
      background: #121820; color: var(--text); font-size: .95rem; margin: .5rem 0 .75rem;
    }
    input#watch-input, input#opt-watch-input { max-width: 100%; }
    .btn-save { background: #3d7ea6; color: white; }
  </style>
</head>
<body>
  <main>
    <h1>Khabari Desk</h1>
    <p class="sub">Confirm trades and manage watchlists. Stocks and Options are separate paper books.</p>

    <div class="tabs">
      <button type="button" class="tab" id="tab-stocks" onclick="setTab('stocks')">Stocks</button>
      <button type="button" class="tab" id="tab-options" onclick="setTab('options')">Options</button>
    </div>

    <div id="pane-stocks" class="pane">
      <section class="card" id="rec-card">
        <div class="meta">Loading recommendation…</div>
      </section>
      <section class="card">
        <div style="font-weight:650;margin-bottom:.5rem">Your portfolio</div>
        <div id="portfolio">Loading…</div>
      </section>
      <section class="card">
        <div style="font-weight:650">Stocks you care about</div>
        <div class="meta">Comma-separated tickers. The agent only analyzes this list (+ any holdings).</div>
        <input id="watch-input" type="text" placeholder="AAPL, NVDA, MSFT" />
        <div class="row">
          <button class="btn-save" onclick="saveWatchlist()">Save watchlist</button>
        </div>
        <div class="status" id="watch-status"></div>
      </section>
    </div>

    <div id="pane-options" class="pane">
      <section class="card" id="opt-rec-card">
        <div class="meta">Loading options recommendation…</div>
      </section>
      <section class="card">
        <div style="font-weight:650;margin-bottom:.5rem">Options paper book</div>
        <div id="opt-portfolio">Loading…</div>
      </section>
      <section class="card">
        <div style="font-weight:650">Options underlyings</div>
        <div class="meta">Comma-separated tickers for long call/put scans (separate from stocks).</div>
        <input id="opt-watch-input" type="text" placeholder="AAPL, NVDA, TSLA" />
        <div class="row">
          <button class="btn-save" onclick="saveOptWatchlist()">Save options watchlist</button>
        </div>
        <div class="status" id="opt-watch-status"></div>
      </section>
    </div>
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const id = params.get('id');
    let tab = (params.get('tab') || 'stocks').toLowerCase();
    if (tab !== 'options') tab = 'stocks';

    function setTab(next) {
      tab = next === 'options' ? 'options' : 'stocks';
      document.getElementById('tab-stocks').classList.toggle('active', tab === 'stocks');
      document.getElementById('tab-options').classList.toggle('active', tab === 'options');
      document.getElementById('pane-stocks').classList.toggle('active', tab === 'stocks');
      document.getElementById('pane-options').classList.toggle('active', tab === 'options');
      const u = new URL(location.href);
      u.searchParams.set('tab', tab);
      if (id) u.searchParams.set('id', id); else u.searchParams.delete('id');
      history.replaceState({}, '', u);
      if (tab === 'options') {
        loadOptRec();
        loadOptPortfolio();
        loadOptWatchlist();
      } else {
        loadRec();
        loadPortfolio();
        loadWatchlist();
      }
    }

    async function loadWatchlist() {
      const d = await fetch('/watchlist').then(r => r.json());
      document.getElementById('watch-input').value = (d.tickers || []).join(', ');
    }

    async function saveWatchlist() {
      const status = document.getElementById('watch-status');
      const tickers = document.getElementById('watch-input').value;
      status.textContent = 'Saving…';
      const r = await fetch('/watchlist', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ tickers })
      });
      const d = await r.json();
      status.textContent = r.ok ? d.message + ': ' + d.tickers.join(', ') : (d.detail || JSON.stringify(d));
      if (r.ok) document.getElementById('watch-input').value = d.tickers.join(', ');
    }

    async function loadPortfolio() {
      const p = await fetch('/portfolio/marked').then(r => r.json());
      let html = `<div class="meta">Cash: $${Number(p.cash).toFixed(2)} · Total: $${Number(p.total_value).toFixed(2)}</div>`;
      const keys = Object.keys(p.positions || {});
      if (!keys.length) {
        html += '<p class="meta">No open positions yet.</p>';
      } else {
        html += '<table><tr><th>Ticker</th><th>Shares</th><th>Price</th><th>P&amp;L</th></tr>';
        for (const t of keys) {
          const x = p.positions[t];
          html += `<tr><td>${t}</td><td>${x.shares}</td><td>$${x.last_price}</td><td>${x.unrealized_pnl} (${x.unrealized_pnl_pct}%)</td></tr>`;
        }
        html += '</table>';
      }
      document.getElementById('portfolio').innerHTML = html;
    }

    async function loadRec() {
      const url = id && tab === 'stocks' ? `/recommendations/${id}` : '/recommendations/pending';
      const r = await fetch(url);
      const card = document.getElementById('rec-card');
      if (!r.ok) {
        card.innerHTML = '<div class="meta">No pending stock recommendation. When the agent alerts you, open the link from ntfy.</div>';
        return;
      }
      const d = await r.json();
      const reasons = (d.reasoning || []).map(x => `<li>${x}</li>`).join('');
      const executed = d.status === 'executed';
      const skipped = d.status === 'skipped';
      const canFix = executed && d.action !== 'HOLD' && d.trade && d.trade.shares > 0;
      const recorded = canFix
        ? `<div class="meta">Recorded: ${d.trade.shares} shares @ $${d.trade.price}${d.trade.dollars != null ? ` ($${d.trade.dollars})` : ''}</div>`
        : '';
      card.innerHTML = `
        <div class="action ${d.action}">${d.action} ${d.ticker}</div>
        <div class="meta">Invest $${d.investment} · Confidence ${d.confidence}% · Risk ${d.risk} · Status: ${d.status || 'pending'}</div>
        ${recorded}
        <ul>${reasons}</ul>
        ${skipped ? '' : `
        ${(!executed || canFix) && d.action !== 'HOLD' ? `
        <label class="meta" for="fill-price">Fill price (per share)</label>
        <input id="fill-price" type="number" step="0.01" min="0.01" />
        <label class="meta" for="fill-shares">Quantity (shares)</label>
        <input id="fill-shares" type="number" step="0.000001" min="0.000001" />
        ` : ''}
        ${!executed ? `
        <div class="row">
          <button class="btn-yes" onclick="confirmTrade('${d.id}')">I did this trade</button>
          <button class="btn-no" onclick="skipTrade('${d.id}')">Skip / ignore</button>
        </div>` : canFix ? `
        <div class="row">
          <button class="btn-yes" onclick="updateTrade('${d.id}')">Save correction</button>
        </div>
        <div class="meta">Use Save correction if price/qty was wrong.</div>` : ''}
        `}
        <div class="status" id="status"></div>
      `;
      if (!skipped && d.action !== 'HOLD') {
        const priceInput = document.getElementById('fill-price');
        const sharesInput = document.getElementById('fill-shares');
        if (executed && d.trade) {
          if (priceInput && d.trade.price) priceInput.value = d.trade.price;
          if (sharesInput && d.trade.shares) sharesInput.value = d.trade.shares;
        } else {
          fetch('/indicators/' + encodeURIComponent(d.ticker)).then(async (pr) => {
            if (!priceInput || !pr.ok) return;
            const ind = await pr.json();
            if (ind.price) {
              priceInput.value = ind.price;
              if (sharesInput && d.investment && Number(ind.price) > 0) {
                sharesInput.value = (Math.round((d.investment / ind.price) * 1e6) / 1e6);
              }
            }
          }).catch(() => {});
        }
      }
    }

    async function confirmTrade(recId) {
      const status = document.getElementById('status');
      status.textContent = 'Updating portfolio…';
      const priceInput = document.getElementById('fill-price');
      const sharesInput = document.getElementById('fill-shares');
      const payload = {};
      if (priceInput && priceInput.value) payload.fill_price = Number(priceInput.value);
      if (sharesInput && sharesInput.value) payload.shares = Number(sharesInput.value);
      const r = await fetch(`/trades/${recId}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      status.textContent = r.ok ? d.message : (d.detail || JSON.stringify(d));
      await loadPortfolio();
      await loadRec();
    }

    async function updateTrade(recId) {
      const status = document.getElementById('status');
      const priceInput = document.getElementById('fill-price');
      const sharesInput = document.getElementById('fill-shares');
      if (!priceInput?.value || !sharesInput?.value) {
        status.textContent = 'Fill price and quantity are required to correct';
        return;
      }
      status.textContent = 'Correcting recorded trade…';
      const r = await fetch(`/trades/${recId}/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fill_price: Number(priceInput.value),
          shares: Number(sharesInput.value),
        }),
      });
      const d = await r.json();
      status.textContent = r.ok ? d.message : (d.detail || JSON.stringify(d));
      await loadPortfolio();
      await loadRec();
    }

    async function skipTrade(recId) {
      const status = document.getElementById('status');
      status.textContent = 'Skipping…';
      const r = await fetch(`/trades/${recId}/skip`, { method: 'POST' });
      const d = await r.json();
      status.textContent = r.ok ? d.message : (d.detail || JSON.stringify(d));
      await loadRec();
    }

    /* ---------- Options pane ---------- */
    async function loadOptWatchlist() {
      const d = await fetch('/options/watchlist').then(r => r.json());
      document.getElementById('opt-watch-input').value = (d.tickers || []).join(', ');
    }

    async function saveOptWatchlist() {
      const status = document.getElementById('opt-watch-status');
      const tickers = document.getElementById('opt-watch-input').value;
      status.textContent = 'Saving…';
      const r = await fetch('/options/watchlist', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ tickers })
      });
      const d = await r.json();
      status.textContent = r.ok ? d.message + ': ' + d.tickers.join(', ') : (d.detail || JSON.stringify(d));
      if (r.ok) document.getElementById('opt-watch-input').value = d.tickers.join(', ');
    }

    async function loadOptPortfolio() {
      const p = await fetch('/options/portfolio/marked').then(r => r.json());
      let html = `<div class="meta">Cash: $${Number(p.cash).toFixed(2)} · Total: $${Number(p.total_value).toFixed(2)}</div>`;
      const keys = Object.keys(p.positions || {});
      if (!keys.length) {
        html += '<p class="meta">No open options positions yet.</p>';
      } else {
        html += '<table><tr><th>Contract</th><th>Qty</th><th>Mark</th><th>P&amp;L</th></tr>';
        for (const k of keys) {
          const x = p.positions[k];
          const label = `${x.underlying} ${(x.right||'').toUpperCase()} $${x.strike} ${x.expiry}`;
          html += `<tr><td>${label}</td><td>${x.contracts}</td><td>$${x.last_premium}</td><td>${x.unrealized_pnl} (${x.unrealized_pnl_pct}%)</td></tr>`;
        }
        html += '</table>';
      }
      document.getElementById('opt-portfolio').innerHTML = html;
    }

    async function loadOptRec() {
      const url = id && tab === 'options' ? `/options/recommendations/${id}` : '/options/recommendations/pending';
      const r = await fetch(url);
      const card = document.getElementById('opt-rec-card');
      if (!r.ok) {
        card.innerHTML = '<div class="meta">No pending options recommendation. Open an Options ntfy link or run POST /options/analyze.</div>';
        return;
      }
      const d = await r.json();
      const reasons = (d.reasoning || []).map(x => `<li>${x}</li>`).join('');
      const executed = d.status === 'executed';
      const skipped = d.status === 'skipped';
      const actionable = d.action === 'BUY_TO_OPEN' || d.action === 'SELL_TO_CLOSE';
      const canFix = executed && actionable && d.trade && d.trade.contracts > 0;
      const contractLine = (d.right && d.strike != null)
        ? `${d.ticker} ${String(d.right).toUpperCase()} $${d.strike} exp ${d.expiry} × ${d.contracts}`
        : `${d.action} ${d.ticker}`;
      const recorded = canFix
        ? `<div class="meta">Recorded: ${d.trade.contracts} contracts @ $${d.trade.premium}${d.trade.dollars != null ? ` ($${d.trade.dollars})` : ''}</div>`
        : '';
      card.innerHTML = `
        <div class="action ${d.action}">${d.action}</div>
        <div class="meta">${contractLine}</div>
        <div class="meta">Premium $${d.investment} · Max loss $${d.max_loss ?? '—'} · Confidence ${d.confidence}% · Risk ${d.risk} · Status: ${d.status || 'pending'}</div>
        ${recorded}
        <ul>${reasons}</ul>
        ${skipped ? '' : `
        ${(!executed || canFix) && actionable ? `
        <label class="meta" for="opt-fill-premium">Fill premium (per share)</label>
        <input id="opt-fill-premium" type="number" step="0.01" min="0.01" />
        <label class="meta" for="opt-fill-contracts">Contracts</label>
        <input id="opt-fill-contracts" type="number" step="1" min="1" />
        ` : ''}
        ${!executed ? `
        <div class="row">
          <button class="btn-yes" onclick="confirmOptTrade('${d.id}')">I did this trade</button>
          <button class="btn-no" onclick="skipOptTrade('${d.id}')">Skip / ignore</button>
        </div>` : canFix ? `
        <div class="row">
          <button class="btn-yes" onclick="updateOptTrade('${d.id}')">Save correction</button>
        </div>
        <div class="meta">Use Save correction if premium/contracts was wrong.</div>` : ''}
        `}
        <div class="status" id="opt-status"></div>
      `;
      if (!skipped && actionable) {
        const prem = document.getElementById('opt-fill-premium');
        const qty = document.getElementById('opt-fill-contracts');
        if (executed && d.trade) {
          if (prem && d.trade.premium) prem.value = d.trade.premium;
          if (qty && d.trade.contracts) qty.value = d.trade.contracts;
        } else {
          if (prem && d.premium) prem.value = d.premium;
          if (qty && d.contracts) qty.value = d.contracts;
        }
      }
    }

    async function confirmOptTrade(recId) {
      const status = document.getElementById('opt-status');
      status.textContent = 'Updating options portfolio…';
      const prem = document.getElementById('opt-fill-premium');
      const qty = document.getElementById('opt-fill-contracts');
      const payload = {};
      if (prem && prem.value) payload.fill_premium = Number(prem.value);
      if (qty && qty.value) payload.contracts = Number(qty.value);
      const r = await fetch(`/options/trades/${recId}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      status.textContent = r.ok ? d.message : (d.detail || JSON.stringify(d));
      await loadOptPortfolio();
      await loadOptRec();
    }

    async function updateOptTrade(recId) {
      const status = document.getElementById('opt-status');
      const prem = document.getElementById('opt-fill-premium');
      const qty = document.getElementById('opt-fill-contracts');
      if (!prem?.value || !qty?.value) {
        status.textContent = 'Fill premium and contracts are required to correct';
        return;
      }
      status.textContent = 'Correcting recorded options trade…';
      const r = await fetch(`/options/trades/${recId}/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fill_premium: Number(prem.value),
          contracts: Number(qty.value),
        }),
      });
      const d = await r.json();
      status.textContent = r.ok ? d.message : (d.detail || JSON.stringify(d));
      await loadOptPortfolio();
      await loadOptRec();
    }

    async function skipOptTrade(recId) {
      const status = document.getElementById('opt-status');
      status.textContent = 'Skipping…';
      const r = await fetch(`/options/trades/${recId}/skip`, { method: 'POST' });
      const d = await r.json();
      status.textContent = r.ok ? d.message : (d.detail || JSON.stringify(d));
      await loadOptRec();
    }

    setTab(tab);
  </script>
</body>
</html>
"""
