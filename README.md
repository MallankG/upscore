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
- "what should I do this week?"

## Not yet supported
iMessage needs a macOS relay, and WhatsApp needs Meta Business API approval. Both could plug into `agent.respond()` the same way Telegram does.

*Upscore gives educational guidance based on public credit-scoring factors. It is not financial advice.*
