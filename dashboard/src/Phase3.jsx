import { useEffect, useState } from 'react'

const rs = (paise) => '₹' + Math.round((paise || 0) / 100).toLocaleString('en-IN')
const CONTAINER_TO_SERVICE = { 'demo-db': 'database', 'shop-cart': 'cart', 'shop-payment': 'payment', 'shop-storefront': 'storefront', 'shop-redis': 'redis' }

const LIVE = {
  customers: { x: 305, y: 14 }, storefront: { x: 305, y: 84 },
  cart: { x: 135, y: 158 }, payment: { x: 475, y: 158 }, database: { x: 305, y: 232 }, redis: { x: 560, y: 84 },
}
const EDGES = [['customers', 'storefront'], ['storefront', 'cart'], ['storefront', 'payment'], ['storefront', 'redis'],
  ['cart', 'database'], ['payment', 'database']]
const W = 150, H = 46
const SHADOW_ORDER = ['storefront', 'cart', 'payment', 'database', 'redis']

const COLORS = {
  ok: ['#dafbe1', '#1a7f37', '#116329'], failed: ['#ffebe9', '#cf222e', '#a40e26'],
  working: ['#fff8c5', '#bf8700', '#7d4e00'], idle: ['#f6f8fa', '#8c959f', '#57606a'],
}

function Node({ x, y, w = W, h = H, label, sub, status, root }) {
  const [fill, stroke, text] = COLORS[status] || COLORS.idle
  return (
    <g>
      {root && <rect x={x - 5} y={y - 5} width={w + 10} height={h + 10} rx="12" fill="none" stroke="#bf8700" strokeWidth="3" strokeDasharray="6 4" />}
      <rect x={x} y={y} width={w} height={h} rx="9" fill={fill} stroke={stroke} strokeWidth="1.5" />
      <text x={x + w / 2} y={y + (sub ? 19 : h / 2 + 5)} textAnchor="middle" fontSize="14" fontWeight="600" fill={text}>{label}</text>
      {sub && <text x={x + w / 2} y={y + 35} textAnchor="middle" fontSize="11" fill={text}>{sub}</text>}
      {root && <text x={x + w / 2} y={y - 10} textAnchor="middle" fontSize="12" fontWeight="700" fill="#7d4e00">▼ ROOT CAUSE</text>}
    </g>
  )
}

function shadowStatuses(inc, shadow) {
  const base = shadow?.available ? 'ok' : 'idle'
  const out = Object.fromEntries(SHADOW_ORDER.map((s) => [s, { status: base, sub: shadow?.available ? 'ready' : 'offline' }]))
  if (!inc || inc.status === 'resolved' || inc.status === 'escalated') return out
  const steps = inc.sim_live?.length ? inc.sim_live : inc.simulation?.steps || []
  if (!steps.length) return out
  const twin = inc.proposal?.parameters?.service || CONTAINER_TO_SERVICE[inc.proposal?.target]
  for (const s of steps) {
    if (s.stage === 'baseline' && s.status === 'ok') SHADOW_ORDER.forEach((n) => (out[n] = { status: 'ok', sub: 'baseline ok' }))
    if (!twin || !out[twin]) continue
    if (s.stage === 'reproduce' && s.status === 'running') out[twin] = { status: 'working', sub: 'injecting fault…' }
    if (s.stage === 'reproduce' && s.status === 'red') out[twin] = { status: 'failed', sub: 'fault reproduced' }
    if (s.stage === 'reproduce' && s.status === 'not reproduced') out[twin] = { status: 'idle', sub: 'not reproduced' }
    if (s.stage === 'apply') out[twin] = { status: 'working', sub: 'applying fix…' }
    if (s.stage === 'verify') out[twin] = s.status === 'green' ? { status: 'ok', sub: 'fix works ✓' } : { status: 'failed', sub: 'fix failed ✗' }
  }
  return out
}

export function ServiceMap({ state }) {
  const services = state?.services || {}
  const inc = state?.active
  const root = inc?.diagnosis?.root_cause?.service
  const status = (n) => (n === 'customers' ? (state?.probe?.result === 'ok' ? 'ok' : state?.probe ? 'failed' : 'idle')
    : services[n] ? (services[n].health === 'ok' ? 'ok' : 'failed') : 'idle')
  const sub = (n) => {
    if (n === 'customers') return (state?.traffic?.live ? 'fake customers' : 'checkout probe')
      + (state?.probe?.latency_ms != null ? ` · ${state.probe.latency_ms} ms` : '')
    const m = /"version":"([^"]+)"/.exec(services[n]?.detail || '')
    if (services[n]?.oom_killed) return 'OOM-killed'
    return m ? `v${m[1]}` : services[n]?.container_status
  }
  const shadow = shadowStatuses(inc, state?.shadow)
  const rehearsing = inc?.steps?.some((s) => s.stage === 'simulation' && s.status === 'running') && !inc?.simulation
  return (
    <div className="card map">
      <h3>Live service map {rehearsing && <span className="pill v-running">practice run on the shadow copy…</span>}</h3>
      <svg viewBox="0 0 760 400" width="100%" role="img" aria-label="Service map of the live shop and its shadow copy">
        {EDGES.map(([a, b]) => {
          const bad = status(b) === 'failed'
          const side = LIVE[a].y === LIVE[b].y  // same row: draw side to side
          const [x1, y1, x2, y2] = side
            ? [LIVE[a].x + W, LIVE[a].y + H / 2, LIVE[b].x, LIVE[b].y + H / 2]
            : [LIVE[a].x + W / 2, LIVE[a].y + H, LIVE[b].x + W / 2, LIVE[b].y]
          return <line key={a + b} x1={x1} y1={y1} x2={x2} y2={y2}
            stroke={bad ? '#cf222e' : '#8c959f'} strokeWidth={bad ? 2.5 : 1.5} strokeDasharray={bad ? '5 4' : ''} />
        })}
        {Object.entries(LIVE).map(([n, p]) => (
          <Node key={n} x={p.x} y={p.y} label={n} sub={sub(n)} status={status(n)} root={root === n} />
        ))}
        <rect x="20" y="300" width="720" height="92" rx="12" fill="none" stroke="#8c959f" strokeDasharray="6 5" />
        <text x="34" y="320" fontSize="12" fontWeight="600" fill="#57606a">SHADOW COPY (isolated rehearsal environment)</text>
        {SHADOW_ORDER.map((n, i) => (
          <Node key={n} x={32 + i * 142} y={332} w={132} h={46} label={`shadow-${n === 'database' ? 'db' : n}`}
            sub={shadow[n].sub} status={shadow[n].status} />
        ))}
      </svg>
    </div>
  )
}

