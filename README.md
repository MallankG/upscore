# 📈 Upscore

**A personal agent that coaches your credit score up.** You forward it your card alerts and receipts, and text it your spending in plain English. It tracks utilization, due dates and subscriptions, then gives you a dated plan with reminders so you never pay late or report a high balance.

Built for [Fly.io Hacksgiving 2026](https://fly.io/hacksgiving/). It runs on a **Sprite** and uses three Sprites connectors: **Meta (Muse Spark)** for the brain, **AgentMail** for email in and out, and **Slack (Bot)** for chat. No API keys live on the box. See [PRD.md](PRD.md) and [HACKATHON.md](HACKATHON.md).

```
forward bank alerts ─▶ AgentMail ─┐
Slack / Telegram chat ────────────┼─▶ agent (Muse Spark + tools) ─▶ SQLite ─▶ credit engine ─▶ dated plan + reminders
web dashboard / CLI ──────────────┘   (Muse Ember handles email extraction)
```

- **Every number is computed, not generated.** The LLM only reads and writes through tools ([agent.py](upscore/agent.py)). Utilization, payoff amounts and dates come from [credit.py](upscore/credit.py).
- **Statement-date aware.** It tells you what to pay *before the statement closes* so the card reports under 10%.
- **Python standard library only.** There's nothing to `pip install`.

### Gamified, so the habits stick
- **Pip, your credit buddy.** Pip grows from **seed → sprout → sapling → bloom → mighty oak** as you level up. Its face follows your utilization: happy, okay or worried (wilting leaves and a sweat drop).
- **XP and levels.** You earn XP for logging transactions, on-time payments, score check-ins, daily check-ins and achievements.
- **Streaks.** 🔥 tracks days you've checked in, and 🛡 tracks months paid on time on every card.
- **19 achievements** with progress bars, in tiers: Card on file → Bookkeeper → Ledger legend, On time → Clean quarter → Half-year hero → Perfect year, Under 30 → Single digits, Up ten/twenty-five/fifty, Goal crusher, Snip snip, Survived Uncle Gary.
- **A milestone path** to your goal score, where the next milestone is highlighted.
- **Unlock celebrations.** New achievements show up as a toast with confetti on the dashboard, and as a "🏆 Achievement unlocked" line in Slack, Telegram or email.

### Which card where (rewards optimizer)
Pip knows each card's reward rates, including **rotating quarterly categories** such as 5% on gas this quarter. It looks them up with web search and stores them with start and end dates.
- Ask "which card for groceries?" and it ranks your cards.
- Log a purchase on the wrong card and it tells you which card would have earned more, and how much.
- It never recommends a card that the purchase would push over 30% utilization. Score beats points.
- The dashboard's **Which card where** panel shows the best card per category, and using the best card 10 times unlocks **Reward hunter**.

### Connectors (auto-discovered on the Sprite)
| Connector | What it does in Upscore |
|---|---|
| Meta | Muse Spark and Ember (with `LLM_PROVIDER=meta`) |
| AgentMail | The inbox for forwarded alerts and receipts, plus replies and cancellation emails |
| Slack (Bot) | Chat with Pip in a channel or DM |
| Deepgram | Voice in (dashboard mic, Telegram voice notes) and Pip's spoken voice |
| Era | Real bank accounts and transactions, synced every 30 minutes |
| Pushover | Phone push for due dates, missed payments and achievements |
| Tavily / Exa | Web search for current rotating categories and cancellation steps |
| Pinecone | Semantic search over your history ("how much on coffee since September?") |
| TypeSafeAI | Jev assigns each purchase a spending category, which drives the rewards logic |
| Box | Statement photos and PDFs are archived to a Box folder |

Any connector that isn't connected is skipped quietly. The dashboard's **Connected** strip shows which ones are live.

### Models: one flag
`LLM_PROVIDER=ollama` (the default on our Sprite) is **free**: Ollama runs on the Sprite itself with `qwen3.5:4b`, a small CPU model that supports tool calling and vision. `LLM_PROVIDER=meta` switches to Muse Spark and Muse Ember through the Meta connector, which is paid. Any `LLM_*` variable overrides the preset.

### Voice
Tap the mic on the dashboard or send a Telegram voice note. The audio is transcribed by the **Deepgram connector**, and Pip shows what it heard ("🎙 …") before acting on it. The speaker button reads Pip's replies aloud.

### Features
- **Statements and receipts as images or PDFs.** Send a photo on Telegram or upload one on the dashboard. Muse Spark reads it and records the card and its transactions.
- **What-if simulator.** "What if I pay $800 on Chase?" returns utilization before and after plus an estimated score range.
- **Weekly check-in and streaks.** Every Sunday you get spend vs. last week, upcoming due dates, your on-time streak and the one move to make. If a due date passes with no payment logged, it alerts you while there's still time.
- **Subscription cancel helper.** It drafts the cancellation email in AgentMail and sends it only after you say so, then drops that subscription from your totals.
- **🦃 Uncle Gary mode.** Your nosy Thanksgiving uncle roasts your spending, then gives you the real plan anyway.
- **Score history chart.** Tell it your score whenever you check, and it charts progress toward your goal.

## Run locally (with demo data)
```bash
UPSCORE_DB=/tmp/demo.db python3 -m upscore.seed
UPSCORE_DB=/tmp/demo.db LLM_API_KEY=$MODEL_API_KEY python3 -m upscore.server   # defaults to https://api.meta.ai/v1
open http://localhost:8080
```
To run the tests: `python3 -m unittest discover tests`. These cover the credit engine plus every channel, run end to end against mock Meta, AgentMail, Slack and Telegram servers.
To try the real model from a terminal: `python3 -m upscore.chat "my chase limit is 2000, balance 1450, due the 28th"`

## Deploy on a Sprite
1. Install the CLI and create a Sprite:
   ```bash
   curl -fsSL https://sprites.dev/install.sh | sh
   sprite org auth
   sprite create upscore
   ```
2. In the Fly dashboard, go to **Sprites → Connectors** and add **Meta (Muse Spark)** (with a key from dev.meta.ai), **AgentMail** (with a key from console.agentmail.to) and **Slack (Bot)** (via OAuth). Under Access Configuration, grant all three to the `upscore` sprite. Copy each gateway URL from the connector's detail page.
3. Create an AgentMail inbox (in the console, or through the gateway: `POST /v0/inboxes`). Then add a forwarding rule in your email for bank alerts and receipts that sends them to that inbox.
4. Invite the Slack bot to a channel (for example `#money`) or DM it, then put that channel's ID in `SLACK_CHANNEL`. Telegram is optional: create a bot with @BotFather and set `TELEGRAM_BOT_TOKEN`.
5. Push the code and config, then start it as a service:
   ```bash
   sprite exec -- git clone <your-repo-url> upscore
   sprite exec -- sh -c 'cat > upscore/.env' < .env      # filled-in copy of .env.example
   sprite exec -- sprite-env services create upscore --cmd python3 --args "-m,upscore.server" --dir /home/sprite/upscore --http-port 8080
   sprite exec -- sh -c 'cd upscore && python3 -m upscore.chat "hi, what can you do?"'   # real-model smoke test
   sprite info                                           # get the URL, then open it with ?token=<WEB_TOKEN>
   ```
   The service restarts on wake. While running on a Sprite, the agent holds a Tasks API heartbeat so the email and Telegram pollers stay awake. Set `KEEP_AWAKE=0` to let the Sprite sleep instead.

## Talk to it
Say these in Slack, Telegram, the dashboard, or an email to the agent's inbox:
- "my chase freedom limit is 2000, statement closes the 3rd, due the 28th, balance 1450"
- "spent 42 on dinner with amex"  ·  "got paid 2400"  ·  "paid 300 on chase"
- "my score is 642, I want 720 by June"
- "what should I do this week?"  ·  "what if I pay 800 on chase before the statement?"
- "cancel netflix for me"  ·  "turn on uncle gary mode"  ·  (send a photo of your statement)

## Not yet supported
- iMessage needs a macOS relay, and WhatsApp needs Meta Business API approval. Both could plug into `agent.respond()` the same way Telegram does.
- Files sent in Slack: Slack serves them from `files.slack.com`, which the connector doesn't proxy, so they only work when `SLACK_BOT_TOKEN` is set. Email attachments: the AgentMail connector doesn't allow the attachments endpoint. Use Telegram or the dashboard for statements.

*Upscore gives educational guidance based on public credit-scoring factors. It is not financial advice.*
