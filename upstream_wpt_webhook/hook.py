#!/usr/bin/env python

from flask import Flask, request, jsonify, render_template, make_response, abort
import json
import os
import requests
import sys
import subprocess

app = Flask(__name__)
pr_db = {}
try:
    with open('pr_map.json') as f:
        pr_db = json.loads(f.read())
except:
    pass

with open('config.json') as f:
    config = json.loads(f.read())
    assert 'token' in config
    assert 'username' in config
    assert 'wpt_path' in config
    assert 'upstream_org' in config
    assert 'servo_org' in config
    assert 'port' in config

API = "https://api.github.com/"
UPSTREAM_PULLS = API + ("repos/%s/web-platform-tests/pulls" % config['upstream_org'])
UPSTREAMABLE_PATH = 'tests/wpt/web-platform-tests/'
NO_SYNC_SIGNAL = '[no-wpt-sync]'

class Step:
    def __init__(self, name):
        self.name = name

    def provides(self):
        return {}


class AsyncValue:
    _value = None

    def resolve(self, value):
        self._value = value

    def value(self):
        assert(self._value)
        return self._value


def authenticated(method, url, json=None):
    s = requests.Session()
    if not method:
        method = 'GET'
    s.headers = {
        'Authorization': 'token %s' % config['token'],
        'User-Agent': 'Servo web-platform-test sync service',
    }
    print('fetching %s' % url)
    response = s.request(method, url, json=json)
    if int(response.status_code / 100) != 2:
        raise ValueError('got unexpected %d response: %s' % (response.status_code, response.text))
    return response


def git(*args, **kwargs):
    command_line = ["git"] + list(*args)
    print(' '.join(map(lambda x: ('"%s"' % x) if ' ' in x else x, command_line)))
    return subprocess.check_output(command_line, cwd=kwargs['cwd'])


class UpstreamStep(Step):
    def __init__(self, servo_pr_number, commits):
        Step.__init__(self, 'UpstreamStep')
        self.servo_pr_number = servo_pr_number
        self.commits = commits

    def provides(self):
        self.branch = AsyncValue()
        return {'branch': self.branch}

    def run(self):
        branch = _upstream(self.servo_pr_number, self.commits.value())
        self.branch.resolve(branch)


def upstream(servo_pr_number, commits, steps):
    step = UpstreamStep(servo_pr_number, commits)
    steps += [step]
    return step.provides()['branch']

def _upstream(servo_pr_number, commits):
    BRANCH_NAME = "servo_export_%s" % servo_pr_number

    def upstream_inner(commits):
        PATCH_FILE = 'tmp.patch'
        STRIP_COUNT = UPSTREAMABLE_PATH.count('/') + 1

        # Ensure shallow WPT clone is up to date.
        git(["checkout", "master"], cwd=config['wpt_path'])
        git(["fetch", "origin", "master", "--depth", "1"], cwd=config['wpt_path'])
        git(["reset", "--hard", "origin/master"], cwd=config['wpt_path'])

        # Create a new branch with a unique name that is consistent between updates of the same PR
        git(["checkout", "-b", BRANCH_NAME], cwd=config['wpt_path'])

        patch_path = os.path.join(config['wpt_path'], PATCH_FILE)

        for commit in commits:
            # Export the current diff to a file
            with open(patch_path, 'w') as f:
                f.write(commit['diff'])

            # Remove all non-WPT changes from the diff.
            filtered = subprocess.check_output(["filterdiff",
                                                "-p", "1",
                                                "-i", UPSTREAMABLE_PATH + "*",
                                                PATCH_FILE],
                                               cwd=config['wpt_path'])
            with open(patch_path, 'w') as f:
                f.write(filtered.decode("utf-8"))

            # Apply the filtered changes
            git(["apply", PATCH_FILE, "-p", str(STRIP_COUNT)], cwd=config['wpt_path'])

            # Ensure the patch file is not added with the other changes.
            os.remove(patch_path)

            # Commit the changes
            git(["add", "--all"], cwd=config['wpt_path'])
            git(["commit", "--message", commit['message'], "--author", commit['author']],
                cwd=config['wpt_path'])

        remote_url = "https://{user}:{token}@github.com/{user}/web-platform-tests.git".format(
            user=config['username'],
            token=config['token'],
        )

        # Push the branch upstream (forcing to overwrite any existing changes)
        git(["push", "-f", remote_url, BRANCH_NAME], cwd=config['wpt_path'])
        return BRANCH_NAME

    try:
        return upstream_inner(commits)
    except Exception as e:
        raise e
    finally:
        try:
            git(["checkout", "master"], cwd=config['wpt_path'])
            git(["branch", "-D", BRANCH_NAME], cwd=config['wpt_path'])
        except:
            pass


