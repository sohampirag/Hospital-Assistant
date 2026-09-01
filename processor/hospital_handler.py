"""
hospital_handler.py  –  Aradhya Mishra Hospital Assistant

Architecture:
  - One HospitalHandler instance per session (per call). State is NOT shared.
  - LLM extracts intent + entities (structured JSON).
  - This handler merges entities, resolves DB facts, validates, and produces
    a deterministic response. No hallucinations.
"""

import re
import difflib

from processor.hospital_db import (
    get_hospitals,
    get_doctors,
    get_doctors_by_hospital,
    get_doctors_by_specialization,
    get_doctor_by_name,
    check_slot_available,
    create_appointment,
    get_appointment_for_cancellation,
    cancel_appointment,
    get_patient_appointments,
    verify_appointment_booked,
    verify_appointment_cancelled,
    get_hospital_address,
)

# ---------------------------------------------------------------------------
# Hospital name normalizer – deterministic, no LLM
# ---------------------------------------------------------------------------
_HOSPITAL_ALIASES = {
    "apollo": "Apollo Hospitals Indore",
    "shukla": "Shukla Hospital",
    "sukla": "Shukla Hospital",
    "bombay": "Bombay Hospital Indore",
    "choithram": "Choithram Hospital & Research Centre",
    "chaitram": "Choithram Hospital & Research Centre",
    "chotram": "Choithram Hospital & Research Centre",
    "choitharam": "Choithram Hospital & Research Centre",
}

def _normalize_hospital(raw: str) -> str | None:
    if not raw:
        return None
    lower = raw.lower()
    for alias, canonical in _HOSPITAL_ALIASES.items():
        if alias in lower:
            return canonical
    return None


# ---------------------------------------------------------------------------
# Doctor name resolver – safe (only when appropriate)
# ---------------------------------------------------------------------------
def _resolve_doctor(name_hint: str, hospital_filter: str | None = None) -> tuple | None:
    if not name_hint:
        return None

    clean = re.sub(r'^(dr\.?|doctor)\s+', '', name_hint.strip(), flags=re.I).strip()

    # 1. Direct DB lookup (ILIKE)
    doc = get_doctor_by_name(clean) or get_doctor_by_name(name_hint.strip())
    if doc:
        if hospital_filter and hospital_filter.lower() not in doc[5].lower():
            return None
        return doc

    # 2. Fuzzy match
    if hospital_filter:
        candidates = get_doctors_by_hospital(hospital_filter)
        cand_names = [r[0] for r in candidates]
    else:
        all_docs = get_doctors()
        cand_names = [r[0] for r in all_docs]

    if not cand_names:
        return None

    # Map cleaned candidate names to original DB names
    cand_map = {}
    for c in cand_names:
        c_clean = re.sub(r'^(dr\.?|doctor)\s+', '', c.strip(), flags=re.I).strip().lower()
        # Keep track of the original name so we can look it up in DB
        cand_map[c_clean] = c

    matches = difflib.get_close_matches(clean.lower(), cand_map.keys(), n=1, cutoff=0.5)
    if matches:
        resolved_original = cand_map[matches[0]]
        return get_doctor_by_name(resolved_original)

    return None


def _fmt_doc(doc_row) -> str:
    name = doc_row[1]
    return name if str(name).startswith("Dr") else f"Dr. {name}"


