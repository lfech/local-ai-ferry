"""Drive the saved terminal connection through a disposable Open WebUI."""

import asyncio
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sys
from zipfile import ZipFile

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, FormData


CONNECTION = "/api/v1/configs/terminal_servers"
TERMINALS = "/api/v1/terminals/"
WORKSPACE = TERMINALS + "workspace"

BUILD_SOFTWARE = """set -eu
cat > CMakeLists.txt <<'EOF'
cmake_minimum_required(VERSION 3.16)
project(Arithmetic LANGUAGES C CXX)
enable_testing()
add_library(arithmetic arithmetic.c)
add_executable(check_arithmetic check.cpp)
target_link_libraries(check_arithmetic PRIVATE arithmetic)
add_test(NAME arithmetic COMMAND check_arithmetic)
EOF
cat > arithmetic.c <<'EOF'
int add(int left, int right) { return left + right - 1; }
EOF
cat > check.cpp <<'EOF'
extern "C" int add(int, int);
int main() { return add(19, 23) == 42 ? 0 : 1; }
EOF
gcc -std=c11 -Wall -Wextra -Werror -fsyntax-only arithmetic.c
cmake -S . -B build -G Ninja -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
cmake --build build
"""

FIX_SOFTWARE = """set -eu
python3 - <<'PY'
from pathlib import Path
source = Path("arithmetic.c")
source.write_text(source.read_text().replace("right - 1", "right"))
PY
cmake --build build
cat > test_cli.py <<'EOF'
import subprocess

def test_arithmetic_binary():
    subprocess.run(["./build/check_arithmetic"], check=True)
EOF
"""

TEST_SOFTWARE = """set -eu
ctest --test-dir build --output-on-failure
pytest -q test_cli.py
"""

CONFIGURE_DOCS = """set -eu
mkdir -p docs
printf "project = 'Arithmetic'\n" > docs/conf.py
"""
BUILD_DOCS = "sphinx-build -W -q -b html docs html"


def credentials(name):
    return {"email": f"{name}@example.com", "password": "Disposable-workspace-test-42!"}


class WebUI:
    def __init__(self, session, url):
        self.session = session
        self.url = url

    async def request(self, method, path, account=None, *, status=200, raw=False, headers=None, **kwargs):
        headers = dict(headers or {})
        if account:
            headers["Authorization"] = f"Bearer {account['token']}"
        async with self.session.request(method, self.url + path, headers=headers, **kwargs) as response:
            if response.status != status:
                body = (await response.text())[:512]
                raise AssertionError(f"{method} {path}: expected {status}, got {response.status}\n{body}")
            if status != 200:
                return None
            return await (response.read() if raw else response.json())

    async def run(self, account, command, *, exit_code=0, wait=55, chat=None, agent=False):
        # Model tool calls use the published agent paths; the plain path is the account's own request.
        result = await self.request(
            "POST", WORKSPACE + ("/agent" if agent else "") + "/execute", account, params={"wait": wait},
            json={"command": command}, headers={"X-Session-Id": chat} if chat else None,
        )
        assert result["status"] != "running", result
        if exit_code is None:
            assert result["exit_code"] not in (0, None), result
        else:
            assert result["exit_code"] == exit_code, result

    async def upload_docs(self, account, content):
        form = FormData()
        form.add_field("file", content.encode(), filename="index.rst", content_type="text/plain")
        uploaded = await self.request(
            "POST", WORKSPACE + "/files/upload", account, params={"directory": "docs"}, data=form,
        )
        assert uploaded["size"] == len(content.encode()), uploaded

    async def check_docs(self, account, expected):
        data = await self.request(
            "POST", WORKSPACE + "/files/archive", account, json={"paths": ["html/index.html"]}, raw=True,
        )
        with ZipFile(BytesIO(data)) as archive:
            assert expected.encode() in archive.read("index.html"), expected


async def configure(webui, key):
    admin = await webui.request("POST", "/api/v1/auths/signup", json={"name": "admin", **credentials("admin")})
    assert admin["role"] == "admin"
    accounts = []
    for name in ("alice", "bob", "denied"):
        account = await webui.request(
            "POST", "/api/v1/auths/add", admin, json={"name": name, "role": "user", **credentials(name)},
        )
        assert account["role"] == "user"
        accounts.append(account)
    connection = {
        "id": "workspace", "name": "Build workspace", "url": "http://terminal-server:8000",
        "auth_type": "bearer", "key": key,
        "config": {"access_grants": [
            {"principal_type": "user", "principal_id": account["id"], "permission": "read"}
            for account in accounts[:2]
        ]},
    }
    verified = await webui.request("POST", CONNECTION + "/verify", admin, json=connection)
    assert verified == {"status": True, "type": "terminal"}, verified
    connection["server_type"] = verified["type"]
    await webui.request("POST", CONNECTION, admin, json={"TERMINAL_SERVER_CONNECTIONS": [connection]})
    return accounts


