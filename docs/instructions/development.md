# Development

See [Architecture](architecture.md) for implemented workflows and deployment
design, and the [development record](../development-record.md) and
[verification record](../verification-record.md) for completed work.

## Workflow

### Fixes and feature implementation

These steps are necessary to run one development cycle. If any top step here is empty, then leave it out. If a sub-step does not nicely correlate, then report that to the author, but do not change anything.
1. Implement the fixes in [Problem fix decisions](/docs/instructions/development.md#problem-fix-decisions) if they correlate to items in [Problems](/docs/instructions/problems.md). Each item is a decision for the number-corresponding element in `docs/instructions/problems.md`. If for an item where no decision is given there, assume to implement your proposed fix:
2. Implement the [Core functionality](/docs/instructions/development.md#core-functionality).
3. Run the [After implementation](/docs/instructions/development.md#after-implementation).

### Verification

Then run the verification With the following prompt:

```txt
You are an independent auditor. Forget all you know about this repository and clean your cache.
1. Make yourself familiar with this repository.
2. Try to fill any gaps in the testing and verification that has been done.
3. Review this repository and propose resolutions for any problems arising.
```

## ToDo List

### Problem fix decisions

### Core functionality

### After implementation
