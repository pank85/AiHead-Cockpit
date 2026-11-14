# Rust cockpit offline and packaging verification

Run in the worktree, never against an active Herdr session. Prerequisites: Python 3 with `venv`, project `.venv` with test dependencies, `uv` (for isolated Hatchling builds and installs), network or populated package cache for declared build/runtime dependencies, and Rust/Cargo **1.85.1 or newer** with rustfmt and clippy. Missing tools, build dependencies, or failed installations are failures, not skips. Build/install may access a package index; native runtime and PTY scenarios use a synthetic HOME and no live Herdr server.

`herdr-master>=0.1.0` is not available from the configured public index in this environment. For verification only, the runner copies the authorized AiHead-Orchestrator source inputs into its temporary directory, builds a wheel from the copy, and installs it into each test venv. It does not install the canonical source editably or change the published dependency. By default the copy source is `/home/pank/projects/AiHead-Orchestrator`; override with `COCKPIT_HERDR_SOURCE` when using a different authorized source. The original source is read only.

The runner creates a unique directory under `.orchestration/` and a separate short `/tmp/t6-*` guarded-suite parent (needed for Unix socket path limits), prints its HOME, XDG, cwd, temp, tool and source paths, uses a whitelist environment without HERDR/HERDM variables, and removes both directories on exit. It snapshots the approved config and ancestor config paths and synthetic canaries. It builds source, editable, wheel and sdist installs; exercises both commands with `--help`, `--install-skills` and a PTY `--demo` from another cwd; verifies artifact contents and executable mode, and removes Cargo from runtime PATH. Its PTY fixture never opens sockets, tests Ctrl+C, bridge crash, malformed wire and leaked descendant cases, and kills test process groups. The guarded Python suite receives Hatchling from a temporary isolated target, so its real archive regression is not silently skipped.

Exact gates (from the worktree root):

```sh
export RUN_DIR="$PWD/.orchestration/ergo-rust-cockpit-20261113"
export RUSTUP_HOME="$RUN_DIR/rust-toolchain/rustup"
export CARGO_HOME="$RUN_DIR/rust-toolchain/cargo"
export CARGO_TARGET_DIR="$RUN_DIR/rust-target"
export PATH="$CARGO_HOME/bin:$PATH"
cargo fmt --manifest-path rust/cockpit/Cargo.toml --all -- --check
cargo test --locked --manifest-path rust/cockpit/Cargo.toml --all-targets
cargo clippy --locked --manifest-path rust/cockpit/Cargo.toml --all-targets -- -D warnings
p=$(mktemp -d /tmp/t6-XXXXXX)
UI_SANDBOX_RUNTIME_PARENT="$p" .venv/bin/python -B tests/ui_sandbox.py -- tests
status=$?; rm -rf -- "$p"; test "$status" -eq 0
python3 scripts/verify-rust-cockpit.py --all
sha256sum .herdr-master.toml
```

Expected approved hash: `14d1b988ca6fb21f56834e0de6739254831c698f8fa174c9a12a7158d281add2`. The standalone guarded command may explicitly report its real Hatchling test skipped if Hatchling is not installed in `.venv`; that skip is not a pass. The acceptance runner installs Hatchling into its temporary test target and must run the test. Optional visual inspection is read-only and requires an authorized session; never change agents, layouts or configuration for verification.
