# Astra Claw — local, mod-free Factorio teammate

Astra Claw runs an ordinary licensed Factorio client in a private virtual display.
Its mouse, keyboard and screenshots target that display. You can keep using your
desktop and playing your own character. There is no companion mod, server Lua
injection, RCON connection, or model-accessible shell in this backend.

The companion is MIT-licensed and user-hosted. Factorio remains proprietary;
supply your own licensed installation. Game assets and account credentials are
excluded from the source release.

## Supported environment

The current implementation targets Linux with Python 3.11 or later. It requires
Bubblewrap, Xvfb, xauth, xdotool, system Python and Mesa software OpenGL rendering.
Bubblewrap is mandatory: an isolation failure stops startup, with no fallback
to the user's desktop. Windows and macOS packaging is not implemented yet.

On Ubuntu/Debian, install the system prerequisites:

```sh
sudo apt install python3 python3-venv bubblewrap xvfb xauth xdotool libx11-6 libgl1-mesa-dri libegl-mesa0 libglx-mesa0
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Extract the licensed full Linux client from Factorio's official download. Its
version must match the target server exactly. The small headless-server download
cannot serve as Claw's playable client. The source release never bundles either
download or downloads the game using an embedded account.

## Start the companion

Keep game installation and companion data in separate directories:

```sh
.venv/bin/astra-claw --data ./claw-data init --factorio-install /path/to/factorio
.venv/bin/astra-claw --data ./claw-data serve
```

Open the printed `http://127.0.0.1:8790/` address. The desk is loopback-only.
Starting the service opens no host desktop window and starts no model task.

1. Start the hidden client from the desk, optionally supplying the server address.
2. Complete Factorio login inside its private preview using the manual controls.
3. Select **Sign in with ChatGPT** and authorize this application's plan usage.
4. Give Astra a task. Follow-up messages steer the same conversation immediately.
5. Pause before using the manual game controls. Resume takes a fresh screenshot.

You can instead launch the client when starting the service:

```sh
.venv/bin/astra-claw --data ./claw-data serve --start-client --server factorio.example:34197
```

The companion stays idle until you start a task. For private servers without
identity verification, `init --player-name Astra` seeds an isolated nickname.
This does not authenticate a Factorio account. Verified servers require Claw
and the human to use distinct authenticated player identities. Server passwords,
whitelists, player limits, and network reachability still apply. This application
does not alter those server settings.

For an explicitly configured emulator on ARM Linux:

```sh
.venv/bin/astra-claw --data ./claw-data init --factorio-install /path/to/factorio \
  --launcher /path/to/box64 --xvfb /path/to/Xvfb
```

The emulator must already be installed and compatible with the full client.
Other CPU architectures and native versions need separate testing.

## Optional OpenClaw runtime

The default local service calls Astra directly. To use an actual OpenClaw
Gateway as the agent runtime, install the pinned runtime in its own directory
and select it for this companion. This profile does not load the previous native
Factorio plugin, another Claw's skills, or its account credentials.

