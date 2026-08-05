# Voice Coding Cockpit — Project Plan

## What This Is

Voice Coding Cockpit is a local-first, voice-driven workspace. A user talks to a
controller that can operate a terminal, write documents, create diagrams, use evidence
from files/web/vision, and eventually process inbox voice notes from channels such as
Telegram or WhatsApp.

The immediate goal is to refactor the current single large controller into an agent
runtime before adding Excalidraw. Excalidraw is the validation feature: if the refactor
is good, adding a new visual capability should not bloat the controller prompt or tangle
doc/diagram/image logic again.

Run: `cd server && uv run bot.py` -> open `http://localhost:7860/cockpit`

Detailed diagramming and Excalidraw design lives in `server/diagramming_plan.md`.

---

## Current State

```text
Browser (cockpit.html)
  - chat UI over Pipecat SmallWebRTC
  - live terminal iframe via ttyd proxy
  - doc overlay for markdown + rendered Mermaid
  - diagram focus overlay for one diagram at a time

Server (server/bot.py)
  - STT -> user transcript
  - one shared LLM context and prompt
  - switchable LLM/TTS providers
  - tool modules for shell, docs, diagrams, images, web
  - doc and diagram-focus state machines
```

Doc mode currently assumes one user plus the controller. Speaker identification gating
was removed; transcripts are chronological user/controller records.

The major problem now is context overload: shell commands, document writing, diagrams,
images, web search, future inbox workflows, and admin controls cannot keep sharing one
large prompt and one global tool list.

---

## Storage Model

Documents are stored in one existing git repository configured by
`VOICE_COCKPIT_GIT_ROOT`. The app does not auto-initialize this repo.

```text
VOICE_COCKPIT_GIT_ROOT/
  .git/
  <project-slug>/
    <project-slug>.md
    transcript.md
    speakers.json
    metadata.json
    .session/              # uncommitted drafts/snapshots
    images/
    diagrams/
    artifacts/
```

Every successful save writes local files, commits the changed project files, and tries
to push. Push failures are warnings only: local commits remain valid.

Git history is the document version history. Diagram-specific `v<k>` artifacts are only
for accepted diagram states after a diagram is promoted to Excalidraw; see
`server/diagramming_plan.md`.

---

## Target Architecture

### Controller And Agents

The controller remains in the path for every user turn. It decides intent, chooses a
worker agent, and composes agents into workflows. Domain work lives behind registered
agents with smaller prompts and narrower tools.

```text
ControllerAgent
  - receives every user turn
  - lists available agents and tools
  - routes to one active worker or a workflow plan
  - owns admin-only prompt/model reload tools
  - restores control after worker completion

Worker agents
  - ShellAgent
  - DocAgent
  - DiagramAgent
  - ImageAgent or image capability
  - WebResearchAgent or web evidence capability
  - future VisionAgent, LocationAgent, InboxAgent
```

Agents should live under an `agents/` tree. Each agent owns its prompt, tool list,
context policy, model policy, and lifecycle hooks. The controller can compose agents,
but worker agents must not switch agents, reload prompts, or call tools outside their
allowlist. Agent-to-agent composition should remain controller-mediated and policy-checked,
not worker-to-worker control flow.

### Agent Registry

The runtime should have an `AgentRegistry` that is the code source of truth for:

| Field | Meaning |
|---|---|
| `agent_id` | Stable id used by controller and logs. |
| `prompt_path` | Markdown prompt file for the agent. |
| `tool_names` | Tools visible to the agent. |
| `default_model` | Model used unless overridden by admin/config. |
| `allowed_models` | Model ids the agent may use. |
| `context_policy` | Preserve, summarize, or reset context on activation/reload. |
| `activation_hints` | Descriptive hints for what kinds of user requests fit this agent. |
| `direct_entry` | Whether the controller may activate this agent directly from a user turn. |
| `may_request` | Which other agents the controller may compose on behalf of this agent. |

Prompt frontmatter can declare expected metadata, but it must be validated against the
registry. Editing a prompt file must not grant unknown tools or unauthorized models.

### External Prompts And Reload

Prompts should be markdown files, not Python strings. They should support YAML
frontmatter for readable metadata:

```markdown
---
agent_id: doc
default_model: gpt-4o-mini
allowed_models:
  - gpt-4o-mini
  - gpt-4o
context_policy: preserve_with_summary
activation_hints:
  - document
  - markdown
composition_policy:
  direct_entry: true
  may_request:
    - diagram
    - web
tools:
  - read_doc
  - write_to_doc
  - edit_doc
  - exit_doc_mode
---

Prompt body here.
```

