"""Tests for the sketch pipeline's tool-agnostic middle.

Layer 2 (ops) and layer 3 (Excalidraw translation) are pure logic and tested
here without touching a model. Layer 1 — asking a model to read a drawing —
needs a real image and a real provider, so it lives in the spike.
"""

import json

import pytest

import sketch
from vision import VisionSettings, vision_capable, vision_models


def ops(*commands):
    return json.dumps({"commands": list(commands)})


class TestOpsValidation:
    """Bad ops must be reported, not silently repaired.

    Which model produced invalid ops is exactly what a comparison is measuring,
    so papering over it destroys the signal.
    """

    def test_a_well_formed_diagram_validates(self):
        ok, problems = sketch.validate_ops(ops(
            {"op": "create_node", "id": "api", "label": "API"},
            {"op": "create_node", "id": "db", "label": "DB"},
            {"op": "connect", "id": "e", "from": "api", "to": "db"},
        ))
        assert ok and problems == []

    def test_an_arrow_to_a_node_that_does_not_exist_is_caught(self):
        ok, problems = sketch.validate_ops(ops(
            {"op": "create_node", "id": "api", "label": "API"},
            {"op": "connect", "id": "e", "from": "api", "to": "ghost"},
        ))
        assert not ok
        assert any("ghost" in p for p in problems)

    def test_an_unknown_op_is_caught(self):
        ok, problems = sketch.validate_ops(ops({"op": "teleport", "id": "x"}))
        assert not ok

    def test_missing_required_fields_are_named(self):
        ok, problems = sketch.validate_ops(ops({"op": "create_node", "id": "a"}))
        assert not ok
        assert any("label" in p for p in problems)

    def test_prose_around_the_json_is_tolerated(self):
        """Models wrap JSON in fences or commentary despite being told not to."""
        raw = "Here you go:\n```json\n" + ops(
            {"op": "create_node", "id": "a", "label": "A"}) + "\n```"
        ok, _ = sketch.validate_ops(raw)
        assert ok

    def test_non_json_fails_cleanly(self):
        ok, problems = sketch.validate_ops("I cannot see an image.")
        assert not ok and problems


class TestExcalidrawTranslation:
    """Layer 3 is the only layer that knows about a drawing tool."""

    def test_nodes_and_arrows_become_elements(self):
        els, errors, _ = sketch.translate_ops_to_excalidraw([
            {"op": "create_node", "id": "a", "label": "A", "shape": "rectangle", "x": 0, "y": 0},
            {"op": "create_node", "id": "b", "label": "B", "shape": "ellipse", "x": 200, "y": 0},
            {"op": "connect", "id": "e", "from": "a", "to": "b"},
        ])
        assert not errors
        assert {e.type for e in els} == {"rectangle", "ellipse", "arrow"}

    def test_shapes_map_to_tool_types(self):
        assert sketch.shape_to_excalidraw_type("rhombus") == "diamond"
        assert sketch.shape_to_excalidraw_type("rounded") == "rectangle"

    def test_move_repositions_rather_than_duplicating(self):
        els, _, _ = sketch.translate_ops_to_excalidraw([
            {"op": "create_node", "id": "a", "label": "A", "x": 0, "y": 0},
            {"op": "move", "id": "a", "x": 500, "y": 300},
        ])
        nodes = [e for e in els if e.type != "text"]
        assert len(nodes) == 1
        assert (nodes[0].x, nodes[0].y) == (500, 300)

    def test_the_ops_carry_no_tool_specifics(self):
        """Swapping canvas should mean replacing layer 3 alone."""
        assert "excalidraw" not in sketch.SYSTEM_OPS_GENERATION.lower().replace(
            "no excalidraw json", "")


class TestVisionSettings:
    """A separate model for sketching, changeable without disturbing the chat."""

    def test_it_follows_the_conversation_model_by_default(self):
        assert VisionSettings("claude-sonnet-5").current.model == "claude-sonnet-5"

    def test_voice_overrides_and_says_so(self):
        vs = VisionSettings("claude-sonnet-5")
        choice = vs.choose("claude-opus-5-5")
        assert choice.model == "claude-opus-5-5"
        assert "voice" in choice.source

    def test_clearing_returns_to_the_conversation_model(self):
        vs = VisionSettings("claude-sonnet-5")
        vs.choose("claude-opus-5-5")
        assert vs.clear().model == "claude-sonnet-5"

    def test_env_sits_between_voice_and_the_conversation_model(self, monkeypatch):
        monkeypatch.setenv("VISION_MODEL", "gpt-6-sol")
        vs = VisionSettings("claude-sonnet-5")
        assert vs.current.model == "gpt-6-sol"
        assert vs.choose("claude-opus-5-5").model == "claude-opus-5-5"
        assert vs.clear().model == "gpt-6-sol"

    def test_the_fallback_follows_the_dropdown(self):
        vs = VisionSettings("claude-sonnet-5")
        vs.set_conversation_model("gpt-6-sol")
        assert vs.current.model == "gpt-6-sol"


class TestVisionCapability:
    def _catalog(self):
        from catalog import ModelInfo
        return {
            "sees": ModelInfo(id="sees", provider="anthropic", vision=True),
            "blind": ModelInfo(id="blind", provider="ollama", vision=False),
            "unsaid": ModelInfo(id="unsaid", provider="openai", vision=None),
        }

    def test_known_blind_models_are_not_offered(self):
        """Offering one is offering a guaranteed failure."""
        assert "blind" not in vision_models(self._catalog())

    def test_unknown_is_offered_after_known(self):
        """None means nobody published it — it may work perfectly."""
        assert vision_models(self._catalog()) == ["sees", "unsaid"]

    @pytest.mark.parametrize("model,expected", [("sees", True), ("blind", False), ("unsaid", None)])
    def test_capability_is_tri_state(self, model, expected):
        assert vision_capable(self._catalog(), model) is expected

    def test_an_unknown_model_id_is_unknown_not_false(self):
        assert vision_capable(self._catalog(), "never-heard-of-it") is None
