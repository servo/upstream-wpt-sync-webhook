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

base_wpt_dir = tempfile.mkdtemp()
git(["clone", "--depth=1", "https://github.com/jdm/web-platform-tests-mock.git"], cwd=base_wpt_dir)
git(["clone", "--depth=1", "https://github.com/jdm/servo-mock.git"], cwd=base_wpt_dir)

config = {
    'servo_org': 'servo',
    'username': 'servo-wpt-sync',
    'upstream_org': 'jdm',
    'port': 5000,
    'token': '',
    'api': 'http://localhost:9000',
    'override_host': 'http://localhost:9000',
    'suppress_force_push': True,
    'wpt_path': os.path.join(base_wpt_dir, "web-platform-tests-mock"),
    'servo_path': os.path.join(base_wpt_dir, "servo-mock"),
}

def git_callback(test, git):
    commits = git(["log", "--oneline", "-%d" % len(test['commits'])], cwd=config['wpt_path'])
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

with open('git_tests.json') as f:
    git_tests = json.loads(f.read())
for test in git_tests:
    for commit in test['commits']:
        with open(commit['diff']) as f:
            commit['diff'] = f.read()
    pr_number = test['pr_number']
    _upstream(config, pr_number, test['commits'], None, partial(git_callback, test))
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
    def __init__(self, config, port):
        #print('Starting API server on port ' + str(port))
        self.port = port
        thread = threading.Thread(target=self.run, args=(config,))
        thread.daemon = True
        thread.start()
        wait_for_server(self.port)

    def run(self, config):
        start_server(self.port, config)

    def shutdown(self):
        r = requests.post('http://localhost:%d/shutdown' % self.port)
        assert(r.status_code == 204)
        # Wait for the server to stop responding to ping requests
        while True:
            try:
                r = requests.get('http://localhost:' + str(port) + '/ping')
                time.sleep(0.5)
            except:
                break
        #print('Stopped API server on port ' + str(self.port))

def pr_diff_files(test, pull_request):
    def fake_commit_data(filename):
        return [filename, "tmp author", "tmp@tmp.com", "tmp commit message"]

    if 'diff' in test:
        if isinstance(test['diff'], list):
            return list(map(lambda d: fake_commit_data(d) if not isinstance(d, list) else d,
                            test['diff']))
        diff_file = test['diff']
    else:
        diff_file = str(pull_request['number']) + '.diff'
    return [fake_commit_data(diff_file)]


def get_pr_diff(test, pull_request):
    diff_files = pr_diff_files(test, pull_request)
    diffs = []
    for diff_file_props in diff_files:
        with open(os.path.join('tests', diff_file_props[0])) as f:
            diffs += [f.read()]
    return '\n'.join(diffs)


with open('tests.json') as f:
    tests = json.loads(f.read())

def make_api_config(test, payload, servo_path):
    api_config = {
        "diff_files": pr_diff_files(test, payload["pull_request"]),
        "servo_path": servo_path,
    }
    api_config.update(test.get('api_config', {}))
    return api_config

for (i, test) in enumerate(filter(lambda x: not x.get('disabled', False), tests)):
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())

    port = 9000 + i
    config['api'] = 'http://localhost:' + str(port)
    config['override_host'] = config['api']
    server = APIServerThread(make_api_config(test, payload, config['servo_path']), port)

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
    def pre_commit_callback(commits_to_check):
        commit = commits_to_check.pop(0)
        last_commit = git(["log", "-1", "--format=%an %ae %s"], cwd=config['wpt_path']).rstrip()
        expected = "%s %s %s" % (commit[1], commit[2], commit[3])
        assert last_commit == expected, "%s != %s" % (last_commit, expected)
    commits = pr_diff_files(test, payload['pull_request'])
    result = process_and_run_steps(config,
                                   test['db'],
                                   payload,
                                   partial(get_pr_diff, test),
                                   "master",
                                   step_callback=callback,
                                   error_callback=error_callback,
                                   pre_commit_callback=partial(pre_commit_callback, commits))
    server.shutdown()
    if result and all(map(lambda values: values[0] == values[1]
                          if ':' not in values[1] else values[0].startswith(values[1]),
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
        #print('Starting server thread on port ' + str(config['port']))
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
        #print('Stopped server thread on port ' + str(self.port))

print('testing server hook with /test')

for (i, test) in enumerate(tests):
    print(test['name'] + ':'),

    this_config = copy.deepcopy(config)
    this_config['port'] += i
    port += i
    this_config['api'] = 'http://localhost:' + str(port)
    this_config['override_host'] = this_config['api']
    server = ServerThread(this_config)

    with open(os.path.join('tests', test['payload'])) as f:
        payload = f.read()

    api_server = APIServerThread(make_api_config(test, json.loads(payload), config['servo_path']), port)

    r = requests.post('http://localhost:' + str(this_config['port']) + '/test', data={'payload': payload})
    if r.status_code != 204:
        print(r.status_code)
    assert(r.status_code == 204)
    server.shutdown()
    api_server.shutdown()

    print('passed')
