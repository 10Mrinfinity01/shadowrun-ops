// Plain-English layer for the dashboard: same data, easier words.
import { useEffect, useState } from 'react'

export const NAMES = { storefront: 'Website', cart: 'Cart', payment: 'Payment', database: 'Database', redis: 'Login sessions (Redis)' }
const nm = (s) => (s === 'checkout' ? 'Customer checkout' : NAMES[s] || s)

export function plainCause(rc) {
  if (!rc) return 'cause not found yet'
  const s = nm(rc.service)
  return {
    container_down: `${s} has stopped`,
    oom_killed: `${s} ran out of memory and crashed`,
    bad_deploy: `a bad new version of ${s}`,
    bad_config: `a wrong setting in ${s}`,
    service_unhealthy: (rc.supporting || []).some((x) => x.includes('frozen')) ? `${s} is frozen (running, but not answering)`
      : `${s} is not working properly`,
  }[rc.kind] || `${s} has a problem`
}

export function plainAction(p) {
  if (!p) return ''
  const s = nm(p.parameters?.service)
  return {
    start_container: `Start ${s} again`,
    restart_container: `Restart ${s}`,
    rollback_release: `Go back to the last good version of ${s}${p.parameters?.to?.version ? ` (v${p.parameters.to.version})` : ''}`,
    restore_config: `Put back ${s}'s last good settings`,
  }[p.operation] || p.operation
}

const has = (steps, stages) => steps.filter((s) => stages.includes(s.stage))
const lastOf = (steps, stages) => has(steps, stages).at(-1)
const BAD = ['failed', 'BLOCK', 'denied', 'error']

// The 8 checkpoints of an incident, worked out from the agent's own steps.
export function journey(inc, killSwitch) {
  const steps = inc?.steps || []
  const finished = ['resolved', 'partially_resolved', 'escalated', 'error'].includes(inc?.status)
  const sim = inc?.simulation
  const dec = inc?.decision
  const C = [
    { key: 'detect', label: 'Problem detected', stages: ['detected'],
      sub: () => (inc?.event?.affected_services || []).map(nm).join(', ') },
    { key: 'investigate', label: 'Finding the cause', stages: ['investigating', 'diagnosed'],
      sub: () => inc?.diagnosis ? plainCause(inc.diagnosis.root_cause) : 'reading health checks, logs and recent changes' },
    { key: 'plan', label: 'Fix chosen', stages: ['planned'], sub: () => plainAction(inc?.proposal) },
    { key: 'practice', label: 'Practice run on a copy', stages: ['simulation'],
      sub: () => !sim ? 'breaking a copy of the shop the same way…'
        : { passed: 'the fix worked on the copy', failed: 'the fix did NOT work on the copy',
            not_reproducible: 'the copy did not break the same way', error: 'the copy could not be tested' }[sim.status] },
    { key: 'safety', label: 'Safety check', stages: ['policy', 'awaiting_approval', 'approval'],
      sub: () => !dec ? '' : inc?.status === 'awaiting_approval' ? 'waiting for the on-call engineer to approve'
        : lastOf(steps, ['approval'])?.status === 'approved' ? 'approved by the on-call engineer'
        : lastOf(steps, ['approval'])?.status === 'denied' ? 'the engineer said no: nothing changed'
        : killSwitch && dec.verdict === 'BLOCK' ? 'paused: kill switch is on'
        : dec.verdict === 'BLOCK' ? 'blocked by the safety rules' : 'allowed: reversible, inside the rules' },
    { key: 'apply', label: 'Fix applied', stages: ['executing', 'executed'], sub: () => inc?.result ? (inc.result.ok ? 'done on the real shop' : 'could not be applied') : 'changing the real shop…' },
    { key: 'verify', label: 'Checked with a real order', stages: ['verifying'], sub: () => ['resolved', 'partially_resolved'].includes(inc?.status) ? 'a test order went through' : 'placing a test order…' },
    { key: 'done', label: 'Resolved', stages: ['resolved', 'partial', 'escalated', 'error'],
      sub: () => inc?.status === 'resolved' ? `customers can pay again · ${inc.recovery_seconds}s`
        : inc?.status === 'partially_resolved' ? 'this part is fixed; another problem remains'
        : inc?.status === 'escalated' ? 'handed to an engineer' : '' },
  ]
  let seenActive = false
  return C.map((c, i) => {
    const mine = has(steps, c.stages)
    const last = mine.at(-1)
    let state = 'todo'
    if (mine.length) {
      state = last.status === 'running' ? 'active' : BAD.includes(last.status) ? 'failed' : 'done'
      if (c.key === 'practice' && sim && sim.status !== 'passed') state = sim.status === 'failed' ? 'failed' : 'warn'
      if (c.key === 'investigate' && lastOf(steps, ['diagnosed'])) state = 'done'
      if (c.key === 'apply' && lastOf(steps, ['executed'])) state = BAD.includes(lastOf(steps, ['executed']).status) ? 'failed' : 'done'
      if (c.key === 'verify' && lastOf(steps, ['resolved', 'partial'])) state = 'done'
      if (c.key === 'safety' && inc.status === 'awaiting_approval') state = 'active'
      if (c.key === 'done' && inc.status === 'escalated') state = 'failed'
      if (c.key === 'done' && inc.status === 'partially_resolved') state = 'warn'
    } else if (!finished && !seenActive && i > 0 && C.slice(0, i).every((p) => has(steps, p.stages).length)) {
      state = 'active'
    }
    if (state === 'active') seenActive = true
    return { ...c, state, subText: mine.length || state === 'active' ? c.sub() : '' }
  })
}

