# This allows using types that are defined later in the file.
from __future__ import annotations

import os
import subprocess
import traceback

from typing import Optional
from typing import Union
from github import GithubBranch, GithubRepository, PullRequest

UPSTREAMABLE_PATH = 'tests/wpt/web-platform-tests/'
NO_SYNC_SIGNAL = '[no-wpt-sync]'

UPDATED_EXISTING_UPSTREAM_PR = 'Transplanted upstreamable changes to existing upstream PR ({upstream_pr}).'
CLOSING_EXISTING_UPSTREAM_PR = 'No upstreamable changes; closed existing upstream PR ({upstream_pr}).'
OPENED_NEW_UPSTREAM_PR = 'Found upstreamable WPT changes. Opened new upstream PR ({upstream_pr}).'
UPDATED_TITLE_IN_EXISTING_UPSTREAM_PR = 'PR title changed. Updated existing upstream PR ({upstream_pr}).'
NO_UPSTREAMBLE_CHANGES_COMMENT = "Downstream PR ({servo_pr}) no longer contains any upstreamable changes. Closing PR."

COULD_NOT_APPLY_CHANGES_DOWNSTREAM_COMMENT = """These changes could not be applied onto the latest
 upstream web-platform-tests. Servo may be out of sync."""
COULD_NOT_APPLY_CHANGES_UPSTREAM_COMMENT = """The downstream PR ({servo_pr}) can no longer be applied.
 Waiting for a new version of these changes downstream."""

COULD_NOT_MERGE_CHANGES_DOWNSTREAM_COMMENT = """These changes could not be merged at the upstream
 PR ({upstream_pr}). Please address any CI issues and try to merge manually."""
COULD_NOT_MERGE_CHANGES_UPSTREAM_COMMENT = """The downstream PR has merged ({servo_pr}), but these
 changes could not be merged properly. Please address any CI issues and try to merge manually."""

def make_comment(context: RunContext, template: str):
    return template.format(
        upstream_pr=context.upstream_pr,
        servo_pr=context.servo_pr,
    )

def wpt_branch_name_from_servo_pr_number(servo_pr_number):
    return f"servo_export_{servo_pr_number}"

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

        self.servo = GithubRepository(self, self.servo_repo)
        self.wpt = GithubRepository(self, self.wpt_repo)
        self.downstream_wpt = GithubRepository(self, self.downstream_wpt_repo)

        self.upstream_pr = "N/A"
        self.servo_pr = "N/A"


class Step:
    def __init__(self, name):
        self.name = name

    def provides(self) -> Optional[AsyncValue]:
        return None

    def run(self) -> Optional[list[Step]]:
        """Run this step. If a new list of steps is returned, stop execution
           of the currently running list and run the new list."""
        pass

    @classmethod
    def add(cls, steps: list[Step], *args) -> Optional[AsyncValue]:
        steps.append(cls(*args))
        return steps[-1].provides()


class AsyncValue:
    _value = None

    def resolve(self, value):
        self._value = value

    def value(self):
        assert(self._value != None)
        return self._value


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
    def __init__(self, action_pr_data, pull_request, pre_commit_callback):
        Step.__init__(self, 'CreateOrUpdateBranchForPRStep')
        self.action_pr_data = action_pr_data
        self.pull_request = pull_request
        self.pre_commit_callback = pre_commit_callback
        self.branch = AsyncValue()

    def provides(self):
        return self.branch

    def run(self, context: RunContext):
        try:
            commits = _fetch_upstreamable_commits(context, self.action_pr_data)
            branch_name = _create_or_update_branch_for_pr(context, self.action_pr_data, commits, self.pre_commit_callback)
            branch = context.downstream_wpt.get_branch(branch_name)

            self.branch.resolve(branch)
            self.name += ':%d:%s' % (len(commits), branch)
        except Exception as exception:
            print("Could not apply changes to upstream WPT repository.")
            traceback.print_exception(exception)

            steps = []
            CommentStep.add(steps, self.pull_request, COULD_NOT_APPLY_CHANGES_DOWNSTREAM_COMMENT)
            if context.upstream_pr:
                CommentStep.add(steps, context.upstream_pr, COULD_NOT_APPLY_CHANGES_UPSTREAM_COMMENT)
            return steps


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


