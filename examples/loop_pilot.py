"""Opt-in local pilot. The caller supplies a structured answer and an opaque workspace."""


def answer_question(collector,workspace,observation_id,answer):
    collector.track('codex.connection.started',distinct_id=workspace,properties={'environment':'local','observation_id':observation_id})
    return answer
