import ast
import unittest

from loop_recipe import entry_event, repair


BASELINE = '''def answer_question(collector, workspace, observation_id, answer, release):
    collector.track("codex.question.started", distinct_id=workspace, properties={"environment":"local","observation_id":observation_id,"release":release})
    return answer
'''
REPAIRED = '''def answer_question(collector, workspace, observation_id, answer, release):
    collector.track("codex.question.started", distinct_id=workspace, properties={"environment":"local","observation_id":observation_id,"release":release})
    collector.track("codex.answer.ready", distinct_id=workspace, properties={"environment":"local","observation_id":observation_id,"release":release})
    return answer
'''


class RecipeTests(unittest.TestCase):
    def test_only_adds_the_literal_completion_hook(self):
        self.assertEqual(repair(BASELINE), REPAIRED)
        before, after = ast.parse(BASELINE), ast.parse(REPAIRED)
        del after.body[0].body[-2]
        self.assertEqual(ast.dump(after), ast.dump(before))

    def test_already_tracked_is_healthy(self):
        self.assertIsNone(repair(REPAIRED))
        self.assertEqual(repair(BASELINE).count('"codex.answer.ready"'), 1)

    def test_derives_the_existing_entry_event_and_rejects_ambiguity(self):
        self.assertEqual(entry_event(BASELINE), "codex.question.started")
        self.assertEqual(entry_event(REPAIRED), "codex.question.started")
        self.assertEqual(entry_event(BASELINE.replace("codex.question.started", "codex.connection.started")), "codex.connection.started")
        for source in [BASELINE.replace('"environment":"local"', '"environment":"production"'),
                       BASELINE.replace('"observation_id":observation_id', '"observation_id":answer'),
                       BASELINE.replace("    return answer", BASELINE.splitlines()[1] + "\n    return answer")]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                entry_event(source)

    def test_unsupported_or_ambiguous_functions_are_rejected(self):
        cases = ["not python (", BASELINE + BASELINE, BASELINE.replace("    return answer", "    return str(answer)"),
                 BASELINE.replace("    return answer", "    if answer:\n        return answer\n    return answer"),
                 BASELINE.replace("    return answer", "        return answer"), BASELINE.replace("release):", "release=None):"),
                 BASELINE.replace("def answer_question", "async def answer_question"), "@decorator\n" + BASELINE,
                 "class Wrapper:\n" + "".join("    " + line for line in BASELINE.splitlines(keepends=True)),
                 BASELINE.replace("    return answer", '    collector.track("codex.answer.ready")\n    return answer'),
                 REPAIRED.replace('"environment":"local"', '"environment":"production"'),
                 REPAIRED.replace('"observation_id":observation_id', '"observation_id":answer')]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(ValueError):
                repair(source)

    def test_emitted_event_has_no_answer_or_credentials(self):
        events = []

        class Collector:
            def track(self, event, **kwargs):
                events.append({"event": event, **kwargs})

        namespace = {}
        exec(repair(BASELINE), namespace)
        answer = {"rate": 0, "private_answer": "a private patient answer"}
        self.assertIs(namespace["answer_question"](Collector(), "workspace-1", "observation-1", answer, "a" * 40), answer)
        self.assertEqual(events[-1], {"event": "codex.answer.ready", "distinct_id": "workspace-1",
                                     "properties": {"environment": "local", "observation_id": "observation-1", "release": "a" * 40}})


if __name__ == "__main__":
    unittest.main()
