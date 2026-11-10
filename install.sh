#!/usr/bin/env bash
# ==============================================================================
# AiHead-Cockpit & AiHead-Orchestrator Skills Installer
# ==============================================================================
# Sets up the virtual environment for AiHead-Cockpit, installs dependencies,
# and configures the essential Herdr-Master / AiHead standalone skills across AI harnesses.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

# Text styles
BOLD="\033[1m"
GREEN="\033[32m"
CYAN="\033[36m"
YELLOW="\033[33m"
RED="\033[31m"
DIM="\033[2m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}🧠 AiHead-Cockpit Installer & Skills Setup${RESET}"
echo -e "${DIM}Repository: ${REPO_ROOT}${RESET}\n"

# ------------------------------------------------------------------------------
# 1. Detect Python & Package Manager (uv preferred)
# ------------------------------------------------------------------------------
HAS_UV=false
if command -v uv >/dev/null 2>&1; then
    HAS_UV=true
    echo -e "${GREEN}✓${RESET} Found ${BOLD}uv${RESET} package manager ($(uv --version))"
else
    echo -e "${YELLOW}!${RESET} uv not found in PATH; falling back to standard python3/pip"
fi

if ! command -v python3 >/dev/null 2>&1 && [ "$HAS_UV" = false ]; then
    echo -e "${RED}Error: Neither 'uv' nor 'python3' was found in PATH.${RESET}" >&2
    echo -e "Please install Python 3.10+ or uv (https://docs.astral.sh/uv/) and re-run." >&2
    exit 1
fi

# ------------------------------------------------------------------------------
# 2. Virtual Environment Setup
# ------------------------------------------------------------------------------
VENV_DIR="${REPO_ROOT}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"

if [ ! -d "$VENV_DIR" ]; then
    echo -e "\n${BOLD}Creating virtual environment at .venv...${RESET}"
    if [ "$HAS_UV" = true ]; then
        uv venv "$VENV_DIR"
    else
        python3 -m venv "$VENV_DIR"
    fi
    echo -e "${GREEN}✓${RESET} Virtual environment created"
else
    echo -e "${GREEN}✓${RESET} Using existing virtual environment at ${DIM}${VENV_DIR}${RESET}"
fi

# ------------------------------------------------------------------------------
# 3. Install AiHead-Orchestrator & AiHead-Cockpit dependencies
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}Installing dependencies...${RESET}"

# Check for sibling AiHead-Orchestrator or herdr-master repository
SIBLING_ORCH=""
if [ -d "${REPO_ROOT}/../AiHead-Orchestrator" ] && [ -f "${REPO_ROOT}/../AiHead-Orchestrator/pyproject.toml" ]; then
    SIBLING_ORCH="${REPO_ROOT}/../AiHead-Orchestrator"
elif [ -d "${REPO_ROOT}/../herdr-master" ] && [ -f "${REPO_ROOT}/../herdr-master/pyproject.toml" ]; then
    SIBLING_ORCH="${REPO_ROOT}/../herdr-master"
fi

if [ "$HAS_UV" = true ]; then
    # Ensure pip tools exist in venv if needed
    if [ -n "$SIBLING_ORCH" ]; then
        echo -e "${GREEN}✓${RESET} Found local AiHead-Orchestrator checkout at ${DIM}${SIBLING_ORCH}${RESET}"
        uv pip install --python "$VENV_PYTHON" -e "$SIBLING_ORCH"
    else
        echo -e "${YELLOW}!${RESET} Local AiHead-Orchestrator checkout not found; installing from GitHub..."
        uv pip install --python "$VENV_PYTHON" "git+https://github.com/pank85/AiHead-Orchestrator.git" || true
    fi

    echo -e "Installing ${BOLD}aiheaddev${RESET} (editable)..."
    uv pip install --python "$VENV_PYTHON" -e "$REPO_ROOT"
else
    if [ -n "$SIBLING_ORCH" ]; then
        echo -e "${GREEN}✓${RESET} Found local AiHead-Orchestrator checkout at ${DIM}${SIBLING_ORCH}${RESET}"
        "$VENV_DIR/bin/pip" install -e "$SIBLING_ORCH"
    else
        echo -e "${YELLOW}!${RESET} Local AiHead-Orchestrator checkout not found; installing from GitHub..."
        "$VENV_DIR/bin/pip" install "git+https://github.com/pank85/AiHead-Orchestrator.git" || true
    fi

    echo -e "Installing ${BOLD}aiheaddev${RESET} (editable)..."
    "$VENV_DIR/bin/pip" install -e "$REPO_ROOT"
fi

echo -e "${GREEN}✓${RESET} Dependencies successfully installed"

# ------------------------------------------------------------------------------
# 4. Install & Link Herdr-Master Standalone Skills across Agent Harnesses
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}Installing standalone skills across AI agent harnesses...${RESET}"

if "$VENV_PYTHON" -c "import herdr_master" >/dev/null 2>&1; then
    "$VENV_PYTHON" -m herdr_master.cli.main skills install "$@"
else
    echo -e "${RED}Warning: 'herdr_master' module could not be loaded; skipping skill linking.${RESET}" >&2
    echo -e "Please ensure AiHead-Orchestrator is installed: pip install -e ../AiHead-Orchestrator" >&2
fi

# ------------------------------------------------------------------------------
# 5. Summary & Next Steps
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}${GREEN}==============================================================================${RESET}"
echo -e "${BOLD}${GREEN}🎉 Setup Complete! AiHead-Cockpit & Skills are ready.${RESET}"
echo -e "${BOLD}${GREEN}==============================================================================${RESET}\n"

echo -e "To launch the AiHead-Cockpit:"
echo -e "  ${BOLD}${CYAN}source .venv/bin/activate && aiheaddev${RESET}"
echo -e "  or directly:"
echo -e "  ${BOLD}${CYAN}${VENV_DIR}/bin/aiheaddev${RESET}\n"

echo -e "Available agent slash commands in ${BOLD}agy${RESET}, ${BOLD}pi${RESET}, ${BOLD}claude${RESET}, and ${BOLD}opencode${RESET}:"
echo -e "  • ${CYAN}/aihead${RESET}        — Interactive AI Technical Director & Decision-Maker"
echo -e "  • ${CYAN}/herdr-master${RESET}  — Master coordinator for parallel tasks across worktrees"
echo -e "  • ${CYAN}/ergo${RESET}          — Multi-harness cross-session implementation pipeline"
echo -e "  • ${CYAN}/orchestration${RESET} — Single-harness native subagent pipeline"
echo -e "  • ${CYAN}/council${RESET}        — Multi-agent AI Council consensus deliberation\n"
