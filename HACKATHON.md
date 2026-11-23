# Hacksgiving 2026 (Fly.io): notes

Source: https://fly.io/hacksgiving/ (read 2026-11-23)

## What it is
- A hackathon from Fly.io, **Mon Nov 23 to Sun Nov 29, 2026**. The goal is to build a **personal agent**.
- Hackathon compute is free on Fly's Free plan. Sign up at https://fly.io/app/sign-up?s=hacksgiving
- Every Sprite comes with Claude Code, Codex, Gemini CLI, Antigravity, Cursor, Pi and Grok already installed. You bring your own model subscription.

## Hard requirements (both must be met)
1. **The agent runs on a Sprite.** A Sprite is a hardware-isolated Linux computer that persists as long as you want it to.
2. **The agent uses at least one Sprites Connector.** See https://fly.io/sprites/ecosystem/#connectors

## Rules
- The project must be **built during Hacksgiving week**.
- It must be an agent that runs on Fly.io Sprites and uses at least one connector.
- You need a Fly.io account.
- Any AI tools are allowed.
- **One entry per developer.**
- Projects must follow the Fly.io terms of service.

## How to submit (deadline: before midnight PST, Sun Nov 29)
1. **Build** the agent on Sprites during the week, using at least one connector.
2. **Share** a short demo post on X, Bluesky, LinkedIn or Reddit and **tag @flydotio**. A video demo earns "bonus points" but doesn't affect the score.
3. **Submit** through the form, with a link to the code, a link to the demo, and 1–2 sentences about what the agent does:
   https://docs.google.com/forms/d/e/1FAIpQLSfZ_xearoDOSRXI3VjAbM4BJlObUdxSFO0SDwyZ8G6M2lapRQ/viewform

Winners are announced by email on **Tuesday, Dec 1, 2026**.

## Prizes
- Grand Prize: **$2,500**
- Second Place: **$1,500**
- Weird Uncle Award (strangest agent): **$1,000**

## Judges
Daniel Gafni (TypeSafe AI), Carter Rabasa (Box), Scott Johnston (Fly.io), Asaf Ashirov (Pinecone), Michael Chiang (Ollama).
Partner connectors (worth considering): TypeSafeAI, Box, Pinecone, Ollama Cloud.

## Connectors: how they work
- Credentials live in Fly's control plane and never enter the Sprite. Connectors are deny-by-default, so you grant Sprites access by label, by name prefix, or to all Sprites.
- Gateway URL: `https://api.sprites.dev/v1/gateway/<provider>/<connection_id>/<path>`. Calls need no auth header, and **they only work from inside a Sprite**.
- To see what's available: `curl -s https://api.sprites.dev/v1/gateway/list`
- Set them up in the dashboard under Sprites → Connectors → Add connector. The Gateway Playground there can fire test requests.
- Docs: https://docs.fly.io/sprites/concepts/connectors
- Useful connectors include AgentMail, Resend, OpenRouter, Anthropic, OpenAI, Ollama Cloud, Pinecone, Box, Slack (Bot), Pushover, Stripe, Custom API, and more.
- **AgentMail** gives the agent an inbox. The connector covers inboxes, messages, threads, send and reply. Incoming mail is **polling only** (no webhooks or websockets).

## Our plan
Upscore is a credit-score coach agent. It runs on a Sprite and uses three connectors: **Meta (Muse Spark)** for the LLM (Muse Ember handles email extraction), **AgentMail** for email ingestion and replies, and **Slack (Bot)** for chat. See PRD.md.

## Checklist
- [ ] Fly.io account + Sprite created
- [ ] AgentMail connector set up and access granted to the sprite
- [ ] Meta (Muse Spark) connector set up (key from dev.meta.ai)
- [ ] Slack (Bot) connector set up, bot invited to a channel
- [ ] Telegram bot token (optional)
- [ ] Deploy and run as a Sprite service
- [ ] Record the demo video and post it tagging @flydotio
- [ ] Submit the form before **Nov 29, 11:59pm PST**
