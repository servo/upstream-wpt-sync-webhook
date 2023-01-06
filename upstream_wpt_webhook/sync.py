import copy
from functools import partial
import json
import os
import requests
import sys
import subprocess
import time
import traceback
import typing

try:
    import urlparse
except ImportError:
    from urllib import parse as urlparse

GITHUB_PR_URL = "https://github.com/%s/pull/%s"
GITHUB_COMMENTS_URL = "repos/%s/issues/%s/comments"
GITHUB_PULLS_URL = "repos/%s/pulls"
UPSTREAMABLE_PATH = 'tests/wpt/web-platform-tests/'
NO_SYNC_SIGNAL = '[no-wpt-sync]'

NO_UPSTREAMBLE_CHANGES_COMMENT = "Downstream PR no longer contains any upstreamable changes. Closing PR."
DOWNSTREAM_COMMENT = "%s\n\nCompleted upstream sync of web-platform-test changes at %s"


class RunContext:
    """
    This class stores the context of a single exectuion of this script,
    triggered with one GitHub action. As the run progresses the values
    here are progressively filled in.
    """
    def __init__(self, config):
        self.servo_repo = config['servo_repo']
        self.wpt_repo = config['wpt_repo']
        self.downstream_wpt_repo = config['downstream_wpt_repo']
        self.suppress_force_push = config.get('suppress_force_push', False)

        self.servo_path = config['servo_path']
        self.wpt_path = config['wpt_path']

        self.github_api_token = config['github_api_token']
        self.github_api_url = config['github_api_url']
        self.github_username = config['github_username']
        self.github_email = config['github_email']
        self.github_name = config['github_name']

    def url_for_wpt_upstream_pulls(self):
        return GITHUB_PULLS_URL % self.wpt_repo


def branch_head_for_upstream(context: RunContext, branch):
    downstream_wpt_org = context.downstream_wpt_repo.split('/')[0]
    if downstream_wpt_org != context.wpt_repo.split('/')[0]:
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


def authenticated(context: RunContext, method, url, json=None):
    s = requests.Session()
    if not method:
        method = 'GET'
    s.headers = {
        'Authorization': f'token {context.github_api_token}',
        'User-Agent': 'Servo web-platform-test sync service',
    }

    # Ensure that any URLs retrieved are rewritten to use the configured API host.
    parts = urlparse.urlsplit(url)
    url = urlparse.urlunsplit(['', '', parts[2], parts[3], parts[4]])
    url = urlparse.urljoin(context.github_api_url, url)

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


class CreateOrUpdateBranchForPRStep(Step):
    def __init__(self, pull_request, pre_commit_callback):
        Step.__init__(self, 'CreateOrUpdateBranchForPRStep')
        self.pull_request = pull_request
        self.pre_commit_callback = pre_commit_callback

    def provides(self):
        self.branch = AsyncValue()
        return {'branch': self.branch}

    def run(self, context: RunContext):
        commits = _fetch_upstreamable_commits(context, self.pull_request)
        branch = _create_or_update_branch_for_pr(context, self.pull_request, commits, self.pre_commit_callback)
        self.branch.resolve(branch)
        self.name += ':%d:%s' % (len(commits), branch)


def create_or_update_branch_for_pr(pull_request, pre_commit_callback, steps):
    step = CreateOrUpdateBranchForPRStep(pull_request, pre_commit_callback)
    steps += [step]
    return step.provides()['branch']

def _fetch_upstreamable_commits(context: RunContext, pull_request):
    path = context.servo_path
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