const ICON = { done: '✓', active: '', failed: '✕', warn: '!', todo: '' }

export function Sidebar({ inc, killSwitch, live }) {
  const items = inc ? journey(inc, killSwitch) : []
  return (
    <aside className="side">
      <div className="side-title">Incident journey</div>
      {!inc ? <p className="muted side-empty">No incident yet. When something breaks, each step lights up here.</p> : (
        <>
          <div className="side-inc">{live ? 'Live' : 'Last incident'} · {inc.id}</div>
          <ol className="journey">
            {items.map((c, i) => (
              <li key={c.key} className={`j-${c.state}`}>
                <span className="dot">{c.state === 'active' ? <span className="spin" /> : ICON[c.state] || i + 1}</span>
                <div><div className="j-label">{c.label}</div>{c.subText && <div className="j-sub">{c.subText}</div>}</div>
              </li>
            ))}
          </ol>
        </>
      )}
      <div className="side-title links-title">Jump to</div>
      <nav className="side-links">
        <a href="#status">Status</a><a href="#map">Live map</a><a href="#services">Services</a><a href="#agent">AI agent</a>
        <a href="#incident">What happened</a><a href="#results">Test results</a><a href="#more">More details</a>
      </nav>
      <div className="side-safety">
        <b>Safety rules</b>
        <div>✓ every fix practised on a copy first</div>
        <div>✓ every fix can be undone</div>
        <div>✓ never deletes data</div>
        <div>✓ kill switch stops the AI</div>
        <div>✓ AI only reads; it never changes the shop</div>
        <div>✓ repeat problems: 0 AI tokens</div>
      </div>
    </aside>
  )
}

export function StatusHero({ state, connected, inc }) {
  let tone = 'ok', title = 'All systems healthy', text = 'Customers can shop and pay normally.'
  const unhealthy = state && (Object.values(state.services || {}).some((s) => s.health !== 'ok') || state.probe?.result === 'failed')
  const recent = !state?.active && inc && inc.ended && Date.now() / 1000 - inc.ended < 120
  if (!connected) { tone = 'off'; title = 'Agent offline'; text = 'The dashboard cannot reach the ShadowRun-Ops agent.' }
  else if (state?.active) {
    const a = state.active
    const cause = a.diagnosis ? plainCause(a.diagnosis.root_cause) : null
    const practising = a.steps?.some((s) => s.stage === 'simulation' && s.status === 'running') && !a.simulation
    tone = 'bad'
    title = cause ? `Problem found: ${cause}` : 'Problem detected: finding the cause…'
    if (a.status === 'awaiting_approval') {
      tone = 'warn'; title = 'Waiting for the on-call engineer'
      text = `The AI wants to ${plainAction(a.proposal).toLowerCase()}. This is risky (${a.approval_reason || 'needs a human decision'}), so it is asking first.`
    } else text = state.kill_switch ? 'The engineer has paused the AI (kill switch is on). It will not change anything.'
      : practising ? 'Practising the fix on a copy of the shop before touching the real one…'
      : a.proposal ? `Fixing it: ${plainAction(a.proposal).toLowerCase()}.` : 'Reading health checks, logs and recent changes.'
  } else if (unhealthy) { tone = 'warn'; title = 'Something looks wrong'; text = 'Confirming it is a real problem and not a blip…' }
  else if (recent && inc.status === 'resolved') {
    tone = 'fixed'; title = `Fixed: ${plainCause(inc.diagnosis?.root_cause)}`
    text = `${plainAction(inc.proposal)}. Recovered in ${inc.recovery_seconds}s, checked with a real order.`
  } else if (recent && inc.status === 'escalated') {
    tone = 'warn'; title = 'Handed to an engineer'; text = 'The agent could not prove a safe fix, so it left things as they were.'
  }
  return (
    <section id="status" className={`hero hero-${tone}`}>
      <div className="hero-icon">{{ ok: '✓', fixed: '✓', bad: '!', warn: '…', off: '×' }[tone]}</div>
      <div><div className="hero-title">{title}</div><div className="hero-text">{text}</div></div>
    </section>
  )
}

