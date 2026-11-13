import { useCallback, useEffect, useMemo, useState } from 'react'

const rs = (p) => '₹' + Math.round((p || 0) / 100).toLocaleString('en-IN')
const off = (p) => (p.mrp_paise > p.price_paise ? Math.round((1 - p.price_paise / p.mrp_paise) * 100) : 0)

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback } catch { return fallback }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode */ }
}

async function api(method, path, body, token) {
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  let r
  try {
    r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined })
  } catch {
    throw new Error('We could not reach QuickCart. Please check your connection.')
  }
  let data = {}
  try { data = await r.json() } catch { /* empty body */ }
  if (!r.ok) throw new Error(friendly(r.status, data.detail))
  return data
}

function friendly(status, detail) {
  const d = String(detail || '')
  if (d.includes('session store')) return 'We are having trouble keeping you signed in. Please try again in a moment.'
  if (d.includes('gateway timeout')) return 'The payment took too long and was not completed. You have not been charged twice. Please try again.'
  if (d.includes('order could not be created')) return 'Your payment went through but we could not create your order. Our team has been alerted.'
  if (d.includes('payment failed')) return 'Payment failed. Please try again in a moment.'
  if (status === 401 || status === 409 || status === 400) return d.charAt(0).toUpperCase() + d.slice(1)
  if (status >= 500) return 'Something went wrong on our side. Please try again in a moment.'
  return d || 'Something went wrong.'
}

function useToast() {
  const [toast, setToast] = useState(null)
  const show = useCallback((text, kind = 'error') => {
    setToast({ text, kind, id: Date.now() })
    setTimeout(() => setToast((t) => (t && Date.now() - t.id >= 3900 ? null : t)), 4000)
  }, [])
  return [toast, show]
}

function Stepper({ qty, onChange, busy, big }) {
  if (!qty) return <button className={`add ${big ? 'big' : ''}`} disabled={busy} onClick={(e) => { e.stopPropagation(); onChange(1) }}>ADD</button>
  return (
    <div className={`stepper ${big ? 'big' : ''}`} onClick={(e) => e.stopPropagation()}>
      <button disabled={busy} onClick={() => onChange(qty - 1)} aria-label="Remove one">−</button>
      <span>{qty}</span>
      <button disabled={busy} onClick={() => onChange(qty + 1)} aria-label="Add one">+</button>
    </div>
  )
}

function ProductCard({ p, qty, onQty, busy, onOpen }) {
  return (
    <div className="pcard" onClick={() => onOpen(p)}>
      <div className="pimg">{p.emoji}{off(p) > 0 && <span className="off">{off(p)}% OFF</span>}</div>
      <div className="eta">⏱ 10 MINS</div>
      <div className="pname">{p.name}</div>
      <div className="punit">{p.unit}</div>
      <div className="pfoot">
        <div><div className="price">{rs(p.price_paise)}</div>
          {p.mrp_paise > p.price_paise && <div className="mrp">{rs(p.mrp_paise)}</div>}</div>
        <Stepper qty={qty} onChange={(q) => onQty(p, q)} busy={busy} />
      </div>
    </div>
  )
}

function Modal({ children, onClose, wide }) {
  return (
    <div className="overlay" onClick={onClose}>
      <div className={`modal ${wide ? 'wide' : ''}`} onClick={(e) => e.stopPropagation()}>
        <button className="x" onClick={onClose} aria-label="Close">×</button>
        {children}
      </div>
    </div>
  )
}

