"""Phase 1 — DocWriter: accumulates utterances and produces document.md + transcript.md.

DocWriter never sees provider-specific frame structure. Callers convert raw frames
into AttributedUtterance before passing them here.
"""

import time
from dataclasses import dataclass, field


@dataclass
class AttributedUtterance:
    text: str
    timestamp: float
    speaker_id: str | None       # Deepgram speaker ID, e.g. "0", "1"
    confidence: float | None     # Attribution confidence, if available
    fallback_label: str = "Speaker Unknown"
    raw_payload: dict = field(default_factory=dict)

    def display_name(self, speaker_map: dict[str, str]) -> str:
        """Resolve speaker_id to a human-readable name via speaker_map."""
        if self.speaker_id is not None:
            return speaker_map.get(str(self.speaker_id), f"Speaker {self.speaker_id}")
        return self.fallback_label


class DocWriter:
    """Accumulates AttributedUtterances and produces structured markdown output.

    Produces two files:
    - document.md: ## Main Content (placeholder for Phase 1) + ## Transcript
    - transcript.md: raw chronological utterances with timestamps
    """

    def __init__(self, title: str = "Session"):
        self._title = title
        self._utterances: list[AttributedUtterance] = []
        self._speaker_map: dict[str, str] = {}  # speaker_id → display name

    def set_speaker_map(self, speaker_map: dict[str, str]) -> None:
        self._speaker_map = dict(speaker_map)

    def add_utterance(self, utterance: AttributedUtterance) -> None:
        self._utterances.append(utterance)

    def _group_by_speaker(self) -> dict[str, list[AttributedUtterance]]:
        """Group utterances by resolved display name, preserving insertion order."""
        groups: dict[str, list[AttributedUtterance]] = {}
        for u in self._utterances:
            name = u.display_name(self._speaker_map)
            groups.setdefault(name, []).append(u)
        return groups

    def render_document_md(self) -> str:
        """Render document.md with Main Content placeholder and Transcript section."""
        lines: list[str] = [f"# {self._title}", ""]

        lines += ["## Main Content", ""]
        if not self._utterances:
            lines += ["*(No content yet.)*", ""]
        else:
            lines += ["*(AI synthesis will be added here.)*", ""]

        lines += ["---", "", "## Transcript", ""]

        if not self._utterances:
            lines += ["*(No utterances recorded.)*", ""]
        else:
            groups = self._group_by_speaker()
            for speaker_name, utterances in groups.items():
                lines.append(f"### {speaker_name}")
                lines.append("")
                for u in utterances:
                    lines.append(f"- {u.text}")
                lines.append("")

        return "\n".join(lines)

    def render_transcript_md(self) -> str:
        """Render transcript.md: raw chronological utterances with timestamps."""
        if not self._utterances:
            return "*(No utterances recorded.)*\n"

        lines: list[str] = ["# Raw Transcript", ""]
        for u in self._utterances:
            ts = time.strftime("%H:%M:%S", time.localtime(u.timestamp))
            name = u.display_name(self._speaker_map)
            conf = f" (conf={u.confidence:.2f})" if u.confidence is not None else ""
            lines.append(f"**[{ts}] {name}{conf}**: {u.text}")
        lines.append("")
        return "\n".join(lines)

    def utterance_count(self) -> int:
        return len(self._utterances)
