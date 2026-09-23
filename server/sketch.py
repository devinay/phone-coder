"""Sketch to diagram: the tool-agnostic middle, promoted out of the spike.

Three layers, and the split is the point:

1. **Vision** reads the drawing — ``vision.py``, the only part that talks to a
   model.
2. **This module** validates the ops a model produced. They know nothing about
   any drawing tool: ``create_node``, ``connect``, ``move``, ``set_style``.
3. **The translator** at the bottom turns those ops into Excalidraw elements.

Supporting a different canvas means replacing layer 3 alone, which is why the
ops vocabulary is deliberately free of Excalidraw concepts. A model emitting
Excalidraw JSON directly would weld the product to one tool.

Proven first in ``spikes/vision_spike.py``, which still imports nothing from
here — it keeps its own copy for provider comparison. Promoted so the running
app never imports from a spike.
"""

import json
import re
import uuid
from dataclasses import dataclass, field

SYSTEM_OPS_GENERATION = """You convert a visual sketch plus the user's spoken intent into a
tool-agnostic command list. Output ONLY JSON of the form {"commands": [ ... ]}.

Allowed ops (tool-independent):
- create_node   {op, id, label, shape?, x?, y?, w?, h?}
- set_shape     {op, id, shape}
- set_style     {op, id, style}
- rename        {op, id, label}
- move          {op, id, x, y}
- connect       {op, id, from, to, label?}
- delete        {op, id}
- add_annotation{op, id, kind, x, y, text?, w?, h?}

Shape vocabulary (tool-independent, mapped to any tool at render time):
  rectangle, rounded, ellipse, circle, diamond, rhombus

Rules:
1. Give every element a stable snake_case id; reuse ids consistently
2. Arrows in `connect` must reference existing node ids
3. Do NOT output any tool-specific syntax (no Excalidraw JSON, no tldraw store, no SVG)
4. JSON ONLY — no prose, no markdown fences

Few-shot examples (note: these do NOT reference drawing tool specifics):

Example 1: Net-new diagram from a sketch.
User intent: "this is an API talking to a cache and a database"
Image shows: three scribbled boxes, arrows between them.
Output (tool-agnostic ops):
{"commands": [
  {"op": "create_node", "id": "api", "label": "API", "shape": "rectangle", "x": 100, "y": 100},
  {"op": "create_node", "id": "cache", "label": "Cache", "shape": "ellipse", "x": 300, "y": 100},
  {"op": "create_node", "id": "db", "label": "Database", "shape": "rectangle", "x": 500, "y": 100},
  {"op": "connect", "id": "api_to_cache", "from": "api", "to": "cache"},
  {"op": "connect", "id": "cache_to_db", "from": "cache", "to": "db"}
]}

Example 2: Style edit on an existing node (selection-based).
User intent: "make this pink"
(Selection indicates which node is "this".)
Output (tool-agnostic):
{"commands": [
  {"op": "set_style", "id": "cache", "style": {"color": "pink"}}
]}
"""


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


ALLOWED_OPS = {
    "create_node": {"id", "label", "shape", "x", "y", "w", "h"},
    "set_shape": {"id", "shape"},
    "set_style": {"id", "style"},
    "rename": {"id", "label"},
    "move": {"id", "x", "y"},
    "connect": {"id", "from", "to", "label"},
    "delete": {"id"},
    "add_annotation": {"id", "kind", "x", "y", "text", "w", "h"},
}
REQUIRED_FIELDS = {
    "create_node": {"id", "label"},
    "set_shape": {"id", "shape"},
    "set_style": {"id", "style"},
    "rename": {"id", "label"},
    "move": {"id", "x", "y"},
    "connect": {"id", "from", "to"},
    "delete": {"id"},
    "add_annotation": {"id", "kind", "x", "y"},
}
VALID_SHAPES = {"rectangle", "rounded", "ellipse", "circle", "diamond", "rhombus"}
VALID_ANNOTATION_KINDS = {"text", "box"}


