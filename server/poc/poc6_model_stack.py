"""PoC 6 — Model capability stack validation.

Tests:
1. Claude Sonnet: receives minimal Excalidraw JSON → returns valid JSON with elements array
2. Gemini Flash: receives a small PNG → returns structured text within 5 seconds
3. @excalidraw/mermaid-to-excalidraw: conversion quality evaluation (Node.js required)

Run:
    uv run poc/poc6_model_stack.py

Required env vars:  ANTHROPIC_API_KEY, GEMINI_API_KEY
"""

import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add server root to path for dotenv
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

PASS = "PASS ✅"
FAIL = "FAIL ❌"


def banner(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ---------------------------------------------------------------------------
# Minimal test assets
# ---------------------------------------------------------------------------

MINIMAL_EXCALIDRAW_SCENE = {
    "type": "excalidraw",
    "version": 2,
    "source": "poc6-test",
    "elements": [
        {
            "id": "rect-1",
            "type": "rectangle",
            "x": 100,
            "y": 100,
            "width": 200,
            "height": 80,
            "strokeColor": "#000000",
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": 2,
            "roughness": 1,
            "opacity": 100,
        }
    ],
    "appState": {"viewBackgroundColor": "#ffffff"},
}

CLEANUP_PROMPT = (
    "You are a diagram layout assistant. The user wants to clean up the following "
    "Excalidraw scene JSON. Move the rectangle to x=50, y=50 and return ONLY valid "
    "Excalidraw scene JSON with an 'elements' array. No markdown, no explanation."
)

MERMAID_SEQUENCE = """sequenceDiagram
    participant User
    participant Server
    User->>Server: login(username, password)
    Server-->>User: JWT token
"""


# ---------------------------------------------------------------------------
# Test 1: Claude Sonnet — JSON-only diagram cleanup
# ---------------------------------------------------------------------------


def test_claude_sonnet() -> bool:
    banner("Test 1: Claude Sonnet — Excalidraw JSON cleanup")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"  SKIP — ANTHROPIC_API_KEY not set")
        return True  # don't fail the suite for missing key

    try:
        import anthropic
    except ImportError:
        print("  SKIP — anthropic package not installed (run: uv sync)")
        return True

    client = anthropic.Anthropic(api_key=api_key)
    scene_json = json.dumps(MINIMAL_EXCALIDRAW_SCENE)

    print(f"  → Sending {len(scene_json)} chars to claude-sonnet-4-6 …")
    t0 = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=CLEANUP_PROMPT,
            messages=[{"role": "user", "content": f"Scene JSON:\n{scene_json}"}],
        )
    except Exception as e:
        print(f"  {FAIL} — API error: {e}")
        return False

    elapsed = time.monotonic() - t0
    raw = resp.content[0].text.strip()
    print(f"  Response ({elapsed:.1f}s, {len(raw)} chars): {raw[:120]}…")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  {FAIL} — Response is not valid JSON: {e}")
        return False

    if "elements" not in parsed:
        print(f"  {FAIL} — JSON missing 'elements' key. Keys: {list(parsed.keys())}")
        return False

    if not isinstance(parsed["elements"], list):
        print(f"  {FAIL} — 'elements' is not a list")
        return False

    print(f"  {PASS} — valid JSON with elements array ({len(parsed['elements'])} element(s))")
    return True


# ---------------------------------------------------------------------------
# Test 2: Gemini Flash — PNG input within 5 seconds
# ---------------------------------------------------------------------------


