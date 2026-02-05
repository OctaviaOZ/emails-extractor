
import sys
import os
# Add current directory to path to allow importing from app
sys.path.append(os.path.join(os.getcwd(), "smart-job-tracker"))
from app.models import ApplicationStatus

def sanitize_test(data):
    status_val = data.get('status')
    if status_val:
        # Match logic in app/services/extractor.py
        status_upper = str(status_val).upper().strip()
        valid_values = ApplicationStatus.all_values()
        if status_upper in valid_values:
            data['status'] = ApplicationStatus(status_upper)
        else:
            data['status'] = ApplicationStatus.UNKNOWN
    else:
        data['status'] = ApplicationStatus.UNKNOWN
    return data

# Test Case: LLM returns Title Case
llm_output = {"status": "Applied"}
result = sanitize_test(llm_output)
print(f"LLM Output: {llm_output}")
print(f"Sanitized: {result}")
if result['status'] == ApplicationStatus.UNKNOWN:
    print("BUG CONFIRMED: 'Applied' was converted to 'UNKNOWN'")
else:
    print("FIX VERIFIED: 'Applied' was correctly converted to 'APPLIED'")

