# Installation and platform troubleshooting

[Back to Comeback](../README.md) · [Installation](installation.md) · [Workflows](workflows.md) · [Base](base.md) · [Security](security.md) · [Validation](validation.md)

Use the [README quick path](../README.md#quick-start) first. This page preserves the detailed setup, ownership and recovery guidance for both supported harnesses.

## Prerequisites

- Git and a Git repository with at least one commit.
- An installed and authenticated coding agent. Historical authenticated checks cover Codex CLI `0.152.1`, `0.153.1`, and `0.153.3`, plus Windows Claude Code `2.1.263`; see [the versioned validation records](validation.md#versioned-results) for their exact releases and limits. The September 10 recorded release also shows Codex `0.153.4` with Comeback `3db9acc`. This is not a blanket compatibility claim for later releases. The externally reported `0.150.0-alpha.12.2` Windows Codex build is not supported. Validate activation in your own installation using the agent-specific instructions below; Claude doctor alone does not prove real lifecycle dispatch.
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/). It can install the required Python automatically.
- Git Bash only when using Claude Code on Windows.

`comeback init` requires a normal Git working tree with at least one commit. It refuses non-Git directories, repositories without `HEAD`, and linked Git worktrees before writing installation files. Install in a normal clone until project-hook discovery in linked worktrees is independently proven.

## Install on Windows PowerShell

Install `uv`, close and reopen PowerShell so `uv` is on `PATH`, then install Comeback with Python 3.13:

```powershell
winget install --id=astral-sh.uv -e --source winget --scope user
# Close this PowerShell window, open a new one, then continue.
uv python install 3.13
$env:UV_LINK_MODE = "copy"
uv tool install --python 3.13 "git+https://github.com/qdeeworld/comeback.git"
uv tool update-shell
# Close and reopen PowerShell again so `comeback` is on PATH.
cd C:\path\to\your-repository
comeback init --agent codex
```

If `winget` is unavailable, use one of the other Windows installation methods in the official `uv` documentation linked above. Python, `pipx`, and `python3-venv` do not need to be installed separately when `uv` manages Python.

Select copy mode **before the first install** on Windows. This avoids hardlink failures such as `ERROR_CLOUD_FILE_INCOMPATIBLE_HARDLINKS` or OS error 396, including failures in uv's cache outside OneDrive:

```powershell
$env:UV_LINK_MODE = "copy"
```

If an earlier development install already failed, rerunning it can leave missing package metadata. Close processes using that environment, rename only the disposable `.uvenv` directory to an unused backup name, then recreate `.uvenv` and repeat the install with copy mode enabled. Preserve your repository and owner keystore. For a failed `uv tool install`, retry with `--force --link-mode copy`; the tool environment is separate from a development clone's `.uvenv`.

### Windows application-control compatibility

Windows hooks and signed capability commands use the same installation environment's `python.exe -I -m comeback.hook` / `comeback.cli`, not the per-install console-script `.exe` stubs. The interpreter may be alongside the launcher in a virtual environment or one directory above `Scripts` in a base Python installation. `-I` isolates imports from repository files and Python environment variables. Re-run `init` and review/retrust the changed hooks after upgrading; existing hook files are not silently updated.

Initialization and the authenticated Claude gates first probe the environment interpreter without starting an agent. A blocked or broken interpreter stops the workflow. This does **not** certify Smart App Control compatibility: Windows may also block Python or dependencies. Do not disable Windows security or keep retrying. Consult Code Integrity logs and use an administrator-approved Python distribution/environment. A signed interpreter is a candidate installation route, not a guarantee that all dependencies are accepted.

If the `comeback.exe` console command itself is blocked, the same installed environment can be invoked explicitly as `PATH_TO_ENV\Scripts\python.exe -I -m comeback.cli --help` (then `init`), provided that interpreter is allowed. Do not substitute a different Python from PATH or assume the uv-managed interpreter is signed. The preflight reports launch failure; it does not label every permission error as Smart App Control without OS evidence.

## Install on macOS or Linux

Install `uv` using its official installer or package-manager instructions, then run:

```bash
uv python install 3.13
uv tool install --python 3.13 "git+https://github.com/qdeeworld/comeback.git"
uv tool update-shell
# Close and reopen the terminal so `comeback` is on PATH.
cd /path/to/your-repository
comeback init --agent codex
```

`comeback init` writes two kinds of files:

- Review and commit the portable repository files: `.comeback-repository.json`, `.agents/skills/release-safety/SKILL.md`, and the `.gitignore` change for `.comeback/`.
- Review but do not commit the selected machine-local launcher: `.codex/hooks.json` for `--agent codex`, `.claude/settings.json` for `--agent claude`, or both files for `--agent both`. They contain absolute paths to this clone's installed `comeback-hook` and `comeback` executables. When Comeback creates an untracked launcher file, it adds that file to this clone's `.git/info/exclude` without changing the shared `.gitignore`.

It merges unrelated hook entries and refuses to overwrite an unrelated Skill. Before running the doctor, stage only the portable files, review their complete staged diff, and commit them:

```bash
git status --short
git add .comeback-repository.json .agents/skills/release-safety/SKILL.md .gitignore
git diff --cached -- .comeback-repository.json .agents/skills/release-safety/SKILL.md .gitignore
git commit -m "Install Comeback repository policy"
```

`comeback init` adds the `.comeback/` ignore rule but does not create that runtime directory. The first memory or owner write creates it. If you need the directory earlier for an intervention record or a disposable local release target, create it explicitly with `mkdir -p .comeback` on macOS/Linux or `New-Item -ItemType Directory -Force .comeback | Out-Null` in PowerShell.

The committed `.comeback-repository.json` is the stable repository identity. Comeback deliberately refuses activation when that anchor is missing, uncommitted, or differs from the copy at `HEAD`.

Comeback merges unrelated entries in an existing **untracked** hook configuration. It refuses a requested `.codex/hooks.json` or `.claude/settings.json` that Git already tracks, because the current launcher contains machine- and clone-absolute executable paths. The refusal happens before Comeback writes installation files; it does not silently untrack, move, or rewrite the tracked configuration.

For `--agent both`, Comeback validates both existing hook configurations and checks that any existing `release-safety` Skill belongs to Comeback before its first write. Invalid JSON or a conflicting Skill therefore refuses the whole installation instead of leaving only one agent partially configured.

If you deliberately choose to convert a tracked hook configuration into a clone-local file, preserve and review its contents first, run `git rm --cached -- PATH_TO_HOOK_FILE`, commit that deliberate repository-policy change, confirm the file remains in the working tree, and rerun `comeback init`. If the hook configuration must remain portable and tracked, leave it tracked and do not install the current Comeback integration for that agent in this clone. Run `comeback init` again in every new clone or after moving or reinstalling the uv tool; a generated hook file copied from another machine is not portable.

## Activate and prove Codex hooks

Installation does not prove activation. Codex ignores project-local hook configuration until both the repository and the exact hook commands are trusted.

1. Open interactive `codex` in the repository.
2. Choose **Yes, continue** when Codex asks whether to trust the directory.
3. Run `/hooks`.
4. Review the Comeback commands and choose **Trust all and continue**.
5. Exit Codex completely.
6. Run:

```text
comeback doctor --agent codex
```

The doctor consumes two authenticated Codex turns in two genuinely fresh, ephemeral processes and does not bypass hook trust. Both use `workspace-write`, the working-session sandbox. The first must create exactly one Sibyl run through the real `UserPromptSubmit` hook and execute one read-only Git `rev-parse --show-toplevel` command successfully in that sandbox. The probe pins an absolute installed Git executable, rather than trusting bare `git` from PATH; it also validates shell wrappers and refuses unexpected tool activity. Comeback checks the actual tool result and repository path, not the model's summary. The second uses a separate isolated Sibyl database and a disposable file under the ignored `.comeback/` directory; it must recall a seeded intervention, emit one exact `PreToolUse` denial, and leave the disposable release marker absent. `PASS` proves activation, sandbox access through that pinned Git and a real pre-execution block—not checkpoint execution, destination access, the safety of arbitrary PATH entries or the full owner-approved release journey. Both diagnostic databases are temporary and deliberately separate from `.comeback/memory.db`, so `comeback status` will still report `NO_WORKING_AGENT_RUNS` immediately after a passing doctor. That is expected until the next genuinely fresh working agent session writes the first real run.

Before executing a configured hook, doctor requires the current installation's exact canonical launcher: `HOOK_LAUNCHER_UNTRUSTED` refuses changed launchers, and `CAPABILITY_EXECUTABLE_MISSING` refuses a missing current launcher rather than finding a different installation on PATH. Restore the intended installation if necessary, run `comeback init` from it, review and trust the updated hooks, then rerun doctor.

### Windows Git ownership

`GIT_OWNERSHIP_UNSAFE` means the hook activated but Git refused the repository inside the agent sandbox. `SANDBOX_GIT_NOT_PROVEN` means the doctor did not obtain the unique, successful Git tool result it requires. Neither result authorizes a release. Stop before owner setup, capture or approval; do not repeatedly reinstall Comeback.

`TRUSTED_GIT_NOT_FOUND` means no supported standard Git installation was found. The Windows probe currently uses Git for Windows under the operating system's Program Files known folder (`Git/cmd` or `Git/bin`), not the mutable `ProgramFiles` environment variable; macOS/Linux uses `/usr/bin/git`, `/opt/homebrew/bin/git` or `/usr/local/bin/git`. Custom Git installations are not yet supported by this diagnostic. A successful probe does not validate a different Git executable used by later commands.

The activation probe removes inherited `GIT_*` variables from its child environment so temporary configuration injection or `GIT_DIR`/`GIT_WORK_TREE` overrides cannot substitute a different repository or trust exception. Normal persistent Git configuration still applies. The operator's environment and Git configuration are not modified; a workflow that depends on custom Git environment overrides needs separate verification.

The [preferred native Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox) uses a dedicated lower-privilege account. Git can therefore see a different user from the owner who created the repository. A successful Git command in your own terminal does not establish that Git works in the agent's sandbox. Review the repository owner, executing account and enterprise sandbox policy with the owner or administrator before changing trust. A new folder alone may preserve the same cross-account mismatch.

For an intentionally shared, reviewed repository, Git supports an exact-path `safe.directory` exception in [protected configuration](https://git-scm.com/docs/git-config#Documentation/git-config.txt-safedirectory). Such an exception grants trust to repository configuration and hooks, so it must be an explicit owner/administrator decision and visible to the account that actually runs Git. Comeback does not add it automatically. Do not set `safe.directory=*`, broadly change ownership/ACLs or disable the sandbox to make a test pass. A one-command exception on an agent's direct Git call does not establish that Comeback's own Git subprocesses or a local bare destination can work.

After an authorized ownership/trust correction, start a fresh agent process and rerun the doctor. Establish the same stable execution environment before checkpointing; a later Git configuration or environment change invalidates the checkpoint fingerprint and requires a new check and approval. Keep the supervised agent session open while you approve in a separate native terminal, then let that same agent session invoke its release capability. Do not move the release into a different process environment merely because approval has been signed.

Codex's per-launch `tmp/arg0/codex-arg0*` PATH entries under its default or configured `CODEX_HOME` are excluded from **both** protected-command execution and fingerprinting. A normal CLI restart can therefore resume the same approved session without invalidation solely from those temporary launcher paths. Comeback does not ignore the rest of PATH: changes to ordinary search paths, executable files, captured environment, repository state, or Git configuration still require a new checkpoint and approval. An exhausted PATH fails closed. Commands that depend on the excluded agent shims are not supported as PATH-resolved capabilities. Upgrading to this execution-environment policy invalidates older checkpoint receipts; rerun the check and owner approval once after upgrading. This does not extend the 15-minute receipt lifetime or transfer approval to a different session.

Never invoke `comeback-hook` yourself. It is a lifecycle protocol endpoint that expects structured JSON from the agent. `NO_WORKING_AGENT_RUNS` means only that the primary store has no real working-session run; it does not erase a passing doctor result because the doctor uses isolated stores. If the doctor has not passed, run it and fix the reported trust or installation issue. After `PASS`, start a genuinely fresh working agent process, then use `comeback status` to obtain that real session ID.

If the Stop hook reports that no Sibyl supervision run exists for this session, absence is not permission to finish: the run may have been deleted or recall may never have activated. Start a genuinely fresh session; if recall still does not activate, run doctor before continuing rather than bypassing the hook.

See the [official Codex hooks documentation](https://developers.openai.com/codex/hooks) for the project-layer and hook-review model.

## Activate Claude Code hooks

Run `comeback init --agent claude`, review the generated `.claude/settings.json`, open Claude Code in the repository, and approve only the exact Comeback hooks you reviewed. Then exit Claude Code and run:

```text
comeback doctor --agent claude
```

Claude doctor intentionally returns `PARTIAL`: it proves the installed Git Bash launcher, lifecycle JSON, and Sibyl write, but it does not claim that a real Claude Code process dispatched the hook. The authenticated `scripts/run_cross_agent_gate.py` and `scripts/run_claude_unlock_gate.py` checks are separate and are required before making the cross-agent claim. Consequently, `comeback doctor --agent both` also remains `PARTIAL` when Codex passes and only the Claude launcher has been proven.

## Optional credential-free Windows release target

For a disposable local test that does not require a GitHub account or credentials, create an absolute bare-repository target inside the ignored runtime directory:

```powershell
New-Item -ItemType Directory -Force .comeback | Out-Null
$remote = Join-Path (Get-Location) ".comeback\local-release.git"
git init --bare $remote
$remote = (Resolve-Path $remote).Path.Replace('\', '/')
$remote
```

Use the printed absolute path directly wherever the [workflow examples](workflows.md#record-the-first-intervention) use an HTTPS URL. For example, construct the signed release arguments with `$release = @("git", "push", $remote, "HEAD:refs/heads/approved") | ConvertTo-Json -Compress`. Do not use the literal name `$remote` inside a coding-agent prompt; it is only a variable in the operator's current PowerShell session. Verify that a denied release left no ref, and that an approved release created one, with `git --git-dir=$remote rev-parse --verify refs/heads/approved`.