function friendly(name, s) {
  if (!s) return { word: 'Waiting', why: 'waiting for data', tone: '' }
  if (s.health === 'ok') return { word: 'Healthy', why: 'working normally', tone: 'ok' }
  if (s.oom_killed) return { word: 'Down', why: 'crashed: out of memory', tone: 'bad' }
  if (s.container_status === 'paused') return { word: 'Frozen', why: 'running, but not answering', tone: 'bad' }
  if (s.container_status !== 'running') return { word: 'Down', why: 'stopped', tone: 'bad' }
  const d = (s.detail || '').toLowerCase()
  if (d.includes('redis') || d.includes('session store')) return { word: 'Down', why: "can't reach login sessions (Redis)", tone: 'bad' }
  if (d.includes('database') || d.includes('5433') || d.includes('demo-db')) return { word: 'Down', why: "can't reach the database", tone: 'bad' }
  if (d.includes('timed out') || d.includes('unreachable')) return { word: 'Down', why: 'not answering', tone: 'bad' }
  return { word: 'Down', why: 'not working', tone: 'bad' }
}

export function SimpleServices({ state, rootService }) {
  const probe = state?.probe, cons = state?.consistency
  const version = (s) => (/"version":"([^"]+)"/.exec(s?.detail || '') || [])[1]
  const cards = Object.keys(NAMES).map((n) => {
    const s = state?.services?.[n]; const f = friendly(n, s)
    const root = rootService === n && !!state?.active
    const shown = root ? { word: 'Cause found', why: plainCause(state.active.diagnosis?.root_cause), tone: 'bad' } : f
    return { key: n, name: NAMES[n], ...shown, raw: s?.detail, extra: version(s) ? `v${version(s)}` : '', root }
  })
  cards.push({ key: 'checkout', name: 'Customer checkout', raw: probe?.detail,
    ...(probe?.result === 'ok' ? { word: 'Working', why: `customers can pay · ${probe.latency_ms ?? '…'} ms`, tone: 'ok' }
      : probe?.slow ? { word: 'Too slow', why: probe.detail, tone: 'bad' }
      : probe ? { word: 'Failing', why: "customers can't pay", tone: 'bad' } : { word: 'Waiting', why: '', tone: '' }) })
  cards.push({ key: 'money', name: 'Money vs orders', raw: cons?.detail,
    ...(cons?.result === 'failed' ? { word: 'Mismatch', why: 'money taken without a matching paid order', tone: 'bad' }
      : cons?.result === 'ok' ? { word: 'Matching', why: 'every payment has its order', tone: 'ok' } : { word: 'Waiting', why: '', tone: '' }) })
  return (
    <section id="services" className="sgrid">
      {cards.map((c) => (
        <div key={c.key} className={`scard ${c.tone} ${c.root ? 'root' : ''}`} title={c.raw || ''}>
          <div className="srow"><span className={`sdot ${c.tone}`} /><b>{c.name}</b>{c.extra && <span className="pill">{c.extra}</span>}</div>
          <div className={`sword ${c.tone}`}>{c.word}</div>
          <div className="swhy">{c.why}</div>
        </div>
      ))}
    </section>
  )
}

function useCountdown(deadline) {
  const [now, setNow] = useState(Date.now() / 1000)
  useEffect(() => { const t = setInterval(() => setNow(Date.now() / 1000), 500); return () => clearInterval(t) }, [])
  return Math.max(0, Math.ceil((deadline || 0) - now))
}

