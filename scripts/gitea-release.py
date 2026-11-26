"""Attach built distributions to the Gitea draft for an existing tag."""

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # The authorization header belongs to the configured Gitea API only.
        return None


def publish(tag: str, directory: Path) -> None:
    wheel, = directory.glob("*.whl")
    package, = directory.glob("*.deb")
    source, = directory.glob("*.tar.gz")
    assets = {path.name: path for path in (
        directory / "loaf", directory / "LICENSE.txt", wheel, package, source, directory / "SHA256SUMS",
    )}
    for path in assets.values():
        if not path.is_file():
            raise FileNotFoundError(f"Missing release asset: {path}")
    api_url = os.environ["GITHUB_API_URL"].rstrip("/")
    repository = quote(os.environ["GITHUB_REPOSITORY"], safe="/")
    token = os.environ["GITEA_TOKEN"]
    if not token:
        raise ValueError("The runner did not provide GITEA_TOKEN.")
    opener = build_opener(NoRedirect())
    releases = f"{api_url}/repos/{repository}/releases"

    def request(method, url, data=None, content_type="application/json"):
        headers = {"Authorization": f"token {token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type
        with opener.open(Request(url, data=data, headers=headers, method=method), timeout=60) as response:
            return json.load(response) if response.status != 204 else None

    try:
        release = request("GET", f"{releases}/tags/{quote(tag, safe='')}")
    except HTTPError as error:
        if error.code != 404:
            raise
        notes = subprocess.check_output(
            ["git", "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}"], text=True,
        ).strip()
        commit = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], text=True,
        ).strip()
        release = request("POST", releases, json.dumps({
            "tag_name": tag, "name": tag, "body": notes, "draft": True, "target_commitish": commit,
        }).encode())
    if not release["draft"]:
        print(f"Release {tag} is already published.")
        return

    endpoint = f"{releases}/{int(release['id'])}/assets"
    for attachment in release["assets"] or ():
        if attachment["name"] in assets:
            request("DELETE", f"{endpoint}/{int(attachment['id'])}")
    for name, path in assets.items():
        boundary = uuid4().hex
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="attachment"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        body = header.encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request("POST", endpoint, body, f"multipart/form-data; boundary={boundary}")
        print(f"Attached {name} to draft {tag}.")


if __name__ == "__main__":
    try:
        publish(sys.argv[1], Path(sys.argv[2]))
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"Gitea draft release failed: {error}")
