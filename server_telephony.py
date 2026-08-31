import asyncio
import os
import uuid

from dotenv import load_dotenv

from pipecat.frames.frames import Frame, TranscriptionFrame, InterimTranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIProcessor
from pipecat.services.cartesia.stt import CartesiaSTTService
from pipecat.services.cartesia.tts import CartesiaTTSService

from pipecat.transports.websocket.server import (
    SingleClientWebsocketServerTransport,
    SingleClientWebsocketServerParams,
)
from pipecat.serializers.exotel import ExotelFrameSerializer

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


async def main():
    # Warmup database connection on startup
    await asyncio.to_thread(warmup_db)
    
    cartesia_api_key = os.getenv("CARTESIA_API_KEY")
    if not cartesia_api_key:
        print("CARTESIA_API_KEY is missing from .env")
        return
    
    conversation_id = uuid.uuid4()
    user_id = uuid.uuid4() # We use phone number for identity in Jeevan

    while True:
        print(f"Waiting for incoming call on ws://0.0.0.0:8082... Conversation ID: {conversation_id}")

        transport = SingleClientWebsocketServerTransport(
            host="0.0.0.0",
            port=8082,
            params=SingleClientWebsocketServerParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_out_sample_rate=8000,
                audio_out_channels=1,
                serializer=ExotelFrameSerializer(stream_sid="tata_smartflo_stream")
            )
        )

        stt = CartesiaSTTService(
            api_key=cartesia_api_key,
        )

        # Note: TTS needs to output 8kHz for PSTN
        tts = CartesiaTTSService(
            api_key=cartesia_api_key,
            voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22", # British Lady
            sample_rate=8000,
        )

        llm_processor = GroqProcessor(
            GroqLLM(),
            conversation_id,
            user_id,
        )

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
            idle_timeout_secs=None, # Disable idle timeout so it waits indefinitely
        )

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, websocket):
            print("Call disconnected")
            await worker.cancel()

        from pipecat.workers.runner import WorkerRunner
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)

        try:
            print("Starting telephony pipeline...")
            await runner.run()
        except Exception as e:
            print(f"Error in pipeline: {e}")
            
        print("Call ended. Resetting pipeline for next call...\n")
        # Generate new conversation ID for next call
        conversation_id = uuid.uuid4()


if __name__ == "__main__":
    asyncio.run(main())
