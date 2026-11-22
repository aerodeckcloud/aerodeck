# AeroDeck agent rules

## Change workflow (mandatory)
- Never commit or push to `main`. Always work on a feature branch.
- Open a PR for every change. Link its issue (`Closes #N`) and use the repo's label conventions (`type: story|task|bug|epic`, `status: *`).
- Wait for the AI review (Gemini) and CI checks. Fix findings with new commits on the same branch before asking to merge.
- Merge only after the review is approved. If approval needs the user, ask. Never self-approve or force a merge.
- If a push prints "Bypassed rule violations" or any branch-protection notice, stop and tell the user. Do not repeat it.
- Do not use admin or bypass privileges to merge or push.

## Operational notes
- `gh` and `git push` need network, so run them with the sandbox bypassed. Keep all other work sandboxed.
- Never print or commit secrets (DigitalOcean token, DB passwords, private keys).
- The DigitalOcean token cannot read database credentials. Do not try to work around that. Ask the owner.
- The control and edge Droplets accept SSH only with the `aerodeck-dev` key. On auth failure, stop and report instead of changing Droplet access paths.
- The self-hosted Windows runner needs Git Bash first on `PATH` and BOM-free `GITHUB_PATH` writes (`[System.IO.File]::AppendAllLines`).
- In `run_command` PowerShell, avoid `$_` inside `powershell -Command "..."` because quoting mangles it. Write logs to a file and use `Select-String`.
