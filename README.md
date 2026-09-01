# Hospital Assistant 🏥🎙️

**Hospital Assistant** is a low-latency, voice-based AI receptionist for hospitals, built on [Pipecat](https://github.com/pipecat-ai/pipecat). It answers calls (via WebRTC for testing or PSTN telephony via Exotel/Tata Smartflo in production), understands natural speech, and helps callers:

- Browse hospitals, doctors, specializations, fees, and schedules
- Check doctor/slot availability
- Book a new appointment
- Check existing appointments
- Cancel an appointment
- Get pointed to the right department based on a described symptom (e.g. "my stomach hurts" → Gastroenterology), then optionally roll straight into booking

The assistant is named **Aradhya Mishra** in the default prompts and greetings — feel free to rename it for your deployment.

---

## How it works

The system is a real-time voice pipeline with a fast, deterministic conversation core:

```
Caller audio
   │
   ▼
Transport (WebRTC or WebSocket/Exotel PSTN)
   │
   ▼
STT (Cartesia)              — speech → text
   │
   ▼
TurnManager                 — merges fragmented STT finals into complete user turns
   │
   ▼
GroqProcessor                — orchestrates the turn
   ├─ GroqLLM.extract_intent()   → single fast Groq call (openai/gpt-oss-120b)
   │                                turns free text into structured JSON:
   │                                intent + entities (doctor, hospital, date,
   │                                time, patient name, phone, address,
   │                                specialization, etc.)
   └─ HospitalHandler.process_intent()
                                → pure-Python state machine that merges
                                  entities into conversation state, queries
                                  Postgres (Neon), and returns the exact
                                  reply text — no LLM used for the actual
                                  response, which keeps latency low and
                                  answers factually grounded in the DB
   │
   ▼
TTS (Cartesia)               — text → speech
   │
   ▼
Transport output → caller
```

Key design choice: **the LLM is only used to extract structured intent from what the caller said.** The actual reply text, appointment logic, date/time normalization, department matching, and database reads/writes are all handled in plain Python (`hospital_handler.py`, `hospital_db.py`). This keeps responses fast, deterministic, and immune to hallucination about hospitals, doctors, fees, or appointment status. The LLM is explicitly instructed to never diagnose, prescribe, or invent medical information — it may only map a described symptom to one of a fixed list of departments.

---

## Project structure

```
Hospital-Assistant-main/
├── bot.py                    # Entry point for local/dev testing over WebRTC
├── server_telephony.py       # Entry point for PSTN calls over WebSocket (Exotel/Tata Smartflo)
├── setup_db.py                # Creates/resets the `appointments` table in Postgres
├── requirements.txt
├── .gitignore
└── processor/
    ├── __init__.py
    ├── frames.py               # Custom Pipecat frame: UserTurnFrame
    ├── manager.py               # TurnManager: merges fragmented STT output into full turns
    ├── llm.py                   # GroqLLM: intent/entity extraction + phone-number normalizer
    ├── llm_processor.py         # GroqProcessor: pipeline glue between STT turns and TTS output
    ├── hospital_handler.py      # Conversation state machine (booking/cancel/lookup/navigation flows)
    └── hospital_db.py           # Postgres access layer (hospitals, doctors, appointments)
```

---

## Prerequisites

- Python 3.10+
- A [Neon](https://neon.tech) (or any) Postgres database
- API keys for:
  - **Cartesia** (STT + TTS)
  - **Groq** (intent extraction LLM)
- Existing `hospital` and `doctors` tables populated in your database (this repo only creates/manages the `appointments` table — see [Database](#database) below)

---

## Setup

1. **Clone and install dependencies**

   ```bash
   git clone <this-repo-url>
   cd Hospital-Assistant-main
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Create a `.env` file** in the project root:

   ```env
   CARTESIA_API_KEY=your_cartesia_api_key
   GROQ_API_KEY=your_groq_api_key
   NEON_DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require
   ```

3. **Set up the database**

   `setup_db.py` (re)creates the `appointments` table:

   ```bash
   python setup_db.py
   ```

   > ⚠️ This **drops and recreates** the `appointments` table — don't run it against a database with appointments you want to keep.

   Your database must also contain `hospital` and `doctors` tables, referenced throughout `processor/hospital_db.py`, with (at minimum) columns like:

   - `hospital(id, name, address)`
   - `doctors(id, name, specialization, fee, schedule, hospital_id)`

   Populate these yourself with your hospital's real data before running the bot. For patient navigation to work well, `specialization` values should align with the six supported departments (see [Patient navigation](#patient-navigation) below).

---

## Running

### Option 1 — Local/dev testing over WebRTC

```bash
python bot.py
```

This uses Pipecat's runner to spin up a WebRTC-based session you can connect to for local testing (see [Pipecat's runner docs](https://github.com/pipecat-ai/pipecat) for connecting a client).

### Option 2 — Telephony (PSTN via Exotel / Tata Smartflo)

```bash
python server_telephony.py
```

This starts a WebSocket server on `ws://0.0.0.0:8082` that expects an Exotel/Tata Smartflo-compatible audio stream (8kHz, mono). Point your telephony provider's voice stream webhook at this server. The server loops indefinitely, handling one call at a time and resetting its pipeline after each call ends.

---

## Conversation flow

- **Greeting** — first user turn always triggers a canned greeting.
- **Intent extraction** — every subsequent user turn is sent to Groq (`openai/gpt-oss-120b`) with a system prompt that returns strict JSON: `intent`, and any entities mentioned (`doctor_name`, `hospital_name`, `specialization`, `appointment_date`, `appointment_time`, `patient_name`, `phone`, `address`, `confirmation`).
- **State machine** — `HospitalHandler` merges new entities into persistent per-call state and executes the matching flow:
  - `list_hospitals`, `list_doctors`, `specialization_information`, `fee_information`, `schedule_information`, `hospital_information`, `check_availability` → read-only DB lookups
  - `patient_navigation` → maps a described symptom to one of six fixed departments (Cardiology, Gastroenterology, Orthopaedics, Gynaecology, General Medicine, Dermatology), lists matching doctors, and asks if the caller wants to book; a "yes" rolls straight into the booking flow
  - `book_appointment` → multi-turn slot-filling (doctor/hospital, date, time, patient name, phone, address) followed by a confirmation step and DB insert
  - `cancel_appointment` → looks up the caller's bookings and cancels the confirmed one
  - `check_appointment` → looks up existing bookings by phone number
  - `cancel_booking_process` → aborts an in-progress booking/cancellation
  - `unrelated` → politely redirects the caller back to hospital-related topics
- **Turn merging** — `TurnManager` buffers likely-incomplete STT finals (e.g., trailing "and", "at", "on") for a short window (`timeout=0.18s`) before emitting a full user turn, to avoid acting on truncated speech.
- **Call end** — certain phrases ("bye", "that's all", etc.) or a completed cancel/booking flow can end the call by emitting `[END_CALL]`, which triggers an `EndFrame` to gracefully close the pipeline.

### Patient navigation

If a caller describes a symptom instead of naming a department directly (e.g. "I have chest pain" or "my stomach hurts"), the LLM is instructed to classify it into exactly one of six supported specializations — **Cardiology, Gastroenterology, Orthopaedics, Gynaecology, General Medicine, or Dermatology** — and never to diagnose, prescribe, or invent medical advice. `HospitalHandler` then looks up doctors in that department, reads back their names/fees/schedules, and asks if the caller wants to book. A "yes" hands off directly into the existing booking flow with the specialization pre-filled.

---

## Notes & known behaviors

- Date/time parsing (`hospital_db.normalize_date` / `normalize_time`) supports relative dates ("today", "tomorrow", weekday names, "27 August"), and includes a heuristic that treats ambiguous `12 AM` as `12 PM` and bare hours 1–7 as PM, since these are typical outpatient hours.
- Phone numbers are normalized from spoken word sequences (e.g., "double four", "oh", "nine") into digit strings by `llm.py::normalize_phone`.
- Fees are coerced to whole rupees (`int(float(fee))`) when read back to the caller, and the final booking confirmation reads the phone number's last four digits one digit at a time for clarity.
- The Postgres connection in `hospital_db.py` is a single lazily-created global connection reused across requests, with a `warmup()` call fired at startup to avoid first-call latency.
- TTS voice defaults to a Cartesia "British Lady" voice (`voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22"`) — swap this out in `bot.py` / `server_telephony.py` for a different voice.

--