`activation_hints` are advisory routing metadata and logging/debug context. They should
not silently veto the controller's choice. `composition_policy` is the enforceable layer:
it defines whether an agent is a valid direct entrypoint and which downstream agents the
controller may bring in while that agent owns a workflow.

Admin-only controller tools:

| Tool | Purpose |
|---|---|
| `list_agents` | Show registered agents, active agent, model, and prompt path. |
| `list_agent_tools` | Show tools available to one agent. |
| `read_agent_prompt` | Read the current prompt file for debugging. |
| `prompt_reload_with_context` | Reload prompt and optionally model while preserving allowed context. |
| `prompt_reload_reset_context` | Reload prompt and model, then reset that agent's context. |

Reload/model-switching rules:

- Reload tools are visible only to `ControllerAgent`.
- Reload should require explicit admin phrasing from the user/controller path.
- Worker agents never see reload tools in their schema.
- Runtime tool enforcement must reject forbidden calls even if an LLM emits an
  unadvertised tool call.
- Same-provider model switches may preserve context if supported.
- Cross-provider switches should reset context unless a compatibility layer proves
  preservation is safe.

### Context Switching

The runtime should mutate the active `LLMContext` messages/tools for the selected agent
rather than rebuilding the whole Pipecat pipeline for ordinary handoffs. Each handoff
should log:

- source agent
- target agent
- user intent summary
- context policy used
- model selected
- visible tools

The controller can compose agents by creating an explicit workflow plan, not by letting
agents chat freely with each other. Composition remains explicit and visible in logs via
controller-mediated calls such as
`activate_agent(agent_id, user_request, requested_by_agent="doc")`. The
`requested_by_agent` value is for readable logs; runtime enforcement should derive the
actual workflow owner from runtime state, not trust the controller string blindly.
Example flow:

```text
User request
  -> ControllerAgent
  -> workflow plan
  -> DocAgent creates/opens markdown page
  -> VisionAgent extracts attachment evidence
  -> WebResearchAgent gathers cited evidence
  -> DiagramAgent creates Mermaid/Excalidraw artifact
  -> DocAgent updates final page
  -> ControllerAgent reports result
```

### Pre-Excalidraw Stabilization Slice

Before any Excalidraw work lands, finish this short runtime slice:

| Step | Change | Done when |
|---|---|---|
| 1 | Tighten `ControllerAgent` routing prompt | Worker-owned requests trigger `activate_agent(...)` instead of narration like "I'm having trouble activating..." |
| 2 | Harden `composition_policy` | Workflow ownership is derived from runtime state, not a controller-supplied string; `force=True` is admin-only |
| 3 | Keep `activation_hints` advisory | Hints log mismatches but do not silently veto handoffs |
| 4 | Promote `image` to a top-level agent (Phase A) | Cold-start image/icon search works through `ImageAgent`, while existing diagram embedding continues to work |
| 5 | Preserve nested document behavior | `DiagramAgent` and `ImageAgent` may still operate against the active doc/diagram context when one exists |
| 6 | Expand tests and manual smoke coverage | Composition allow/deny, forced activation, top-level image search, and diagram/doc embedding paths are all covered |

This slice should ship before Excalidraw so image search and controller-mediated
composition are not solved twice for Mermaid first and Excalidraw later.

---

## Workflows To Support

### Live Cockpit Workflow

The current browser app remains the primary V1 interface:

- speak to the cockpit
- run terminal commands
- create/open docs
- insert or edit diagrams
- focus a diagram
- later: sketch and edit Excalidraw diagrams

### Chinnu Inbox Workflow

"Chinnu" is the controller persona exposed through an inbox adapter. WhatsApp is a
placeholder; Telegram or another officially supported channel can be used first if it is
cleaner.

Inputs may include:

- text messages
- voice notes
- photos
- whiteboard images
- locations/coordinates
- channel or group context such as `real-estate-search` or `startup-ideas`

The inbox adapter should normalize each message batch into an input bundle:

```text
InputBundle
  - channel_id / conversation_id
  - sender identity
  - text
  - transcribed voice notes
  - attachments
  - locations
  - timestamps
  - permission/context labels
```

The controller then routes the bundle through agents. The channel name can bias the
workflow but should not be treated as an instruction by itself.

### Example: Real Estate Channel

User sends voice notes, property photos, and coordinates. Expected workflow:

1. Language layer transcribes/translates if needed.
2. Controller identifies a real-estate note-taking workflow.
3. DocAgent creates/updates a markdown page.
4. VisionAgent extracts photo observations as evidence.
5. Location/WebResearchAgent searches for nearby infrastructure projects, zoning/public
   information, travel context, and cited facts.
