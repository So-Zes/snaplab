# Local SnapCode with WSL2

New installations use a dedicated `SnapLab-Worker` Linux distribution on the same Windows PC
(`codex.backend: "wsl"`). Existing installations and sessions keep their selected backend. Installing this
feature does not switch an existing `codex.backend` or SSH destination.

Run `powershell -File install.ps1 -SetupWorker` from the checkout. This installs Ubuntu Base,
Bubblewrap, OpenSSH, Codex CLI, Pi, Node.js, Google Chrome, Xvfb, desktop tools, fonts and Python dependencies
inside the dedicated distribution. It also creates a separate SSH identity under
`%USERPROFILE%\.snaplab\wsl`. The image is verified against Ubuntu's HTTPS SHA256SUMS.
The keys and records are kept outside AppData on purpose: a program installed as an app package writes AppData
files into its own private copy, which the SnapLab logon task cannot see. A setup made earlier under
`%LOCALAPPDATA%\SnapLab\wsl` is copied over once, and the distribution's disk stays where WSL registered it.
Run the same command to refresh a previously managed environment after updating SnapLab.

WSL2 must already be installed. Windows Home supports WSL2. If Windows asks for administrator
permission or a restart while installing WSL, finish that Windows operation yourself before rerunning
SnapLab setup. SnapLab does not enable Windows features, change `.wslconfig`, change Windows
firewall policy, start Docker, or shut down other distributions. The current installer supports x64.

Until setup and isolation checks pass, SnapCode is disabled and chat remains available. The unavailable
SnapCode card explains how to prepare WSL in English, Japanese and Russian. Setup failures never
fall back to running commands directly on Windows. Every task reruns the checks before starting Codex or Pi.

The worker starts when a task needs it. The jobs of one SnapLab process share it: the first job starts it,
others run alongside, and the last one to finish terminates it (another SnapLab process waits while it is in use).
No Windows startup service is added: the next task starts the distribution again, including after a PC restart.
Jobs retain their files in the dedicated distribution and exchange only the existing bounded project-file protocol
with SnapLab. The worker cgroup has no memory, task or CPU cap of its own; each job waits until the worker has
enough free memory before it starts. No global WSL memory limit is changed.

Each job has separate mount, PID, IPC, UTS, cgroup, user and network namespaces. It can see its own
workspace, selected read-only Linux software, its private virtual desktop, and two per-job Unix sockets.
The outer internet broker permits TCP connections to public IPv4 ports 80 and 443, through an HTTP proxy.
It does not inspect the contents of HTTPS tunnels. It rejects private,
loopback, link-local, multicast, reserved, Tailscale and known Windows host addresses, and pins the
validated numeric address for the connection. The per-job authenticated translator on Windows
opens that connection after repeating the destination check with a fresh Windows IP Helper interface
inventory. A connected socket whose local and remote addresses are equal is closed before any content
is sent or received, including if a new Windows address appeared during the check. This route is disabled
on other translators. IPv6 and direct IP networking are disabled. Browser,
npm, pip and curl traffic must use the supplied HTTP proxy; other network protocols are unsupported.
The other socket reaches only this turn's token-protected model translator. The provider key stays
on Windows and is never copied into WSL.

`tools/check_wsl_isolation.py` runs in the real job restrictions and checks Windows paths, manual
mounts, executable launch, WSL interop, vsock, network access, policy permissions, sibling files and
processes, a public positive control, and the model relay's authentication. A failed or inconclusive
check blocks the job. Root-owned files outside the job define the policy. Seccomp prevents remounting
hidden WSL drivers, entering other namespaces, and creating vsock or packet sockets.

Every task also directly executes a two-byte `MZ` file with executable permissions in its workspace
and private `/tmp`. Each attempt has a one-second limit; a timeout, exit code zero, or unexplained
failure blocks work. The probe accepts an explicit exec denial or the known WSL failure to create a
vsock socket or traverse the private PID namespace. Raw socket and socketpair calls also test a vsock
domain with nonzero upper bits: seccomp compares the low 32 bits consumed by the kernel.

Setup additionally copies the installed Windows `cmd.exe` and tests it and `MZ` in both locations,
as the worker account outside Bubblewrap and through the actual job wrapper
(`tools/check_wsl_windows_exec.py`). Windows observes a unique temporary marker, first validates the same command
using a native copy of cmd, and continues observing until the worker stops. It then removes the temporary files.
Results and the current binfmt registration are saved in `%USERPROFILE%\.snaplab\wsl\windows-exec-result.json`.
Outside attempts may take ten seconds to fail because interop remains disabled; their failure is recorded, not
treated as evidence that Bubblewrap denied execution. Only setup pays this additional cost; it does not call a
model.

Hiding `/init` and the binfmt registration path is insufficient when `WSLInterop` has the `F` flag:
the kernel retains the interpreter. Setup does not unregister or disable this VM-wide registration,
which can affect other WSL distributions. Isolation relies on the enforced process, filesystem and
socket restrictions, and each task verifies them again.

These checks test the configured boundaries; they do not prove the absence of Linux kernel, WSL,
browser or broker vulnerabilities. WSL distributions share a kernel. This is not a separate physical
machine. Public Internet content and files intentionally placed in the job can leave through the
allowed public connection. Windows administrators and the account owner can change the installation.
An external public reverse proxy or router port forwarding to a Windows service is indistinguishable
from another public website. Such externally published personal services are outside this boundary;
do not expose private Windows services to the public Internet while using unrestricted public egress.
Local and Tailscale services are blocked by the configured address and namespace restrictions.

Model-free checks:

```powershell
python tools/setup_wsl.py --resume
python tools/test_offline.py --pattern test_round18.py
python tools/test_offline.py --pattern "test_wsl_*.py"
```

The first command reruns setup on the existing distribution, including the isolation probes and the Windows
executable check. The tests use fakes for WSL and do not call a provider.