async def check_access(webui, accounts):
    alice, bob, denied = accounts
    for account in (alice, bob):
        connections = await webui.request("GET", TERMINALS, account)
        assert [connection["id"] for connection in connections] == ["workspace"], connections
    assert await webui.request("GET", TERMINALS, denied) == []
    await webui.request("POST", WORKSPACE + "/execute", denied, status=404, json={"command": "true"})
    metadata = await webui.request("GET", WORKSPACE + "/api/config", alice)
    assert metadata["features"]["terminal"], metadata
    schema = await webui.request("GET", WORKSPACE + "/openapi.json", alice)
    assert "/agent/execute" in schema["paths"] and "/execute" not in schema["paths"], sorted(schema["paths"])


async def check_draft_directories(webui, account):
    cwd = await webui.request("GET", WORKSPACE + "/files/cwd", account)
    assert cwd["cwd"] == cwd["home"] == "/home/user", cwd
    assert cwd["root"] == {"path": "/home", "label": "home"}, cwd
    listing = await webui.request("GET", WORKSPACE + "/files/list", account, params={"directory": "."})
    assert listing["dir"] == "/home/user" and listing["writable"], listing
    folders = await entries(webui, WORKSPACE, account, cwd["root"]["path"])
    assert folders["user"] == folders["chat"] == "directory", folders
    draft = await webui.request("GET", WORKSPACE + "/files/list", account, params={"directory": "/home/chat"})
    assert draft["dir"] == "/home/chat" and not draft["writable"] and not draft["entries"], draft


async def entries(webui, terminal, account, directory="."):
    listing = await webui.request("GET", terminal + "/files/list", account, params={"directory": directory})
    return {entry["name"]: entry["type"] for entry in listing["entries"]}


def folder_history(account):
    return f"/home/user/.open-webui/users/{account['id']}/folders"


async def picker_choices(webui, account, chat=None):
    directory = folder_history(account)
    headers = {"X-Session-Id": chat} if chat else None
    response = await webui.request("GET", WORKSPACE + "/files/list", account, headers=headers,
                                   params={"directory": directory})
    # The v0.13.4 menu reads the eight newest path-hashed files from this directory.
    records = [
        entry for entry in response["entries"]
        if entry["type"] == "file" and re.fullmatch(r"[a-f0-9]{64}\.json", entry["name"])
    ]
    records.sort(key=lambda entry: entry.get("modified") or 0, reverse=True)
    files = await asyncio.gather(*(
        webui.request("GET", WORKSPACE + "/files/read", account, headers=headers,
                      params={"path": f"{directory}/{entry['name']}"})
        for entry in records[:8]
    ))
    return [json.loads(file["content"])["path"] for file in files]


async def check_picker_choices(webui, account, other_account):
    defaults = ["/home/user", "/home/chat"]
    assert await picker_choices(webui, account) == defaults
    await webui.request("GET", WORKSPACE + "/files/list", account, status=404,
                        params={"directory": folder_history(other_account)})

    # The native writes still store ordinary recent folders and a choice of chat.
    recent = [f"/home/user/project-{number}" for number in range(8)]
    for path in [*defaults, *recent]:
        filename = sha256(path.encode()).hexdigest() + ".json"
        await webui.request("POST", WORKSPACE + "/files/write", account, json={
            "path": f"{folder_history(account)}/{filename}", "content": json.dumps({"path": path}),
        })
    choices = await picker_choices(webui, account)
    assert choices[:2] == defaults and len(choices[2:]) == 6 and set(choices[2:]) <= set(recent), choices
    stored = await entries(webui, WORKSPACE, account, folder_history(account))
    assert {sha256(path.encode()).hexdigest() + ".json" for path in recent} <= stored.keys()
    assert await picker_choices(webui, other_account) == defaults


