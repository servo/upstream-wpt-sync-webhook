import json
import locale
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Tuple
import unittest

from functools import partial
import unittest
from mock_github_api_server import MockGitHubAPIServer, MockPullRequest
from sync import ChangePRStep, CreateOrUpdateBranchForPRStep, SyncRun, WPTSync

SYNC = None
TMP_DIR = None
PORT = 9000
TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")

if '-v' in sys.argv or '--verbose' in sys.argv:
    logging.getLogger().level = logging.DEBUG

"""
Tests that sync.SyncRun.clean_up_body_text properly prepares the
body text for an upstream pull request.
"""
class TestCleanUpBodyText(unittest.TestCase):
    def test_prepare_body(self):
        text = "Simple body text"
        self.assertEqual(text, SyncRun.clean_up_body_text(text))
        self.assertEqual("With reference: #<!-- nolink -->3",
            SyncRun.clean_up_body_text("With reference: #3"))
        self.assertEqual("Multi reference: #<!-- nolink -->3 and #<!-- nolink -->1020",
            SyncRun.clean_up_body_text("Multi reference: #3 and #1020"))
        self.assertEqual("Subject\n\nBody text #<!-- nolink -->1",
            SyncRun.clean_up_body_text(
                "Subject\n\nBody text #1\n---<!-- Thank you for contributing"))
        self.assertEqual("Subject\n\nNo dashes",
            SyncRun.clean_up_body_text(
                "Subject\n\nNo dashes<!-- Thank you for contributing"))
        self.assertEqual("Subject\n\nNo --- comment",
            SyncRun.clean_up_body_text(
                "Subject\n\nNo --- comment\n---Other stuff that"))

"""
Tests that commits are properly applied to WPT by
:func:sync.CreateOrUpdateBranchForPrStep._create_or_update_branch_for_pr.
"""
class TestApplyCommitsToWPT(unittest.TestCase):
    def run_test(self, pr_number: int, commits: dict):
        def make_commit(data):
            with open(os.path.join(TESTS_DIR, data[2])) as file:
                return { "author": data[0], "message": data[1], "diff": file.read() }
        commits = [make_commit(data) for data in commits]

        pull_request = SYNC.servo.get_pull_request(pr_number)
        step = CreateOrUpdateBranchForPRStep({'number': pr_number}, pull_request)

        def get_applied_commits(num_commits: int, applied_commits: list[Tuple[str, str]]):
            repo = SYNC.local_wpt_repo
            log = ["log", "--oneline", f"-{num_commits}"]
            applied_commits += list(zip(
                repo.run(*log, "--format=%aN <%ae>").splitlines(),
                repo.run(*log, "--format=%s").splitlines()
            ))
            applied_commits.reverse()

        applied_commits = []
        callback = partial(get_applied_commits, len(commits), applied_commits)
        step._create_or_update_branch_for_pr(
            SyncRun(SYNC, pull_request, None, None), commits, callback)

        expected_commits = [(commit['author'], commit['message']) for commit in commits]
        self.assertListEqual(applied_commits, expected_commits)

    def test_simple_commit(self):
        self.run_test(45, [
            ["test author <test@author>", "test commit message", "18746.diff"]
        ])

    def test_two_commits(self):
        self.run_test(100, [
            ["test author <test@author>", "test commit message", "18746.diff"],
            ["another person <two@author>", "a different message", "wpt.diff"]
        ])

class TestFullSyncRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockGitHubAPIServer(PORT)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def mock_servo_repository_state(self, diffs: list):
        # Clean up any old files.
        first_commit_hash = SYNC.local_servo_repo.run("rev-list", "HEAD").splitlines()[-1]
        SYNC.local_servo_repo.run("reset", "--hard", first_commit_hash)
        SYNC.local_servo_repo.run("clean", "-fxd")

        def make_commit_data(diff):
            if not isinstance(diff, list):
                return [diff, "tmp author", "tmp@tmp.com", "tmp commit message"]
            else:
                return diff
        commits = [make_commit_data(diff) for diff in diffs]

        # Apply each commit to the repository.
        for commit in commits:
            patch_file, author, email, message = commit
            SYNC.local_servo_repo.run("apply", os.path.join(TESTS_DIR, patch_file))
            SYNC.local_servo_repo.run("add", ".")
            SYNC.local_servo_repo.run("commit", "-a", "--author", f"{author} <{email}>", "-m", message,
                env={'GIT_COMMITTER_NAME': author.encode(locale.getpreferredencoding()),
                    'GIT_COMMITTER_EMAIL': email})

    def run_test(self, payload_file: str, diffs: list, existing_prs: list[MockPullRequest] = []):
        with open(os.path.join('tests', payload_file)) as f:
            payload = json.loads(f.read())

        logging.info(f'Mocking application of PR to servo.')
        self.mock_servo_repository_state(diffs)

        logging.info(f'Resetting server state')
        self.server.reset_server_state_with_prs(existing_prs)

        actual_steps = []
        SYNC.run(
            payload,
            step_callback=lambda step: actual_steps.append(step.name)
        )
        return actual_steps

    def test_opened_upstremable_pr(self):
        self.assertListEqual(
            self.run_test("opened.json", ["18746.diff"]),
            [
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_18746",
                "OpenPRStep:servo-wpt-sync/wpt/servo_export_18746→wpt/wpt#1",
                "CommentStep:servo/servo#18746:Found upstreamable WPT changes. Opened new upstream PR (wpt/wpt#1)."
            ]
        )

    def test_opened_upstremable_pr_with_move_into_wpt(self):
        self.assertListEqual(
            self.run_test("opened.json", ["move-into-wpt.diff"]),
            [
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_18746",
                "OpenPRStep:servo-wpt-sync/wpt/servo_export_18746→wpt/wpt#1",
                "CommentStep:servo/servo#18746:Found upstreamable WPT changes. Opened new upstream PR (wpt/wpt#1)."
            ]
        )

    def test_opened_upstreamble_pr_with_move_into_wpt_and_non_ascii_author(self):
        self.assertListEqual(
            self.run_test(
                "opened.json",
                [["move-into-wpt.diff", "Fernando Jiménez Moreno", "foo@bar.com", "ééééé"]]
            ),
            [
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_18746",
                "OpenPRStep:servo-wpt-sync/wpt/servo_export_18746→wpt/wpt#1",
                "CommentStep:servo/servo#18746:Found upstreamable WPT changes. Opened new upstream PR (wpt/wpt#1)."
            ]
        )

    def test_opened_uptreamable_pr_with_move_out_of_wpt(self):
        self.assertListEqual(
            self.run_test("opened.json", ["move-out-of-wpt.diff"]),
            [
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_18746",
                "OpenPRStep:servo-wpt-sync/wpt/servo_export_18746→wpt/wpt#1",
                "CommentStep:servo/servo#18746:Found upstreamable WPT changes. Opened new upstream PR (wpt/wpt#1)."
            ]
        )

    def test_opened_new_mr_with_no_sync_signal(self):
        self.assertListEqual(self.run_test("opened-with-no-sync-signal.json", ["18746.diff"]), [])
        self.assertListEqual(self.run_test("opened-with-no-sync-signal.json", ["non-wpt.diff"]), [])

    def test_opened_upstremable_pr_not_applying_cleanly_to_upstream(self):
        self.assertListEqual(
            self.run_test("opened.json", ["does-not-apply-cleanly.diff"]),
            [
                "CreateOrUpdateBranchForPRStep",
                "CommentStep:servo/servo#18746:These changes could not be applied onto the latest\n upstream web-platform-tests. Servo may be out of sync."
            ]
        )

    def test_open_new_upstreamable_pr_with_prexisting_upstream_pr(self):
        self.assertListEqual(
            self.run_test("opened.json",
                          ["18746.diff"],
                          [MockPullRequest("servo-wpt-sync:servo_export_18746", 1)]),
            [
                "ChangePRStep:wpt/wpt#1:opened:This is a test:<!-- Please...[95]",
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_18746",
                "CommentStep:servo/servo#18746:Transplanted upstreamable changes to existing upstream PR (wpt/wpt#1)."
            ]
        )

    def test_open_new_nonupstreamable_pr_with_prexisting_upstream_pr(self):
        self.assertListEqual(
            self.run_test("opened.json",
                          ["non-wpt.diff"],
                          [MockPullRequest("servo-wpt-sync:servo_export_18746", 1)]),
            [
                "CommentStep:wpt/wpt#1:Downstream PR (servo/servo#18746) no longer contains any upstreamable changes. Closing PR.",
                "ChangePRStep:wpt/wpt#1:closed",
                "RemoveBranchForPRStep",
                "CommentStep:servo/servo#18746:No upstreamable changes; closed existing upstream PR (wpt/wpt#1)."
            ]
        )

    def test_open_new_upstreamable_pr_with_prexisting_upstream_pr_not_apply_cleanly_to_upstream(self):
        self.assertListEqual(
            self.run_test("opened.json",
                          ["does-not-apply-cleanly.diff"],
                          [MockPullRequest("servo-wpt-sync:servo_export_18746", 1)]),
            [
                "ChangePRStep:wpt/wpt#1:opened:This is a test:<!-- Please...[95]",
                "CreateOrUpdateBranchForPRStep",
                "CommentStep:servo/servo#18746:These changes could not be applied onto the latest\n upstream web-platform-tests. Servo may be out of sync.",
                "CommentStep:wpt/wpt#1:The downstream PR (servo/servo#18746) can no longer be applied.\n Waiting for a new version of these changes downstream."
            ]
        )

    def test_closed_pr_no_upstream_pr(self):
        self.assertListEqual(self.run_test("closed.json", ["18746.diff"]), [])

    def test_closed_pr_with_preexisting_upstream_pr(self):
        self.assertListEqual(self.run_test(
            "closed.json", ["18746.diff"],
            [MockPullRequest("servo-wpt-sync:servo_export_18746", 10)]),
            ["ChangePRStep:wpt/wpt#10:closed", "RemoveBranchForPRStep"]
        )

    def test_synchronize_move_new_changes_to_preexisting_upstream_pr(self):
        self.assertListEqual(self.run_test(
            "synchronize.json", ["18746.diff"],
            [MockPullRequest("servo-wpt-sync:servo_export_19612", 10)]),
            [
                "ChangePRStep:wpt/wpt#10:opened:deny warnings:<!-- Please...[142]",
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_19612",
                "CommentStep:servo/servo#19612:Transplanted upstreamable changes to existing upstream PR (wpt/wpt#10)."
            ]
        )

    def test_synchronize_close_upstream_pr_after_new_changes_do_not_include_wpt(self):
        self.assertListEqual(self.run_test(
            "synchronize.json", ["non-wpt.diff"],
            [MockPullRequest("servo-wpt-sync:servo_export_19612", 11)]),
            [
                "CommentStep:wpt/wpt#11:Downstream PR (servo/servo#19612) no longer contains any upstreamable changes. Closing PR.",
                "ChangePRStep:wpt/wpt#11:closed",
                "RemoveBranchForPRStep",
                "CommentStep:servo/servo#19612:No upstreamable changes; closed existing upstream PR (wpt/wpt#11)."
            ]
        )

    def test_synchronize_open_upstream_pr_after_new_changes_include_wpt(self):
        self.assertListEqual(
            self.run_test("synchronize.json", ["18746.diff"]),
            [
                "CreateOrUpdateBranchForPRStep:1:servo-wpt-sync/wpt/servo_export_19612",
                "OpenPRStep:servo-wpt-sync/wpt/servo_export_19612→wpt/wpt#1",
                "CommentStep:servo/servo#19612:Found upstreamable WPT changes. Opened new upstream PR (wpt/wpt#1)."
            ]
        )

    def test_synchronize_fail_to_update_preexisting_pr_after_new_changes_do_not_apply(self):
        self.assertListEqual(self.run_test(
            "synchronize.json", ["does-not-apply-cleanly.diff"],
            [MockPullRequest("servo-wpt-sync:servo_export_19612", 11)]),
            [
                "ChangePRStep:wpt/wpt#11:opened:deny warnings:<!-- Please...[142]",
                "CreateOrUpdateBranchForPRStep",
                "CommentStep:servo/servo#19612:These changes could not be applied onto the latest\n upstream web-platform-tests. Servo may be out of sync.",
                "CommentStep:wpt/wpt#11:The downstream PR (servo/servo#19612) can no longer be applied.\n Waiting for a new version of these changes downstream."
            ]
        )

    def test_synchronize_move_new_changes_to_preexisting_upstream_pr_with_multiple_commits(self):
        self.assertListEqual(
            self.run_test(
                "synchronize-multiple.json",
                ["18746.diff", "non-wpt.diff", "wpt.diff"]
            ),
            [
                "CreateOrUpdateBranchForPRStep:2:servo-wpt-sync/wpt/servo_export_19612",
                "OpenPRStep:servo-wpt-sync/wpt/servo_export_19612→wpt/wpt#1",
                "CommentStep:servo/servo#19612:Found upstreamable WPT changes. Opened new upstream PR (wpt/wpt#1)."
            ]
        )

    def test_synchronize_with_non_upstremable_changes(self):
        self.assertListEqual(self.run_test("synchronize.json", ["non-wpt.diff"]), [])

    def test_merge_upstream_pr_after_merge(self):
        self.assertListEqual(
            self.run_test("merged.json", ["18746.diff"],
                [MockPullRequest("servo-wpt-sync:servo_export_19620", 100)]),
            ["MergePRStep:wpt/wpt#100"]
        )

    def test_pr_merged_no_upstream_pr(self):
        self.assertListEqual(self.run_test("merged.json", ["18746.diff"]), [])

    def test_merge_of_non_upstreamble_pr(self):
        self.assertListEqual(self.run_test("merged.json", ["non-wpt.diff"]), [])

