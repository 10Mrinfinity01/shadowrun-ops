# Phase 5a: your vision, part 1 (before the mentor session)

## What's new

| Area | What |
|---|---|
| **Human approval (principle #3)** | Dashboard switch **Approval: always ask / only when unsure**. Default **always ask**: an engineer approves every fix. The chaos scoreboard temporarily switches to "only when unsure" and switches back when done |
| **Payment category (4/4)** | Payment failure (`bad_deploy`), **payment timeout**, **paid but no order**, **payment/order status mismatch** |
| **Consistency check** | New monitor check: every captured payment must have an order marked paid. It catches the *silent* mismatch bug where checkout still looks fine. New dashboard card **payment ↔ order check** |
| **Money follow-up** | After a payment bug, the incident lists what a human must clean up (refunds, wrong statuses). The agent never edits payment data itself |
| **Advanced category** | Database down, **Redis down** (new session store), **API slowdown** (checkout response time > 1.2 s counts as failing), broken config, crash (memory leak) |
| **Judge Injector web app** | http://localhost:7070: buttons by category, a reset button, and a log. Never talks to the agent. Auth and cart cards show "coming next" |
| **Shop** | Redis session store, real `payments` table, simulated payment gateway, order history API (`/api/orders/{user}`) |

| Scenario | The agent should find | Fix |
|---|---|---|
| `payment_timeout` | `bad_config:payment` (gateway_timeout_ms 2000 → 20) | restore config |
| `paid_no_order` | `bad_deploy:payment` (2.4) + consistency failing | rollback + follow-up: refund list |
| `status_mismatch` | `bad_deploy:payment` (2.5), found by the consistency check while checkout looks fine | rollback + follow-up: fix statuses |
| `redis_down` | `container_down:redis` (storefront is a symptom) | start shop-redis |
| `api_slow` | `bad_deploy:cart` (2.6) + "too slow" checkout + slow-query warnings | rollback cart |

## Install (Laptop A)

1. Stop the agent, dashboard and fake customers (Ctrl + C in each).
2. Extract, replacing files:
   ```powershell
   cd ~\Documents\shadowrun-ops
   Expand-Archive -Path shadowrun-ops-phase5a.zip -DestinationPath . -Force
   del shadowrun-ops-phase5a.zip
   ```
3. **Rebuild the shop and the shadow copy** (new code + Redis):
   ```powershell
   cd shop
   docker compose up -d --build
   docker compose -f docker-compose.shadow.yml up -d --build
   cd ..
   ```
4. Tests:
   ```powershell
   .\venv\Scripts\python.exe -m unittest tests.test_phase1 tests.test_phase2 tests.test_phase2b tests.test_phase3 tests.test_phase5
   ```
   You should see `Ran 59 tests ... OK`.
5. Start everything (now 4 windows, including the judge app):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
   ```
6. `powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1` should say READY (it now checks 12 containers, the judge app and the approval mode).
7. Commit on a branch, so `main` stays the safe demo:
   ```powershell
   git checkout -b vision-features
   git add .
   git commit -m "Phase 5a: payment + advanced categories, approval mode, judge app"
   git push -u origin vision-features
   ```

## Try it
Open the judge app at http://localhost:7070 and click **Payment / order mismatch**. On the dashboard:
- checkout stays green, but the **payment ↔ order check** card turns red
- the incident appears → root cause `bad_deploy:payment` → practice run passes → **Approve / Deny** buttons
- click **Approve** → rollback → **Follow-up** row lists the orders a human must correct

Then try `redis_down`, `api_slow`, `payment_timeout` and `paid_no_order` (about 60 s apart).

## For Laptop B: build the modern QuickCart UI
A React app (Zepto/Blinkit style) that calls the storefront's API at `http://<Laptop A IP>:8080`:

| Call | Does |
|---|---|
| `GET /api/products` | product list (`id`, `name`, `price_paise`) |
| `GET /api/search?q=chai` | search |
| `POST /api/cart/add` body `{"user_id","product_id","qty"}` | add to cart |
| `GET /api/cart/{user_id}` | cart items + `total_paise` |
| `POST /api/checkout/{user_id}` | pay (simulated) → `order_id` |
| `GET /api/orders/{user_id}` | order history |

Login/register, categories, remove/update quantity and product details come with the auth + cart phase after the mentor session. Build the screens now with placeholders for those.

## Troubleshooting
| Problem | Fix |
|---|---|
| Storefront red right after the rebuild | Redis starts with it. Wait 10 s, or `inject.py reset` |
| `status_mismatch` raises no incident | The consistency check needs orders flowing: keep the fake customers running |
| Approve buttons don't appear | Approval switch must say **always ask** |
| Judge app port 7070 in use | Use another port in `start_all.ps1` and the preflight script |
| Chaos run is long | It now has 10 scenarios. Pick some: `--scenarios status_mismatch,redis_down,api_slow --runs 1` |