Install [Node.js 24](https://nodejs.org/en/download) for your Linux architecture.
Node 24.21.0 is the version used for this project; the installer accepts Node
24.16.0 or newer within Node 24. Keep the Node installation available afterward,
because the private runtime links to its executable. The official Linux Node
archive includes `lib/node_modules/npm/bin/npm-cli.js`.

From the source release directory, after creating `.venv` and running `init`:

```sh
.venv/bin/python tools/install_visual_claw_runtime.py ./claw-runtime \
  --node /path/to/node-v24.21.0-linux-x64/bin/node \
  --npm-cli /path/to/node-v24.21.0-linux-x64/lib/node_modules/npm/bin/npm-cli.js
.venv/bin/astra-claw --data ./claw-data claw-setup --runtime ./claw-runtime
.venv/bin/astra-claw --data ./claw-data serve
```

Use the ARM64 Node archive paths on ARM Linux. If a suitable Node and npm are
already installed on your PATH, the shorter installer command is sufficient:

```sh
.venv/bin/python tools/install_visual_claw_runtime.py ./claw-runtime
```

The installer uses the included dependency lock and `npm ci` to install OpenClaw
2026.9.7 with registry integrity checks. Installation scripts, user/global npm
configuration, inherited API keys and audit calls are disabled. npm receives a
private home and two distinct empty configuration files. It needs access
to the public npm registry. A completed runtime is reused without reinstalling;
an unrelated existing directory is rejected. The installer does not download
Factorio or sign into any account. Dependency lock reproducibility does not prove
that every CPU/OS combination has been tested.

The local service starts the separate loopback Gateway on port 18941 and model
broker on 8792. To avoid a conflict, select unused private ports explicitly:

```sh
.venv/bin/astra-claw --data ./claw-data claw-setup --runtime ./claw-runtime \
  --gateway-port 18945 --model-port 8794
```

Open the desk at `http://127.0.0.1:8790/`, select **Sign in with ChatGPT**, and
authorize plan usage for this application. An eligible account must offer
`gpt-6-astra`; logging in does not guarantee that model or unlimited allowance.
If you already have a Sign in with ChatGPT account profile owned by this same
application, reuse it explicitly when starting the service:

```sh
.venv/bin/astra-claw --data ./claw-data \
  --account-data /path/to/this-app-account serve
.venv/bin/astra-claw --data ./claw-data \
  --account-data /path/to/this-app-account models
```

This option points at the application's renewable account store; it does not
copy credentials into OpenClaw. Do not point it at Codex, an unrelated app, or
another user's account. OpenClaw holds only a token for this companion's local
broker. Both Factorio login and an explicit gameplay task remain necessary.

The visual profile disables built-in shell, file, browser, session and cron
tools. Its external tools remain the same bounded game actions and learning
tools. The companion owns durable tasks, steering and lessons; each Gateway
turn receives the bounded conversation and latest images in a fresh session.
Stopping the companion shuts down its owned Gateway processes as well.

## How Astra plays and learns

The companion owns a streaming Astra conversation using the application's own
Sign in with ChatGPT connection. It checks the account's offered model catalog
for `gpt-6-astra`; it does not silently substitute a different model or read
credentials belonging to Codex or another application.

The model sees PNG game screenshots and a small set of bounded actions: pointer
movement, clicks, short key holds, typing, scrolling and waiting. It must inspect
screenshots between actions. A changed instruction invalidates a response already
being streamed before further actions can run. Pausing releases held input.

Factory preferences and observed lessons persist in the companion data folder.
They are experience memory, not model weight training. Lessons remain labeled as
model-interpreted visual evidence. Existing native/RCON helpers do not become
verified visual routines merely by being copied. Finishing a task saves actual
screenshots and the model's explanation of the visible result; this is not an
independent proof of every claimed factory outcome.

The owner sets factory conventions in the preferences panel, such as conveyors
feeding a central hub. New users start with no preset factory preferences.

## Isolation and lifecycle

Each client has its own game write directory, configuration, login, saves and
empty third-party mod directory. Bubblewrap mounts the licensed game read-only
and exposes only this client's writable game data. Its private X display sockets
are separate from the host's display sockets; DBus, Wayland, host input devices
and the user's home folder are not mounted into the client.

The trusted launcher supervises its own processes. An exclusive profile lease
prevents duplicate sessions; startup failures stop only owned children. Normal
shutdown stops Astra, releases input, closes the hidden client and display, and
preserves the user's factory on its existing server. Restarted work stays paused
until the owner explicitly resumes it.

The local desk accepts requests only for its exact loopback address. Mutations
require its same-origin CSRF token. It has no arbitrary file, shell or host
desktop-control endpoint. Factorio login takes place in the game; game login
screenshots are not sent to Astra until the owner explicitly begins a task.

## Current scope

This is the local companion runtime and desk. A published ChatGPT plugin and
an authenticated connection from ChatGPT's cloud to the local runner are separate
integration work. Starting this local service does not publish a public game
server or install a ChatGPT plugin.

Visual control has less direct information than the previous native integration.
Blueprints, inventories, recipes and entity settings must be inspected through
Factorio's UI. Active tasks can read visible game chat, but an idle chat listener
and reliable instant steering from Factorio chat are not implemented. Use the
desk to start or steer work. Performance and success depend on the computer, world size and
model allowance. Long autonomous construction needs more gameplay evaluation.

## Tests and source references

```sh
.venv/bin/python -m pip install '.[test]'
.venv/bin/python -m pytest -q
```

The tests exercise desktop isolation boundaries, lifecycle cleanup, bounded
input, stale-response rejection, durable learning and local HTTP protections.
Live gameplay results are recorded separately in `VALIDATION.md`.

- [Factorio multiplayer](https://wiki.factorio.com/Multiplayer)
- [Factorio client command line](https://wiki.factorio.com/Command_line_parameters)
- [Xvfb virtual display](https://xorg.freedesktop.org/releases/X11R6.9.0/doc/html/Xvfb.1.html)
- [User-hosted ChatGPT plan inference](https://developers.openai.com/siwc/token-sharing-open-source)
- [Plan inference tool limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
