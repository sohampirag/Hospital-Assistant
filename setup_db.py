import os
from processor import hospital_db
from dotenv import load_dotenv

load_dotenv(override=True)

def setup_db():
    with hospital_db.get_connection() as conn:
        with conn.cursor() as cur:
            # Recreate appointments table with correct schema
            cur.execute("DROP TABLE IF EXISTS appointments")
            
            cur.execute("""
                CREATE TABLE appointments (
                    id SERIAL PRIMARY KEY,
                    patient_name VARCHAR(255),
                    phone VARCHAR(50),
                    address TEXT,
                    doctor_name VARCHAR(255),
                    appointment_date VARCHAR(50),
                    appointment_time VARCHAR(50),
                    status VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
        conn.commit()
    print("Appointments table recreated.")

if __name__ == "__main__":
    setup_db()
