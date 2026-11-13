import { useEffect, useRef, useState } from 'react'
import './App.css'
import { Comparison, ImpactBanner, Scoreboard, ServiceMap, rs } from './Phase3.jsx'
import { Sidebar, SimpleIncident, SimpleServices, StatusHero, NAMES, plainCause, plainAction } from './Simple.jsx'
import { AgentGraph, AgentWork, AlertsBar, MemoryPanel, Postmortem } from './Agentic.jsx'

// Laptop A: works with no settings. Laptop B: put VITE_BACKEND_URL=http://<Laptop A IP>:8000 in dashboard/.env.local
const BACKEND = import.meta.env.VITE_BACKEND_URL || `http://${window.location.hostname}:8000`
const WS_URL = BACKEND.replace(/^http/, 'ws') + '/ws'
const ORDER = ['storefront', 'cart', 'payment', 'database', 'redis']

function useBackendState() {
  const [state, setState] = useState(null)
  const [connected, setConnected] = useState(false)
  const retry = useRef(null)
  useEffect(() => {
    let ws
    const connect = () => {
      ws = new WebSocket(WS_URL)
      ws.onopen = () => setConnected(true)
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data)
        if (msg.type === 'state') setState(msg.state)
      }
      ws.onclose = () => {
        setConnected(false)
        retry.current = setTimeout(connect, 2000)
      }
    }
    connect()
    return () => { clearTimeout(retry.current); ws && ws.close() }
  }, [])
  return [state, connected]
}

const MODES = { always: 'always ask', timed: 'ask, auto after 20s', auto: 'only when unsure' }
const NEXT_MODE = { always: 'timed', timed: 'auto', auto: 'always' }

function Countdown({ deadline, auto }) {
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), 500); return () => clearInterval(t) }, [])
  const left = Math.max(0, Math.ceil(deadline - now))
  return <span className={`pill ${auto ? 'v-running' : 'v-BLOCK'}`}>
    {auto ? `auto-approves in ${left}s if nobody answers` : `${left}s left, then denied (no answer means no)`}</span>
}

const post = (path) => fetch(BACKEND + path, { method: 'POST' }).then((r) => r.json())

function versionOf(detail) {
  const m = /"version":"([^"]+)"/.exec(detail || '')
  return m ? m[1] : null
}

function ServiceCard({ name, s, rootCause }) {
  const ok = s?.health === 'ok'
  const version = versionOf(s?.detail)
  return (
    <div className={`card service ${ok ? 'ok' : 'bad'} ${rootCause ? 'root' : ''}`}>
      <div className="row">
        <strong>{name}</strong>
        {version && <span className="pill">v{version}</span>}
        {s?.oom_killed && <span className="pill v-BLOCK">OOM</span>}
        {rootCause && <span className="pill root-pill">root cause</span>}
      </div>
      <div className="muted">{s?.container} · {s?.container_status ?? '…'}</div>
      <div className="detail">{s ? s.detail : 'waiting for data'}</div>
    </div>
  )
}

