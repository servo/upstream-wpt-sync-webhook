import flask
import json
import logging
import requests
import threading
import time

from dataclasses import dataclass, asdict
from flask import Flask, request
from sync import UPSTREAMABLE_PATH
from typing import NamedTuple
from wsgiref.simple_server import WSGIRequestHandler, make_server

@dataclass
class MockPullRequest():
    head: str
    number: int
    state: str = "open"

class MockGitHubAPIServer(object):
    def __init__(self, port: int):
        self.port = port
        self.disable_logging()
        self.app = flask.Flask(__name__)

        class NoLoggingWSGIRequestHandler(WSGIRequestHandler):
            def log_message(self, format, *args):
                pass
        handler = NoLoggingWSGIRequestHandler
        if logging.getLogger().level == logging.DEBUG:
            handler = WSGIRequestHandler
        self.server = make_server('localhost', self.port, self.app, handler_class=handler)
        self.start_server_thread()

    def disable_logging(self):
        import flask.cli
        flask.cli.show_server_banner = lambda *args: None
        logging.getLogger("werkzeug").disabled = True
        logging.getLogger('werkzeug').setLevel(logging.CRITICAL)

    def start(self):
        self.thread.start()

        # Wait for the server to be started.
        while True:
            try:
                r = requests.get(f'http://localhost:{self.port}/ping')
                assert(r.status_code == 200)
                assert(r.text == 'pong')
                break
            except:
                time.sleep(0.1)

    def reset_server_state_with_prs(self, prs: list[MockPullRequest]):
        response = requests.get(
            f'http://localhost:{self.port}/reset-mock-github',
            json=[asdict(pr) for pr in prs]
        )
        assert(response.status_code == 200)
        assert(response.text == '👍')

    def shutdown(self):
        self.server.shutdown()
        self.thread.join()

    def start_server_thread(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        @self.app.route("/ping")
        def ping(self):
            return ('pong', 200)

        @self.app.route("/reset-mock-github")
        def reset_server():
            self.pulls = [MockPullRequest(pr['head'], pr['number'], pr['state']) for pr in flask.request.json]
            return ('👍', 200)

        @self.app.route("/repos/<org>/<repo>/pulls/<int:number>/merge", methods=['PUT'])
        def merge_pull_request(org, repo, number):
            for pr in self.pulls:
                if pr.number == number:
                    pr.state = 'closed'
                    return ('', 204)
            return ('', 404)

        @self.app.route("/repos/<org>/<repo>/pulls", methods=['GET'])
        def get_pulls(*args, **kwargs):
            for pr in self.pulls:
                if pr.head == flask.request.args["head"]:
                    return json.dumps([{"number": pr.number}])
            return json.dumps([])

        @self.app.route("/repos/<org>/<repo>/pulls", methods=['POST'])
        def create_pull_request(org, repo):
            new_pr_number = len(self.pulls) + 1
            self.pulls.append(MockPullRequest(
                request.json["head"],
                new_pr_number,
                "open"
            ))
            return { "number": new_pr_number }

        @self.app.route("/repos/<org>/<repo>/pulls/<int:number>", methods=['PATCH'])
        def update_pull_request(org, repo, number):
            for pr in self.pulls:
                if pr.number == number:
                    if 'state' in flask.request.json:
                        pr.state = flask.request.json['state']
                    return ('', 204)
            return ('', 404)

        @self.app.route("/repos/<org>/<repo>/issues/<number>/labels", methods=['GET', 'POST'])
        @self.app.route("/repos/<org>/<repo>/issues/<number>/labels/<label>", methods=['DELETE'])
        @self.app.route("/repos/<org>/<repo>/issues/<issue>/comments", methods=['GET', 'POST'])
        def other_requests(*args, **kwargs):
            return ('', 204)