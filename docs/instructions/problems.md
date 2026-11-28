# Problems to resolve

### Problems to resolve (do not remove header)
> ℹ️ **INFO for Agents - do not implement**\
>The problems of the audits of commits 7263cf5, c223ab7 and efc1df5 are resolved as the
>[development record](/docs/development-record.md#2026-11-28-1455-cet--fixes-for-the-audit-of-commit-efc1df5-one-tool-call-per-command-and-small-models)
>describes.

>## Audit of the working tree after the fixes for commit efc1df5
>The [development record](/docs/development-record.md#2026-11-28-1530-cet--audit-of-the-fixes-and-core-tasks)
>lists what the audit fixed and checked. These problems remain, each with a proposed resolution:
>1. **Medium – the coding models of the example lists cannot drive the terminal (confirmed).** Qwen2.5-Coder 7B,
>   the model of `models.8gb_code.ini` and `models.8gb.ini`, wrote all its tool calls as JSON text, which Ollama
>   0.40.18 does not turn into tool calls, so Open WebUI never runs them. Qwen2.5 7B Instruct and Qwen3 4B called
>   the tools every time. The guides now say so.
>   - Fix: replace the Coder entries with a tool-calling model, or keep them for editor completion and name that
>     purpose in the lists and `models.md`. The 14B Coder of the 16 GB lists was not checked. Your call.
>2. **Low – the GPU tests cannot run from the checkout.** `.test-artifacts/` is owned by root since 13:02, before
>   this session, so `test_docker_gpu` fails in `setUpClass` with a permission error and the GPU badge kept its
>   11:22 run; the badge run at 15:06 is recorded as failed. The six tests passed from a copy of the working tree.
>   - Fix: remove the directory as root (`sudo rm -r .test-artifacts`), then rerun `loaf test --badges` with
>     `RUN_GPU_TESTS=1`.
>3. **Low – other commands still show Docker's raw error.** With the daemon unreachable, `run`, `build`, `stop`
>   and `stop PROFILE` without the terminal server print Compose's message and `docker failed (exit 1)`; only
>   `restart` and `stop PROFILE` with the terminal server use the new message with its pointer.
>   - Fix: give a Compose failure that says "Cannot connect to the Docker daemon" the same pointer to
>     `docker version` and the Docker access guide.
>4. **Low – the new system prompt makes Ministral 3B Reasoning call a tool for one of six plain questions.**
>   - Fix: accept it, or shorten the prompt's list of requests that call for the terminal. Your call.
>5. **Proposal – fewer terminal tools for small models.** Open WebUI offers the model 14 of the terminal server's
>   16 tools, two more with a terminal tab open; the four search tools repeat what `run_command` does with `grep`
>   and `find`. Studies of tool selection, and Granite
>   4.0 H 350M answering only 17 of 24 terminal requests with a call, suggest that small models choose better
>   among fewer tools.
>   - Fix: a `[terminal_server]` setting that publishes only `run_command`, `get_process_status` and the file
>     tools. It removes nothing that `run_command` cannot do. Your call.