export function ApprovalCard({ inc, post }) {
  const left = useCountdown(inc.approval_deadline)
  const [sent, setSent] = useState(null)
  const p = inc.proposal, sim = inc.simulation
  const answer = async (yes) => {
    setSent(yes ? 'approving' : 'denying')
    await post(`/${yes ? 'approve' : 'deny'}/${p.action_id}?approver=${encodeURIComponent('on-call engineer')}`)
  }
  return (
    <div className="approval">
      <div className="ap-head"><span className="ap-bell">🔔</span>
        <div><b>On-call engineer: approval needed</b><div className="ap-sub">The AI will not do this without you. No answer in {left}s means no.</div></div></div>
      <div className="ap-grid">
        <div><span>Problem</span><b>{plainCause(inc.diagnosis?.root_cause)}</b></div>
        <div><span>The AI wants to</span><b>{plainAction(p)}</b></div>
        <div><span>Why it needs you</span><b>{inc.approval_reason || 'this fix is risky'}</b></div>
        <div><span>Practice run on the copy</span><b className={sim?.status === 'passed' ? 'ap-ok' : ''}>
          {sim?.status === 'passed' ? '✓ the fix worked on the copy' : sim ? sim.status.replace('_', ' ') : 'not run'}</b></div>
      </div>
      <div className="ap-buttons">
        <button className="approve big" disabled={!!sent} onClick={() => answer(true)}>{sent === 'approving' ? 'Approving…' : '✓ Approve the fix'}</button>
        <button className="deny big" disabled={!!sent} onClick={() => answer(false)}>{sent === 'denying' ? 'Denying…' : '✕ Deny'}</button>
      </div>
    </div>
  )
}

export function SimpleIncident({ inc, post }) {
  const d = inc.diagnosis, p = inc.proposal
  const outcome = {
    resolved: `Fixed in ${inc.recovery_seconds}s and checked with a real order.`,
    partially_resolved: 'This part is fixed. Another problem is still being handled.',
    escalated: 'Handed to an engineer. Nothing was left half-changed.',
    awaiting_approval: 'Waiting for the on-call engineer to approve the fix.',
  }[inc.status] || 'In progress…'
  return (
    <div className="card simple-inc">
      {inc.status === 'awaiting_approval' && inc.proposal && <ApprovalCard inc={inc} post={post} />}
      <h3>What happened <span className={`pill st-${inc.status}`}>{inc.status.replace('_', ' ')}</span>
        {inc.recurring && <span className="pill v-BLOCK">happened {inc.recurring}× in 10 min</span>}</h3>
      <div className="si-row"><span>1</span><div><b>The problem</b><p>{(inc.event?.affected_services || []).map(nm).join(', ')} stopped working for customers.</p></div></div>
      <div className="si-row"><span>2</span><div><b>The cause</b><p>{d ? <>It was <b>{plainCause(d.root_cause)}</b>.</> : 'Still investigating…'}</p>
        {d?.root_cause?.supporting?.length > 0 && <ul>{d.root_cause.supporting.slice(0, 3).map((e, i) => <li key={i}>{e}</li>)}</ul>}</div></div>
      <div className="si-row"><span>3</span><div><b>What the AI did</b><p>{p ? <>{plainAction(p)}. It practised this on a copy of the shop first
        {inc.simulation ? ` (${inc.simulation.status === 'passed' ? 'it worked' : inc.simulation.status.replace('_', ' ')})` : ''}.</> : 'Choosing a safe fix…'}</p>
        {inc.approval_reason && <p className="muted">Because {inc.approval_reason}, it asked the on-call engineer first
          {inc.steps?.some((s) => s.stage === 'approval' && s.status === 'approved') ? ', who approved.' : inc.steps?.some((s) => s.stage === 'approval' && s.status === 'denied') ? ', who said no.' : '.'}</p>}</div></div>
      <div className="si-row"><span>4</span><div><b>The result</b><p>{outcome}</p>
        {inc.impact && <p className="muted">Customer impact: {inc.impact.failed_checkouts} failed checkouts during the outage.</p>}
        {inc.follow_up && <p className="followup">{inc.follow_up.detail}</p>}
        {inc.result?.undo && !inc.undone && inc.status !== 'escalated' && (
          <button className="small" onClick={() => {
            if (confirm('Undo the agent\'s fix? This puts back exactly what was there before (it may re-create the problem).')) post(`/undo/${inc.id}`)
          }}>Undo this fix</button>)}
        {inc.undone && <span className="pill v-skipped">undone</span>}</div></div>
      {d?.suspicious_log_lines?.length > 0 && <div className="si-alert">🛡️ {d.suspicious_log_lines.length} hidden instruction(s) to the AI were found in the logs and ignored.</div>}
    </div>
  )
}
