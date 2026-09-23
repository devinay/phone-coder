# Voice Coding Cockpit

Talk to a terminal, a document, and a diagram — at the same time, out loud.

A browser page with three things side by side: a conversation, a live terminal,
and a document/diagram pane. You speak; a controller works out what you meant
and hands the job to whichever agent owns it.

---

## Run it

```sh
cd server
uv run python bot.py
```

Then open **http://localhost:7860/cockpit**.

The terminal pane is `ttyd` proxied through the same server on `7681`; it starts
on its own. A fresh tmux session called `cockpit` is created at the repo root on
every boot — deliberately, so "an empty terminal is attached" is true even after
a crash.

### What you need in `server/.env`

| Variable | Needed for | Without it |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI models | **the server will not start** — the service is constructed unconditionally |
| `ANTHROPIC_API_KEY` | Claude models | Claude entries in the picker fail when selected |
| `DEEPGRAM_API_KEY` | speech recognition | no microphone; typing still works |
| `CARTESIA_API_KEY` | Cartesia voice | only if `TTS_PROVIDER=cartesia` |

Optional: `LLM_MODEL` (default model), `VISION_MODEL` (model that reads
sketches), `AGENT_MODEL_<AGENT>` (per-agent override), `TTS_PROVIDER` (`kokoro` runs locally and needs no key, and is what the
opening turn is spoken by — the configured provider is selected at startup, not
after the first switch),
`TTS_ENABLED`, `MEMORY_ENABLED`, `VOICE_COCKPIT_GIT_ROOT` (where documents are
stored), `MODEL_SHORTLIST=off` (show every fetched model).

---

## How it decides what to do

You never pick an agent. The **controller** is the entrypoint for every turn: it
reads what you said, routes to a worker, and control returns to it afterwards.

| Agent | Owns | You'd say |
|---|---|---|
| **controller** | routing; reading the terminal | — |
| **shell** | running commands, answering prompts, watching output | "run the tests", "what's it doing?" |
| **doc** | markdown documents | "start a doc", "write that down" |
| **diagram** | Mermaid diagrams, focus mode | "draw a flowchart of this" |
| **image** | finding and placing images | "find a logo for that" |
| **web** | search and fetching pages | "look that up" |

Only `shell` can type into the terminal. The controller can *read* it — so
"what's running?" is answered directly, while "run this" is routed.

---

## Doc mode

Say "start a document" or "open the notes". The right pane becomes an editor and
the doc agent takes over; everything you say is treated as content or an
instruction about content until you leave.

Documents live in a git repository (`VOICE_COCKPIT_GIT_ROOT`) and are written
atomically. **That repository needs a git identity**, or every save fails —
git has nothing to attribute the commit to:

```sh
git -C ~/voice-notes config user.name "Your Name"
git -C ~/voice-notes config user.email "you@example.com"
```

A global identity works too. The cockpit tells you this in plain words if it
happens, rather than reporting an unexpected error. Your speech and the assistant's replies are both recorded, so the
document can reflect a conversation rather than dictation.

Say "exit doc mode" to leave.

---

## Diagrams

Ask for one — "draw a sequence diagram of the login flow". The diagram agent
writes **Mermaid** into the document and the pane renders it.

**Focus mode** puts a single diagram full-screen and updates it live as you
talk, which is the right mode for iterating on one picture. "Focus on that
diagram" enters; "exit focus" leaves. `revert_diagram_edit` undoes the last
change if a revision goes wrong.

Two engines. **Mermaid** is what the diagram agent writes when you describe a
diagram in words. **Excalidraw** is the canvas you draw on — see *Sketching*
below. Mermaid-to-Excalidraw promotion (`server/diagramming_plan.md`) is still
designed rather than built, so the two do not yet convert between each other.

---

## The terminal

The terminal is a real fish shell in tmux that you can also type into yourself.
Nothing is remembered about it: what is running is read from the operating
system every turn, so a program you launched by hand is seen exactly like one
the agent started.

Ask "what's running?" and it will tell you, including how long and what
directory. It reads the screen, follows your `cd`, and can report only what has
changed since it last looked.

### Watching, and answering prompts

Say "watch claude and accept the defaults, but pick the always-allow option if
it's offered". That instruction is stored **in your own words** and shown to the
model at the start of every turn — nothing parses it into a policy, because a
conditional like that cannot survive being turned into an enum.

If something starts waiting while you have gone quiet, the model is woken,
switched to an agent that can actually press a key, and the alert also appears
on screen so it is not missed with the voice off.

Several watches can run at once, on different panes. `list_terminal_panes` shows
what is available.

**Multi-select dialogs are handled specially.** Claude Code's checkbox prompts
look like ordinary menus but the numbers only *toggle* — submitting needs
`Right` then `1`. The agent is told this when one is on screen, because pressing
the number and seeing nothing happen is otherwise a dead end.

---

## Switching models

The dropdown at the top switches the model mid-session. Each entry shows its
price per million tokens; hover for context size, vision support and a one-line
note.

The list is built on every boot by asking each provider whose key you have set
which models your account can actually reach — then narrowed to about five per
provider. `MODEL_SHORTLIST=off` shows everything.

Switching across vendors **resets the conversation context**, because vendors
encode tool calls differently. Switching within a vendor keeps it.

Messages that no provider can turn into a request are dropped before the model
sees them, and the drop is logged. This exists because a provider's own
reasoning artifacts can come back in a shape its adapter then refuses, killing
the next turn with an error that names neither the message nor its origin.

To pin one agent without changing the rest:

```sh
AGENT_MODEL_DIAGRAM=claude-opus-5-5   # in server/.env
```

### Asking which model is running

