// The agentic layer on the dashboard: alerts, the agent's path through the graph, the AI's tool calls,
// token use, memory and the postmortem.
import { useState } from 'react'

const fmt = (n) => (n ?? 0).toLocaleString('en-IN')

function Spark({ values, bad, label, unit }) {
  const v = (values || []).map((x) => (x == null ? 0 : x))
  if (v.length < 2) return null
  const max = Math.max(...v, 1), w = 180, h = 36
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * w},${h - (x / max) * (h - 4) - 2}`).join(' ')
  return (
    <div className="spark">
      <div className="spark-label">{label} <b>{fmt(v.at(-1))}{unit}</b></div>
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}><polyline points={pts} fill="none" stroke={bad ? '#dc2626' : '#16a34a'} strokeWidth="2" /></svg>
    </div>
  )
}

export function AlertsBar({ state }) {
  const o = state?.observability
  if (!o) return null
  const firing = o.alerts || []
  return (
    <section className={`alerts ${firing.length ? 'firing' : 'quiet'}`}>
      <div className="alerts-left">
        <div className="alerts-title">{firing.length ? `🚨 ${firing.length} alert${firing.length > 1 ? 's' : ''} firing` : '✓ No alerts firing'}</div>
        <div className="alerts-sub">Sensors watch the shop every 3 s with plain code: 0 AI tokens while healthy.</div>
        <div className="alert-pills">
          {firing.map((a) => <span key={a.name} className={`apill sev-${a.severity}`} title={a.detail}>{a.name}<small>{a.detail}</small></span>)}
        </div>
      </div>
      <div className="alerts-right">
        <Spark values={o.series?.latency_ms} bad={firing.some((a) => a.name.startsWith('Checkout'))} label="checkout" unit=" ms" />
        <Spark values={o.series?.errors} bad={firing.some((a) => a.name === 'ErrorSpike')} label="errors logged" unit="" />
      </div>
    </section>
  )
}

const POS = {
  triage: [10, 120], recall: [130, 46], investigate: [130, 194], plan: [250, 120], practise: [370, 120],
  rethink: [370, 236], safety: [490, 120], ask_engineer: [490, 20], execute: [610, 120], verify: [720, 120],
  learn: [720, 236], undo: [610, 236], escalate: [250, 290],
}
const LABEL = {
  triage: 'Triage', recall: 'Recall memory', investigate: 'AI investigate', plan: 'Plan fix', practise: 'Practise (copy)',
  rethink: 'Rethink', safety: 'Safety check', ask_engineer: 'Ask engineer', execute: 'Execute', verify: 'Verify',
  learn: 'Learn', undo: 'Undo', escalate: 'Escalate',
}
const BW = 100, BH = 34

function edgePoints(a, b) {
  const [ax, ay] = POS[a], [bx, by] = POS[b]
  const ca = [ax + BW / 2, ay + BH / 2], cb = [bx + BW / 2, by + BH / 2]
  const clip = ([cx, cy], [tx, ty]) => {
    const dx = tx - cx, dy = ty - cy
    const s = Math.min(Math.abs((BW / 2) / (dx || 1e-6)), Math.abs((BH / 2) / (dy || 1e-6)))
    return [cx + dx * s, cy + dy * s]
  }
  return [...clip(ca, cb), ...clip(cb, ca)]
}

export function AgentGraph({ inc, info, live }) {
  const trail = inc?.agent?.trail || []
  const visited = new Set(trail.map((t) => t.node))
  const taken = new Set(trail.slice(1).map((t, i) => `${trail[i].node}>${t.node}`))
  const current = live ? trail.at(-1)?.node : null
  const edges = info?.edges || []
  return (
    <div className="card agent-graph">
      <h3>The agent's path (LangGraph) <span className="pill">{info?.engine === 'LangGraph' ? '🕸 LangGraph' : 'built-in graph engine'}</span></h3>
      <svg viewBox="0 0 830 336" width="100%" role="img" aria-label="Agent workflow graph with the path taken highlighted">
        <defs>
          <marker id="ag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#94a3b8" /></marker>
          <marker id="ag-arrow-on" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#2563eb" /></marker>
        </defs>
        {edges.map(([a, b]) => {
          const on = taken.has(`${a}>${b}`)
          const [x1, y1, x2, y2] = edgePoints(a, b)
          return <line key={a + b} x1={x1} y1={y1} x2={x2} y2={y2} stroke={on ? '#2563eb' : '#cbd5e1'} strokeWidth={on ? 2.5 : 1}
            markerEnd={`url(#${on ? 'ag-arrow-on' : 'ag-arrow'})`} />
        })}
        {Object.entries(POS).map(([n, [x, y]]) => {
          const on = visited.has(n), cur = current === n
          const fill = cur ? '#fef3c7' : on ? (n === 'escalate' || n === 'undo' || n === 'rethink' ? '#fee2e2' : '#dbeafe') : '#f8fafc'
          const stroke = cur ? '#d97706' : on ? '#2563eb' : '#cbd5e1'
          return (
            <g key={n}>
              <rect x={x} y={y} width={BW} height={BH} rx="8" fill={fill} stroke={stroke} strokeWidth={on || cur ? 2 : 1} />
              <text x={x + BW / 2} y={y + 21} textAnchor="middle" fontSize="12.5" fontWeight={on ? 700 : 500} fill={on ? '#0f172a' : '#94a3b8'}>{LABEL[n]}</text>
            </g>
          )
        })}
      </svg>
      {trail.length > 0 && <ol className="trail">{trail.map((t, i) => <li key={i}><b>{LABEL[t.node] || t.node}</b> <span className="muted">{t.time}</span> {t.note}</li>)}</ol>}
      {!trail.length && <p className="muted">When an alert fires, the path the agent takes through this graph lights up here.</p>}
    </div>
  )
}