def _create_or_update_branch_for_pr(context: RunContext, pull_request, commits, pre_commit_callback, pre_delete_callback=None):
    branch_name = wpt_branch_name_from_servo_pr_number(pull_request['number'])

    def upstream_inner(Context: RunContext, commits):
        PATCH_FILE = 'tmp.patch'
        STRIP_COUNT = UPSTREAMABLE_PATH.count('/') + 1

        # Create a new branch with a unique name that is consistent between updates of the same PR
        git(["checkout", "-b", branch_name], cwd=context.wpt_path)

        patch_path = os.path.join(context.wpt_path, PATCH_FILE)

        for commit in commits:
            # Export the current diff to a file
            with open(patch_path, 'w') as f:
                f.write(commit['diff'])

            # Apply the filtered changes
            git(["apply", PATCH_FILE, "-p", str(STRIP_COUNT)], cwd=context.wpt_path)

            # Ensure the patch file is not added with the other changes.
            os.remove(patch_path)

            # Commit the changes
            git(["add", "--all"], cwd=context.wpt_path)
            git(["commit", "--message", commit['message'],
                 "--author", commit['author']],
                cwd=context.wpt_path,
                env={'GIT_COMMITTER_NAME': context.github_name,
                     'GIT_COMMITTER_EMAIL': context.github_email})

            if pre_commit_callback:
                pre_commit_callback()

        # Push the branch upstream (forcing to overwrite any existing changes)
        if not context.suppress_force_push:
            remote_url = "https://{user}:{token}@github.com/{repo}.git".format(
                user=context.github_username,
                token=context.github_api_token,
                repo=context.downstream_wpt_repo
            )
            git(["push", "-f", remote_url, branch_name], cwd=context.wpt_path)

        return branch_name

    try:
        result = upstream_inner(context, commits)
        if pre_delete_callback:
            pre_delete_callback(git)
        return result
    except Exception as e:
        raise e
    finally:
        try:
            git(["checkout", "master"], cwd=context.wpt_path)
            git(["branch", "-D", branch_name], cwd=context.wpt_path)
        except:
            pass


class RemoveBranchForPRStep(Step):
    def __init__(self, pull_request):
        Step.__init__(self, 'RemoveBranchForPRStep')
        self.pull_request = pull_request

    def run(self, context: RunContext):
        print("  -> Removing branch used for upstream PR")
        branch_name = wpt_branch_name_from_servo_pr_number(self.pull_request['number'])
        if not context.suppress_force_push:
            git(["push", "origin", "--delete", branch_name], cwd=context.wpt_path)


def remove_branch_for_pr(pull_request, steps):
    steps += [RemoveBranchForPRStep(pull_request)]


class ChangeUpstreamPRStep(Step):
    def __init__(self, upstream, state, title):
        Step.__init__(self, 'ChangeUpstreamPRStep:%s:%s:%s' % (upstream, state, title) )
        self.upstream = upstream
        self.state = state
        self.title = title

    def run(self, context: RunContext):
        _change_upstream_pr(context, self.upstream, self.state, self.title)


def change_upstream_pr(upstream, state, title, steps):
    steps += [ChangeUpstreamPRStep(upstream, state, title)]

def _change_upstream_pr(context: RunContext, upstream, state, title):
    data = {
        'state': state,
        'title': title
    }
    return authenticated(context,
                         'PATCH',
                         context.url_for_wpt_upstream_pulls() + '/' + str(upstream),
                         json=data)


class MergeUpstreamStep(Step):
    def __init__(self, upstream):
        Step.__init__(self, 'MergeUpstreamStep:' + str(upstream))
        self.upstream = upstream

    def run(self, context):
        _merge_upstream_pr(context, self.upstream)


def merge_upstream_pr(upstream, steps):
    steps += [MergeUpstreamStep(upstream)]

def _merge_upstream_pr(context: RunContext, upstream):
    remove_upstream_pr_label(context, 'do not merge yet', str(upstream))
    data = {
        'merge_method': 'rebase',
    }
    return authenticated(context,
                         'PUT',
                         context.url_for_wpt_upstream_pulls() + '/' + str(upstream) + '/merge',
                         json=data)


def remove_upstream_pr_label(context: RunContext, label, pr_number):
    authenticated(context,
                  'DELETE',
                  ('repos/%s/issues/%s/labels/%s' %
                   (context.wpt_repo, pr_number, label)))