class ChangePRStep(Step):
    def __init__(self, pull_request: PullRequest, state: str, title: str = None):
        name = f'ChangePRStep:{pull_request}:{state}'
        if title:
            name += f':{title}'

        Step.__init__(self, name)
        self.pull_request = pull_request
        self.state = state
        self.title = title

    def run(self, context: RunContext):
        self.pull_request.change(state=self.state, title=self.title)


class MergePRStep(Step):
    def __init__(self, pull_request: PullRequest, labels_to_remove: list[str] = []):
        Step.__init__(self, f'MergePRStep:{pull_request}')
        self.pull_request = pull_request
        self.labels_to_remove = labels_to_remove

    def run(self, context: RunContext):
        for label in self.labels_to_remove:
            self.pull_request.remove_label(label)
        try:
            self.pull_request.merge()
        except Exception as exception:
            print(f"Could not merge PR ({self.pull_request}).")
            traceback.print_exception(exception)

            steps = []
            CommentStep.add(steps, self.pull_request, COULD_NOT_MERGE_CHANGES_UPSTREAM_COMMENT)
            CommentStep.add(steps, context.servo_pr, COULD_NOT_MERGE_CHANGES_DOWNSTREAM_COMMENT)
            self.pull_request.add_labels(['stale-servo-export'])
            return steps


class OpenPRStep(Step):
    def __init__(self, source_branch: GithubBranch, target_repo: GithubRepository, title: str, body: str, labels: list[str]):
        Step.__init__(self, 'OpenPRStep')
        self.title = title
        self.body = body
        self.source_branch = source_branch
        self.target_repo = target_repo
        self.new_pr = AsyncValue()
        self.labels = labels

    def provides(self):
        return self.new_pr

    def run(self, context: RunContext):
        pull_request = self.target_repo.open_pull_request(
            self.source_branch.value(), self.title, self.body
        )
        context.upstream_pr = pull_request

        if self.labels:
            pull_request.add_labels(self.labels)

        self.new_pr.resolve(pull_request)

        self.name += f":{self.source_branch.value()}→{self.new_pr.value()}"


class CommentStep(Step):
    def __init__(self, pull_request: PullRequest, comment_template: str):
        Step.__init__(self, 'CommentStep')
        self.pull_request = pull_request
        self.comment_template = comment_template

    def run(self, context: RunContext):
        comment = make_comment(context, self.comment_template)
        self.name += f":{self.pull_request}:{comment}"
        self.pull_request.leave_comment(comment)


def patch_contains_upstreamable_changes(context: RunContext, pull_request):
    output = git([
        "diff",
         f"HEAD~{pull_request['commits']}",
        '--', UPSTREAMABLE_PATH
    ], cwd=context.servo_path)
    return len(output) > 0


def process_new_pr_contents(context: RunContext, action_pr_data: dict,
                            servo_pr: PullRequest, upstream_pr: Optional[PullRequest],
                            pre_commit_callback, steps):
    is_upstreamable = patch_contains_upstreamable_changes(context, action_pr_data)
    print(f"  → PR is upstreamable: '{is_upstreamable}'")

    if upstream_pr:
        if is_upstreamable:
            # In case this is adding new upstreamable changes to a PR that was closed
            # due to a lack of upstreamable changes, force it to be reopened.
            # Github refuses to reopen a PR that had a branch force pushed, so be sure
            # to do this first.
            ChangePRStep.add(steps, upstream_pr, 'opened', action_pr_data['title'])
            # Push the relevant changes to the upstream branch.
            CreateOrUpdateBranchForPRStep.add(steps, action_pr_data, servo_pr, pre_commit_callback)
            CommentStep.add(steps, servo_pr, UPDATED_EXISTING_UPSTREAM_PR)
        else:
            # Close the upstream PR, since would contain no changes otherwise.
            CommentStep.add(steps, upstream_pr, NO_UPSTREAMBLE_CHANGES_COMMENT)
            ChangePRStep.add(steps, upstream_pr, 'closed')
            RemoveBranchForPRStep.add(steps, action_pr_data)
            CommentStep.add(steps, servo_pr, CLOSING_EXISTING_UPSTREAM_PR)

    elif is_upstreamable:
        # Push the relevant changes to a new upstream branch.
        branch = CreateOrUpdateBranchForPRStep.add(steps, action_pr_data, servo_pr, pre_commit_callback)
        # Create a pull request against the upstream repository for the new branch.
        # TODO: extract the non-checklist/reviewable parts of the pull request body
        #       and add it to the upstream body.
        upstream_pr = OpenPRStep.add(
            steps, branch,
            context.wpt,
            action_pr_data['title'],
            f"Reviewed in {servo_pr}",
            ['servo-export', 'do not merge yet']
        )

        # Leave a comment to the new pull request in the original pull request.
        CommentStep.add(steps, servo_pr, OPENED_NEW_UPSTREAM_PR)