export function AgentWork({ inc }) {
  const a = inc?.agent
  if (!a) return <div className="card muted">No AI work yet.</div>
  const t = a.tokens || { used: 0, budget: 8000, llm_calls: 0, providers: [] }
  const pct = Math.min(100, Math.round((100 * t.used) / (t.budget || 1)))
  const mem = a.memory || {}
  return (
    <div className="card agent-work">
      <h3>What the AI investigator did</h3>
      {mem.hit && <div className="mem-hit">🧠 Recognised from memory (fixed {mem.times_fixed}× before). The AI was not needed: <b>0 tokens</b>. The fix is still practised and verified.</div>}
      <div className="tokens">
        <div className="tok-row"><b>AI tokens this incident</b><span>{fmt(t.used)} / {fmt(t.budget)}</span></div>
        <div className="tok-bar"><span style={{ width: `${pct}%` }} /></div>
        <div className="muted">{t.llm_calls} AI call(s){t.providers?.length ? ` · ${t.providers.join(', ')}` : ''} · budget set in policy.yaml</div>
      </div>
      {(a.investigations || []).map((inv, k) => (
        <div key={k} className="inv">
          <div className="inv-head">Investigation {k + 1}: <span className="pill">{{ agent: 'AI + tools', 'agent+rules': 'AI + rules', rules: 'rules only', memory: 'memory' }[inv.mode] || inv.mode}</span></div>
          {inv.steps?.map((s, i) => (
            <div key={i} className="tool">
              <div className="thought">💭 {s.thought}</div>
              <code>{s.tool}({Object.entries(s.args || {}).map(([k2, v]) => `${k2}=${v}`).join(', ')})</code>
              <div className="result">{String(s.result).slice(0, 220)}{String(s.result).length > 220 ? '…' : ''}</div>
            </div>
          ))}
          {inv.final && <div className="final">✓ Conclusion: <b>{inv.final.root_cause_id}</b>{inv.final.explanation ? ` · ${inv.final.explanation}` : ''}</div>}
          {inv.note && <div className="muted">{inv.note}</div>}
        </div>
      ))}
      {mem.readable && <div className="muted fp">Fingerprint: <code>{mem.readable}</code></div>}
    </div>
  )
}

export function Postmortem({ inc }) {
  const pm = inc?.agent?.postmortem
  if (!pm) return null
  return (
    <details className="fold" open>
      <summary>Postmortem: {pm.title} (written automatically, 0 tokens)</summary>
      <dl className="pm">
        <dt>What broke</dt><dd>{pm.what_broke}</dd>
        <dt>Root cause</dt><dd>{pm.root_cause}</dd>
        {pm.evidence?.length > 0 && <><dt>Evidence</dt><dd><ul>{pm.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul></dd></>}
        <dt>Fix</dt><dd>{pm.fix}</dd>
        <dt>Practice run</dt><dd>{pm.practice_run}</dd>
        <dt>Approval</dt><dd>{pm.approval}</dd>
        <dt>Time to recover</dt><dd>{pm.time_to_recover_s != null ? `${pm.time_to_recover_s}s` : '—'}</dd>
        <dt>Agent path</dt><dd><code>{pm.agent_path}</code></dd>
        <dt>AI cost</dt><dd>{pm.ai_cost}</dd>
        <dt>Prevent it</dt><dd>{pm.prevent_it}</dd>
      </dl>
    </details>
  )
}

export function MemoryPanel({ info, post }) {
  const [busy, setBusy] = useState(false)
  const m = info?.memory
  return (
    <div className="card">
      <div className="row"><h3 style={{ margin: 0 }}>Incident memory: {m?.problems_remembered || 0} problem(s) remembered</h3>
        <span className="muted">repeat problems used 0 tokens {info?.memory_hits || 0}× · {fmt(info?.tokens_total)} tokens used in total</span>
        <button className="small" disabled={busy} onClick={async () => { setBusy(true); await post('/memory/clear'); setBusy(false) }}>Clear memory</button></div>
      {m?.entries?.length ? (
        <table><thead><tr><th>Fingerprint (what was observed)</th><th>Cause</th><th>Fix</th><th>Fixed</th></tr></thead>
          <tbody>{m.entries.map((e) => <tr key={e.fingerprint}><td><code>{e.readable}</code></td><td>{e.cause.kind} · {e.cause.service}</td><td>{e.operation}</td><td>{e.times_fixed}×</td></tr>)}</tbody></table>
      ) : <p className="muted">Nothing remembered yet. After a fix is verified, the problem and its fix are saved here.</p>}
    </div>
  )
}
