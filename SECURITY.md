# Data handling and security

Astra Claw is user-hosted. Its source release contains no Factorio binaries,
assets, saves, account credentials or private gameplay records.

## Local data

Keep companion data outside a repository, or in the ignored `claw-data/` folder.
The data directory contains the application's ChatGPT account/registration,
Factorio player login, private game configuration, screenshots, task history and
learned lessons. Keep it private and do not attach it to public issues.
OpenClaw receives a local broker token; ChatGPT OAuth tokens remain in the
companion's own account store. The npm installer uses private empty configuration
files rather than an existing npm account configuration.

The desk binds to 127.0.0.1, checks its exact Host/Origin, and protects mutations
with a CSRF token. Do not expose it directly through a public reverse proxy.
Game actions and screenshots target an authenticated private X display under
mandatory Bubblewrap. The model has no shell or host desktop tool. Networking
is shared so the licensed client can join a server. Factorio and OpenClaw are
user-supplied software dependencies, rather than untrusted model-written code.

## Model data

Starting a task sends private game screenshots and task context to the authorized
OpenAI model service. Manual setup does not start inference. Complete account
login before starting a model task, and avoid displaying account credentials in
an active task. Lessons record visual experience, not model weight training.

## Reporting an issue

Report security concerns privately using GitHub's private vulnerability reporting
when enabled for this repository. Include a minimal reproduction with synthetic
values; do not publish tokens, passwords, account files, save files or raw logs.