def modify_upstream_pr_labels(context: RunContext, method, labels, pr_number):
    authenticated(context,
                  method,
                  ('repos/%s/issues/%s/labels' %
                   (context.wpt_repo, pr_number)),
                  json=labels)


class OpenUpstreamPRStep(Step):
    def __init__(self, title, branch, body):
        Step.__init__(self, 'OpenUpstreamPRStep')
        self.title = title
        self.branch = branch
        self.body = body

    def provides(self):
        self.new_pr_url = AsyncValue()
        return {'pr_url': self.new_pr_url}

    def run(self, context: RunContext):
        pr_url = _open_upstream_pr(context,
                                   self.title,
                                   self.branch.value(),
                                   self.body)
        self.new_pr_url.resolve(pr_url)


def open_upstream_pr(title, branch, body, steps):
    step = OpenUpstreamPRStep(title, branch, body)
    steps += [step]
    return step.provides()['pr_url']

def _open_upstream_pr(context: RunContext, title, branch, body):
    data = {
        'title': title,
        'head': branch_head_for_upstream(context, branch),
        'base': 'master',
        'body': body,
        'maintainer_can_modify': False,
    }
    r = authenticated(context,
                      'POST',
                      context.url_for_wpt_upstream_pulls(),
                      json=data)
    result = r.json()
    new_pr_number = result["number"]
    modify_upstream_pr_labels(context, 'POST', ['servo-export', 'do not merge yet'], new_pr_number)
    return result["html_url"]


def get_upstream_pr_number_from_pr(context: RunContext, pull_request):
    branch = wpt_branch_name_from_servo_pr_number(pull_request['number'])
    head = branch_head_for_upstream(context, branch)
    response = authenticated(context,
                          'GET',
                          f"{context.url_for_wpt_upstream_pulls()}?head={head}&base=master&state=open")
    if int(response.status_code / 100) != 2:
        return None

    result = response.json()
    if not result or not type(result) is list:
        return None
    return str(result[0]["number"])


class DownstreamComment(AsyncValue):
    def __init__(self, upstream_url, text):
        self.upstream_url = upstream_url
        self.text = text

    def value(self):
        if isinstance(self.upstream_url, AsyncValue):
            self.upstream_url = self.upstream_url.value()
        return DOWNSTREAM_COMMENT % (self.text, self.upstream_url)


class UpstreamComment(AsyncValue):
    def __init__(self, text):
        self.text = text

    def value(self):
        return self.text


class CommentStep(Step):
    def __init__(self, repository, pr_number, comment):
        Step.__init__(self, 'CommentStep')
        self.repository = repository
        self.pr_number = pr_number
        self.comment = comment

    def run(self, context: RunContext):
        if isinstance(self.comment, AsyncValue):
            self.comment = self.comment.value()
        self.name += f":{self.repository}:{self.pr_number}:{self.comment}"
        return authenticated(context,
                            'POST',
                            GITHUB_COMMENTS_URL % (self.repository, self.pr_number),
                            json={'body': self.comment})


def comment_on_pr(repository, pr_number, comment, steps):
    step = CommentStep(repository, pr_number, comment)
    steps += [step]



def patch_contains_upstreamable_changes(context: RunContext, pull_request):
    output = git([
        "diff",
         f"HEAD~{pull_request['commits']}",
        '--', UPSTREAMABLE_PATH
    ], cwd=context.servo_path)
    return len(output) > 0



