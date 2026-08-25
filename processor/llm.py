import os
import json
from dotenv import load_dotenv
from groq import Groq

load_dotenv(override=True)

class GroqLLM:
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY is missing from .env")

        self.client = Groq(api_key=api_key)
        self.model = "openai/gpt-oss-20b"

    def extract_intent(self, text: str, state: dict) -> dict:
        """
        Takes user text and current state, returns JSON with intent and extracted entities.
        """
        system_prompt = """You are an intent extractor for a hospital appointment booking system.
Extract the user's intent and any relevant entities.

Return ONLY a valid JSON object with the following schema:
{
  "intent": "<intent_name>",
  "doctor_name": "<name of the doctor the user wants to see>",
  "hospital_name": "<hospital if mentioned>",
  "appointment_date": "<date if mentioned (e.g. '27 August', 'tomorrow')>",
  "appointment_time": "<time if mentioned (e.g. '1 PM', '13:00')>",
  "patient_name": "<the user's own name, if providing patient details>",
  "phone": "<phone if mentioned>",
  "address": "<address if mentioned>"
}

CRITICAL: If the user provides both date and time in one sentence (e.g., '27 August 1 PM'), you MUST separate them into `appointment_date` ('27 August') and `appointment_time` ('1 PM').

Possible intents:
- 'greeting': User says hello.
- 'list_hospitals': User asks what hospitals are available.
- 'list_doctors': User asks for doctors (optionally in a hospital or by specialization).
- 'check_fee': User asks for a doctor's fee or schedule.
- 'book_appointment': User wants to book an appointment, or is providing details for one.
- 'cancel_appointment': User wants to cancel.
- 'check_appointment': User asks to check if they have an appointment or check its status.
- 'unknown': If you cannot determine the intent.

Current Conversation State:
""" + json.dumps(state) + """

Be extremely fast and concise."""

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0,
                response_format={"type": "json_object"}
            )
            
            result = completion.choices[0].message.content
            return json.loads(result)
        except Exception as e:
            print(f"[LLM INTENT ERROR] {e}")
            return {"intent": "unknown"}
