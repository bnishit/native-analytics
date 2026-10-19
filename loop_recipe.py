import ast


PATH = "examples/loop_pilot.py"
TRACK = '    collector.track("codex.answer.ready", distinct_id=workspace, properties={"environment":"local","observation_id":observation_id,"release":release})\n'
_CALL = ast.parse(TRACK.strip()).body[0]


def _target(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise ValueError("The tracking source is not supported") from exc
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "answer_question"]
    if len(functions) != 1 or functions[0] not in tree.body:
        raise ValueError("One top-level answer_question function is required")
    function = functions[0]
    args = function.args
    if (not isinstance(function, ast.FunctionDef) or function.decorator_list or args.posonlyargs
            or [arg.arg for arg in args.args] != ["collector", "workspace", "observation_id", "answer", "release"]
            or args.vararg or args.kwarg or args.kwonlyargs or args.defaults):
        raise ValueError("The answer_question signature is not supported")
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    lines = source.splitlines(keepends=True)
    if (len(returns) != 1 or function.body[-1] is not returns[0]
            or not isinstance(returns[0].value, ast.Name) or returns[0].value.id != "answer"
            or lines[returns[0].lineno - 1].rstrip("\r\n") != "    return answer"
            or sum(line.rstrip("\r\n") == "    return answer" for line in lines) != 1):
        raise ValueError("One exact return hook is required")
    return tree, function, lines, returns[0].lineno


def entry_event(source: str) -> str:
    _, function, _, _ = _target(source)
    events = []
    for statement in function.body[:-1]:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if len(call.args) != 1 or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
            continue
        event = call.args[0].value
        expected = ast.Expr(value=ast.Call(func=_CALL.value.func, args=[ast.Constant(event)], keywords=_CALL.value.keywords))
        if event != "codex.answer.ready" and ast.dump(statement) == ast.dump(expected):
            events.append(event)
    if len(events) != 1:
        raise ValueError("One supported local entry event is required")
    return events[0]


def repair(source: str) -> str | None:
    tree, function, lines, return_line = _target(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "codex.answer.ready"]
    if calls:
        if len(calls) == 1 and len(function.body) > 1 and ast.dump(function.body[-2]) == ast.dump(_CALL):
            return None
        raise ValueError("Completion tracking is ambiguous")
    result = "".join(lines[:return_line - 1] + [TRACK] + lines[return_line - 1:])
    changed = ast.parse(result)
    changed_function = next(node for node in changed.body if isinstance(node, ast.FunctionDef) and node.name == "answer_question")
    del changed_function.body[-2]
    if ast.dump(changed) != ast.dump(tree):
        raise ValueError("The repair would change business behavior")
    return result
