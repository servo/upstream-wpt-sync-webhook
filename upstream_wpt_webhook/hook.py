#!/usr/bin/env python

from flask import Flask, request, jsonify, render_template, make_response, abort
from hook import process_and_run_steps, _do_comment_on_pr, pr_db, config
import json

app = Flask(__name__)

@app.route("/")
def index():
    return "Hi!"

ERROR_BODY = "Error syncing changes upstream. Logs saved in %s."

@app.route("/hook", methods=["POST"])
def webhook():
    payload = request.form.get('payload', '{}')
    payload = json.loads(payload)
    if not process_and_run_steps(payload, get_pr_diff, False, None):
        _do_comment_on_pr(payload["pull_request"]["number"], ERROR_BODY % dir_name)
        return ('', 503)

    with open('pr_map.json', 'w') as f:
        f.write(json.dumps(pr_db))
    return ('', 204)

def main():
    app.run(port=config['port'])

if __name__ == "__main__":
    main()
