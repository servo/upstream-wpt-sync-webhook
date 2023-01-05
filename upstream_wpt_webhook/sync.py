import copy
from functools import partial
import json
import os
import requests
import sys
import subprocess
import time
import traceback
try:
    import urlparse
except ImportError:
    from urllib import parse as urlparse

GITHUB_PR_URL = "https://github.com/%s/pull/%s"
GITHUB_COMMENTS_URL = "repos/%s/issues/%s/comments"
GITHUB_PULLS_URL = "repos/%s/pulls"
UPSTREAMABLE_PATH = 'tests/wpt/web-platform-tests/'
NO_SYNC_SIGNAL = '[no-wpt-sync]'

def upstream_pulls(config):
    return GITHUB_PULLS_URL % config['wpt_repo']

def branch_head_for_upstream(config, branch):
    downstream_wpt_org = config['downstream_wpt_repo'].split('/')[0]
    if downstream_wpt_org != config['wpt_repo'].split('/')[0]:
        return f"{downstream_wpt_org}:{branch}"
    else:
        return branch

def wpt_branch_name_from_servo_pr_number(servo_pr_number):
    return f"servo_export_{servo_pr_number}"

class Step:
    def __init__(self, name):
        self.name = name

    def provides(self):
        return {}

    def run(self):
        pass


class AsyncValue:
    _value = None

    def resolve(self, value):
        self._value = value

    def value(self):
        assert(self._value != None)
        return self._value


def authenticated(config, method, url, json=None):
    s = requests.Session()
    if not method:
        method = 'GET'
    s.headers = {
        'Authorization': 'token %s' % config['token'],
        'User-Agent': 'Servo web-platform-test sync service',
    }

    # Ensure that any URLs retrieved are rewritten to use the configured API host.
    parts = urlparse.urlsplit(url)
    url = urlparse.urlunsplit(['', '', parts[2], parts[3], parts[4]])
    url = urlparse.urljoin(config['api'], url)

    print('  → Request: %s' % url)
    response = s.request(method, url, json=json)
    if int(response.status_code / 100) != 2:
        raise ValueError('got unexpected %d response: %s' % (response.status_code, response.text))
    return response


def git(*args, **kwargs):
    command_line = ["git"] + list(*args)
    cwd = kwargs['cwd']
    print(f"  → Execution (cwd='{cwd}'): {' '.join(command_line)}")

    try:
        out = subprocess.check_output(command_line, cwd=cwd, env=kwargs.get('env', {}))
        return out.decode('utf-8')
    except subprocess.CalledProcessError as e:
        print(e.output)
        raise e


class UpstreamStep(Step):
    def __init__(self, servo_pr_number, commits, pre_commit_callback):
        Step.__init__(self, 'UpstreamStep')
        self.servo_pr_number = servo_pr_number
        self.commits = commits
        self.pre_commit_callback = pre_commit_callback

    def provides(self):
        self.branch = AsyncValue()
        return {'branch': self.branch}

    def run(self, config):
        commits = self.commits.value()
        branch = _upstream(config, self.servo_pr_number, commits, self.pre_commit_callback)
        self.branch.resolve(branch)
        self.name += ':%d:%s' % (len(commits), branch)


def upstream(servo_pr_number, commits, pre_commit_callback, steps):
    step = UpstreamStep(servo_pr_number, commits, pre_commit_callback)
    steps += [step]
    return step.provides()['branch']

