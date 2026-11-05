# Atlas — the agent stops guessing how your projects run

Hermes can read a repository, but it does not know that *this* checkout is a uv project whose tests run
through `uv run pytest`, or that the other one is a Go module where `make test` does not exist. So it
guesses: `npm test` in a Python repo, `pytest` in a Go repo, `localhost:3000` because most dev servers
are on 3000. Every wrong guess costs a failed command and a round trip.

Atlas scans the folders you keep code in — once, read-only — detects each project's stack from its marker
files, derives the exact install / test / lint / build / run invocations **that those markers declare**,
and caches one card per project. At the start of every session whose working directory is inside a known
project it injects a short card into the system prompt: name, stack, exact commands. The first command
the agent runs is the right one.

- **Marker-file detection, no execution.** Nothing in a scanned project is run, imported, or written.
- **Every command carries its reason.** `uv run pytest` because `pyproject.toml` has
  `[tool.pytest.ini_options]`; `npm run test` because `package.json` has a `scripts.test`.
- **Silence over invention.** No test script, no `npm test`. No port declared, no port printed.
- **Local and small.** One SQLite file under the profile, stdlib only, no network, no telemetry.

## Quickstart

Atlas is a native Hermes plugin (manifest v2, plugin API 1) and needs Hermes `>=0.22.0`.

```sh
git clone https://github.com/okyssta/hermes-atlas ~/.hermes/plugins/atlas
hermes plugins enable atlas
hermes plugins doctor ~/.hermes/plugins/atlas      # manifests + register() against the real runtime
```

Restart the gateway (`hermes gateway restart`) if one is running, then start a session inside a project:

```
$ cd ~/code/my-service && hermes
# the system prompt now contains:
#   Atlas: project my-service · python (uv)
#   path: /root/code/my-service
#   install: uv sync
#   test: uv run pytest
#   lint: uv run ruff check .
#   ...
```

If the folders you care about are not the defaults, set them once:

```sh
hermes config set plugins.entries.atlas.settings.roots '["~/code", "~/work"]'
```

Defaults are the session's working directory plus every existing folder among `~/code`, `~/projects`,
`~/src`, `~/work`, `~/repos`.

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `project_list` | `query=""` (filters name/path/stack) | Projects, newest commit first, capped at 40: name, stack, path, last commit date. Empty result says how to build the index. |
| `project_info` | `path_or_name=""` (empty = cwd) | The full card: stack, package manager + why, every command with its marker, env-file names, branch, last commit, README title, ports, compose services, top-level layout, and notes about what could not be derived. |
| `project_commands` | `path_or_name=""` | Just the runnable commands, one per line, with a leading `# name — path` comment. No reasons, no prose — made to paste. |
| `project_refresh` | `path_or_name=""` (empty = all roots) | Re-scans the roots (or one project) and reports directories visited, projects found/pruned, elapsed time, and whether the budget cut the walk short. |

The same index is on `/atlas`: `list [query]`, `info <name|path>`, `commands <name>`,
`refresh [name]`, `show` (the card that would be injected), `forget`.

## The session card

Registered through `ctx.register_system_prompt_section("atlas.project", callable, position="after_memory",
max_chars=900)`. The callable receives the session info mapping, reads `cwd`, and returns `""` unless the
cwd is inside a detected project. Real output for `/root/peripheral` (210 characters):

```
Atlas: project peripheral · python (uv)
path: /root/peripheral
test: /root/peripheral/.venv/bin/python -m pytest
branch: master
Use these exact commands instead of guessing. Call project_info for the full card.
```

The renderer dedupes by task (one `install`, one `run`), truncates any single command at 120 characters,
and drops command lines from the bottom rather than cutting the header or the instruction line. The card
is memoised per project for the life of the process, so a session start costs one `scandir` walk up from
the cwd — no root scan, no database scan.

## How detection works

A directory is a project when a marker file is in it. A command is emitted only when a marker declares
it; the `reason` field is the file and key that justified it.

