import json
import locale
import os
import sys
import tempfile

from flask import Flask, request, jsonify, render_template, make_response, abort
from sync import UPSTREAMABLE_PATH, git

try:
    xrange
except NameError:
    xrange = range

app = Flask(__name__)
exiting = False

config = {
    'servo_path': None,
    'upstreamable': {},
}
pr_database = {}

def start_server(port, _config):
    global config
    config.update(_config)

    global pr_database
    pr_database = config['pr_database']
    app.run(port=port)

exiting = False
@app.route("/shutdown", methods=["POST"])
def shutdown():
    global exiting
    exiting = True
    return ('', 204)

@app.route("/ping")
def ping():
    global exiting
    if exiting:
        exiting = False
        sys.exit()
    return ('pong', 200)


def commit_with_single_file(upstreamable):
    return {
        "files": [{
            "filename": "%sfoo" % (UPSTREAMABLE_PATH if upstreamable else "something/else/")
        }]
    }

def new_pull_request(new_pr):
    global pr_database
    number = len(pr_database)
    pr_database[(new_pr["head"], new_pr["base"])] = number
    return {
        "number": number,
        "html_url": f"http://path/to/pull/{number}"
    }

def get_pull_requests(args):
    global pr_database
    key = (args["head"], args["base"])
    if key not in pr_database:
        return []

    number = pr_database[key]
    return [{
        "number": number,
        "html_url": f"http://path/to/pull/{number}"
    }]

@app.route("/", defaults={'path': ''})
@app.route("/<path:path>", methods=["POST","PATCH","GET", "DELETE", "PUT"])
def catch_all(path):
    if path.endswith('pulls') and request.method == 'POST':
        return (json.dumps(new_pull_request(request.json)), 200)
    elif path.endswith('pulls') and request.method == 'GET':
        return (json.dumps(get_pull_requests(request.args)), 200)
    elif path.endswith('merge'):
        return ('', 204)
    elif '/pulls/' in path:
        return ('', 204)
    elif '/labels/' in path or path.endswith('/labels'):
        return ('', 204)
    elif path.endswith('comments'):
        return ('', 204)
    elif path.endswith(".diff"):
        fname = os.path.join('tests', path[path.rfind('/') + 1:])
        try:
            with open(fname) as f:
                return (f.read(), 200)
        except:
            return ('Diff not found: %s' % fname, 404)

    return ('Unexpected request: %s' % path, 500)
