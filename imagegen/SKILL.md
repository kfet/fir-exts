---
name: imagegen
description: Generate or edit images from a chat conversation. Use when the user asks for a picture, drawing, illustration, render, logo, or an edit of an image they sent — anything whose deliverable is a raster image rather than text or code.
---

# imagegen

Your driving model is text-only. Image generation is a **tool call**, not
something the model does itself. `generate_image(prompt)` writes a PNG to
`~/imagegen-out/` and returns the path; you then deliver it to the user.

## Delivering the result

The tool only produces a file. It does not reach the user. Attach it with the
relay's directive, on its own line, with `inline` so it renders in the chat
rather than as a download chip:

```
<!--poe-attach path="/home/kfet/imagegen-out/img-20260905-013155.png" name="Fir sapling" inline-->
```

Never paste base64 or raw bytes into the reply.

## Provider policy — the important part

Two providers: **OpenRouter** (default) and **Poe**.

They cost the same. OpenRouter is the default for **budget isolation**: on a
Poe-hosted relay, image spend and the bot's own survival draw on one points
pool. Draining OpenRouter costs you images. Draining Poe takes the user's
control channel offline until the month rolls over.

- **Never select Poe implicitly.** It is reached only when the user names it
  ("use poe") or when `imagegen.json` pins it.
- A Poe call preflights the points balance and refuses below the floor. Do not
  work around that refusal — report the balance and offer OpenRouter.

## Model selection

Do **not** pass `model` unless the user names one. Default is auto: the
provider's live `/models` catalog (24h cache), filtered to models that actually
emit images, ranked with per-image price as a capability proxy.

- `quality: "best"` (default) — the flagship
- `quality: "fast"` — cheapest, for drafts and iterating

New flagships are adopted automatically. If a pinned model disappears the tool
errors with the live list — surface it and let the user choose; never silently
substitute, because a different image model is a different product.

`image_models` shows what is currently available and what it costs.

## Editing an existing image

Pass `image` with a path to an input file (e.g. an attachment the user sent,
staged into your working directory) alongside the prompt. The Gemini models are
markedly better at this than the OpenAI ones.

## Cost awareness

Every result carries provider, model and per-image price. After several images
in one session the result gains a running count and cost warning — that is a
**nudge, not a limit**. Surface it rather than silently stopping; the user
decides whether to continue.

Images are the most expensive thing per call this agent does — roughly 3000 Poe
points (~$0.09) for a flagship render. Do not generate speculative extra
variants nobody asked for.

## Switching provider

- One-off: pass `provider` on the call.
- Sticky: `/imagegen provider openrouter|poe` — writes
  `~/.config/fir/imagegen.json`, inherited by every relay on the host.
- `/imagegen` bare shows provider, model, credentials and Poe balance.

Prefer the slash command for a durable change; it works from mobile, where the
user has no shell. There is deliberately no env-var configuration.

## Credentials

Resolved from `OPENROUTER_API_KEY` / `POE_API_KEY`, else the `openrouter` /
`poe` slots in `~/.config/fir/auth.json`. Never print, echo, log or grep a key.
If none is present the tool stays registered and returns an actionable error —
relay that error verbatim rather than guessing at the cause.
