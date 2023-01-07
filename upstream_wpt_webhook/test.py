import locale
import os
import subprocess
import sys
import json
import tempfile

from functools import partial
from mock_github_api_server import MockGitHubAPIServer
from sync import CreateOrUpdateBranchForPRStep, SyncRun, WPTSync

TMP_DIR = tempfile.mkdtemp()
TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
SYNC = WPTSync(
        servo_repo='servo/servo',
        wpt_repo='wpt/wpt',
        downstream_wpt_repo='servo-wpt-sync/wpt',
        servo_path=os.path.join(TMP_DIR, "servo-mock"),
        wpt_path=os.path.join(TMP_DIR, "wpt-mock"),
        github_api_token='',
        github_api_url='http://localhost:9000',
        github_username='servo-wpt-sync',
        github_email='servo-wpt-sync',
        github_name='servo-wpt-sync@servo.org',
        suppress_force_push=True,
)

def setup_mock_repo(repo_name, local_repo):
    print("=" * 80)
    print(f"Setting up {repo_name} Git repositry")
    subprocess.run(["cp", "-R", "-p", os.path.join(TESTS_DIR, repo_name), local_repo.path])
    local_repo.run("init", "-b", "master")
    local_repo.run("add", ".")
    local_repo.run("commit", "-a", "-m", "Initial commit")
setup_mock_repo("servo-mock", SYNC.local_servo_repo)
setup_mock_repo("wpt-mock", SYNC.local_wpt_repo)


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
    first_commit_hash = SYNC.local_servo_repo.run("rev-list", "HEAD").splitlines()[-1]
    SYNC.local_servo_repo.run("reset", "--hard", first_commit_hash)
    SYNC.local_servo_repo.run("clean", "-fxd")

    # Apply each commit to the repository.
    for commit in pr_commits(test, pull_request):
        patch_file, author, email, message = commit
        SYNC.local_servo_repo.run("apply", os.path.join(TESTS_DIR, patch_file))
        SYNC.local_servo_repo.run("add", ".")
        SYNC.local_servo_repo.run("commit", "-a", "--author", f"{author} <{email}>", "-m", message,
            env={'GIT_COMMITTER_NAME': author.encode(locale.getpreferredencoding()),
                 'GIT_COMMITTER_EMAIL': email})


def git_callback(test):
    commits = SYNC.local_wpt_repo.run("log", "--oneline", f"-{len(test['commits'])}")
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
    SYNC.run(
        payload,
        step_callback=lambda step: executed.append(step.name)
    )
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