6. DocAgent adds a table with property attributes, evidence, and follow-ups.
7. DiagramAgent may add a location/context diagram if useful.

### Example: Startup Ideas Channel

User sends a voice note and whiteboard photos. Expected workflow:

1. Language layer transcribes/translates if needed.
2. VisionAgent extracts whiteboard text/shapes.
3. DocAgent creates a structured idea note.
4. DiagramAgent creates a Mermaid or Excalidraw diagram from the whiteboard.
5. WebResearchAgent can gather supporting evidence only when asked or clearly useful.

### Example: Visual Asset Request

User asks for a "database" or "S3" picture for a diagram. Expected workflow:

1. DiagramAgent determines where the asset belongs.
2. Asset/Web agent searches or selects from a curated icon library.
3. Evidence metadata is recorded where relevant.
4. DiagramAgent applies a validated command, not raw image edits from the model.

---

## Evidence And Trust Policy

External data must be treated as evidence, not instructions. This applies to web pages,
search results, OCR text, photo contents, whiteboard text, document text, and messages
forwarded from an inbox.

Rules:

- User intent comes from the authenticated user/controller path.
- Attachments and web content are data for analysis.
- WebResearchAgent must capture source URLs, retrieved timestamps, and concise evidence
  notes.
- Current/latest facts should use live web search when network access is available.
- The controller should distinguish "the user asked" from "a document/webpage says".
- Generated documents should preserve citations or evidence notes where factual claims
  matter.

---

## Platform And Framework Direction

Pipecat should remain the core runtime for low-latency voice, WebRTC, interruption,
streaming STT/TTS, and the live cockpit. The agent runtime should be a thin layer over
the current Pipecat pipeline, not a wholesale framework rewrite.

LangGraph is a later option for durable, long-running, resumable inbox workflows if the
homegrown controller workflow planner becomes too ad hoc. It should not replace Pipecat
for the realtime voice/canvas path.

Model routing should be local-first over time:

- V1 can use current cloud/provider models.
- V2 should introduce model slots per agent.
- OpenRouter should be considered for the vision model path, especially for
  `VisionAgent`/visual evidence workflows. The goal is not to make it the only provider,
  but to make it easy to switch between different vision-capable models and compare
  performance on property photos, whiteboards, diagram screenshots, and visual asset
  requests.
- V3 should route many slots to local models on an NVIDIA DGX Spark or similar local
  GPU box, with cloud fallback for tasks that need stronger models or current web access.

Vision model experiments should log provider, model id, latency, cost when available,
input type, and a short task label so model quality/performance comparisons are
debuggable rather than anecdotal.

Remote access should use a VPN-like private network such as Tailscale or an equivalent
mesh/VPN layer so the cockpit can be reached securely from anywhere without exposing it
directly to the public internet. The same private network approach should extend to
future DGX/NVIDIA-hosted services so the controller can reach local model endpoints,
artifacts, and workflow services across machines with a stable private address space.

Realtime calling is a later capability. Voice notes and text messages are easier and
should come first. Direct calls require low-latency context retrieval, streaming TTS
such as Cartesia or a local equivalent, interruption handling, and a telephony/channel
adapter with official support.

Multilingual support should be a controller-layer capability, not a separate worker
agent. Kannada/Telugu/English input should be normalized before routing, while original
text/transcripts are preserved for audit and family use.

---

## Implementation Phases

