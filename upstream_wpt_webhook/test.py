import hook
from hook import process_json_payload
import os
import json
import sys

def get_pr_diff(pull_request):
    with open(os.path.join('tests', str(pull_request['number']) + '.diff')) as f:
        return f.read()

with open('tests.json') as f:
    tests = json.loads(f.read())

for test in tests:
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())
    hook.pr_db = test['db']

    print(test['name'] + ':'),
    steps = map(lambda x: x.name, process_json_payload(payload, get_pr_diff))
    if steps == test['expected']:
        print 'passed'
    else:
        assert(steps == test['expected'])
