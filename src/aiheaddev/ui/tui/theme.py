"""Visual styling and color palette tokens for Herdr-Master TUI.

Based on ui-ux-pro-max design system:
- Deep slate palette (OLED eye-friendly #0B0F19 base, #161E2E cards, #1E293B panels)
- High contrast WCAG-compliant text (#F8FAFC foreground, #94A3B8 secondary, #64748B muted)
- Semantic state badges with vibrant accents and clean unicode icons
"""

from herdr_master.core.models import WorkerState

# --- UI/UX Pro Max Deep Slate Palette Tokens ---
BG_DARKEST = "#0B0F19"
SURFACE_CARD = "#161E2E"
PANEL_BG = "#1E293B"
BORDER_SUBTLE = "#334155"
BORDER_FOCUS = "#38BDF8"

# High-Contrast Text Tokens
TEXT_PRIMARY = "#F8FAFC"
TEXT_SECONDARY = "#94A3B8"
TEXT_MUTED = "#64748B"

# Semantic Accent Tokens
ACCENT_CYAN = "#38BDF8"
ACCENT_EMERALD = "#10B981"
ACCENT_VIOLET = "#A855F7"
ACCENT_AMBER = "#F59E0B"
ACCENT_ROSE = "#EF4444"
ACCENT_BLUE = "#3B82F6"

# Backwards Compatibility Aliases
PRIMARY_ACCENT = ACCENT_CYAN
SECONDARY_ACCENT = ACCENT_VIOLET
BACKGROUND_DARK = BG_DARKEST
SURFACE_DARK = SURFACE_CARD
PANEL_BORDER = BORDER_SUBTLE

# State Badge Colors (WCAG 4.5:1+ against dark slate)
STATE_COLORS = {
    WorkerState.SPAWNING: "#38BDF8",     # Sky 400
    WorkerState.IDLE: "#10B981",         # Emerald 500
    WorkerState.WORKING: "#06B6D4",      # Cyan 500
    WorkerState.BLOCKED: "#F59E0B",      # Amber 500
    WorkerState.DONE: "#3B82F6",         # Blue 500
    WorkerState.NEEDS_REVIEW: "#F43F5E", # Rose 500
    WorkerState.FIXING: "#FB923C",       # Orange 400
    WorkerState.APPROVED: "#60A5FA",     # Blue 400
    WorkerState.SQUASHING: "#8B5CF6",    # Violet 500
    WorkerState.LANDED: "#64748B",       # Slate 500
    WorkerState.FAILED: "#EF4444",       # Red 500
}

# Clean Unicode State Icons
STATE_ICONS = {
    WorkerState.SPAWNING: "○",
    WorkerState.IDLE: "●",
    WorkerState.WORKING: "◐",
    WorkerState.BLOCKED: "▲",
    WorkerState.DONE: "✔",
    WorkerState.NEEDS_REVIEW: "◆",
    WorkerState.FIXING: "⚡",
    WorkerState.APPROVED: "★",
    WorkerState.SQUASHING: "⇥",
    WorkerState.LANDED: "✔",
    WorkerState.FAILED: "✖",
}


def badge_markup(state: WorkerState) -> str:
    """Format a styled Rich markup badge for a given WorkerState."""
    color = STATE_COLORS.get(state, "#F8FAFC")
    icon = STATE_ICONS.get(state, "•")
    return f"[{color}]{icon} {state.value}[/{color}]"
