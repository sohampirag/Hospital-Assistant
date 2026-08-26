import os
import re
import json
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Word-to-digit phone number normalizer (no external API)
# ---------------------------------------------------------------------------
_WORD_DIGIT = {
    "zero": "0", "oh": "0", "o": "0",
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_MULTIPLIER = {"double": 2, "triple": 3, "quadruple": 4}

def normalize_phone(raw: str) -> str:
    """Convert spoken phone strings to a digit string. Returns '' if nothing found."""
    if not raw:
        return ""
    # First strip all non-alphanumeric except spaces
    text = re.sub(r"[^a-zA-Z0-9 ]", " ", str(raw).lower())
    tokens = text.split()
    digits = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _MULTIPLIER and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            d = _WORD_DIGIT.get(nxt) or (nxt if nxt.isdigit() else None)
            if d:
                digits.append(d * _MULTIPLIER[tok])
                i += 2
                continue
        if tok in _WORD_DIGIT:
            digits.append(_WORD_DIGIT[tok])
        elif tok.isdigit():
            digits.append(tok)
        i += 1
    result = "".join(digits)
    # Also try extracting raw digit-only sequences from original string
    if not result:
        result = re.sub(r"\D", "", str(raw))
    return result


class GroqLLM:
    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY is missing from .env")
        self.client = AsyncGroq(api_key=api_key)
        self.model = "openai/gpt-oss-20b"

    async def extract_intent(self, text: str, state: dict) -> dict:
        """
        Single fast Groq call.  Returns a dict with intent + entities.
        We pass only the fields that are NOT yet resolved in state, to keep
        the prompt tiny.
        """
        # Build a minimal state summary (only unresolved fields)
        pending = {k: v for k, v in state.items() if v is None}
        pending_str = ", ".join(pending.keys()) if pending else "none"

        system_prompt = (
            "You are Jeevan Mishra, a hospital appointment assistant.\n"
            "Help ONLY with: hospitals, doctors, specialties, fees, schedules, "
            "availability, appointments (booking/cancellation/check).\n"
            "For any unrelated question reply with intent=unrelated.\n\n"
            "Return ONLY valid JSON with these keys:\n"
            "{\n"
            '  "intent": "<see list below>",\n'
            '  "doctor_name": "<doctor if explicitly mentioned, else null>",\n'
            '  "hospital_name": "<hospital if explicitly mentioned, else null>",\n'
            '  "specialization": "<specialty if mentioned, else null>",\n'
            '  "appointment_date": "<date string or null>",\n'
            '  "appointment_time": "<time string or null>",\n'
            '  "patient_name": "<patient full name if providing personal details, else null>",\n'
            '  "phone": "<phone digits only, convert spoken words to digits, else null>",\n'
            '  "address": "<address if provided, else null>",\n'
            '  "confirmation": "<yes|no|null>"\n'
            "}\n\n"
            "Intent values: greeting, book_appointment, list_hospitals, list_doctors, "
            "doctor_information, specialization_information, fee_information, schedule_information, "
            "hospital_information, check_availability, check_appointment, cancel_appointment, "
            "cancel_booking_process, unrelated.\n\n"
            "RULES:\n"
            "- If the user says 'I want to book an appointment', intent is EXACTLY book_appointment. Do NOT hallucinate a hospital or doctor.\n"
            "- If the user says 'Book Dr. X tomorrow at 11 AM', set intent=book_appointment, "
            "doctor_name=Dr. X, appointment_date=tomorrow, appointment_time=11 AM.\n"
            "- NEVER put a patient name into doctor_name. Patient names go in patient_name.\n"
            "- If user says 'My name is Ashish', patient_name=Ashish, doctor_name=null.\n"
            "- For phone: convert 'one two three' -> '123', 'double four' -> '44', 'oh' -> '0'.\n"
            "- Separate date and time always. '27 August 11 AM' -> appointment_date='27 August', appointment_time='11 AM'.\n"
            "- If the user provides an address, location, or area, set address to it. If we are booking, keep intent=book_appointment.\n"
            f"- Fields still needed from user: {pending_str}.\n"
        )

        try:
            completion = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0,
                response_format={"type": "json_object"},
                max_tokens=300,
            )
            raw = completion.choices[0].message.content
            result = json.loads(raw)

            # Normalize phone right here so handler always gets clean digits
            if result.get("phone"):
                result["phone"] = normalize_phone(str(result["phone"]))

            return result
        except Exception as e:
            print(f"[LLM INTENT ERROR] {e}")
            return {"intent": "unrelated"}
