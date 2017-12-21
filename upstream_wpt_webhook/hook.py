#!/usr/bin/env python

from flask import Flask, request, jsonify, render_template, make_response, abort
from functools import partial
from hook import process_and_run_steps, _do_comment_on_pr, pr_db, config
import json
import requests

app = Flask(__name__)

@app.route("/")
def index():
    return "Hi!"

def get_pr_diff(pull_request):
    return requests.get(pull_request["diff_url"]).text

ERROR_BODY = "Error syncing changes upstream. Logs saved in %s."

def error_callback(payload, dir_name):
    _do_comment_on_pr(payload["pull_request"]["number"], ERROR_BODY % dir_name)

@app.route("/hook", methods=["POST"])
def webhook():
    payload = request.form.get('payload', '{}')
    payload = json.loads(payload)
    if not process_and_run_steps(payload, get_pr_diff, False, error_callback=partial(error_callback, payload)):
        return ('', 503)

    with open('pr_map.json', 'w') as f:
        f.write(json.dumps(pr_db))
    return ('', 204)

def main():
    app.run(port=config['port'])

if __name__ == "__main__":
    main()