function AuthModal({ onClose, onDone, notify }) {
  const [mode, setMode] = useState('login')
  const [f, setF] = useState({ name: '', phone: '', password: '' })
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => { setF({ ...f, [k]: e.target.value }); setErr('') }
  const submit = async (e) => {
    e.preventDefault()
    const phone = f.phone.replace(/\D/g, '')
    if (mode === 'register' && !f.name.trim()) return setErr('Enter your name')
    if (phone.length !== 10) return setErr('Enter a 10-digit phone number')
    if (f.password.length < 4) return setErr('Password must be at least 4 characters')
    setBusy(true)
    try {
      const body = mode === 'login' ? { phone, password: f.password } : { name: f.name, phone, password: f.password }
      const r = await api('POST', `/api/auth/${mode}`, body)
      onDone(r)
      notify(`Welcome${mode === 'register' ? '' : ' back'}, ${r.user.name}!`, 'ok')
    } catch (e2) { setErr(e2.message) } finally { setBusy(false) }
  }
  return (
    <Modal onClose={onClose}>
      <div className="auth">
        <div className="brand big">⚡ quick<b>cart</b></div>
        <h2>{mode === 'login' ? 'Log in' : 'Create your account'}</h2>
        <p className="muted">{mode === 'login' ? 'Log in to check out and see your orders.' : 'Takes 10 seconds. No real details needed: this is a demo shop.'}</p>
        <form onSubmit={submit}>
          {mode === 'register' && <input placeholder="Your name" value={f.name} onChange={set('name')} autoFocus />}
          <input placeholder="Phone number" inputMode="numeric" value={f.phone} onChange={set('phone')} autoFocus={mode === 'login'} />
          <input placeholder="Password" type="password" value={f.password} onChange={set('password')} />
          {err && <div className="err">{err}</div>}
          <button className="primary" disabled={busy}>{busy ? 'Please wait…' : mode === 'login' ? 'Log in' : 'Create account'}</button>
        </form>
        <button className="link" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setErr('') }}>
          {mode === 'login' ? 'New to QuickCart? Create an account' : 'Already have an account? Log in'}
        </button>
      </div>
    </Modal>
  )
}

function CartDrawer({ cart, onClose, onQty, busy, onCheckout, user }) {
  const savings = cart.items.reduce((s, i) => s + (i.mrp_paise - i.price_paise) * i.qty, 0)
  return (
    <div className="overlay right" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <header><h2>My Cart</h2><button className="x static" onClick={onClose} aria-label="Close">×</button></header>
        {cart.items.length === 0 ? (
          <div className="empty"><div className="big-emoji">🛒</div><h3>Your cart is empty</h3>
            <p className="muted">Add something tasty. It'll be at your door in 10 minutes.</p>
            <button className="primary" onClick={onClose}>Browse products</button></div>
        ) : (
          <>
            <div className="delivery"><span className="bolt">⚡</span><div><b>Delivery in 10 minutes</b>
              <div className="muted">Shipment of {cart.items.reduce((s, i) => s + i.qty, 0)} items</div></div></div>
            <div className="lines">
              {cart.items.map((i) => (
                <div className="line" key={i.id}>
                  <div className="limg">{i.emoji}</div>
                  <div className="lmid"><div>{i.name}</div><div className="muted">{i.unit}</div>
                    <div><b>{rs(i.price_paise)}</b> {i.mrp_paise > i.price_paise && <s className="muted">{rs(i.mrp_paise)}</s>}</div></div>
                  <Stepper qty={i.qty} onChange={(q) => onQty(i, q)} busy={busy} />
                </div>
              ))}
            </div>
            <div className="bill">
              <h3>Bill details</h3>
              <div className="row"><span>Items total</span><span>{rs(cart.total_paise + savings)}</span></div>
              {savings > 0 && <div className="row save"><span>Product discount</span><span>−{rs(savings)}</span></div>}
              <div className="row"><span>Delivery charge</span><span><s className="muted">₹30</s> <b className="free">FREE</b></span></div>
              <div className="row total"><span>Grand total</span><span>{rs(cart.total_paise)}</span></div>
              {savings > 0 && <div className="saved">You are saving {rs(savings)} on this order 🎉</div>}
            </div>
            <footer>
              <button className="primary pay" onClick={onCheckout} disabled={busy}>
                <span>{rs(cart.total_paise)}<small>TOTAL</small></span>
                <span>{user ? 'Proceed to pay ›' : 'Log in to proceed ›'}</span>
              </button>
            </footer>
          </>
        )}
      </aside>
    </div>
  )
}