def process_new_pr_contents(context: RunContext, pull_request, pre_commit_callback, steps):
    pr_number = str(pull_request['number'])
    upstream_pr_number = get_upstream_pr_number_from_pr(context, pull_request)
    is_upstreamable = patch_contains_upstreamable_changes(context, pull_request)

    print("Processing new PR contents:")
    print(f"  → PR is upstreamable: '{is_upstreamable}'")
    if upstream_pr_number:
        print(f"  → Detected existing upstream PR #{upstream_pr_number}")

    if upstream_pr_number:
        if is_upstreamable:
            # In case this is adding new upstreamable changes to a PR that was closed
            # due to a lack of upstreamable changes, force it to be reopened.
            # Github refuses to reopen a PR that had a branch force pushed, so be sure
            # to do this first.
            change_upstream_pr(upstream_pr_number, 'opened', pull_request['title'], steps)
            # Push the relevant changes to the upstream branch.
            create_or_update_branch_for_pr(pull_request, pre_commit_callback, steps)
            extra_comment = 'Transplanted upstreamable changes to existing PR.'
        else:
            # Close the upstream PR, since would contain no changes otherwise.
            comment_on_pr(context.wpt_repo, upstream_pr_number,
                          UpstreamComment(NO_UPSTREAMBLE_CHANGES_COMMENT),
                          steps)
            change_upstream_pr(upstream_pr_number, 'closed', pull_request['title'], steps)
            remove_branch_for_pr(pull_request, steps)
            extra_comment = 'No upstreamable changes; closed existing PR.'

        comment_on_pr(context.servo_repo, pr_number,
                      DownstreamComment(
                        f'{context.wpt_repo}#{upstream_pr_number}',
                        extra_comment),
                      steps)
    elif is_upstreamable:
        # Push the relevant changes to a new upstream branch.
        branch = create_or_update_branch_for_pr(pull_request, pre_commit_callback, steps)
        # TODO: extract the non-checklist/reviewable parts of the pull request body
        #       and add it to the upstream body.
        body = "Reviewed in %s." % (GITHUB_PR_URL % (context.servo_repo, pr_number))
        # Create a pull request against the upstream repository for the new branch.
        upstream_url = open_upstream_pr(pull_request['title'], branch, body, steps)
        # Leave a comment to the new pull request in the original pull request.
        comment = DownstreamComment(
            upstream_url,
            'Opened new PR for upstreamable changes.'
        )
        comment_on_pr(context.servo_repo, pr_number, comment, steps)


def change_upstream_pr_title(context: RunContext, pull_request, steps):
    print("Processing PR title change")
    upstream_pr_number = get_upstream_pr_number_from_pr(context, pull_request)
    if upstream_pr_number:
        change_upstream_pr(upstream_pr_number, 'open', pull_request['title'], steps)

        comment = DownstreamComment(
            f'{context.wpt_repo}#{upstream_pr_number}',
            'PR title changed; changed existing PR.'
        )
        comment_on_pr(context.servo_repo, pull_request["number"], comment, steps)


def process_closed_pr(context: RunContext, pull_request, steps):
    print("Processing closed PR")
    upstream_pr_number = get_upstream_pr_number_from_pr(context, pull_request)
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
        remove_branch_for_pr(pull_request, steps)


def process_json_payload(context: RunContext, payload, pre_commit_callback):
    pull_request = payload['pull_request']
    if NO_SYNC_SIGNAL in pull_request.get('body', ''):
        return []

    steps = []
    if payload['action'] in ['opened', 'synchronize', 'reopened']:
        process_new_pr_contents(context, pull_request,
                                pre_commit_callback, steps)
    elif payload['action'] == 'edited' and 'title' in payload['changes']:
        change_upstream_pr_title(context, pull_request, steps)
    elif payload['action'] == 'closed':
        process_closed_pr(context, pull_request, steps)
    return steps


def process_and_run_steps(context: RunContext, payload, step_callback=None, error_callback=None, pre_commit_callback=None):
    try:
        steps = process_json_payload(context, payload, pre_commit_callback)
        for step in steps:
            step.run(context)
            if step_callback:
                step_callback(step)
        return True
    except Exception as exception:
        print(payload)
        traceback.print_exception(exception)
        if error_callback:
            error_callback()
        return False
