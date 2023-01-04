import json
import locale
import os
import sys
import tempfile

from flask import Flask, request, jsonify, render_template, make_response, abort
from sync import UPSTREAMABLE_PATH, git, get_filtered_diff

try:
    xrange
except NameError:
    xrange = range

app = Flask(__name__)
exiting = False

config = {
    'servo_path': None,
    'diff_files': None,
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


def commits():
    def make_commit(diff_file):
        this_dir = os.path.abspath(os.path.dirname(__file__))

        diff_file, author, email, message = diff_file

        # Temporarily apply the diff to make a commit object that can be retrieved later
        git(["apply", os.path.join(this_dir, 'tests', diff_file)],
            cwd=config['servo_path'])
        git(["add", "."], cwd=config['servo_path'])
        git(["commit", "-a", "--author",
             "%s <%s>" % (author, email),
             "-m", message],
            cwd=config['servo_path'],
            env={'GIT_COMMITTER_NAME': author.encode(locale.getpreferredencoding()),
                 'GIT_COMMITTER_EMAIL': email})
        output = git(["log", "-1", "--oneline"], cwd=config['servo_path'])
        sha = output.split()[0]
        config['upstreamable'][sha] = (get_filtered_diff(config['servo_path'], sha) == '')
        git(["reset", "--hard", "HEAD^"], cwd=config['servo_path'])

        return {
            "url": "/commit_metadata/" + sha,
            "html_url": diff_file[:-5],  # strip trailing .diff
            "sha": sha,
            "commit": {
                "author": {
                    "name": author,
                    "email": email,
                },
                "message": message,
            },
        }
    return list(map(lambda x: make_commit(x), config['diff_files']))

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
    if path.endswith('/commits'):
        return (json.dumps(commits()), 200)
    elif 'commit_metadata/' in path:
        sha = path.split('/')[-1]
        return (json.dumps(commit_with_single_file(config['upstreamable'][sha])), 200)
    elif path.endswith('pulls') and request.method == 'POST':
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