def _upstream(config, servo_pr_number, commits, pre_commit_callback, pre_delete_callback=None):
    branch_name = wpt_branch_name_from_servo_pr_number(servo_pr_number)

    def upstream_inner(config, commits):
        PATCH_FILE = 'tmp.patch'
        STRIP_COUNT = UPSTREAMABLE_PATH.count('/') + 1

        # Create a new branch with a unique name that is consistent between updates of the same PR
        wpt_path = config['wpt_path']
        git(["checkout", "-b", branch_name], cwd=wpt_path)

        patch_path = os.path.join(wpt_path, PATCH_FILE)

        for commit in commits:
            # Export the current diff to a file
            with open(patch_path, 'w') as f:
                f.write(commit['diff'])

            # Apply the filtered changes
            git(["apply", PATCH_FILE, "-p", str(STRIP_COUNT)], cwd=wpt_path)

            # Ensure the patch file is not added with the other changes.
            os.remove(patch_path)

            # Commit the changes
            git(["add", "--all"], cwd=wpt_path)
            git(["commit", "--message", commit['message'],
                 "--author", commit['author']],
                cwd=wpt_path,
                env={'GIT_COMMITTER_NAME': 'Servo WPT Sync',
                     'GIT_COMMITTER_EMAIL': 'josh+wptsync@joshmatthews.net'})

            if pre_commit_callback:
                pre_commit_callback()

        # Push the branch upstream (forcing to overwrite any existing changes)
        if not config.get('suppress_force_push', False):
            remote_url = "https://{user}:{token}@github.com/{repo}.git".format(
                user=config['username'],
                token=config['token'],
                repo=config['downstream_wpt_repo'],
            )
            git(["push", "-f", remote_url, branch_name], cwd=config['wpt_path'])

        return branch_name

    try:
        result = upstream_inner(config, commits)
        if pre_delete_callback:
            pre_delete_callback(git)
        return result
    except Exception as e:
        raise e
    finally:
        try:
            git(["checkout", "master"], cwd=config['wpt_path'])
            git(["branch", "-D", branch_name], cwd=config['wpt_path'])
        except:
            pass


class ChangeUpstreamStep(Step):
    def __init__(self, upstream, state, title):
        Step.__init__(self, 'ChangeUpstreamStep:%s:%s:%s' % (upstream, state, title) )
        self.upstream = upstream
        self.state = state
        self.title = title

    def run(self, config):
        _change_upstream_pr(config, self.upstream, self.state, self.title)


def change_upstream_pr(upstream, state, title, steps):
    steps += [ChangeUpstreamStep(upstream, state, title)]

def _change_upstream_pr(config, upstream, state, title):
    data = {
        'state': state,
        'title': title
    }
    return authenticated(config,
                         'PATCH',
                         upstream_pulls(config) + '/' + str(upstream),
                         json=data)


class MergeUpstreamStep(Step):
    def __init__(self, upstream):
        Step.__init__(self, 'MergeUpstreamStep:' + str(upstream))
        self.upstream = upstream

    def run(self, config):
        _merge_upstream_pr(config, self.upstream)


def merge_upstream_pr(upstream, steps):
    steps += [MergeUpstreamStep(upstream)]

def _merge_upstream_pr(config, upstream):
    remove_upstream_pr_label(config, 'do not merge yet', str(upstream))
    data = {
        'merge_method': 'rebase',
    }
    return authenticated(config,
                         'PUT',
                         upstream_pulls(config) + '/' + str(upstream) + '/merge',
                         json=data)


def remove_upstream_pr_label(config, label, pr_number):
    authenticated(config,
                  'DELETE',
                  ('repos/%s/issues/%s/labels/%s' %
                   (config['wpt_repo'], pr_number, label)))


def modify_upstream_pr_labels(config, method, labels, pr_number):
    authenticated(config,
                  method,
                  ('repos/%s/issues/%s/labels' %
                   (config['wpt_repo'], pr_number)),
                  json=labels)


class OpenUpstreamStep(Step):
    def __init__(self, title, branch, body):
        Step.__init__(self, 'OpenUpstreamStep')
        self.title = title
        self.branch = branch
        self.body = body

    def provides(self):
        self.new_pr_url = AsyncValue()
        return {'pr_url': self.new_pr_url}

    def run(self, config):
        pr_url = _open_upstream_pr(config,
                                   self.title,
                                   self.branch.value(),
                                   self.body)
        self.new_pr_url.resolve(pr_url)


def open_upstream_pr(title, branch, body, steps):
    step = OpenUpstreamStep(title, branch, body)
    steps += [step]
    return step.provides()['pr_url']

def _open_upstream_pr(config, title, branch, body):
    data = {
        'title': title,
        'head': branch_head_for_upstream(config, branch),
        'base': 'master',
        'body': body,
        'maintainer_can_modify': False,
    }
    r = authenticated(config,
                      'POST',
                      upstream_pulls(config),
                      json=data)
    result = r.json()
    new_pr_number = result["number"]
    modify_upstream_pr_labels(config, 'POST', ['servo-export', 'do not merge yet'], new_pr_number)
    return result["html_url"]


