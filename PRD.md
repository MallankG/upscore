# Upscore: PRD

> A personal agent that watches your spending and coaches your credit score up, one on-time payment at a time.
> Built for Fly.io Hacksgiving 2026. It runs on a Sprite and uses three Sprites connectors: Meta (Muse Spark), AgentMail and Slack (Bot).

## Problem
Most people know the rules of credit (pay on time, keep utilization low) but don't follow them, because:
- The data is scattered across card alerts, receipts and subscription emails.
- Utilization is reported on the **statement date**, not the due date, so even people who pay in full can get dinged.
- Forgotten subscriptions quietly push balances up.
- Budgeting apps want bank logins (Plaid). Many people won't give those out.

## Solution
Upscore is an agent you talk to, not an app you maintain.
1. **Forward your mail.** Set a forwarding rule from your inbox to the agent's AgentMail address for card alerts, receipts and statements. The agent extracts transactions, payments, balances, due dates and subscriptions.
2. **Text it like a friend** (Slack, Telegram, email reply or web chat): "paid $300 on my Amex", "got paid $2,400", "my Chase limit is $5k, due on the 15th".
3. **It builds you a pathway.** It gives concrete, dated steps toward a better score: what to pay, on which card, and before which date.
4. **It nags you.** It sends reminders before statement close and due dates, and alerts when utilization crosses 30% or 10%.

No bank credentials. No screen scraping. The connector keeps the API keys out of the Sprite entirely.

## Users
- People building or repairing credit: students, new immigrants, anyone recovering from a bad stretch.
- People with 1–4 credit cards who pay roughly on time but don't optimize.

## Core features (MVP, hackathon week)
| # | Feature | Notes |
|---|---------|-------|
| F1 | Email ingestion via AgentMail | Polls the inbox, sends each new message to the agent, which extracts structured data with tools and replies when useful |
| F2 | Natural-language logging | Through Slack (Bot connector), Telegram, email or web chat. The agent calls tools such as `add_transaction`, `upsert_account` and `record_payment` |
| F3 | Accounts and utilization | Per-card and overall utilization, compared against limits |
| F4 | Subscriptions | Flagged recurring charges, with a monthly total |
| F5 | Credit pathway | A fixed engine (no LLM) produces prioritized actions; the LLM explains them |
| F6 | Reminders | A daily check for due dates within 3 days, statement close within 3 days, and utilization over 30% |
| F7 | Dashboard | A single web page showing utilization, upcoming dates, subscriptions, the plan, and a chat box |
| F8 | Statement vision | Images and PDFs (via Telegram or dashboard upload) go to Muse Spark, which records the card and its transactions |
| F9 | What-if simulator | Utilization before and after a payment, plus an estimated score range from FICO-style utilization bands |
| F10 | Weekly check-in and streaks | A Sunday digest, on-time payment streaks per card, and a missed-payment alert 1–3 days after a due date with no payment logged |
| F11 | Subscription cancel helper | An AgentMail draft is created, then sent only after the user explicitly confirms; the subscription is then marked cancelled |
| F12 | Uncle Gary mode | A persona toggle: the Thanksgiving uncle roasts your spending, with the same numbers and plan (aimed at the Weird Uncle Award) |
| F13 | Score history | Each reported score is logged and charted against the goal |
| F14 | Gamification | Pip the mascot grows through 5 stages by level and its mood tracks utilization. Includes XP and levels, a daily check-in streak, an on-time streak, 19 achievements with thresholds, a milestone path to the goal score, and unlock toasts in the dashboard and chat. Everything is recomputed from data in `gamify.py`. |

## Credit engine rules (deterministic)
These are based on FICO factor weights: payment history 35%, amounts owed 30%, length of history 15%, new credit 10%, credit mix 10%.
- **Payment history:** every card due within 7 days → "Pay at least the minimum on X by DATE". Strongly suggest autopay for the minimum.
- **Utilization:** overall and per card. Above 30% is bad, 10–30% is okay, 1–9% is best. For each card above 10%, compute the payment needed **before the statement date** to reach under 10%.
- **Don't close old cards.** If the user mentions closing a card, warn them.
- **Subscriptions:** list recurring charges, flag duplicates and ones over $15/month as worth reviewing, and show the monthly total.
- **Cash flow:** if income is known, check that minimum payments plus subscriptions fit inside it.
- The plan is ordered by impact: missed or upcoming due dates first, then high utilization, then subscriptions.

## Architecture
```
            ┌──────────────── Sprite (always-on service) ─────────────────┐
 Telegram ─▶│ telegram poller ─┐                                          │
 (bot API)  │                  ├─▶ agent loop (LLM + tools) ─▶ SQLite     │
 AgentMail ─▶│ email poller ────┤        │                                 │
 connector  │                  │        └─▶ credit engine (pure Python)   │
 Web chat  ─▶│ http server ─────┘                                          │
            │ scheduler (daily reminders) ─▶ Telegram / email               │
            └───────────────────────────────────────────────────────────────┘
 LLM calls ─▶ Sprites gateway ─▶ Meta connector: Muse Spark (chat/plan), Muse Ember (email extraction)
 Slack     ─▶ Sprites gateway ─▶ Slack (Bot) connector (polls one channel/DM; no public webhook needed)
```
- Python 3, standard library only. SQLite runs on the Sprite's persistent disk.
- The LLM speaks any OpenAI-compatible `/chat/completions` endpoint with tool calling.
- Config comes from environment variables. Inside a Sprite, the base URLs point at the gateway and no keys are needed.

## Out of scope (for now)
- **iMessage:** requires macOS. It could be added later through a Mac relay (for example BlueBubbles).
- **WhatsApp:** requires Meta Business API approval. It could come later through a Twilio custom connector.
- Plaid or bank aggregation, real credit-bureau pulls, and multi-user auth. The MVP is single-user, a *personal* agent.

## Demo script (≈90s)
1. Forward a "Your Chase card was charged $84.12 at Whole Foods" alert. The dashboard updates.
2. On Telegram: "my chase limit is 2000, statement closes on the 3rd, due on the 28th, balance is 1450". The agent shows 72% utilization: bad.
3. "what should I do?" The agent replies with a dated plan: pay $1,250 before the 3rd to report under 10%.
4. Forward a Netflix receipt and a second streaming receipt. The agent flags the duplicate streaming subscriptions.
5. Show the reminder message the agent sent.

## Success metrics
- Captures 90% or more of forwarded alerts correctly (spot-checked).
- The plan always includes concrete amounts and dates.
- Setup takes under 5 minutes: one forwarding rule and one Telegram chat.

## Risks
- **LLM hallucinating numbers.** Mitigation: every number comes from the database or the engine, and the LLM only reads and writes through tools.
- **Email parsing variety.** Mitigation: the LLM does the extraction, and raw text is stored so it can be re-processed.
- **Financial advice framing.** Upscore gives educational guidance based on public credit-scoring factors. It is not a licensed advisor.
