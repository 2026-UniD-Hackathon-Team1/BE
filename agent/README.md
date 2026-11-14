# Galtalka local agent

The Galtalka local agent connects a user's computer to the Galtalka service.
It scans only a project directory that the user explicitly approves and runs
comparison jobs requested by the service.

## Requirements

- Python 3.11 or newer
- uv
- Codex CLI installed and signed in for comparison jobs
- Access to the Galtalka server

## Install

```sh
uv tool install galtalka-agent
```

## Connect a project

Use a pairing code from Galtalka and choose the project directory to approve:

```sh
galtalka connect PAIRING-CODE --server https://YOUR-GALTALKA-SERVER --root /path/to/project
```

Start the agent:

```sh
galtalka run
```

Press Ctrl+C to stop it. The agent stores its connection settings under
`~/.galtalka/` and does not start automatically.

To see the installed version:

```sh
galtalka --version
```
