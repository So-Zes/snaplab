# Computer use: a real Chrome, the mouse, the keyboard and DevTools

Status: available to Codex and Pi in the WSL workspace and with the SSH backend. Not available with
`codex.backend: "local"` (the Windows sandbox).

A SnapCode worker can open a real Chrome window on a private Linux screen and use it like a person: click,
double-click, right-click, drag, scroll, hover, type, press and hold keys, use shortcuts, open DevTools with F12 and
type in its Console. The same Chrome is also reachable through the DevTools protocol, so the worker can read the
console, network requests, the DOM, the accessibility tree, localStorage and JavaScript values as text. Screenshots
of the whole screen, of the page, and enlarged parts of the screen go to the model as images. Logins and passwords
given by the task are typed. Card details, identity verification and CAPTCHA still stop the work. The private
screen is never the user's own Windows desktop.

## What the worker reads

The worker reads one operation list first: `instructions()` in `snaplab/harnesses/remote.py` (Codex receives it as
`AGENTS.md`, Pi as its appended system prompt). The task text each turn (`DELIVERY`) only points to it. The list
uses commands that run on the worker as written (`python3`, `node`, paths relative to the job folder).

Two helpers drive one browser:

- `node .snaplab/browser.mjs` speaks the DevTools protocol. `start` opens a browser without a window (fast checks),
  `start --headful` opens the window on the job's screen, `open URL` and `do STEP ...` act on the page. Steps:
  `click:`, `dblclick:`, `rightclick:`, `hover:`, `drag:`, `mousedown:`, `mouseup:`, `wheel:`, `type:`, `press:`
  (with modifiers, for example `press:Control+s`), `keydown:`, `keyup:`, `hold:`, `wait:`, `scroll:`, `url:`,
  `reload`, `tabs`, `tab:N`, `shot:`, and for reading `eval:`, `console`, `network`, `dom:`, `elements`, `a11y`,
  `storage`, `text`. `x,y` in steps are page pixels of `.snaplab/browser.png`.
- `python3 .snaplab/desktop.py ACTION` is the real mouse and keyboard (xdotool, real X events).
  `launch --app chrome --url index.html [--devtools]` starts the window (the same as `browser.mjs start --headful`
  and `open`), and `launch --app mousepad|libreoffice|terminal` or `launch --command '...'` starts other programs.
  Actions: `screenshot`, `zoom`, `click`, `double-click`, `triple-click`, `right-click`, `middle-click`
  (`--modifiers ctrl+shift`), `move`/`hover`, `drag`, `mouse-down`, `mouse-up`, `scroll` (up, down, left, right, at
  a point), `type` (ASCII as real key presses, other text pasted), `key` (`F12`, `ctrl+shift+j`, `Tab Tab Return`,
  `--repeat`), `key-down`, `key-up`, `hold`, `click-text`, `marks` (numbered targets; `click --mark N`), `inspect`,
  `windows`, `focus`, `wait`, `stop`.
  Coordinates are screen pixels of the full screenshot (1024 by 768 by default). Every action waits until the
  screen settles (at most about 3 seconds more, so a DevTools panel or a dialog has appeared), then saves
  `desktop.png` (or `--output`), the pointer position, and the screen text read by OCR (`--no-text` skips the OCR).
  `--grid` draws labelled screen coordinates on the saved picture (also on `zoom`, where the labels are still screen
  coordinates), so the model can read a point off the picture instead of estimating it.

Keys, shortcuts and held modifiers go to the X server as key codes through XTest. xdotool's own key command added an
Alt press to function keys on this keymap: F5 reached the page as Alt+F5, F12 did not open DevTools, and an arrow
key would have become Alt+arrow (Back or Forward). ASCII text is still typed with `xdotool type`, which sends no
extra modifier; newlines and tabs inside it go through XTest.

`browser.mjs ... elements` lists buttons, links, fields and canvases with page `x,y`, and in the window also screen
`sx,sy` for `desktop.py click`. `click-text` uses OCR first and, in the Chrome window, falls back to the page's own
buttons and links when OCR cannot read a label. The window shows no info bars, and a new profile does not offer to
save passwords or translate pages, so nothing covers the page.

`console` shows every console message since the page loaded (Chrome replays them when the helper connects); lines
logged during the current command are marked `[new]`. `network` lists the requests the page made with status, size
and time (Resource Timing), plus requests seen during the command. Each run also reports new console errors, page
errors and failed requests.

## Logins, passwords, cards and CAPTCHA

