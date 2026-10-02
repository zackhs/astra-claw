# Validation and current limits

The Linux prototype was exercised with a licensed full Factorio 2.0.77 x64
client on an ARM64 host through Box64, using Mesa software rendering and a
private authenticated 1024×768 Xvfb display. A separate vanilla loopback practice
server was used, without mods, RCON, Lua injection, granted items or research.
The practice server did not verify Factorio identities; a verified-server login
has not been independently validated.

## Live model tasks

Actual OpenClaw 2026.9.7 and gpt-6-astra through the application's own authorized
ChatGPT connection completed a small task: joined the server, sent ordinary chat,
walked to clear ground, opened inventory and placed the starting stone furnace.
Subsequent native UI inspection identified the placed furnace and confirmed the
message in chat history. An immediate post-input frame was not treated as proof
that a game action had rendered or reached the server.

The furnace reported no fuel and zero products finished. This verifies placement
and inspection, not smelting production or a larger autonomous factory build.

A second model task saved a lesson from the furnace panel using `remember`.
The lesson and an owner-defined layout preference survived restart, with model
counters unchanged and no automatic inference after restart.

All ten owned processes in the final client/display/Gateway/broker tree stopped
on shutdown, and the service ports had no remaining listeners. Existing game
processes stayed running. The separate practice server saved and exited normally.

The standalone installer was tested with user-supplied Node 24.21.0 and its
locked public npm dependencies. The resulting OpenClaw 2026.9.7 runtime started
successfully with the private client and retained companion memory.

## Automated tests

The prepared public source passed **128 tests** before publication. Tests exercise private
capture, mandatory sandboxing, bounded input, release/cleanup, steering and
stale-response rejection, persistent visual memory, image transport, startup
failures and same-origin loopback HTTP controls. No real game, account sign-in
or model inference is needed for the automated suite.

## Limitations

Linux is supported. Windows/macOS packaging and ChatGPT plugin/cloud-to-local
integration are pending. Active tasks can read visible chat; passive wake-up and
reliable immediate steering from Factorio chat are not implemented. Use the desk
to start or steer work. Complex blueprints, mining outposts, trains, long sessions
and full survival progression need more gameplay evaluation.

Private screenshots, raw task transcripts, learning records, credentials and
game files are intentionally excluded from this public repository. This summary
reports the local acceptance results without publishing those private records.
