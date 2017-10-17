This is a github webhook that is intended to be run as a flask server.
Its purpose is to watch the servo/servo repository for changes to
the web-platform-tests subdirectory, and transplant each commit
that modifies that directory to an equivalent one that applies
to the [web-platform-tests repository](https://github.com/w3c/web-platform-tests/).

To operate, a `config.json` file is required that looks like this:
```json
{
    "servo_token": "github token allowing access to w3c/web-platform-tests and servo/servo"",
    "username": "user",
    "wpt_path": "relative/path/to/web-platform-tests/clone",
    "upstream_org": "w3c",
    "servo_org": "servo",
    "port": 5000,
}
```

Username is the github user that is used in order to push to the
upstream orgazination's web-platform-tests repository.

When it works as expected, the following control flow occurs:
* when a new PR is opened in servo/servo:
  * if it contains WPT changes:
    *  a new branch is created in the local `web-platform-tests` clone
    * the relevant commits are transplanted into this branch
    * the branch is pushed to the upstream repository
    * a pull request is created for the new remote branch
  * otherwise, the PR is ignored
* when an open PR receives new changes:
  * if it contains WPT changes and there is no existing PR, the same steps as a brand new PR are followed
  * if it contains WPT changes and there is an existing PR, the branch is force-pushed with the updated transplanted commits
  * if it does not contain WPT changes:
    * if there is an existing associated upstream PR, it is closed
    * otherwise the PR is ignored
* when a PR is closed:
  * if the PR was merged and there is an associated upstream PR, it is also merged upstream
  * if the PR was not merged and there is an associated upstream PR, it is also closed
  * otherwise the PR is ignored
