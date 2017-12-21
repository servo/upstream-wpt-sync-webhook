from functools import partial
import sync
from sync import process_and_run_steps
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
    sync.pr_db = test['db']

    print(test['name'] + ':'),
    executed = []
    def callback(step):
        global executed
        executed += [step.name]
    process_and_run_steps(payload, partial(get_pr_diff, test), True, callback)
    if executed == test['expected']:
        print 'passed'
    else:
        print
        print executed
        print 'vs'
        print test['expected']
        assert(executed == test['expected'])