def validate_ops(raw: str) -> tuple[bool, list[str]]:
    """Validate tool-agnostic ops structure."""
    problems: list[str] = []
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError as e:
        return False, [f"not valid JSON: {e}"]
    cmds = data.get("commands")
    if not isinstance(cmds, list) or not cmds:
        return False, ["missing or empty 'commands' array"]

    node_ids: set[str] = set()
    for i, c in enumerate(cmds):
        op = c.get("op")
        if op not in ALLOWED_OPS:
            problems.append(f"[{i}] unknown op {op!r}")
            continue
        missing = REQUIRED_FIELDS[op] - c.keys()
        if missing:
            problems.append(f"[{i}] {op} missing required fields {sorted(missing)}")
        unknown = c.keys() - ALLOWED_OPS[op] - {"op"}
        if unknown:
            problems.append(f"[{i}] {op} has unexpected fields {sorted(unknown)}")
        if op == "create_node" and "shape" in c:
            if c["shape"] not in VALID_SHAPES:
                problems.append(f"[{i}] create_node shape {c['shape']!r} not in {sorted(VALID_SHAPES)}")
        if op == "set_shape" and c.get("shape") not in VALID_SHAPES:
            problems.append(f"[{i}] set_shape shape {c.get('shape')!r} not in {sorted(VALID_SHAPES)}")
        if op == "add_annotation" and c.get("kind") not in VALID_ANNOTATION_KINDS:
            problems.append(f"[{i}] add_annotation kind {c.get('kind')!r} not in {sorted(VALID_ANNOTATION_KINDS)}")
        if op == "create_node":
            node_ids.add(c.get("id"))
    for i, c in enumerate(cmds):
        if c.get("op") == "connect":
            for end in ("from", "to"):
                if c.get(end) not in node_ids:
                    problems.append(f"[{i}] connect {end}={c.get(end)!r} references non-existent node")
    return len(problems) == 0, problems


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3: TRANSLATOR (Tool-Specific Adapter for Excalidraw)
# TO SWAP TOOLS: replace this entire section with a tldraw_translator, svg_translator, etc.
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExcalidrawElement:
    """Tool-specific: Excalidraw element representation."""
    id: str
    type: str
    x: float
    y: float
    width: float
    height: float
    angle: float = 0
    strokeColor: str = "#000000"
    backgroundColor: str = "transparent"
    fillStyle: str = "hachure"
    strokeWidth: int = 1
    roundness: str | None = "adaptive"
    text: str = ""
    fontSize: int = 16
    fontFamily: int = 1
    textAlign: str = "center"
    verticalAlign: str = "middle"
    startBinding: dict | None = None
    endBinding: dict | None = None
    boundElements: list[dict] | None = None
    seed: int | None = None
    versionNonce: int | None = None
    isDeleted: bool = False

    def to_dict(self):
        d = {
            "id": self.id,
            "type": self.type,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "angle": self.angle,
            "strokeColor": self.strokeColor,
            "backgroundColor": self.backgroundColor,
            "fillStyle": self.fillStyle,
            "strokeWidth": self.strokeWidth,
            "roundness": self.roundness,
            "seed": self.seed or int.from_bytes(uuid.uuid4().bytes[:4], 'little'),
            "versionNonce": self.versionNonce or int.from_bytes(uuid.uuid4().bytes[:4], 'little'),
            "isDeleted": self.isDeleted,
        }
        if self.text:
            d.update({"text": self.text, "fontSize": self.fontSize, "fontFamily": self.fontFamily,
                      "textAlign": self.textAlign, "verticalAlign": self.verticalAlign})
        if self.startBinding:
            d["startBinding"] = self.startBinding
        if self.endBinding:
            d["endBinding"] = self.endBinding
        if self.boundElements:
            d["boundElements"] = self.boundElements
        return d


def shape_to_excalidraw_type(shape: str) -> str:
    """Translate tool-agnostic shape name to Excalidraw type."""
    mapping = {
        "rectangle": "rectangle",
        "rounded": "rectangle",
        "ellipse": "ellipse",
        "circle": "ellipse",
        "diamond": "diamond",
        "rhombus": "diamond",
    }
    return mapping.get(shape, "rectangle")