| Stack | Marker | Commands derived |
| --- | --- | --- |
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `Pipfile`, `uv.lock`, `poetry.lock`, `manage.py`; `.venv/pyvenv.cfg` next to `*.py` | Package manager: `uv.lock` → uv, `poetry.lock`/`[tool.poetry]` → Poetry, `Pipfile` → Pipenv, otherwise pip (uv when `pyvenv.cfg` says uv created the venv). `install` from the manager (`uv sync`, `poetry install`, `pip install -r requirements.txt`, `pip install -e .`). `test` only with a pytest config, pytest in the declared dependencies, `conftest.py`, a `tests/test_*.py`, pytest in the venv, or Django's `manage.py`. `lint`/`format`/`typecheck` from the ruff / flake8 / black / mypy configs actually present. `build` only with `[build-system]`. `run` from Django, `[project.scripts]`, or a package with `__main__.py`. |
| Node | `package.json` (+ lockfile) | pnpm / yarn / bun / npm from the lockfile, else npm. `install`, then one command per **existing** script: `test`, `lint`, `typecheck`, `build`, `dev`/`start`/`serve`, `format`. Nonexistent scripts produce a note, not a command. |
| Rust | `Cargo.toml` | `cargo fetch/build/test/clippy`; `cargo fmt --check` only with `rustfmt.toml`; `cargo run` only with `src/main.rs` or `[[bin]]`. |
| Go | `go.mod` | `go mod download`, `go build ./...`, `go test ./...`, `go vet ./...`; `go run .` only with a root `main.go`, `go run ./cmd/<x>` only when `cmd/x/main.go` exists. |
| Java | `pom.xml`, `build.gradle[.kts]` | `mvn`/`./mvnw` `package` and `test`; `spring-boot:run` only when the Spring Boot plugin is declared. Gradle `build`/`test`; `run` only with the `application` plugin. |
| Ruby | `Gemfile` | `bundle install`; `bundle exec rspec` (with `spec/` or `.rspec`) or `rake test` (with a `Rakefile`); `rubocop` with `.rubocop.yml`; `rails server` with `bin/rails`. |
| PHP | `composer.json` | `composer install`; `composer test` (script) or `vendor/bin/phpunit` (with `phpunit.xml`); `php artisan serve` with `artisan`. |
| Compose | `compose.yaml`, `docker-compose.yml`, … | Service names and **host** ports, parsed line by line; `docker compose up` / `build` when a services block is readable. |
| Make | `Makefile` | Top-level targets only, from `^name:` at column 0 — `.PHONY`, variable assignments and `%` pattern rules are skipped. Only `test`/`lint`/`fmt`/`build`/`run`/`install`-style targets become commands, and only when no real stack already provides that task. Other targets are listed in a note. |

Also read, never executed: `git` branch and default branch (`refs/heads/…` and `refs/remotes/origin/HEAD`),
last commit time (`.git/logs/HEAD`), README title (first heading), `.env*` **names**, top-level layout,
dev-server ports from scripts (`--port`, `-p`, `PORT=`).

## What it refuses to guess

- **No `npm test` without a `scripts.test`.** A package.json without one yields a note: "no test command".
- **No package manager by fashion.** The lockfile decides; no lockfile means npm and the reason says so.
- **No ports by convention.** A dev server's port appears only if a script, compose file, or `[project.scripts]`
  entry states it. `localhost:3000` is not "detected" from a `vite` dependency.
- **No `make` target that is not there**, and no mapping of arbitrary targets (`deploy`) to a task.
- **No install command for a bare venv.** A python folder with a `.venv` and no manifest gets a test
  command and an explicit "no install command: no dependency manifest" note.
- **No `.env` contents.** Names are listed so the agent knows a `.env` is expected; values are never opened,
  and `tests/test_safety.py` asserts the secret never reaches the database.
- **No `git` runs.** Branch and commit come from files; `.git/HEAD` is read as text.
- **No test for a `tests/` folder alone** unless a pytest marker, dependency, `conftest.py` or installed
  `pytest` binary backs it.

## Limits

- Scan depth is 3 below each root (configurable 1–8), project cap 500 (`max_projects`), and one walk
  is bounded by `scan_seconds` (default 6). A truncated scan is reported as partial — in `project_refresh`,
  `/atlas status`, and the demo — instead of pretending to be complete.
