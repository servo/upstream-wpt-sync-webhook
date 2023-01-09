# pylint: disable=broad-except
# pylint: disable=dangerous-default-value
# pylint: disable=fixme
# pylint: disable=missing-docstring

# This allows using types that are defined later in the file.
from __future__ import annotations
import dataclasses

import json
import logging
import os
import re
import subprocess
import textwrap

from typing import Callable, Optional
from github import GithubBranch, GithubRepository, PullRequest

UPSTREAMABLE_PATH = "tests/wpt/web-platform-tests/"
NO_SYNC_SIGNAL = "[no-wpt-sync]"
PATCH_FILE_NAME = "tmp.patch"

OPENED_NEW_UPSTREAM_PR = (
    "🤖 Opened new upstream WPT pull request ({upstream_pr}) "
    "with upstreamable changes."
)
UPDATED_EXISTING_UPSTREAM_PR = (
    "📝 Transplanted new upstreamable changes to existing "
    "upstream WPT pull request ({upstream_pr})."
)
UPDATED_TITLE_IN_EXISTING_UPSTREAM_PR = (
    "✍ Updated existing upstream WPT pull request ({upstream_pr}) title and body."
)
CLOSING_EXISTING_UPSTREAM_PR = (
    "🤖 This change no longer contains upstreamable changes to WPT; closed existing "
    "upstream pull request ({upstream_pr})."
)
NO_UPSTREAMBLE_CHANGES_COMMENT = (
    "👋 Downstream pull request ({servo_pr}) no longer contains any upstreamable "
    "changes. Closing pull request without merging."
)
COULD_NOT_APPLY_CHANGES_DOWNSTREAM_COMMENT = (
    "🛠 These changes could not be applied onto the latest upstream WPT. "
    "Servo's copy of the Web Platform Tests may be out of sync."
)
COULD_NOT_APPLY_CHANGES_UPSTREAM_COMMENT = (
    "🛠 Changes from the source pull request ({servo_pr}) can no longer be "
    "cleanly applied. Waiting for a new version of these changes downstream."
)
COULD_NOT_MERGE_CHANGES_DOWNSTREAM_COMMENT = (
    "⛔ Failed to properly merge the upstream pull request ({upstream_pr}). "
    "Please address any CI issues and try to merge manually."
)
COULD_NOT_MERGE_CHANGES_UPSTREAM_COMMENT = (
    "⛔ The downstream PR has merged ({servo_pr}), but these changes could not "
    "be merged properly. Please address any CI issues and try to merge manually."
)


def wpt_branch_name_from_servo_pr_number(servo_pr_number):
    return f"servo_export_{servo_pr_number}"


class Step:
    def __init__(self, name):
        self.name = name

    def provides(self) -> Optional[AsyncValue]:
        return None

    def run(self, _run: SyncRun):
        return


class AsyncValue:
    _value = None

    def resolve(self, value):
        self._value = value

    def value(self):
        assert self._value is not None
        return self._value


class LocalGitRepo:
    def __init__(self, path: str, sync: WPTSync):
        self.path = path
        self.sync = sync

    def run(self, *args, env: dict = {}):
        command_line = ["git"] + list(args)
        logging.info("  → Execution (cwd='%s'): %s", self.path, " ".join(command_line))

        env.setdefault("GIT_AUTHOR_EMAIL", self.sync.github_email)
        env.setdefault("GIT_COMMITTER_EMAIL", self.sync.github_email)
        env.setdefault("GIT_AUTHOR_NAME", self.sync.github_name)
        env.setdefault("GIT_COMMITTER_NAME", self.sync.github_name)

        return subprocess.check_output(
            command_line, cwd=self.path, env=env, stderr=subprocess.STDOUT
        ).decode("utf-8")