class ChangeUpstreamStep(Step):
    def __init__(self, upstream, state):
        Step.__init__(self, 'ChangeUpstreamStep')
        self.upstream = upstream
        self.state = state

    def run(self):
        self._change_upstream_pr(self.upstream, self.state)


def change_upstream_pr(upstream, state, steps):
    steps += [ChangeUpstreamStep(upstream, state)]

def _change_upstream_pr(upstream, state):
    data = {
        'state': state
    }
    return authenticated('PATCH',
                         UPSTREAM_PULLS + '/' + str(upstream),
                         json=data)


class MergeUpstreamStep(Step):
    def __init__(step, upstream):
        Step.__init__(self, 'MergeUpstreamStep')
        self.upstream = upstream

    def run(self):
        _merge_upstream_pr(self.upstream)


def merge_upstream_pr(upstream, steps):
    steps += [MergeUpstreamStep(upstream)]

def _merge_upstream_pr(upstream):
    modify_upstream_pr_labels('DELETE', ['do not merge yet'], upstream)
    data = {
        'merge_method': 'merge',
    }
    return authenticated('PUT',
                         UPSTREAM_PULLS + '/' + str(upstream) + '/merge',
                         json=data)


def modify_upstream_pr_labels(method, labels, pr_number):
    authenticated(method,
                  API + ('repos/%s/web-platform-tests/issues/%s/labels' %
                         (config['upstream_org'], pr_number)),
                  json=labels)


class OpenUpstreamStep(Step):
    def __init__(self, pr_number, title, source_org, branch, body):
        Step.__init__(self, 'OpenUpstreamStep')
        self.pr_number = pr_number
        self.title = title
        self.source_org = source_org
        self.branch = branch
        self.body = body

    def provides(self):
        self.new_pr_url = AsyncValue()
        return {'pr_url': self.new_pr_url}

    def run(self):
        pr_url = _open_upstream_pr(self.pr_number,
                                   self.title,
                                   self.source_org,
                                   self.branch.value(),
                                   self.body)
        self.new_pr_url.resolve(pr_url)


def open_upstream_pr(pr_number, title, source_org, branch, body, steps):
    step = OpenUpstreamStep(pr_number, title, source_org, branch, body)
    steps += [step]
    return step.provides()['pr_url']

def _open_upstream_pr(pr_number, title, source_org, branch, body):
    data = {
        'title': title,
        'head': (config['username'] + ':' + branch) if source_org != config['upstream_org'] else branch,
        'base': 'master',
        'body': body,
        'maintainer_can_modify': False,
    }
    r = authenticated('POST',
                      UPSTREAM_PULLS,
                      json=data)
    result = r.json()
    pr_db[pr_number] = result["number"]
    pr_url = result["html_url"]
    modify_upstream_pr_labels('POST', ['servo-export', 'do not merge yet'], pr_db[pr_number])
    return pr_url


class CommentStep(Step):
    def __init__(self, pr_number, upstream_url):
        Step.__init__(self, 'CommentStep')
        self.pr_number = pr_number
        self.upstream_url = upstream_url

    def run(self):
        return comment_on_pr(self.pr_number, self.upstream_url.value())


def comment_on_pr(pr_number, upstream_url, steps):
    step = CommentStep(pr_number, upstream_url)
    steps += [step]

def _comment_on_pr(pr_number, upstream_url):
    data = {
        'body': 'Upstream web-platform-test changes at %s.' % upstream_url,
    }
    return authenticated('POST',
                         API + ('repos/%s/servo/issues/%s/comments' % (config['servo_org'], pr_number)),
                         json=data)


def patch_contains_upstreamable_changes(patch_contents):
    for line in patch_contents.splitlines():
        if line.startswith("diff --git") and UPSTREAMABLE_PATH in line:
            return True
    return False


def get_pr_diff(pull_request):
    return requests.get(pull_request["diff_url"]).text


