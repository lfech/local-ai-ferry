"""Exercise the authenticated Open WebUI chat API with a disposable account."""

import json
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


class WebUIChat:
    def __init__(self, address):
        self.address = address
        self.opener = build_opener(ProxyHandler({}))
        self.token = None

    def request(self, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.token is not None:
            headers["Authorization"] = "Bearer " + self.token
        body = json.dumps(data).encode() if data is not None else None
        with self.opener.open(Request(self.address + path, data=body, headers=headers), timeout=120) as response:
            return json.load(response)

    def sign_up(self, credentials):
        account = self.request("/api/v1/auths/signup", {"name": "Handoff test", **credentials})
        if account["role"] != "admin":
            raise AssertionError("Open WebUI did not create an admin for the first user.")
        self.token = account["token"]
        return account["id"]

    def sign_in(self, credentials):
        account = self.request("/api/v1/auths/signin", credentials)
        self.token = account["token"]
        return account["id"]

    def require_sign_in(self):
        try:
            self.request("/api/models")
        except HTTPError as error:
            if error.code in (401, 403):
                return
            raise
        raise AssertionError("Open WebUI returned a model list without authentication.")

    def require_model(self, model):
        models = {entry["id"] for entry in self.request("/api/models")["data"]}
        if model not in models:
            raise AssertionError(f"Open WebUI did not offer {model!r}: {sorted(models)}")

    def reply(self, model, messages):
        response = self.request("/api/chat/completions", {
            "model": model, "messages": messages, "stream": False, "max_tokens": 48, "temperature": 0,
        })
        choice = response["choices"][0]
        message = choice["message"]
        content = message.get("content")
        if (message["role"] != "assistant" or not isinstance(content, str) or not content.strip()
                or choice.get("finish_reason") not in ("stop", "length")):
            raise AssertionError(f"Open WebUI returned an incomplete chat response: {choice}")
        return {"role": "assistant", "content": content}

    def save(self, model, messages):
        history = {}
        previous = None
        for message in messages:
            ident = uuid4().hex
            history[ident] = {**message, "id": ident, "parentId": previous, "childrenIds": []}
            if previous is not None:
                history[previous]["childrenIds"].append(ident)
            previous = ident
        return self.request("/api/v1/chats/new", {"chat": {
            "title": "Cold handoff test",
            "models": [model],
            "messages": messages,
            "history": {"messages": history, "currentId": previous},
        }})["id"]

    def saved_messages(self, chat_id, model):
        chat = self.request(f"/api/v1/chats/{chat_id}")["chat"]
        if chat["models"] != [model]:
            raise AssertionError("Open WebUI returned a different model selection for the saved chat.")
        # The API adds authorship fields to the saved messages.
        return [{"role": message["role"], "content": message["content"]} for message in chat["messages"]]


if __name__ == "__main__":
    address, model = sys.argv[1:]
    fixture = json.load(sys.stdin)
    webui = WebUIChat(address)
    webui.require_sign_in()
    if webui.sign_in(fixture["credentials"]) != fixture["account"]:
        raise AssertionError("Restore changed the saved Open WebUI account.")
    webui.require_model(model)
    messages = webui.saved_messages(fixture["chat_id"], model)
    if messages != fixture["messages"]:
        raise AssertionError("Restore changed the saved chat messages.")
    webui.reply(model, [*messages, {"role": "user", "content": "Say hello again."}])
    print("Authenticated Open WebUI chat and saved conversation passed.")
