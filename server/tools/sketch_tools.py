"""Tools for turning a drawing plus speech into a diagram."""

import json

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from sketch import translate_ops_to_excalidraw, validate_ops
from vision import read_sketch, vision_capable, vision_models


def create_sketch_tools(vision_settings, catalog, canvas, api_keys):
    """Sketch tools.

    ``canvas`` supplies the current drawing and receives the result — it is the
    seam to the UI, kept behind an interface so the pipeline does not depend on
    how the browser stores a scene.
    """

    async def set_vision_model(params: FunctionCallParams, model: str = ""):
        """Choose which model reads your sketches.

        Changing this does NOT change the model you are talking to, and does not
        reset the conversation — the sketch call is a separate one-shot request.
        That is what makes it safe to switch mid-session while comparing.

        Args:
            model: A model id from list_vision_models. Leave empty to go back to
                the configured default.
        """
        if not model:
            choice = vision_settings.clear()
            await params.result_callback(f"Vision model back to {choice.describe()}.")
            return
        if model not in catalog:
            offered = ", ".join(vision_models(catalog))
            await params.result_callback(
                f"{model!r} is not in the catalogue. Available: {offered}"
            )
            return
        if vision_capable(catalog, model) is False:
            await params.result_callback(
                f"{model} cannot read images, so it cannot work on sketches. "
                f"Try one of: {', '.join(vision_models(catalog))}"
            )
            return
        choice = vision_settings.choose(model)
        unknown = vision_capable(catalog, model) is None
        caveat = " Nobody publishes whether it does vision, so this may fail." if unknown else ""
        await params.result_callback(f"Sketches will now be read by {choice.describe()}.{caveat}")

    async def list_vision_models(params: FunctionCallParams):
        """List the models that can read a sketch, with prices."""
        lines = []
        for i in vision_models(catalog):
            m = catalog[i]
            lines.append(f"{i} — {m.price_label}, {m.vision_label}. {m.blurb}".strip())
        current = vision_settings.current
        result = "\n".join(lines) + f"\n\nCurrently using: {current.describe()}"
        await params.result_callback(result)

    async def sketch_to_diagram(params: FunctionCallParams, intent: str):
        """Read what is drawn on the canvas and turn it into a diagram.

        The drawing carries the shape of things — how many boxes, what connects
        to what. Your words carry what they *are*. Neither is enough alone: a
        model can see three rectangles but cannot know one is a database.

        Args:
            intent: What the user said the sketch means, in their words — e.g.
                "this is an auth flow: the user hits the API, the API reads the
                user database". Describing the picture back ("three boxes with
                arrows") is useless; the model can already see that.
        """
        png = await canvas.png()
        if not png:
            await params.result_callback(
                "There is nothing on the canvas yet — ask the user to draw something first."
            )
            return

        choice = vision_settings.current
        provider = getattr(catalog.get(choice.model), "provider", "openai")
        key = api_keys.get(provider, "")
        logger.info(f"[SKETCH] reading canvas with {choice.describe()} ({provider})")

        try:
            raw = await read_sketch(png, intent, choice.model, provider, key)
        except Exception as e:
            logger.error(f"[SKETCH] {choice.model} failed: {type(e).__name__}: {e}")
            await params.result_callback(
                f"{choice.model} could not read the sketch ({type(e).__name__}). "
                "Try a different vision model with set_vision_model."
            )
            return

        ok, problems = validate_ops(raw)
        if not ok:
            # Reported rather than retried: which model produced bad ops is the
            # thing being compared, so silently papering over it destroys the
            # signal the user is testing for.
            logger.warning(f"[SKETCH] {choice.model} produced invalid ops: {problems}")
            await params.result_callback(
                f"{choice.model} returned ops that did not validate: {'; '.join(problems[:3])}"
            )
            return

        commands = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]).get("commands", [])
        elements, errors, warnings = translate_ops_to_excalidraw(commands)
        if errors:
            await params.result_callback(f"Could not build the diagram: {'; '.join(errors[:3])}")
            return

        await canvas.set_scene([e.to_dict() for e in elements])
        note = f" ({len(warnings)} warning(s))" if warnings else ""
        await params.result_callback(
            f"Drew {len(elements)} elements from your sketch using {choice.model}{note}."
        )

    return {
        "set_vision_model": set_vision_model,
        "list_vision_models": list_vision_models,
        "sketch_to_diagram": sketch_to_diagram,
    }
