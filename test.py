import hook
from hook import process_json_payload
import json
import sys

with open(sys.argv[1]) as f:
    payload = json.loads(f.read())

if len(sys.argv) > 2:
    hook.pr_db = json.loads(sys.argv[2])

process_json_payload(payload)
