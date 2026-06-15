"""Image search and embedding tools for diagrams."""

import asyncio
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

if TYPE_CHECKING:
    from diagram_focus import DiagramFocusStateMachine
    from doc_state import DocStateMachine


def _ddg_image_search(query: str, max_results: int = 5) -> list[dict]:
    """Synchronous DuckDuckGo image search. Run in a thread pool."""
    from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        return ddgs.images(query, max_results=max_results, type_image="transparent") or \
               ddgs.images(query, max_results=max_results)


async def _download_image(
    session: aiohttp.ClientSession,
    url: str,
    dest_stem: Path,
    n: int,
    max_bytes: int = 2 * 1024 * 1024,
    timeout: int = 8,
) -> Path:
    """Download one image, infer extension, return Path on success."""
    import mimetypes
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            ext = mimetypes.guess_extension(ct) or ".jpg"
            ext = ext.replace(".jpe", ".jpg")
            dest = dest_stem.with_suffix(ext)
            data = await resp.read()
            if len(data) > max_bytes:
                raise ValueError("Image too large")
            dest.write_bytes(data)
            return dest
    except Exception as e:
        raise RuntimeError(f"Image {n} download failed: {e}") from e


def _embed_image_in_node(
    source: str, element_id: str, img_url: str, width: int
) -> tuple[str, bool]:
    """Rewrite a Mermaid flowchart node label to embed an image tag.

    Handles common node shapes: [text], (text), {text}, [(text)], ((text)), >text].
    Returns (new_source, found).
    """
    img_tag = f'<img src="{img_url}" width="{width}"/>'
    # Match: element_id optionally followed by whitespace, then opening bracket(s), content, closing bracket(s)
    pattern = re.compile(
        rf'(?<![A-Za-z0-9_])({re.escape(element_id)}\s*)(\[{{1,2}}|\({{1,3}}|\{{|\>)([^\]\)\}}>]*)(\]{{1,2}}|\){{1,3}}|\}}|\])',
        re.DOTALL,
    )
    result, count = pattern.subn(
        lambda m: f'{m.group(1)}{m.group(2)}"{img_tag}"{m.group(4)}',
        source,
        count=1,
    )
    return result, count > 0


