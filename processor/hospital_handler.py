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
            # Don't let providing details (like a phone number) hijack it into booking
            if intent != "greeting":
                intent = "cancel_appointment"
        elif self.current_flow == "book_appointment":
            # Allow switching to cancel midway, but otherwise lock the flow
            if intent == "cancel_appointment":
                self._reset_state()
                self.current_flow = "cancel_appointment"
            elif intent != "greeting":
                intent = "book_appointment"
        elif self.current_flow == "check_appointment":
            if intent != "greeting":
                intent = "check_appointment"
                
        if not self.current_flow:
            if intent in ["book_appointment", "cancel_appointment", "check_appointment"]:
                self.current_flow = intent
            
        # Update local state with any extracted entities
        for key in ["doctor_name", "hospital_name", "appointment_date", "appointment_time", "patient_name", "phone", "address"]:
            val = intent_data.get(key)
            if val:
                val_str = str(val).strip().lower()
                if val_str not in ["none", "null", "n/a", "unknown", "not mentioned", ""]:
                    self.state[key] = str(val).strip()
                    
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
        if self.state["doctor_name"]:
            import difflib
            docs = get_doctors()
            if docs:
                doc_names = [d[0] for d in docs]
                # Lowercase comparison
                doc_names_lower = {d.lower(): d for d in doc_names}
                matches = difflib.get_close_matches(self.state["doctor_name"].lower(), doc_names_lower.keys(), n=1, cutoff=0.5)
                if matches:
                    self.state["doctor_name"] = doc_names_lower[matches[0]]
                
        # Fallback: if we are asking for a doctor, and the LLM misclassified the name as patient_name
        if self.current_flow == "book_appointment" and not self.state["doctor_name"] and self.state["patient_name"]:
            # Check if this name matches a doctor
            doc = get_doctor_by_name(self.state["patient_name"])
            if doc:
                self.state["doctor_name"] = self.state["patient_name"]
                self.state["patient_name"] = None

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
            hospital_name = intent_data.get("hospital_name")
            if hospital_name:
                doctors = get_doctors_by_hospital(hospital_name)
                if not doctors:
                    return f"I couldn't find any doctors at {hospital_name}."
                doc_list = [f"Dr. {d[0]} ({d[1]})" for d in doctors]
                return f"At {hospital_name}, we have: {', '.join(doc_list)}. Who would you like to see?"
            else:
                doctors = get_doctors()
                if not doctors:
                    return "I couldn't find any doctors."
                doc_list = [f"Dr. {d[0]}" for d in doctors]
                return f"We have several doctors including: {', '.join(doc_list[:5])}. Do you have a specific doctor in mind?"

        elif intent == "check_fee":
            doc_name = self.state.get("doctor_name")
            if not doc_name:
                return "Could you please tell me which doctor's fee you want to know?"
            doc = get_doctor_by_name(doc_name)
            if not doc:
                return f"I couldn't find Dr. {doc_name}. Could you verify the name?"
            # doc tuple: id, name, specialization, fee, schedule, hospital_name
            return f"Dr. {doc[1]} charges ₹{doc[3]}. Their schedule is {doc[4]}."

        elif intent == "book_appointment":
            return self._handle_booking_flow()

        elif intent == "cancel_appointment":
            return self._handle_cancel_flow()
            
        elif intent == "check_appointment":
            return self._handle_check_flow()

        else:
            return "I'm sorry, I didn't quite catch that. Would you like to book an appointment or check a doctor's availability?"

    def _handle_booking_flow(self) -> str:
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
            doc_list = [f"Dr. {d[0]} ({d[1]})" for d in doctors]
            return f"At {self.state['hospital_name']}, we have: {', '.join(doc_list)}. Which doctor would you like to see?"
            
        doc = get_doctor_by_name(self.state["doctor_name"])
        if not doc:
            self.state["doctor_name"] = None
            return "I couldn't find that doctor. Which doctor did you mean?"
        
        self.state["doctor_name"] = doc[1]

        # Step 3: Tell Slot/Fee and ask Date/Time
        if not self.state["appointment_date"] or not self.state["appointment_time"]:
            return f"Dr. {doc[1]} charges ₹{doc[3]} and their schedule is {doc[4]}. What date and time would you like to book?"

        # Verify availability
        is_available = check_slot_available(
            self.state["doctor_name"],
            self.state["appointment_date"],
            self.state["appointment_time"]
        )
        if not is_available:
            self.state["appointment_date"] = None
            self.state["appointment_time"] = None
            return f"I'm sorry, Dr. {doc[1]} is not available at that time. What other time works for you?"

        # Step 4: Patient Name
        if not self.state["patient_name"]:
            return "Great, that slot is available! Could you please tell me your full name?"

        # Step 5: Phone
        if not self.state["phone"]:
            return f"Thanks, {self.state['patient_name']}. What is your 10-digit mobile number?"
        elif len(str(self.state["phone"]).replace(" ", "")) != 10 or not str(self.state["phone"]).replace(" ", "").isdigit():
            self.state["phone"] = None
            return "Please provide a valid 10-digit mobile number."

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
            return "And what is your 10-digit mobile number?"
        elif len(str(self.state["phone"]).replace(" ", "")) != 10 or not str(self.state["phone"]).replace(" ", "").isdigit():
            self.state["phone"] = None
            return "Please provide a valid 10-digit mobile number."

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
        if not self.state["phone"]:
            return "Sure, I can check your appointment status. What is your 10-digit mobile number?"
        elif len(str(self.state["phone"]).replace(" ", "")) != 10 or not str(self.state["phone"]).replace(" ", "").isdigit():
            self.state["phone"] = None
            return "Please provide a valid 10-digit mobile number."

        apps = get_patient_appointments(self.state["phone"])
        self._reset_state()
        
        if not apps:
            return "I couldn't find any confirmed appointments booked under that mobile number."
            
        # apps[0] is id, doctor_name, appointment_date, appointment_time, status, hospital_name, patient_name
        a = apps[0]
        return f"Yes, your booking is confirmed, {a[6]} has an appointment with {a[1]} at {a[5]} on {a[2]} at {a[3]}."

    def _reset_state(self):
        self.current_flow = None
        for key in self.state:
            self.state[key] = None
