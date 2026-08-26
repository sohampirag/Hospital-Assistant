import json
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
    get_patient_appointments
)

class HospitalHandler:
    def __init__(self):
        self.current_flow = None
        # We store booking state locally instead of relying on the LLM history.
        self.state = {
            "hospital_name": None,
            "doctor_name": None,
            "appointment_date": None,
            "appointment_time": None,
            "patient_name": None,
            "phone": None,
            "address": None
        }

    def process_intent(self, intent_data: dict, user_text: str = "") -> str:
        intent = intent_data.get("intent", "unknown")
        
        if self.current_flow == "cancel_appointment":
            if intent not in ["greeting", "farewell"]:
                intent = "cancel_appointment"
        elif self.current_flow == "book_appointment":
            if intent == "cancel_appointment":
                self._reset_state()
                self.current_flow = "cancel_appointment"
            elif intent in ["greeting", "farewell", "check_hospital", "check_fee"]:
                pass  # allow these through even during booking
            else:
                intent = "book_appointment"
        elif self.current_flow == "check_appointment":
            if intent not in ["greeting", "farewell"]:
                intent = "check_appointment"
        elif self.current_flow == "check_availability":
            # User is selecting a doctor from the listed doctors
            if intent not in ["greeting", "farewell"]:
                intent = "list_doctors"
                
        if not self.current_flow:
            if intent in ["book_appointment", "cancel_appointment", "check_appointment"]:
                self.current_flow = intent
            
        # Update local state with any extracted entities
        for key in ["doctor_name", "hospital_name", "appointment_date", "appointment_time", "patient_name", "phone", "address"]:
            val = intent_data.get(key)
            if val:
                val_str = str(val).strip().lower()
                if val_str not in ["none", "null", "n/a", "unknown", "not mentioned", ""]:
                    if key == "phone":
                        import re
                        cleaned = re.sub(r'\D', '', str(val))
                        if cleaned:
                            self.state[key] = cleaned
                    else:
                        self.state[key] = str(val).strip()
                    
        # Fallback: if LLM lumps time into the date string
        if self.state["appointment_date"] and not self.state["appointment_time"]:
            import re
            time_match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM|am|pm)\b', self.state["appointment_date"])
            if time_match:
                self.state["appointment_time"] = time_match.group(0)
                    
        # Fallback: if we are at the address step, use raw text if LLM failed to extract it
        if self.current_flow == "book_appointment" and self.state["phone"] and not self.state["address"]:
            # Make sure we didn't JUST extract the phone number in this very turn
            if user_text and intent != "greeting" and not intent_data.get("phone"):
                self.state["address"] = user_text
                
        # Normalize hospital names to handle STT variations (like Chaitram -> Choithram)
        if self.state["hospital_name"]:
            hn = self.state["hospital_name"].lower()
            if "choithram" in hn or "chaitram" in hn or "chotram" in hn:
                self.state["hospital_name"] = "Choithram Hospital & Research Centre"
            elif "apollo" in hn:
                self.state["hospital_name"] = "Apollo Hospitals Indore"
            elif "shukla" in hn:
                self.state["hospital_name"] = "Shukla Hospital"
            elif "bombay" in hn:
                self.state["hospital_name"] = "Bombay Hospital Indore"
                
        # Normalize doctor names using fuzzy matching
        import difflib
        import re as _re
        if self.state["doctor_name"]:
            docs = get_doctors()
            if docs:
                doc_names = [d[0] for d in docs]
                doc_names_lower = {d.lower(): d for d in doc_names}
                matches = difflib.get_close_matches(self.state["doctor_name"].lower(), doc_names_lower.keys(), n=1, cutoff=0.5)
                if matches:
                    self.state["doctor_name"] = doc_names_lower[matches[0]]
                else:
                    # No fuzzy match - clear bad name so raw text fallback can try
                    self.state["doctor_name"] = None

        # Raw text fallback: if still no doctor name, try matching the full user utterance
        if not self.state["doctor_name"] and user_text and self.current_flow == "check_availability":
            docs = get_doctors()
            if docs:
                doc_names = [d[0] for d in docs]
                doc_names_lower = {d.lower(): d for d in doc_names}
                # Strip common prefixes like 'doctor', 'dr.', 'okay', etc.
                clean_text = _re.sub(r'^(okay|ok|yes|doctor|dr\.?)\s+', '', user_text.lower().strip())
                matches = difflib.get_close_matches(clean_text, doc_names_lower.keys(), n=1, cutoff=0.5)
                if matches:
                    self.state["doctor_name"] = doc_names_lower[matches[0]]
                    intent = "list_doctors"  # force intent regardless of LLM output
                
        # Fallback: if we are asking for a doctor, and the LLM misclassified the name as patient_name
        if self.current_flow == "book_appointment" and not self.state["doctor_name"] and self.state["patient_name"]:
            # Check if this name matches a doctor
            doc = get_doctor_by_name(self.state["patient_name"])
            if doc:
                self.state["doctor_name"] = self.state["patient_name"]
                self.state["patient_name"] = None

        if intent == "farewell":
            self._reset_state()
            return "Thank you for calling, have a great day and stay healthy, goodbye!"

        if intent == "greeting":
            if self.current_flow == "book_appointment":
                return "Hello! " + self._handle_booking_flow()
            elif self.current_flow == "cancel_appointment":
                return "Hello! " + self._handle_cancel_flow()
            else:
                self._reset_state()
                return "Hello! I am Jeevan Mishra, your hospital assistant. How can I help you today?"

        elif intent == "list_hospitals":
            hospitals = get_hospitals()
            if not hospitals:
                return "I'm sorry, I couldn't find any hospitals in our database right now."
            names = [h[0] for h in hospitals]
            return f"We have the following hospitals available: {', '.join(names)}."

        elif intent == "list_doctors":
            # If user picked a specific doctor (from check_availability flow or direct mention)
            if self.state["doctor_name"]:
                doc = get_doctor_by_name(self.state["doctor_name"])
                if doc:
                    # doc: id, name, specialization, fee, schedule, hospital_name
                    doc_display = doc[1] if str(doc[1]).startswith("Dr") else f"Dr. {doc[1]}"
                    self.current_flow = "book_appointment"
                    self.state["hospital_name"] = doc[5]
                    return f"{doc_display} specializes in {doc[2]} and is available at {doc[5]}, their fee is rupees {doc[3]} and schedule is {doc[4]}, would you like to book an appointment?"
                else:
                    # Doctor name not found, list again
                    self.state["doctor_name"] = None
            # Otherwise list all doctors and enter check_availability flow
            hospital_name = intent_data.get("hospital_name")
            if hospital_name:
                doctors = get_doctors_by_hospital(hospital_name)
                if not doctors:
                    return f"I couldn't find any doctors at {hospital_name}."
                doc_list = [f"{d[0] if str(d[0]).startswith('Dr') else 'Dr. ' + str(d[0])} ({d[1]})" for d in doctors]
                self.current_flow = "check_availability"
                return f"At {hospital_name}, we have: {', '.join(doc_list)}, which doctor would you like to know about?"
            else:
                doctors = get_doctors()
                if not doctors:
                    return "I couldn't find any doctors."
                doc_list = [f"{d[0] if str(d[0]).startswith('Dr') else 'Dr. ' + str(d[0])}" for d in doctors]
                self.current_flow = "check_availability"
                return f"We have the following doctors: {', '.join(doc_list)}, which doctor would you like to know about?"

        elif intent == "check_fee":
            doc_name = self.state.get("doctor_name")
            if not doc_name:
                return "Could you please tell me which doctor's fee you want to know?"
            doc = get_doctor_by_name(doc_name)
            if not doc:
                return f"I couldn't find Dr. {doc_name}, could you verify the name?"
            # doc tuple: id, name, specialization, fee, schedule, hospital_name
            doc_display = doc[1] if str(doc[1]).startswith("Dr") else f"Dr. {doc[1]}"
            return f"{doc_display} is available at {doc[5]}, charges rupees {doc[3]}, and their schedule is {doc[4]}."

        elif intent == "check_hospital":
            doc_name = self.state.get("doctor_name")
            if not doc_name:
                return "Could you please tell me which doctor you are asking about?"
            doc = get_doctor_by_name(doc_name)
            if not doc:
                return f"I couldn't find that doctor, could you verify the name?"
            doc_display = doc[1] if str(doc[1]).startswith("Dr") else f"Dr. {doc[1]}"
            return f"{doc_display} specializes in {doc[2]} and is available at {doc[5]}, would you like to book an appointment?"

        elif intent == "book_appointment":
            return self._handle_booking_flow()

        elif intent == "cancel_appointment":
            return self._handle_cancel_flow()
            
        elif intent == "check_appointment":
            return self._handle_check_flow()

        else:
            return "I'm sorry, I didn't quite catch that, would you like to book an appointment or check a doctor's availability?"

    def _handle_booking_flow(self) -> str:
        # If we already know the doctor, look up their hospital automatically
        if self.state["doctor_name"] and not self.state["hospital_name"]:
            doc_lookup = get_doctor_by_name(self.state["doctor_name"])
            if doc_lookup:
                self.state["hospital_name"] = doc_lookup[5]

        # Step 1: Need Hospital
        if not self.state["hospital_name"]:
            hospitals = get_hospitals()
            names = [h[0] for h in hospitals] if hospitals else []
            return f"Which hospital would you prefer? Available options are: {', '.join(names)}."
            
        # Step 2: Need Doctor
        if not self.state["doctor_name"]:
            doctors = get_doctors_by_hospital(self.state["hospital_name"])
            if not doctors:
                return f"I couldn't find any doctors at {self.state['hospital_name']}."
            doc_list = [f"{d[0] if str(d[0]).startswith('Dr') else 'Dr. ' + str(d[0])} ({d[1]})" for d in doctors]
            return f"At {self.state['hospital_name']}, we have: {', '.join(doc_list)}, which doctor would you like to see?"
            
        doc = get_doctor_by_name(self.state["doctor_name"])
        if not doc:
            self.state["doctor_name"] = None
            return "I couldn't find that doctor, which doctor did you mean?"
        
        self.state["doctor_name"] = doc[1]
        # Also ensure hospital is always populated from DB
        if not self.state["hospital_name"]:
            self.state["hospital_name"] = doc[5]

        # Step 3: Tell Slot/Fee and ask Date/Time
        doc_display = doc[1] if str(doc[1]).startswith("Dr") else f"Dr. {doc[1]}"
        if not self.state["appointment_date"] or not self.state["appointment_time"]:
            return f"{doc_display} charges ₹{doc[3]} and their schedule is {doc[4]}. What date and time would you like to book?"

        # Verify availability
        is_available = check_slot_available(
            self.state["doctor_name"],
            self.state["appointment_date"],
            self.state["appointment_time"]
        )
        if not is_available:
            self.state["appointment_date"] = None
            self.state["appointment_time"] = None
            return f"I'm sorry, {doc_display} is not available at that time. What other time works for you?"

        # Step 4: Patient Name
        if not self.state["patient_name"]:
            return "Great, that slot is available! Could you please tell me your full name?"

        # Step 5: Phone
        if not self.state["phone"]:
            return f"Thanks, {self.state['patient_name']}. What is your ten-digit mobile number?"
        elif len(str(self.state["phone"]).replace(" ", "")) != 10 or not str(self.state["phone"]).replace(" ", "").isdigit():
            self.state["phone"] = None
            return "Please provide a valid ten-digit mobile number."

        # Step 6: Address
        if not self.state["address"]:
            return "Got it. Finally, could you share your address?"

        # Step 7: Execute booking
        app_id = create_appointment(
            self.state["patient_name"],
            self.state["phone"],
            self.state["address"],
            self.state["doctor_name"],
            self.state["appointment_date"],
            self.state["appointment_time"]
        )

        if app_id:
            self._reset_state()
            return f"Your appointment is confirmed, your booking ID is {app_id}, is there anything else I can help you with?"
        else:
            return "I'm sorry, there was a problem booking your appointment, please try again."

    def _handle_cancel_flow(self) -> str:
        # Step 1: Need Patient Name
        if not self.state["patient_name"]:
            return "Sure, I can help with that. May I have your full name?"

        # Step 2: Need Phone
        if not self.state["phone"]:
            return "And what is your ten-digit mobile number?"
        elif len(str(self.state["phone"]).replace(" ", "")) != 10 or not str(self.state["phone"]).replace(" ", "").isdigit():
            self.state["phone"] = None
            return "Please provide a valid ten-digit mobile number."

        # Both provided, check DB
        apps = get_appointment_for_cancellation(self.state["patient_name"], self.state["phone"])
        
        if not apps:
            self._reset_state()
            return "I couldn't find any active appointment booked under that name and mobile number. You have not booked an appointment."

        # Cancel the first active appointment found
        app_id = apps[0][0]
        success = cancel_appointment(app_id)
        
        self._reset_state()
        
        if success:
            return "Your appointment has been successfully cancelled."
        else:
            return "I'm sorry, there was an issue cancelling your appointment. Please try again later."
            
    def _handle_check_flow(self) -> str:
        # Step 1: Need Patient Name
        if not self.state["patient_name"]:
            return "Sure, I can check your appointment status. May I have your full name?"

        # Step 2: Need Phone
        if not self.state["phone"]:
            return "And what is your ten-digit mobile number?"
        elif len(str(self.state["phone"]).replace(" ", "")) != 10 or not str(self.state["phone"]).replace(" ", "").isdigit():
            self.state["phone"] = None
            return "Please provide a valid ten-digit mobile number."

        # Both provided, check DB
        apps = get_appointment_for_cancellation(self.state["patient_name"], self.state["phone"])
        self._reset_state()
        
        if not apps:
            return "I couldn't find any confirmed appointments booked under that name and mobile number."
            
        # apps[0] is id, doctor_name, appointment_date, appointment_time, status, hospital_name, patient_name
        a = apps[0]
        return f"Yes, your booking is confirmed, {a[6]} has an appointment with {a[1]} at {a[5]} on {a[2]} at {a[3]}."

    def _reset_state(self):
        self.current_flow = None
        for key in self.state:
            self.state[key] = None