function PayModal({ total, onClose, onPay }) {
  const [method, setMethod] = useState('upi')
  const [busy, setBusy] = useState(false)
  const pay = async () => { setBusy(true); try { await onPay() } finally { setBusy(false) } }
  return (
    <Modal onClose={busy ? () => {} : onClose}>
      <h2>Choose a payment method</h2>
      <p className="muted">Simulated payments: no real money moves.</p>
      {[['upi', '📱', 'UPI', 'Pay with any UPI app'], ['card', '💳', 'Credit / debit card', 'Visa, Mastercard, RuPay'],
        ['cod', '💵', 'Cash on delivery', 'Pay when it arrives']].map(([k, icon, t, s]) => (
        <label key={k} className={`pm ${method === k ? 'on' : ''}`}>
          <input type="radio" checked={method === k} onChange={() => setMethod(k)} />
          <span className="pmicon">{icon}</span><span><b>{t}</b><div className="muted">{s}</div></span>
        </label>
      ))}
      <button className="primary full" disabled={busy} onClick={pay}>{busy ? 'Processing payment…' : `Pay ${rs(total)}`}</button>
    </Modal>
  )
}

function Success({ order, onClose }) {
  const [left, setLeft] = useState(600)
  useEffect(() => { const t = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000); return () => clearInterval(t) }, [])
  return (
    <Modal onClose={onClose}>
      <div className="success">
        <div className="tick">✓</div>
        <h2>Order placed!</h2>
        <p className="muted">Order #{order.order_id} · {rs(order.amount_paise)}</p>
        <div className="eta-big">Arriving in <b>{Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')}</b> min</div>
        <div className="rider">🛵 Your rider is picking up your order</div>
        <button className="primary full" onClick={onClose}>Continue shopping</button>
      </div>
    </Modal>
  )
}

function Orders({ user, token, onBack, notify }) {
  const [orders, setOrders] = useState(null)
  useEffect(() => {
    api('GET', `/api/orders/${user.id}`, null, token).then(setOrders).catch((e) => { notify(e.message); setOrders([]) })
  }, [user.id, token, notify])
  return (
    <main className="page">
      <button className="link" onClick={onBack}>‹ Back to shopping</button>
      <h1>My orders</h1>
      {orders === null ? <p className="muted">Loading…</p> : orders.length === 0 ? <p className="muted">No orders yet.</p> : (
        <div className="orders">
          {orders.map((o) => (
            <div className="order" key={o.id}>
              <div className="ohead"><b>Order #{o.id}</b><span className={`status s-${o.status}`}>{o.status === 'paid' ? 'Delivered' : o.status}</span></div>
              <div className="muted">{new Date(o.created_at).toLocaleString('en-IN')}</div>
              <div className="oitems">{(o.items || []).map((i, k) => <span key={k} title={i.name}>{i.emoji} {i.qty}× {i.name}</span>)}</div>
              <div className="ototal">{rs(o.amount_paise)}</div>
            </div>
          ))}
        </div>
      )}
    </main>
  )
}

