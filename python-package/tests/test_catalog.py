# test_catalog.py - Opt-in checks that public example specifiers resolve at HF.
# Registry metadata validates names/tags, not hardware fit or inference quality.
# Set RUN_CATALOG_TESTS=1 before discovery; default tests perform no network I/O.
"""Small optional registry-manifest requests; never downloads model weights."""
import json
import os
import unittest
import urllib.error
import urllib.request

from loaf import cli
from tests import DATA, ROOT


# --- Public Ollama-compatible registry manifests, without fetching layer blobs ---
@unittest.skipUnless(os.environ.get('RUN_CATALOG_TESTS') == '1', 'Set RUN_CATALOG_TESTS=1 for public Hugging Face manifest checks.')
class CatalogTests(unittest.TestCase):
    def test_every_example_resolves_to_an_ollama_model_manifest(self):
        paths = [
            *sorted((DATA / 'config').glob('model*.ini')),
            *sorted((ROOT / 'config/model-examples').glob('*.ini')),
        ]
        # dict preserves first occurrence, so a shared model gets one request.
        models = list(dict.fromkeys(model.source for path in paths for model in cli.read_models(path)))
        for model in models:
            with self.subTest(model=model):
                repository, separator, tag = model.removeprefix('hf.co/').partition(':')
                # HF's /v2 endpoint resolves Ollama quantization/filename tags; this
                # is not a raw GGUF download URL. Untagged models resolve as latest.
                url = f'https://hf.co/v2/{repository}/manifests/{tag if separator else "latest"}'
                request = urllib.request.Request(url, headers={
                    'Accept': 'application/vnd.docker.distribution.manifest.v2+json',
                    'User-Agent': 'offline-ollama-project-tests',
                })
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        manifest = json.load(response)
                except urllib.error.HTTPError as exc:
                    self.fail(f'{model}: HTTP {exc.code}: {exc.read(400).decode(errors="replace")}')
                self.assertEqual(manifest['schemaVersion'], 2)
                self.assertTrue(manifest['layers'])
                # Reject an implausible metadata-only manifest; size is not a VRAM
                # estimate, which must also account for KV cache and runtime buffers.
                self.assertTrue(any(layer.get('size',0)>1_000_000 for layer in manifest['layers']))
                for layer in manifest['layers']:
                    self.assertRegex(layer['digest'], r'^sha256:[0-9a-f]{64}$')
