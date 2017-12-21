import copy
from functools import partial
import hook
import json
import os
import requests
import sync
from sync import process_and_run_steps
import sys
import threading

def get_pr_diff(test, pull_request):
    if 'diff' in test:
        diff_file = test['diff']
    else:
        diff_file = str(pull_request['number']) + '.diff'
    with open(os.path.join('tests', diff_file)) as f:
        return f.read()

with open('tests.json') as f:
    tests = json.loads(f.read())

config = {
    'servo_org': 'servo',
    'username': 'servo-wpt-sync',
    'upstream_org': 'jdm',
    'port': 5000,
}

for test in tests:
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())

    print(test['name'] + ':'),
    executed = []
    def callback(step):
        global executed
        executed += [step.name]
    def error_callback(dir_name):
        print('saved error snapshot: %s' % dir_name)
    process_and_run_steps(config,
                          test['db'],
                          payload,
                          partial(get_pr_diff, test),
                          True,
                          step_callback=callback,
                          error_callback=error_callback)
    if executed == test['expected']:
        print 'passed'
    else:
        print
        print executed
        print 'vs'
        print test['expected']
        assert(executed == test['expected'])

class ServerThread(object):
    def __init__(self, config):
        thread = threading.Thread(target=self.run, args=(config,))
        thread.daemon = True
        thread.start()

    def run(self, config):
        hook.main(config, {})

print('testing server hook with /test')

for (i, test) in enumerate(tests):
    print(test['name'] + ':'),

    this_config = copy.deepcopy(config)
    this_config['port'] += i
    server = ServerThread(this_config)

    with open(os.path.join('tests', test['payload'])) as f:
        payload = f.read()

    r = requests.post('http://localhost:' + str(this_config['port']) + '/test', data={'payload': payload})
    if r.status_code != 204:
        print(r.status_code)
    assert(r.status_code == 204)
    r = requests.post('http://localhost:' + str(this_config['port']) + '/shutdown')
    if r.status_code != 204:
        print(r.status_code)
    assert(r.status_code == 204)

    print('passed')
