# Prompts

Agent system prompts live here as versioned Markdown files, one file per
version: `<name>.v<N>.md` (for example `quote_review.v1.md`, `quote_review.v2.md`).

An `AgentDefinition` names its prompt file and version explicitly:

```python
AgentDefinition(
    name="quote_review",
    prompt_file="quote_review.v2.md",
    prompt_version=2,
    ...
)
```

The definition refuses a file name whose `vN` does not match `prompt_version`.

## Rules

- **A versioned file is immutable once merged.** To change a prompt, add
  `<name>.v<N+1>.md` and point the agent at it in the same PR. Old versions stay
  so past runs (and eval baselines) can be reproduced.
- **Evals run through `run()`**, the same entry point production uses, against
  a specific prompt version. A prompt change ships with its eval results.
- Keep the final-output instruction out of the prompt: the runner appends the
  `submit_output` instruction itself.
- No secrets, customer data, or tenant-specific content in prompt files.
  Per-run data arrives as the run input.

`example.v1.md` is a tiny prompt used by the runner tests.
