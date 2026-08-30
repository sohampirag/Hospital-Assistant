import asyncio
import time
import uuid

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

from processor.frames import UserTurnFrame
from processor.hospital_handler import HospitalHandler


class GroqProcessor(FrameProcessor):
    def __init__(self, llm, conversation_id: uuid.UUID, user_id: uuid.UUID):
        super().__init__()
        self.llm = llm
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.handler = HospitalHandler()
        self._generation_task = None
        self.greeted = False

    async def warmup(self):
        pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, UserTurnFrame):
            await self.push_frame(frame, direction)
            return

        user_text = (frame.text or "").strip()
        if not user_text:
            return

        request_start = time.perf_counter()
        print(f"[USER] {user_text}")

        if not self.greeted:
            self.greeted = True
            assistant_text = "Hello, I'm Jeevan Mishra, your hospital assistant. How can I help you today?"
            print(f"[PERF] RESPONSE READY: {time.perf_counter()-request_start:.3f}s")
            await self._speak_local_result(assistant_text, request_start)
            return

        if self._generation_task and not self._generation_task.done():
            self._generation_task.cancel()

        self._generation_task = asyncio.create_task(
            self._generate(
                user_text,
                request_start
            )
        )

        # Make _generation_task a background task; don't await it here so we don't block process_frame
        # If a new turn arrives while this runs, the next process_frame will cancel it.

    async def _speak_local_result(self, assistant_text, request_start):
        first_tts_time = time.perf_counter() - request_start
        print(f"[PERF] TOTAL: {first_tts_time:.3f}s")
        print(f"[ASSISTANT] {assistant_text}")

        await self.push_frame(
            LLMFullResponseStartFrame(),
            FrameDirection.DOWNSTREAM,
        )

        await self.push_frame(
            LLMTextFrame(text=assistant_text),
            FrameDirection.DOWNSTREAM,
        )

        await self.push_frame(
            LLMFullResponseEndFrame(),
            FrameDirection.DOWNSTREAM,
        )

    async def _generate(self, user_text, request_start):
        try:
            # 1. Fast LLM Intent Extraction
            groq_start = time.perf_counter()
            
            # Extract intent directly as an async call to not block event loop
            intent_data = await self.llm.extract_intent(
                user_text, 
                self.handler.state
            )
            
            print(f"[PERF] GROQ TTFT: {time.perf_counter()-groq_start:.3f}s")
            print(f"[INTENT EXTRACTED] {intent_data}")
            
            # 2. Local Python execution (DB access + String formatting)
            assistant_text = await asyncio.to_thread(self.handler.process_intent, intent_data, user_text)
            
            should_end = False
            if "[END_CALL]" in assistant_text:
                assistant_text = assistant_text.replace("[END_CALL]", "").strip()
                should_end = True
                
            # 3. Speak
            await self._speak_local_result(assistant_text, request_start)
            
            if should_end:
                from pipecat.frames.frames import EndFrame
                await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
                
        except asyncio.CancelledError:
            return
        except Exception as exc:
            print(f"[PROCESSOR ERROR] {exc}")
