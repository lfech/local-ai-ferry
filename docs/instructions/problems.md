# Problems to resolve

### Problems to resolve
> ℹ️ **INFO for Agents - do not implement**\
>I audited the repository at commit 0c47cf0 and found 23 problems. Four of them can lose or break data during a backup or restore. The test suite misses most of the problems, so I tested functionality directly with dry runs, real backups and restores in scratch projects, and Docker runs in a throwaway deployment name (`auditNNN`). Your running deployment was not touched.
>
>**Test results**
>- **Docker-free suite:** 306 tests, all passed, 48 skipped.
>- **Docker tests** (`RUN_DOCKER_TESTS=1`): 35 ran, 34 passed, 1 failed (#21).
>- **Not run:** the GPU and CPU model tests, the Open Terminal Docker tests, the Debian package checks, and anything that stops your >running deployment.
>
>The only file I changed is [docs/verification-record.md](docs/verification-record.md), where I added the audit entry as `AGENTS.md` requires.
>
>## High: backups and restores that fail or break things
>
>1. **A backup can pass all checks and still fail to restore.** If any file in `config/` is a symlink, for example `models.ini -> model-examples/models.8gb.ini`, backup stores the link itself ([transfer.py:478](python-package/src/loaf/transfer.py#L478)). It also stores links that point outside `config/`. Restore then rejects the archive with `Invalid file in config.tar` ([transfer.py:537]>(python-package/src/loaf/transfer.py#L537)), and `backup --verify` reports nothing wrong. I reproduced this end to end.
>   - **Fix:** at backup time, store the target file for links that stay inside `config/` and refuse the rest. Make `--verify` run the same checks as restore.
>2. **Restore changes files before it fails, which goes against the design spec.** Whether the WebUI volume is empty is only checked inside the Docker step. By then the source, config, installers, images and models are already in place ([transfer.py:1376]>(python-package/src/loaf/transfer.py#L1376)). I confirmed it: `config.ini` was overwritten, then the restore stopped with `Restore requires empty volumes`. The error names `/transfer/webui` instead of the actual volume. The terminal user-volume label check >([transfer.py:1390](python-package/src/loaf/transfer.py#L1390)) also runs only after the first change.
>   - **Fix:** check that each target volume is missing or empty, and check the labels, before the first change.
>3. **After a default backup and restore, the WebUI terminal stops working.** Since the 17:41 change, Open WebUI stores the bearer key in its database. The default backup leaves the key out (`include_in_backup = false`), and setup on the destination creates a new one, so the stored connection gets rejected.
>   - **Fix (needs your decision, because the spec sets the default):** either include the key whenever `webui` is backed up with the terminal enabled, or let `start-webui.py` refresh the key of its own `loaf-terminal` connection on each start.
>4. **Idle chat containers may never stop (likely, not yet confirmed).** `idle()` ([provisioner.py:568](python-package/src/loaf/data/docker/terminal-server/provisioner.py#L568)) treats any container with more than two processes as busy. Open Terminal keeps terminal shells running after the browser disconnects, and one of your live chat containers has such a shell. Once 4 chats have opened a terminal, new chats would get "All terminal workspace slots are in use".
>   - **Fix:** close disconnected sessions through `/api/terminals` before parking a container.
>
>## Medium
>5. **Backup from a source checkout always needs internet.** It re-downloads the bundled Python runtime every time, with no cache >([build-release.sh:64](scripts/build-release.sh#L64)), so it cannot run on an offline machine. **Fix:** cache the checked downloads in `install-cache/` and include them in the `installers` type.
>6. **Running setup as root makes the checkout unusable.** This is the state of this checkout now: `.venv` is owned by root and points to root's own Python, so `./loaf` fails with "bad interpreter: Permission denied". Rerunning setup as your user cannot move the old `.venv` out of the way. **Fix:** refuse source setup as root when the checkout belongs to someone else; APT steps already use sudo.
>7. **The "Next:" line after restore can suggest a command that fails.** It suggests `offline` even when the project has no `docker-project` link or the terminal key is missing. Running `offline` then gives "No compose.yml" or "key file is missing". **Fix:** create the link during restore, or print `setup` as the next step.
>8. **With a pip-installed loaf, setup in an empty directory gives a circular error:** "No compose.yml … Run loaf setup" ([cli.py:1128](python-package/src/loaf/cli.py#L1128)). **Fix:** do the standalone setup (config, link, key) when the project is not a source checkout.
>9. **`backup --verify` copies `images.tar` into `/tmp`** ([transfer.py:1280](python-package/src/loaf/transfer.py#L1280)). On Debian 13 >`/tmp` is in RAM (7.6 GB here), so larger image sets fail to verify. **Fix:** read the image list from the stream, or stage under the >project directory.
>
>## Low: confusing CLI behavior (the top goal in `AGENTS.md`)
>10. **`restore --import --wait-timeout`:** `0` silently becomes 300 seconds, and a negative value crashes with a Python error after the restore has finished ([cli.py:1342](python-package/src/loaf/cli.py#L1342)). **Fix:** reject non-positive values when parsing ([cli.>py:555](python-package/src/loaf/cli.py#L555)).
>11. **`offline --models-file` is accepted and then ignored** ([cli.py:1078](python-package/src/loaf/cli.py#L1078)). **Fix:** stop accepting it for `offline`.
>12. **`stop --remove-containers -n` is accepted silently, and the last flag wins.** **Fix:** make the two options mutually exclusive.
>13. **Some dry runs hide errors that a real run would hit:** `restore --dry-run` with no backup, or with types the archive doesn't contain, and `backup source --dry-run` outside a checkout.
>14. **Option names (needs your decision, because the spec names them):**
>    - `--version` picks a deployment name, so `loaf --version` fails with "expected one argument". Possible new name: `--deployment`.
>    - `-n` keeps containers, while most tools use `-n` for a dry run. Possible new name: `--keep-containers`.
>    - `--no-terminal-server` only skips the terminal image in a backup. Possible new name: `--no-terminal-image`.
>15. **`loaf docker ps` lists chat containers as the Compose service `terminal-server`.** They inherit Compose labels from the image they are created from. **Fix:** check the `loaf.terminal.*` labels first in [resources.py:79](python-package/src/loaf/resources.py#L79).
>16. **Restore deletes all comments from `config.ini`** when it has to write in the backup's deployment version or the system key path ([transfer.py:589](python-package/src/loaf/transfer.py#L589)). A file generated by setup went from 104 lines to 17. **Fix:** edit only the one line instead of rewriting the file.
>17. **systemd units:**
>    - The unit printed by `loaf integration systemd` has no `After=`/`Requires=docker.service` ([integrations.py:39](python-package/src/loaf/integrations.py#L39)); the packaged unit has them.
>    - Both units stop after `TimeoutStopSec=30`, but the terminal server is allowed 45 s to shut down. **Fix:** raise it to about 120 s.
>18. **`clean --full` deletes `install-cache/` without asking** ([cleanup.py:17](python-package/src/loaf/cleanup.py#L17)). On an offline host those installers cannot be downloaded again.
>19. **`offline` with `[terminal_server] network = connected` gives no warning** that chat containers driven by the model have outbound >internet access.
>20. **The Continue integration marks every model as able to read images** and gives each one the embed and rerank roles.
>
>## Tests and docs
>21. **A Docker test is out of date since the 16:50 volume renaming.** [test_docker.py:358](python-package/tests/test_docker.py#L358) fails and leaves four empty volumes behind (I removed them). **Fix:** pass `volumes=` to `volume_command` instead of patching `VOLUMES`.
>22. **The model examples don't match the docs:**
>    - [docs/models.md:75](docs/models.md#L75) uses `config/model.6gb.ini`, which doesn't exist.
>    - The packaged `data/config/model.6gb.ini` is a misnamed duplicate.
>    - The README and `models.md` list Ministral in the 8 GB and 16 GB profiles, but those files no longer include it.
>    - The new `_chat`/`_code` files aren't documented.
>    - The example files aren't packaged, so `models_file = model-examples/…` (shown in `debian.md` and `configuration.md`) fails outside a checkout.
>23. **Minor drift:** the working `config/config.ini` lacks `webui_access`, and the README badges still show 212 tests.
>
>I can implement the fixes that don't touch the spec (1, 2, 5–13, 15–18, 21–23). Items 3 and 14, and possibly 4, need your decision first.