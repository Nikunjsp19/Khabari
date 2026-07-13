"""Simple trade confirmation desk (HTML)."""

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
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; font-family: "Segoe UI", system-ui, sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1c2a3a, var(--bg));
      color: var(--text); padding: 2rem 1rem;
    }
    main { max-width: 640px; margin: 0 auto; }
    h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: .02em; }
    .sub { color: var(--muted); margin-bottom: 1.5rem; }
    .card {
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 14px; padding: 1.25rem 1.35rem; margin-bottom: 1rem;
    }
    .action { font-size: 1.35rem; font-weight: 700; }
    .action.BUY { color: var(--buy); }
    .action.SELL { color: var(--sell); }
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
    input[type=text] {
      width: 100%; padding: .7rem .8rem; border-radius: 10px; border: 1px solid var(--line);
      background: #121820; color: var(--text); font-size: .95rem; margin-top: .5rem;
    }
    .btn-save { background: #3d7ea6; color: white; }
  </style>
</head>
<body>
  <main>
    <h1>Khabari Desk</h1>
    <p class="sub">Confirm trades and manage the stocks the agent watches.</p>

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
  </main>
  <script>
    const params = new URLSearchParams(location.search);
    const id = params.get('id');

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
      const url = id ? `/recommendations/${id}` : '/recommendations/pending';
      const r = await fetch(url);
      const card = document.getElementById('rec-card');
      if (!r.ok) {
        card.innerHTML = '<div class="meta">No pending recommendation. When the agent alerts you, open the link from ntfy.</div>';
        return;
      }
      const d = await r.json();
      const reasons = (d.reasoning || []).map(x => `<li>${x}</li>`).join('');
      const done = d.status === 'executed' || d.status === 'skipped';
      card.innerHTML = `
        <div class="action ${d.action}">${d.action} ${d.ticker}</div>
        <div class="meta">Invest $${d.investment} · Confidence ${d.confidence}% · Risk ${d.risk} · Status: ${d.status || 'pending'}</div>
        <ul>${reasons}</ul>
        ${done ? '' : `
        <div class="row">
          <button class="btn-yes" onclick="confirmTrade('${d.id}')">I did this trade</button>
          <button class="btn-no" onclick="skipTrade('${d.id}')">Skip / ignore</button>
        </div>`}
        <div class="status" id="status"></div>
      `;
    }

    async function confirmTrade(recId) {
      const status = document.getElementById('status');
      status.textContent = 'Updating portfolio…';
      const r = await fetch(`/trades/${recId}/execute`, { method: 'POST' });
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

    loadRec();
    loadPortfolio();
    loadWatchlist();
  </script>
</body>
</html>
"""
