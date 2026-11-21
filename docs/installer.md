# The Windows installer

`SnapLab-Setup.exe` is a small Windows program (source: [installer/SnapLabSetup.cs](../installer/SnapLabSetup.cs)).
It is built with the C# compiler that ships with Windows, so building needs nothing extra:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1      # writes installer\dist\SnapLab-Setup.exe
```

## What it does

| Step | Runs as | What happens |
|---|---|---|
| Check this PC | user | 64-bit Windows 10 2004 (build 19041) or later. |
| Administrator part | administrator, only if needed | Turns on WSL (`wsl --install --no-distribution`) and installs Python 3.13 and Git (winget, or the signed installers from python.org and git-for-windows; the signature is checked before running). If WSL needs a restart, setup registers itself in `RunOnce` and continues after the next sign-in. |
| Download SnapLab | user | `git clone` into `~\.snaplab\app` (or `git pull` when it is already there). |
| Prepare SnapLab | user | A Python environment in `app\.venv` with `cryptography`. |
| Linux workspace | user | `tools/setup_wsl.py --resume`: the dedicated WSL distribution, its packages, Chrome, Codex and the isolation checks. If this fails, chat still works and setup says why; running setup again continues. |
| Start menu | user | `SnapLab` in the Start menu, "start when I sign in" (`HKCU\...\Run`), and an entry in Installed apps that runs `SnapLab-Setup.exe --uninstall`. |
| Open SnapLab | user | `tools/launch.py` starts the server and opens the browser with a one-use link. |

Only the administrator part changes Windows. It runs in a second copy of the program started with "Run as
administrator"; the first copy waits for it and reads its result from a small state file in `~\.snaplab`. Everything
else runs as the signed-in user, so files and the WSL distribution belong to that user. A log is kept in
`~\.snaplab\install.log`.

Removing SnapLab stops its server, unregisters the WSL distribution only if this installation's setup created it,
and removes the app folder, the Start menu entry and the sign-in entry. Setup writes `~\.snaplab\workspace-<name>.txt`
once: `created` when it made the distribution, `existing` when the distribution was already there (another
installation, or SnapLab run from a git checkout, uses it), `skipped` with `--skip-wsl`. Only `created` (and a
`setup-result.json` in its folder) leads to `wsl --unregister`. An installation from 1.1.0 or earlier has no such
file; then only the default installation ("SnapLab") removes the workspace.
Data and saved API keys are removed only when the user ticks the box.

## Options (for testing and support)

| Option | Meaning |
|---|---|
| `--quiet` | No window. Exit code 0 = done, 3 = restart needed, 1 = stopped. |
| `--auto` | Window, but presses the main button on each page (with `--screenshots <dir>` it saves every page). |
| `--no-elevate` | Run the administrator part in this process (for a runner that already is an administrator). |
| `--skip-wsl` | Leave out WSL and the Linux workspace (for machines without virtualization, such as CI runners). |
| `--name`, `--profile`, `--app-dir`, `--data-dir`, `--port` | A second, separate installation (another Start menu name, folders and port). |
| `--repo`, `--branch` | Install from another repository or a local folder. |
| `--no-launch`, `--no-startup`, `--no-shortcut` | Do not open the browser, register "start when I sign in", or add Start menu and Installed apps entries. |
| `--uninstall [--delete-data]` | Remove the installation named by `--name` and its folders. |

The environment variables `SNAPLAB_WSL_DISTRO` and `SNAPLAB_WSL_DIR` give a second installation its own WSL
distribution and key folder, so a test never touches the first one.

## How it was tested

On a Windows 11 PC that already had an installation, with `--name SnapLab-Test`, a separate profile folder, port
18480 and `SNAPLAB_WSL_DISTRO=SnapLab-InstallTest`: the whole run, including a real WSL distribution that passed
the isolation checks (about 4 minutes), then `--uninstall --delete-data`, which removed the distribution, folders,
Start menu entry, sign-in entry and Installed apps entry. The first run found that Git for Windows checks files out
with CRLF line endings, which broke the shell scripts copied into Linux; `.gitattributes` now keeps LF everywhere.

Not tested on a real PC yet: the administrator part itself (turning WSL on, installing Python and Git) and the
restart-and-continue path, because the test PC already had them and the UAC prompt needs a person at the screen.
