"""Compact quick-launch toolbar for the primary orchestration actions."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static


class QuickLaunchBar(Static):
    """Compact launch actions with distinct styling, clear focus rings, and clean labels."""

    DEFAULT_CSS = """
    QuickLaunchBar {
        height: 4;
        padding: 0;
        background: #161E2E;
    }
    QuickLaunchBar .launch-container {
        height: 4;
    }
    QuickLaunchBar .launch-action {
        width: 1fr;
        height: 4;
    }
    QuickLaunchBar .launch-description {
        height: 1;
        text-align: center;
        color: #94A3B8;
        background: #161E2E;
    }
    QuickLaunchBar Button {
        width: 1fr;
        min-width: 0;
        margin: 0;
        height: 3;
        border: none;
        text-style: bold;
    }
    QuickLaunchBar #btn-new-master {
        background: #0284C7;
        color: #F8FAFC;
    }
    QuickLaunchBar #btn-new-master:hover {
        background: #0369A1;
    }
    QuickLaunchBar #btn-new-master:focus {
        background: #0284C7;
        border-top: solid #38BDF8;
        border-bottom: solid #38BDF8;
        color: #FFFFFF;
    }

    QuickLaunchBar #btn-new-ergo {
        background: #059669;
        color: #F8FAFC;
    }
    QuickLaunchBar #btn-new-ergo:hover {
        background: #047857;
    }
    QuickLaunchBar #btn-new-ergo:focus {
        background: #059669;
        border-top: solid #34D399;
        border-bottom: solid #34D399;
        color: #FFFFFF;
    }

    QuickLaunchBar #btn-new-orchestration {
        background: #7C3AED;
        color: #F8FAFC;
    }
    QuickLaunchBar #btn-new-orchestration:hover {
        background: #6D28D9;
    }
    QuickLaunchBar #btn-new-orchestration:focus {
        background: #7C3AED;
        border-top: solid #A78BFA;
        border-bottom: solid #A78BFA;
        color: #FFFFFF;
    }

    QuickLaunchBar #btn-new-council {
        background: #4F46E5;
        color: #F8FAFC;
    }
    QuickLaunchBar #btn-new-council:hover {
        background: #4338CA;
    }
    QuickLaunchBar #btn-new-council:focus {
        background: #4F46E5;
        border-top: solid #818CF8;
        border-bottom: solid #818CF8;
        color: #FFFFFF;
    }

    QuickLaunchBar #btn-settings {
        background: #334155;
        color: #F8FAFC;
    }
    QuickLaunchBar #btn-settings:hover {
        background: #475569;
    }
    QuickLaunchBar #btn-settings:focus {
        background: #334155;
        border-top: solid #94A3B8;
        border-bottom: solid #94A3B8;
        color: #FFFFFF;
    }
    """

    class LaunchMasterRequested(Message):
        """User requested to launch a new Herdr Master."""

    class LaunchErgoRequested(Message):
        """User requested to launch a new Ergo Pipeline."""

    class LaunchOrchestrationRequested(Message):
        """User requested to launch a single-harness Orchestration pipeline."""

    class LaunchCouncilRequested(Message):
        """User requested to launch an AI Council deliberation run."""

    class OpenSettingsRequested(Message):
        """User requested to configure squad/settings."""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="launch-container"):
            with Vertical(classes="launch-action"):
                yield Button(
                    "New Master",
                    id="btn-new-master",
                    variant="primary",
                    tooltip="[n] Coordinate multiple independent jobs in isolated Git worktrees.",
                )
                yield Static("Coordinate ongoing jobs", classes="launch-description")
            with Vertical(classes="launch-action"):
                yield Button(
                    "New Ergo",
                    id="btn-new-ergo",
                    variant="success",
                    tooltip="[e] Run one job through Scout, Architect, Coders, and Reviewer.",
                )
                yield Static("Run one planned pipeline", classes="launch-description")
            with Vertical(classes="launch-action"):
                yield Button(
                    "Orchestrate",
                    id="btn-new-orchestration",
                    variant="success",
                    tooltip="[o] Run Scout -> Architect -> Coders -> Reviewer with subagents in a single harness.",
                )
                yield Static("Single-harness subagents", classes="launch-description")
            with Vertical(classes="launch-action"):
                yield Button(
                    "AI Council",
                    id="btn-new-council",
                    variant="primary",
                    tooltip="Start an AI Council deliberation run.",
                )
                yield Static("Deliberate consensus plan", classes="launch-description")
            with Vertical(classes="launch-action"):
                yield Button(
                    "Settings",
                    id="btn-settings",
                    variant="warning",
                    tooltip="[s/c] Configure projects, squads, council, and persona.",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-new-master":
            self.post_message(self.LaunchMasterRequested())
        elif event.button.id == "btn-new-ergo":
            self.post_message(self.LaunchErgoRequested())
        elif event.button.id == "btn-new-orchestration":
            self.post_message(self.LaunchOrchestrationRequested())
        elif event.button.id == "btn-new-council":
            self.post_message(self.LaunchCouncilRequested())
        elif event.button.id == "btn-settings":
            self.post_message(self.OpenSettingsRequested())
