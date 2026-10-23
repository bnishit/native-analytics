import base64
import difflib
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
_REVIEWED_FIELDS = ("kind", *_PLAN_FIELDS, "scan", "definition")
_SCAN_FIELDS = ("repository", "repository_id", "commit", "base", "tree_sha", "scanner_version", "files", "findings", "coverage", "workflows")
_SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt")
_EXCLUDED = {"node_modules", "vendor", "dist", "build", "coverage", "generated", "__pycache__", "venv", "tests", "__tests__"}
_SECRET = re.compile(r"(?:^|[._-])(?:env|secrets?|credentials?|tokens?|private|id_rsa|id_ed25519)(?:$|[._-])", re.I)
_SCAN_BYTES = 2 * 1024 * 1024
_FILE_BYTES = 128 * 1024
_REVIEW_BYTES = 128 * 1024
_REVIEW_LINES = 4000
_LIMIT = 1024 * 1024


def _fail(detail, code=409):
    raise HTTPException(code, detail)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _blob(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def _relative(path):
    return (isinstance(path, str) and 0 < len(path) <= 240
            and re.fullmatch(r"[A-Za-z0-9_@()\[\]./-]+", path) is not None
            and all(part not in ("", ".", "..") and len(part) <= 128 for part in path.split("/")))


def _source_path(path):
    if not _relative(path):
        return False
    parts = path.lower().split("/")
    name = parts[-1]
    return (not any(part.startswith(".") or part in _EXCLUDED or _SECRET.search(part) for part in parts)
            and not any(tag in name for tag in (".generated.", ".min.", ".test.", ".spec."))
            and not name.startswith(("test_", "generated_"))
            and (name in _MANIFESTS or name.endswith(_SOURCE_EXTENSIONS)))


def _text_bytes(content):
    if not isinstance(content, str):
        _fail("Artifact files require UTF-8 text", 422)
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        _fail("Artifact files require UTF-8 text", 422)
    if any(ord(char) < 32 and char not in "\n\r\t" for char in content) or "\x7f" in content:
        _fail("Binary files are unsupported", 422)
    return raw


def _goal_bytes(definition):
    """Goal files use sorted, compact UTF-8 JSON followed by one newline."""
    try:
        content = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError, UnicodeError):
        _fail("The approved goal is not JSON", 422)
    _text_bytes(content)
    return content


def _line_changes(before, after):
    added = deleted = 0
    for kind, start, end, next_start, next_end in difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False).get_opcodes():
        if kind in ("insert", "replace"):
            added += next_end - next_start
        if kind in ("delete", "replace"):
            deleted += end - start
    return added, deleted


