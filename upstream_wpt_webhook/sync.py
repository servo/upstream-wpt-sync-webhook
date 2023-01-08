# This allows using types that are defined later in the file.
from __future__ import annotations

import json
import logging
import os
import subprocess
import traceback

from typing import Callable, Optional
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

def wpt_branch_name_from_servo_pr_number(servo_pr_number):
    return f"servo_export_{servo_pr_number}"

class Step:
    def __init__(self, name):
        self.name = name

    def provides(self) -> Optional[AsyncValue]:
        return None

    def run(self, run: SyncRun) -> Optional[list[Step]]:
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


class LocalGitRepo:
    def __init__(self, path: str, sync: WPTSync):
        self.path = path
        self.sync = sync

    def run(self, *args, env: dict = {}):
        command_line = ["git"] + list(args)
        logging.info(f"  → Execution (cwd='{self.path}'): {' '.join(command_line)}")

        env.setdefault('GIT_AUTHOR_EMAIL', self.sync.github_email)
        env.setdefault('GIT_COMMITTER_EMAIL', self.sync.github_email)
        env.setdefault('GIT_AUTHOR_NAME', self.sync.github_name)
        env.setdefault('GIT_COMMITTER_NAME', self.sync.github_name)

        return subprocess.check_output(
            command_line, cwd=self.path, env=env, stderr=subprocess.STDOUT).decode('utf-8')

class CreateOrUpdateBranchForPRStep(Step):
    def __init__(self, pull_data: dict, pull_request: PullRequest):
        Step.__init__(self, 'CreateOrUpdateBranchForPRStep')
        self.pull_data = pull_data
        self.pull_request = pull_request
        self.branch = AsyncValue()

    def provides(self):
        return self.branch

    def run(self, run: SyncRun):
        try:
            commits = self._get_upstreamable_commits_from_local_servo_repo(run.sync)
            branch_name = self._create_or_update_branch_for_pr(run, commits)
            branch = run.sync.downstream_wpt.get_branch(branch_name)

            self.branch.resolve(branch)
            self.name += ':%d:%s' % (len(commits), branch)
        except Exception as exception:
            logging.info("Could not apply changes to upstream WPT repository.")
            logging.info(exception, exc_info=True)

            steps = []
            CommentStep.add(steps, self.pull_request, COULD_NOT_APPLY_CHANGES_DOWNSTREAM_COMMENT)
            if run.upstream_pr:
                CommentStep.add(steps, run.upstream_pr, COULD_NOT_APPLY_CHANGES_UPSTREAM_COMMENT)
            return steps

    def _get_upstreamable_commits_from_local_servo_repo(self, sync: WPTSync):
        local_servo_repo = sync.local_servo_repo
        number_of_commits = self.pull_data["commits"]
        commit_shas = local_servo_repo.run("log", "--pretty=%H", f"-{number_of_commits}").splitlines()

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
            diff = local_servo_repo.run("show", "--binary", "--format=%b", sha, '--',  UPSTREAMABLE_PATH)

            # Retrieve the diff of any changes to files that are relevant
            if diff:
                # Create an object that contains everything necessary to transplant this
                # commit to another repository.
                filtered_commits += [{
                    'author': local_servo_repo.run("show", "-s", "--pretty=%an <%ae>", sha),
                    'message': local_servo_repo.run("show", "-s", "--pretty=%B", sha),
                    'diff': diff,
                }]
        return filtered_commits

    def _apply_filtered_servo_commit_to_wpt(self, run: SyncRun, commit: dict):
        PATCH_FILE = 'tmp.patch'
        PATCH_PATH = os.path.join(run.sync.wpt_path, PATCH_FILE)
        STRIP_COUNT = UPSTREAMABLE_PATH.count('/') + 1

        try:
            with open(PATCH_PATH, 'w') as f:
                f.write(commit['diff'])
            run.sync.local_wpt_repo.run("apply", PATCH_FILE, "-p", str(STRIP_COUNT))
        finally:
            # Ensure the patch file is not added with the other changes.
            os.remove(PATCH_PATH)

        run.sync.local_wpt_repo.run("add", "--all")
        run.sync.local_wpt_repo.run("commit", "--message", commit['message'], "--author", commit['author'])


    def _create_or_update_branch_for_pr(self, run: SyncRun, commits: list[dict], pre_commit_callback=None):
        branch_name = wpt_branch_name_from_servo_pr_number(self.pull_data['number'])
        try:
            # Create a new branch with a unique name that is consistent between updates of the same PR
            run.sync.local_wpt_repo.run("checkout", "-b", branch_name)

            for commit in commits:
                self._apply_filtered_servo_commit_to_wpt(run, commit)

            if pre_commit_callback:
                pre_commit_callback()

            # Push the branch upstream (forcing to overwrite any existing changes)
            if not run.sync.suppress_force_push:
                remote_url = "https://{user}:{token}@github.com/{repo}.git".format(
                    user=run.sync.github_username,
                    token=run.sync.github_api_token,
                    repo=run.sync.downstream_wpt_repo
                )
                run.sync.local_wpt_repo.run("push", "-f", remote_url, branch_name)

            return branch_name
        finally:
            try:
                run.sync.local_wpt_repo.run("checkout", "master")
                run.sync.local_wpt_repo.run("branch", "-D", branch_name)
            except:
                pass


