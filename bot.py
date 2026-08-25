import asyncio
import os
import uuid

from dotenv import load_dotenv
from pipecat.frames.frames import Frame, TranscriptionFrame, InterimTranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

from processor.hospital_db import warmup as warmup_db
from processor.llm import GroqLLM
from processor.llm_processor import GroqProcessor
from processor.manager import TurnManager

load_dotenv(override=True)


class TranscriptionLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            print(f"FINAL: {frame.text}")
        elif isinstance(frame, InterimTranscriptionFrame):
            print(f"INTERIM: {frame.text}")
        await self.push_frame(frame, direction)


transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    )
}


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)

    sarvam_api_key = os.getenv("SARVAM_API_KEY")
    if not sarvam_api_key:
        raise ValueError("SARVAM_API_KEY is missing from .env")
    if not os.getenv("GROQ_API_KEY"):
        raise ValueError("GROQ_API_KEY is missing from .env")
    if not os.getenv("NEON_DATABASE_URL"):
        raise ValueError("NEON_DATABASE_URL is missing from .env")

    conversation_id = uuid.uuid4()
    user_id = uuid.uuid4() # We use phone number for identity in Jeevan

    print(f"Conversation ID: {conversation_id}")
    print(f"Agent User ID: {user_id} (Dummy for hospital assistant)")

    stt = SarvamSTTService(
        api_key=sarvam_api_key,
        model="saaras:v3",
        mode="transcribe",
        settings=SarvamSTTService.Settings(
            language="en-IN",
            vad_signals=True,
            high_vad_sensitivity=True,
            positive_speech_threshold=0.70,
            negative_speech_threshold=0.50,
            min_speech_frames=2,
            first_turn_min_speech_frames=6,
            negative_frames_count=5,
            negative_frames_window=8,
            interrupt_min_speech_frames=4,
            pre_speech_pad_frames=9,
        ),
    )

    # Keep the WebSocket TTS configuration simple and supported.
    # Sentence-sized LLM frames are emitted by GroqProcessor.
    tts = SarvamTTSService(
        api_key=sarvam_api_key,
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice="shubh",
            language="en-IN",
            min_buffer_size=30,
            max_chunk_length=120,
        ),
    )

    llm_processor = GroqProcessor(
        GroqLLM(),
        conversation_id,
        user_id,
    )

    # Warmup database connection
    asyncio.create_task(asyncio.to_thread(warmup_db))

    turn_manager = TurnManager(
        conversation_id=conversation_id,
        user_id=user_id,
        timeout=0.18,
    )

    rtvi = RTVIProcessor()

    pipeline = Pipeline(
        [
            transport.input(),
            rtvi,
            stt,
            TranscriptionLogger(),
            turn_manager,
            llm_processor,
            tts,
            transport.output(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        observers=[rtvi.create_rtvi_observer()],
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        print("Client connected")
        print(f"Conversation ID: {conversation_id}")
        print("Jeevan Mishra is ready.")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        print("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    print("Starting voice pipeline...")
    await runner.run()


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
