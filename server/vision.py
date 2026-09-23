"""Layer 1: ask a model to read a sketch.

Deliberately a **side call**, not a conversation turn. The sketch-to-ops request
is one-shot — an image, an intent, ops back — with no history before it and none
after, so routing it through the pipeline's conversation model would buy nothing
and cost a great deal: switching models across vendors resets the conversation
context (vendors encode tool calls differently), and the whole point of a
separate vision model is to change it freely while comparing.

So the conversation keeps its model and its context, and this picks its own.
That also makes an honest A/B possible — same session, same sketch, swap only
the model under test.

Resolution order, most specific first:
    a runtime override (voice, or the diagram-mode picker)
    VISION_MODEL in .env
    whatever the conversation is using
"""

import base64
import os
from dataclasses import dataclass

from loguru import logger

from sketch import SYSTEM_OPS_GENERATION


@dataclass
class VisionChoice:
    """Which model will read the sketch, and why that one."""

    model: str
    source: str  # "voice", "VISION_MODEL", or "conversation"

    def describe(self) -> str:
        return f"{self.model} ({self.source})"


class VisionSettings:
    """The vision model for this session.

    Lives in memory beside the watch instructions and for the same reason: it is
    an experiment, not a decision. A restart returns to VISION_MODEL, so "which
    model produced this?" stays answerable after the fact. When an answer is
    settled, it goes in .env.
    """

    def __init__(self, conversation_model: str = ""):
        self._override: str | None = None
        self._conversation = conversation_model

    def set_conversation_model(self, model: str) -> None:
        """Follow the dropdown, so the fallback tracks what is actually running."""
        self._conversation = model

    def choose(self, model: str) -> VisionChoice:
        self._override = model
        logger.info(f"[VISION] model set to {model} for this session")
        return self.current

    def clear(self) -> VisionChoice:
        self._override = None
        logger.info("[VISION] override cleared; following configuration again")
        return self.current

    @property
    def current(self) -> VisionChoice:
        if self._override:
            return VisionChoice(self._override, "set by voice this session")
        configured = os.getenv("VISION_MODEL", "").strip()
        if configured:
            return VisionChoice(configured, "from VISION_MODEL")
        return VisionChoice(self._conversation, "following the conversation model")


def vision_capable(catalog: dict, model_id: str) -> bool | None:
    """Tri-state, matching the catalogue.

    True and False are claims; None means no source said — which is the honest
    answer for every OpenAI model whose specs are unpublished. Callers must not
    read None as False and refuse the model; it may work perfectly.
    """
    info = catalog.get(model_id)
    return None if info is None else info.vision


def vision_models(catalog: dict) -> list[str]:
    """Ids worth offering for sketching: known-capable first, unknown after.

    Models known *not* to do vision are excluded outright — offering one is
    offering a guaranteed failure.
    """
    known = [i for i, m in catalog.items() if m.vision is True]
    unknown = [i for i, m in catalog.items() if m.vision is None]
    return known + unknown


async def read_sketch(png: bytes, intent: str, model: str, provider: str, api_key: str) -> str:
    """Send the drawing and the words; return whatever the model replied.

    Returns raw text rather than parsed ops — validation belongs to ``sketch``,
    which is the layer that knows what a valid op is.
    """
    b64 = base64.b64encode(png).decode()
    user_text = f"User intent: {intent}\nGenerate the ops. JSON only."

    if provider == "anthropic":
        from anthropic import AsyncAnthropic

        resp = await AsyncAnthropic(api_key=api_key).messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_OPS_GENERATION,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": user_text},
            ]}],
        )
        return resp.content[0].text

    # Everything else speaks the OpenAI shape, including Ollama.
    from openai import AsyncOpenAI

    kwargs = {"api_key": api_key}
    if provider == "ollama":
        kwargs = {"api_key": "local",
                  "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")}
    resp = await AsyncOpenAI(**kwargs).chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=[
            {"role": "system", "content": SYSTEM_OPS_GENERATION},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ],
    )
    return resp.choices[0].message.content
