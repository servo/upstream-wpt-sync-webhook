#!/usr/bin/env python

import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from flask import Flask, request, jsonify, render_template, make_response, abort
from functools import partial
from sync import process_and_run_steps, _do_comment_on_pr, modify_upstream_pr_labels, git, UPSTREAMABLE_PATH, fetch_upstream_branch, get_upstream_pr_number_from_pr
import json
import requests

app = Flask(__name__)
config = None

@app.route("/")
def index():
    return "Hi!"

def read_config():
    with open('config.json') as f:
        config = json.loads(f.read())
        assert 'token' in config
        assert 'username' in config
        assert 'servo_path' in config
        assert 'wpt_path' in config

        assert 'servo_repo' in config
        assert 'wpt_repo' in config
        assert 'downstream_wpt_repo' in config

        assert 'port' in config
        assert 'api' in config
        return config

def get_pr_diff(pull_request):
    return requests.get(pull_request["diff_url"]).text

ERROR_BODY = "Error syncing changes upstream. Logs saved in %s."
UPSTREAM_ERROR_BODY = "Error merging pull request automatically. Please merge manually after addressing any CI issues."

def error_callback(config, payload, dir_name):
    pull_request = payload["pull_request"]
    pull_request_number = pull_request["number"]

    _do_comment_on_pr(config, pull_request_number, ERROR_BODY % dir_name)
    if 'action' in payload and payload['action'] == 'closed' and payload['pull_request']['merged']:
        upstream_pr_number = get_upstream_pr_number_from_pr(config, pull_request)
        if upstream_pr_number:
            modify_upstream_pr_labels(config, 'POST', ['stale-sevo-export'], upstream_pr_number)
            _do_comment_on_pr(config, upstream_pr_number, UPSTREAM_ERROR_BODY)


def _webhook_impl(dry_run):
    payload = request.form.get('payload', '{}')
    payload = json.loads(payload)
    error = partial(error_callback, config, payload) if not dry_run else None
    if dry_run:
        branch_name = "master"
    else:
        branch_name = "pull/%s/head" % payload["pull_request"]["number"]
    result = process_and_run_steps(config, payload, get_pr_diff, branch_name,
                                   error_callback=error)
    if not result:
        return ('', 500)

    return ('', 204)

@app.route("/hook", methods=["POST"])
def webhook():
    return _webhook_impl(False)

@app.route("/test", methods=["POST"])
def test():
    return _webhook_impl(True)

exiting = False
@app.route("/ping")
def ping():
    global exiting
    if exiting:
        exiting = False
        sys.exit()
    return ('pong', 200)

@app.route("/shutdown", methods=["POST"])
def shutdown():
    global exiting
    exiting = True
    return ('', 204)

def main(_config):
    global config
    config = _config
    app.run(port=config['port'])

def start():
    config = read_config()
    if not os.path.isdir(config['wpt_path']):
        git(["clone",
             "--depth", "1",
             "https://github.com/w3c/web-platform-tests.git",
             config["wpt_path"]], cwd='.')
    if not os.path.isdir(config['servo_path']):
        git(["clone", 
             "--depth", "1",
             "https://github.com/servo/servo.git",
             config["servo_path"]], cwd='.')
    main(config)

if __name__ == "__main__":
    start()