class CreateOrUpdateBranchForPRStep(Step):
    def __init__(self, pull_data: dict, pull_request: PullRequest):
        Step.__init__(self, "CreateOrUpdateBranchForPRStep")
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
            self.name += f":{len(commits)}:{branch}"
        except Exception as exception:
            logging.info("Could not apply changes to upstream WPT repository.")
            logging.info(exception, exc_info=True)

            run.steps = []
            run.add_step(CommentStep(
                self.pull_request, COULD_NOT_APPLY_CHANGES_DOWNSTREAM_COMMENT
            ))
            if run.upstream_pr:
                run.add_step(CommentStep(
                    run.upstream_pr, COULD_NOT_APPLY_CHANGES_UPSTREAM_COMMENT
                ))

    def _get_upstreamable_commits_from_local_servo_repo(self, sync: WPTSync):
        local_servo_repo = sync.local_servo_repo
        number_of_commits = self.pull_data["commits"]
        commit_shas = local_servo_repo.run(
            "log", "--pretty=%H", f"-{number_of_commits}"
        ).splitlines()

        filtered_commits = []
        for sha in commit_shas:
            # Specifying the path here does a few things. First, it excludes any
            # changes that do not touch WPT files at all. Secondly, when a file is
            # moved in or out of the WPT directory the filename which is outside the
            # directory becomes /dev/null and the change becomes an addition or
            # deletion. This makes the patch usable on the WPT repository itself.
            # TODO: If we could cleverly parse and manipulate the full commit diff
            # we could avoid cloning the servo repository altogether and only
            # have to fetch the commit diffs from GitHub.
            diff = local_servo_repo.run(
                "show", "--binary", "--format=%b", sha, "--", UPSTREAMABLE_PATH
            )

            # Retrieve the diff of any changes to files that are relevant
            if diff:
                # Create an object that contains everything necessary to transplant this
                # commit to another repository.
                filtered_commits += [
                    {
                        "author": local_servo_repo.run(
                            "show", "-s", "--pretty=%an <%ae>", sha
                        ),
                        "message": local_servo_repo.run(
                            "show", "-s", "--pretty=%B", sha
                        ),
                        "diff": diff,
                    }
                ]
        return filtered_commits

    def _apply_filtered_servo_commit_to_wpt(self, run: SyncRun, commit: dict):
        patch_path = os.path.join(run.sync.wpt_path, PATCH_FILE_NAME)
        strip_count = UPSTREAMABLE_PATH.count("/") + 1

        try:
            with open(patch_path, "w", encoding="utf-8") as file:
                file.write(commit["diff"])
            run.sync.local_wpt_repo.run(
                "apply", PATCH_FILE_NAME, "-p", str(strip_count)
            )
        finally:
            # Ensure the patch file is not added with the other changes.
            os.remove(patch_path)

        run.sync.local_wpt_repo.run("add", "--all")
        run.sync.local_wpt_repo.run(
            "commit", "--message", commit["message"], "--author", commit["author"]
        )

    def _create_or_update_branch_for_pr(
        self, run: SyncRun, commits: list[dict], pre_commit_callback=None
    ):
        branch_name = wpt_branch_name_from_servo_pr_number(self.pull_data["number"])
        try:
            # Create a new branch with a unique name that is consistent between
            # updates of the same PR.
            run.sync.local_wpt_repo.run("checkout", "-b", branch_name)

            for commit in commits:
                self._apply_filtered_servo_commit_to_wpt(run, commit)

            if pre_commit_callback:
                pre_commit_callback()

            # Push the branch upstream (forcing to overwrite any existing changes).
            if not run.sync.suppress_force_push:
                user = run.sync.github_username
                token = run.sync.github_api_token
                repo = run.sync.downstream_wpt_repo
                remote_url = f"https://{user}:{token}@github.com/{repo}.git"
                run.sync.local_wpt_repo.run("push", "-f", remote_url, branch_name)

            return branch_name
        finally:
            try:
                run.sync.local_wpt_repo.run("checkout", "master")
                run.sync.local_wpt_repo.run("branch", "-D", branch_name)
            except Exception:
                pass


class RemoveBranchForPRStep(Step):
    def __init__(self, pull_request):
        Step.__init__(self, "RemoveBranchForPRStep")
        self.branch_name = wpt_branch_name_from_servo_pr_number(pull_request["number"])

    def run(self, run: SyncRun):
        self.name += f":{run.sync.downstream_wpt.get_branch(self.branch_name)}"
        logging.info("  -> Removing branch used for upstream PR")
        if not run.sync.suppress_force_push:
            run.sync.local_wpt_repo.run("push", "origin", "--delete",
                                        self.branch_name)


class ChangePRStep(Step):
    def __init__(
        self,
        pull_request: PullRequest,
        state: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ):
        name = f"ChangePRStep:{pull_request}:{state}"
        if title:
            name += f":{title}"

        Step.__init__(self, name)
        self.pull_request = pull_request
        self.state = state
        self.title = title
        self.body = body

    def run(self, run: SyncRun):
        body = self.body
        if body:
            body = run.prepare_body_text(body)
            self.name += (
                f':{textwrap.shorten(body, width=20, placeholder="...")}[{len(body)}]'
            )

        self.pull_request.change(state=self.state, title=self.title, body=body)


