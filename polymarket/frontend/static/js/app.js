// Polymarket Copy Trading Research — SPA Frontend
const app = document.getElementById('app');

// ── API helpers ──────────────────────────────────────────────────────────────

async function api(path) {
    const res = await fetch(`/api${path}`);
    if (!res.ok) throw new Error(`API error: ${res.status}`);
    return res.json();
}

async function apiPost(path, body) {
    const res = await fetch(`/api${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`API error: ${res.status}`);
    return res.json();
}

function tierBadge(tier) {
    return `<span class="tier-badge tier-${tier}">${tier}</span>`;
}

function truncateWallet(w) {
    return w ? w.slice(0, 6) + '...' + w.slice(-4) : '';
}

function formatDate(ts) {
    return new Date(ts * 1000).toLocaleDateString();
}

// ── Router ───────────────────────────────────────────────────────────────────

function getRoute() {
    const hash = location.hash || '#/';
    return hash.slice(1);
}

async function route() {
    const path = getRoute();
    document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('nav-active'));

    if (path === '/' || path === '') {
        document.querySelector('[data-page="dashboard"]')?.classList.add('nav-active');
        await renderDashboard();
    } else if (path === '/traders') {
        document.querySelector('[data-page="traders"]')?.classList.add('nav-active');
        await renderTraders();
    } else if (path.startsWith('/traders/')) {
        document.querySelector('[data-page="traders"]')?.classList.add('nav-active');
        await renderTraderDetail(path.split('/traders/')[1]);
    } else if (path === '/backtest') {
        document.querySelector('[data-page="backtest"]')?.classList.add('nav-active');
        await renderBacktest();
    } else if (path === '/reports') {
        document.querySelector('[data-page="reports"]')?.classList.add('nav-active');
        await renderReports();
    } else {
        app.innerHTML = '<p class="text-gray-500">Page not found</p>';
    }
}

window.addEventListener('hashchange', route);
window.addEventListener('load', route);

// ── Dashboard ────────────────────────────────────────────────────────────────

async function renderDashboard() {
    app.innerHTML = '<p class="text-gray-400">Loading dashboard...</p>';
    try {
        const [report, status] = await Promise.all([
            api('/reports/latest').catch(() => null),
            api('/pipeline/status'),
        ]);

        let html = '<div class="space-y-6">';

        // Status card
        html += `<div class="card">
            <div class="flex justify-between items-center">
                <div>
                    <h2 class="text-xl font-bold text-gray-800">Dashboard</h2>
                    <p class="text-sm text-gray-500 mt-1">
                        Pipeline: ${status.running ? '<span class="text-green-600 font-medium">Running</span>' : '<span class="text-gray-400">Idle</span>'}
                        ${status.last_run ? ` | Last run: ${new Date(status.last_run).toLocaleString()}` : ''}
                    </p>
                </div>
                <button onclick="triggerPipeline()" class="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">
                    Run Pipeline
                </button>
            </div>
        </div>`;

        // Latest report
        if (report) {
            html += `<div class="card">
                <h3 class="text-lg font-semibold mb-3">Latest Report — ${report.report_date}</h3>
                <p class="text-sm text-gray-500 mb-4">${report.traders_scanned} scored | ${report.traders_passing} passing</p>
                <div class="overflow-x-auto"><table class="w-full text-sm">
                    <thead><tr class="text-left text-gray-500 border-b">
                        <th class="pb-2">#</th><th class="pb-2">Tier</th><th class="pb-2">Name</th>
                        <th class="pb-2">Score</th><th class="pb-2">ROI</th><th class="pb-2">Win Rate</th>
                        <th class="pb-2">Liq</th><th class="pb-2">Wallet</th>
                    </tr></thead><tbody>`;

            for (const t of report.top_10 || []) {
                html += `<tr class="border-b hover:bg-gray-50 cursor-pointer" onclick="location.hash='#/traders/${t.proxy_wallet}'">
                    <td class="py-2">${t.rank}</td>
                    <td>${tierBadge(t.tier)}</td>
                    <td class="font-medium">${t.name || '?'}</td>
                    <td>${t.composite_score}</td>
                    <td class="${t.roi >= 0 ? 'text-green-600' : 'text-red-600'}">${t.roi}%</td>
                    <td>${t.win_rate}%</td>
                    <td>${t.liquidity_score}%</td>
                    <td class="font-mono text-xs text-gray-400">${t.proxy_wallet}</td>
                </tr>`;
            }
            html += '</tbody></table></div></div>';
        } else {
            html += '<div class="card"><p class="text-gray-500">No reports yet. Run the pipeline to generate your first report.</p></div>';
        }

        html += '</div>';
        app.innerHTML = html;
    } catch (e) {
        app.innerHTML = `<div class="card"><p class="text-red-500">Error: ${e.message}</p></div>`;
    }
}

async function triggerPipeline() {
    try {
        const res = await apiPost('/pipeline/trigger', {});
        alert(res.status === 'started' ? 'Pipeline started!' : 'Pipeline already running.');
    } catch (e) {
        alert('Failed: ' + e.message);
    }
}

// ── Traders List ─────────────────────────────────────────────────────────────

async function renderTraders() {
    app.innerHTML = '<p class="text-gray-400">Loading traders...</p>';
    try {
        const traders = await api('/traders?limit=50');
        let html = '<div class="card"><h2 class="text-xl font-bold mb-4">Scored Traders</h2>';
        html += '<div class="overflow-x-auto"><table class="w-full text-sm">';
        html += `<thead><tr class="text-left text-gray-500 border-b">
            <th class="pb-2">#</th><th class="pb-2">Tier</th><th class="pb-2">Name</th>
            <th class="pb-2">Score</th><th class="pb-2">ROI</th><th class="pb-2">WR</th>
            <th class="pb-2">PF</th><th class="pb-2">Sharpe</th><th class="pb-2">Trades</th>
            <th class="pb-2">Liq</th><th class="pb-2">Flags</th>
        </tr></thead><tbody>`;

        for (const t of traders) {
            const flagStr = (t.red_flags || []).length > 0
                ? `<span class="text-red-500">${t.red_flags.length}</span>` : '-';
            html += `<tr class="border-b hover:bg-gray-50 cursor-pointer" onclick="location.hash='#/traders/${t.proxy_wallet}'">
                <td class="py-2">${t.rank}</td>
                <td>${tierBadge(t.tier)}</td>
                <td class="font-medium">${t.name || truncateWallet(t.proxy_wallet)}</td>
                <td>${t.composite_score}</td>
                <td class="${t.roi >= 0 ? 'text-green-600' : 'text-red-600'}">${t.roi}%</td>
                <td>${t.win_rate}%</td>
                <td>${t.profit_factor}</td>
                <td>${t.sharpe_ratio}</td>
                <td>${t.trade_count}</td>
                <td>${t.liquidity_score}%</td>
                <td>${flagStr}</td>
            </tr>`;
        }
        html += '</tbody></table></div></div>';
        app.innerHTML = html;
    } catch (e) {
        app.innerHTML = `<div class="card"><p class="text-red-500">Error: ${e.message}</p></div>`;
    }
}

// ── Trader Detail ────────────────────────────────────────────────────────────

async function renderTraderDetail(wallet) {
    app.innerHTML = '<p class="text-gray-400">Loading trader...</p>';
    try {
        const t = await api(`/traders/${wallet}`);

        let html = `<div class="space-y-6">
            <div class="card">
                <div class="flex justify-between items-start">
                    <div>
                        <h2 class="text-xl font-bold">${t.name || 'Unknown'} ${tierBadge(t.tier)}</h2>
                        <p class="font-mono text-sm text-gray-400 mt-1 cursor-pointer" onclick="navigator.clipboard.writeText('${t.proxy_wallet}').then(()=>alert('Wallet copied!'))">${t.proxy_wallet} (click to copy)</p>
                        ${t.bio ? `<p class="text-sm text-gray-600 mt-2">${t.bio}</p>` : ''}
                    </div>
                    <div class="text-right">
                        <div class="text-3xl font-bold text-blue-600">${t.composite_score}</div>
                        <div class="text-sm text-gray-500">Composite Score</div>
                    </div>
                </div>
            </div>`;

        // Metrics grid
        html += `<div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            ${metricCard('ROI', `${t.roi}%`, t.roi >= 0 ? 'green' : 'red')}
            ${metricCard('Win Rate', `${t.win_rate}%`, t.win_rate >= 55 ? 'green' : 'gray')}
            ${metricCard('Profit Factor', t.profit_factor, t.profit_factor >= 1.5 ? 'green' : 'gray')}
            ${metricCard('Sharpe', t.sharpe_ratio, t.sharpe_ratio >= 1 ? 'green' : 'gray')}
            ${metricCard('Max Drawdown', `${t.max_drawdown}%`, 'red')}
            ${metricCard('Recovery Factor', t.recovery_factor, 'blue')}
            ${metricCard('Consistency', t.consistency_score, 'blue')}
            ${metricCard('Liquidity', `${t.liquidity_score}%`, t.liquidity_score >= 70 ? 'green' : 'red')}
            ${metricCard('Markets', t.unique_markets, 'blue')}
            ${metricCard('Trades', t.trade_count, 'blue')}
            ${metricCard('Active Days', t.active_days, 'blue')}
            ${metricCard('Volume', `$${(t.total_volume / 1000).toFixed(1)}k`, 'blue')}
        </div>`;

        // Red flags
        if (t.red_flags && t.red_flags.length > 0) {
            html += `<div class="card bg-red-50 border border-red-200">
                <h3 class="text-sm font-semibold text-red-700 mb-2">Red Flags</h3>
                <div class="flex flex-wrap gap-2">${t.red_flags.map(f => `<span class="bg-red-100 text-red-800 text-xs px-2 py-1 rounded">${f}</span>`).join('')}</div>
            </div>`;
        }

        // Checklist status
        html += `<div class="card ${t.passes_checklist ? 'bg-green-50 border border-green-200' : 'bg-yellow-50 border border-yellow-200'}">
            <span class="text-sm font-semibold ${t.passes_checklist ? 'text-green-700' : 'text-yellow-700'}">
                ${t.passes_checklist ? 'PASSES checklist — eligible for copy trading' : 'DOES NOT pass minimum checklist'}
            </span>
        </div>`;

        // Backtest button
        html += `<div class="card">
            <button onclick="location.hash='#/backtest?wallet=${wallet}'" class="bg-blue-600 text-white px-6 py-2 rounded hover:bg-blue-700">
                Run Backtest for this Trader
            </button>
        </div>`;

        html += '</div>';
        app.innerHTML = html;
    } catch (e) {
        app.innerHTML = `<div class="card"><p class="text-red-500">Error: ${e.message}</p></div>`;
    }
}

function metricCard(label, value, color) {
    const colorClass = { green: 'text-green-600', red: 'text-red-600', blue: 'text-blue-600', gray: 'text-gray-800' }[color] || 'text-gray-800';
    return `<div class="card text-center">
        <div class="text-2xl font-bold ${colorClass}">${value}</div>
        <div class="text-xs text-gray-500 mt-1">${label}</div>
    </div>`;
}

// ── Backtest ─────────────────────────────────────────────────────────────────

async function renderBacktest() {
    const params = new URLSearchParams(location.hash.split('?')[1] || '');
    const prefillWallet = params.get('wallet') || '';

    app.innerHTML = `<div class="space-y-6">
        <div class="card">
            <h2 class="text-xl font-bold mb-4">Copy Trading Backtest</h2>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm text-gray-600 mb-1">Wallet Address</label>
                    <input id="bt-wallet" type="text" value="${prefillWallet}" placeholder="0x..." class="w-full border rounded px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-sm text-gray-600 mb-1">Initial Capital ($)</label>
                    <input id="bt-capital" type="number" value="3000" min="100" class="w-full border rounded px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-sm text-gray-600 mb-1">Position Size (%)</label>
                    <input id="bt-pct" type="number" value="2" min="0.1" max="50" step="0.1" class="w-full border rounded px-3 py-2 text-sm">
                </div>
                <div>
                    <label class="block text-sm text-gray-600 mb-1">Slippage (bps)</label>
                    <input id="bt-slip" type="number" value="30" min="0" max="500" class="w-full border rounded px-3 py-2 text-sm">
                </div>
            </div>
            <button onclick="submitBacktest()" class="mt-4 bg-blue-600 text-white px-6 py-2 rounded hover:bg-blue-700">Run Backtest</button>
        </div>
        <div id="bt-results"></div>
    </div>`;
}

async function submitBacktest() {
    const wallet = document.getElementById('bt-wallet').value;
    const capital = parseFloat(document.getElementById('bt-capital').value);
    const pct = parseFloat(document.getElementById('bt-pct').value) / 100;
    const slip = parseInt(document.getElementById('bt-slip').value);

    if (!wallet) return alert('Enter a wallet address');

    const resultsDiv = document.getElementById('bt-results');
    resultsDiv.innerHTML = '<p class="text-gray-400">Submitting backtest...</p>';

    try {
        const bt = await apiPost('/backtests', {
            wallet, initial_capital: capital, position_pct: pct, slippage_bps: slip,
        });

        resultsDiv.innerHTML = '<p class="text-gray-400">Backtest running... Polling for results...</p>';

        // Poll for results
        let attempts = 0;
        const poll = setInterval(async () => {
            attempts++;
            try {
                const result = await api(`/backtests/${bt.id}`);
                if (result.final_capital > 0 || attempts > 60) {
                    clearInterval(poll);
                    renderBacktestResults(result);
                }
            } catch (e) {
                if (attempts > 60) clearInterval(poll);
            }
        }, 3000);
    } catch (e) {
        resultsDiv.innerHTML = `<p class="text-red-500">Error: ${e.message}</p>`;
    }
}

function renderBacktestResults(bt) {
    const resultsDiv = document.getElementById('bt-results');
    const returnColor = bt.total_return >= 0 ? 'text-green-600' : 'text-red-600';

    let html = `<div class="card">
        <h3 class="text-lg font-semibold mb-4">Backtest Results</h3>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            ${metricCard('Return', `${bt.total_return}%`, bt.total_return >= 0 ? 'green' : 'red')}
            ${metricCard('Final Capital', `$${bt.final_capital.toFixed(0)}`, 'blue')}
            ${metricCard('Max Drawdown', `${bt.max_drawdown}%`, 'red')}
            ${metricCard('Sharpe', bt.sharpe_ratio, 'blue')}
            ${metricCard('Win Rate', `${bt.win_rate}%`, bt.win_rate >= 50 ? 'green' : 'red')}
            ${metricCard('Trades', bt.total_trades_copied, 'blue')}
            ${metricCard('Best Trade', `$${bt.best_trade_pnl}`, 'green')}
            ${metricCard('Worst Trade', `$${bt.worst_trade_pnl}`, 'red')}
        </div>`;

    // Equity curve chart
    if (bt.equity_curve && bt.equity_curve.length > 1) {
        html += '<canvas id="equity-chart" height="100"></canvas>';
    }

    html += `<p class="text-xs text-gray-400 mt-4">Past performance does not guarantee future results. Slippage and timing estimates are approximate. Actual Polycop execution may differ.</p>`;
    html += '</div>';
    resultsDiv.innerHTML = html;

    // Render chart
    if (bt.equity_curve && bt.equity_curve.length > 1) {
        const ctx = document.getElementById('equity-chart').getContext('2d');
        new Chart(ctx, {
            type: 'line',
            data: {
                labels: bt.equity_curve.map(p => new Date(p.timestamp * 1000).toLocaleDateString()),
                datasets: [{
                    label: 'Portfolio Equity ($)',
                    data: bt.equity_curve.map(p => p.equity),
                    borderColor: '#2196F3',
                    backgroundColor: 'rgba(33,150,243,0.1)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0,
                }],
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: { x: { display: false }, y: { beginAtZero: false } },
            },
        });
    }
}

// ── Reports ──────────────────────────────────────────────────────────────────

async function renderReports() {
    app.innerHTML = '<p class="text-gray-400">Loading reports...</p>';
    try {
        const reports = await api('/reports?limit=30');
        let html = '<div class="card"><h2 class="text-xl font-bold mb-4">Daily Reports</h2>';

        if (!reports.length) {
            html += '<p class="text-gray-500">No reports yet. Run the pipeline first.</p>';
        } else {
            for (const r of reports) {
                html += `<div class="border-b py-3 cursor-pointer hover:bg-gray-50" onclick="showReport(${r.id}, '${r.report_date}')">
                    <div class="flex justify-between items-center">
                        <div>
                            <span class="font-medium">${r.report_date}</span>
                            <span class="text-sm text-gray-500 ml-3">${r.traders_scanned} scored | ${r.traders_passing} passing</span>
                        </div>
                    </div>
                    <div id="report-${r.id}" class="hidden mt-3"></div>
                </div>`;
            }
        }
        html += '</div>';
        app.innerHTML = html;
    } catch (e) {
        app.innerHTML = `<div class="card"><p class="text-red-500">Error: ${e.message}</p></div>`;
    }
}

async function showReport(id, date) {
    const container = document.getElementById(`report-${id}`);
    if (!container.classList.contains('hidden')) {
        container.classList.add('hidden');
        return;
    }

    try {
        const report = await api(`/reports/${date}`);
        let html = '<div class="text-sm">';
        if (report.summary) {
            html += `<pre class="text-gray-600 whitespace-pre-wrap mb-3">${report.summary}</pre>`;
        }
        if (report.top_10 && report.top_10.length > 0) {
            html += '<table class="w-full"><thead><tr class="text-gray-500 text-left"><th>#</th><th>Tier</th><th>Name</th><th>Score</th><th>ROI</th><th>WR</th><th>Wallet</th></tr></thead><tbody>';
            for (const t of report.top_10) {
                html += `<tr class="border-t hover:bg-gray-50 cursor-pointer" onclick="location.hash='#/traders/${t.proxy_wallet}'">
                    <td class="py-1">${t.rank}</td><td>${tierBadge(t.tier)}</td>
                    <td>${t.name || '?'}</td><td>${t.composite_score}</td>
                    <td class="${t.roi >= 0 ? 'text-green-600' : 'text-red-600'}">${t.roi}%</td>
                    <td>${t.win_rate}%</td>
                    <td class="font-mono text-xs text-gray-400">${truncateWallet(t.proxy_wallet)}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }
        html += '</div>';
        container.innerHTML = html;
        container.classList.remove('hidden');
    } catch (e) {
        container.innerHTML = `<p class="text-red-500">${e.message}</p>`;
        container.classList.remove('hidden');
    }
}