def translate_ops_to_excalidraw(ops_list: list[dict]) -> tuple[list[ExcalidrawElement], list[str], list[str]]:
    """LAYER 3: Translate tool-agnostic ops to Excalidraw elements.

    To swap tools, replace this function with translate_ops_to_tldraw, etc.
    The LLM layer (above) remains unchanged.
    """
    elements: dict[str, ExcalidrawElement] = {}
    arrows: dict[str, dict] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for i, op in enumerate(ops_list):
        op_type = op.get("op")
        try:
            if op_type == "create_node":
                elem_id = op["id"]
                if elem_id in elements:
                    errors.append(f"[{i}] create_node id {elem_id!r} already exists")
                    continue
                shape = op.get("shape", "rectangle")
                w = op.get("w", 100)
                h = op.get("h", 60)
                elements[elem_id] = ExcalidrawElement(
                    id=elem_id,
                    type=shape_to_excalidraw_type(shape),
                    x=op.get("x", 0),
                    y=op.get("y", 0),
                    width=w,
                    height=h,
                    text=op.get("label", ""),
                    roundness="adaptive" if shape == "rounded" else "adaptive",
                )
            elif op_type == "set_shape":
                elem_id = op["id"]
                if elem_id not in elements:
                    errors.append(f"[{i}] set_shape id {elem_id!r} does not exist")
                    continue
                elements[elem_id].type = shape_to_excalidraw_type(op["shape"])
            elif op_type == "set_style":
                elem_id = op["id"]
                if elem_id not in elements:
                    errors.append(f"[{i}] set_style id {elem_id!r} does not exist")
                    continue
                style = op.get("style", {})
                if "color" in style:
                    elements[elem_id].strokeColor = style["color"]
                if "fill" in style:
                    elements[elem_id].fillStyle = style["fill"]
                if "strokeWidth" in style:
                    elements[elem_id].strokeWidth = style["strokeWidth"]
            elif op_type == "rename":
                elem_id = op["id"]
                if elem_id not in elements:
                    errors.append(f"[{i}] rename id {elem_id!r} does not exist")
                    continue
                elements[elem_id].text = op["label"]
            elif op_type == "move":
                elem_id = op["id"]
                if elem_id not in elements:
                    errors.append(f"[{i}] move id {elem_id!r} does not exist")
                    continue
                elements[elem_id].x = op["x"]
                elements[elem_id].y = op["y"]
            elif op_type == "connect":
                arrow_id = op["id"]
                from_id = op["from"]
                to_id = op["to"]
                if from_id not in elements:
                    errors.append(f"[{i}] connect from {from_id!r} does not exist")
                    continue
                if to_id not in elements:
                    errors.append(f"[{i}] connect to {to_id!r} does not exist")
                    continue
                arrows[arrow_id] = {"from": from_id, "to": to_id, "label": op.get("label", "")}
            elif op_type == "delete":
                elem_id = op["id"]
                if elem_id not in elements:
                    warnings.append(f"[{i}] delete id {elem_id!r} does not exist (skipped)")
                    continue
                elements[elem_id].isDeleted = True
            elif op_type == "add_annotation":
                elem_id = op["id"]
                if elem_id in elements:
                    errors.append(f"[{i}] add_annotation id {elem_id!r} already exists")
                    continue
                kind = op.get("kind", "text")
                elem_type = "text" if kind == "text" else "rectangle"
                elements[elem_id] = ExcalidrawElement(
                    id=elem_id,
                    type=elem_type,
                    x=op.get("x", 0),
                    y=op.get("y", 0),
                    width=op.get("w", 100),
                    height=op.get("h", 30),
                    text=op.get("text", ""),
                )
        except KeyError as e:
            errors.append(f"[{i}] {op_type} missing field: {e}")

    for arrow_id, arrow_info in arrows.items():
        from_elem = elements.get(arrow_info["from"])
        to_elem = elements.get(arrow_info["to"])
        if not from_elem or not to_elem:
            continue
        arrow = ExcalidrawElement(
            id=arrow_id,
            type="arrow",
            x=from_elem.x + from_elem.width / 2,
            y=from_elem.y + from_elem.height / 2,
            width=0,
            height=0,
            startBinding={"elementId": arrow_info["from"], "focus": 0.5, "gap": 10},
            endBinding={"elementId": arrow_info["to"], "focus": 0.5, "gap": 10},
        )
        if arrow_info["label"]:
            arrow.text = arrow_info["label"]
        elements[arrow_id] = arrow
        if not from_elem.boundElements:
            from_elem.boundElements = []
        from_elem.boundElements.append({"type": "arrow", "id": arrow_id})
        if not to_elem.boundElements:
            to_elem.boundElements = []
        to_elem.boundElements.append({"type": "arrow", "id": arrow_id})

    return list(elements.values()), errors, warnings
