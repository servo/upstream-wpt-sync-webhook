# pylint: disable=broad-except
# pylint: disable=dangerous-default-value
# pylint: disable=missing-docstring
# pylint: disable=unused-argument

import json
import logging
import threading
import time

from dataclasses import dataclass, asdict
from wsgiref.simple_server import WSGIRequestHandler, make_server

import flask
import flask.cli
import requests


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
        self.pulls: list[MockPullRequest] = []

        class NoLoggingHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass
        if logging.getLogger().level == logging.DEBUG:
            handler = WSGIRequestHandler
        else:
            handler = NoLoggingHandler

        self.server = make_server('localhost', self.port, self.app, handler_class=handler)
        self.start_server_thread()

    def disable_logging(self):
        flask.cli.show_server_banner = lambda *args: None
        logging.getLogger("werkzeug").disabled = True
        logging.getLogger('werkzeug').setLevel(logging.CRITICAL)

    def start(self):
        self.thread.start()

        # Wait for the server to be started.
        while True:
            try:
                response = requests.get(f'http://localhost:{self.port}/ping', timeout=1)
                assert response.status_code == 200
                assert response.text == 'pong'
                break
            except Exception:
                time.sleep(0.1)

    def reset_server_state_with_pull_requests(self, pulls: list[MockPullRequest]):
        response = requests.get(
            f'http://localhost:{self.port}/reset-mock-github',
            json=[asdict(pull_request) for pull_request in pulls],
            timeout=1
        )
        assert response.status_code == 200
        assert response.text == '👍'

    def shutdown(self):
        self.server.shutdown()
        self.thread.join()

    def start_server_thread(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        @self.app.route("/ping")
        def ping():
            return ('pong', 200)

        @self.app.route("/reset-mock-github")
        def reset_server():
            self.pulls = [
                MockPullRequest(pull_request['head'],
                                pull_request['number'],
                                pull_request['state'])
                for pull_request in flask.request.json]
            return ('👍', 200)

        @self.app.route("/repos/<org>/<repo>/pulls/<int:number>/merge", methods=['PUT'])
        def merge_pull_request(org, repo, number):
            for pull_request in self.pulls:
                if pull_request.number == number:
                    pull_request.state = 'closed'
                    return ('', 204)
            return ('', 404)

        @self.app.route("/repos/<org>/<repo>/pulls", methods=['GET'])
        def get_pulls(org, repo):
            for pull_request in self.pulls:
                if pull_request.head == flask.request.args["head"]:
                    return json.dumps([{"number": pull_request.number}])
            return json.dumps([])

        @self.app.route("/repos/<org>/<repo>/pulls", methods=['POST'])
        def create_pull_request(org, repo):
            new_pr_number = len(self.pulls) + 1
            self.pulls.append(MockPullRequest(
                flask.request.json["head"],
                new_pr_number,
                "open"
            ))
            return {"number": new_pr_number}

        @self.app.route("/repos/<org>/<repo>/pulls/<int:number>", methods=['PATCH'])
        def update_pull_request(org, repo, number):
            for pull_request in self.pulls:
                if pull_request.number == number:
                    if 'state' in flask.request.json:
                        pull_request.state = flask.request.json['state']
                    return ('', 204)
            return ('', 404)

        @self.app.route("/repos/<org>/<repo>/issues/<number>/labels", methods=['GET', 'POST'])
        @self.app.route("/repos/<org>/<repo>/issues/<number>/labels/<label>", methods=['DELETE'])
        @self.app.route("/repos/<org>/<repo>/issues/<issue>/comments", methods=['GET', 'POST'])
        def other_requests(*args, **kwargs):
            return ('', 204)
