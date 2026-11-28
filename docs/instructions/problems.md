# Problems to resolve

### Problems to resolve (do not remove header)
> ℹ️ **INFO for Agents - do not implement**\
>The problems of the audits of commits 7263cf5 and c223ab7 are resolved as the
>[development record](/docs/development-record.md#2026-11-28-1135-cet--fixes-for-the-audit-of-commit-c223ab7)
>describes.
I fixed both test failures, filled the main verification gaps, and implemented the documentation layout you approved. The audit also turned up one data-loss problem and a few smaller ones; nothing has been committed.

>## Problems found and proposed fixes
>1. **High – `loaf setup --force` with no parts named loses data (confirmed).** In a deployment it replaced `config.ini`, so `namespace = prod` was gone and the next run would use different volumes. It also deleted a `docs/notes.txt` the user had written. In a checkout it would overwrite the tracked `config/config.ini`.
>   - Fix: apply `--force` only to parts named on the command line.
>   - Fix: replace `docs/` only when it carries loaf's export stamp.
>   - Fix: keep a copy of settings files it replaces.
>2. **Medium – `loaf restart` forgets CPU mode (from the code, not run live).** Without `--cpu`, a CPU-only host gets its services stopped and then fails to start them on GPU, leaving it down. Fix: keep the running mode, or refuse when it differs. This needs your decision.
>3. **Medium – a `.env` file in the project silently overrides settings (confirmed).** Example: `OLLAMA_IMAGE` swapped the image, and neither `status` nor `-v` showed it. Fix: either disable Compose's `.env` file, or list it in `status` and the `-v` output and add it to the architecture. Your call.
>4. **Medium – misleading `restart` message.** Without a profile and with Docker unreachable, it says "No serving profile of loaf runs" instead of reporting the Docker error.
>5. **Low:**
>   - The profile checks for `restart` and `stop PROFILE` run before the lock is taken, so a concurrent `loaf run` can change the answer.
>   - `restore --project-dir` with a typo creates that directory before failing.
>   - In the Verify workflow, one failing step skips the Debian package and wheel checks.
>   - Two comments in `cleanup.py` say "before 0.6.0" where "before 0.5.0" is meant.
>
>## Gaps in architecture.md
>I changed only where its links sit. It doesn't yet say:
>- that the shell environment and the project's `.env` override the settings files (item 3);
>- which parts `--force` applies to (item 1);
>- whether `restart` keeps CPU or GPU mode (item 2);
>- that the profile checks run outside the lock (item 5);
>- how the Debian package is upgraded.
>
>Still on disk: the four `loaf/*:latest` images, the two `python:*-slim-bookworm` images and the scratch clone. I can remove them, or copy the problem list into `problems.md` if you want it kept there.