import asyncio
import re
from pipecat.frames.frames import Frame,TranscriptionFrame,InterimTranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor,FrameDirection
from processor.frames import UserTurnFrame

_CONTINUATION_START=re.compile(r"^(today|tomorrow|with|for|and|at|on|in|to|pm|am|\d)",re.I)
_CONTINUATION_END=re.compile(r"(?:\b(on|with|for|and|at|to|in|today|tomorrow)\s*)$",re.I)

class TurnManager(FrameProcessor):
    def __init__(self,conversation_id=None,user_id=None,timeout=0.18):
        super().__init__()
        self.timeout=timeout
        self.current_text=""
        self.timer=None

    async def process_frame(self,frame:Frame,direction:FrameDirection):
        await super().process_frame(frame,direction)
        if isinstance(frame,InterimTranscriptionFrame):
            await self.push_frame(frame,direction)
            return
        if not isinstance(frame,TranscriptionFrame):
            await self.push_frame(frame,direction)
            return
        text=(frame.text or "").strip()
        if not text:
            return
            
        # Filter out random noise blips that only contain punctuation (like "." or "-")
        if not any(c.isalnum() for c in text):
            return
        # Sarvam can split one spoken sentence into several finals when VAD ends early.
        # Keep only incomplete/continuation fragments for a very short merge window.
        if self.current_text:
            if _CONTINUATION_START.match(text) or _CONTINUATION_END.search(self.current_text):
                self.current_text = self._merge(self.current_text, text)
                self._restart_timer()
                return
            else:
                text = self._merge(self.current_text, text)
                self.current_text = ""
                
        if _CONTINUATION_END.search(text):
            self.current_text = text
            self._restart_timer()
            return
            
        await self._emit(text)

    def _restart_timer(self):
        if self.timer and not self.timer.done(): self.timer.cancel()
        self.timer=asyncio.create_task(self._flush())

    async def _flush(self):
        try: await asyncio.sleep(self.timeout)
        except asyncio.CancelledError: return
        text=self.current_text.strip(); self.current_text=""
        if text: await self._emit(text)

    async def _emit(self,text):
        print(f"\nUSER TURN: {text}")
        await self.push_frame(UserTurnFrame(text=text),FrameDirection.DOWNSTREAM)

    @staticmethod
    def _merge(old,new):
        if not old: return new
        if not new: return old
        a,b=old.lower(),new.lower()
        if b==a or b.startswith(a): return new if len(new)>=len(old) else old
        if a.startswith(b) or a.endswith(b): return old
        return f"{old} {new}"