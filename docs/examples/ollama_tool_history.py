"""
title: Ollama Tool History Compatibility
description: Close an interrupted tool turn before replaying a user follow-up.
"""


class Filter:
    def __init__(self):
        self.toggle = True

    def request(self, body: dict, __model__: dict | None = None) -> dict:
        if (__model__ or {}).get("owned_by") != "ollama":
            return body

        messages = body.get("messages") or []
        repaired = []
        awaiting_reply = False
        previous_role = None

        for message in messages:
            role = message.get("role")
            if role == "user":
                if awaiting_reply and previous_role == "tool":
                    # Close the prior turn in the prompt with an empty assistant turn.
                    repaired.append({"role": "assistant", "content": ""})
                awaiting_reply = True
            elif role == "assistant" and not message.get("tool_calls"):
                awaiting_reply = False

            repaired.append(message)
            if role != "system":
                previous_role = role

        if len(repaired) == len(messages):
            return body
        return {**body, "messages": repaired}