export function ImpactBanner({ state }) {
  const t = state?.traffic
  if (!t?.live) return <p className="muted">Fake customers are not running (start traffic\traffic.py to count real customer impact).</p>
  const o = t.current_outage
  if (o) {
    const secs = Math.round(Date.now() / 1000 - o.started)
    return (
      <div className="impact live">
        <strong>Customers affected right now:</strong> {o.failed} failed checkouts · <strong>{rs(o.lost_paise)} lost</strong> · {secs}s
      </div>
    )
  }
  return (
    <div className="impact ok">
      Fake customers: {t.ok.toLocaleString('en-IN')} orders ({rs(t.revenue_paise)}) · {t.failed} failed ({rs(t.lost_paise)} lost in total)
    </div>
  )
}

export function Comparison({ state, post }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const c = state?.comparison
  const run = async () => {
    setBusy(true); setMsg('')
    try {
      const r = await post('/compare/careless')
      if (!r.ok) setMsg(r.detail)
    } finally { setBusy(false) }
  }
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Careless agent vs ShadowRun-Ops</h3>
        <button onClick={run} disabled={busy}>{busy ? 'Asking…' : 'Ask a careless agent'}</button>
      </div>
      <p className="muted">Same poisoned logs. The careless agent gets them raw, with no sanitizer and no policy. Its command is only displayed, never executed.</p>
      {msg && <p className="muted">{msg}</p>}
      {c && (
        <div className="compare">
          <div className={`side ${c.careless.destructive ? 'bad' : ''}`}>
            <h4>Careless agent <span className="muted">({c.careless.provider})</span></h4>
            {c.careless.command ? <code className="cmd">$ {c.careless.command}</code> : <p className="muted">no answer</p>}
            <p>{c.careless.verdict}</p>
          </div>
          <div className="side good">
            <h4>ShadowRun-Ops</h4>
            <code className="cmd">$ {c.shadowrun.action || '(no action)'}</code>
            <p>Quarantined {c.shadowrun.quarantined} poisoned line(s). The LLM never saw them. Fixed the real cause
              ({c.shadowrun.root_cause}) after a shadow rehearsal. Data intact. Status: {c.shadowrun.status}.</p>
          </div>
        </div>
      )}
    </div>
  )
}

export function Scoreboard({ backend }) {
  const [data, setData] = useState(null)
  const load = () => fetch(backend + '/scoreboard').then((r) => r.json()).then(setData).catch(() => setData(null))
  useEffect(() => { load() }, [])
  return (
    <div className="card">
      <div className="row">
        <h3 style={{ margin: 0 }}>Chaos scoreboard</h3>
        <button onClick={load}>Refresh</button>
        {data?.generated_at && <span className="muted">generated {data.generated_at}</span>}
      </div>
      {!data?.available ? (
        <p className="muted">No results yet. Run: .\venv\Scripts\python.exe tools\chaos_run.py</p>
      ) : (
        <table>
          <thead><tr><th>Scenario</th><th>Runs</th><th>Correct diagnosis</th><th>Resolved</th><th>Human needed</th>
            <th>Unsafe actions</th><th>Avg detect</th><th>Avg recover</th><th>False alarms</th></tr></thead>
          <tbody>
            {data.summary.map((s) => (
              <tr key={s.scenario}>
                <td>{s.scenario}</td><td>{s.runs}</td>
                <td>{s.correct != null ? `${s.correct}/${s.runs}` : '—'}</td>
                <td>{s.resolved != null ? `${s.resolved}/${s.runs}` : '—'}</td>
                <td>{s.human_needed ?? '—'}</td>
                <td>{s.unsafe_actions ?? '—'}</td>
                <td>{s.avg_detect_s != null ? `${s.avg_detect_s}s` : '—'}</td>
                <td>{s.avg_recover_s != null ? `${s.avg_recover_s}s` : '—'}</td>
                <td>{s.false_alarms ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export { rs }
