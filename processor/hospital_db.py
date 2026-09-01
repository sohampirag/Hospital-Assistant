import os
import psycopg
from datetime import datetime, timedelta

_global_conn = None

def get_connection():
    global _global_conn
    if _global_conn is None or _global_conn.closed:
        url = os.getenv("NEON_DATABASE_URL")
        if not url:
            raise ValueError("NEON_DATABASE_URL is missing from .env")
        _global_conn = psycopg.connect(url, autocommit=True)
    return _global_conn

import re

def normalize_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str).lower().strip()
    date_str = date_str.replace(" of ", " ")
    
    # Try YYYY-MM-DD directly
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    today = datetime.now()
    if "today" in date_str or "आज" in date_str:
        return today.strftime("%Y-%m-%d")
    if "tomorrow" in date_str or "कल" in date_str:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
        
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(days):
        if day in date_str:
            days_ahead = i - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }
    
    found_month = None
    for m in months:
        if m in date_str:
            found_month = months[m]
            break
            
    day_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', date_str)
    
    if found_month and day_match:
        day = int(day_match.group(1))
        year = today.year
        if found_month < today.month:
            year += 1
        try:
            return datetime(year, found_month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass

    # If parsing fails, do NOT silently book for today.
    # Return None so the handler rejects the invalid date slot and reprompts.
    return None

def normalize_time(time_str):
    if not time_str:
        return None
    time_str = str(time_str).upper().strip()
    
    # Strip dots from a.m. / p.m.
    time_str = re.sub(r'([AP])\.M\.', r'\1M', time_str)
    
    # Add space before AM/PM if missing (e.g. 1PM -> 1 PM)
    time_str = re.sub(r'(\d)(AM|PM)', r'\1 \2', time_str)
    
    # STT often confuses 12 PM for 12 AM. Nobody books a doctor at midnight.
    if "12 AM" in time_str:
        time_str = time_str.replace("12 AM", "12 PM")
        
    try:
        t = datetime.strptime(time_str, "%I %p")
        return t.strftime("%H:%M:00")
    except:
        try:
            t = datetime.strptime(time_str, "%I:%M %p")
            return t.strftime("%H:%M:00")
        except:
            pass
            
    # Fallback for bare numbers like "4" or "4:30"
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?$", time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        # If hour is between 1 and 7, assume PM for doctor appointments
        if 1 <= hour <= 7:
            hour += 12
        return f"{hour:02d}:{minute:02d}:00"
        
    return "00:00:00"

def warmup():
    try:
        get_connection()
        print("[DB] Warmup successful.")
    except Exception as e:
        print(f"[DB ERROR] Warmup failed: {e}")

def get_all_context_string():
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, address FROM hospital")
            hospitals = cur.fetchall()
            
            cur.execute("SELECT name, specialization, fee, schedule, hospital_id FROM doctors")
            doctors = cur.fetchall()
            
            context = "AVAILABLE HOSPITALS:\n"
            for h in hospitals:
                context += f"- {h[1]} (Address: {h[2]})\n"
            
            context += "\nAVAILABLE DOCTORS:\n"
            h_map = {h[0]: h[1] for h in hospitals}
            for d in doctors:
                h_name = h_map.get(d[4], "Unknown Hospital")
                context += f"- Dr. {d[0]} (Specialist: {d[1]}, Fee: {d[2]}, Schedule: {d[3]}, Hospital: {h_name})\n"
            return context
    except Exception as e:
        print(f"[DB ERROR] Could not fetch context for LLM: {e}")
        return ""

def get_hospitals():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT name, address FROM hospital")
        return cur.fetchall()

def get_doctors():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT name, specialization, fee, schedule FROM doctors")
        return cur.fetchall()

def get_doctors_by_specialization(specialization, hospital_name=None):
    conn = get_connection()
    with conn.cursor() as cur:
        if hospital_name:
            cur.execute("""
                SELECT d.name, d.fee, d.schedule, h.name 
                FROM doctors d
                JOIN hospital h ON d.hospital_id = h.id
                WHERE d.specialization ILIKE %s AND h.name ILIKE %s
            """, (f"%{specialization}%", f"%{hospital_name}%"))
        else:
            cur.execute("""
                SELECT d.name, d.fee, d.schedule, h.name 
                FROM doctors d
                JOIN hospital h ON d.hospital_id = h.id
                WHERE d.specialization ILIKE %s
            """, (f"%{specialization}%",))
        return cur.fetchall()

def get_all_specializations():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT specialization FROM doctors WHERE specialization IS NOT NULL")
        return [row[0] for row in cur.fetchall()]

def get_doctor_by_name(doctor_name):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.id, d.name, d.specialization, d.fee, d.schedule, h.name 
            FROM doctors d
            JOIN hospital h ON d.hospital_id = h.id
            WHERE d.name ILIKE %s
        """, (f"%{doctor_name}%",))
        return cur.fetchone()

def get_doctors_by_hospital(hospital_name):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.name, d.specialization, d.fee, d.schedule, h.name 
            FROM doctors d 
            JOIN hospital h ON d.hospital_id = h.id 
            WHERE h.name ILIKE %s
        """, (f"%{hospital_name}%",))
        return cur.fetchall()