| Phase | Capability | Implementation work | Done criteria / manual testing | Estimate |
|---|---|---|---|---|
| V0.0 | Stabilize current base | Finish current manual smoke tests; confirm git save/push warnings and file allowlist. | Shell, doc, Mermaid focus, model switching, commit list, and push-warning flows pass manually. | 0.5-1 day |
| V1.1 | Agent runtime foundation | Add `AgentRegistry`, agent metadata, active-agent state, tool allowlist enforcement, handoff logging. | Controller can list agents; active agent sees only its tools; forbidden tool calls are rejected in runtime. | 2-4 days |
| V1.2 | External prompts and reload | Move prompts to markdown files with validated frontmatter; add controller-only reload/read/list tools. | Edit prompt file, reload with context, observe behavior change without restart; reset reload clears context. | 2-4 days |
| V1.3 | Controller routing and composition | Route every turn through ControllerAgent; implement worker activation and return-to-controller flow. | Shell/doc/diagram requests route correctly; workers cannot reload prompts or switch agents. | 3-5 days |
| V1.4 | Migrate existing modes | Move shell/doc/diagram/image/web tool registration into agent-owned modules/directories. | Existing cockpit workflows still pass; controller prompt is smaller; each agent has a debuggable prompt/tool list. | 4-7 days |
| V1.5 | Excalidraw MVP | Add DiagramAgent Excalidraw scene storage, metadata, SVG/PNG export, validated command application. | Create, save, reopen, export, and commit one Excalidraw diagram; invalid commands do not corrupt scene. | 1-2 weeks |
| V1.6 | Mermaid-to-Excalidraw promotion | Convert Mermaid diagrams to Excalidraw on explicit promotion; update markdown render links from metadata. | Mermaid remains default; promoted diagram loads from `.excalidraw`; SVG/PNG are regenerated and committed. | 3-5 days |
| V1.7 | Vision model switching | Add a vision model slot and OpenRouter-backed provider option for comparing VLMs on screenshots, photos, whiteboards, and visual asset requests. | Run the same visual task through at least two models; logs show provider/model/latency/cost where available; chosen output is recorded as evidence. | 2-4 days |
| V2.1 | Web/evidence agent | Add web research with citations, source timestamps, and evidence summaries. | Ask for current/local facts; generated doc includes sourced evidence and does not treat pages as instructions. | 3-6 days |
| V2.2 | Inbox adapter MVP | Add Telegram or official WhatsApp-style message adapter for text, voice notes, attachments, locations. | Send voice note + photo to a channel; Chinnu creates/updates a markdown page with transcript and attachments. | 1-2 weeks |
| V2.3 | Workflow templates | Add controller workflow plans for real estate, startup ideas, visual asset requests, and generic notes. | Channel-specific examples produce structured docs/tables/diagrams with clear evidence trails. | 1-2 weeks |
| V2.4 | Multilingual input | Add language detection, Kannada/Telugu transcription/translation path, original transcript preservation. | Kannada/Telugu voice note creates an English or bilingual note without losing original transcript. | 3-7 days |
| V3.1 | Durable workflow engine | Evaluate LangGraph for resumable inbox jobs, retries, checkpoints, and long-running research. | A multi-step inbox workflow can pause/retry/resume without losing state; Pipecat live path is unaffected. | 1-2 weeks |
| V3.15 | Private network access | Add Tailscale or equivalent private-network access for the cockpit and future DGX/NVIDIA boxes. | Cockpit is reachable remotely over the private network; no direct public exposure is required; controller can reach remote/local model services over stable private addresses. | 2-5 days |
| V3.2 | Local-first DGX runtime | Add local model slots and routing to DGX-hosted LLM/VLM/STT/TTS where practical. | Selected agents run locally; cloud fallback is explicit; logs show model/source per step. | 1-3 weeks |
| V3.3 | Realtime calls | Add direct voice-call channel with streaming context retrieval, interruption, and low-latency TTS. | Call Chinnu, ask about a known document/channel, receive timely spoken response with correct context. | 2-4 weeks |

---

## Immediate Next Step

Start with V1.1: `AgentRegistry` and runtime tool allowlist enforcement. Do not start
Excalidraw until there is a working controller-to-worker handoff and external prompt
reload path. Excalidraw should be the first proof that the new runtime makes a complex
capability easier to add.

---

## Manual Test Priorities

Before agent refactor:

1. Shell flow works before and after doc mode.
2. Doc mode creates/opens a project under `VOICE_COCKPIT_GIT_ROOT`.
3. Save commits only expected project files and reports committed/pushed files.
4. Push failure leaves a local commit and produces an explicit warning.
5. Mermaid creation, focus, update, and exit work.
6. Model switching does not crash the pipeline.

After agent refactor:

1. Existing shell/doc/diagram/image flows still work.
2. Active agent context exposes only that agent's tools.
3. Runtime rejects forbidden tool calls.
4. Worker agents cannot call admin reload tools.
5. Prompt reload with context changes behavior without restart.
6. Prompt reload with reset clears the agent context.
7. Controller can compose DocAgent + DiagramAgent + WebResearchAgent in one workflow.
8. Controller-enforced composition policy blocks disallowed chains such as `doc -> shell`
   unless an explicit admin/debug override is used.

After Excalidraw:

1. Create a scene and save `.excalidraw`, `.svg`, `.png`, and diagram metadata.
2. Commit exactly the expected files.
3. Re-open the document and load the current render from metadata.
4. Reject invalid diagram commands without corrupting the scene.

---

## Detailed Specs

| File | Purpose |
|---|---|
| `server/diagramming_plan.md` | Diagram-specific plan: Mermaid, Excalidraw, exports, visual model, freehand/vision loop. |
| `server/manual_tests_phase1.md` | Manual checklist for existing doc-mode and storage behavior. |
| `server/poc/PHASE0_STATUS.md` | Historical Phase 0 PoC handoff; not the current roadmap. |