The worker types credentials that the task, the user or workspace files give it, such as a game's developer
password, and it does not guess others. It stops without input and reports when card details or identity
verification are needed, or when a CAPTCHA or another human check appears; it never tries to solve or bypass a
CAPTCHA. `desktop.py` reads the screen with OCR before each input and blocks input for the rest of the turn when it
recognizes a CAPTCHA, a human check, card fields or identity verification. Login and password screens do not
block it. The check reuses the last reading while the screen is unchanged. It is a best-effort guard, not a
security boundary; filesystem, process and display separation are the boundaries.

## Seeing the screen

The translator passes images to the model: Codex's `view_image` and Pi's `read` of a PNG become `image_url` parts,
and the newest two images stay in the history of workspace turns. In live tests the Snapscribe model read images:
a PNG with a random word and number drawn on noise was answered exactly, through Codex and through Pi, with no OCR
or script call. When an image is unclear, the same screen is available as text: `visible_text` (OCR of the screen),
`zoom` (an enlarged region, with its own OCR), and in Chrome `elements`, `a11y`, `text` and `dom`.

Reading is reliable; pointing is not. In a live game test the model aimed at the DevTools "Console" tab and clicked
about 40 pixels too high, in the address bar, several times. A canvas test (round buttons drawn on a canvas, so no
element to target) measured it again: with `--grid` and `zoom` Codex still read button centres 15 to 30 pixels wrong
and missed 20-pixel buttons; both Codex and Pi got every click right only after writing their own script that found
the blue circles in the screenshot's pixels.

That script is now part of the desktop: `desktop.py marks [--x X --y Y --width W --height H]` numbers everything
clickable it finds and `click --mark N` (also `double-click`, `right-click`, `move`, `drag --mark N --to-mark M`)
acts on its centre. The marks come from three places and are merged into one list:

- the Chrome window's own elements (exact, green boxes),
- shapes found in the pixels (magenta): an edge image at half size, connected regions, specks, separators and
  panels larger than a fifth of the screen dropped; a button-sized shape (up to 96 px) holding exactly one thing (the
  digit on a round button) is one target; each shape gets its colour (the commonest colour that is not the colour
  around it),
- text read by OCR (orange); text inside a button becomes that button's label.

It saves `marks.png` (the screen with the numbers) and `marks-sheet.png` (every mark enlarged with `#N` under it,
because the number labels can sit next to digits drawn on the screen), and keeps the list for `--mark`. Plain PIL,
no numpy: about 0.05 seconds for the shapes on a 1024 by 768 screen, plus the OCR. Offline, 240 round buttons on
synthetic screens and the 12 buttons of a real Chrome rendering were all found as one target each, 0 to 3 pixels
from the centre (`tests/test_desktop_marks.py`). The operation list tells the worker not to estimate coordinates
from a picture and to use marks; `--grid` and `zoom` remain for what marks misses.

## Capacity

There is no fixed number of desktops. A job starts when the worker has room and otherwise waits without a time
limit until it has (Stop still cancels it); the session shows a note while it waits. Room means:

- memory: the available memory, and the headroom of any limited parent cgroup (file cache not counted), leave at
  least 1024 MiB for the new job;
- tasks: the tightest parent limit, if there is one, has at least 300 free task slots (threads count as tasks).

Jobs admitted during the last minute count with those expected sizes, because their memory has not grown yet.

In the WSL workspace the worker cgroup has no memory, task or CPU cap of its own and jobs run without a per-job
scope (see [local-wsl.md](local-wsl.md)); only the admission check above applies.

