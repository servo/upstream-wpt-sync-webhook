import locale
import os
import subprocess
import sys
import json
import tempfile

from functools import partial
from mock_github_api_server import MockGitHubAPIServer
from sync import CreateOrUpdateBranchForPRStep, SyncRun, WPTSync, git

def setup_mock_repo(tmp_dir, repo_name):
    print("=" * 80)
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


TMP_DIR = tempfile.mkdtemp()
TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
SYNC = WPTSync(
        servo_repo='servo/servo',
        wpt_repo='wpt/wpt',
        downstream_wpt_repo='servo-wpt-sync/wpt',
        servo_path=setup_mock_repo(TMP_DIR, "servo-mock"),
        wpt_path=setup_mock_repo(TMP_DIR, "wpt-mock"),
        github_api_token='',
        github_api_url='http://localhost:9000',
        github_username='servo-wpt-sync',
        github_email='servo-wpt-sync',
        github_name='servo-wpt-sync@servo.org',
        suppress_force_push=True,
)

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


def setup_servo_repository_with_mock_pr_data(test, pull_request):
    servo_path = SYNC.servo_path

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
            cwd=servo_path,
            env={'GIT_COMMITTER_NAME': author.encode(locale.getpreferredencoding()),
                 'GIT_COMMITTER_EMAIL': email})


def git_callback(test):
    commits = git(["log", "--oneline", "-%d" % len(test['commits'])], cwd=SYNC.wpt_path)
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

with open(os.path.join(TESTS_DIR, 'git_tests.json')) as f:
    git_tests = json.loads(f.read())
    for test in git_tests:
        for commit in test['commits']:
            with open(commit['diff']) as f:
                commit['diff'] = f.read()

        pull_request = SYNC.servo.get_pull_request(test['pr_number'])
        step = CreateOrUpdateBranchForPRStep({'number': test['pr_number']}, pull_request)
        step._create_or_update_branch_for_pr(
            SyncRun(SYNC, pull_request, None, None),
            test['commits'],
            pre_commit_callback=partial(git_callback, test)
        )

print('=' * 80)
print(f'Result: PASSED'),
print('=' * 80)


with open(os.path.join(TESTS_DIR, 'tests.json')) as f:
    tests = json.loads(f.read())

for (i, test) in enumerate(filter(lambda x: not x.get('disabled', False), tests)):
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())

    print()
    print('=' * 80)
    print(f'Running "{test["name"]}"'),
    print('=' * 80)

    print(f'Mocking application of PR to servo.')
    pull_request = payload['pull_request']
    setup_servo_repository_with_mock_pr_data(test, pull_request);

    port = 9000 + i
    SYNC.github_api_url = 'http://localhost:' + str(port)

    prs = []
    for (branch, pr_number) in test["existing_prs"].items():
        prs.append((SYNC.downstream_wpt.get_branch(branch).get_pr_head_reference_for_repo(SYNC.wpt), pr_number))

    server = MockGitHubAPIServer(port, prs)

    executed = []
    def callback(step):
        global executed
        executed += [step.name]
    def pre_commit_callback():
        commit = pr_commits(test, pull_request).pop(0)
        last_commit = git(["log", "-1", "--format=%an %ae %s"], cwd=SYNC.wpt_path).rstrip()
        expected = "%s %s %s" % (commit[1], commit[2], commit[3])
        assert last_commit == expected, "%s != %s" % (last_commit, expected)

    SYNC.run(payload, step_callback=callback, pre_commit_callback=pre_commit_callback)
    server.shutdown()

    expected = test['expected']
    equal = len(expected) == len(executed) and all([
        v[0] == v[1] for v in zip(executed, expected)
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
