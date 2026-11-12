import { useEffect, useRef, useState } from 'react'
import './App.css'
import { Comparison, ImpactBanner, Scoreboard, ServiceMap, rs } from './Phase3.jsx'

// Laptop A: works with no settings. Laptop B: put VITE_BACKEND_URL=http://<Laptop A IP>:8000 in dashboard/.env.local
const BACKEND = import.meta.env.VITE_BACKEND_URL || `http://${window.location.hostname}:8000`
const WS_URL = BACKEND.replace(/^http/, 'ws') + '/ws'
const ORDER = ['storefront', 'cart', 'payment', 'database']

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
      <h3>Incident {inc.id} <span className={`pill st-${inc.status}`}>{inc.status.replace('_', ' ')}</span></h3>
      <dl>
        <dt>What broke</dt><dd>{inc.event?.affected_services?.join(', ')}</dd>
        {d && (<><dt>Why</dt><dd>{d.explanation} <span className="muted">(confidence {d.confidence}{d.llm_provider ? `, reviewed by ${d.llm_provider}` : ''})</span></dd></>)}
        {d?.root_cause?.supporting?.length > 0 && (<><dt>Evidence</dt><dd><ul className="evidence">
          {d.root_cause.supporting.map((e, i) => <li key={i}>{e}</li>)}</ul></dd></>)}
        {d?.suspicious_log_lines?.length > 0 && (<><dt>Quarantined</dt><dd>
          {d.suspicious_log_lines.map((l, i) => <code key={i} className="quarantined">{l}</code>)}</dd></>)}
        {p && (<><dt>Fix</dt><dd>{p.operation} → <code>{p.target}</code></dd>
          <dt>Undo plan</dt><dd>{p.rollback}</dd>
          <dt>Practice run</dt><dd><PracticeRun inc={inc} /></dd></>)}
        {dec && (<><dt>Policy</dt><dd><span className={`pill v-${dec.verdict}`}>{dec.verdict}</span> {dec.reasons.join('; ')}</dd></>)}
        {inc.recovery_seconds != null && (<><dt>Recovered in</dt><dd>{inc.recovery_seconds}s</dd></>)}
        {inc.impact && (<><dt>Customer impact</dt><dd>{inc.impact.failed_checkouts} failed checkouts · {rs(inc.impact.lost_paise)} lost · outage {inc.impact.outage_seconds}s</dd></>)}
      </dl>
      {waiting && p && (
        <div className="row gap">
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
    <div className="page">
      <header className="row">
        <h1>ShadowRun-Ops</h1>
        <span className="muted">Practice first. Then real.</span>
        <span className={`pill ${connected ? 'v-ALLOW' : 'v-BLOCK'}`}>{connected ? 'agent connected' : 'agent offline'}</span>
        {state?.deploy_window && <span className="pill v-running">deploy in progress: monitoring paused</span>}
        {state?.shadow?.available === true && <span className="pill v-skipped">shadow copy ready</span>}
        {state?.shadow?.available === false && <span className="pill v-BLOCK">shadow copy offline</span>}
        <button
          className={state?.kill_switch ? 'deny' : ''}
          onClick={() => post(`/kill-switch/${!state?.kill_switch}`)}
          title="When ON, the agent may observe but never act">
          Kill switch: {state?.kill_switch ? 'ON' : 'off'}
        </button>
      </header>

      <ImpactBanner state={state} />

      <ServiceMap state={state} />

      <section className="grid">
        {ORDER.map((n) => (
          <ServiceCard key={n} name={n} s={state?.services?.[n]}
            rootCause={state?.active && rootService === n} />
        ))}
        <div className={`card service ${probe?.result === 'ok' ? 'ok' : 'bad'}`}>
          <strong>customer checkout</strong>
          <div className="muted">synthetic probe</div>
          <div className="detail">{probe?.detail ?? 'waiting for data'}</div>
        </div>
      </section>

      {inc ? (
        <section className="two-col">
          <OneMinuteCard inc={inc} />
          <div className="card"><h3>Timeline</h3><Timeline steps={inc.steps} /></div>
        </section>
      ) : (
        <p className="muted">No incidents yet. All quiet.</p>
      )}

      <Comparison state={state} post={post} />

      <section className="two-col">
        <div className="card"><h3>Recent changes (ledger)</h3><Changes items={state?.changes} /></div>
        <div className="card"><h3>Security events</h3><SecurityEvents items={state?.security_events} /></div>
      </section>

      <Scoreboard backend={BACKEND} />

      {state?.history?.length > 0 && (
        <section className="card">
          <h3>History</h3>
          <table>
            <thead><tr><th>Incident</th><th>Root cause</th><th>Action</th><th>Practice run</th><th>Status</th><th>Recovery</th><th>Lost</th></tr></thead>
            <tbody>
              {state.history.map((h) => (
                <tr key={h.id}>
                  <td>{h.id}</td>
                  <td>{h.diagnosis?.root_cause?.id ?? '—'}</td>
                  <td>{h.proposal ? `${h.proposal.operation} ${h.proposal.target}` : '—'}</td>
                  <td>{h.simulation?.status?.replace('_', ' ') ?? '—'}</td>
                  <td>{h.status}</td>
                  <td>{h.recovery_seconds != null ? `${h.recovery_seconds}s` : '—'}</td>
                  <td>{h.impact ? rs(h.impact.lost_paise) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  )
}