With the SSH backend each job runs in a systemd scope with `MemoryMax=3072M`, no swap, `TasksMax=1024` and
`OOMPolicy=continue`. When a heavy page reaches the limit, the kernel kills one process (usually a browser tab,
which Chrome shows as crashed) and the turn carries on. With systemd's default `OOMPolicy=stop` the whole scope
would be stopped with SIGTERM and the turn would end with "exited with code -15". The worker account's slice is set
by `tools/setup_worker.sh` (needs sudo): `MemoryMax` is the host memory minus 1.5 GiB (at least 3 GiB), there is no
`MemoryHigh` (throttling raises memory pressure, and Ubuntu's systemd-oomd kills whole jobs under pressure), and
`TasksMax=8192`.

Measured on a Linux worker with 8 threads and 7.4 GiB of memory: a desktop job with Chrome, DevTools and the test
game peaked at 552 to 580 MiB and 231 to 233 tasks. Three such jobs ran at the same time without waiting.

## Worker setup

The WSL workspace is prepared by `install.ps1 -SetupWorker` (see [local-wsl.md](local-wsl.md)). It installs Xvfb,
Openbox, xdotool, scrot, xclip, xterm, Mousepad, LibreOffice Writer, Tk, Pillow, Japanese fonts, Tesseract with
English and Japanese data, a private D-Bus daemon, Google Chrome, Codex and Pi. No provider key is copied into it.
There the desktop is always on, with the default 1024x768 screen.

The SSH backend (`codex.backend: "ssh"`) is an advanced option that runs the same jobs on a Linux machine over SSH.
It needs a dedicated unprivileged worker account (the name SnapLab expects is the default of
`tools/setup_worker.sh` and `codex.remote.user`), SSH, Bubblewrap and systemd with cgroup v2. On an Ubuntu or Debian
machine, run from the repository:

```sh
sudo bash tools/setup_worker.sh
```

The script installs the same desktop packages, sets the slice limits above and enables the account's systemd user
manager. Install Codex and Chrome or Chromium (found on `PATH` as `chromium`, `chromium-browser` or
`google-chrome`) separately; snap-packaged browsers may not work in Bubblewrap. No provider key belongs on the
worker. Configure `codex.remote.host` and `ssh_exe`. The desktop is on by default for Codex and Pi turns
(`codex.remote.desktop=false` turns it off). `codex.remote.desktop_size`, for example `"1280x800"`, changes the
screen size (default 1024x768). The helpers are sent with each task.

## Isolation and lifetime

Each turn starts a new Xvfb display, Openbox and D-Bus inside the job's Bubblewrap sandbox. Its temporary directory,
home, Xauthority cookie, X socket and PID namespace are private; X11 TCP and abstract sockets are off. The Windows
desktop and home folder are never mounted. The project workspace is writable and system software is read-only.

`keep.py` supervises the agent, and the desktop supervisor runs inside it: programs started with
`desktop.py launch`, `keep.py start` or `browser.mjs start` survive each tool call and end with the turn. Kept
processes get the caller's environment (without secret-looking names), so a kept Chrome opens on the job's
screen. Bubblewrap's PID namespace removes everything on exit, timeout, SSH disconnect and Stop. The browser profile
in `.snaplab/browser-profile` stays in the job folder on the worker between turns (logins and localStorage
survive); `.snaplab/` files other than the plan, notes and attachment list are not copied back to SnapLab's
project folder.

In the WSL workspace each job also has its own network namespace. With the SSH backend the live browser's DevTools
port listens on the worker's loopback, which other jobs of the same account share; the port carries no password.

## Phone view and final images

The session page shows a JPEG of the current screen, sampled every two seconds when it changes, bounded to 512 kB,
kept in memory only and sent with `Cache-Control: no-store`. For a final result the worker saves a PNG in the
workspace and lists it in `.snaplab/attachments.json`:

```json
[{"path":"final.png","caption":"The developer screen with DevTools"}]
```

## Verification

- `tests/test_desktop_helper.py`: real-input commands, key names, typing, zoom, the narrowed sensitive-screen check,
  memory and task admission, and the operation list. `tests/test_browser_helper.py` and `tests/test_keep_helper.py`
  cover the browser helper and kept processes. `tests/test_round15.py` covers image forwarding and job limits.
  `tests/test_desktop_marks.py` covers the marks.
- `tests/fixtures/dev_game.html` is a test game for end-to-end checks: a developer password, a developer screen,
  a canvas that takes drags, right-clicks, wheel scrolling and held keys, a code drawn on the canvas, and values
  (`game.gold`, `game.level`) to read in the DevTools Console.
- In a live run of a task with that game and the Snapscribe model, Codex and Pi worked at the same time on one
  Linux worker:
  Codex finished in 105 seconds with 15 model calls (it logged in and pressed the buttons through browser.mjs, then
  opened DevTools with F12 and typed in the Console), and Pi finished in 218 seconds with 31 model calls using the
  mouse and keyboard. Both reported the game's gold, level and canvas code correctly and attached a screenshot of
  the Console.

## Known limits

Pi's safety extension reports that it is ready on stderr, so the desktop supervisor passes the agent's stderr
through the same JSON-only filter as stdout (as the path without a desktop always did).

OCR misses some labels and can misread small text; `elements`, `zoom` and the screenshot cover those cases. Seeing
an image does not guarantee reading every digit or clicking the exact pixel. Very heavy pages can still lose a tab
to the 3 GiB job limit on an SSH worker. Report actual task failures rather than claiming the feature completes
every request.