def change_upstream_pr_title(context: RunContext, action_pr_data: dict, servo_pr: PullRequest, upstream_pr: PullRequest, steps):
    print("Changing upstream PR title")
    if upstream_pr:
        ChangePRStep.add(steps, upstream_pr, 'open', action_pr_data['title'])
        CommentStep.add(steps, servo_pr, UPDATED_TITLE_IN_EXISTING_UPSTREAM_PR)


def process_closed_pr(context: RunContext, action_pr_data: dict, servo_pr: PullRequest, upstream_pr: PullRequest, steps):
    print("Processing closed PR")

    if not upstream_pr:
        # If we don't recognize this PR, it never contained upstreamable changes.
        return
    if action_pr_data['merged']:
        # Since the upstreamable changes have now been merged locally, merge the
        # corresponding upstream PR.
        MergePRStep.add(steps, upstream_pr, ['do not merge yet'])
    else:
        # If a PR with upstreamable changes is closed without being merged, we
        # don't want to merge the changes upstream either.
        ChangePRStep.add(steps, upstream_pr, 'closed')
        RemoveBranchForPRStep.add(steps, action_pr_data)


def process_json_payload(context: RunContext, payload, pre_commit_callback):
    action_pr_data = payload['pull_request']
    if NO_SYNC_SIGNAL in action_pr_data.get('body', ''):
        return []

    # Only look for an existing remote PR if the action is appropriate.
    print(f"Processing '{payload['action']}' action...")
    if payload['action'] not in [
        'opened', 'synchronize', 'reopened',
        'edited', 'closed']:
        return []

    servo_pr = context.servo.get_pull_request(action_pr_data['number'])
    context.servo_pr = servo_pr

    downstream_wpt_branch = context.downstream_wpt.get_branch(
        wpt_branch_name_from_servo_pr_number(servo_pr.number))

    upstream_pr = context.wpt.get_open_pull_request_for_branch(downstream_wpt_branch)
    if upstream_pr:
        context.upstream_pr = upstream_pr
        print(f"  → Detected existing upstream PR {upstream_pr}")

    steps = []
    if payload['action'] in ['opened', 'synchronize', 'reopened']:
        process_new_pr_contents(context, action_pr_data,
                                servo_pr, upstream_pr,
                                pre_commit_callback, steps)
    elif payload['action'] == 'edited' and 'title' in payload['changes']:
        change_upstream_pr_title(context, action_pr_data, servo_pr, upstream_pr, steps)
    elif payload['action'] == 'closed':
        process_closed_pr(context, action_pr_data, servo_pr, upstream_pr, steps)
    return steps


def process_and_run_steps(context: RunContext, payload, step_callback=None, pre_commit_callback=None):
    try:
        def run_steps(step_set):
            for step in step_set:
                new_steps = step.run(context)
                if step_callback:
                    step_callback(step)
                if new_steps:
                    return new_steps
            return None

        steps = process_json_payload(context, payload, pre_commit_callback)
        while steps:
            steps = run_steps(steps)
        return True
    except Exception as exception:
        print(payload)
        traceback.print_exception(exception)
        return False
