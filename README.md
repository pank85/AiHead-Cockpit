# 🧠 AiHeadDev

**AiHeadDev** is an autonomous AI-driven orchestration cockpit and terminal user interface (TUI) built on [Textual](https://github.com/Textualize/textual) and powered by [herdr-master](https://github.com/pank85/herdr-master).

---

## 🌟 Overview

AiHeadDev provides an interactive terminal cockpit for developers and autonomous agent squads:
* **Terminal Cockpit**: Monitor multi-agent sessions, active workspaces, and background tasks in real time.
* **Orchestration Launchers**: Quick launch workflows for `/orchestration` (single-harness), `/ergo` (cross-harness), `/council` (multi-agent consensus), and `/herdr-master` (multi-task coordination).
* **AI Head Decision Support**: Dedicated AI Head tab and supervisor loop to triage questions, resolve architectural decisions, and escalate when needed.
* **Settings & Squad Editor**: Intuitive 5-tab settings management with per-harness orchestration persistence, favorites, and squad topology editors.

---

## 🚀 Installation

```bash
# Using uv (recommended)
uv pip install aiheaddev

# Or with pip
pip install aiheaddev
```

### Development Setup

```bash
git clone https://github.com/pank85/AiHeadDev.git
cd AiHeadDev
uv sync --all-extras
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

## 🔗 Architecture & Relationship with `herdr-master`

AiHeadDev acts as the user interface layer that consumes `herdr-master`:
* **`herdr-master`** provides core execution runtime, catalog resolution, launcher protocols, and skills (`/ergo`, `/orchestration`, `/council`, `/herdr-master`).
* **`AiHeadDev`** provides the interactive dashboard, terminal widgets, modals, live streaming output, and visual cockpit.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