export default function App() {
  const [categories, setCategories] = useState([])
  const [products, setProducts] = useState([])
  const [cat, setCat] = useState('')
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [cart, setCart] = useState({ items: [], total_paise: 0 })
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState('shop')
  const [open, setOpen] = useState(null)
  const [drawer, setDrawer] = useState(false)
  const [authOpen, setAuthOpen] = useState(false)
  const [paying, setPaying] = useState(false)
  const [placed, setPlaced] = useState(null)
  const [menu, setMenu] = useState(false)
  const [session, setSession] = useState(() => load('qc_session', null))
  const [guest] = useState(() => { const g = load('qc_guest', null) || 'guest-' + Math.random().toString(36).slice(2, 10); save('qc_guest', g); return g })
  const [toast, notify] = useToast()
  const user = session?.user
  const uid = user?.id || guest

  const refreshCart = useCallback(async (id = uid) => {
    try { setCart(await api('GET', `/api/cart/${id}`)) } catch (e) { notify(e.message) }
  }, [uid, notify])

  useEffect(() => {
    api('GET', '/api/categories').then(setCategories).catch(() => {})
    api('GET', '/api/products').then(setProducts).catch((e) => notify(e.message))
  }, [notify])
  useEffect(() => { refreshCart() }, [refreshCart])
  useEffect(() => {  // a session that expired on the server is cleared here
    if (!session?.token) return
    api('GET', '/api/auth/me', null, session.token).catch(() => { setSession(null); save('qc_session', null) })
  }, [session?.token])
  useEffect(() => {
    if (!q.trim()) { setResults(null); return }
    const t = setTimeout(() => api('GET', `/api/search?q=${encodeURIComponent(q.trim())}`).then(setResults).catch((e) => notify(e.message)), 250)
    return () => clearTimeout(t)
  }, [q, notify])

  const qtyOf = useMemo(() => Object.fromEntries(cart.items.map((i) => [i.id, i.qty])), [cart])
  const count = cart.items.reduce((s, i) => s + i.qty, 0)

  const setQty = async (p, qty) => {
    setBusy(true)
    try {
      await api('POST', '/api/cart/set', { user_id: uid, product_id: p.id, qty: Math.max(0, qty) })
      await refreshCart()
    } catch (e) { notify(e.message) } finally { setBusy(false) }
  }

  const onLogin = async (r) => {
    const s = { token: r.token, user: r.user }
    // carry the guest cart over to the account
    for (const i of cart.items) {
      try { await api('POST', '/api/cart/set', { user_id: r.user.id, product_id: i.id, qty: i.qty }) } catch { /* best effort */ }
    }
    setSession(s); save('qc_session', s); setAuthOpen(false)
    refreshCart(r.user.id)
  }
  const logout = async () => {
    try { await api('POST', '/api/auth/logout', null, session?.token) } catch { /* ignore */ }
    setSession(null); save('qc_session', null); setMenu(false); setView('shop'); refreshCart(guest)
    notify('You are logged out', 'ok')
  }
  const checkout = () => {
    if (!user) { setAuthOpen(true); return }
    setDrawer(false); setPaying(true)
  }
  const pay = async () => {
    try {
      const r = await api('POST', `/api/checkout/${uid}`, null, session?.token)
      setPaying(false); setPlaced(r); refreshCart()
    } catch (e) { notify(e.message); refreshCart() }
  }

  const shown = results ?? (cat ? products.filter((p) => p.category === cat) : null)
  const sections = categories.map((c) => ({ ...c, items: products.filter((p) => p.category === c.id) })).filter((s) => s.items.length)

  return (
    <div className="app">
      <header className="top">
        <div className="brand" onClick={() => { setView('shop'); setCat(''); setQ('') }}>⚡ quick<b>cart</b></div>
        <div className="where"><b>Delivery in 10 minutes</b><span className="muted">Home · Bengaluru 560078 ▾</span></div>
        <div className="search"><span>🔍</span>
          <input placeholder='Search "milk", "chips", "tea"…' value={q} onChange={(e) => { setQ(e.target.value); setView('shop') }} />
          {q && <button className="clear" onClick={() => setQ('')} aria-label="Clear search">×</button>}</div>
        {user ? (
          <div className="account">
            <button className="ghost" onClick={() => setMenu(!menu)}>Account ▾</button>
            {menu && <div className="menu" onMouseLeave={() => setMenu(false)}>
              <div className="who"><b>{user.name}</b><div className="muted">+91 {user.phone}</div></div>
              <button onClick={() => { setView('orders'); setMenu(false) }}>My orders</button>
              <button onClick={logout}>Log out</button>
            </div>}
          </div>
        ) : <button className="ghost" onClick={() => setAuthOpen(true)}>Login</button>}
        <button className="cartbtn" onClick={() => setDrawer(true)}>
          🛒 {count ? <span><b>{count} item{count > 1 ? 's' : ''}</b><br />{rs(cart.total_paise)}</span> : <span>My Cart</span>}
        </button>
      </header>

      {view === 'orders' && user ? <Orders user={user} token={session.token} onBack={() => setView('shop')} notify={notify} /> : (
        <main className="page">
          {!q && (
            <>
              <section className="hero">
                <div><h1>Groceries at your door<br />in 10 minutes</h1><p>Fresh fruit, daily essentials and snacks, delivered faster than you can find your keys.</p>
                  <button className="primary" onClick={() => document.getElementById('shop')?.scrollIntoView({ behavior: 'smooth' })}>Shop now</button></div>
                <div className="hero-art">🛵<span>🥛</span><span>🍌</span><span>🍫</span></div>
              </section>
              <section className="cats">
                <button className={`cat ${!cat ? 'on' : ''}`} onClick={() => setCat('')}><span>🛍️</span>All</button>
                {categories.map((c) => (
                  <button key={c.id} className={`cat ${cat === c.id ? 'on' : ''}`} onClick={() => setCat(c.id)}><span>{c.emoji}</span>{c.name}</button>
                ))}
              </section>
            </>
          )}
          <div id="shop" />
          {shown ? (
            <section>
              <h2 className="sec">{q ? `Results for "${q}"` : categories.find((c) => c.id === cat)?.name}</h2>
              {shown.length === 0 ? <p className="muted">Nothing found. Try "milk" or "tea".</p> : (
                <div className="grid">{shown.map((p) => <ProductCard key={p.id} p={p} qty={qtyOf[p.id]} onQty={setQty} busy={busy} onOpen={setOpen} />)}</div>
              )}
            </section>
          ) : sections.length === 0 ? (
            <div className="loading">Loading the shop…</div>
          ) : sections.map((s) => (
            <section key={s.id}>
              <div className="sechead"><h2 className="sec">{s.name}</h2><button className="link" onClick={() => setCat(s.id)}>see all ›</button></div>
              <div className="grid">{s.items.slice(0, 6).map((p) => <ProductCard key={p.id} p={p} qty={qtyOf[p.id]} onQty={setQty} busy={busy} onOpen={setOpen} />)}</div>
            </section>
          ))}
          <footer className="foot">QuickCart is a demo shop for the ShadowRun-Ops project. All products, payments and orders are simulated.</footer>
        </main>
      )}

      {count > 0 && !drawer && view === 'shop' && (
        <button className="floatcart" onClick={() => setDrawer(true)}>
          <span>🛒 {count} item{count > 1 ? 's' : ''} · {rs(cart.total_paise)}</span><span>View cart ›</span>
        </button>
      )}

      {open && (
        <Modal onClose={() => setOpen(null)} wide>
          <div className="detail">
            <div className="dimg">{open.emoji}</div>
            <div>
              <div className="eta">⏱ 10 MINS</div>
              <h2>{open.name}</h2>
              <div className="muted">{open.unit}</div>
              <div className="dprice">{rs(open.price_paise)} {open.mrp_paise > open.price_paise && <><s className="muted">MRP {rs(open.mrp_paise)}</s> <span className="offtag">{off(open)}% OFF</span></>}</div>
              <p>{open.description}</p>
              <Stepper qty={qtyOf[open.id]} onChange={(q2) => setQty(open, q2)} busy={busy} big />
              <ul className="perks"><li>⚡ Superfast delivery</li><li>↩️ Easy returns</li><li>✅ Quality checked</li></ul>
            </div>
          </div>
        </Modal>
      )}
      {drawer && <CartDrawer cart={cart} onClose={() => setDrawer(false)} onQty={setQty} busy={busy} onCheckout={checkout} user={user} />}
      {authOpen && <AuthModal onClose={() => setAuthOpen(false)} onDone={onLogin} notify={notify} />}
      {paying && <PayModal total={cart.total_paise} onClose={() => setPaying(false)} onPay={pay} />}
      {placed && <Success order={placed} onClose={() => setPlaced(null)} />}
      {toast && <div key={toast.id} className={`toast ${toast.kind}`}>{toast.text}</div>}
    </div>
  )
}
