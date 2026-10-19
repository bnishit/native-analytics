import base64
import hashlib
import http.client
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from fastapi import HTTPException

from loop_recipe import PATH, entry_event, repair


_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,150}")
_ACTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_PLAN_FIELDS = ("repository", "repository_id", "base", "base_sha", "goal_path", "goal_sha256", "files", "workflows")
_LIMIT = 1024 * 1024


def _fail(detail, code=409):
    raise HTTPException(code, detail)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _blob(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def _event_rule(rule, event):
    if not isinstance(rule, dict) or set(rule) - {"event", "filters"} or rule.get("event") != event:
        return False
    filters = rule.get("filters", [])
    if not isinstance(filters, list):
        return False
    for condition in filters:
        if (not isinstance(condition, dict) or set(condition) - {"property", "op", "value"}
                or condition.get("property") != "environment"):
            return False
        if condition.get("op", "eq") == "eq" and condition.get("value") == "local":
            continue
        if condition.get("op") == "in" and isinstance(condition.get("value"), list) and "local" in condition["value"]:
            continue
        return False
    return True


class _UncertainWrite(Exception):
    pass


class GitHub:
    def __init__(self, repository="", token=""):
        self.repository = repository
        self._token = token

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("AFTERSPAN_GITHUB_REPOSITORY", ""), os.environ.get("AFTERSPAN_GITHUB_TOKEN", ""))

    def _configured(self, repository):
        if not self._token or not _REPO.fullmatch(self.repository):
            _fail("GitHub is not configured", 503)
        if repository != self.repository:
            _fail("The repository is outside the operator allowlist", 403)

    def _request(self, method, path, body=None, missing=False, guard=None):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "afterspan-loop", "Content-Type": "application/json"}
        connection = http.client.HTTPSConnection("api.github.com", timeout=15)
        try:
            if method != "GET":
                guard()
            connection.request(method, "/repos/" + self.repository + path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(_LIMIT + 1)
            status = response.status
        except (OSError, http.client.HTTPException):
            if method != "GET":
                raise _UncertainWrite() from None
            _fail("GitHub could not be read", 503)
        finally:
            connection.close()
        if status == 404 and missing:
            return None
        if method != "GET" and status >= 500:
            raise _UncertainWrite()
        if not 200 <= status < 300:
            _fail("GitHub denied the operation" if method != "GET" else "GitHub evidence is unavailable", 503 if status >= 500 else 409)
        if len(raw) > _LIMIT:
            if method != "GET":
                raise _UncertainWrite()
            _fail("GitHub evidence exceeded the read limit", 503)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            if method != "GET":
                raise _UncertainWrite() from None
            _fail("GitHub returned invalid evidence", 503)

    def _get(self, path, missing=False):
        result = self._request("GET", path, missing=missing)
        if result is None and missing:
            return None
        if not isinstance(result, dict):
            _fail("GitHub returned invalid evidence", 503)
        return result

    def _list(self, path):
        value = self._request("GET", path + ("&" if "?" in path else "?") + "per_page=100")
        if not isinstance(value, list) or len(value) >= 100 or any(not isinstance(row, dict) for row in value):
            _fail("GitHub evidence is incomplete", 503)
        return value

    def _source(self, source):
        if not isinstance(source, dict):
            _fail("A committed source snapshot is required", 422)
        self._configured(source.get("repository"))
        base = source.get("base", "")
        if (not isinstance(base, str) or not _REF.fullmatch(base) or ".." in base or "//" in base
                or base.endswith(("/", ".", ".lock")) or "@{" in base):
            _fail("The base branch is invalid", 422)
        if (not isinstance(source.get("commit"), str) or not _SHA.fullmatch(source["commit"])
                or not isinstance(source.get("content_sha256"), str) or not _DIGEST.fullmatch(source["content_sha256"])):
            _fail("The committed source hashes are invalid", 422)
        if source.get("path") != ".afterspan/goals/first-answer.json":
            _fail("Only the first-answer goal is supported", 422)

    def _content(self, path, commit):
        value = self._get("/contents/" + quote(path, safe="/") + "?" + urlencode({"ref": commit}))
        if (value.get("type") != "file" or value.get("encoding") != "base64"
                or not isinstance(value.get("content"), str) or type(value.get("size")) is not int or not 0 <= value["size"] <= 131072):
            _fail("The committed file is unsupported", 422)
        try:
            content = base64.b64decode(value["content"].replace("\n", ""), validate=True)
            text = content.decode("utf-8")
        except (ValueError, UnicodeError):
            _fail("The committed file is not bounded UTF-8 text", 422)
        if len(content) != value["size"] or value.get("sha") != _blob(content):
            _fail("The committed file hash does not match")
        return text, value["sha"]

    def _commit(self, commit):
        value = self._get("/git/commits/" + quote(commit, safe=""))
        if (value.get("sha") != commit or not isinstance(value.get("tree"), dict)
                or not _SHA.fullmatch(str(value["tree"].get("sha", "")))):
            _fail("The commit evidence is invalid")
        return value

    def _workflows(self, commit):
        tree = self._get("/git/trees/" + self._commit(commit)["tree"]["sha"] + "?recursive=1")
        if (tree.get("truncated") is not False or not isinstance(tree.get("tree"), list)
                or any(not isinstance(node, dict) or not isinstance(node.get("path"), str) for node in tree["tree"])):
            _fail("The repository tree is incomplete")
        workflows = {}
        for node in tree["tree"]:
            path = node["path"]
            if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
                if node.get("type") != "blob" or node.get("mode") != "100644" or not _SHA.fullmatch(str(node.get("sha", ""))):
                    _fail("A workflow is unsupported")
                workflows[path] = node["sha"]
        return workflows

    def _head(self, base):
        value = self._get("/branches/" + quote(base, safe=""))
        if not isinstance(value.get("commit"), dict) or not _SHA.fullmatch(str(value["commit"].get("sha", ""))):
            _fail("The base branch could not be verified")
        return value

    def load_goal(self, source):
        self._source(source)
        repository = self._get("")
        repository_id = repository.get("id")
        if (not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0
                or repository.get("full_name") != self.repository or repository.get("archived") is not False):
            _fail("The active repository identity could not be verified")
        if source.get("repository_id", repository_id) != repository_id:
            _fail("The configured repository identity changed")
        content, _ = self._content(source["path"], source["commit"])
        if hashlib.sha256(content.encode()).hexdigest() != source["content_sha256"]:
            _fail("The goal no longer matches the committed source")
        try:
            definition = json.loads(content)
        except ValueError:
            _fail("The goal is not valid JSON", 422)
        if (not isinstance(definition, dict) or definition.get("schema") != "afterspan-goal-v1"
                or definition.get("id") != "first-answer" or definition.get("tracking") != {
                    "recipe": "python-answer-v1", "path": PATH, "dependencies": [PATH]}):
            _fail("The goal tracking recipe is unsupported", 422)
        dependency, blob_sha = self._content(PATH, source["commit"])
        measurement = definition.get("measurement")
        if (definition.get("mapping") != {"source": "native", "environment": "local", "observation_id_property": "observation_id"}
                or not isinstance(measurement, dict) or set(measurement) != {"unit", "entry", "completion", "followup_seconds", "entry_rule", "rubric"}
                or measurement.get("unit") != "workspace" or measurement.get("followup_seconds") != 86400
                or measurement.get("entry_rule") != "first_in_entry_window" or measurement.get("rubric") != "aggregate-answer-v1"
                or not _event_rule(measurement.get("completion"), "codex.answer.ready")):
            _fail("The goal does not match the supported local tracking recipe", 422)
        try:
            started = entry_event(dependency)
        except ValueError:
            _fail("The source has no supported local entry event", 422)
        if not _event_rule(measurement.get("entry"), started):
            _fail("The goal entry does not match the source event", 422)
        return {key: source[key] for key in ("repository", "base", "commit", "path", "content_sha256")} | {
            "repository_id": repository_id, "definition": definition, "dependencies": [{"path": PATH, "blob_sha": blob_sha,
            "content_sha256": hashlib.sha256(dependency.encode()).hexdigest()}], "workflows": self._workflows(source["commit"])}

    def prepare(self, bundle):
        snapshot = self.load_goal(bundle)
        if snapshot != bundle:
            _fail("The source snapshot has changed")
        current = self._head(bundle["base"])["commit"]["sha"]
        current_snapshot = self.load_goal(bundle | {"commit": current})
        source, before_sha = self._content(PATH, current)
        try:
            content = repair(source)
        except ValueError:
            _fail("The current tracking source is unsupported", 422)
        if content is None:
            return {"status": "healthy", "reason": "The supported completion event is already tracked"}
        plan = {"repository": self.repository, "repository_id": snapshot["repository_id"], "base": bundle["base"], "base_sha": current,
                "goal_path": bundle["path"], "goal_sha256": bundle["content_sha256"],
                "files": [{"path": PATH, "before_sha": before_sha, "content": content}], "workflows": current_snapshot["workflows"]}
        return plan | {"artifact_sha256": _digest(plan), "status": "prepared"}

    def _plan(self, plan, current=False):
        if not isinstance(plan, dict) or any(key not in plan for key in (*_PLAN_FIELDS, "artifact_sha256")):
            _fail("A complete immutable plan is required", 422)
        self._source({"repository": plan["repository"], "base": plan["base"], "commit": plan["base_sha"],
                      "path": plan["goal_path"], "content_sha256": plan["goal_sha256"]})
        artifact = {key: plan[key] for key in _PLAN_FIELDS}
        if _digest(artifact) != plan["artifact_sha256"]:
            _fail("The plan artifact changed")
        goal = self.load_goal({"repository": self.repository, "base": plan["base"], "commit": plan["base_sha"],
                               "repository_id": plan["repository_id"], "path": plan["goal_path"], "content_sha256": plan["goal_sha256"]})
        if goal["workflows"] != plan["workflows"]:
            _fail("The plan workflow snapshot changed")
        source, before_sha = self._content(PATH, plan["base_sha"])
        try:
            content = repair(source)
        except ValueError:
            _fail("The planned source is unsupported", 422)
        if content is None or plan["files"] != [{"path": PATH, "before_sha": before_sha, "content": content}]:
            _fail("The plan is not the supported tracking repair")
        if current and self._head(plan["base"])["commit"]["sha"] != plan["base_sha"]:
            _fail("The base branch moved. Prepare a new plan")

    def _action(self, action_id):
        if not isinstance(action_id, str) or not _ACTION.fullmatch(action_id):
            _fail("A stable action ID is required", 422)
        return "afterspan/loop/" + action_id

    def _marker(self, plan, action_id):
        return "<!-- afterspan-loop " + action_id + " " + plan["artifact_sha256"] + " -->"

    def _verify_head(self, plan, head):
        if head == plan["base_sha"]:
            return False
        commit = self._commit(head)
        if [parent.get("sha") for parent in commit.get("parents", [])] != [plan["base_sha"]]:
            _fail("The tracking branch has an unexpected parent")
        comparison = self._get("/compare/" + plan["base_sha"] + "..." + head)
        files = comparison.get("files", [])
        expected_sha = _blob(plan["files"][0]["content"].encode())
        if (comparison.get("status") != "ahead" or comparison.get("ahead_by") != 1
                or comparison.get("behind_by") != 0 or comparison.get("total_commits") != 1
                or not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
                or files[0].get("filename") != PATH or files[0].get("status") != "modified"
                or files[0].get("sha") != expected_sha or files[0].get("additions") != 1 or files[0].get("deletions") != 0):
            _fail("The provider diff differs from the approved artifact")
        content, _ = self._content(PATH, head)
        goal, _ = self._content(plan["goal_path"], head)
        if (content != plan["files"][0]["content"] or hashlib.sha256(goal.encode()).hexdigest() != plan["goal_sha256"]
                or self._workflows(head) != plan["workflows"]):
            _fail("The tracking branch files or workflows changed")
        return True

    def _state(self, plan, action_id, receipt=None):
        branch = self._action(action_id)
        marker = self._marker(plan, action_id)
        pulls = self._list("/pulls?" + urlencode({"state": "all", "head": self.repository.split("/")[0] + ":" + branch}))
        if len(pulls) > 1:
            _fail("More than one PR matches this action")
        if pulls and (type(pulls[0].get("number")) is not int or pulls[0]["number"] <= 0):
            _fail("The PR number is invalid")
        if receipt and receipt.get("pr_number") and (not pulls or pulls[0]["number"] != receipt["pr_number"]):
            _fail("The recorded PR could not be reconciled")
        result = {"status": "prepared", "action_id": action_id, "branch": branch, "repository": self.repository,
                  "repository_id": plan["repository_id"], "base_sha": plan["base_sha"], "artifact_sha256": plan["artifact_sha256"], "merged": False,
                  "files": [], "workflows": plan["workflows"]}
        pr = self._get("/pulls/" + str(pulls[0]["number"])) if pulls else None
        if pr:
            if (not isinstance(pr.get("body"), str) or any(not isinstance(pr.get(side), dict)
                    or not isinstance(pr[side].get("repo"), dict) for side in ("base", "head"))
                    or not isinstance(pr.get("user"), dict) or type(pr["user"].get("id")) is not int or pr["user"]["id"] <= 0):
                _fail("The PR evidence is incomplete")
            if (marker not in pr["body"] or pr.get("base", {}).get("ref") != plan["base"]
                    or pr.get("base", {}).get("sha") != plan["base_sha"] or pr.get("head", {}).get("ref") != branch
                    or pr.get("number") != pulls[0]["number"]
                    or any(pr.get(side, {}).get("repo", {}).get("full_name") != self.repository
                           or pr.get(side, {}).get("repo", {}).get("id") != plan["repository_id"] for side in ("base", "head"))):
                _fail("The PR does not match the action, repository, or base")
            head = pr.get("head", {}).get("sha", "")
            if not isinstance(head, str) or not _SHA.fullmatch(head) or not self._verify_head(plan, head):
                _fail("The PR has no verified tracking artifact")
            if receipt and receipt.get("head_sha") not in (None, plan["base_sha"], head):
                _fail("The PR head changed after the receipt")
            result.update({"status": "merged" if pr.get("merged") is True else "drafted", "head_sha": head,
                           "pr_number": pr["number"], "pr_url": "https://github.com/" + self.repository + "/pull/" + str(pr["number"]),
                           "draft": pr.get("draft"), "merged": pr.get("merged") is True, "merge_sha": pr.get("merge_commit_sha") if pr.get("merged") is True else None,
                           "files": [{"path": PATH, "sha": _blob(plan["files"][0]["content"].encode())}]})
            if pr.get("merged") is True:
                if not _SHA.fullmatch(str(result["merge_sha"])):
                    _fail("The merge commit could not be verified")
                merged = self._commit(result["merge_sha"])
                if [parent.get("sha") for parent in merged.get("parents", [])] == [plan["base_sha"], head]:
                    tree, expected = merged.get("tree"), self._commit(head).get("tree")
                    if (not isinstance(tree, dict) or not isinstance(expected, dict)
                            or not isinstance(tree.get("sha"), str) or not _SHA.fullmatch(tree["sha"])
                            or tree["sha"] != expected.get("sha")):
                        _fail("The merge commit changed the verified tracking tree")
                else:
                    self._verify_head(plan, result["merge_sha"])
                return result, pr
            if pr.get("state") != "open":
                _fail("The tracking PR was closed without merging")
        ref = self._get("/git/ref/heads/" + quote(branch, safe="/"), missing=True)
        if ref is None:
            if pr:
                _fail("The PR branch is missing")
            return result, None
        if not isinstance(ref.get("object"), dict):
            _fail("The remote branch evidence is incomplete")
        head = ref["object"].get("sha", "")
        if not isinstance(head, str) or not _SHA.fullmatch(head) or (pr and head != pr["head"]["sha"]):
            _fail("The remote branch head is inconsistent")
        patched = self._verify_head(plan, head)
        result.update({"head_sha": head, "status": "drafted" if pr else "patched" if patched else "branch_created"})
        if patched:
            result["files"] = [{"path": PATH, "sha": _blob(plan["files"][0]["content"].encode())}]
        return result, pr

    def draft(self, plan, action_id, guard):
        self._plan(plan, current=True)
        receipt, pr = self._state(plan, action_id)
        if pr:
            return receipt
        wrote = False
        try:
            if receipt["status"] == "prepared":
                self._request("POST", "/git/refs", {"ref": "refs/heads/" + receipt["branch"], "sha": plan["base_sha"]}, guard=guard)
                wrote = True
                receipt, _ = self._state(plan, action_id)
            if receipt["status"] == "branch_created":
                self._plan(plan, current=True)
                self._request("PUT", "/contents/" + PATH, {"message": "Track the first answer completion", "branch": receipt["branch"],
                              "sha": plan["files"][0]["before_sha"], "content": base64.b64encode(plan["files"][0]["content"].encode()).decode()}, guard=guard)
                wrote = True
                receipt, _ = self._state(plan, action_id)
            self._plan(plan, current=True)
            self._request("POST", "/pulls", {"title": "Track the first answer completion", "head": receipt["branch"], "base": plan["base"], "draft": True,
                          "body": "Adds the supported local completion event before the existing answer return.\n\n" + self._marker(plan, action_id)}, guard=guard)
            wrote = True
            return self._state(plan, action_id)[0]
        except _UncertainWrite:
            return receipt | {"status": "reconcile_required", "operation": "draft-pr", "reason": "The write result is unknown. Reconcile GitHub before retrying"}
        except HTTPException as exc:
            if not wrote or exc.status_code != 503:
                raise
            return receipt | {"status": "reconcile_required", "operation": "draft-pr", "reason": "The write needs a fresh GitHub read before retrying"}

    def reconcile(self, plan, receipt):
        self._plan(plan)
        action_id = receipt.get("action_id") if receipt else plan.get("action_id", plan.get("id"))
        state, _ = self._state(plan, action_id, receipt)
        if state["merged"]:
            environment = receipt.get("release_environment") if receipt else None
            state.update(self._release(state, environment))
        return state

    def _release(self, receipt, environment):
        pending = {"release_status": "release_pending", "release_environment": environment}
        if not environment:
            return pending
        try:
            deployments = self._list("/deployments?" + urlencode({"sha": receipt["merge_sha"], "environment": environment}))
        except HTTPException:
            return pending
        for deployment in deployments:
            payload = deployment.get("payload")
            if (deployment.get("sha") != receipt["merge_sha"] or deployment.get("environment") != environment
                    or not isinstance(payload, dict) or payload.get("artifact_sha256") != receipt["artifact_sha256"]):
                continue
            if not isinstance(deployment.get("id"), int) or isinstance(deployment["id"], bool):
                continue
            try:
                statuses = self._list("/deployments/" + str(deployment["id"]) + "/statuses")
            except HTTPException:
                return pending
            if statuses and statuses[0].get("state") == "success" and statuses[0].get("environment") == environment:
                try:
                    released_at = datetime.fromisoformat(statuses[0]["created_at"].replace("Z", "+00:00"))
                    if released_at.tzinfo is None or released_at > datetime.now(timezone.utc):
                        continue
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
                return {"release_status": "released", "release_environment": environment,
                        "release": {"verified": True, "deployment_id": deployment["id"], "commit": receipt["merge_sha"], "released_at": released_at.isoformat(),
                                    "environment": environment, "artifact_sha256": receipt["artifact_sha256"]}}
        return pending

    def _merge_gate(self, plan, receipt, pr, policy):
        if (policy.get("state") != "active" or policy.get("repository") != self.repository or policy.get("base") != plan["base"]
                or not {"merge", "release-on-merge"}.issubset(set(policy.get("operations", [])))
                or policy.get("paths") != [PATH] or not isinstance(policy.get("environment"), str) or not policy["environment"]):
            _fail("Current merge and release authority is required", 403)
        try:
            expires = datetime.fromisoformat(policy["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                _fail("The owner policy expired", 403)
        except (KeyError, ValueError, TypeError, AttributeError):
            _fail("The owner policy expiry is invalid", 403)
        branch = self._head(plan["base"])
        if branch["commit"]["sha"] != plan["base_sha"] or branch.get("protected") is not True:
            _fail("Automatic merge requires the unchanged protected base")
        protection = self._get("/branches/" + quote(plan["base"], safe="") + "/protection")
        checks = protection.get("required_status_checks") or {}
        reviews = protection.get("required_pull_request_reviews") or {}
        if (not isinstance(checks, dict) or not isinstance(reviews, dict) or checks.get("strict") is not True
                or not isinstance(protection.get("enforce_admins"), dict) or protection["enforce_admins"].get("enabled") is not True
                or reviews.get("dismiss_stale_reviews") is not True or type(reviews.get("required_approving_review_count")) is not int
                or reviews["required_approving_review_count"] < 1):
            _fail("Strict checks and current protected reviews are required")
        if pr.get("draft") is not False or pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean":
            _fail("The PR is not confirmed ready to merge")
        rules = policy.get("required_checks")
        required = checks.get("checks")
        if (not isinstance(rules, list) or not rules or any(not isinstance(rule, dict) for rule in rules)
                or not isinstance(required, list) or not required or any(not isinstance(rule, dict) for rule in required)
                or {(r.get("context"), r.get("app_id")) for r in required} != {(r.get("name"), r.get("app_id")) for r in rules}
                or set(checks.get("contexts", [])) != {r.get("name") for r in rules}):
            _fail("Required checks differ from the owner policy")
        head = receipt["head_sha"]
        runs = self._get("/commits/" + head + "/check-runs?per_page=100&filter=latest")
        actions = self._get("/actions/runs?" + urlencode({"head_sha": head, "per_page": 100}))
        if (type(runs.get("total_count")) is not int or not 0 <= runs["total_count"] < 100 or not isinstance(runs.get("check_runs"), list)
                or any(not isinstance(run, dict) for run in runs["check_runs"]) or runs["total_count"] != len(runs["check_runs"])
                or type(actions.get("total_count")) is not int or not 0 <= actions["total_count"] < 100 or not isinstance(actions.get("workflow_runs"), list)
                or any(not isinstance(run, dict) for run in actions["workflow_runs"]) or actions["total_count"] != len(actions["workflow_runs"])):
            _fail("Check evidence is incomplete")
        for rule in rules:
            workflow = rule.get("workflow_path")
            if (not isinstance(rule.get("app_id"), int) or isinstance(rule["app_id"], bool) or rule["app_id"] <= 0
                    or workflow not in plan["workflows"] or plan["workflows"][workflow] != rule.get("workflow_sha")):
                _fail("The independent check workflow is not pinned")
            matching = [run for run in runs["check_runs"] if run.get("name") == rule.get("name")]
            if len(matching) != 1:
                _fail("A required check is missing or ambiguous")
            check = matching[0]
            if (check.get("head_sha") != head or check.get("status") != "completed" or check.get("conclusion") != "success"
                    or not isinstance(check.get("app"), dict) or check["app"].get("id") != rule["app_id"]
                    or not isinstance(check.get("check_suite"), dict) or type(check["check_suite"].get("id")) is not int
                    or check["check_suite"]["id"] <= 0):
                _fail("A required independent check did not pass on this exact commit")
            evidence = [run for run in actions["workflow_runs"] if run.get("check_suite_id") == check.get("check_suite", {}).get("id")
                        and run.get("head_sha") == head and run.get("path") == workflow
                        and run.get("status") == "completed" and run.get("conclusion") == "success"
                        and isinstance(run.get("repository"), dict) and run["repository"].get("full_name") == self.repository]
            if len(evidence) != 1:
                _fail("The check is not bound to the pinned workflow")
        review_rows = self._list("/pulls/" + str(pr["number"]) + "/reviews")
        latest = {}
        for review in review_rows:
            user = review.get("user", {})
            if isinstance(user, dict) and type(user.get("id")) is int and review.get("state") != "PENDING":
                latest[user["id"]] = review
        if any(review.get("state") == "CHANGES_REQUESTED" for review in latest.values()):
            _fail("A current review requests changes")
        approvals = [review for user, review in latest.items() if user != pr.get("user", {}).get("id")
                     and review.get("user", {}).get("type") == "User" and review.get("state") == "APPROVED" and review.get("commit_id") == head]
        if len(approvals) < reviews["required_approving_review_count"]:
            _fail("Independent approval of this exact commit is required")

    def merge(self, plan, receipt, policy, guard):
        self._plan(plan)
        action_id = receipt.get("action_id")
        state, pr = self._state(plan, action_id, receipt)
        if state["merged"]:
            return state | self._release(state, policy.get("environment"))
        if not pr:
            _fail("A verified PR is required")
        self._merge_gate(plan, state, pr, policy)
        self._plan(plan, current=True)
        refreshed, current_pr = self._state(plan, action_id, state)
        self._merge_gate(plan, refreshed, current_pr, policy)
        try:
            self._request("PUT", "/pulls/" + str(state["pr_number"]) + "/merge", {"sha": state["head_sha"], "merge_method": "squash"}, guard=guard)
        except _UncertainWrite:
            return state | {"status": "reconcile_required", "operation": "merge", "release_environment": policy["environment"],
                            "reason": "The merge result is unknown. Reconcile GitHub before retrying"}
        try:
            result, _ = self._state(plan, action_id, state)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            return state | {"status": "reconcile_required", "operation": "merge", "release_environment": policy["environment"],
                            "reason": "The merge needs a fresh GitHub read before retrying"}
        if not result["merged"]:
            return result | {"status": "reconcile_required", "operation": "merge", "release_environment": policy["environment"]}
        return result | self._release(result, policy["environment"])
