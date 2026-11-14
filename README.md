# 🧠 AiHead-Cockpit (`aiheaddev`)

**AiHead-Cockpit** is an autonomous AI-driven orchestration cockpit with a native Rust terminal interface, powered by [AiHead-Orchestrator](https://github.com/pank85/AiHead-Orchestrator).

---

## 🌟 Overview

AiHead-Cockpit provides an interactive terminal cockpit for developers and autonomous agent squads:
* **Terminal Cockpit**: Monitor multi-agent sessions, active workspaces, and background tasks in real time.
* **Orchestration Launchers**: Quick launch workflows for `/orchestration` (single-harness), `/ergo` (cross-harness), `/council` (multi-agent consensus), and `/herdr-master` (multi-task coordination).
* **AI Head Decision Support**: Dedicated AI Head tab and supervisor loop to triage questions, resolve architectural decisions, and escalate when needed.
* **Settings & Squad Editor**: Intuitive 5-tab settings management with per-harness orchestration persistence, favorites, and squad topology editors.

---

## 🚀 Installation & Quick Start

Platform wheels bundle the native executable and do not require Rust at runtime. Source, sdist and editable installs require Cargo and Rust 1.85.1 or newer, plus a working native build toolchain. If the bundled executable is missing or damaged, reinstall the wheel or rebuild the source installation; there is no Textual fallback.

### 1-Step Quick Install (Recommended)

Run the automated install script to configure the virtual environment, install dependencies, and automatically link all required **AiHead-Orchestrator & AiHead standalone skills** across your AI agent harnesses (`agy`, `pi`, `claude`, `opencode`):

```bash
git clone https://github.com/pank85/AiHead-Cockpit.git
cd AiHead-Cockpit
./install.sh
```

### Manual Installation

```bash
# Using uv (recommended)
uv pip install aiheaddev

# Or with pip
pip install aiheaddev

# Configure the AiHead-Orchestrator & AiHead standalone skills across harnesses
aiheaddev --install-skills
```

### Development Setup

```bash
git clone https://github.com/pank85/AiHead-Cockpit.git
cd AiHead-Cockpit
./install.sh
```

---

## 🕹️ Usage

Launch the cockpit directly:

```bash
aiheaddev
# or using the shorthand
aihead
```

Launch with a specific Herdr session or in demo mode:

```bash
aiheaddev --session my-workspace
aiheaddev --demo
```

---

## 🔗 Architecture & Relationship with `AiHead-Orchestrator`

AiHead-Cockpit acts as the user interface layer that consumes `AiHead-Orchestrator` (`herdr-master`):
* **`AiHead-Orchestrator`** (`herdr-master`) provides core execution runtime, catalog resolution, launcher protocols, and skills (`/ergo`, `/orchestration`, `/council`, `/herdr-master`).
* **`AiHead-Cockpit`** uses a Rust terminal frontend with a Python bridge for orchestration, settings and existing configuration locations. Both `aihead` and `aiheaddev` use the same entrypoint; `--session`, `--demo` and `--install-skills` remain available, and skill installation does not start the native UI.

The cockpit cannot reliably detect Herdr permission-dialog readiness. Use **Open in Herdr** to interact with permission prompts directly; do not assume an automated reply was accepted. If the bridge or terminal fails, exit and restart the cockpit; existing workspaces and configuration are not removed.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
