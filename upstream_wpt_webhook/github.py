from __future__ import annotations

import requests
import urllib
import logging

from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sync import SyncRun

"""
This modules contains some abstractions of GitHub repositories. It could one
day be entirely replaced with something like PyGithub.
"""

USER_AGENT = "Servo web-platform-test sync service"


def authenticated(context: SyncRun, method, url, json=None) -> requests.Response:
    logging.info(f"  → Request: {method} {url}")
    if json:
        logging.info(f"  → Request JSON: {json}")

    headers = {
        "Authorization": f"Bearer {context.github_api_token}",
        "User-Agent": USER_AGENT
    }

    url = urllib.parse.urljoin(context.github_api_url, url)
    response = requests.request(method, url, headers=headers, json=json)
    if int(response.status_code / 100) != 2:
        raise ValueError(
            "got unexpected %d response: %s" % (response.status_code, response.text)
        )
    return response


class GithubRepository:
    """
    This class allows interacting with a single GitHub repository.
    """

    def __init__(self, context: SyncRun, repo: str):
        self.context = context
        self.repo = repo
        self.org = repo.split("/")[0]
        self.pulls_url = f"repos/{self.repo}/pulls"

    def __str__(self):
        return self.repo

    def get_pull_request(self, number: int) -> PullRequest:
        return PullRequest(self, number)

    def get_branch(self, name: str) -> GithubBranch:
        return GithubBranch(self, name)

    def get_open_pull_request_for_branch(
        self, branch: GithubBranch
    ) -> Optional[PullRequest]:
        """If this repository has an open pull request with the
        given source head reference targeting the master branch,
        return the first matching pull request, otherwise return None."""
        head = branch.get_pr_head_reference_for_repo(self)
        response = authenticated(
            self.context, "GET", f"{self.pulls_url}?head={head}&base=master&state=open"
        )
        if int(response.status_code / 100) != 2:
            return None

        json = response.json()
        if not json or not type(json) is list:
            return None
        return self.get_pull_request(json[0]["number"])

    def open_pull_request(self, branch: GithubBranch, title: str, body: str):
        data = {
            "title": title,
            "head": branch.get_pr_head_reference_for_repo(self),
            "base": "master",
            "body": body,
            "maintainer_can_modify": False,
        }
        r = authenticated(self.context, "POST", self.pulls_url, json=data)
        return self.get_pull_request(r.json()["number"])


class GithubBranch:
    def __init__(self, repo: GithubRepository, branch_name: str):
        self.repo = repo
        self.name = branch_name

    def __str__(self):
        return f"{self.repo}/{self.name}"

    def get_pr_head_reference_for_repo(self, other_repo: GithubRepository) -> str:
        """Get the head reference to use in pull requests for the given repository.
        If the organization is the same this is just `<branchname>` otherwise
        it will be `<orgname>:<branchname>`."""
        if self.repo.org == other_repo.org:
            return self.name
        else:
            return f"{self.repo.org}:{self.name}"


class PullRequest:
    """
    This class allows interacting with a single pull request on GitHub.
    """

    def __init__(self, repo: GithubRepository, number: int):
        self.repo = repo
        self.context = repo.context
        self.number = number
        self.base_url = f"repos/{self.repo.repo}/pulls/{self.number}"
        self.base_issues_url = f"repos/{self.repo.repo}/issues/{self.number}"

    def __str__(self):
        return f"{self.repo}#{self.number}"

    def api(self, *args, **kwargs) -> requests.Response:
        return authenticated(self.context, *args, **kwargs)

    def leave_comment(self, comment: str):
        return self.api("POST", f"{self.base_issues_url}/comments", json={"body": comment})

    def change(self, state: str = None, title: str = None, body: str = None):
        data = {}
        if title:
            data["title"] = title
        if body:
            data["body"] = body
        if state:
            data["state"] = state
        return self.api("PATCH", self.base_url, json=data)

    def remove_label(self, label: str):
        self.api("DELETE", f"{self.base_issues_url}/labels/{label}")

    def add_labels(self, labels: list[str]):
        self.api("POST", f"{self.base_issues_url}/labels", json=labels)

    def merge(self):
        self.api("PUT", f"{self.base_url}/merge", json={"merge_method": "rebase"})