async def check_chat_directories(webui, account, other_account):
    # Open WebUI sends the picker's selection as the working directory of the new chat.
    chats = []
    for title, directory in (("Delete active workspace", "/home/chat/"), ("Keep through restart", "/home/user")):
        saved = await webui.request(
            "POST", "/api/v1/chats/new", account,
            json={"chat": {"title": title, "terminal_id": "workspace", "working_directory": directory}},
        )
        assert saved["chat"]["working_directory"] == directory, saved
        chats.append(saved)
    ids = [saved["id"] for saved in chats]
    first, second = (f"{WORKSPACE}/chats/{chat}" for chat in ids)
    chat_header = {"X-Session-Id": ids[0]}
    assert (await picker_choices(webui, account, chat=ids[0]))[:2] == ["/home/user", "/home/chat"]
    cwd = await webui.request("GET", WORKSPACE + "/files/cwd", account, headers=chat_header)
    assert cwd == {"cwd": "/home/chat", "home": "/home/user", "root": {"path": "/home", "label": "home"}}, cwd
    folders = await entries(webui, first, account, "/home")
    assert folders["user"] == folders["chat"] == "directory", folders

    # The first message initializes the selected directory after Open WebUI saves the chat.
    selected = await webui.request("POST", WORKSPACE + "/files/cwd", account, headers=chat_header,
                                   json={"path": chats[0]["chat"]["working_directory"]})
    assert selected["cwd"] == "/home/chat", selected
    await webui.run(account, """set -eu
mkdir project
printf first > project/model.txt
""", chat=ids[0])
    assert (await entries(webui, first, account))["project"] == "directory"
    assert (await entries(webui, first, account, "project"))["model.txt"] == "file"

    selected = await webui.request("POST", WORKSPACE + "/files/cwd", account, headers=chat_header,
                                   json={"path": "/home/user"})
    assert selected["cwd"] == "/home/user", selected
    await webui.run(account, "mkdir shared-check && printf shared > shared-check/model.txt", chat=ids[0])
    assert (await entries(webui, first, account))["shared-check"] == "directory"
    assert (await entries(webui, first, account, "shared-check"))["model.txt"] == "file"
    assert (await entries(webui, WORKSPACE, account))["shared-check"] == "directory"
    # Model commands read the account's folder in the selected directory but cannot write to it.
    await webui.run(account, "test -f shared-check/model.txt && ! touch model-denied && pwd > /home/chat/agent-cwd",
                    chat=ids[0], agent=True)
    assert "model-denied" not in await entries(webui, first, account)
    agent_cwd = await webui.request("GET", first + "/files/read", account, params={"path": "/home/chat/agent-cwd"})
    assert agent_cwd["content"].strip() == "/home/user", agent_cwd

    assert "project" not in await entries(webui, second, account, "/home/chat")
    assert (await entries(webui, second, account, "/home/user/shared-check"))["model.txt"] == "file"
    await webui.request("GET", first + "/files/list", other_account, status=403)
    await webui.run(account, "printf retained > /home/chat/marker.txt", chat=ids[1])
    archived = await webui.request("POST", f"/api/v1/chats/{ids[1]}/archive", account)
    assert archived["archived"], archived
    assert await webui.request("DELETE", f"/api/v1/chats/{ids[0]}", account)
    await webui.request("GET", first + "/files/list", account, status=403)
    return ids[1]


async def seed_workspaces(webui, alice, bob):
    await asyncio.gather(
        webui.run(alice, "printf 'alice' > identity"),
        webui.run(bob, "test ! -e identity && printf 'bob' > identity"),
    )
    await webui.run(alice, BUILD_SOFTWARE)
    await webui.run(alice, "./build/check_arithmetic", exit_code=1)
    await webui.run(alice, FIX_SOFTWARE)
    await webui.run(alice, TEST_SOFTWARE)
    await webui.run(alice, CONFIGURE_DOCS)
    await webui.upload_docs(alice, "Arithmetic\n==========\n\nOriginal offline example.\n")
    await webui.run(alice, BUILD_DOCS)
    await webui.check_docs(alice, "Original offline example.")
    await webui.run(alice, "sleep 120", exit_code=None)


async def reopen_workspaces(webui, alice, bob):
    await asyncio.gather(
        webui.run(alice, 'test "$(cat identity)" = alice'),
        webui.run(bob, 'test "$(cat identity)" = bob && test ! -e build && test ! -e html'),
    )
    await webui.request(
        "POST", WORKSPACE + "/files/archive", bob, status=404, json={"paths": ["html/index.html"]},
    )
    await webui.check_docs(alice, "Original offline example.")
    await webui.run(alice, "cmake --build build --clean-first")
    await webui.run(alice, TEST_SOFTWARE)
    await webui.upload_docs(alice, "Arithmetic\n==========\n\nRevised offline example.\n")
    await webui.run(alice, BUILD_DOCS)
    await webui.check_docs(alice, "Revised offline example.")


async def main(service, phase, saved_chat=None):
    async with ClientSession(timeout=ClientTimeout(total=90), cookie_jar=DummyCookieJar()) as session:
        webui = WebUI(session, f"http://{service}:8080")
        if phase == "seed":
            accounts = await configure(webui, Path("/test.key").read_text().strip())
            await check_access(webui, accounts)
            await asyncio.gather(*(check_draft_directories(webui, account) for account in accounts[:2]))
            await check_picker_choices(webui, *accounts[:2])
            retained = await check_chat_directories(webui, *accounts[:2])
            await seed_workspaces(webui, *accounts[:2])
            print(json.dumps(retained))
        else:
            accounts = [
                await webui.request("POST", "/api/v1/auths/signin", json=credentials(name))
                for name in ("alice", "bob", "denied")
            ]
            await check_access(webui, accounts)
            await asyncio.gather(*(check_draft_directories(webui, account) for account in accounts[:2]))
            await reopen_workspaces(webui, *accounts[:2])
            alice = accounts[0]
            chat = f"{WORKSPACE}/chats/{saved_chat}"
            assert (await entries(webui, chat, alice, "/home/chat"))["marker.txt"] == "file"
            await webui.run(alice, 'test "$(cat /home/chat/marker.txt)" = retained', chat=saved_chat)
            assert await webui.request("DELETE", "/api/v1/chats/", alice)
            await webui.request("GET", chat + "/files/list", alice, status=403)
            assert (await entries(webui, WORKSPACE, alice))["shared-check"] == "directory"


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
