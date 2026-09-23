---
name: refresh-models
description: Refresh the cockpit's model picker. Discovers which providers have API keys in server/.env, asks each provider which models the account can actually reach, looks up current pricing and capabilities on the web, then writes a ranked shortlist of ~5 models per provider with cost, vision support and a one-line ability note. Use when the model dropdown looks stale, after adding or removing a provider key, when a provider ships new models, or when the user asks to update/refresh the model list.
---

# Refresh the model picker

The cockpit's dropdown is built from two sources, and this skill owns one of them.

**The runtime** (`server/catalog.py`, on every boot) asks each provider which
models the configured key can reach. That is live and needs no maintenance.

**This skill** supplies everything the provider APIs do not publish:

| | Runtime | This skill |
|---|---|---|
| Which models the key can reach | live API | — |
| Context window, capability tree | Anthropic only | — |
| **Price** | no API exposes it | web lookup |
| **Vision support** | Anthropic only | web lookup for the rest |
| **Ranking — the "best 5"** | needs judgement | here |
| **Ability blurb** | not published anywhere | here |

Output is `server/model_shortlist.json`. The runtime reads it if present and
falls back to a built-in list if not, so the cockpit always starts.

## Rules

**Never invent a price or a capability.** Every number must come from a page you
fetched this run. A model whose price you could not find gets `"price": null` —
the picker renders that as unknown, which is honest. A guessed price shown as
fact is worse than a gap, and a `$0` for a paid model is the worst outcome of
all: it reads as free.

**Vision is tri-state.** `true` and `false` are claims; `null` means no source
said. Do not infer vision from a model being "modern".

**Rank by fit for this cockpit, not by raw capability.** The controller is a
router that makes many small tool calls; the diagram agent does structured
visual generation. A cheap fast model with reliable tool calling is worth more
here than an expensive one that reasons longer.

**Prefer the cheaper of two close models, and say so.** Newer is often cheaper
— at the last run Opus 5.5 undercut Opus 5, and Sonnet 5 undercut Sonnet 4.6 —
so do not assume price tracks recency.

## Steps

1. **Find the configured providers.**
   ```
   cd server && uv run python -c "
   from catalog import configured_providers
   from dotenv import dotenv_values
   print(configured_providers(dotenv_values('.env')))"
   ```
   Never print key values — only which names are set.

2. **Ask each provider what the account can reach.**
   ```
   cd server && uv run python -c "
   from dotenv import dotenv_values
   from catalog import fetch_anthropic, fetch_openai
   v = dotenv_values('.env')
   for m in fetch_anthropic(v.get('ANTHROPIC_API_KEY','')): print(m.provider, m.id, m.context)
   for m in fetch_openai(v.get('OPENAI_API_KEY','')): print(m.provider, m.id)"
   ```
   If a provider in `PROVIDER_KEYS` has no fetcher yet, add one to
   `catalog.py` rather than hand-maintaining its list here.

3. **Look up price, context and vision on the web** for the candidates. Known
   sources — re-check rather than trusting these paths blindly, they move:
   - Anthropic: `https://platform.claude.com/docs/en/about-claude/pricing`
   - OpenAI: `https://developers.openai.com/api/docs/pricing` and `.../models`

4. **Choose about five per provider**, spanning a range rather than ranking:
   a flagship, a strong general-purpose model, a value workhorse, and something
   cheap for high volume. Drop anything beaten on both capability and price.

5. **Write `server/model_shortlist.json`** in the shape below.

6. **Verify, then report.** Run
   `cd server && uv run python -c "import bot; from catalog import grouped; print(grouped(bot.CATALOG))"`
   and show the user the resulting picker, flagging every `price: null` or
   `vision: null` so the gaps are visible rather than buried.

## Output shape

```json
{
  "generated": "2026-09-23",
  "sources": [
    "https://platform.claude.com/docs/en/about-claude/pricing",
    "https://developers.openai.com/api/docs/pricing"
  ],
  "providers": {
    "anthropic": [
      {
        "id": "claude-opus-5-5",
        "price": [4.0, 20.0],
        "vision": true,
        "blurb": "strongest general-purpose; effort up to max; undercuts Opus 5"
      }
    ],
    "openai": [
      {
        "id": "gpt-6-sol",
        "price": [2.0, 10.0],
        "vision": true,
        "blurb": "1.05M context; same price as Sonnet 5"
      }
    ]
  }
}
```

Order within each provider is the picker's order, best first. `price` is
`[input, output]` USD per million tokens. Keep `blurb` under about 70
characters — it shares a dropdown line with the id and the price.