def setUpModule():
    global TMP_DIR, SYNC

    TMP_DIR = tempfile.mkdtemp()
    SYNC = WPTSync(
        servo_repo='servo/servo',
        wpt_repo='wpt/wpt',
        downstream_wpt_repo='servo-wpt-sync/wpt',
        servo_path=os.path.join(TMP_DIR, "servo-mock"),
        wpt_path=os.path.join(TMP_DIR, "wpt-mock"),
        github_api_token='',
        github_api_url=f'http://localhost:{PORT}',
        github_username='servo-wpt-sync',
        github_email='servo-wpt-sync',
        github_name='servo-wpt-sync@servo.org',
        suppress_force_push=True,
    )

    def setup_mock_repo(repo_name, local_repo):
        subprocess.run(["cp", "-R", "-p", os.path.join(TESTS_DIR, repo_name), local_repo.path])
        local_repo.run("init", "-b", "master")
        local_repo.run("add", ".")
        local_repo.run("commit", "-a", "-m", "Initial commit")

    logging.info("=" * 80)
    logging.info(f"Setting up mock repositories")
    setup_mock_repo("servo-mock", SYNC.local_servo_repo)
    setup_mock_repo("wpt-mock", SYNC.local_wpt_repo)
    logging.info("=" * 80)

def tearDownModule():
    global TMP_DIR
    shutil.rmtree(TMP_DIR)

if __name__ == '__main__':
    unittest.main()