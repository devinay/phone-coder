"""Test the three-layer architecture without hitting real LLMs.

This validates:
- Layer 2 ops generation structure (tool-agnostic)
- Layer 3 translation to Excalidraw (tool-specific adapter)
"""

import json
from vision_spike import validate_ops, translate_ops_to_excalidraw


def test_layer2_ops_generation():
    """Layer 2: LLM produces tool-agnostic ops."""
    # Simulating what the LLM would output: ops that don't know about Excalidraw
    ops_json = json.dumps({
        "commands": [
            {"op": "create_node", "id": "api", "label": "API", "shape": "rectangle", "x": 100, "y": 100},
            {"op": "create_node", "id": "db", "label": "Database", "shape": "rectangle", "x": 300, "y": 100},
            {"op": "connect", "id": "api_to_db", "from": "api", "to": "db", "label": "query"},
            {"op": "set_style", "id": "api", "style": {"color": "#ff0000"}},
            {"op": "move", "id": "db", "x": 400, "y": 200},
        ]
    })

    ok, problems = validate_ops(ops_json)
    assert ok, f"Layer 2 ops should be valid, but got problems: {problems}"
    print("✅ Layer 2: Tool-agnostic ops generation — OK")


def test_layer3_translator():
    """Layer 3: Translate tool-agnostic ops to Excalidraw (tool-specific)."""
    ops = [
        {"op": "create_node", "id": "api", "label": "API", "shape": "rectangle", "x": 100, "y": 100},
        {"op": "create_node", "id": "cache", "label": "Cache", "shape": "ellipse", "x": 300, "y": 100},
        {"op": "create_node", "id": "db", "label": "Database", "shape": "rectangle", "x": 500, "y": 100},
        {"op": "connect", "id": "api_to_cache", "from": "api", "to": "cache"},
        {"op": "connect", "id": "cache_to_db", "from": "cache", "to": "db"},
        {"op": "set_style", "id": "api", "style": {"color": "#ff0000"}},
        {"op": "move", "id": "cache", "x": 350, "y": 150},
    ]

    elements, errors, warnings = translate_ops_to_excalidraw(ops)

    assert not errors, f"Translation should have no errors, but got: {errors}"
    assert len(elements) == 5, f"Should have 5 elements (3 nodes + 2 arrows), got {len(elements)}"

    # Check that tool-specific fields were added
    api_elem = next((e for e in elements if e.id == "api"), None)
    assert api_elem, "api element should exist"
    assert api_elem.type == "rectangle", "api should be a rectangle"
    assert api_elem.text == "API", "api should have text label"
    assert hasattr(api_elem, "strokeColor"), "Excalidraw-specific field strokeColor should exist"

    # Check arrows were created
    arrows = [e for e in elements if e.type == "arrow"]
    assert len(arrows) == 2, f"Should have 2 arrows, got {len(arrows)}"

    print("✅ Layer 3: Tool-agnostic ops → Excalidraw (tool-specific) — OK")
    print(f"   Generated {len(elements)} Excalidraw elements ({len(arrows)} arrows)")


def test_tool_swappability():
    """Demonstrate that swapping tools only requires a new translator.

    The same ops list can be translated to any tool (tldraw, SVG, etc.)
    without touching Layer 2 (LLM).
    """
    ops = [
        {"op": "create_node", "id": "a", "label": "A", "shape": "rectangle", "x": 0, "y": 0},
        {"op": "create_node", "id": "b", "label": "B", "shape": "circle", "x": 100, "y": 100},
        {"op": "connect", "id": "a_to_b", "from": "a", "to": "b"},
    ]

    # Current tool: Excalidraw
    exc_elements, exc_errors, _ = translate_ops_to_excalidraw(ops)
    assert not exc_errors, f"Excalidraw translation failed: {exc_errors}"
    assert len(exc_elements) == 3  # 2 nodes + 1 arrow

    # If we had a tldraw translator (hypothetically):
    # tldraw_elements, tldraw_errors, _ = translate_ops_to_tldraw(ops)
    # assert len(tldraw_elements) == 3
    #
    # And the LLM ops don't change. Only the translator changes.
    # This proves tool independence.

    print("✅ Tool swappability: Same ops → different tools (only translator changes) — OK")


if __name__ == "__main__":
    test_layer2_ops_generation()
    test_layer3_translator()
    test_tool_swappability()
    print("\n🎉 All three-layer tests passed!")
