from functools import partial
import hook
from hook import process_json_payload
import os
import json
import sys

def get_pr_diff(test, pull_request):
    if 'diff' in test:
        diff_file = test['diff']
    else:
        diff_file = str(pull_request['number']) + '.diff'
    with open(os.path.join('tests', diff_file)) as f:
        return f.read()

with open('tests.json') as f:
    tests = json.loads(f.read())

for test in tests:
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())
    hook.pr_db = test['db']

    print(test['name'] + ':'),
    steps = map(lambda x: x.name, process_json_payload(payload, partial(get_pr_diff, test)))
    if steps == test['expected']:
        print 'passed'
    else:
        print
        print steps
        print 'vs'
        print test['expected']
        assert(steps == test['expected'])
