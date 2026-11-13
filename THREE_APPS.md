# The three applications

| App | Who uses it | Address | Talks to |
|---|---|---|---|
| **QuickCart** | Customers (and judges, to see the damage) | http://localhost:3000 | the shop only |
| **Ghost Injector** | Judges | http://localhost:7070 | the shop only (never the agent) |
| **ShadowRun-Ops** | Engineers | http://localhost:5173 | the agent |

From Laptop B or a phone on the same hotspot, replace `localhost` with Laptop A's IP. Each app links to the others: the dashboard's top bar, and the Ghost Injector's buttons.

## QuickCart (new React app, `quickcart/`)
- Header with "delivery in 10 minutes", search, login/account, cart button showing item count and total
- Hero banner, 6 categories, 24 products with discount badges, ADD → `– 2 +` steppers
- Product details pop-up, cart drawer with bill details and savings
- Login / register (phone + password; synthetic accounts, sessions kept in Redis)
- Payment choice (simulated UPI / card / cash), order-placed screen with a 10-minute countdown
- My orders: order history with items
- **When a judge breaks something, customers see a friendly error** ("Payment failed. Please try again in a moment.", "The payment took too long…", "We are having trouble keeping you signed in…")

## Backend additions
- Products have categories, MRP, emoji "photo", unit and description (`shop/services/catalog.py`)
- Cart: set exact quantity / remove (`POST /api/cart/set`)
- Accounts: `POST /api/auth/register`, `/api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout`
- Orders store their items (`GET /api/orders/{user_id}`)
- Everything the agent and the fake customers use is unchanged.

## Install (Laptop A)
1. Stop everything: close the agent, dashboard, fake customers and judge windows.
2. Extract this zip into `shadowrun-ops`, replacing files:
   ```powershell
   cd ~\Documents\shadowrun-ops
   Expand-Archive -Path shadowrun-ops-three-apps.zip -DestinationPath . -Force
   del shadowrun-ops-three-apps.zip
   ```
3. **Rebuild the shop and the shadow copy** (new catalogue, accounts and cart code):
   ```powershell
   cd shop
   docker compose up -d --build
   docker compose -f docker-compose.shadow.yml up -d --build
   cd ..
   ```
4. Install QuickCart once (needs internet):
   ```powershell
   cd quickcart
   npm install
   cd ..
   ```
5. Start everything (now 5 windows: agent, dashboard, fake customers, Ghost Injector, QuickCart):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
   ```
6. Check: `powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1` should say READY (it now checks QuickCart too).
7. Commit: `git add .`, `git commit -m "Three apps: QuickCart, Ghost Injector, ShadowRun-Ops"`, `git push`.

## Demo flow with three screens
1. **QuickCart:** create an account, add a few things, pay. "This is the customer."
2. **Ghost Injector:** a judge clicks **Payment failure**.
3. **QuickCart:** try to pay → "Payment failed. Please try again." "Customers are hurting."
4. **ShadowRun-Ops:** red map, money lost, root cause, practice run on the shadow copy, fix, resolved.
5. **QuickCart:** pay again → order placed. "Fixed before most customers noticed."

## Troubleshooting
| Problem | Fix |
|---|---|
| QuickCart shows "We could not reach QuickCart" | The shop isn't running on 8080. Run `inject.py reset`, or check `docker ps` |
| Only 4 products in QuickCart | The cart container wasn't rebuilt: `cd shop; docker compose up -d --build cart` |
| `npm install` fails | It needs internet. Use the phone hotspot with mobile data |
| Port 3000 busy | Close the old QuickCart window, or change `port` in `quickcart\vite.config.js` |
| Login says "session store unavailable" | Redis is down (maybe a judge injected it). The agent fixes it; then try again |