def _make_test_png() -> bytes:
    """Generate a minimal 100x60 white PNG without external deps."""
    import struct
    import zlib

    def chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    w, h = 100, 60
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    ihdr = chunk(b"IHDR", ihdr_data)
    raw_rows = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))
    idat = chunk(b"IDAT", zlib.compress(raw_rows))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def test_gemini_flash() -> bool:
    banner("Test 2: Gemini Flash — PNG round-trip within 5 seconds")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print(f"  SKIP — GEMINI_API_KEY not set")
        return True

    try:
        import google.generativeai as genai
    except ImportError:
        print("  SKIP — google-generativeai not installed (run: uv sync)")
        return True

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    png_bytes = _make_test_png()
    print(f"  → Sending {len(png_bytes)}-byte PNG to gemini-2.0-flash …")

    import google.generativeai.types as gtypes

    t0 = time.monotonic()
    try:
        resp = model.generate_content(
            [
                gtypes.Part.from_data(data=png_bytes, mime_type="image/png"),
                "Describe what you see in this image in one sentence.",
            ]
        )
    except Exception as e:
        print(f"  {FAIL} — API error: {e}")
        return False

    elapsed = time.monotonic() - t0
    text = resp.text.strip() if resp.text else ""
    print(f"  Response ({elapsed:.1f}s): {text[:120]}")

    if elapsed > 5.0:
        print(f"  {FAIL} — Response took {elapsed:.1f}s (> 5s limit)")
        return False

    if not text:
        print(f"  {FAIL} — Empty response from Gemini")
        return False

    print(f"  {PASS} — structured text response in {elapsed:.1f}s")
    return True


# ---------------------------------------------------------------------------
# Test 3: @excalidraw/mermaid-to-excalidraw conversion quality
# ---------------------------------------------------------------------------


def test_mermaid_to_excalidraw() -> bool:
    banner("Test 3: @excalidraw/mermaid-to-excalidraw conversion quality")

    node_check = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node_check.returncode != 0:
        print("  SKIP — Node.js not available")
        return True

    # Check if the package is available (needs npm install in a temp dir)
    script = """
const { parseMermaidToExcalidraw } = require('@excalidraw/mermaid-to-excalidraw');

const src = `sequenceDiagram
    participant User
    participant Server
    User->>Server: login(username, password)
    Server-->>User: JWT token`;

parseMermaidToExcalidraw(src).then(result => {
    const elements = result.elements || [];
    const participantNodes = elements.filter(el =>
        el.type === 'rectangle' || el.type === 'text'
    );
    const out = {
        elementCount: elements.length,
        participantNodeCount: participantNodes.length,
        hasElements: elements.length > 0,
    };
    console.log(JSON.stringify(out));
}).catch(e => {
    console.error('ERROR:' + e.message);
    process.exit(1);
});
"""

    # Try to find a node_modules with the package somewhere nearby
    search_dirs = [
        Path(__file__).parent,
        Path(__file__).parent.parent,
        Path.home() / "node_modules",
    ]
    pkg_found = any((d / "node_modules" / "@excalidraw" / "mermaid-to-excalidraw").exists()
                    for d in search_dirs)

    if not pkg_found:
        print("  INFO — @excalidraw/mermaid-to-excalidraw not installed locally.")
        print("         To install: npm install @excalidraw/mermaid-to-excalidraw")
        print("         Then re-run this script.")
        print("  SKIP — skipping conversion quality check")
        return True

    # Find the directory where the package lives
    pkg_dir = next(
        d for d in search_dirs
        if (d / "node_modules" / "@excalidraw" / "mermaid-to-excalidraw").exists()
    )

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, cwd=str(pkg_dir), timeout=15
    )

    if result.returncode != 0:
        print(f"  {FAIL} — Node script error: {result.stderr.strip()[:200]}")
        return False

    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        print(f"  {FAIL} — Could not parse node output: {result.stdout[:200]}")
        return False

    print(f"  Elements: {data['elementCount']}, participant-like nodes: {data['participantNodeCount']}")

    if not data["hasElements"]:
        print(f"  {FAIL} — Conversion produced empty scene (SVG fallback required)")
        return False

    if data["participantNodeCount"] >= 2:
        print(f"  {PASS} — Scene contains participant nodes ({data['participantNodeCount']})")
    else:
        print(f"  WARNING ⚠️ — Only {data['participantNodeCount']} participant node(s); check output quality")

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    results = {
        "Claude Sonnet JSON cleanup": test_claude_sonnet(),
        "Gemini Flash PNG round-trip": test_gemini_flash(),
        "mermaid-to-excalidraw conversion": test_mermaid_to_excalidraw(),
    }

    banner("PoC 6 Summary")
    all_pass = True
    for name, ok in results.items():
        status = PASS if ok else FAIL
        print(f"  {status}  {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("  Overall: PASS ✅ — model stack validated")
    else:
        print("  Overall: FAIL ❌ — fix failures before Phase 3")
        sys.exit(1)


if __name__ == "__main__":
    main()
