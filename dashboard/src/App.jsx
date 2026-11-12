import { useEffect, useRef, useState } from 'react'
import './App.css'

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

function ServiceCard({ name, s, rootCause }) {
  const ok = s?.health === 'ok'
  return (
    <div className={`card service ${ok ? 'ok' : 'bad'} ${rootCause ? 'root' : ''}`}>
      <div className="row">
        <strong>{name}</strong>
        {rootCause && <span className="pill root-pill">root cause</span>}
      </div>
      <div className="muted">{s?.container} · {s?.container_status ?? '…'}</div>
      <div className="detail">{s ? s.detail : 'waiting for data'}</div>
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
        {p && (<><dt>Fix</dt><dd>{p.operation} → <code>{p.target}</code></dd>
          <dt>Undo plan</dt><dd>{p.rollback}</dd></>)}
        {dec && (<><dt>Policy</dt><dd><span className={`pill v-${dec.verdict}`}>{dec.verdict}</span> {dec.reasons.join('; ')}</dd></>)}
        {inc.recovery_seconds != null && (<><dt>Recovered in</dt><dd>{inc.recovery_seconds}s</dd></>)}
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
        <button
          className={state?.kill_switch ? 'deny' : ''}
          onClick={() => post(`/kill-switch/${!state?.kill_switch}`)}
          title="When ON, the agent may observe but never act">
          Kill switch: {state?.kill_switch ? 'ON' : 'off'}
        </button>
      </header>

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

      {state?.history?.length > 0 && (
        <section className="card">
          <h3>History</h3>
          <table>
            <thead><tr><th>Incident</th><th>Root cause</th><th>Action</th><th>Status</th><th>Recovery</th></tr></thead>
            <tbody>
              {state.history.map((h) => (
                <tr key={h.id}>
                  <td>{h.id}</td>
                  <td>{h.diagnosis?.root_cause?.id ?? '—'}</td>
                  <td>{h.proposal ? `${h.proposal.operation} ${h.proposal.target}` : '—'}</td>
                  <td>{h.status}</td>
                  <td>{h.recovery_seconds != null ? `${h.recovery_seconds}s` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  )
}
