# Contributing

Keep each change small and testable.
Open an issue before you change the public tool schemas.

## Local checks

Run these commands before you submit a change:

```powershell
python -m pytest
python -m compileall src tests
```

Do not add a fallback that hides a missing provider or executable.
Return a clear error instead.

Keep the MCP and CLI adapters thin.
Put shared behavior in the manager or schemas.

Do not include API keys, job output, or local state in a commit.