def get_upstream_pr_number_from_pr(config, pull_request):
    branch = wpt_branch_name_from_servo_pr_number(pull_request['number'])
    head = branch_head_for_upstream(config, branch)
    result = authenticated(config,
                          'GET',
                          f"{upstream_pulls(config)}?head={head}&base=master&state=open")
    result = result.json()
    if not result or not type(result) is list:
        return None

    return str(result[0]["number"])


class CommentStep(Step):
    def __init__(self, pr_number, upstream_url, extra):
        Step.__init__(self, 'CommentStep')
        self.pr_number = pr_number
        self.upstream_url = upstream_url
        self.extra = extra

    def run(self, config):
        upstream_url = self.upstream_url.value() if isinstance(self.upstream_url, AsyncValue) else self.upstream_url
        self.name += ':' + _comment_on_pr(config, self.pr_number, self.extra, upstream_url)


def comment_on_pr(pr_number, upstream_url, extra, steps):
    step = CommentStep(pr_number, upstream_url, extra)
    steps += [step]


def _do_comment_on_pr(config, pr_number, body):
    data = {
        'body': body,
    }
    return authenticated(config,
                         'POST',
                         GITHUB_COMMENTS_URL % (config['servo_repo'], pr_number),
                         json=data)


def _comment_on_pr(config, pr_number, upstream_url, extra):
    body = '%s\n\nCompleted upstream sync of web-platform-test changes at %s.' % (
        upstream_url, extra)
    _do_comment_on_pr(config, pr_number, body)
    return body


def patch_contains_upstreamable_changes(config, pull_request):
    output = git([
        "diff",
         f"HEAD~{pull_request['commits']}",
        '--', UPSTREAMABLE_PATH
    ], cwd=config["servo_path"])
    return len(output) > 0


class FetchUpstreamableStep(Step):
    def __init__(self, pull_request):
        Step.__init__(self, 'FetchUpstreamableStep')
        self.pull_request = pull_request

    def provides(self):
        self.commits = AsyncValue()
        return {'commits': self.commits}

    def run(self, config):
        commits = _fetch_upstreamable_commits(config, self.pull_request)
        self.name += ':%d' % len(commits)
        self.commits.resolve(commits)


def fetch_upstreamable_commits(pull_request, steps):
    step = FetchUpstreamableStep(pull_request)
    steps += [step]
    return step.provides()['commits']


def _fetch_upstreamable_commits(config, pull_request):
    path = config["servo_path"]
    number_of_commits = pull_request["commits"]

    commit_shas = git(["log", "--pretty=%H", f"-{number_of_commits}"], cwd=path).splitlines()
    filtered_commits = []
    for sha in commit_shas:
        # Specifying the path here does a few things. First, it exludes any
        # changes that do not touch WPT files at all. Secondly, when a file is
        # moved in or out of the WPT directory the filename which is outside the
        # directory becomes /dev/null and the change becomes an addition or
        # deletion. This makes the patch usable on the WPT repository itself.
        # TODO: If we could cleverly parse and manipulate the full commit diff
        # we could avoid cloning the servo repository altogether and only
        # have to fetch the commit diffs from GitHub.
        diff = git(["show", "--binary", "--format=%b", sha, '--',  UPSTREAMABLE_PATH], cwd=path)

        # Retrieve the diff of any changes to files that are relevant
        if diff:
            # Create an object that contains everything necessary to transplant this
            # commit to another repository.
            filtered_commits += [{
                'author': git(["show", "-s", "--pretty=%an <%ae>", sha], cwd=path),
                'message': git(["show", "-s", "--pretty=%B", sha], cwd=path),
                'diff': diff,
            }]
    return filtered_commits


