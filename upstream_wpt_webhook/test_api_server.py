from flask import Flask, request, jsonify, render_template, make_response, abort
import json
import os
from sync import UPSTREAMABLE_PATH

try:
    xrange
except NameError:
    xrange = range

app = Flask(__name__)
config = {
    'upstreamable_commits': 0,
    'non_upstreamable_commits': 0,
}

def start_server(port, _config):
    global config
    config.update(_config)
    app.run(port=port)

@app.route("/shutdown", methods=["POST"])
def shutdown():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()
    return ('', 204)

@app.route("/ping")
def ping():
    return ('pong', 200)

def commits(upstreamable=0, non_upstreamable=0):
    def make_commit(upstreamble):
        return {
            "url": "/commit_metadata",
            "html_url": "18746" if upstreamable else "non-wpt",
            "commit": {
                "author": {
                    "name": "foo",
                    "email": "foo@foo",
                },
                "message": "%supstreamable commit" % ("non-" if not upstreamable else ""),
            },
        }
    commits = []
    for i in xrange(upstreamable):
        commits += [make_commit(True)]
    for i in xrange(non_upstreamable):
        commits += [make_commit(False)]
    return commits

def commit_with_single_file(upstreamable):
    return {
        "files": [{
            "filename": "%sfoo" % (UPSTREAMABLE_PATH if upstreamable else "something/else/")
        }]
    }

def new_pull_request():
    return {
        "number": 45,
        "html_url": "http://path/to/pull/45",
    }

@app.route("/", defaults={'path': ''})
@app.route("/<path:path>", methods=["POST","PATCH","GET", "DELETE", "PUT"])
def catch_all(path):
    if path.endswith('/commits'):
        return (json.dumps(commits(config['upstreamable_commits'],
                                   config['non_upstreamable_commits'])), 200)
    elif path.endswith('commit_metadata'):
        return (json.dumps(commit_with_single_file(config['upstreamable_commits'] != 0)), 200)
    elif path.endswith('pulls'):
        return (json.dumps(new_pull_request()), 200)
    elif path.endswith('merge'):
        return ('', 204)
    elif '/pulls/' in path:
        return ('', 204)
    elif path.endswith('labels'):
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
