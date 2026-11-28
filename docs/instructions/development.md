# Development

See [Architecture](architecture.md) for implemented workflows and deployment
design, and the [development record](../development-record.md) and
[verification record](../verification-record.md) for completed work.

## To Do

### Core functionality (do not remove header)
- The chat inside openwebui displays one tool call with multiple UI elements, especially when between the start of a command and it's exit states reaping was some thinking. To the "Ran command xyz" tool for one execution. One UI element for the start of the command where confusingly no stdout/stderr is show (as the command has not finished yet) and one for the exit status and stdout stderr retrieval. 
Can you make that it updates the first call UI output on successfull retrieval? Maybe make the UI element wait for the finishing of the command. Can we configure this as normal admin devs would do?

### Problem Fixes (do not remove header)
1. With --force, require explicit mentioning of parts.
   move settings to config.old, but overwrite docs.
2. Make it remember --cpu
3. Disable .env by default, but make it reactivatable in config with "docker_env_enable = false" the default.
4. Report the docker error in a nice way.
5. + Whatever is more safe.
   + Check first, create directory afterwards.
   + Whatever is safer but does not interrupt workflow.
   + Adapt to correct version.