- Symlinks are never followed, so loops cannot hang a walk; symlinked project directories are skipped.
- `node_modules`, `.venv`, `venv`, `dist`, `build`, `target`, `.git`, `vendor`, `__pycache__`, `.next`,
  `.cache`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.svelte-kit`, `.turbo`, `.gradle` are
  never descended into and never treated as projects. Hidden directories are walked but queued last, so a
  time budget is spent on `~/code` before `~/.cache`.
- "Default branch" is `refs/remotes/origin/HEAD` when that symref file exists, else the branch `HEAD` points
  at. On a repo that was never cloned from a remote this is the current branch, not GitHub's idea of default.
- Workspaces/monorepos: each package with its own manifest is indexed as its own project when the walk
  reaches it; a root `package.json` with `workspaces` is noted, not expanded.
- The session card reflects the index as of detection. It is refreshed by `project_refresh`, `/atlas refresh`,
  or a lookup after `cache_ttl_minutes` (default 60) — not mid-session.

## Privacy and disclosure

Atlas reads marker files under the configured roots (the list is in `plugin.yaml`'s description), directory
names for the layout summary, `.env*` **names only**, and `.git/HEAD` + `.git/logs/HEAD`. It writes exactly
one SQLite database at `<HERMES_HOME>/plugin-data/atlas/index.db`. It executes no project code, no `git`, and
no package manager. It makes no network calls, has no account, no key, and no telemetry. Scanning is read-only: `tests/test_safety.py` snapshots every file's size, mtime and hash before and after a scan and
requires them to be identical.

## Development

```sh
# tests (uses the reference harness interpreter; any Python >= 3.11 works)
PYTHONPATH=/root/candidates/atlas:/root/.hermes/hermes-agent \
  /root/peripheral/.venv/bin/python -m pytest -q
# 79 passed  (timing varies with the host)

# catalog admission gate (static manifest checks + isolated register() probe)
hermes plugins validate /root/candidates/atlas
# ✓ manifest — plugin.yaml parses
# ...
# Validation passed.

# the same plugin through the real discovery/register path
hermes plugins doctor /root/candidates/atlas
# OK: runtime discovery, manifest parsing, import, and registration passed
# registrations: 4 tool(s), 0 hook(s)

# read-only demo: scans /root and prints two real cards + the session card
/root/peripheral/.venv/bin/python scripts/demo.py
```

### Demo output (real, trimmed to the interesting parts)

```
atlas demo · HERMES_HOME=…/atlas-demo-xywdndt_ (throwaway)
scanning /root — read-only, no project code executed

scan report
===========
roots:            /root
directories:      494
projects found:   34 (index holds 34)
wall clock:       0.052s
budget reached:   False  (time/project cap; the walk is bounded)

project_info — /root/peripheral
===============================
peripheral — python (uv)
  path      /root/peripheral
  manager   uv (.venv/pyvenv.cfg created by uv)
  markers   .venv/pyvenv.cfg
  branch    master · last commit 2026-11-05 12:52:19 UTC
  readme    Peripheral — Hermes gets a memory of what you've seen
  layout    docs/, out/, scripts/, skills/, tests/, .gitignore, __init__.py, catalog-entry.yaml (+4 more)
  commands
    test      /root/peripheral/.venv/bin/python -m pytest   # pytest installed in the project venv
  notes
    - no install command: no dependency manifest at the project root

project_info — /root/.hermes/hermes-agent
=========================================
hermes-agent — python (uv) + node (npm) + docker-compose
  path      /root/.hermes/hermes-agent
  manager   uv (uv.lock)
  markers   pyproject.toml, setup.py, uv.lock, package.json, docker-compose.yml
  branch    main · last commit 2026-10-31 10:32:26 UTC
  readme    Hermes Agent ☤
  env files .env.example, .envrc (names only — contents never read)
  services  gateway, dashboard
  layout    .github/, .hermes/, acp_adapter/, agent/, apps/, assets/, contributors/, cron/ (+4 more)
  commands
    install   uv sync   # uv.lock present
    test      uv run pytest   # pyproject.toml [tool.pytest.ini_options]
    lint      uv run ruff check .   # ruff config in pyproject.toml/ruff.toml
    build     uv build   # pyproject.toml [build-system]
    run       hermes   # pyproject.toml [project.scripts] entry 'hermes'
…
```

Note the `peripheral` card: no `pyproject.toml` exists there, only a uv-created `.venv`. Atlas says uv,
derives the test command through the venv interpreter instead of inventing `uv run` (which would fail
without a project), and states that no install command can be derived.

## Files

```
plugin.yaml      manifest v2 (tools, config schema, disclosure)
__init__.py      register(): 4 tools, /atlas, the after_memory prompt section
settings.py      config resolution and default roots
detect.py        marker-file detection: readers, detectors, bounded scan
store.py         SQLite index, TTL refresh, cwd → project resolution
tools.py         handlers + card/list/prompt rendering
commands.py      /atlas
schemas.py       tool schemas
tests/           detection, store, tools, prompt section, read-only guarantees, manifest contract
scripts/demo.py  read-only scan of /root + real cards
catalog-entry.yaml
```

## License

MIT.
