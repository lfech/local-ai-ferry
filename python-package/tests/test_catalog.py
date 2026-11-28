# test_catalog.py - Opt-in checks that public example specifiers resolve at HF.
# Registry metadata validates names/tags, not hardware fit or inference quality.
# Set RUN_CATALOG_TESTS=1 before discovery; default tests perform no network I/O.
"""Small optional registry-manifest requests; never downloads model weights."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import unittest
import urllib.error
import urllib.request

from loaf import cli
from tests import DATA, ROOT


def fetch_manifest(model):
    repository, separator, tag = model.removeprefix('hf.co/').partition(':')
    # HF's /v2 endpoint resolves quantization/filename tags; untagged models resolve as latest.
    url = f'https://hf.co/v2/{repository}/manifests/{tag if separator else "latest"}'
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
        'User-Agent': 'offline-ollama-project-tests',
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


# --- Public Ollama-compatible registry manifests, without fetching layer blobs ---
@unittest.skipUnless(os.environ.get('RUN_CATALOG_TESTS') == '1', 'Set RUN_CATALOG_TESTS=1 for public Hugging Face manifest checks.')
class CatalogTests(unittest.TestCase):
    def test_every_example_resolves_to_an_ollama_model_manifest(self):
        paths = [
            *sorted((DATA / 'config').glob('model*.ini')),
            *sorted((ROOT / 'config/model-examples').glob('*.ini')),
        ]
        # dict preserves first occurrence, so a shared model gets one request.
        models = dict.fromkeys(model.source for path in paths for model in cli.read_models(path))
        with ThreadPoolExecutor(max_workers=4) as workers:
            requests = {model: workers.submit(fetch_manifest, model) for model in models}
            for model, request in requests.items():
                with self.subTest(model=model):
                    try:
                        manifest = request.result()
                    except urllib.error.HTTPError as exc:
                        with exc:
                            self.fail(f'{model}: HTTP {exc.code}: {exc.read(400).decode(errors="replace")}')
                    self.assertEqual(manifest['schemaVersion'], 2)
                    self.assertTrue(manifest['layers'])
                    # Reject an implausible metadata-only manifest. Size alone cannot estimate VRAM.
                    self.assertTrue(any(layer.get('size', 0) > 1_000_000 for layer in manifest['layers']))
                    for layer in manifest['layers']:
                        self.assertRegex(layer['digest'], r'^sha256:[0-9a-f]{64}$')