class MergePRStep(Step):
    def __init__(self, pull_request: PullRequest, labels_to_remove: list[str] = []):
        Step.__init__(self, f"MergePRStep:{pull_request}")
        self.pull_request = pull_request
        self.labels_to_remove = labels_to_remove

    def run(self, run: SyncRun):
        for label in self.labels_to_remove:
            self.pull_request.remove_label(label)
        try:
            self.pull_request.merge()
        except Exception as exception:
            logging.warning("Could not merge PR (%s).", self.pull_request)
            logging.warning(exception, exc_info=True)

            run.steps = []
            run.add_step(CommentStep(
                self.pull_request, COULD_NOT_MERGE_CHANGES_UPSTREAM_COMMENT
            ))
            run.add_step(CommentStep(
                run.servo_pr, COULD_NOT_MERGE_CHANGES_DOWNSTREAM_COMMENT
            ))
            self.pull_request.add_labels(["stale-servo-export"])


class OpenPRStep(Step):
    def __init__(
        self,
        source_branch: GithubBranch,
        target_repo: GithubRepository,
        title: str,
        body: str,
        labels: list[str],
    ):
        Step.__init__(self, "OpenPRStep")
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
            self.source_branch.value(), self.title, run.prepare_body_text(self.body)
        )
        assert not run.upstream_pr
        run.upstream_pr = pull_request

        if self.labels:
            pull_request.add_labels(self.labels)

        self.new_pr.resolve(pull_request)

        self.name += f":{self.source_branch.value()}→{self.new_pr.value()}"


class CommentStep(Step):
    def __init__(self, pull_request: PullRequest, comment_template: str):
        Step.__init__(self, "CommentStep")
        self.pull_request = pull_request
        self.comment_template = comment_template

    def run(self, run: SyncRun):
        comment = run.make_comment(self.comment_template)
        self.name += f":{self.pull_request}:{comment}"
        self.pull_request.leave_comment(comment)


@dataclasses.dataclass()
class SyncRun:
    sync: WPTSync
    servo_pr: PullRequest
    upstream_pr: Optional[PullRequest]
    step_callback: Optional[Callable[[Step], None]]
    steps: list[Step] = dataclasses.field(default_factory=list)

    def make_comment(self, template: str) -> str:
        return template.format(
            upstream_pr=self.upstream_pr,
            servo_pr=self.servo_pr,
        )

    def add_step(self, step) -> Optional[AsyncValue]:
        self.steps.append(step)
        return step.provides()

    def run(self):
        while self.steps:
            step = self.steps.pop(0)
            step.run(self)
            if self.step_callback:
                self.step_callback(step)

    @staticmethod
    def clean_up_body_text(body: str) -> str:
        # Turn all bare or relative issue references into unlinked ones, so that
        # the PR doesn't inadvertently close or link to issues in the upstream
        # repository.
        return (
            re.sub(
                r"(^|\s)(\w*)#([1-9]\d*)",
                r"\g<1>\g<2>#<!-- nolink -->\g<3>",
                body,
                flags=re.MULTILINE,
            )
            .split("\n---")[0]
            .split("<!-- Thank you for")[0]
        )

    def prepare_body_text(self, body: str) -> str:
        return SyncRun.clean_up_body_text(body) + f"\nReviewed in {self.servo_pr}"