def _signals(sources):
    events, packages, frameworks, dynamic = [], set(), set(), []
    token = re.compile(r"(?P<comment>//[^\n]*|/\*[\s\S]*?\*/|\#[^\n]*)|(?P<string>'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`)|(?P<word>[A-Za-z_$][\w$]*)|(?P<other>\S)")
    for path, source in sources.items():
        if path.rsplit("/", 1)[-1] == "package.json":
            try:
                manifest = json.loads(source)
                for group in ("dependencies", "devDependencies", "peerDependencies"):
                    dependencies = manifest.get(group, {}) if isinstance(manifest, dict) else {}
                    if isinstance(dependencies, dict):
                        packages.update(name for name in dependencies if isinstance(name, str))
            except ValueError:
                dynamic.append({"path": path, "reason": "invalid_package_manifest"})
        rows = [match for match in token.finditer(source) if match.lastgroup != "comment"]
        for index, row in enumerate(rows):
            value = row.group()
            if row.lastgroup == "string":
                literal = value[1:-1]
                if literal in ("next", "react", "fastapi", "posthog-js", "posthog-node", "posthog", "@afterspan/analytics", "afterspan"):
                    packages.add(literal)
            if value not in ("trackEvent", "track", "capture") or index + 1 >= len(rows) or rows[index + 1].group() != "(":
                continue
            owner = rows[index - 2].group() if index >= 2 and rows[index - 1].group() == "." else ""
            if value == "track" and owner not in ("collector", "client", "analytics", "native", "nativeAnalytics"):
                continue
            sink = "posthog" if value == "capture" else "native"
            argument = rows[index + 2] if index + 2 < len(rows) else None
            if (argument is not None and argument.lastgroup == "string" and index + 3 < len(rows) and rows[index + 3].group() in (",", ")")
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,119}", argument.group()[1:-1])):
                events.append({"name": argument.group()[1:-1], "path": path, "line": source.count("\n", 0, row.start()) + 1, "sink": sink})
            else:
                dynamic.append({"path": path, "line": source.count("\n", 0, row.start()) + 1, "reason": "dynamic_event_name"})
    for package, framework in (("next", "next"), ("react", "react"), ("fastapi", "fastapi")):
        if package in packages:
            frameworks.add(framework)
    sinks = sorted({event["sink"] for event in events} | ({"posthog"} if packages & {"posthog-js", "posthog-node", "posthog"} else set())
                   | ({"native"} if packages & {"@afterspan/analytics", "afterspan"} else set()))
    return {"frameworks": sorted(frameworks), "packages": sorted(packages)[:128], "sinks": sinks,
            "events": events[:512], "unsupported": dynamic[:128]}, len(events) > 512 or len(dynamic) > 128 or len(packages) > 128


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
    def __init__(self, repository="", token="", read_guard=None):
        self.repository = repository
        self._token = token
        self.read_guard = read_guard

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("AFTERSPAN_GITHUB_REPOSITORY", ""), os.environ.get("AFTERSPAN_GITHUB_TOKEN", ""))

    def _configured(self, repository):
        if not self._token or not _REPO.fullmatch(self.repository):
            _fail("GitHub is not configured", 503)
        if repository != self.repository:
            _fail("The repository is outside the operator allowlist", 403)

    def _request(self, method, path, body=None, missing=False, guard=None):
        if self.read_guard:
            self.read_guard()
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

    def _pin(self, repository, base, commit):
        self._configured(repository)
        if (not isinstance(base, str) or not _REF.fullmatch(base) or ".." in base or "//" in base
                or base.endswith(("/", ".", ".lock")) or "@{" in base):
            _fail("The base branch is invalid", 422)
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            _fail("The committed source hashes are invalid", 422)

    def _source(self, source):
        if not isinstance(source, dict):
            _fail("A committed source snapshot is required", 422)
        self._pin(source.get("repository"), source.get("base", ""), source.get("commit"))
        if not isinstance(source.get("content_sha256"), str) or not _DIGEST.fullmatch(source["content_sha256"]):
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

    def _identity(self):
        self._configured(self.repository)
        value = self._get("")
        if (type(value.get("id")) is not int or value["id"] <= 0
                or value.get("full_name") != self.repository or value.get("archived") is not False):
            _fail("The active repository identity could not be verified")
        return value["id"]

    def _tree(self, commit):
        tree_sha = self._commit(commit)["tree"]["sha"]
        if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
            _fail("The repository tree hash is invalid")
        value = self._get("/git/trees/" + tree_sha + "?recursive=1")
        nodes = value.get("tree")
        if (value.get("truncated") is not False or value.get("sha", tree_sha) != tree_sha
                or not isinstance(nodes, list) or len(nodes) > 10000):
            _fail("The repository tree is incomplete")
        result = {}
        for node in nodes:
            if (not isinstance(node, dict) or not isinstance(node.get("path"), str) or not 0 < len(node["path"]) <= 1024
                    or any(part in ("", ".", "..") for part in node["path"].split("/"))
                    or any(ord(char) < 32 for char in node["path"]) or node["path"] in result
                    or not isinstance(node.get("sha"), str) or not _SHA.fullmatch(node["sha"])
                    or (node.get("type"), node.get("mode")) not in {
                        ("tree", "040000"), ("blob", "100644"), ("blob", "100755"), ("blob", "120000"), ("commit", "160000")}):
                _fail("The repository tree contains unsupported entries")
            result[node["path"]] = node
        return tree_sha, result

    def scan(self, commit, base):
        self._pin(self.repository, base, commit)
        repository_id = self._identity()
        tree_sha, tree = self._tree(commit)
        paths = [path for path in tree if _source_path(path)]
        paths.sort(key=lambda path: (path.rsplit("/", 1)[-1] not in _MANIFESTS,
                                    not any(word in path.lower() for word in ("analytics", "telemetry", "tracking", "native")), path))
        sources, files, reasons, workflows = {}, [], set(), {}
        total = reads = 0
        for path, node in tree.items():
            if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
                if node["type"] != "blob" or node["mode"] != "100644":
                    _fail("A workflow is unsupported")
                workflows[path] = node["sha"]
        for path in paths:
            node = tree[path]
            if node["type"] != "blob" or node["mode"] not in ("100644", "100755"):
                reasons.add("unsupported_file_mode")
                continue
            if reads >= 64:
                reasons.add("file_count_limit")
                break
            size = node.get("size")
            if type(size) is not int or not 0 <= size <= _FILE_BYTES:
                reasons.add("file_size_limit")
                continue
            if total + _FILE_BYTES > _SCAN_BYTES:
                reasons.add("total_byte_limit")
                continue
            total += size
            reads += 1
            try:
                content, blob_sha = self._content(path, commit)
                if len(content.encode()) != size:
                    _fail("The scanned size differs from the committed tree")
                raw = _text_bytes(content)
            except HTTPException as exc:
                if exc.status_code != 422:
                    raise
                total += _FILE_BYTES - size
                reasons.add("unsupported_text")
                continue
            if blob_sha != node["sha"]:
                _fail("The scanned blob differs from the committed tree")
            sources[path] = content
            files.append({"path": path, "blob_sha": blob_sha, "content_sha256": hashlib.sha256(raw).hexdigest()})
        findings, limited = _signals(sources)
        if limited:
            reasons.add("finding_count_limit")
        if findings["unsupported"]:
            reasons.add("unsupported_static_patterns")
        return {"repository": self.repository, "repository_id": repository_id, "commit": commit, "base": base,
                "tree_sha": tree_sha, "scanner_version": "event-literals-v1", "files": files, "findings": findings,
                "coverage": {"complete": not reasons, "reasons": sorted(reasons)}, "workflows": workflows}

    def prepare_artifact(self, scan, definition, files, *, current=True):
        if not isinstance(scan, dict) or any(key not in scan for key in _SCAN_FIELDS):
            _fail("A complete immutable scan is required", 422)
        fresh = self.scan(scan["commit"], scan["base"])
        if any(fresh[key] != scan[key] for key in _SCAN_FIELDS):
            _fail("The scan no longer matches the committed repository")
        if not isinstance(definition, dict) or not isinstance(definition.get("id"), str) or not _ACTION.fullmatch(definition["id"]):
            _fail("The approved goal ID is invalid", 422)
        if not isinstance(files, list) or not 2 <= len(files) <= 8:
            _fail("A reviewed artifact requires its goal and one to seven source changes", 422)
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "before_sha", "content"} or not _relative(file.get("path")):
                _fail("The reviewed artifact file list is invalid", 422)
            _text_bytes(file["content"])
        goal_path = ".afterspan/goals/" + definition["id"] + ".json"
        plan = {"kind": "reviewed-artifact-v1", "repository": self.repository, "repository_id": scan["repository_id"],
                "base": scan["base"], "base_sha": scan["commit"], "goal_path": goal_path,
                "goal_sha256": hashlib.sha256(_goal_bytes(definition).encode()).hexdigest(), "files": files,
                "workflows": scan["workflows"], "scan": scan, "definition": definition}
        plan["artifact_sha256"] = _digest(plan)
        line_counts = self._plan(plan, current=current)
        return plan | {"status": "prepared", "assurance": "reviewed_bytes_only", "line_counts": line_counts}

    def merged_proposal(self, goal, pins, binding):
        path = ".afterspan/goals/" + goal + ".json"
        content, _ = self._content(path, pins["merge_sha"])
        if hashlib.sha256(content.encode()).hexdigest() != pins["goal_sha256"]:
            _fail("The merged goal differs from the requested bytes")
        from loop_onboarding import RepoGoal
        try:
            definition = RepoGoal.model_validate_json(content).model_dump(mode="json", by_alias=True)
        except ValueError:
            _fail("The merged goal is unsupported", 422)
        if definition["id"] != goal or content != _goal_bytes(definition):
            _fail("The merged goal must have its exact canonical bytes", 422)
        scan = self.scan(pins["base_sha"], pins["base"]) | binding | {
            "id": definition["scan_id"], "provenance": "server_recomputed_at_pinned_base"}
        # The committed scan ID is descriptive; no foreign database receipt is imported.
        changed = self._list("/pulls/" + str(pins["pr_number"]) + "/files")
        if not 2 <= len(changed) <= 8 or any(not isinstance(f.get("filename"), str) for f in changed):
            _fail("The merged artifact exceeds the reviewed file bounds", 422)
        files = []
        dependencies = set(definition["tracking"]["dependencies"])
        if not dependencies.issubset({ref['path'] for ref in scan['files']}):
            _fail("The merged goal depends on files outside the bounded scan", 422)
        companions = {p.rsplit('.', 1)[0] + '.test.' + p.rsplit('.', 1)[1]
                      for p in dependencies if p.endswith(('.ts', '.tsx', '.js', '.jsx'))}
        for file in sorted(changed, key=lambda f: f["filename"]):
            name = file["filename"]
            if not _relative(name) or name not in dependencies | companions | {path}:
                _fail("The merged PR changes an unapproved artifact path", 422)
            text, _ = self._content(name, pins["head_sha"])
            before = None if name == path else self._content(name, pins["base_sha"])[1]
            files.append({"path": name, "before_sha": before, "content": text})
        plan = self.prepare_artifact(scan, definition, files, current=False)
        receipt = self.verify_adopted(plan, pins | {"adoption": "merged-pr-v1", "artifact_sha256": plan['artifact_sha256']})
        return {"definition": definition, "scan": scan, "plan": plan, "merged_receipt": receipt}

    def verify_adopted(self, plan, receipt):
        self._plan(plan)
        number = receipt.get("pr_number")
        if (receipt.get("adoption") != "merged-pr-v1" or type(number) is not int or number <= 0
                or receipt.get('base_sha') != plan['base_sha'] or receipt.get('artifact_sha256') != plan['artifact_sha256']):
            _fail("The merged adoption receipt is invalid")
        pr = self._get("/pulls/" + str(number))
        for side, sha in (("base", plan["base_sha"]), ("head", receipt.get("head_sha"))):
            ref = pr.get(side, {})
            if (not isinstance(ref, dict) or not isinstance(ref.get("repo"), dict)
                    or ref.get("sha") != sha or ref.get("repo", {}).get("id") != plan["repository_id"]
                    or ref.get("repo", {}).get("full_name") != self.repository):
                _fail("The merged PR repository or lineage changed")
        head, merge = receipt.get("head_sha"), receipt.get("merge_sha")
        if (pr.get("number") != number or pr.get("merged") is not True
                or pr.get("base", {}).get("ref") != plan["base"]
                or pr.get("merge_commit_sha") != merge
                or any(not isinstance(sha, str) or not _SHA.fullmatch(sha) for sha in (head, merge))):
            _fail("The exact merged PR could not be verified")
        self._verify_artifact_head(plan, head)
        merged = self._commit(merge)
        if ([p.get("sha") for p in merged.get("parents", [])] != [plan["base_sha"], head]
                or merged.get("tree", {}).get("sha") != self._commit(head).get("tree", {}).get("sha")):
            _fail("The merge changed the reviewed tree or parent lineage")
        self.verify_committed_goal(plan, merge)
        return {"adoption": "merged-pr-v1", "status": "merged", "merged": True,
                "repository": self.repository, "repository_id": plan["repository_id"],
                "base_sha": plan["base_sha"], "head_sha": head, "merge_sha": merge,
                "pr_number": number, "pr_url": "https://github.com/" + self.repository + "/pull/" + str(number),
                "artifact_sha256": plan["artifact_sha256"],
                "action_id": receipt.get("action_id"), "release_status": "release_pending"}

    def artifact_release_binding(self, plan, merge, commit):
        self._plan(plan)
        if any(not isinstance(sha, str) or not _SHA.fullmatch(sha) for sha in (merge, commit)):
            _fail("The release commits are invalid", 422)
        if commit != merge:
            comparison = self._get("/compare/" + merge + "..." + commit)
            if (comparison.get("status") != "ahead" or comparison.get("base_commit", {}).get("sha") != merge
                    or comparison.get("merge_base_commit", {}).get("sha") != merge):
                _fail("The release does not descend from the verified merge")
        paths = sorted({file["path"] for file in plan["files"]} | set(plan["definition"]["tracking"]["dependencies"]))
        _, source_tree = self._tree(merge)
        _, target_tree = self._tree(commit)
        manifest = []
        for path in paths:
            source_node, target_node = source_tree.get(path), target_tree.get(path)
            if (not source_node or not target_node or source_node['type'] != 'blob'
                    or source_node['mode'] not in ('100644', '100755')
                    or any(source_node[key] != target_node[key] for key in ('type', 'mode', 'sha'))):
                _fail("The release changed an artifact or dependency file type, mode or blob")
            source, _ = self._content(path, merge)
            target, blob = self._content(path, commit)
            if source != target:
                _fail("The release changed an artifact or measurement dependency")
            manifest.append({"path": path, "blob_sha": blob, "content_sha256": hashlib.sha256(target.encode()).hexdigest()})
        return {"merge_commit": merge, "commit": commit, "files": manifest,
                "scope": "unchanged_artifact_and_declared_dependencies", "other_source_reviewed": False}

    def _reviewed_plan(self, plan, current):
        if (any(key not in plan for key in (*_REVIEWED_FIELDS, "artifact_sha256"))
                or _digest({key: plan[key] for key in _REVIEWED_FIELDS}) != plan["artifact_sha256"]):
            _fail("The plan artifact changed")
        self._pin(plan["repository"], plan["base"], plan["base_sha"])
        scan, definition, files = plan["scan"], plan["definition"], plan["files"]
        if (not isinstance(scan, dict) or any(key not in scan for key in _SCAN_FIELDS)
                or type(plan["repository_id"]) is not int or plan["repository_id"] <= 0
                or scan["repository"] != self.repository or scan["repository_id"] != plan["repository_id"]
                or scan["commit"] != plan["base_sha"] or scan["base"] != plan["base"]
                or self._identity() != plan["repository_id"] or scan["scanner_version"] != "event-literals-v1"):
            _fail("The plan scan binding changed")
        if (not isinstance(definition, dict) or set(definition) != {"schema", "id", "question", "scan_id", "measurement", "mapping", "tracking"}
                or definition.get("schema") != "afterspan-repo-goal-v2" or not isinstance(definition.get("id"), str)
                or not _ACTION.fullmatch(definition["id"]) or plan["goal_path"] != ".afterspan/goals/" + definition["id"] + ".json"
                or definition.get("scan_id") != scan.get("id") or not isinstance(definition.get("scan_id"), str)
                or not isinstance(definition.get("question"), str) or not 1 <= len(definition["question"]) <= 2000):
            _fail("The approved goal does not match the scan", 422)
        tracking = definition.get("tracking")
        if (not isinstance(tracking, dict) or set(tracking) != {"kind", "dependencies"}
                or tracking.get("kind") != "reviewed-artifact-v1" or not isinstance(tracking.get("dependencies"), list)
                or not 1 <= len(tracking["dependencies"]) <= 16
                or any(not isinstance(path, str) for path in tracking["dependencies"])
                or len(set(tracking["dependencies"])) != len(tracking["dependencies"])):
            _fail("The approved tracking dependencies are unsupported", 422)
        measurement, mapping = definition.get("measurement"), definition.get("mapping")
        from loop_onboarding import RepoGoal
        try:
            RepoGoal.model_validate(definition)
        except ValueError:
            _fail("The approved measurement is unsupported", 422)
        if (not isinstance(scan["findings"], dict)
                or measurement["event"] not in {event.get("name") for event in scan["findings"].get("events", []) if isinstance(event, dict)}
                or not isinstance(mapping, dict) or set(mapping) - {"kind", "environment", "environment_property", "release_property", "connection_id", "connection_revision"}
                or mapping.get("kind") not in ("native", "posthog")):
            _fail("The approved measurement is unsupported", 422)
        goal_content = _goal_bytes(definition)
        if hashlib.sha256(goal_content.encode()).hexdigest() != plan["goal_sha256"]:
            _fail("The approved goal bytes changed")
        tree_sha, tree = self._tree(plan["base_sha"])
        workflows = {path: node["sha"] for path, node in tree.items()
                     if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))}
        if tree_sha != scan["tree_sha"] or workflows != plan["workflows"] or scan["workflows"] != plan["workflows"]:
            _fail("The plan tree or workflows changed")
        if not isinstance(scan["files"], list) or len(scan["files"]) > 64:
            _fail("The scanned file manifest is invalid", 422)
        scanned = {}
        for ref in scan["files"]:
            if (not isinstance(ref, dict) or set(ref) != {"path", "blob_sha", "content_sha256"}
                    or not _source_path(ref.get("path")) or ref["path"] in scanned
                    or tree.get(ref["path"], {}).get("sha") != ref["blob_sha"]
                    or tree[ref["path"]]["mode"] not in ("100644", "100755")
                    or not isinstance(ref["content_sha256"], str) or not _DIGEST.fullmatch(ref["content_sha256"])):
                _fail("The scanned file manifest changed")
            scanned[ref["path"]] = ref
        dependencies = tracking["dependencies"]
        if any(path not in scanned for path in dependencies):
            _fail("The goal depends on files outside the scan", 422)
        if not any(event.get("name") == measurement["event"] and event.get("path") in dependencies and event.get("sink") == mapping["kind"]
                   for event in scan["findings"].get("events", []) if isinstance(event, dict)):
            _fail("The selected event is outside the approved source dependencies", 422)
        before = {}
        for path in dependencies:
            content, blob_sha = self._content(path, plan["base_sha"])
            if blob_sha != scanned[path]["blob_sha"] or hashlib.sha256(_text_bytes(content)).hexdigest() != scanned[path]["content_sha256"]:
                _fail("A committed dependency changed")
            before[path] = content
        if not isinstance(files, list) or not 2 <= len(files) <= 8:
            _fail("A reviewed artifact requires its goal and one to seven source changes", 422)
        changed = {file.get('path') for file in files if isinstance(file, dict) and isinstance(file.get('path'), str)}
        companions = {path.rsplit('.', 1)[0] + '.test.' + path.rsplit('.', 1)[1]
                      for path in dependencies if path in changed and path.endswith(('.ts', '.tsx', '.js', '.jsx'))}
        for path in companions & changed:
            node = tree.get(path)
            if not node or node['type'] != 'blob' or node['mode'] != '100644':
                _fail("Companion tests must be existing regular files beside a changed source dependency", 422)
            content, sha = self._content(path, plan['base_sha'])
            if sha != node['sha']:
                _fail("A companion test changed since the pinned tree")
            before[path] = content
        paths, total, lines = set(), 0, 0
        for file in files:
            if not isinstance(file, dict) or set(file) != {"path", "before_sha", "content"} or not _relative(file.get("path")) or file["path"] in paths:
                _fail("The reviewed artifact file list is invalid", 422)
            path = file["path"]
            paths.add(path)
            total += len(_text_bytes(file["content"]))
            lines += len(file["content"].splitlines()) + len(before.get(path, "").splitlines())
            for index in range(1, len(path.split("/"))):
                parent = tree.get("/".join(path.split("/")[:index]))
                if parent and parent["type"] != "tree":
                    _fail("An artifact parent is not a directory", 422)
            if path == plan["goal_path"]:
                if path in tree or file["before_sha"] is not None or file["content"] != goal_content:
                    _fail("The new goal must contain exactly the approved canonical bytes", 422)
            elif path in companions:
                if file['before_sha'] != tree[path]['sha'] or file['content'] == before[path]:
                    _fail("Companion tests must change from their exact pinned bytes", 422)
            elif (path not in dependencies or path.rsplit("/", 1)[-1] in _MANIFESTS
                    or not _source_path(path) or file["before_sha"] != scanned[path]["blob_sha"]
                    or file["content"] == before[path]):
                _fail("Only changed, scanned source dependencies may be edited", 422)
        if plan["goal_path"] not in paths or total > _REVIEW_BYTES or lines > _REVIEW_LINES:
            _fail("The reviewed artifact is missing its goal or exceeds 128 KiB or 4000 review lines", 422)
        if current and self._head(plan["base"])["commit"]["sha"] != plan["base_sha"]:
            _fail("The base branch moved. Prepare a new plan")
        counts = {"added": 0, "deleted": 0}
        for file in files:
            for line in difflib.ndiff(before.get(file["path"], "").splitlines(), file["content"].splitlines()):
                if line.startswith("+ "):
                    counts["added"] += 1
                elif line.startswith("- "):
                    counts["deleted"] += 1
        return counts

    def verify_committed_goal(self, plan, commit):
        self._plan(plan)
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            _fail("The goal commit is invalid", 422)
        content, _ = self._content(plan["goal_path"], commit)
        if (hashlib.sha256(content.encode()).hexdigest() != plan["goal_sha256"]
                or plan.get("kind") == "reviewed-artifact-v1" and content != _goal_bytes(plan["definition"])):
            _fail("The committed goal differs from the approved bytes")
        return {"commit": commit, "path": plan["goal_path"], "content_sha256": plan["goal_sha256"], "verified": True}

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
        local_mapping = {"source": "native", "environment": "local", "observation_id_property": "observation_id"}
        production = definition.get("mapping") == local_mapping | {"environment": "production"}
        if (definition.get("mapping") != local_mapping and not production
                or not isinstance(measurement, dict) or set(measurement) != {"unit", "entry", "completion", "followup_seconds", "entry_rule", "rubric"}
                or measurement.get("unit") != "workspace" or measurement.get("followup_seconds") != 86400
                or measurement.get("entry_rule") != "first_in_entry_window" or measurement.get("rubric") != "aggregate-answer-v1"
                or not _event_rule(measurement.get("completion"), "codex.answer.ready")):
            _fail("The goal does not match the supported first-answer measurement contract", 422)
        if production:
            if (not _event_rule(measurement.get("entry"), "codex.connection.started")
                    or measurement["entry"].get("filters") or measurement["completion"].get("filters")):
                _fail("Production goals require the unfiltered service Codex connection contract", 422)
        else:
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
        if snapshot["definition"]["mapping"]["environment"] != "local":
            _fail("The tracking repair supports local goals only", 422)
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
        if isinstance(plan, dict) and plan.get("kind") == "reviewed-artifact-v1":
            return self._reviewed_plan(plan, current)
        if not isinstance(plan, dict) or any(key not in plan for key in (*_PLAN_FIELDS, "artifact_sha256")):
            _fail("A complete immutable plan is required", 422)
        if plan.get("kind") is not None:
            _fail("The plan kind is unsupported", 422)
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
        if plan.get("kind") == "reviewed-artifact-v1":
            return self._verify_artifact_head(plan, head)
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

    def _verify_artifact_head(self, plan, head):
        commit = self._commit(head)
        if [parent.get("sha") for parent in commit.get("parents", [])] != [plan["base_sha"]]:
            _fail("The tracking branch has an unexpected parent")
        _, base_tree = self._tree(plan["base_sha"])
        _, head_tree = self._tree(head)
        expected = {path: (node["type"], node["mode"], node["sha"]) for path, node in base_tree.items() if node["type"] != "tree"}
        directories = {path for path, node in base_tree.items() if node["type"] == "tree"}
        for file in plan["files"]:
            path = file["path"]
            expected[path] = ("blob", base_tree[path]["mode"] if path in base_tree else "100644", _blob(file["content"].encode()))
            directories.update("/".join(path.split("/")[:index]) for index in range(1, len(path.split("/"))))
        if (expected != {path: (node["type"], node["mode"], node["sha"]) for path, node in head_tree.items() if node["type"] != "tree"}
                or {path for path, node in head_tree.items() if node["type"] == "tree"} != directories):
            _fail("The branch tree differs from the approved artifact")
        comparison = self._get("/compare/" + plan["base_sha"] + "..." + head)
        rows = comparison.get("files")
        if (comparison.get("status") != "ahead" or comparison.get("ahead_by") != 1 or comparison.get("behind_by") != 0
                or comparison.get("total_commits") != 1 or not isinstance(rows, list) or len(rows) != len(plan["files"])
                or any(not isinstance(row, dict) or not isinstance(row.get("filename"), str) for row in rows)
                or len({row["filename"] for row in rows}) != len(rows)):
            _fail("The provider diff differs from the approved artifact")
        actual = {row["filename"]: row for row in rows}
        if set(actual) != {file["path"] for file in plan["files"]}:
            _fail("The provider diff changed an unapproved file")
        for file in plan["files"]:
            row, path = actual[file["path"]], file["path"]
            before = self._content(path, plan["base_sha"])[0] if file["before_sha"] is not None else ""
            additions, deletions = _line_changes(before, file["content"])
            if (row.get("status") != ("modified" if file["before_sha"] is not None else "added")
                    or row.get("sha") != expected[path][2] or type(row.get("additions")) is not int or row["additions"] != additions
                    or type(row.get("deletions")) is not int or row["deletions"] != deletions
                    or self._content(path, head)[0] != file["content"]):
                _fail("The branch file or patch counts differ from the approved artifact")
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
                           "files": [{"path": file["path"], "sha": _blob(file["content"].encode())} for file in plan["files"]]})
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
            result["files"] = [{"path": file["path"], "sha": _blob(file["content"].encode())} for file in plan["files"]]
        return result, pr

    def _write_artifact(self, plan, branch, guard):
        _, tree = self._tree(plan["base_sha"])
        entries = []
        for file in plan["files"]:
            result = self._request("POST", "/git/blobs", {"content": base64.b64encode(file["content"].encode()).decode(), "encoding": "base64"}, guard=guard)
            sha = _blob(file["content"].encode())
            if not isinstance(result, dict) or result.get("sha") != sha:
                raise _UncertainWrite()
            entries.append({"path": file["path"], "mode": tree[file["path"]]["mode"] if file["path"] in tree else "100644", "type": "blob", "sha": sha})
        result = self._request("POST", "/git/trees", {"base_tree": plan["scan"]["tree_sha"], "tree": entries}, guard=guard)
        if not isinstance(result, dict) or not isinstance(result.get("sha"), str) or not _SHA.fullmatch(result["sha"]):
            raise _UncertainWrite()
        commit = self._request("POST", "/git/commits", {"message": "Add the reviewed tracking artifact and goal", "tree": result["sha"], "parents": [plan["base_sha"]]}, guard=guard)
        if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str) or not _SHA.fullmatch(commit["sha"]):
            raise _UncertainWrite()
        self._verify_head(plan, commit["sha"])
        self._plan(plan, current=True)
        self._request("PATCH", "/git/refs/heads/" + quote(branch, safe="/"), {"sha": commit["sha"], "force": False}, guard=guard)

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
                if plan.get("kind") == "reviewed-artifact-v1":
                    self._write_artifact(plan, receipt["branch"], guard)
                else:
                    self._request("PUT", "/contents/" + PATH, {"message": "Track the first answer completion", "branch": receipt["branch"],
                                  "sha": plan["files"][0]["before_sha"], "content": base64.b64encode(plan["files"][0]["content"].encode()).decode()}, guard=guard)
                wrote = True
                receipt, _ = self._state(plan, action_id)
            self._plan(plan, current=True)
            reviewed = plan.get("kind") == "reviewed-artifact-v1"
            self._request("POST", "/pulls", {"title": "Add reviewed tracking and its goal" if reviewed else "Track the first answer completion",
                          "head": receipt["branch"], "base": plan["base"], "draft": True,
                          "body": ("Contains the exact reviewed source changes and canonical goal. Human review and independent checks must assess the behavior."
                                   if reviewed else "Adds the supported local completion event before the existing answer return.") + "\n\n" + self._marker(plan, action_id)}, guard=guard)
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
        if receipt and receipt.get("adoption") == "merged-pr-v1":
            return self.verify_adopted(plan, receipt)
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
        paths = [file["path"] for file in plan["files"]]
        if (policy.get("state") != "active" or policy.get("repository") != self.repository or policy.get("base") != plan["base"]
                or not {"merge", "release-on-merge"}.issubset(set(policy.get("operations", [])))
                or policy.get("paths") != paths or not isinstance(policy.get("environment"), str) or not policy["environment"]
                or plan.get("kind") == "reviewed-artifact-v1" and policy.get("artifact_sha256") != plan["artifact_sha256"]):
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
