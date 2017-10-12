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

API = "https://api.github.com/"
UPSTREAM_PULLS = API + ("repos/%s/web-platform-tests/pulls" % config['upstream_org'])
UPSTREAMABLE_PATH = 'tests/wpt/web-platform-tests'
NO_SYNC_SIGNAL = '[no-wpt-sync]'

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
    if response.status_code / 100 != 2:
        raise ValueError('got unexpected %d response: %s' % (response.status_code, response.text))
    return response


def git(*args, **kwargs):
    command_line = ["git"] + list(*args)
    print(' '.join(map(lambda x: ('"%s"' % x) if ' ' in x else x, command_line)))
    return subprocess.check_output(command_line, cwd=kwargs['cwd'])


def upstream(servo_pr_number, commits):
    BRANCH_NAME = "servo_export_%s" % servo_pr_number

    def upstream_inner(commits):
        PATCH_FILE = 'tmp.patch'
        STRIP_COUNT = UPSTREAMABLE_PATH.count('/') + 1

        # Ensure WPT clone is up to date.
        git(["checkout", "master"], cwd=config['wpt_path'])
        git(["pull", "origin", "master"], cwd=config['wpt_path'])

        # Create a new branch with a unique name that is consistent between updates of the same PR
        git(["checkout", "-b", BRANCH_NAME], cwd=config['wpt_path'])

        for commit in commits:
            # Export the current diff to a file
            with open(os.path.join(config['wpt_path'], PATCH_FILE), 'w') as f:
                f.write(commit['diff'])

            # Apply the exported commit
            git(["apply", PATCH_FILE, "--include", UPSTREAMABLE_PATH, "-p", str(STRIP_COUNT)],
                cwd=config['wpt_path'])

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


def change_upstream_pr(upstream, state):
    data = {
        'state': state
    }
    return authenticated('PATCH',
                         UPSTREAM_PULLS + '/' + str(upstream),
                         json=data)

    
def merge_upstream_pr(upstream):
    modify_upstream_pr_labels('DELETE', ['do not merge yet'], upstream)
    data = {
        'merge_method': 'merge',
    }
    return authenticated('PUT',
                         UPSTREAM_PULLS + '/' + str(upstream) + '/merge',
                         json=data)


def modify_upstream_pr_labels(method, labels, pr_number):
    authenticated(method,
                  API + ('repos/%s/web-platform-tests/pull/%d/labels' %
                         (config['upstream_org'], result["id"])),
                  json=labels)


def open_upstream_pr(pr_number, title, source_org, branch, body):
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
    pr_db[pr_number] = result["id"]
    pr_url = result["html_url"]
    modify_upstream_pr_labels('POST', ['servo-export', 'do not merge yet'], pr_number)
    return pr_url


def comment_on_pr(pr_number, upstream_url):
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


def pr_contains_upstreamable_changes(pull_request):
    r = requests.get(pull_request["diff_url"])
    return patch_contains_upstreamable_changes(r.text)


def fetch_upstreamable_commits(pull_request):
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

def process_new_pr_contents(pull_request):
    pr_number = str(pull_request['number'])
    # Is this updating an existing pull request?
    if pr_number in pr_db:
        if pr_contains_upstreamable_changes(pull_request):
            # Retrieve the set of commits that need to be transplanted.
            commits = fetch_upstreamable_commits(pull_request)
            # Push the relevant changes to the upstream branch.
            upstream(pr_number, commits)
            # In case this is adding new upstreamable changes to a PR that was closed
            # due to a lack of upstreamable changes, force it to be reopened.
            change_upstream_pr(pr_db[pr_number], 'opened')
        else:
            # Close the upstream PR, since would contain no changes otherwise.
            change_upstream_pr(pr_db[pr_number], 'closed')
    elif pr_contains_upstreamable_changes(pull_request):
        # Retrieve the set of commits that need to be transplanted.
        commits = fetch_upstreamable_commits(pull_request)
        # Push the relevant changes to a new upstream branch.
        branch = upstream(pr_number, commits)
        # TODO: extract the non-checklist/reviewable parts of the pull request body
        #       and add it to the upstream body.
        body = "Reviewed in %s." % (SERVO_PR_URL % (config['servo_org'], pr_number))
        # Create a pull request against the upstream repository for the new branch.
        upstream_url = open_upstream_pr(pr_number, pull_request['title'], config['username'], branch, body)
        # Leave a comment to the new pull request in the original pull request.
        comment_on_pr(pr_number, upstream_url)

    
def process_closed_pr(pull_request):
    pr_number = str(pull_request['number'])
    if not pr_number in pr_db:
        # If we don't recognize this PR, it never contained upstreamable changes.
        return
    if pull_request['merged']:
        # Since the upstreamable changes have now been merged locally, merge the
        # corresponding upstream PR.
        merge_upstream_pr(pr_db[pr_number])
        pr_db.pop(pr_number)
    else:
        # If a PR with upstreamable changes is closed without being merged, we
        # don't want to merge the changes upstream either.
        change_upstream_pr(pr_db[pr_number], 'closed')

        
def process_json_payload(payload):
    pull_request = payload['pull_request']
    if NO_SYNC_SIGNAL in pull_request['body']:
        return

    if payload['action'] in ['opened', 'synchronized']:
        process_new_pr_contents(pull_request)
    elif payload['action'] == 'closed':
        process_closed_pr(pull_request)    


@app.route("/")
def index():
    return "Hi!"


@app.route("/hook", methods=["POST"])
def webhook():
    payload = request.args.get('payload', "{}")
    process_json_payload(payload)
    with open('pr_map.json', 'w') as f:
        f.write(json.dumps(pr_map))
    return ('', 204)


def main(port=None):
    app.run(port=port)


if __name__ == "__main__":
    import sys
    port = sys.argv[1] if len(sys.argv) > 1 else None
    main(port)
