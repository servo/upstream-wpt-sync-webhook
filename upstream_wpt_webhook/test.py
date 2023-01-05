import itertools
import locale
import os
import subprocess
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

import copy
from functools import partial
import json
import requests
import sync
from sync import branch_head_for_upstream, process_and_run_steps, UPSTREAMABLE_PATH, _create_or_update_branch_for_pr, git
from test_api_server import start_server
import threading
import time
import tempfile

TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")

def setup_mock_repo(tmp_dir, repo_name):
    print(f"Setting up {repo_name} Git repositry")
    repo_path = os.path.join(tmp_dir, repo_name)
    subprocess.run(["cp", "-R", "-p", os.path.join(TESTS_DIR, repo_name), tmp_dir])
    git(["init", "-b", "master"], cwd=repo_path)
    git(["add", "."], cwd=repo_path)
    git(["commit", "-a",
         "-m", "Initial commit",
         "--author", "Fake <fake@fake.com>",
        ],
        cwd=repo_path,
        env={'GIT_COMMITTER_NAME': 'Fake', 'GIT_COMMITTER_EMAIL': 'fake@fake.com'}
    )
    return repo_path


def pr_commits(test, pull_request):
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


def setup_servo_repository_with_mock_pr_data(config, test, pull_request):
    servo_path = config["servo_path"]

    # Clean up any old files.
    first_commit_hash = git(["rev-list", "HEAD"], cwd=servo_path).splitlines()[-1]
    git(["reset", "--hard", first_commit_hash], cwd=servo_path)
    git(["clean", "-fxd"], cwd=servo_path)

    # Apply each commit to the repository.
    for commit in pr_commits(test, pull_request):
        patch_file, author, email, message = commit
        git(["apply", os.path.join(TESTS_DIR, patch_file)], cwd=servo_path)
        git(["add", "."], cwd=servo_path)
        git(["commit", "-a", "--author", f"{author} <{email}>", "-m", message],
            cwd=config['servo_path'],
            env={'GIT_COMMITTER_NAME': author.encode(locale.getpreferredencoding()),
                 'GIT_COMMITTER_EMAIL': email})


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


config = {
    'servo_repo': 'servo/servo',
    'wpt_repo': 'wpt/wpt',
    'downstream_wpt_repo': 'servo-wpt-sync/wpt',
    'username': 'servo-wpt-sync',
    'port': 5000,
    'token': '',
    'api': 'http://localhost:9000',
    'override_host': 'http://localhost:9000',
    'suppress_force_push': True,
}

print('=' * 80)
print(f'Setting up mock repositories and running Git tests'),
print('=' * 80)

tmp_dir = tempfile.mkdtemp()
config['servo_path'] = setup_mock_repo(tmp_dir, "servo-mock")
config['wpt_path'] = setup_mock_repo(tmp_dir, "wpt-mock")

with open(os.path.join(TESTS_DIR, 'git_tests.json')) as f:
    git_tests = json.loads(f.read())
for test in git_tests:
    for commit in test['commits']:
        with open(commit['diff']) as f:
            commit['diff'] = f.read()
    pull_request = {'number': test['pr_number']}
    _create_or_update_branch_for_pr(config, pull_request, test['commits'], None,
                                    partial(git_callback, test))

print('=' * 80)
print(f'Result: PASSED'),
print('=' * 80)

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


with open(os.path.join(TESTS_DIR, 'tests.json')) as f:
    tests = json.loads(f.read())

def make_api_config(config, test, payload):
    pr_database = {}
    for (branch, pr_number) in test["existing_prs"].items():
        pr_database[(branch_head_for_upstream(config, branch), 'master')] = pr_number

    api_config = {
        "servo_path": config["servo_path"],
        "pr_database": pr_database
    }
    api_config.update(test.get('api_config', {}))
    return api_config

for (i, test) in enumerate(filter(lambda x: not x.get('disabled', False), tests)):
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())

    print()
    print('=' * 80)
    print(f'Running "{test["name"]}"'),
    print('=' * 80)

    print(f'Mocking application of PR to servo.')
    pull_request = payload['pull_request']
    setup_servo_repository_with_mock_pr_data(config, test, pull_request);

    port = 9000 + i
    config['api'] = 'http://localhost:' + str(port)
    config['override_host'] = config['api']
    server = APIServerThread(make_api_config(config, test, payload), port)

    executed = []
    def callback(step):
        global executed
        executed += [step.name]
    def pre_commit_callback():
        commit = pr_commits(test, pull_request).pop(0)
        last_commit = git(["log", "-1", "--format=%an %ae %s"], cwd=config['wpt_path']).rstrip()
        expected = "%s %s %s" % (commit[1], commit[2], commit[3])
        assert last_commit == expected, "%s != %s" % (last_commit, expected)

    result = process_and_run_steps(config,
                                   payload,
                                   step_callback=callback,
                                   pre_commit_callback=pre_commit_callback)
    server.shutdown()


    expected = test['expected']
    equal = len(expected) == len(executed) and all([
        v[0] == v[1] or (':' in v[0] and v[0].startswith(v[1]))
        for v in zip(executed, expected)
    ])

    print('=' * 80)
    if equal:
        print(f'Test result: PASSED'),
    else:
        print(f'Test result: FAILED'),
        print()
        print(executed)
        print('vs')
        print(test['expected'])
        print('=' * 80)

    assert(equal)
