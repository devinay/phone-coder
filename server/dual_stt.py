"""Dual-mode STT: Deepgram (English) + Whisper (non-English), switchable at runtime.

Pipeline position:
    transport.input() → WhisperSideCar → DeepgramSTTService → LanguageGate → user_aggregator

WhisperSideCar: taps audio, runs its own VAD, calls Whisper on utterance end in non-English mode.
LanguageGate: drops Deepgram TranscriptionFrames in non-English mode; always passes WhisperTranscriptionFrame.
"""

import asyncio
import io
import time
import wave
from dataclasses import dataclass

import openai
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import AudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor


LANGUAGE_CODES: dict[str, str] = {
    "kannada": "kn",
    "telugu":  "te",
    "tamil":   "ta",
    "hindi":   "hi",
}


@dataclass
class WhisperTranscriptionFrame(TranscriptionFrame):
    """TranscriptionFrame produced by Whisper. Passes through LanguageGate in any mode."""
    pass


class WhisperSideCar(FrameProcessor):
    """Sits before Deepgram in the pipeline.

    - Always passes every frame through unchanged (Deepgram gets audio regardless).
    - In non-English mode: accumulates audio via its own VAD; on utterance end sends
      the audio chunk to Whisper and pushes a WhisperTranscriptionFrame downstream.
    - In English mode: does nothing with audio.
    """

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key
        self._language = "english"
        self._language_code: str | None = None
        self._vad = SileroVADAnalyzer()
        self._buffer: list[bytes] = []
        self._sample_rate = 16000
        self._num_channels = 1

    def set_language(self, language: str):
        self._language = language.lower()
        self._language_code = LANGUAGE_CODES.get(self._language)
        self._buffer.clear()
        logger.info(f"WhisperSideCar: language={self._language!r} whisper_code={self._language_code!r}")

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame) and self._language != "english":
            self._sample_rate = frame.sample_rate
            self._num_channels = frame.num_channels

            vad_state = self._vad.analyze_audio(frame.audio)

            if vad_state in (VADState.STARTING, VADState.SPEAKING):
                self._buffer.append(frame.audio)
            elif vad_state == VADState.STOPPING and self._buffer:
                audio_data = b"".join(self._buffer)
                self._buffer.clear()
                asyncio.create_task(self._transcribe(audio_data))

        await self.push_frame(frame, direction)

    async def _transcribe(self, audio_data: bytes):
        try:
            wav_bytes = _encode_wav(audio_data, self._sample_rate, self._num_channels)
            client = openai.AsyncOpenAI(api_key=self._api_key)
            kwargs: dict = {"model": "whisper-1", "file": ("audio.wav", wav_bytes, "audio/wav")}
            if self._language_code:
                kwargs["language"] = self._language_code
            response = await client.audio.transcriptions.create(**kwargs)
            text = response.text.strip()
            if text:
                logger.info(f"Whisper [{self._language_code}]: {text}")
                await self.push_frame(WhisperTranscriptionFrame(
                    text=text,
                    user_id="user",
                    timestamp=str(time.time()),
                ))
        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")


class LanguageGate(FrameProcessor):
    """Sits after Deepgram in the pipeline.

    - English mode: all frames pass through.
    - Non-English mode: drops TranscriptionFrame and InterimTranscriptionFrame from Deepgram.
      WhisperTranscriptionFrame (subclass) always passes through so user_aggregator receives it.
    """

    def __init__(self):
        super().__init__()
        self._english_mode = True

    def set_language(self, language: str):
        self._english_mode = language.lower() == "english"
        logger.info(f"LanguageGate: english_mode={self._english_mode}")

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not self._english_mode:
            is_deepgram_transcription = (
                isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame))
                and not isinstance(frame, WhisperTranscriptionFrame)
            )
            if is_deepgram_transcription:
                return  # drop silently

        await self.push_frame(frame, direction)


def _encode_wav(pcm_bytes: bytes, sample_rate: int, num_channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()