# ---------------------------------------------------------------------------
# Main handler – one instance per session
# ---------------------------------------------------------------------------
class HospitalHandler:
    def __init__(self):
        self.hospital_name: str | None = None
        self.doctor_name: str | None = None
        self.doctor_row: tuple | None = None
        self.specialization: str | None = None
        self.appointment_date: str | None = None
        self.appointment_time: str | None = None
        self.patient_name: str | None = None
        self.phone: str | None = None
        self.address: str | None = None
        
        self.current_intent: str | None = None
        self.confirmation_pending: bool = False
        self.navigation_booking_pending: bool = False
        
        self.cancel_appointments: list = []
        self.cancel_idx: int = 0

    def process_intent(self, intent_data: dict, user_text: str = "") -> str:
        intent = intent_data.get("intent", "unrelated")
        if not isinstance(intent, str):
            intent = "unrelated"
        intent = intent.lower()

        if self.confirmation_pending:
            return self._handle_confirmation(intent_data, user_text)

        if getattr(self, "navigation_booking_pending", False):
            self.navigation_booking_pending = False
            text_lower = user_text.lower().strip()
            confirm = intent_data.get("confirmation")
            if confirm == "yes" or any(w in text_lower for w in ("yes", "yeah", "sure", "ok", "book")):
                intent = "book_appointment"
                self.current_intent = "book_appointment"
            elif confirm == "no" or any(w in text_lower for w in ("no", "nope")):
                return "Alright. How else can I help you?"

        text_lower = re.sub(r'[^a-z0-9\s\']', '', user_text.lower().strip())
        ending_phrases = {"no", "no thanks", "no thank you", "that's all", "thats all", "nothing else", "that is all", "i'm done", "im done", "bye", "goodbye"}
        if text_lower in ending_phrases:
            self._reset()
            return "Thank you for using Aradhya Hospital Assistant. Have a great day! [END_CALL]"

        self._merge_entities(intent_data)

        # Fallback for LLM JSON failures or complete misclassifications during data collection
        if self.current_intent in ("book_appointment", "cancel_appointment", "check_appointment"):
            if intent in ("unrelated", "unknown") and not any(intent_data.get(k) for k in ["patient_name", "phone", "address", "hospital_name", "doctor_name", "appointment_date", "appointment_time"]):
                text_clean = user_text.strip()
                if text_clean:
                    if not self.patient_name:
                        self.patient_name = text_clean
                        intent = self.current_intent
                    elif not self.phone:
                        digits = re.sub(r"\D", "", text_clean)
                        if len(digits) >= 10:
                            self.phone = digits
                            intent = self.current_intent
                    elif self.current_intent == "book_appointment" and not self.address:
                        self.address = text_clean
                        intent = self.current_intent

        # Robust intent override: if we are in a booking flow and the LLM misclassified a single-entity response
        if self.current_intent in ("book_appointment", "cancel_appointment", "check_appointment"):
            if intent in ("unrelated", "unknown", "hospital_information", "doctor_information", "fee_information", "schedule_information"):
                # If they provided ANY useful booking entity in this turn, force current intent
                def _is_valid(val):
                    return bool(val and str(val).lower() not in ("none", "null", ""))

                if any(_is_valid(intent_data.get(k)) for k in ["patient_name", "phone", "address", "hospital_name", "doctor_name", "appointment_date", "appointment_time"]):
                    intent = self.current_intent

        # Logging to verify router
        print(f"[ROUTER] intent={intent}")
        print(f"[ROUTER] entities={intent_data}")
        print(f"[STATE] after={self.state}")

        # Exact ONE router based on explicit intent
        if intent == "farewell":
            self._reset()
            return "Thank you for calling, have a great day and stay healthy, goodbye!"
            
        if intent == "greeting":
            if self.current_intent:
                return self._resume_flow()
            self._reset()
            return ("Hello! I am Aradhya Mishra, your hospital assistant. "
                    "I can help you with hospital information, doctor availability, "
                    "and appointment booking. How can I help you today?")
                    
        if intent == "list_hospitals":
            return self._handle_list_hospitals()
            
        if intent == "list_doctors":
            return self._handle_list_doctors(intent_data)
            
        if intent == "patient_navigation":
            return self._handle_patient_navigation(intent_data)

        if intent == "check_specialization" or intent == "specialization_information":
            return self._handle_check_specialization(intent_data)
            
        if intent in ("check_fee", "fee_information", "check_schedule", "schedule_information", "check_hospital", "hospital_information", "doctor_information"):
            return self._handle_info_query(intent)

        if intent == "check_availability":
            return self._handle_check_availability()
            
        if intent == "book_appointment":
            self.current_intent = "book_appointment"
            return self._handle_booking()
            
        if intent == "cancel_appointment":
            self.current_intent = "cancel_appointment"
            return self._handle_cancel()
            
        if intent == "cancel_booking_process":
            self._reset()
            return "Booking process cancelled. How else can I help you?"

        if intent == "check_appointment":
            self.current_intent = "check_appointment"
            return self._handle_check_appointment()

        # Fallback for unrelated
        return ("I can help with hospital information and appointments. "
                "What would you like to know?")

    def _merge_entities(self, d: dict):
        extracted_patient = None
        raw_p = d.get("patient_name")
        if raw_p and str(raw_p).lower() not in ("none", "null", ""):
            extracted_patient = str(raw_p).strip()

        # ── Doctor resolution ────────────────────────────────────────────────
        raw_d = d.get("doctor_name")
        explicit_doc = raw_d if (raw_d and str(raw_d).lower() not in ("none", "null", "")) else None

        candidates_to_try = []
        if explicit_doc:
            candidates_to_try.append(explicit_doc)
            
        # Before date/time is known, try guessing from patient_name ONLY if we don't already have a doctor
        if not self.doctor_name and not (self.appointment_date and self.appointment_time):
            if extracted_patient and extracted_patient not in candidates_to_try:
                candidates_to_try.append(extracted_patient)
            if self.patient_name and self.patient_name not in candidates_to_try:
                candidates_to_try.append(self.patient_name)

        for candidate in candidates_to_try:
            doc = _resolve_doctor(candidate, self.hospital_name)
            if doc:
                # If changing doctors, clear the date/time since the schedule is different
                if self.doctor_name and self.doctor_name != doc[1]:
                    self.appointment_date = None
                    self.appointment_time = None
                    
                self.doctor_row = doc
                self.doctor_name = doc[1]
                if not self.hospital_name:
                    self.hospital_name = doc[5]
                self.specialization = doc[2]
                
                # If the candidate came from patient_name state, clear it (it was misclassified)
                if self.patient_name == candidate:
                    self.patient_name = None
                if extracted_patient == candidate:
                    extracted_patient = None
                break  # Stop once we have a valid doctor

        # Only store as patient_name if the value is NOT a doctor
        if extracted_patient:
            if not _resolve_doctor(extracted_patient, self.hospital_name):
                self.patient_name = extracted_patient

        # Phone
        raw_ph = d.get("phone")
        if raw_ph:
            digits = re.sub(r"\D", "", str(raw_ph))
            if digits:
                self.phone = digits

        # Address
        raw_a = d.get("address")
        if raw_a and str(raw_a).lower() not in ("none", "null", ""):
            self.address = str(raw_a).strip()

        # Hospital
        raw_h = d.get("hospital_name")
        if raw_h:
            resolved = _normalize_hospital(raw_h)
            if resolved:
                self.hospital_name = resolved

        # Specialization
        raw_s = d.get("specialization")
        if raw_s:
            self.specialization = str(raw_s).strip()

        # Date/Time
        raw_date = d.get("appointment_date")
        if raw_date and str(raw_date).lower() not in ("none", "null", ""):
            # STT might output a.m. or p.m. with dots. Remove dots to normalize.
            date_clean = re.sub(r'([ap])\.m\.', r'\1m', str(raw_date), flags=re.I)
            time_in_date = re.search(
                r'\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)\b',
                date_clean
            )
            if time_in_date:
                self.appointment_time = time_in_date.group(0)
                self.appointment_date = date_clean[:time_in_date.start()].strip()
            else:
                self.appointment_date = str(raw_date).strip()

        raw_time = d.get("appointment_time")
        if raw_time and str(raw_time).lower() not in ("none", "null", ""):
            time_clean = re.sub(r'([ap])\.m\.', r'\1m', str(raw_time), flags=re.I)
            self.appointment_time = time_clean.strip()

    def _looks_like_patient_context(self, d: dict) -> bool:
        p = d.get("patient_name")
        return bool(p and str(p).lower() not in ("none", "null", ""))


    # ── Information handlers ─────────────────────────────────────────────────
    def _handle_list_hospitals(self) -> str:
        rows = get_hospitals()
        if not rows:
            return "I'm sorry, I couldn't find any hospitals in our system right now."
        names = [r[0] for r in rows]
        return f"We have {len(names)} hospitals: {', '.join(names)}."

    def _handle_list_doctors(self, d: dict) -> str:
        hospital = self.hospital_name
        if not hospital:
            raw_h = d.get("hospital_name")
            if raw_h:
                hospital = _normalize_hospital(raw_h)
        if hospital:
            rows = get_doctors_by_hospital(hospital)
            if not rows:
                return f"I couldn't find any doctors at {hospital}."
            doc_list = [
                f"{r[0] if str(r[0]).startswith('Dr') else 'Dr. ' + r[0]} ({r[1]})"
                for r in rows
            ]
            return f"At {hospital} we have: {', '.join(doc_list)}."
        
        rows = get_doctors()
        if not rows:
            return "I couldn't find any doctors in our system."
        doc_list = [
            f"{r[0] if str(r[0]).startswith('Dr') else 'Dr. ' + r[0]} ({r[1]})"
            for r in rows
        ]
        return f"We have the following doctors: {', '.join(doc_list)}."

    def _handle_patient_navigation(self, d: dict) -> str:
        spec = d.get("specialization") or self.specialization
        valid_specs = ["Cardiology", "Gastroenterology", "Orthopaedics", "Gynaecology", "General Medicine", "Dermatology"]
        
        matched_spec = None
        if spec:
            for v in valid_specs:
                if v.lower() == str(spec).lower():
                    matched_spec = v
                    break

        if not matched_spec:
            return "I can help you find a doctor from Cardiology, Gastroenterology, Orthopaedics, Gynaecology, General Medicine, or Dermatology."

        self.specialization = matched_spec
        rows = get_doctors_by_specialization(matched_spec, self.hospital_name)
        if not rows:
            return f"I couldn't find any {matched_spec} doctors."

        doc_list = []
        for r in rows:
            name = f"{r[0] if str(r[0]).startswith('Dr') else 'Dr. ' + r[0]}"
            fee = int(float(r[1])) if r[1] else 0
            schedule = r[2]
            hospital = r[3]
            if self.hospital_name:
                doc_list.append(f"{name} (fee: rupees {fee}, schedule: {schedule})")
            else:
                doc_list.append(f"{name} at {hospital} (fee: rupees {fee}, schedule: {schedule})")
        
        docs_str = ", ".join(doc_list)
        self.navigation_booking_pending = True
        return f"A {matched_spec} department may be appropriate. We have: {docs_str}. Would you like to book an appointment?"

    def _handle_check_specialization(self, d: dict) -> str:
        spec = self.specialization or d.get("specialization")
        if not spec:
            return "Which specialization are you interested in?"
        rows = get_doctors_by_specialization(spec, self.hospital_name)
        if not rows:
            return f"I couldn't find any {spec} doctors in our system."
        doc_list = [
            f"{r[0] if str(r[0]).startswith('Dr') else 'Dr. ' + r[0]} at {r[3]}"
            for r in rows
        ]
        return f"For {spec} we have: {', '.join(doc_list)}."

    def _handle_info_query(self, intent: str) -> str:
        if not self.doctor_row and self.doctor_name:
            self.doctor_row = get_doctor_by_name(self.doctor_name)
        doc = self.doctor_row
        if not doc:
            return "Which doctor would you like information about?"
        display = _fmt_doc(doc)
        if intent in ("check_fee", "fee_information"):
            return f"{display} charges rupees {int(float(doc[3]))}."
        if intent in ("check_schedule", "schedule_information"):
            return f"{display} is available {doc[4]}."
        if intent in ("check_hospital", "hospital_information", "doctor_information"):
            return f"{display} specializes in {doc[2]} and works at {doc[5]}."
        return ""

    def _handle_check_availability(self) -> str:
        if not self.doctor_name:
            return "For which doctor?"
        if not self.appointment_date or not self.appointment_time:
            return "What date and time?"
        available = check_slot_available(self.doctor_name, self.appointment_date, self.appointment_time)
        return "That slot is available." if available else "I'm sorry, that slot is not available."

    # ── Booking flow ─────────────────────────────────────────────────────────
    def _handle_booking(self) -> str:
        if not self.hospital_name and not self.doctor_name:
            rows = get_hospitals()
            names = [r[0] for r in rows] if rows else []
            if names:
                return f"Which hospital would you prefer? Available options are: {', '.join(names)}."
            return "Which hospital would you prefer?"

        if not self.doctor_name:
            if self.hospital_name:
                rows = get_doctors_by_hospital(self.hospital_name)
                if rows:
                    doc_list = [f"{r[0] if str(r[0]).startswith('Dr') else 'Dr. ' + r[0]} ({r[1]})" for r in rows]
                    return f"At {self.hospital_name} we have: {', '.join(doc_list)}. Which doctor would you like to see?"
            return "Which doctor would you like to see?"

        if not self.doctor_row:
            self.doctor_row = get_doctor_by_name(self.doctor_name)
        if not self.doctor_row:
            return "Could you tell me the doctor's name again?"

        doc = self.doctor_row
        display = _fmt_doc(doc)
        if not self.hospital_name:
            self.hospital_name = doc[5]

        if not self.appointment_date or not self.appointment_time:
            return (f"{display} specializes in {doc[2]} at {self.hospital_name}. "
                    f"The consultation fee is rupees {int(float(doc[3]))} and they are available {doc[4]}. "
                    "What date and time would you like to book?")

        # All core booking info present, check availability
        available = check_slot_available(
            self.doctor_name,
            self.appointment_date,
            self.appointment_time,
        )
        if not available:
            self.appointment_date = None
            self.appointment_time = None
            return f"I'm sorry, {display} is not available at that time. What other date or time works for you?"

        # Slot is available, collect missing patient info
        if not self.patient_name:
            return "That slot is available! Please provide your full name."

        if not self.phone:
            return f"Thanks {self.patient_name}. What is your ten digit mobile number?"
        if len(self.phone) != 10 or not self.phone.isdigit():
            self.phone = None
            return "Please provide a valid ten digit mobile number."

        if not self.address:
            return "Got it. Finally, what is your address?"

        # All fields present – show summary and ask confirmation
        self.confirmation_pending = True
        return (
            f"Please confirm your booking: "
            f"{display} at {self.hospital_name}, "
            f"on {self.appointment_date} at {self.appointment_time}, "
            f"fee rupees {int(float(doc[3]))}, "
            f"patient {self.patient_name}, "
            f"number ending {' '.join(self.phone[-4:])}. "
            "Shall I confirm this booking? Say yes or no."
        )

    def _handle_confirmation(self, intent_data: dict, user_text: str) -> str:
        confirm = intent_data.get("confirmation")
        text_lower = user_text.lower().strip()

        if confirm is None:
            if any(w in text_lower for w in ("yes", "yeah", "confirm", "ok", "haan", "ha", "bilkul")):
                confirm = "yes"
            elif any(w in text_lower for w in ("no", "nahi", "nope", "cancel", "nahin")):
                confirm = "no"

        if confirm == "yes":
            self.confirmation_pending = False
            return self._execute_booking()

        if confirm == "no":
            self.confirmation_pending = False
            self._reset()
            return "No problem, I've cancelled the booking request. How else can I help you?"

        return "Sorry, I didn't catch that. Please say yes to confirm or no to cancel."

    def _execute_booking(self) -> str:
        app_id = create_appointment(
            self.patient_name,
            self.phone,
            self.address,
            self.doctor_name,
            self.appointment_date,
            self.appointment_time,
        )
        if app_id and verify_appointment_booked(app_id):
            booking_id = app_id
            self._reset()
            return (f"Your appointment is confirmed! "
                    f"Your booking ID is {booking_id}. "
                    "Is there anything else I can help you with?")
        self._reset()
        return ("I'm sorry, there was a problem completing your booking. "
                "Please try again or call the hospital directly.")

    # ── Cancellation flow ────────────────────────────────────────────────────
    def _handle_cancel(self) -> str:
        if not self.patient_name:
            return "Sure, I can help with that. May I have your full name?"
        if not self.phone:
            return "And what is your ten digit mobile number?"
        if len(self.phone) != 10 or not self.phone.isdigit():
            self.phone = None
            return "Please provide a valid ten digit mobile number."

        apps = get_appointment_for_cancellation(self.patient_name, self.phone)
        if not apps:
            self._reset()
            return ("I couldn't find any active appointments booked under that name "
                    "and mobile number.")

        if len(apps) > 1:
            self.cancel_appointments = apps
            lines = []
            for i, a in enumerate(apps, 1):
                lines.append(f"{i}. {a[1]} on {a[2]} at {a[3]}")
            self._reset()
            return ("I found multiple appointments: " + "; ".join(lines) +
                    ". Please call us directly to cancel a specific one.")

        app_id = apps[0][0]
        success = cancel_appointment(app_id)
        
        # Verify the DB actually reports cancelled
        if success and verify_appointment_cancelled(app_id):
            self._reset()
            return "Your appointment has been successfully cancelled."
            
        self._reset()
        return "I'm sorry, there was a problem cancelling your appointment. Please try again."

    # ── Check appointment ─────────────────────────────────────────────────────
    def _handle_check_appointment(self) -> str:
        if not self.patient_name:
            return "Sure, I can check that. May I have your full name?"
        if not self.phone:
            return "And what is your ten digit mobile number?"
        if len(self.phone) != 10 or not self.phone.isdigit():
            self.phone = None
            return "Please provide a valid ten digit mobile number."

        apps = get_appointment_for_cancellation(self.patient_name, self.phone)
        self._reset()
        if not apps:
            return "I couldn't find any active appointments under that name and number."
        a = apps[0]
        return (f"Yes, {a[6]} has an appointment with {a[1]} "
                f"at {a[5]} on {a[2]} at {a[3]}.")

    def _resume_flow(self) -> str:
        if self.current_intent == "book_appointment":
            return "Hello! Let me continue with your booking. " + self._handle_booking()
        if self.current_intent == "cancel_appointment":
            return "Hello! Let me continue with your cancellation. " + self._handle_cancel()
        return "Hello! How can I help you?"

    def _reset(self):
        self.hospital_name = None
        self.doctor_name = None
        self.doctor_row = None
        self.specialization = None
        self.appointment_date = None
        self.appointment_time = None
        self.patient_name = None
        self.phone = None
        self.address = None
        self.current_intent = None
        self.confirmation_pending = False
        self.navigation_booking_pending = False
        self.cancel_appointments = []

    @property
    def state(self) -> dict:
        fields = {
            "hospital_name": self.hospital_name,
            "doctor_name": self.doctor_name,
            "appointment_date": self.appointment_date,
            "appointment_time": self.appointment_time,
            "patient_name": self.patient_name,
            "phone": self.phone,
            "address": self.address,
        }
        return fields
