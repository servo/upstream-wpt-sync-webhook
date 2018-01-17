from sync import process_and_run_steps
import json
import os
import sys

if len(sys.argv) != 2:
    print("usage: python replay.py [snapshot_dir]")
    sys.exit(1)

snapshot_dir = sys.argv[1]

with open(os.path.join(snapshot_dir, "payload.json")) as f:
    payload = json.loads(f.read())

with open(os.path.join(snapshot_dir, "pr_db.json")) as f:
    db = json.loads(f.read())

with open(os.path.join(snapshot_dir, "pr.diff")) as f:
    pr_diff = f.read()

config = {
    'servo_org': 'servo',
    'username': 'servo-wpt-sync',
    'upstream_org': 'jdm',
    'port': 5000,
}

def get_pr_diff(pull_request):
    return pr_diff

error = False
def error_callback(dir_name):
    global error
    error = True
    print('saved error snapshot: %s' % dir_name)

process_and_run_steps(config, db, payload, get_pr_diff, True, error_callback=error_callback)
if error:
    sys.exit(1)
