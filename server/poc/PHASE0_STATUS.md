# Phase 0 — Session Handoff (Historical, 2026-06-06)

> Historical note: this file records an early Phase 0 spike handoff. It is not the
> current roadmap. The canonical roadmap is `../../plan.md`; the current diagramming
> design is `../diagramming_plan.md`.

## What was built this session

All Phase 0 infrastructure spikes are implemented. The plan file (`diagramming_plan.md`) has been updated with status markers on each PoC.

### Files created / modified

| File | Change |
|---|---|
| `.env.example` | Added model API key examples and the docs root env var used at the time. Current storage uses `VOICE_COCKPIT_GIT_ROOT`. |
| `pyproject.toml` | Added `anthropic`, `google-generativeai` deps; `pytest`, `pytest-asyncio` to dev |
| `uv.lock` | Updated by `uv add` |
| `editor.html` | New — Excalidraw UMD (pinned v0.17.6), postMessage API (`get-scene`, `set-scene`, `get-png`) |
| `poc/poc1_iframe_test.html` | New — parent PASS/FAIL test harness for PoC 1 |
| `poc/poc3_mermaid_test.html` | New — auto-runs Mermaid XSS + broken-syntax PASS criteria |
| `poc/poc5_overlay_test.html` | New — autosave-on-type + reload recovery test for PoC 5 |
| `poc/atomic_write.py` | New — atomic write, project lock, `create_version_dir`, `sanitize_slug` |
| `poc/poc6_model_stack.py` | New — Claude Sonnet + Gemini Flash + mermaid-to-excalidraw spike |
| `tests/__init__.py` | New |
| `tests/test_poc4_atomic_write.py` | New — 16 pytest tests for PoC 4 |
| `bot.py` | Early STT/PoC routes were wired. Speaker diarization was later removed from the product path. |
| `diagramming_plan.md` | Status table added at top; each PoC annotated with ✅/⬜ and "How to verify" |

### Git state

- Branch: `main`
- Rollback tag: `phase0-pre` (points to `b7d6d34`, the pre-Phase-0 commit)
- All Phase 0 changes are **unstaged / untracked** — not yet committed

---

## PoC verification status

| PoC | Status | How to verify |
|---|---|---|
| PoC 1 — Excalidraw iframe | ⬜ Needs browser test | Bot running → `/poc/poc1_iframe_test` → draw rectangle → "Get Scene" → PASS badge |
| PoC 2 — speaker diarization | Retired | Speaker diarization was later removed; doc mode assumes a single user plus controller. |
| PoC 3 — Mermaid security | ⬜ Needs browser test | Bot running → `/poc/poc3_mermaid_test` → tests auto-run → all 3 should show PASS |
| PoC 4 — Atomic write | ✅ PASS | `uv run pytest tests/test_poc4_atomic_write.py -v` → 16/16 pass (verified this session) |
| PoC 5 — Overlay recovery | ⬜ Needs browser test | Bot running → `/poc/poc5_overlay_test` → type text → "Autosave Now" → "Hard Refresh" → PASS badges |
| PoC 6 — Model stack | ⬜ Needs API keys | Add `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` to `.env` → `uv run poc/poc6_model_stack.py` |

### PoC 6 also needs
```
npm install @excalidraw/mermaid-to-excalidraw
```
Run from `server/` or any parent dir, then re-run poc6 script to evaluate conversion quality.

---

## Next steps (in order)

1. **Add API keys to `.env`** — `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`
2. **Run the bot** — `uv run bot.py` → open `http://localhost:7860`
3. **Browser-verify PoC 1, 3, 5** — open the `/poc/` URLs above
4. **Run PoC 6** — `uv run poc/poc6_model_stack.py`
5. **Skip retired PoC 2** — diarization is no longer in scope.
6. **Record PASS/FAIL** for still-relevant PoCs if rerun.
7. **Use `../../plan.md` for current next steps** — agent runtime first, Excalidraw second.

## To resume with Claude

Use this only as historical context. The active project plan is `../../plan.md`.
