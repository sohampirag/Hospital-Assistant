import sys
import os

# Append the current directory so we can import processor
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

from processor.hospital_handler import _resolve_doctor
from processor.hospital_db import warmup

warmup()

print("Resolving 'Dr. Chaudhary':", _resolve_doctor("Dr. Chaudhary"))
print("Resolving 'Chaudhary':", _resolve_doctor("Chaudhary"))
