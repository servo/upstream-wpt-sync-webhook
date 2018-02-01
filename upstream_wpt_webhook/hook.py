#!/usr/bin/env python

from flask import Flask, request, jsonify, render_template, make_response, abort
from functools import partial
from .sync import process_and_run_steps, _do_comment_on_pr, git
import json
import os
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
        assert 'wpt_path' in config
        assert 'upstream_org' in config
        assert 'servo_org' in config
        assert 'port' in config
        assert 'api' in config
        return config

def get_pr_diff(pull_request):
    return requests.get(pull_request["diff_url"]).text

ERROR_BODY = "Error syncing changes upstream. Logs saved in %s."

def error_callback(config, dry_run, payload, dir_name):
    if not dry_run:
        _do_comment_on_pr(config, payload["pull_request"]["number"], ERROR_BODY % dir_name)

def _webhook_impl(pr_db, dry_run):
    payload = request.form.get('payload', '{}')
    payload = json.loads(payload)
    result = process_and_run_steps(config, pr_db, payload, get_pr_diff, dry_run,
                                   error_callback=partial(error_callback, config, dry_run, payload))
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

@app.route("/ping")
def ping():
    return ('pong', 200)

@app.route("/shutdown", methods=["POST"])
def shutdown():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()
    return ('', 204)

def main(_config, _pr_db):
    global config, pr_db
    config = _config
    pr_db = _pr_db
    app.run(port=config['port'])

def start():
    config = read_config()
    if not os.path.isdir(config['wpt_path']):
        git(["clone", "--depth=1", "https://github.com/w3c/web-platform-tests.git", config["wpt_path"]], cwd='.')
    main(config, read_pr_db())

if __name__ == "__main__":
    start()
