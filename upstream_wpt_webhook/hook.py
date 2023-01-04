#!/usr/bin/env python

import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from flask import Flask, request, jsonify, render_template, make_response, abort
from functools import partial
from sync import process_and_run_steps, _do_comment_on_pr, modify_upstream_pr_labels, git, UPSTREAMABLE_PATH, fetch_upstream_branch
import json
import requests

app = Flask(__name__)
config = None
pr_db = None

@app.route("/")
def index():
    return "Hi!"

def read_pr_db():
    try:
        with open('pr_map.json') as f:
            return json.loads(f.read())
    except:
        return {}

def read_config():
    with open('config.json') as f:
        config = json.loads(f.read())
        assert 'token' in config
        assert 'username' in config
        assert 'servo_path' in config
        assert 'wpt_path' in config
        assert 'upstream_org' in config
        assert 'servo_org' in config
        assert 'port' in config
        assert 'api' in config
        return config

def get_pr_diff(pull_request):
    return requests.get(pull_request["diff_url"]).text

ERROR_BODY = "Error syncing changes upstream. Logs saved in %s."
UPSTREAM_ERROR_BODY = "Error merging pull request automatically. Please merge manually after addressing any CI issues."

def error_callback(config, payload, pr_db, dir_name):
    _do_comment_on_pr(config, payload["pull_request"]["number"], ERROR_BODY % dir_name)
    if 'action' in payload and payload['action'] == 'closed' and payload['pull_request']['merged']:
        pr_number = pr_db[payload["pull_request"]["number"]]
        modify_upstream_pr_labels(config, 'POST', ['stale-sevo-export'], pr_number)
        _do_comment_on_pr(config, pr_number, UPSTREAM_ERROR_BODY)


def _webhook_impl(pr_db, dry_run):
    payload = request.form.get('payload', '{}')
    payload = json.loads(payload)
    error = partial(error_callback, config, payload, pr_db) if not dry_run else None
    if dry_run:
        branch_name = "master"
    else:
        branch_name = "pull/%s/head" % payload["pull_request"]["number"]
    result = process_and_run_steps(config, pr_db, payload, get_pr_diff, branch_name,
                                   error_callback=error)
    if not result:
        return ('', 500)

    if not dry_run:
        with open('pr_map.json', 'w') as f:
            f.write(json.dumps(pr_db))
    return ('', 204)

@app.route("/hook", methods=["POST"])
def webhook():
    return _webhook_impl(pr_db, False)

@app.route("/test", methods=["POST"])
def test():
    return _webhook_impl(pr_db, True)

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

def main(_config, _pr_db):
    global config, pr_db
    config = _config
    pr_db = _pr_db
    app.run(port=config['port'])

def start():
    config = read_config()
    if not os.path.isdir(config['wpt_path']):
        git(["clone", "https://github.com/w3c/web-platform-tests.git", config["wpt_path"]], cwd='.')
    if not os.path.isdir(config['servo_path']):
        git(["clone", "https://github.com/servo/servo.git", config["servo_path"]], cwd='.')
    main(config, read_pr_db())

if __name__ == "__main__":
    start()