def check_slot_available(doctor_name, appointment_date, appointment_time):
    norm_date = normalize_date(appointment_date)
    norm_time = normalize_time(appointment_time)
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM appointments 
            WHERE doctor_name ILIKE %s 
            AND appointment_date = %s 
            AND appointment_time = %s 
            AND status = 'booked'
        """, (f"%{doctor_name}%", norm_date, norm_time))
        return cur.fetchone() is None

def create_appointment(patient_name, phone, address, doctor_name, appointment_date, appointment_time):
    norm_date = normalize_date(appointment_date)
    norm_time = normalize_time(appointment_time)
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO appointments 
            (patient_name, phone, address, doctor_name, appointment_date, appointment_time, status) 
            VALUES (%s, %s, %s, %s, %s, %s, 'booked')
            RETURNING id
        """, (patient_name, phone, address, doctor_name, norm_date, norm_time))
        res = cur.fetchone()
        return res[0] if res else None

def get_appointment_by_id(app_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, doctor_name, appointment_date, appointment_time, status, patient_name, phone, address 
            FROM appointments 
            WHERE id = %s
        """, (app_id,))
        return cur.fetchone()

def get_patient_appointments(phone):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, a.doctor_name, a.appointment_date, a.appointment_time, a.status, h.name, a.patient_name 
            FROM appointments a
            LEFT JOIN doctors d ON d.name ILIKE a.doctor_name
            LEFT JOIN hospital h ON d.hospital_id = h.id
            WHERE a.phone = %s AND a.status = 'booked'
        """, (phone,))
        return cur.fetchall()

def get_appointment_for_cancellation(patient_name, phone):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, a.doctor_name, a.appointment_date, a.appointment_time, a.status, h.name, a.patient_name 
            FROM appointments a
            LEFT JOIN doctors d ON d.name ILIKE a.doctor_name
            LEFT JOIN hospital h ON d.hospital_id = h.id
            WHERE a.phone = %s AND a.patient_name ILIKE %s AND a.status = 'booked'
        """, (phone, f"%{patient_name}%"))
        return cur.fetchall()

def cancel_appointment(appointment_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE appointments 
            SET status = 'cancelled' 
            WHERE id = %s AND status = 'booked'
        """, (appointment_id,))
        
        if cur.rowcount == 0:
            return False
            
        cur.execute("SELECT status FROM appointments WHERE id = %s", (appointment_id,))
        res = cur.fetchone()
        if res and res[0] == 'cancelled':
            return True
        return False


def verify_appointment_booked(app_id):
    """Returns True only if the appointment exists in DB with status='booked'."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM appointments WHERE id = %s", (app_id,))
        row = cur.fetchone()
        return row is not None and row[0] == 'booked'


def verify_appointment_cancelled(app_id):
    """Returns True only if the appointment exists in DB with status='cancelled'."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM appointments WHERE id = %s", (app_id,))
        row = cur.fetchone()
        return row is not None and row[0] == 'cancelled'


def get_hospital_address(hospital_name):
    """Returns (name, address) for a hospital matched by name."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, address FROM hospital WHERE name ILIKE %s",
            (f"%{hospital_name}%",)
        )
        return cur.fetchone()


