# Installation and first use

This is an experimental source distribution for local Codex use. It is not a hosted animation service or a plugin-directory release.

## Prerequisites

- A local Codex environment that can read the skill, execute its scripts, and inspect images.
- A real Python 3.12 interpreter. The publication checks used Python 3.12.14.
- Pillow from [requirements.txt](../requirements.txt).
- Node.js 22 or newer for the provider transport; publication checks used Node.js 24.19.0.
- External FFmpeg for video work.
- Your own FAL account and credit for provider-backed generation.

Only Windows publication checks are recorded. Other environments still need validation.

## Get the source

For code inspection and contribution:

```powershell
git clone https://github.com/brayan-carlo/tower-sprite-pipeline.git
Set-Location tower-sprite-pipeline
```

Review [SKILL.md](../SKILL.md), the scripts, and the workflow references before enabling execution.

## Load as a local skill

Current official Codex documentation lists the user skill directory as `$HOME/.agents/skills` and repository-scoped directories as `.agents/skills`. A skill is a directory containing `SKILL.md`. See [OpenAI's skill documentation](https://learn.chatgpt.com/docs/build-skills).

For a new Windows user-level installation, from a PowerShell session:

```powershell
$skillDirectory = Join-Path $env:USERPROFILE '.agents\skills\tower-sprite-pipeline'
if (Test-Path -LiteralPath $skillDirectory) {
    throw 'An installation already exists. Review it before updating.'
}
New-Item -ItemType Directory -Force -Path (Split-Path $skillDirectory) | Out-Null
git clone https://github.com/brayan-carlo/tower-sprite-pipeline.git $skillDirectory
```

If you already have a development copy or another installed skill with the same name, keep the new clone outside your skill-discovery directories until you deliberately choose which copy to use. Do not overwrite active runs or local modifications.

You can also ask Codex's `$skill-installer` to install the skill from this repository. If a newly installed skill is not visible, restart Codex. Those discovery behaviors follow the official documentation; a fresh Codex UI installation of this publication has not yet been tested.

## Python environment outside the skill

From the cloned repository root, with Python 3.12 available as `python`:

```powershell
$venvDirectory = Join-Path $env:USERPROFILE '.venvs\tower-sprite-pipeline'
python -m venv $venvDirectory
$pythonExe = Join-Path $venvDirectory 'Scripts\python.exe'
& $pythonExe -m pip install -r requirements.txt
& $pythonExe -B scripts/generic_sprite_workflow.py --help
node --version
ffmpeg -version
```

Do not reuse an unrelated virtual environment or install dependencies inside an active skill directory. If FFmpeg is not on PATH, point `TOWER_SPRITE_FFMPEG_EXE` to its actual executable. The runtime also accepts explicit FFmpeg paths on supported commands.

The agent can discover supported bundled runtimes instead. Never substitute an unverified interpreter path.

## FAL credential

Create the environment variable `FAL_KEY` in the environment used by the agent. On Windows, the workflow also checks the User environment after the current Process environment.

Use the Windows “Edit environment variables for your account” interface to enter the value. Never put the value in this repository, an issue, a command argument, or a chat message. Reopen the application when needed for environment changes to take effect.

The repository contains no account credit. Live pricing and the authorized task budget determine whether a generation may proceed.

## First request

Attach one isolated PNG that you are entitled to process, and ask:

```text
$tower-sprite-pipeline
Animate this character walking left: eight frames, 100 ms each.
Maximum total provider budget: USD 0.50.
```

That cap is an example of a user instruction, not a promise that generation will fit it. A request may stop when verified prices exceed the available allowance.

For an attack, specify direction and the intended strike, for example:

```text
$tower-sprite-pipeline
One downward sword attack facing left: eight frames, 150 ms each.
Preserve body and weapon scale. Keep the body centered horizontally
while retaining natural vertical motion. Maximum provider budget: USD 1.00.
```

The agent handles task manifests and review evidence. Do not invent task IDs or invoke internal legacy generation to bypass the managed task contract.

## What to inspect

Expect a sheet, frame images, a preview, and review evidence only after the appropriate workflow finishes. Inspect anatomy, scale, weapon attachment, transparency, action, and the loop or recovery.

`READY_FOR_VISUAL_REVIEW` is an inspection state. It does not mean the result is ready for delivery. A reviewed export still requires separate acceptance in your game engine.

See [validation](VALIDATION.md) for reproducible offline checks and [status](STATUS.md) for known limits.