@dataclasses.dataclass(kw_only=True)
class WPTSync:
    servo_repo: str
    wpt_repo: str
    downstream_wpt_repo: str
    servo_path: str
    wpt_path: str
    github_api_token: str
    github_api_url: str
    github_username: str
    github_email: str
    github_name: str
    suppress_force_push: bool = False

    def __post_init__(self):
        self.servo = GithubRepository(self, self.servo_repo)
        self.wpt = GithubRepository(self, self.wpt_repo)
        self.downstream_wpt = GithubRepository(self, self.downstream_wpt_repo)
        self.local_servo_repo = LocalGitRepo(self.servo_path, self)
        self.local_wpt_repo = LocalGitRepo(self.wpt_path, self)

    def run(self, payload: dict, step_callback=None) -> bool:
        if "pull_request" not in payload:
            return True

        pull_data = payload["pull_request"]
        if NO_SYNC_SIGNAL in pull_data.get("body", ""):
            return True

        # Only look for an existing remote PR if the action is appropriate.
        logging.info("Processing '%s' action...", payload["action"])
        action = payload["action"]
        if action not in ["opened", "synchronize", "reopened", "edited", "closed"]:
            return True

        if (
            action == "edited"
            and "title" not in payload["changes"]
            and "body" not in payload["changes"]
        ):
            return True

        try:
            servo_pr = self.servo.get_pull_request(pull_data["number"])
            downstream_wpt_branch = self.downstream_wpt.get_branch(
                wpt_branch_name_from_servo_pr_number(servo_pr.number)
            )
            upstream_pr = self.wpt.get_open_pull_request_for_branch(
                downstream_wpt_branch
            )
            if upstream_pr:
                logging.info("  → Detected existing upstream PR %s", upstream_pr)

            run = SyncRun(self, servo_pr, upstream_pr, step_callback)

            pull_data = payload["pull_request"]
            if payload["action"] in ["opened", "synchronize", "reopened"]:
                self.handle_new_pull_request_contents(run, pull_data)
            elif payload["action"] == "edited":
                self.handle_edited_pull_request(run, pull_data)
            elif payload["action"] == "closed":
                self.handle_closed_pull_request(run, pull_data)

            run.run()
            return True
        except Exception as exception:
            if isinstance(exception, subprocess.CalledProcessError):
                logging.error(exception.output)
            logging.error(json.dumps(payload))
            logging.error(exception, exc_info=True)
            return False

    def handle_new_pull_request_contents(self, run: SyncRun, pull_data: dict):
        is_upstreamable = (
            len(
                self.local_servo_repo.run(
                    "diff", f"HEAD~{pull_data['commits']}", "--", UPSTREAMABLE_PATH
                )
            )
            > 0
        )
        logging.info("  → PR is upstreamable: '%s'", is_upstreamable)

        title = pull_data['title']
        body = pull_data['body']
        if run.upstream_pr:
            if is_upstreamable:
                # In case this is adding new upstreamable changes to a PR that was closed
                # due to a lack of upstreamable changes, force it to be reopened.
                # Github refuses to reopen a PR that had a branch force pushed, so be sure
                # to do this first.
                run.add_step(ChangePRStep(run.upstream_pr, "opened", title, body))
                # Push the relevant changes to the upstream branch.
                run.add_step(CreateOrUpdateBranchForPRStep(pull_data, run.servo_pr))
                run.add_step(CommentStep(run.servo_pr, UPDATED_EXISTING_UPSTREAM_PR))
            else:
                # Close the upstream PR, since would contain no changes otherwise.
                run.add_step(CommentStep(run.upstream_pr, NO_UPSTREAMBLE_CHANGES_COMMENT))
                run.add_step(ChangePRStep(run.upstream_pr, "closed"))
                run.add_step(RemoveBranchForPRStep(pull_data))
                run.add_step(CommentStep(run.servo_pr, CLOSING_EXISTING_UPSTREAM_PR))

        elif is_upstreamable:
            # Push the relevant changes to a new upstream branch.
            branch = run.add_step(CreateOrUpdateBranchForPRStep(pull_data, run.servo_pr))
            # Create a pull request against the upstream repository for the new branch.
            run.add_step(OpenPRStep(
                 branch, self.wpt, title, body,
                ["servo-export", "do not merge yet"],
            ))

            # Leave a comment to the new pull request in the original pull request.
            run.add_step(CommentStep(run.servo_pr, OPENED_NEW_UPSTREAM_PR))

    def handle_edited_pull_request(self, run: SyncRun, pull_data: dict):
        logging.info("Changing upstream PR title")
        if run.upstream_pr:
            run.add_step(ChangePRStep(
                run.upstream_pr, "open", pull_data["title"], pull_data["body"]
            ))
            run.add_step(CommentStep(run.servo_pr, UPDATED_TITLE_IN_EXISTING_UPSTREAM_PR))

    def handle_closed_pull_request(self, run: SyncRun, pull_data: dict):
        logging.info("Processing closed PR")
        if not run.upstream_pr:
            # If we don't recognize this PR, it never contained upstreamable changes.
            return
        if pull_data["merged"]:
            # Since the upstreamable changes have now been merged locally, merge the
            # corresponding upstream PR.
            run.add_step(MergePRStep(run.upstream_pr, ["do not merge yet"]))
        else:
            # If a PR with upstreamable changes is closed without being merged, we
            # don't want to merge the changes upstream either.
            run.add_step(ChangePRStep(run.upstream_pr, "closed"))

        # Always clean up our remote branch.
        run.add_step(RemoveBranchForPRStep(pull_data))