function Changes({ items }) {
  if (!items?.length) return <p className="muted">No changes recorded yet.</p>
  return (
    <table>
      <thead><tr><th>When</th><th>Who</th><th>Service</th><th>Change</th></tr></thead>
      <tbody>
        {items.map((c) => {
          const what = c.kind === 'release'
            ? `release ${c.before?.version} → ${c.after?.version}`
            : 'config ' + Object.keys(c.after || {}).filter((k) => c.before?.[k] !== c.after[k])
              .map((k) => `${k}: ${c.before?.[k]} → ${c.after[k]}`).join(', ')
          return (
            <tr key={c.id} className={c.is_rollback ? 'rollback-row' : ''}>
              <td>{new Date(c.time * 1000).toLocaleTimeString()}</td>
              <td>{c.actor}{c.is_rollback && <span className="pill v-ALLOW">auto-rollback</span>}</td>
              <td>{c.service}</td>
              <td>{what}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function SecurityEvents({ items }) {
  if (!items?.length) return <p className="muted">No attacks seen.</p>
  return (
    <ul className="security">
      {items.map((e, i) => (
        <li key={i}>
          <span className="muted">{e.time}</span> <span className="pill v-BLOCK">{e.kind.replace('_', ' ')}</span>
          <div className="detail">{e.detail}</div>
          {e.kind === 'poisoned_log' && e.data?.map((l, j) => <code key={j} className="quarantined">{l}</code>)}
        </li>
      ))}
    </ul>
  )
}

const SIM_PILL = { passed: 'v-ALLOW', failed: 'v-BLOCK', not_reproducible: 'v-APPROVAL_REQUIRED', error: 'v-BLOCK' }

function PracticeRun({ inc }) {
  const sim = inc.simulation
  const rehearsing = !sim && inc.steps?.some((s) => s.stage === 'simulation' && s.status === 'running')
  if (rehearsing) return <span className="pill v-running">rehearsing on the shadow copy…</span>
  if (!sim) return null
  return (
    <div>
      <span className={`pill ${SIM_PILL[sim.status] || ''}`}>{sim.status.replace('_', ' ')}</span>
      <span className="muted"> in {sim.duration_s}s</span>
      <ol className="sim">
        {sim.steps.filter((s) => s.stage !== 'restore').map((s, i) => (
          <li key={i} className={`sim-${s.status.replace(' ', '-')}`}><strong>{s.stage}</strong> {s.detail}</li>
        ))}
      </ol>
    </div>
  )
}

function OneMinuteCard({ inc }) {
  const d = inc.diagnosis, p = inc.proposal, dec = inc.decision
  const waiting = inc.status === 'awaiting_approval'
  return (
    <div className="card minute">
      <h3>Incident {inc.id} <span className={`pill st-${inc.status}`}>{inc.status.replace('_', ' ')}</span>
        {inc.recurring && <span className="pill v-BLOCK">recurring ×{inc.recurring}</span>}</h3>
      <dl>
        <dt>What broke</dt><dd>{inc.event?.affected_services?.join(', ')}</dd>
        {d && (<><dt>Why</dt><dd>{d.explanation} <span className="muted">(confidence {d.confidence}{d.llm_provider ? `, reviewed by ${d.llm_provider}` : ''})</span></dd></>)}
        {d?.root_cause?.supporting?.length > 0 && (<><dt>Evidence</dt><dd><ul className="evidence">
          {d.root_cause.supporting.map((e, i) => <li key={i}>{e}</li>)}</ul></dd></>)}
        {d?.suspicious_log_lines?.length > 0 && (<><dt>Quarantined</dt><dd>
          {d.suspicious_log_lines.map((l, i) => <code key={i} className="quarantined">{l}</code>)}</dd></>)}
        {p && (<><dt>Fix</dt><dd>{p.operation} → <code>{p.target}</code></dd>
          <dt>Undo</dt><dd>
            {inc.undone ? <span className="pill v-skipped">undone</span>
              : inc.result?.undo ? <><span>{inc.result.undo.describe}</span>{' '}
                <button className="deny small" onClick={() => {
                  if (confirm('Undo the agent\'s fix? This puts back exactly what was there before (it may re-create the problem).'))
                    post(`/undo/${inc.id}`)
                }}>Undo this fix</button></>
              : inc.result?.snapshot_note ? <span className="muted">{inc.result.snapshot_note}</span>
              : <span className="muted">{p.rollback}</span>}
          </dd>
          <dt>Practice run</dt><dd><PracticeRun inc={inc} /></dd></>)}
        {dec && (<><dt>Policy</dt><dd><span className={`pill v-${(dec.shown_verdict || dec.verdict).split(' ')[0]}`}>{dec.shown_verdict || dec.verdict}</span> {dec.reasons.join('; ')}</dd></>)}
        {inc.recovery_seconds != null && (<><dt>Recovered in</dt><dd>{inc.recovery_seconds}s</dd></>)}
        {inc.follow_up && (<><dt>Follow-up</dt><dd className="followup">{inc.follow_up.detail}</dd></>)}
        {inc.impact && (<><dt>Customer impact</dt><dd>{inc.impact.failed_checkouts} failed checkouts · {rs(inc.impact.lost_paise)} lost · outage {inc.impact.outage_seconds}s</dd></>)}
      </dl>
      {waiting && p && (
        <div className="row gap">
          {inc.approval_deadline && <Countdown deadline={inc.approval_deadline} auto={inc.auto_on_timeout} />}
          <button className="approve" onClick={() => post(`/approve/${p.action_id}?approver=dashboard`)}>Approve</button>
          <button className="deny" onClick={() => post(`/deny/${p.action_id}?approver=dashboard`)}>Deny</button>
        </div>
      )}
    </div>
  )
}

function Timeline({ steps }) {
  return (
    <ol className="timeline">
      {steps.map((s, i) => (
        <li key={i}>
          <span className="muted">{s.time}</span> <strong>{s.stage}</strong>{' '}
          <span className={`pill v-${s.status}`}>{s.status}</span>
          <div className="detail">{s.detail}</div>
        </li>
      ))}
    </ol>
  )
}

export default function App() {
  const [state, connected] = useBackendState()
  const inc = state?.active || state?.history?.[0]
  const rootService = inc?.diagnosis?.root_cause?.service
  const probe = state?.probe
  return (
    <div className="layout">
      <Sidebar inc={inc} killSwitch={state?.kill_switch} live={!!state?.active} />
      <div className="page">
      <nav className="appnav">
        <span className="muted">Apps:</span>
        <a href={`http://${window.location.hostname}:3000`} target="_blank" rel="noreferrer">🛒 QuickCart (customer shop)</a>
        <a href={`http://${window.location.hostname}:7070`} target="_blank" rel="noreferrer">👻 Ghost Injector</a>
        <span className="here">🛡️ ShadowRun-Ops (you are here)</span>
      </nav>
      <header className="row">
        <h1>ShadowRun-Ops</h1>
        <span className="muted">The AI on-call engineer · practice first, then real</span>
        <span className={`pill ${connected ? 'v-ALLOW' : 'v-BLOCK'}`}>{connected ? 'agent connected' : 'agent offline'}</span>
        {state?.shadow?.available === false && <span className="pill v-BLOCK">shadow copy offline</span>}
        {state?.agent_info && <span className="pill">{state.agent_info.engine === 'LangGraph' ? '🕸 LangGraph agent' : 'built-in graph (install langgraph)'}</span>}
        <button
          className={state?.kill_switch ? 'deny' : ''}
          onClick={() => post(`/kill-switch/${!state?.kill_switch}`)}
          title="When ON, the AI may observe but never act">
          Kill switch: {state?.kill_switch ? 'ON (AI paused)' : 'off'}
        </button>
      </header>

      <StatusHero state={state} connected={connected} inc={inc} />
      <AlertsBar state={state} />
      <ImpactBanner state={state} />

      <div id="map"><ServiceMap state={state} /></div>

      <SimpleServices state={state} rootService={rootService} />

      <section id="agent" className="agent-row">
        <AgentGraph inc={inc} info={state?.agent_info} live={!!state?.active} />
        <AgentWork inc={inc} />
      </section>

      <section id="incident">
        {inc ? <SimpleIncident inc={inc} post={post} /> : <div className="card muted">No incidents yet. All quiet.</div>}
        {inc && <Postmortem inc={inc} />}
      </section>

      <div id="results"><Scoreboard backend={BACKEND} /></div>

      <section id="more">
        <h2 className="more-title">More details (for engineers)</h2>
        {inc && (
          <details className="fold">
            <summary>Technical details of {inc.id}: evidence, safety decision, full timeline</summary>
            <section className="two-col">
              <OneMinuteCard inc={inc} />
              <div className="card"><h3>Timeline</h3><Timeline steps={inc.steps} /></div>
            </section>
          </details>
        )}
        <details className="fold">
          <summary>Incident memory: problems the agent already knows ({state?.agent_info?.memory?.problems_remembered || 0})</summary>
          <MemoryPanel info={state?.agent_info} post={post} />
        </details>
        <details className="fold">
          <summary>Security: careless agent vs ShadowRun-Ops, attacks blocked ({state?.security_events?.length || 0})</summary>
          <Comparison state={state} post={post} />
          <div className="card"><h3>Security events</h3><SecurityEvents items={state?.security_events} /></div>
        </details>
        <details className="fold">
          <summary>Change history: who deployed what ({state?.changes?.length || 0} recent)</summary>
          <div className="card"><Changes items={state?.changes} /></div>
        </details>
        {state?.history?.length > 0 && (
          <details className="fold">
            <summary>All incidents ({state.history.length})</summary>
            <section className="card">
              <table>
                <thead><tr><th>Incident</th><th>Cause</th><th>What the AI did</th><th>Practice run</th><th>Result</th><th>Time</th><th>Lost</th></tr></thead>
                <tbody>
                  {state.history.map((h) => (
                    <tr key={h.id}>
                      <td>{h.id}</td>
                      <td>{h.diagnosis?.root_cause ? plainCause(h.diagnosis.root_cause) : '—'}</td>
                      <td>{h.proposal ? plainAction(h.proposal) : '—'}</td>
                      <td>{h.simulation?.status?.replace('_', ' ') ?? '—'}</td>
                      <td>{h.status.replace('_', ' ')}</td>
                      <td>{h.recovery_seconds != null ? `${h.recovery_seconds}s` : '—'}</td>
                      <td>{h.impact ? rs(h.impact.lost_paise) : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </details>
        )}
      </section>
      </div>
    </div>
  )
}
