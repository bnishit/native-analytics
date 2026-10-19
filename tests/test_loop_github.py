import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import HTTPException

from loop_github import GitHub
from test_loop_recipe import BASELINE, REPAIRED


REPO = "clinic/answers"
GOAL_PATH = ".afterspan/goals/first-answer.json"
WORKFLOW = ".github/workflows/check.yml"
BASE, HEAD, MERGE = "a" * 40, "b" * 40, "c" * 40
ACTION = "0d012884-7f2a-4996-8eb8-a2f8a3b52125"
BRANCH = "afterspan/loop/" + ACTION
TOKEN = "private-token-never-return"


def blob(text):
    content = text.encode()
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


class FakeHTTP:
    def __init__(self):
        self.goal = {"schema": "afterspan-goal-v1", "id": "first-answer", "question": "Can owners trust the first answer?",
                     "measurement": {"unit": "workspace", "entry": {"event": "codex.question.started"},
                                     "completion": {"event": "codex.answer.ready"}, "followup_seconds": 86400,
                                     "entry_rule": "first_in_entry_window", "rubric": "aggregate-answer-v1"},
                     "mapping": {"source": "native", "environment": "local", "observation_id_property": "observation_id"},
                     "tracking": {"recipe": "python-answer-v1", "path": "examples/loop_pilot.py", "dependencies": ["examples/loop_pilot.py"]}}
        self.files = {BASE: {GOAL_PATH: json.dumps(self.goal), "examples/loop_pilot.py": BASELINE, WORKFLOW: "name: Check\non: pull_request\n"}}
        self.parents = {BASE: []}
        self.branches = {"main": BASE}
        self.pr = None
        self.writes = []
        self.requests = []
        self.timeout = None
        self.apply_timeout = True
        self.read_failure = None
        self.read_overrides = {}
        self.read_failure_after_write = None
        self.repository_id = 1001
        self.protected = True
        self.protection = {"required_status_checks": {"strict": True, "contexts": ["test"], "checks": [{"context": "test", "app_id": 15368}]},
                           "enforce_admins": {"enabled": True}, "required_pull_request_reviews": {"dismiss_stale_reviews": True, "required_approving_review_count": 1}}
        self.check = {"name": "test", "head_sha": HEAD, "status": "completed", "conclusion": "success", "app": {"id": 15368}, "check_suite": {"id": 20}}
        self.run = {"check_suite_id": 20, "head_sha": HEAD, "path": WORKFLOW, "status": "completed", "conclusion": "success", "repository": {"full_name": REPO}}
        self.reviews = [{"id": 30, "user": {"id": 2, "type": "User"}, "state": "APPROVED", "commit_id": HEAD}]
        self.deployments = []
        self.deployment_statuses = {}

    def connection(self, host, timeout):
        self.assert_host(host, timeout)
        upstream = self

        class Connection:
            def request(self, method, path, body=None, headers=None):
                self.result = upstream.request(method, path, body, headers)

            def getresponse(self):
                status, value = self.result

                class Response:
                    def __init__(self):
                        self.status = status

                    def read(self, limit):
                        return json.dumps(value).encode()[:limit]

                return Response()

            def close(self):
                pass

        return Connection()

    def assert_host(self, host, timeout):
        if host != "api.github.com" or timeout != 15:
            raise AssertionError("Only fixed GitHub HTTPS is permitted")

    def request(self, method, fullpath, body, headers):
        if not fullpath.startswith("/repos/" + REPO) or headers.get("Authorization") != "Bearer " + TOKEN:
            raise AssertionError("Unexpected repository or authentication")
        parsed = urlsplit(fullpath[len("/repos/" + REPO):])
        path, query = unquote(parsed.path), parse_qs(parsed.query)
        value = json.loads(body) if body else None
        self.requests.append((method, path, value))
        if method == "GET":
            if path in self.read_overrides:
                return self.read_overrides[path]
            if self.read_failure and self.read_failure in path:
                return 503, {"message": TOKEN + " provider private body"}
            return self.read(path, query)
        stage = {("POST", "/git/refs"): "branch", ("PUT", "/contents/examples/loop_pilot.py"): "file",
                 ("POST", "/pulls"): "pr", ("PUT", "/pulls/7/merge"): "merge"}.get((method, path))
        self.writes.append((stage, value))
        timed_out = self.timeout == stage
        if timed_out:
            self.timeout = None
        if not timed_out or self.apply_timeout:
            result = self.write(method, path, value)
            if self.read_failure_after_write == stage:
                self.read_failure = "/pulls"
        if timed_out:
            raise TimeoutError("private provider failure " + TOKEN)
        return result

    def read(self, path, query):
        if path == "":
            return 200, {"id": self.repository_id, "full_name": REPO, "archived": False}
        if path == "/branches/main":
            return 200, {"name": "main", "commit": {"sha": self.branches["main"]}, "protected": self.protected}
        if path == "/branches/main/protection":
            return 200, self.protection
        if path.startswith("/contents/"):
            ref = query["ref"][0]
            commit = self.branches.get(ref, ref)
            name = path[len("/contents/"):]
            content = self.files.get(commit, {}).get(name)
            if content is None:
                return 404, {"message": "missing"}
            return 200, {"type": "file", "encoding": "base64", "size": len(content.encode()), "sha": blob(content),
                         "content": base64.b64encode(content.encode()).decode()}
        if path.startswith("/git/commits/"):
            sha = path.rsplit("/", 1)[1]
            return 200, {"sha": sha, "tree": {"sha": self.tree(sha)}, "parents": [{"sha": parent} for parent in self.parents[sha]]}
        if path.startswith("/git/trees/"):
            tree = path.rsplit("/", 1)[1]
            commit = next(sha for sha in self.files if self.tree(sha) == tree)
            return 200, {"truncated": False, "tree": [{"path": name, "sha": blob(content), "type": "blob", "mode": "100644"}
                                                      for name, content in self.files[commit].items()]}
        if path == "/pulls":
            return 200, [{"number": 7}] if self.pr else []
        if path == "/pulls/7":
            return 200, self.pr
        if path.startswith("/git/ref/heads/"):
            branch = path[len("/git/ref/heads/"):]
            return (200, {"object": {"sha": self.branches[branch]}}) if branch in self.branches else (404, {"message": "missing"})
        if path.startswith("/compare/"):
            base, head = path[len("/compare/"):].split("...")
            changed = [{"filename": name, "status": "modified", "sha": blob(content), "additions": 1, "deletions": 0}
                       for name, content in self.files[head].items() if content != self.files[base].get(name)]
            return 200, {"status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1, "files": changed}
        if path == "/commits/" + HEAD + "/check-runs":
            return 200, {"total_count": 1, "check_runs": [self.check]}
        if path == "/actions/runs":
            return 200, {"total_count": 1, "workflow_runs": [self.run]}
        if path == "/pulls/7/reviews":
            return 200, self.reviews
        if path == "/deployments":
            return 200, self.deployments
        if path.startswith("/deployments/"):
            return 200, self.deployment_statuses.get(int(path.split("/")[2]), [])
        raise AssertionError("Unexpected GET " + path)

    def tree(self, commit):
        return hashlib.sha1(json.dumps(self.files[commit], sort_keys=True).encode()).hexdigest()

    def write(self, method, path, value):
        if method == "POST" and path == "/git/refs":
            self.branches[value["ref"][len("refs/heads/"):]] = value["sha"]
            return 201, {"object": {"sha": value["sha"]}}
        if method == "PUT" and path == "/contents/examples/loop_pilot.py":
            parent = self.branches[value["branch"]]
            self.files[HEAD] = self.files[parent] | {"examples/loop_pilot.py": base64.b64decode(value["content"]).decode()}
            self.parents[HEAD] = [parent]
            self.branches[value["branch"]] = HEAD
            return 200, {"commit": {"sha": HEAD}}
        if method == "POST" and path == "/pulls":
            self.pr = {"number": 7, "body": value["body"], "state": "open", "draft": value["draft"], "merged": False,
                       "merge_commit_sha": None, "mergeable": True, "mergeable_state": "clean", "user": {"id": 1},
                       "base": {"ref": value["base"], "sha": BASE, "repo": {"id": self.repository_id, "full_name": REPO}},
                       "head": {"ref": value["head"], "sha": self.branches[value["head"]], "repo": {"id": self.repository_id, "full_name": REPO}}}
            return 201, self.pr
        if method == "PUT" and path == "/pulls/7/merge":
            if value != {"sha": HEAD, "merge_method": "squash"}:
                raise AssertionError("The exact head must be bound to merge")
            self.pr.update({"state": "closed", "merged": True, "merge_commit_sha": MERGE})
            self.files[MERGE] = copy.deepcopy(self.files[HEAD])
            self.parents[MERGE] = [BASE]
            self.branches["main"] = MERGE
            return 200, {"merged": True, "sha": MERGE}
        raise AssertionError("Unexpected mutation " + path)


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.transport = patch("loop_github.http.client.HTTPSConnection", lambda host, timeout: self.http.connection(host, timeout))
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.github = GitHub(REPO, TOKEN)
        self.guards = 0
        self.source = {"repository": REPO, "base": "main", "commit": BASE, "path": GOAL_PATH,
                       "content_sha256": hashlib.sha256(self.http.files[BASE][GOAL_PATH].encode()).hexdigest()}

    def guard(self):
        self.guards += 1

    def plan(self):
        return self.github.prepare(self.github.load_goal(self.source))

    def drafted(self):
        plan = self.plan()
        receipt = self.github.draft(plan, ACTION, self.guard)
        return plan, receipt

    def policy(self, plan):
        return {"state": "active", "repository": REPO, "base": "main", "operations": ["draft-pr", "merge", "release-on-merge"],
                "paths": ["examples/loop_pilot.py"], "environment": "local", "expires_at": "2099-01-01T00:00:00Z",
                "required_checks": [{"name": "test", "app_id": 15368, "workflow_path": WORKFLOW, "workflow_sha": plan["workflows"][WORKFLOW]}]}

    def assertBlocked(self, call, code=409):
        before = len(self.http.writes)
        with self.assertRaises(HTTPException) as error:
            call()
        self.assertEqual(error.exception.status_code, code)
        self.assertNotIn(TOKEN, str(error.exception.detail))
        self.assertEqual(len(self.http.writes), before)

    def test_committed_snapshot_and_exact_minimal_plan(self):
        snapshot = self.github.load_goal(self.source)
        self.assertEqual(snapshot["definition"], self.http.goal)
        self.assertEqual(snapshot["dependencies"], [{"path": "examples/loop_pilot.py", "blob_sha": blob(BASELINE),
                                                    "content_sha256": hashlib.sha256(BASELINE.encode()).hexdigest()}])
        plan = self.github.prepare(snapshot)
        self.assertEqual(plan["status"], "prepared")
        self.assertEqual(plan["files"], [{"path": "examples/loop_pilot.py", "before_sha": blob(BASELINE), "content": REPAIRED}])
        self.assertEqual(plan["workflows"], {WORKFLOW: blob(self.http.files[BASE][WORKFLOW])})
        self.assertEqual(self.http.writes, [])

    def test_draft_is_stable_and_guarded_at_every_write(self):
        plan, receipt = self.drafted()
        self.assertEqual({key: receipt[key] for key in ("status", "pr_number", "pr_url", "head_sha", "base_sha", "draft", "merged")},
                         {"status": "drafted", "pr_number": 7, "pr_url": "https://github.com/clinic/answers/pull/7",
                          "head_sha": HEAD, "base_sha": BASE, "draft": True, "merged": False})
        self.assertEqual(self.guards, 3)
        self.assertEqual([stage for stage, _ in self.http.writes], ["branch", "file", "pr"])
        self.assertEqual(self.github.draft(plan, ACTION, self.guard), receipt)
        self.assertEqual(len(self.http.writes), 3)
        self.assertNotIn(TOKEN, json.dumps(receipt))

    def test_revocation_between_steps_stops_the_next_mutation(self):
        for stop in (1, 2, 3):
            with self.subTest(stop=stop):
                self.http = FakeHTTP()
                self.guards = 0
                plan = self.plan()

                def guard():
                    self.guards += 1
                    if self.guards == stop:
                        raise HTTPException(403, "The owner revoked the grant")

                with self.assertRaises(HTTPException) as error:
                    self.github.draft(plan, ACTION, guard)
                self.assertEqual(error.exception.status_code, 403)
                self.assertEqual([stage for stage, _ in self.http.writes], ["branch", "file"][:stop - 1])
                state = self.github.reconcile(plan | {"id": ACTION}, None)
                self.assertEqual(state["status"], {1: "prepared", 2: "branch_created", 3: "patched"}[stop])

    def test_each_draft_timeout_reconciles_before_retry(self):
        for stage in ("branch", "file", "pr"):
            for applied in (False, True):
                with self.subTest(stage=stage, applied=applied):
                    self.http = FakeHTTP()
                    self.http.timeout, self.http.apply_timeout = stage, applied
                    plan = self.plan()
                    result = self.github.draft(plan, ACTION, self.guard)
                    self.assertEqual(result["status"], "reconcile_required")
                    self.assertEqual(result["operation"], "draft-pr")
                    self.assertNotIn(TOKEN, json.dumps(result))
                    state = self.github.reconcile(plan, result)
                    expected = {("branch", False): "prepared", ("branch", True): "branch_created", ("file", False): "branch_created",
                                ("file", True): "patched", ("pr", False): "patched", ("pr", True): "drafted"}[(stage, applied)]
                    self.assertEqual(state["status"], expected)
                    result = self.github.draft(plan, ACTION, self.guard)
                    self.assertEqual(result["status"], "drafted")
                    self.assertEqual(self.http.files[HEAD]["examples/loop_pilot.py"], REPAIRED)
                    self.assertEqual([name for name, _ in self.http.writes].count(stage), 1 if applied else 2)

    def test_exact_merge_and_deployment_release_are_separate(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False
        merged = self.github.merge(plan, receipt, self.policy(plan), self.guard)
        self.assertEqual((merged["status"], merged["merged"], merged["merge_sha"], merged["release_status"]), ("merged", True, MERGE, "release_pending"))
        self.assertEqual(self.guards, 4)
        self.http.deployments = [{"id": 40, "sha": MERGE, "environment": "local", "payload": {"artifact_sha256": plan["artifact_sha256"]}}]
        self.http.deployment_statuses[40] = [{"state": "success", "environment": "local", "created_at": "2026-01-01T00:00:00Z"}]
        released = self.github.reconcile(plan, merged)
        self.assertEqual(released["release"], {"verified": True, "deployment_id": 40, "commit": MERGE, "released_at": "2026-01-01T00:00:00+00:00", "environment": "local", "artifact_sha256": plan["artifact_sha256"]})
        self.assertEqual(released["release_status"], "released")
        self.assertEqual([stage for stage, _ in self.http.writes], ["branch", "file", "pr", "merge"])

    def test_merge_timeout_reads_actual_result_before_retry(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                self.http = FakeHTTP()
                plan, receipt = self.drafted()
                self.http.pr["draft"] = False
                self.http.timeout, self.http.apply_timeout = "merge", applied
                unknown = self.github.merge(plan, receipt, self.policy(plan), self.guard)
                self.assertEqual((unknown["status"], unknown["operation"]), ("reconcile_required", "merge"))
                reconciled = self.github.reconcile(plan, unknown)
                self.assertEqual(reconciled["merged"], applied)
                result = self.github.merge(plan, reconciled, self.policy(plan), self.guard)
                self.assertEqual((result["merged"], result["release_status"]), (True, "release_pending"))
                self.assertEqual([stage for stage, _ in self.http.writes].count("merge"), 1 if applied else 2)

    def test_stale_or_expanded_plan_blocks_draft(self):
        plan = self.plan()
        changed = copy.deepcopy(plan)
        changed["files"][0]["content"] += "\nprint('unrelated')\n"
        self.assertBlocked(lambda: self.github.draft(changed, ACTION, self.guard))
        artifact = {key: value for key, value in changed.items() if key not in ("status", "artifact_sha256")}
        changed["artifact_sha256"] = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertBlocked(lambda: self.github.draft(changed, ACTION, self.guard))
        self.http.branches["main"] = "d" * 40
        self.assertBlocked(lambda: self.github.draft(plan, ACTION, self.guard))

    def test_edited_branch_goal_source_or_workflow_blocks_merge(self):
        mutations = [lambda: self.http.files[HEAD].update({GOAL_PATH: "{}"}),
                     lambda: self.http.files[HEAD].update({WORKFLOW: "name: forged check\n"}),
                     lambda: self.http.files[HEAD].update({"unrelated.py": "print('unsafe')\n"}),
                     lambda: self.http.files[HEAD].update({"examples/loop_pilot.py": REPAIRED.replace("return answer", "return None")}),
                     lambda: self.http.parents.update({HEAD: ["d" * 40]}),
                     lambda: self.http.pr["head"].update({"sha": BASE}),
                     lambda: self.http.pr["base"].update({"sha": "d" * 40}),
                     lambda: self.http.pr.update({"body": "wrong action marker"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.http = FakeHTTP()
                plan, receipt = self.drafted()
                self.http.pr["draft"] = False
                mutate()
                self.assertBlocked(lambda: self.github.merge(plan, receipt, self.policy(plan), self.guard))

    def test_provider_merge_gates_fail_closed(self):
        mutations = [lambda: setattr(self.http, "protected", False),
                     lambda: self.http.protection["required_status_checks"].update({"strict": False}),
                     lambda: self.http.protection["enforce_admins"].update({"enabled": False}),
                     lambda: self.http.protection["required_pull_request_reviews"].update({"dismiss_stale_reviews": False}),
                     lambda: self.http.protection["required_status_checks"]["checks"][0].update({"app_id": 999}),
                     lambda: self.http.pr.update({"mergeable": None}), lambda: self.http.pr.update({"mergeable_state": "unknown"}),
                     lambda: self.http.pr.update({"draft": True}), lambda: self.http.check.update({"head_sha": BASE}),
                     lambda: self.http.check.update({"conclusion": "failure"}), lambda: self.http.check["app"].update({"id": 999}),
                     lambda: self.http.check.update({"app": None}), lambda: self.http.check.update({"check_suite": None}),
                     lambda: self.http.check["check_suite"].update({"id": None}),
                     lambda: self.http.run.update({"path": ".github/workflows/untrusted.yml"}),
                     lambda: self.http.run.update({"head_sha": BASE}),
                     lambda: self.http.run.update({"repository": None}),
                     lambda: self.http.reviews[0].update({"commit_id": BASE}),
                     lambda: self.http.reviews[0]["user"].update({"id": 1}),
                     lambda: self.http.reviews[0].update({"state": "CHANGES_REQUESTED"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.http = FakeHTTP()
                plan, receipt = self.drafted()
                self.http.pr["draft"] = False
                mutate()
                self.assertBlocked(lambda: self.github.merge(plan, receipt, self.policy(plan), self.guard))

    def test_policy_needs_release_authority_and_exact_workflow(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False
        for field, value, code in [("operations", ["merge"], 403), ("state", "revoked", 403), ("expires_at", "2020-01-01T00:00:00Z", 403),
                                   ("paths", ["examples/loop_pilot.py", "app.py"], 403), ("repository", "other/repo", 403),
                                   ("required_checks", [{"name": "test", "app_id": 15368, "workflow_path": WORKFLOW, "workflow_sha": "d" * 40}], 409)]:
            with self.subTest(field=field):
                self.assertBlocked(lambda: self.github.merge(plan, receipt, self.policy(plan) | {field: value}, self.guard), code)

    def test_revoked_final_merge_guard_writes_nothing(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False

        def revoked():
            raise HTTPException(403, "The owner paused this loop")

        self.assertBlocked(lambda: self.github.merge(plan, receipt, self.policy(plan), revoked), 403)

    def test_wrong_release_sha_environment_artifact_or_status_stays_pending(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False
        merged = self.github.merge(plan, receipt, self.policy(plan), self.guard)
        for field, value in [("sha", BASE), ("environment", "production"), ("payload", {"artifact_sha256": "d" * 64})]:
            with self.subTest(field=field):
                self.http.deployments = [{"id": 40, "sha": MERGE, "environment": "local", "payload": {"artifact_sha256": plan["artifact_sha256"]}, field: value}]
                self.http.deployment_statuses[40] = [{"state": "success", "environment": "local"}]
                self.assertEqual(self.github.reconcile(plan, merged)["release_status"], "release_pending")
        self.http.deployments = [{"id": 40, "sha": MERGE, "environment": "local", "payload": {"artifact_sha256": plan["artifact_sha256"]}}]
        self.http.deployment_statuses[40] = [{"state": "failure", "environment": "local"}, {"state": "success", "environment": "local"}]
        self.assertEqual(self.github.reconcile(plan, merged)["release_status"], "release_pending")

    def test_unconfigured_or_outside_repository_does_not_send_credentials(self):
        self.assertBlocked(lambda: GitHub().load_goal(self.source), 503)
        self.assertBlocked(lambda: self.github.load_goal(self.source | {"repository": "evil.example/repo"}), 403)
        self.assertBlocked(lambda: self.github.load_goal(self.source | {"base": "https://evil.example"}), 422)
        self.assertEqual(self.http.requests, [])

    def test_provider_errors_never_return_private_bodies(self):
        self.http.read_failure = "/contents/"
        self.assertBlocked(lambda: self.github.load_goal(self.source), 503)

    def test_repository_transfer_blocks_actions(self):
        plan = self.plan()
        self.http.repository_id = 2002
        self.assertBlocked(lambda: self.github.draft(plan, ACTION, self.guard))

    def test_recommitted_artifact_does_not_reuse_old_head_approval(self):
        plan, receipt = self.drafted()
        different_head = "d" * 40
        self.http.files[different_head] = copy.deepcopy(self.http.files[HEAD])
        self.http.parents[different_head] = [BASE]
        self.http.branches[BRANCH] = different_head
        self.http.pr["head"]["sha"] = different_head
        self.assertBlocked(lambda: self.github.reconcile(plan, receipt))

    def test_snapshot_changes_or_stale_goal_block_prepare(self):
        snapshot = self.github.load_goal(self.source)
        tampered = copy.deepcopy(snapshot)
        tampered["definition"]["question"] = "A different question"
        self.assertBlocked(lambda: self.github.prepare(tampered))
        newer = "d" * 40
        self.http.files[newer] = self.http.files[BASE] | {GOAL_PATH: "{}"}
        self.http.parents[newer] = [BASE]
        self.http.branches["main"] = newer
        self.assertBlocked(lambda: self.github.prepare(snapshot))

    def test_changed_entry_event_on_latest_branch_blocks_prepare(self):
        snapshot = self.github.load_goal(self.source)
        newer = "d" * 40
        self.http.files[newer] = self.http.files[BASE] | {"examples/loop_pilot.py": BASELINE.replace("codex.question.started", "a.different.start")}
        self.http.parents[newer] = [BASE]
        self.http.branches["main"] = newer
        self.assertBlocked(lambda: self.github.prepare(snapshot), 422)

    def test_incomplete_or_malformed_evidence_blocks_writes(self):
        cases = [("/git/commits/" + BASE, {"sha": BASE, "tree": None}),
                 ("/branches/main", {"commit": None}),
                 ("/git/trees/" + self.http.tree(BASE), {"truncated": True, "tree": []}),
                 ("/git/trees/" + self.http.tree(BASE), {"truncated": False, "tree": [None]}),
                 ("/contents/" + GOAL_PATH, {"type": "symlink", "target": "private.env"})]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                self.http.read_overrides = {path: (200, value)}
                before = len(self.http.writes)
                with self.assertRaises(HTTPException) as error:
                    self.plan()
                self.assertIn(error.exception.status_code, [409, 422])
                self.assertNotIn(TOKEN, str(error.exception.detail))
                self.assertEqual(len(self.http.writes), before)

    def test_successful_write_without_readback_requires_reconcile(self):
        for stage in ("branch", "file", "pr"):
            with self.subTest(stage=stage):
                self.http = FakeHTTP()
                self.http.read_failure_after_write = stage
                plan = self.plan()
                unknown = self.github.draft(plan, ACTION, self.guard)
                self.assertEqual(unknown["status"], "reconcile_required")
                self.http.read_failure = None
                state = self.github.reconcile(plan, unknown)
                self.assertEqual(state["status"], {"branch": "branch_created", "file": "patched", "pr": "drafted"}[stage])

    def test_unavailable_release_read_keeps_verified_merge_pending(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False
        self.http.read_failure = "/deployments"
        merged = self.github.merge(plan, receipt, self.policy(plan), self.guard)
        self.assertEqual((merged["merged"], merged["release_status"]), (True, "release_pending"))
        self.assertNotIn(TOKEN, json.dumps(merged))

    def test_empty_required_checks_and_incomplete_check_pages_block(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False
        policy = self.policy(plan)
        self.assertBlocked(lambda: self.github.merge(plan, receipt, policy | {"required_checks": []}, self.guard))
        self.http.read_overrides["/commits/" + HEAD + "/check-runs"] = (200, {"total_count": 101, "check_runs": [self.http.check]})
        self.assertBlocked(lambda: self.github.merge(plan, receipt, policy, self.guard))

    def test_unknown_or_future_release_timestamp_stays_pending(self):
        plan, receipt = self.drafted()
        self.http.pr["draft"] = False
        merged = self.github.merge(plan, receipt, self.policy(plan), self.guard)
        self.http.deployments = [{"id": 40, "sha": MERGE, "environment": "local", "payload": {"artifact_sha256": plan["artifact_sha256"]}}]
        for stamp in (None, "invalid", "2099-01-01T00:00:00Z", "2026-01-01T00:00:00"):
            with self.subTest(stamp=stamp):
                self.http.deployment_statuses[40] = [{"state": "success", "environment": "local", "created_at": stamp}]
                self.assertEqual(self.github.reconcile(plan, merged)["release_status"], "release_pending")

    def test_healthy_tracking_does_not_offer_a_patch(self):
        self.http.files[BASE]["examples/loop_pilot.py"] = REPAIRED
        self.assertEqual(self.github.prepare(self.github.load_goal(self.source)),
                         {"status": "healthy", "reason": "The supported completion event is already tracked"})

    def test_manual_merge_commit_reconciles_without_claiming_release(self):
        plan, receipt = self.drafted()
        self.http.files[MERGE] = copy.deepcopy(self.http.files[HEAD])
        self.http.parents[MERGE] = [BASE, HEAD]
        self.http.pr.update(merged=True, state="closed", merge_commit_sha=MERGE)
        writes = len(self.http.writes)
        merged = self.github.reconcile(plan, receipt)
        self.assertEqual((merged["merged"], merged["merge_sha"], merged["release_status"]), (True, MERGE, "release_pending"))
        self.assertEqual(len(self.http.writes), writes)

    def test_manual_merge_rejects_changed_tree_or_unapproved_parents(self):
        plan, receipt = self.drafted()
        for mutation in ["tree", "parents", "missing_tree", "invalid_tree"]:
            with self.subTest(mutation=mutation):
                self.http.read_overrides.pop("/git/commits/" + MERGE, None)
                self.http.files[MERGE] = copy.deepcopy(self.http.files[HEAD])
                self.http.parents[MERGE] = [BASE, HEAD]
                self.http.pr.update(merged=True, state="closed", merge_commit_sha=MERGE)
                if mutation == "tree":
                    self.http.files[MERGE]["unrelated.py"] = "unapproved edit"
                elif mutation == "parents":
                    self.http.parents[MERGE] = ["f" * 40, HEAD]
                else:
                    commit = {"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}]}
                    if mutation == "invalid_tree":
                        commit["tree"] = None
                    self.http.read_overrides["/git/commits/" + MERGE] = (200, commit)
                writes = len(self.http.writes)
                self.assertBlocked(lambda: self.github.reconcile(plan, receipt))
                self.assertEqual(len(self.http.writes), writes)

    def test_unrelated_goal_mapping_events_or_filters_do_not_offer_repairs(self):
        mutations = [lambda goal: goal["mapping"].update({"environment": "production"}),
                     lambda goal: goal["mapping"].update({"source": "external"}),
                     lambda goal: goal["measurement"]["entry"].update({"event": "another.start"}),
                     lambda goal: goal["measurement"]["completion"].update({"event": "another.answer"}),
                     lambda goal: goal["measurement"]["completion"].update({"filters": [{"property": "environment", "value": "production"}]}),
                     lambda goal: goal["measurement"]["completion"].update({"filters": [{"property": "answer", "value": "private"}]}),
                     lambda goal: goal["measurement"].update({"followup_seconds": 3600}),
                     lambda goal: goal["measurement"].update({"entry_rule": "every_retry"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                goal = copy.deepcopy(self.http.goal)
                mutate(goal)
                self.http.files[BASE][GOAL_PATH] = json.dumps(goal)
                source = self.source | {"content_sha256": hashlib.sha256(json.dumps(goal).encode()).hexdigest()}
                self.assertBlocked(lambda: self.github.load_goal(source), 422)

    def test_local_environment_filters_are_supported(self):
        self.http.goal["measurement"]["entry"]["filters"] = [{"property": "environment", "value": "local"}]
        self.http.goal["measurement"]["completion"]["filters"] = [{"property": "environment", "op": "in", "value": ["local", "staging"]}]
        goal = json.dumps(self.http.goal)
        self.http.files[BASE][GOAL_PATH] = goal
        source = self.source | {"content_sha256": hashlib.sha256(goal.encode()).hexdigest()}
        plan = self.github.prepare(self.github.load_goal(source))
        self.assertEqual(plan["files"][0]["content"], REPAIRED)


if __name__ == "__main__":
    unittest.main()