class RemoveBranchForPRStep(Step):
    def __init__(self, pull_request):
        Step.__init__(self, 'RemoveBranchForPRStep')
        self.pull_request = pull_request

    def run(self, run: SyncRun):
        logging.info("  -> Removing branch used for upstream PR")
        branch_name = wpt_branch_name_from_servo_pr_number(self.pull_request['number'])
        if not run.sync.suppress_force_push:
            run.sync.local_wpt_repo.run("push", "origin", "--delete", branch_name)


class ChangePRStep(Step):
    def __init__(self, pull_request: PullRequest, state: str, title: str = None):
        name = f'ChangePRStep:{pull_request}:{state}'
        if title:
            name += f':{title}'

        Step.__init__(self, name)
        self.pull_request = pull_request
        self.state = state
        self.title = title

    def run(self, run: SyncRun):
        self.pull_request.change(state=self.state, title=self.title)


class MergePRStep(Step):
    def __init__(self, pull_request: PullRequest, labels_to_remove: list[str] = []):
        Step.__init__(self, f'MergePRStep:{pull_request}')
        self.pull_request = pull_request
        self.labels_to_remove = labels_to_remove

    def run(self, run: SyncRun):
        for label in self.labels_to_remove:
            self.pull_request.remove_label(label)
        try:
            self.pull_request.merge()
        except Exception as exception:
            logging.warning(f"Could not merge PR ({self.pull_request}).")
            logging.warning(exception, exc_info=True)

            steps = []
            CommentStep.add(steps, self.pull_request, COULD_NOT_MERGE_CHANGES_UPSTREAM_COMMENT)
            CommentStep.add(steps, run.servo_pr, COULD_NOT_MERGE_CHANGES_DOWNSTREAM_COMMENT)
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

    def run(self, run: SyncRun):
        pull_request = self.target_repo.open_pull_request(
            self.source_branch.value(), self.title, self.body
        )
        assert not run.upstream_pr
        run.upstream_pr = pull_request

        if self.labels:
            pull_request.add_labels(self.labels)

        self.new_pr.resolve(pull_request)

        self.name += f":{self.source_branch.value()}→{self.new_pr.value()}"


class CommentStep(Step):
    def __init__(self, pull_request: PullRequest, comment_template: str):
        Step.__init__(self, 'CommentStep')
        self.pull_request = pull_request
        self.comment_template = comment_template

    def run(self, run: SyncRun):
        comment = run.make_comment(self.comment_template)
        self.name += f":{self.pull_request}:{comment}"
        self.pull_request.leave_comment(comment)