class FetchUpstreamableStep(Step):
    def __init__(self, pull_request):
        Step.__init__(self, 'FetchUpstreamableStep')
        self.pull_request = pull_request

    def provides(self):
        self.commits = AsyncValue()
        return {'commits': self.commits}

    def run(self):
        commits = _fetch_upstreamable_commits(sef.pull_request)
        self.commits.resolve(commits)


def fetch_upstreamable_commits(pull_request, steps):
    step = FetchUpstreamableStep(pull_request)
    steps += [step]
    return step.provides()['commits']

def _fetch_upstreamable_commits(pull_request):
    r = authenticated('GET', pull_request["commits_url"])
    commit_data = r.json()
    filtered_commits = []
    for commit in commit_data:
        r = authenticated('GET', commit['url'])
        commit_body = r.json()
        for file in commit_body['files']:
            if UPSTREAMABLE_PATH in file['filename']:
                # Retrieve the diff of this commit.
                r = requests.get(commit['html_url'] + '.diff')
                # Create an object that contains everything necessary to transplant this
                # commit to another repository.
                filtered_commits += [{
                    'author': "%s <%s>" % (commit['commit']['author']['name'],
                                           commit['commit']['author']['email']),
                    'message': commit['commit']['message'],
                    'diff': r.text,
                }]
                break
    return filtered_commits


SERVO_PR_URL = "https://github.com/%s/servo/pulls/%s"

def process_new_pr_contents(pull_request, pr_diff, steps):
    pr_number = str(pull_request['number'])
    # Is this updating an existing pull request?
    if pr_number in pr_db:
        if patch_contains_upstreamable_changes(pr_diff):
            # Retrieve the set of commits that need to be transplanted.
            commits = fetch_upstreamable_commits(pull_request, steps)
            # Push the relevant changes to the upstream branch.
            upstream(pr_number, commits, steps)
            # In case this is adding new upstreamable changes to a PR that was closed
            # due to a lack of upstreamable changes, force it to be reopened.
            change_upstream_pr(pr_db[pr_number], 'opened', steps)
        else:
            # Close the upstream PR, since would contain no changes otherwise.
            change_upstream_pr(pr_db[pr_number], 'closed', steps)
    elif patch_contains_upstreamable_changes(pr_diff):
        # Retrieve the set of commits that need to be transplanted.
        commits = fetch_upstreamable_commits(pull_request, steps)
        # Push the relevant changes to a new upstream branch.
        branch = upstream(pr_number, commits, steps)
        # TODO: extract the non-checklist/reviewable parts of the pull request body
        #       and add it to the upstream body.
        body = "Reviewed in %s." % (SERVO_PR_URL % (config['servo_org'], pr_number))
        # Create a pull request against the upstream repository for the new branch.
        upstream_url = open_upstream_pr(pr_number, pull_request['title'], config['username'], branch, body, steps)
        # Leave a comment to the new pull request in the original pull request.
        comment_on_pr(pr_number, upstream_url, steps)

    
def process_closed_pr(pull_request, steps):
    pr_number = str(pull_request['number'])
    if not pr_number in pr_db:
        # If we don't recognize this PR, it never contained upstreamable changes.
        return
    if pull_request['merged']:
        # Since the upstreamable changes have now been merged locally, merge the
        # corresponding upstream PR.
        merge_upstream_pr(pr_db[pr_number], steps)
        pr_db.pop(pr_number)
    else:
        # If a PR with upstreamable changes is closed without being merged, we
        # don't want to merge the changes upstream either.
        change_upstream_pr(pr_db[pr_number], 'closed', steps)

        
def process_json_payload(payload, diff_provider):
    pull_request = payload['pull_request']
    if NO_SYNC_SIGNAL in pull_request['body']:
        return []

    steps = []
    if payload['action'] in ['opened', 'synchronized']:
        process_new_pr_contents(pull_request, diff_provider(pull_request), steps)
    elif payload['action'] == 'closed':
        process_closed_pr(pull_request, steps)
    return steps


@app.route("/")
def index():
    return "Hi!"


@app.route("/hook", methods=["POST"])
def webhook():
    payload = request.form.get('payload', '{}')
    process_json_payload(json.loads(payload), get_pr_diff)
    with open('pr_map.json', 'w') as f:
        f.write(json.dumps(pr_db))
    return ('', 204)


def main():
    app.run(port=config['port'])


if __name__ == "__main__":
    main()
