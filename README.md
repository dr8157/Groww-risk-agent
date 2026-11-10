# Groww Risk Agent

A **deterministic risk manager** that runs beside a Groww account and enforces loss limits automatically.

- It **never opens trades**. It only watches positions and squares them off when a rule is breached.
- Rules are set calmly **before** the session and cannot be loosened **during** it.
- It is a **discipline tool, not a security boundary**: root on the server can stop it, but doing so takes deliberate effort outside market hours.

> **This repository is public.** Never commit API keys, TOTP secrets, Telegram tokens or `.env` files.
> Secrets live only on the server (`/etc/risk-agent/secrets.env`) and are entered by the owner.
> Not financial advice. Trading involves risk of loss.

Status: v3.1, tested in mock mode (27 tests). **Not yet connected to a live account.**

---

## 1. Coverage

| Segment | Product | Covered | Notes |
|---|---|---|---|
| NSE / BSE cash | Intraday (MIS) | Yes | Groww `CASH` |
| NSE / BSE cash | Delivery (CNC) | Yes | Only quantity bought **today** is exitable; older holdings are never sold |
| F&O: Nifty, Bank Nifty, Sensex, Finnifty, stock F&O | Intraday and NRML, options and futures | Yes | Groww `FNO` |
| MCX commodities | Intraday and NRML | **Yes, pending live check** | The SDK has a `COMMODITY` segment. If your account/API refuses it, the agent sends a Telegram alert ("commodities NOT protected") and retries every 10 minutes. It never drops it silently |

Run `python -m risk_agent.main --check` against the real account to confirm each segment.

Market hours used for exits: NSE/BSE 09:15-15:30, MCX 09:00-23:55 (set `RISK_AGENT_MCX_CLOSE=23:30` during US daylight saving). Exchange holidays: list dates in `data/holidays.json`, e.g. `{"NSE": ["2026-11-10"], "MCX": []}`.

## 2. Rules

| Rule | Value | On breach |
|---|---|---|
| Daily loss limit | ₹10,000 | Day P&L (realised + unrealised) <= -10k: cancel pending orders, square off everything, **LOCKED for the rest of the day**. New positions are reversed within about 1-2 s; new pending orders are cancelled every tick |
| Weekly loss limit | ₹30,000 | Week P&L (Mon to today) <= -30k: same lock, re-applied every day until Monday |
| Max executed orders per day | 30 | After the 30th, any order that opens or adds size is reversed. Closing is always allowed. The agent's own exits are not counted |

No overnight-carry rule: the owner does not hold trades overnight. Charges are ignored in P&L for now.

**Change control.** Rules can be changed only between **00:00 and 09:00 IST**, frozen after. Enforced twice: the dashboard form locks after 09:00, and the engine rejects any config saved at or after 09:00. The engine checks both the `saved_at` inside the file **and the file's real modification time**, so hand-editing a forged timestamp does not work.

**Built but OFF** (enable later via the 09:00 window): profit protection (arm at +₹15k peak, lock if 50% is given back) and an anti-revenge cool-down (20 minutes after 50% of the daily limit lost or 2 losses in a row).

## 3. Unattended login (no daily clicks)

Groww access tokens expire at **06:00 IST every day**. The agent handles this itself:

1. Use a Groww **TOTP API key** (Groww Cloud -> API Keys). The agent generates its own 6-digit codes from the stored TOTP secret. The *approval* key type needs a manual click every morning and must not be used.
2. At about **06:02** the agent mints the day's token before any call fails.
3. A failed login never crashes the agent. It retries with backoff (5 s up to 5 min), and failed attempts do not use the daily budget (max 100 tokens/day of Groww's 150).
4. Many threads hitting an expired token trigger **one** re-login, not many.
5. You get a Telegram alert if login keeps failing: a warning early on, **CRITICAL "not protected"** on weekdays from 08:30.

One-time setup by you: generate the TOTP key, put `GROWW_API_KEY` and `GROWW_TOTP_SECRET` in the server's secrets file. The authenticator app is only needed for that one-time step.

## 4. Layout

```
risk_agent/  config.py errors.py ratelimit.py brokers.py executor.py engine.py notify.py main.py
tests/       test_engine.py test_auth.py conftest.py
.github/workflows/tests.yml   runs pytest on every push
```

Coming next (still in the owner's Drive handoff folder, not yet ported): `dashboard.py` (Streamlit) and `deploy/` (VPS setup and update scripts, to be replaced by a GitHub Action deploy).

## 5. Safety principles

1. **Reduce-only exits.** Never sell more than the live quantity minus quantity already in flight.
2. **Never trap the user.** Closing orders are never blocked.
3. **Fail loud.** An unsupported segment, stale price, outage or failed login sends an alert. Nothing is dropped silently.
4. **Chaser, not a gate.** The API cannot block an order before it is placed. Also use Groww's in-app kill switch after a lock.
5. **A limit is a trigger, not a guarantee.** In gaps or illiquid strikes the realised loss can exceed ₹10k.
6. **Staged roll-out:** alert-only, then enforce with a 1-order cap, then real limits.

## 6. Commands

```bash
python -m pytest -q                    # tests
python -m risk_agent.main --mock       # simulated account
python -m risk_agent.main --check      # live, read-only: login, segments, positions, LTP, orders
python -m risk_agent.main              # live enforcement loop
```

## 7. Open questions (settle with `--check`)

1. Does your account return MCX positions through the API?
2. Position/order field names (`net_price`, `quantity`, `net_carry_forward_quantity`, `credit_quantity`, `debit_quantity`, `order_reference_id`, `created_at`).
3. P&L parity with the Groww app, and whether `realised_pnl` is before or after charges.
4. Freeze quantities vs current NSE/BSE circulars.
5. Are MARKET orders accepted for options? If not, exits fall back to a LIMIT 2% through LTP after 2 rejections.
6. Does Groww require the VPS's static IP to be registered for order APIs?

## 8. Roll-out test (3 days)

| Day | Settings (before 09:00) | Pass criteria |
|---|---|---|
| 1 | alert_only | Dashboard P&L tracks the Groww app; alerts arrive |
| 2 | enforce, max orders = 1 | The second order is reversed in about 2 s |
| 3 | enforce, ₹10k / ₹30k / 30 | Normal use |