class SyncRun:
    def __init__(self, sync: WPTSync, servo_pr: PullRequest, upstream_pr: PullRequest, step_callback: Callable[[Step]]):
        self.__dict__.update(locals())

    def make_comment(self, template: str) -> str:
        return template.format(
            upstream_pr=self.upstream_pr,
            servo_pr=self.servo_pr,
        )

    def run_steps(self, steps: list[Step]) -> Optional[Step]:
        for step in steps:
            new_steps = step.run(self)
            if self.step_callback:
                self.step_callback(step)
            if new_steps:
                return new_steps
        return None

    def run(self, payload: dict):
        steps = []
        pull_data = payload['pull_request']
        if payload['action'] in ['opened', 'synchronize', 'reopened']:
            self.steps_for_new_pull_request_contents(steps, pull_data)
        elif payload['action'] == 'edited' and 'title' in pull_data['changes']:
            self.steps_for_new_pull_request_title(steps, pull_data)
        elif payload['action'] == 'closed':
            self.steps_for_closed_pull_request(steps, pull_data)

        while steps:
            steps = self.run_steps(steps)

    def steps_for_new_pull_request_contents(self, steps: list[Step], pull_data: dict):
        is_upstreamable = len(self.sync.local_servo_repo.run(
            "diff", f"HEAD~{pull_data['commits']}", '--', UPSTREAMABLE_PATH
        )) > 0
        logging.info(f"  → PR is upstreamable: '{is_upstreamable}'")

        if self.upstream_pr:
            if is_upstreamable:
                # In case this is adding new upstreamable changes to a PR that was closed
                # due to a lack of upstreamable changes, force it to be reopened.
                # Github refuses to reopen a PR that had a branch force pushed, so be sure
                # to do this first.
                ChangePRStep.add(steps, self.upstream_pr, 'opened', pull_data['title'])
                # Push the relevant changes to the upstream branch.
                CreateOrUpdateBranchForPRStep.add(steps, pull_data, self.servo_pr)
                CommentStep.add(steps, self.servo_pr, UPDATED_EXISTING_UPSTREAM_PR)
            else:
                # Close the upstream PR, since would contain no changes otherwise.
                CommentStep.add(steps, self.upstream_pr, NO_UPSTREAMBLE_CHANGES_COMMENT)
                ChangePRStep.add(steps, self.upstream_pr, 'closed')
                RemoveBranchForPRStep.add(steps, pull_data)
                CommentStep.add(steps, self.servo_pr, CLOSING_EXISTING_UPSTREAM_PR)

        elif is_upstreamable:
            # Push the relevant changes to a new upstream branch.
            branch = CreateOrUpdateBranchForPRStep.add(steps, pull_data, self.servo_pr)
            # Create a pull request against the upstream repository for the new branch.
            # TODO: extract the non-checklist/reviewable parts of the pull request body
            #       and add it to the upstream body.
            upstream_pr = OpenPRStep.add(
                steps, branch,
                self.sync.wpt,
                pull_data['title'],
                f"Reviewed in {self.servo_pr}",
                ['servo-export', 'do not merge yet']
            )

            # Leave a comment to the new pull request in the original pull request.
            CommentStep.add(steps, self.servo_pr, OPENED_NEW_UPSTREAM_PR)

    def steps_for_new_pull_request_title(self, steps: list[Step], pull_data: dict):
        logging.info("Changing upstream PR title")
        if self.upstream_pr:
            ChangePRStep.add(steps, self.upstream_pr, 'open', pull_data['title'])
            CommentStep.add(steps, self.servo_pr, UPDATED_TITLE_IN_EXISTING_UPSTREAM_PR)

    def steps_for_closed_pull_request(self, steps: list[Step], pull_data: dict):
        logging.info("Processing closed PR")
        if not self.upstream_pr:
            # If we don't recognize this PR, it never contained upstreamable changes.
            return
        if pull_data['merged']:
            # Since the upstreamable changes have now been merged locally, merge the
            # corresponding upstream PR.
            MergePRStep.add(steps, self.upstream_pr, ['do not merge yet'])
        else:
            # If a PR with upstreamable changes is closed without being merged, we
            # don't want to merge the changes upstream either.
            ChangePRStep.add(steps, self.upstream_pr, 'closed')
            RemoveBranchForPRStep.add(steps, pull_data)


class WPTSync:
    def __init__(self, *,
                 servo_repo: str, wpt_repo: str, downstream_wpt_repo: str,
                 servo_path: str, wpt_path: str,
                 github_api_token: str, github_api_url: str,
                 github_username: str, github_email: str, github_name: str,
                 suppress_force_push: bool = False):
        self.__dict__.update(locals())
        self.servo = GithubRepository(self, self.servo_repo)
        self.wpt = GithubRepository(self, self.wpt_repo)
        self.downstream_wpt = GithubRepository(self, self.downstream_wpt_repo)
        self.local_servo_repo = LocalGitRepo(self.servo_path, self)
        self.local_wpt_repo = LocalGitRepo(self.wpt_path, self)

    def run(self, payload: dict, step_callback=None, pre_commit_callback=Optional[Callable]) -> bool:
        if not 'pull_request' in payload:
            return True

        pull_data = payload['pull_request']
        if NO_SYNC_SIGNAL in pull_data.get('body', ''):
            return True

        # Only look for an existing remote PR if the action is appropriate.
        logging.info(f"Processing '{payload['action']}' action...")
        if payload['action'] not in [
            'opened', 'synchronize', 'reopened',
            'edited', 'closed']:
            return True

        try:
            servo_pr = self.servo.get_pull_request(pull_data['number'])

            downstream_wpt_branch = self.downstream_wpt.get_branch(
                wpt_branch_name_from_servo_pr_number(servo_pr.number))
            upstream_pr = self.wpt.get_open_pull_request_for_branch(downstream_wpt_branch)
            if upstream_pr:
                logging.info(f"  → Detected existing upstream PR {upstream_pr}")

            SyncRun(self, servo_pr, upstream_pr, step_callback).run(payload)
            return True
        except Exception as exception:
            if isinstance(exception, subprocess.CalledProcessError):
                logging.error(exception.output)
            logging.error(json.dumps(payload))
            logging.error(exception, exc_info=True)
            return False