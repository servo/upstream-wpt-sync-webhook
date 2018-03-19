from flask import Flask, request, jsonify, render_template, make_response, abort
import json
import os
from sync import UPSTREAMABLE_PATH, git, get_filtered_diff

try:
    xrange
except NameError:
    xrange = range

app = Flask(__name__)
config = {
    'servo_path': None,
    'diff_files': None,
    'upstreamable': {},
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

def commits():
    def make_commit(diff_file):
        this_dir = os.path.abspath(os.path.dirname(__file__))

        diff_file, author, email, message = diff_file

        # Temporarily apply the diff to make a commit object that can be retrieved later
        git(["apply", os.path.join(this_dir, 'tests', diff_file)],
            cwd=config['servo_path'])
        git(["add", "."], cwd=config['servo_path'])
        git(["commit", "-a", "-m", message, "--author", "%s <%s>" % (author, email)],
            cwd=config['servo_path'],
            env={'GIT_COMMITTER_NAME': author,
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

def new_pull_request():
    return {
        "number": 45,
        "html_url": "http://path/to/pull/45",
    }

@app.route("/", defaults={'path': ''})
@app.route("/<path:path>", methods=["POST","PATCH","GET", "DELETE", "PUT"])
def catch_all(path):
    if path.endswith('/commits'):
        return (json.dumps(commits()), 200)
    elif 'commit_metadata/' in path:
        sha = path.split('/')[-1]
        return (json.dumps(commit_with_single_file(config['upstreamable'][sha])), 200)
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