Just ask — "which model are you using?" The answer comes from an `[ACTIVE
MODELS]` block injected into the system prompt every turn, naming the
conversation model with its provider and price.

This has to be injected rather than recalled. A model cannot introspect which
weights are serving it, and one released after its own training data has never
read anything about itself — asked without the block it produces a confident
wrong name rather than admitting ignorance. Which model is configured is a
*setting*, like the working directory, so the fix is simply to put it in front
of the model.

The block is generated from live state each turn and never cached: one that
drifted from what is actually running would be worse than none, because it
would be believed.

### Keeping the list current

Prices and vision support are not published by any API — the model endpoints
return neither. They come from `server/model_shortlist.json`, written by the
**`refresh-models`** skill, which looks up current pricing, decides the
shortlist, and records where each number came from.

Run it when the list looks stale or after adding a provider key. If the file is
missing the cockpit still starts, on a built-in list.

A price the skill could not find is shown as unknown, never as `$0`.

---

## Sketching — draw and talk

Draw roughly on the canvas, say what it *means*, and both become a diagram.

The two inputs carry different things. **The drawing** gives shape: how many
boxes, where they sit, what connects to what. **Your words** give meaning: which
box is the database. A model looking at three rectangles sees three rectangles —
it cannot know one is an API. Neither input works alone.

### Example

> **You:** *(sketch three boxes with arrows between them)*
> "Turn that into a diagram — it's an auth flow. The user hits the API, and the
> API reads the user database."

> **Cockpit:** "Drew 7 elements from your sketch using claude-opus-5-5."

Say what it *is*, not what it looks like. "Three boxes with arrows" tells the
model nothing it cannot already see.

If the canvas is empty it says so rather than inventing a diagram from your
words — that is what asking for a diagram directly is for.

The canvas appears in the terminal's pane, replacing it while you draw, and the
terminal comes back when you leave. Nothing running in the terminal is affected;
it is only hidden.

### Choosing the model that reads sketches

Sketching uses its own model, separate from the one you are talking to:

> **You:** "Which models can read sketches?"
> **Cockpit:** lists them with prices and whether vision is confirmed.
>
> **You:** "Use gpt-6-sol for sketches."
> **Cockpit:** "Sketches will now be read by gpt-6-sol (set by voice this session)."

**Changing it does not reset your conversation.** The sketch call is one-shot —
an image, your words, ops back — so it does not go through the conversation
model at all. That is deliberate: switching the *conversation* model across
vendors resets context, which would make comparing sketch models cost a
conversation each time. This way you can A/B them inside a single session, on
the same drawing.

Resolution order: what you said by voice → `VISION_MODEL` in `.env` → whatever
the conversation is using.

Models known not to read images are never offered — `qwen2.5-coder:7b` is a
coder model, and offering it would be offering a guaranteed failure. Models
where nobody has published the capability are offered, marked "vision unknown";
they may work.

### Under the hood

Three layers, so the canvas is replaceable:

1. **Vision** (`vision.py`) — the only part that talks to a model.
2. **Ops** (`sketch.py`) — tool-agnostic commands: `create_node`, `connect`,
   `move`, `set_style`. These know nothing about Excalidraw.
3. **Translation** (`sketch.py`) — ops to Excalidraw elements.

Supporting a different drawing tool means replacing layer 3 alone. A model
emitting Excalidraw JSON directly would weld the product to one canvas.

To compare providers outside the cockpit, the original spike still works:

```sh
cd server
uv run python spikes/vision_spike.py --image sc.png \
  --intent "this is an auth flow: user hits the API, the API reads the user database" \
  --provider anthropic --model claude-opus-5-5
```

## What survives a restart

Decisions persist; experiments do not.

| | Survives? |
|---|---|
| Documents and diagrams | **Yes** — git-backed |
| Cross-session memory | **Yes** — `.cockpit/memory.md` |
| Model shortlist and prices | **Yes** — `model_shortlist.json` |
| Anything in `.env` | **Yes** |
| Conversation context | No — new session |
| Runtime model changes from the dropdown | No — back to `LLM_MODEL` |
| Standing watch instructions | No — in memory by design |
| Vision model set by voice | No — back to `VISION_MODEL` |
| Terminal contents and scrollback | No — the tmux session is reset on boot |

So once you have decided something, write it to `.env` and it sticks. Until
then each restart returns to a known baseline, which is what makes "which model
produced this?" answerable after the fact.

## Memory

At the end of a session the conversation is summarised and appended to
`.cockpit/memory.md` in your document repository, then added to every agent's
prompt next time. Capped at `MEMORY_MAX_ENTRIES` (20). `MEMORY_ENABLED=false`
turns it off.

---

## Testing

```sh
cd server
uv run pytest tests/ -q                            # unit tests
uv run python tests/integration_terminal_panes.py  # real tmux, real panes
```

The integration script **kills and recreates the `cockpit` tmux session**, so it
refuses to run while a server is up. Stop the cockpit first.

---

## Layout

```
server/
  bot.py                  pipeline, services, routes
  agent_router.py         tmux: panes, screens, what is running
  catalog.py              model catalogue, pricing, shortlist
  terminal_*.py           screen parsing, history, watches, prompts
  sketch.py               tool-agnostic ops, and the Excalidraw translator
  vision.py               layer 1 - asking a model to read a drawing
  canvas.py               the seam to the drawing surface in the browser
  editor.html             the Excalidraw canvas, embedded in an iframe
  agents/<name>/prompt.md agent definition — tools and policy in frontmatter
  tools/                  tool implementations per domain
  cockpit.html            the UI
  atomic_write.py         atomic file writes and project locking
  spikes/                 provider bake-off for sketch reading
```

An agent is a prompt file plus a tool list. To change what one can do, edit its
frontmatter — the registry validates it against the tools actually registered.