def create_image_tools(
    diagram_focus_sm: "DiagramFocusStateMachine",
    doc_sm: "DocStateMachine",
    task: Any,
    session_id: str,
    image_tmp_root: Path,
) -> dict[str, Any]:
    """Factory function to create image search and embedding tools.

    Args:
        diagram_focus_sm: DiagramFocusStateMachine instance
        doc_sm: DocStateMachine instance
        task: PipelineTask for queueing frames
        session_id: Session ID for image temp directory
        image_tmp_root: Root path for temporary images

    Returns a dictionary of tool functions ready for registration.
    """
    from pipecat.processors.frameworks.rtvi.models import ServerMessage
    from pipecat.frames.frames import OutputTransportMessageUrgentFrame

    async def search_images(params: FunctionCallParams, query: str, element_id: str):
        """Search for images to embed in the current diagram node.

        Call this when the user asks to replace a diagram element with an image.
        Downloads up to 5 images locally and sends them to the browser as thumbnails.

        Args:
            query: Search query, e.g. "AWS S3 bucket icon transparent PNG"
            element_id: The Mermaid node ID to replace, e.g. "DB" or "User"
        """
        session = doc_sm.session
        if session.state.value != "diagram_focus":
            await params.result_callback("INVALID_STATE: Not in diagram focus mode.")
            return
        if diagram_focus_sm.session.in_image_search:
            await params.result_callback("ALREADY_ACTIVE: Image search already in progress. Say cancel to start over.")
            return

        search_dir = image_tmp_root / uuid.uuid4().hex[:8]
        search_dir.mkdir(parents=True, exist_ok=True)
        diagram_focus_sm.begin_image_search(element_id, search_dir)

        # Run DuckDuckGo search in thread pool (synchronous library)
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: _ddg_image_search(query, 5))
        except Exception as e:
            diagram_focus_sm.cancel_image_search()
            await params.result_callback(f"SEARCH_ERROR: {e}")
            return

        if not results:
            diagram_focus_sm.cancel_image_search()
            await params.result_callback("NO_RESULTS: No images found. Try a different description.")
            return

        # Download all images concurrently
        downloaded: list[Path] = []
        async with aiohttp.ClientSession() as http:
            tasks = [_download_image(http, r["image"], search_dir / f"{i+1}", i+1)
                     for i, r in enumerate(results)]
            paths = await asyncio.gather(*tasks, return_exceptions=True)

        for p in paths:
            if isinstance(p, Path):
                downloaded.append(p)

        if not downloaded:
            diagram_focus_sm.cancel_image_search()
            await params.result_callback("DOWNLOAD_ERROR: Could not download any images. Try again.")
            return

        diagram_focus_sm.set_image_results(downloaded)

        # Tell browser to show thumbnails
        thumb_list = [
            {"n": i + 1, "url": f"/api/images/{session_id}/{p.name}"}
            for i, p in enumerate(downloaded)
        ]
        msg = ServerMessage(data={
            "type": "image-search-results",
            "element_id": element_id,
            "images": thumb_list,
        })
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info(f"[IMAGE] Search '{query}' → {len(downloaded)} images for element '{element_id}'")
        await params.result_callback(
            f"OK: Found {len(downloaded)} images. Thumbnails shown to user. "
            f"Ask: 'Which image would you like — 1 through {len(downloaded)}? Say cancel to go back.'"
        )

    async def select_image(params: FunctionCallParams, number: int):
        """Select one of the image search results to embed in the diagram.

        Call this when the user names a number. Deletes the other images,
        embeds the chosen one in the Mermaid node, and enters sizing state.

        Args:
            number: 1-based index of the chosen image (1–5)
        """
        from git_storage import atomic_write

        fs = diagram_focus_sm.session
        if fs.image_search_state != "selecting":
            await params.result_callback("INVALID_STATE: Not in image selection state.")
            return

        idx = number - 1
        if idx < 0 or idx >= len(fs.image_paths):
            await params.result_callback(f"INVALID: Please pick a number between 1 and {len(fs.image_paths)}.")
            return

        chosen = fs.image_paths[idx]

        # Delete the unchosen temp images immediately
        for i, p in enumerate(fs.image_paths):
            if i != idx:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

        # Copy chosen image to permanent version directory
        doc_session = doc_sm.session
        vi = doc_session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info.")
            return

        # Validate the target diagram block and node BEFORE forking or copying,
        # so a failed embed never creates an orphan version.
        from helpers import _extract_diagram_source, _update_diagram_in_doc

        base_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
        if _extract_diagram_source(base_doc, fs.diagram_id) is None:
            await params.result_callback(
                f"ID_NOT_FOUND: Could not locate diagram '{fs.diagram_id}' in document.md."
            )
            return
        _, node_found = _embed_image_in_node(
            fs.current_source or "", fs.image_search_element_id, "about:blank", fs.current_image_width
        )
        if not node_found:
            await params.result_callback(
                f"NODE_NOT_FOUND: Could not find node '{fs.image_search_element_id}' in Mermaid source. "
                f"Try update_diagram manually."
            )
            return

        # All checks passed — write the image into the project's images/ dir.
        vi = doc_session.version_info

        images_dir = vi.project_dir / "images"
        images_dir.mkdir(exist_ok=True)
        dest_name = f"{fs.diagram_id}-{fs.image_search_element_id}{chosen.suffix}"
        dest = images_dir / dest_name
        import shutil
        shutil.copy2(chosen, dest)

        # Transition state machine
        diagram_focus_sm.select_image(dest)

        # Embed image in Mermaid source (URL points at the project's images dir)
        img_url = f"/api/docs/{doc_session.project_slug}/images/{dest_name}"
        new_source, _ = _embed_image_in_node(
            fs.current_source or "", fs.image_search_element_id, img_url, fs.current_image_width
        )

        current_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
        new_doc, _ = _update_diagram_in_doc(current_doc, fs.diagram_id, new_source)

        atomic_write(vi.document_md, new_doc)
        from .doc_tools import _mark_doc_session_edited
        _mark_doc_session_edited(doc_session)
        diagram_focus_sm.session.current_source = new_source

        # Send live re-render
        frames = [
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
            ),
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={
                    "type": "diagram-focus-updated",
                    "diagram_id": fs.diagram_id,
                    "mermaid_source": new_source,
                }).model_dump()
            ),
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "image-search-clear"}).model_dump()
            ),
        ]
        await task.queue_frames(frames)
        logger.info(f"[IMAGE] Selected image {number} → {dest_name}, width={fs.current_image_width}px")
        await params.result_callback(
            f"OK: Image embedded at {fs.current_image_width}px wide. "
            f"Ask: 'How does that look? Say bigger, smaller, or done.'"
        )

    async def resize_image(params: FunctionCallParams, direction: str):
        """Adjust the width of the embedded image. Call during sizing state.

        Args:
            direction: 'bigger' or 'smaller'
        """
        from git_storage import atomic_write
        from helpers import _update_diagram_in_doc
        from .doc_tools import _mark_doc_session_edited

        fs = diagram_focus_sm.session
        if fs.image_search_state != "sizing":
            await params.result_callback("INVALID_STATE: Not in image sizing state.")
            return

        if direction.lower() in ("bigger", "larger", "up"):
            new_width = diagram_focus_sm.width_bigger()
        elif direction.lower() in ("smaller", "smaller", "down"):
            new_width = diagram_focus_sm.width_smaller()
        else:
            await params.result_callback("INVALID: Say 'bigger' or 'smaller'.")
            return

        doc_session = doc_sm.session
        vi = doc_session.version_info
        if vi is None:
            await params.result_callback("ERROR: No version info.")
            return
        if fs.selected_image_path is None:
            await params.result_callback("ERROR: No selected image to resize.")
            return

        img_url = f"/api/docs/{doc_session.project_slug}/images/{fs.selected_image_path.name}"
        new_source, _ = _embed_image_in_node(
            fs.current_source or "", fs.image_search_element_id, img_url, new_width
        )
        current_doc = vi.document_md.read_text() if vi.document_md.exists() else ""
        new_doc, doc_found = _update_diagram_in_doc(current_doc, fs.diagram_id, new_source)
        if not doc_found:
            await params.result_callback(
                f"ID_NOT_FOUND: Could not locate diagram '{fs.diagram_id}' in document.md."
            )
            return

        atomic_write(vi.document_md, new_doc)
        _mark_doc_session_edited(doc_session)
        diagram_focus_sm.session.current_source = new_source

        frames = [
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={"type": "doc-content-updated", "content": new_doc}).model_dump()
            ),
            OutputTransportMessageUrgentFrame(
                message=ServerMessage(data={
                    "type": "diagram-focus-updated",
                    "diagram_id": fs.diagram_id,
                    "mermaid_source": new_source,
                }).model_dump()
            ),
        ]
        await task.queue_frames(frames)
        logger.info(f"[IMAGE] Resized to {new_width}px")
        await params.result_callback(f"OK: Image is now {new_width}px wide. Bigger, smaller, or done?")

    async def cancel_image_search(params: FunctionCallParams):
        """Cancel image search and return to diagram editing without embedding anything."""
        if not diagram_focus_sm.session.in_image_search:
            await params.result_callback("NOT_ACTIVE: No image search in progress.")
            return
        diagram_focus_sm.cancel_image_search()
        msg = ServerMessage(data={"type": "image-search-clear"})
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg.model_dump())])
        logger.info("[IMAGE] Search cancelled, temp files deleted")
        await params.result_callback("OK: Image search cancelled. Back to diagram editing.")

    async def done_image(params: FunctionCallParams):
        """Confirm the embedded image size and exit image search state.

        Call when the user says 'done', 'looks good', 'that's fine', etc.
        """
        if diagram_focus_sm.session.image_search_state != "sizing":
            await params.result_callback("INVALID_STATE: Not in image sizing state.")
            return
        diagram_focus_sm.complete_image_search()
        logger.info("[IMAGE] Image embedding confirmed, returning to diagram editing")
        await params.result_callback("OK: Image confirmed. Back to diagram editing. Any other changes?")

    return {
        "search_images": search_images,
        "select_image": select_image,
        "resize_image": resize_image,
        "cancel_image_search": cancel_image_search,
        "done_image": done_image,
    }
