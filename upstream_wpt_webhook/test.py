import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import copy
from functools import partial
import hook
import json
import requests
import sync
from sync import process_and_run_steps, UPSTREAMABLE_PATH, _upstream, git
from test_api_server import start_server
import threading
import time
import tempfile

do_git_test = len(sys.argv) != 1 and sys.argv[1] == "--git-test"

base_wpt_dir = tempfile.mkdtemp()
if do_git_test:
    git(["clone", "--depth=1", "https://github.com/w3c/web-platform-tests.git"], cwd=base_wpt_dir)

config = {
    'servo_org': 'servo',
    'username': 'servo-wpt-sync',
    'upstream_org': 'jdm',
    'port': 5000,
    'token': '',
    'api': 'http://localhost:9000',
    'override_host': 'http://localhost:9000',
    'suppress_force_push': True,
    'wpt_path': os.path.join(base_wpt_dir, "web-platform-tests"),
}

def git_callback(test, git):
    commits = git(["log", "--oneline", "-%d" % len(test['commits'])], cwd=config['wpt_path'])
    commits = commits.decode("utf-8")
    commits = commits.splitlines()
    if any(map(lambda values: values[1]['message'] not in values[0],
               zip(commits, test['commits']))):
        print
        print("Observed:")
        print(commits)
        print
        print("Expected:")
        print(map(lambda s: s['message'], test['commits']))
        sys.exit(1)

if do_git_test:
    with open('git_tests.json') as f:
        git_tests = json.loads(f.read())
    for test in git_tests:
        for commit in test['commits']:
            with open(commit['diff']) as f:
                commit['diff'] = f.read()
        pr_number = test['pr_number']
        _upstream(config, pr_number, test['commits'], False, partial(git_callback, test))
    print("Successfully ran git upstreaming tests.")

def wait_for_server(port):
    # Wait for server to finish setting up before continuing
    while True:
        try:
            r = requests.get('http://localhost:' + str(port) + '/ping')
            assert(r.status_code == 200)
            assert(r.text == 'pong')
            break
        except:
            time.sleep(0.5)

class APIServerThread(object):
    def __init__(self, config):
        self.port = 9000
        thread = threading.Thread(target=self.run, args=(config,))
        thread.daemon = True
        thread.start()
        wait_for_server(self.port)

    def run(self, config):
        start_server(self.port, config)

    def shutdown(self):
        r = requests.post('http://localhost:%d/shutdown' % self.port)
        assert(r.status_code == 204)


def get_pr_diff(test, pull_request):
    if 'diff' in test:
        diff_file = test['diff']
    else:
        diff_file = str(pull_request['number']) + '.diff'
    with open(os.path.join('tests', diff_file)) as f:
        return f.read()

with open('tests.json') as f:
    tests = json.loads(f.read())

def make_api_config(test, payload):
    is_upstreamable = UPSTREAMABLE_PATH in get_pr_diff(test, payload["pull_request"])
    api_config = {
        "upstreamable_commits": 1 if is_upstreamable else 0,
        "non_upstreamable_commits": 0 if is_upstreamable else 1,
    }
    api_config.update(test.get('api_config', {}))
    return api_config

for test in tests:
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())

    server = APIServerThread(make_api_config(test, payload))

    print(test['name'] + ':'),
    executed = []
    def callback(step):
        global executed
        executed += [step.name]
    def error_callback(dir_name):
        #print('saved error snapshot: %s' % dir_name)
        with open(os.path.join(dir_name, "exception")) as f:
            print(f.read())
        import shutil
        shutil.rmtree(dir_name)
    process_and_run_steps(config,
                          test['db'],
                          payload,
                          partial(get_pr_diff, test),
                          True,
                          step_callback=callback,
                          error_callback=error_callback)
    server.shutdown()
    if all(map(lambda values: values[0] == values[1] if ':' not in values[1] else values[0].startswith(values[1]),
               zip(executed, test['expected']))):
        print('passed')
    else:
        print()
        print(executed)
        print('vs')
        print(test['expected'])
        assert(executed == test['expected'])

class ServerThread(object):
    def __init__(self, config):
        self.port = config['port']
        thread = threading.Thread(target=self.run, args=(config,))
        thread.daemon = True
        thread.start()
        wait_for_server(config['port'])

    def run(self, config):
        hook.main(config, {})

    def shutdown(self):
        r = requests.post('http://localhost:%d/shutdown' % self.port)
        assert(r.status_code == 204)


print('testing server hook with /test')

for (i, test) in enumerate(tests):
    print(test['name'] + ':'),

    this_config = copy.deepcopy(config)
    this_config['port'] += i
    server = ServerThread(this_config)

    with open(os.path.join('tests', test['payload'])) as f:
        payload = f.read()

    api_server = APIServerThread(make_api_config(test, json.loads(payload)))

    r = requests.post('http://localhost:' + str(this_config['port']) + '/test', data={'payload': payload})
    if r.status_code != 204:
        print(r.status_code)
    assert(r.status_code == 204)
    server.shutdown()
    api_server.shutdown()

    print('passed')