def process_new_pr_contents(config, pull_request, pre_commit_callback, steps):
    pr_number = str(pull_request['number'])
    upstream_pr_number = get_upstream_pr_number_from_pr(config, pull_request)
    is_upstreamable = patch_contains_upstreamable_changes(config, pull_request)

    if upstream_pr_number:
        if is_upstreamable:
            # In case this is adding new upstreamable changes to a PR that was closed
            # due to a lack of upstreamable changes, force it to be reopened.
            # Github refuses to reopen a PR that had a branch force pushed, so be sure
            # to do this first.
            change_upstream_pr(upstream_pr_number, 'opened', pull_request['title'], steps)
            # Retrieve the set of commits that need to be transplanted.
            commits = fetch_upstreamable_commits(pull_request, steps)
            # Push the relevant changes to the upstream branch.
            upstream(pr_number, commits, pre_commit_callback, steps)
            extra_comment = 'Transplanted upstreamable changes to existing PR.'
        else:
            # Close the upstream PR, since would contain no changes otherwise.
            change_upstream_pr(upstream_pr_number, 'closed', pull_request['title'], steps)
            extra_comment = 'No upstreamable changes; closed existing PR.'
        comment_on_pr(pr_number,
                      '%s#%s' % (config['wpt_repo'], upstream_pr_number),
                      extra_comment, steps)
    elif is_upstreamable:
        # Retrieve the set of commits that need to be transplanted.
        commits = fetch_upstreamable_commits(pull_request, steps)
        # Push the relevant changes to a new upstream branch.
        branch = upstream(pr_number, commits, pre_commit_callback, steps)
        # TODO: extract the non-checklist/reviewable parts of the pull request body
        #       and add it to the upstream body.
        body = "Reviewed in %s." % (GITHUB_PR_URL % (config['servo_repo'], pr_number))
        # Create a pull request against the upstream repository for the new branch.
        upstream_url = open_upstream_pr(pull_request['title'], branch, body, steps)
        # Leave a comment to the new pull request in the original pull request.
        comment_on_pr(pr_number, upstream_url, 'Opened new PR for upstreamable changes.', steps)


def change_upstream_pr_title(config, pull_request, steps):
    upstream_pr_number = get_upstream_pr_number_from_pr(config, pull_request)
    if upstream_pr_number:
        change_upstream_pr(upstream_pr_number, 'open', pull_request['title'], steps)

        extra_comment = 'PR title changed; changed existing PR.'
        comment_on_pr(upstream_pr_number,
                      '%s#%s' % (config['wpt_repo'], upstream_pr_number),
                      extra_comment, steps)


def process_closed_pr(config, pull_request, steps):
    upstream_pr_number = get_upstream_pr_number_from_pr(config, pull_request)
    if not upstream_pr_number:
        # If we don't recognize this PR, it never contained upstreamable changes.
        return
    if pull_request['merged']:
        # Since the upstreamable changes have now been merged locally, merge the
        # corresponding upstream PR.
        merge_upstream_pr(upstream_pr_number, steps)
    else:
        # If a PR with upstreamable changes is closed without being merged, we
        # don't want to merge the changes upstream either.
        change_upstream_pr(upstream_pr_number, 'closed', pull_request['title'], steps)


def process_json_payload(config, payload, pre_commit_callback):
    pull_request = payload['pull_request']
    if NO_SYNC_SIGNAL in pull_request.get('body', ''):
        return []

    steps = []
    if payload['action'] in ['opened', 'synchronize', 'reopened']:
        process_new_pr_contents(config, pull_request,
                                pre_commit_callback, steps)
    elif payload['action'] == 'edited' and 'title' in payload['changes']:
        change_upstream_pr_title(config, pull_request, steps)
    elif payload['action'] == 'closed':
        process_closed_pr(config, pull_request, steps)
    return steps


def save_snapshot(payload, exception_info):
    name = 'error-snapshot-%s' % int(round(time.time() * 1000))
    os.mkdir(name)
    with open(os.path.join(name, 'payload.json'), 'w') as f:
        f.write(json.dumps(payload, indent=2))
    with open(os.path.join(name, 'exception'), 'w') as f:
        f.write(''.join(exception_info))
    return name


def process_and_run_steps(config, payload, step_callback=None, error_callback=None, pre_commit_callback=None):
    try:
        steps = process_json_payload(config, payload, pre_commit_callback)
        for step in steps:
            step.run(config)
            if step_callback:
                step_callback(step)
        return True
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        info = traceback.format_exception(exc_type, exc_value, exc_traceback)
        dir_name = save_snapshot(payload, info)
        if error_callback:
            error_callback(dir_name)
        return False
