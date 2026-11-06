"""Pure role-evidence, socket-scoping, and widget-label tests for workspace badges."""

import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

from herdr_master.core.models import HarnessKind, MasterSession
from herdr_master.herdr.discovery import SessionInventory
from herdr_master.herdr.snapshot import SessionSnapshot
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.widgets.workspaces import (
    WorkspaceBrowser,
    known_master_panes_for_workspace,
    workspace_roles,
)


def make_inventory(
    name: str = "test-session",
    socket_path: Path = Path("/test/herdr.sock"),
    workspaces=None,
    tabs=None,
    panes=None,
    agents=None,
    error: str | None = None,
) -> SessionInventory:
    snapshot = None
    if workspaces is not None or tabs is not None or panes is not None or agents is not None:
        snapshot = SessionSnapshot(
            protocol=22,
            workspaces=workspaces or [],
            tabs=tabs or [],
            panes=panes or [],
            agents=agents or [],
        )
    return SessionInventory(
        session_name=name,
        socket_path=socket_path,
        snapshot=snapshot,
        error=error,
    )


# =============================================================================
# Pure workspace_roles helper tests
# =============================================================================


def test_no_snapshot_produces_no_roles():
    inv = SessionInventory(
        session_name="offline",
        socket_path=Path("/test.sock"),
        snapshot=None,
        error="Socket closed",
    )
    assert workspace_roles(inv, "w1") == ()


def test_unknown_workspace_produces_no_roles():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1", "label": "Project 1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "master-coordinator", "agent": "pi"}
        ],
    )
    assert workspace_roles(inv, "unknown-ws") == ()


def test_empty_workspace_produces_no_roles():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1", "label": "Empty WS"}],
        panes=[],
        agents=[],
    )
    assert workspace_roles(inv, "w1") == ()


def test_master_only_agent_name():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "master-coordinator", "agent": "pi"}
        ],
    )
    assert workspace_roles(inv, "w1") == ("Master",)


def test_ergo_only_agent_name():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "ergo-orchestrator", "agent": "agy"}
        ],
    )
    assert workspace_roles(inv, "w1") == ("Ergo",)


def test_both_roles_in_same_workspace():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "ergo-orchestrator", "agent": "agy"},
            {"pane_id": "p2", "workspace_id": "w1", "name": "master-coordinator", "agent": "pi"},
        ],
    )
    # Master must precede Ergo regardless of agent discovery order
    assert workspace_roles(inv, "w1") == ("Master", "Ergo")


def test_duplicates_produce_each_role_at_most_once():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "master-coordinator", "agent": "pi"},
            {"pane_id": "p2", "workspace_id": "w1", "name": "master-coordinator", "agent": "pi"},
            {"pane_id": "p3", "workspace_id": "w1", "name": "ergo-orchestrator", "agent": "claude"},
            {"pane_id": "p4", "workspace_id": "w1", "name": "ergo-orchestrator", "agent": "claude"},
        ],
    )
    roles = workspace_roles(inv, "w1")
    assert roles == ("Master", "Ergo")
    assert roles.count("Master") == 1
    assert roles.count("Ergo") == 1


def test_case_and_whitespace_normalization():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "name": "  Master-Coordinator \n",
                "agent": "pi",
            },
            {
                "pane_id": "p2",
                "workspace_id": "w1",
                "name": "\tERGO-ORCHESTRATOR  ",
                "agent": "agy",
            },
        ],
    )
    assert workspace_roles(inv, "w1") == ("Master", "Ergo")


def test_unnamed_agent_canonical_tab_fallback():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        tabs=[
            {"tab_id": "t1", "workspace_id": "w1", "label": "  Master-Coordinator  "},
            {"tab_id": "t2", "workspace_id": "w1", "label": "ERGO-ORCHESTRATOR"},
        ],
        panes=[
            {"pane_id": "p1", "tab_id": "t1", "agent": "pi"},
            {"pane_id": "p2", "tab_id": "t2", "agent": "claude"},
        ],
        agents=[
            {"pane_id": "p1", "name": None, "agent": "pi"},
            {"pane_id": "p2", "name": "   ", "agent": "claude"},
        ],
    )
    assert workspace_roles(inv, "w1") == ("Master", "Ergo")


def test_unnamed_agent_foreign_workspace_tab_rejected():
    # R4 regression: tab belonging to foreign workspace must not supply role evidence
    inv = make_inventory(
        workspaces=[{"workspace_id": "ws"}, {"workspace_id": "other"}],
        tabs=[
            {"tab_id": "now", "workspace_id": "other", "label": "master-coordinator"},
            {"tab_id": "now-ergo", "workspace_id": "other", "label": "ergo-orchestrator"},
        ],
        panes=[
            {"pane_id": "p1", "tab_id": "now", "workspace_id": "ws", "agent": "pi"},
            {"pane_id": "p2", "tab_id": "now-ergo", "workspace_id": "ws", "agent": "agy"},
        ],
        agents=[
            {"pane_id": "p1", "workspace_id": "ws", "name": None, "agent": "pi"},
            {"pane_id": "p2", "workspace_id": "ws", "name": None, "agent": "agy"},
        ],
    )
    # ws has unnamed agents pointing to foreign tabs: rejected
    assert workspace_roles(inv, "ws") == ()
    # other has no agents: empty
    assert workspace_roles(inv, "other") == ()


def test_unnamed_agent_unestablished_or_absent_tab_rejected():
    # Tabs without workspace ownership or missing tabs must be conservatively rejected
    inv_unestablished = make_inventory(
        workspaces=[{"workspace_id": "ws"}],
        tabs=[
            {"tab_id": "t1", "workspace_id": None, "label": "master-coordinator"},
            {"tab_id": "t2", "workspace_id": None, "label": "ergo-orchestrator"},
        ],
        panes=[
            {"pane_id": "p1", "tab_id": "t1", "workspace_id": "ws", "agent": "pi"},
            {"pane_id": "p2", "tab_id": "t2", "workspace_id": "ws", "agent": "claude"},
        ],
        agents=[
            {"pane_id": "p1", "workspace_id": "ws", "name": None, "agent": "pi"},
            {"pane_id": "p2", "workspace_id": "ws", "name": None, "agent": "claude"},
        ],
    )
    assert workspace_roles(inv_unestablished, "ws") == ()

    inv_missing_tab = make_inventory(
        workspaces=[{"workspace_id": "ws"}],
        tabs=[],
        panes=[{"pane_id": "p1", "tab_id": "t-missing", "workspace_id": "ws", "agent": "pi"}],
        agents=[{"pane_id": "p1", "workspace_id": "ws", "name": None, "agent": "pi"}],
    )
    assert workspace_roles(inv_missing_tab, "ws") == ()

    inv_no_tab_id = make_inventory(
        workspaces=[{"workspace_id": "ws"}],
        tabs=[{"tab_id": "t1", "workspace_id": "ws", "label": "master-coordinator"}],
        panes=[{"pane_id": "p1", "workspace_id": "ws", "agent": "pi"}],
        agents=[{"pane_id": "p1", "workspace_id": "ws", "name": None, "agent": "pi"}],
    )
    assert workspace_roles(inv_no_tab_id, "ws") == ()


def test_unnamed_agent_contradictory_pane_ownership_rejected():
    # If pane has contradictory workspace or tab relationship in snapshot, reject
    inv_contradictory_ws = make_inventory(
        workspaces=[{"workspace_id": "ws"}, {"workspace_id": "other"}],
        tabs=[{"tab_id": "t1", "workspace_id": "ws", "label": "master-coordinator"}],
        panes=[{"pane_id": "p1", "tab_id": "t1", "workspace_id": "other", "agent": "pi"}],
        agents=[{"pane_id": "p1", "workspace_id": "ws", "name": None, "agent": "pi"}],
    )
    assert workspace_roles(inv_contradictory_ws, "ws") == ()

    inv_contradictory_tab = make_inventory(
        workspaces=[{"workspace_id": "ws"}],
        tabs=[{"tab_id": "t1", "workspace_id": "ws", "label": "master-coordinator"}],
        panes=[{"pane_id": "p1", "tab_id": "t-other", "workspace_id": "ws", "agent": "pi"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "ws",
                "tab_id": "t1",
                "name": None,
                "agent": "pi",
            }
        ],
    )
    assert workspace_roles(inv_contradictory_tab, "ws") == ()


def test_named_agent_does_not_use_tab_fallback():
    # If the agent has an explicit name that does not match, tab label is ignored
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        tabs=[{"tab_id": "t1", "workspace_id": "w1", "label": "master-coordinator"}],
        panes=[{"pane_id": "p1", "tab_id": "t1", "agent": "pi"}],
        agents=[{"pane_id": "p1", "name": "Builder", "agent": "pi"}],
    )
    assert workspace_roles(inv, "w1") == ()


def test_ordinary_terminals_with_misleading_labels_do_not_establish_role():
    # Ordinary shell panes (no agent) must not infer role even if tab is named canonical
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        tabs=[
            {"tab_id": "t1", "workspace_id": "w1", "label": "master-coordinator"},
            {"tab_id": "t2", "workspace_id": "w1", "label": "ergo-orchestrator"},
        ],
        panes=[
            {"pane_id": "p1", "tab_id": "t1", "agent": None},
            {"pane_id": "p2", "tab_id": "t2"},
        ],
    )
    assert workspace_roles(inv, "w1") == ()


def test_generic_pipeline_role_names_do_not_establish_role():
    generic_names = [
        "Scout",
        "Architect",
        "Reviewer",
        "Coder",
        "coder-1",
        "master",
        "ergo",
        "master-worker",
        "ergo-runner",
    ]
    for name in generic_names:
        inv = make_inventory(
            workspaces=[{"workspace_id": "w1"}],
            agents=[{"pane_id": "p1", "workspace_id": "w1", "name": name, "agent": "pi"}],
        )
        assert workspace_roles(inv, "w1") == (), f"Agent named {name} should not establish role"


def test_misleading_workspace_session_cwd_branch_ignored():
    inv = make_inventory(
        name="master-coordinator",
        workspaces=[{"workspace_id": "w1", "label": "master-coordinator"}],
        panes=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "agent": "pi",
                "cwd": "/home/user/master-coordinator",
            }
        ],
        agents=[{"pane_id": "p1", "workspace_id": "w1", "name": "custom-worker", "agent": "pi"}],
    )
    assert workspace_roles(inv, "w1") == ()


def test_custom_master_from_known_master_panes():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "my-special-master", "agent": "pi"}
        ],
    )
    assert workspace_roles(inv, "w1", known_master_panes=frozenset(["p1"])) == ("Master",)


def test_known_master_panes_requires_present_agent_bearing_pane():
    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        panes=[{"pane_id": "p-term", "workspace_id": "w1", "agent": None}],
        agents=[],
    )
    # p-term is an ordinary terminal pane (not agent-bearing)
    assert workspace_roles(inv, "w1", known_master_panes=frozenset(["p-term"])) == ()
    # p-missing is not present in the workspace
    assert workspace_roles(inv, "w1", known_master_panes=frozenset(["p-missing"])) == ()


# =============================================================================
# known_master_panes_for_workspace helper tests
# =============================================================================


def test_known_master_panes_matching_and_scoping():
    sock = Path("/srv/herdr.sock")
    masters = {
        "m1": MasterSession(
            session_id="master-custom",
            workspace_id="ws-1",
            pane_id="p1",
            harness=HarnessKind.PI,
            metadata={"socket_path": str(sock)},
        ),
        "m2": MasterSession(
            session_id="master-other-sock",
            workspace_id="ws-1",
            pane_id="p2",
            harness=HarnessKind.PI,
            metadata={"socket_path": "/other/herdr.sock"},
        ),
        "m3": MasterSession(
            session_id="master-other-ws",
            workspace_id="ws-2",
            pane_id="p3",
            harness=HarnessKind.PI,
            metadata={"socket_path": str(sock)},
        ),
        "m4": MasterSession(
            session_id="master-unscoped",
            workspace_id="ws-1",
            pane_id="p4",
            harness=HarnessKind.PI,
            metadata={},
        ),
        "m5": MasterSession(
            session_id="master-empty-socket",
            workspace_id="ws-1",
            pane_id="p5",
            harness=HarnessKind.PI,
            metadata={"socket_path": "   "},
        ),
    }

    result = known_master_panes_for_workspace(masters, sock, "ws-1")
    assert result == frozenset({"p1"})

    assert known_master_panes_for_workspace({}, sock, "ws-1") == frozenset()
    assert known_master_panes_for_workspace(None, sock, "ws-1") == frozenset()


def test_identical_ids_across_different_sockets_are_isolated():
    sock1 = Path("/session1/herdr.sock")
    sock2 = Path("/session2/herdr.sock")

    # Identical display session name, identical workspace ID and pane ID
    inv1 = make_inventory(
        name="shared-name",
        socket_path=sock1,
        workspaces=[{"workspace_id": "w1", "label": "Project"}],
        agents=[{"pane_id": "p1", "workspace_id": "w1", "name": "custom-agent", "agent": "pi"}],
    )
    inv2 = make_inventory(
        name="shared-name",
        socket_path=sock2,
        workspaces=[{"workspace_id": "w1", "label": "Project"}],
        agents=[{"pane_id": "p1", "workspace_id": "w1", "name": "custom-agent", "agent": "pi"}],
    )

    masters = {
        "m1": MasterSession(
            session_id="master-1",
            workspace_id="w1",
            pane_id="p1",
            harness=HarnessKind.PI,
            metadata={"socket_path": str(sock1)},
        )
    }

    panes_sock1 = known_master_panes_for_workspace(masters, sock1, "w1")
    panes_sock2 = known_master_panes_for_workspace(masters, sock2, "w1")

    assert panes_sock1 == frozenset({"p1"})
    assert panes_sock2 == frozenset()

    assert workspace_roles(inv1, "w1", known_master_panes=panes_sock1) == ("Master",)
    assert workspace_roles(inv2, "w1", known_master_panes=panes_sock2) == ()


# =============================================================================
# Widget-label and live UI tests
# =============================================================================


@pytest.mark.asyncio
async def test_workspace_browser_renders_master_and_ergo_badges():
    app = HerdrMasterApp(demo=True)
    inv = make_inventory(
        name="main",
        workspaces=[{"workspace_id": "w1", "label": "project-a"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "name": "master-coordinator",
                "agent": "pi",
                "agent_status": "working",
            },
            {
                "pane_id": "p2",
                "workspace_id": "w1",
                "name": "ergo-orchestrator",
                "agent": "agy",
                "agent_status": "working",
            },
        ],
    )
    app.app_state.live_sessions = [inv]
    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        node = browser._tree.root.children[0]
        plain = node.label.plain
        # Matches plan format: ● running [Master] [Ergo] project-a · main · 2 panes
        assert "● running [Master] [Ergo] project-a · main · 2 panes" == plain

        # Verify Rich text styles
        spans = [(span.start, span.end, span.style) for span in node.label.spans]
        master_span = next(s for s in spans if plain[s[0] : s[1]] == " [Master]")
        ergo_span = next(s for s in spans if plain[s[0] : s[1]] == " [Ergo]")
        assert master_span[2] == "bold cyan"
        assert ergo_span[2] == "bold magenta"


@pytest.mark.asyncio
async def test_workspace_browser_renders_master_only_and_no_role():
    app = HerdrMasterApp(demo=True)
    inv_master = make_inventory(
        name="s1",
        socket_path=Path("/s1.sock"),
        workspaces=[{"workspace_id": "w1", "label": "ws-master"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "name": "master-coordinator",
                "agent": "pi",
                "agent_status": "working",
            }
        ],
    )
    inv_plain = make_inventory(
        name="s2",
        socket_path=Path("/s2.sock"),
        workspaces=[{"workspace_id": "w2", "label": "ws-plain"}],
        agents=[
            {
                "pane_id": "p2",
                "workspace_id": "w2",
                "name": "Builder",
                "agent": "pi",
                "agent_status": "working",
            }
        ],
    )
    app.app_state.live_sessions = [inv_master, inv_plain]
    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        nodes = browser._tree.root.children
        assert "● running [Master] ws-master · s1 · 1 pane" == nodes[0].label.plain
        assert "● running  ws-plain · s2 · 1 pane" == nodes[1].label.plain


@pytest.mark.asyncio
async def test_workspace_browser_offline_renders_dimmed_badges():
    app = HerdrMasterApp(demo=True)
    inv = make_inventory(
        name="s1",
        workspaces=[{"workspace_id": "w1", "label": "ws-off"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "name": "master-coordinator",
                "agent": "pi",
                "agent_status": "working",
            }
        ],
    )
    app.app_state.live_sessions = [inv]
    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        node = browser._tree.root.children[0]
        assert "[Master]" in node.label.plain

        # Simulate offline error on snapshot
        inv.error = "Connection lost"
        browser.update_state(app.app_state)

        plain = node.label.plain
        # Row must start with unmistakable offline marker and retain dimmed role
        assert plain.startswith("× offline [Master]")
        assert plain.endswith("· stale")

        # Verify dimmed style
        spans = [(span.start, span.end, span.style) for span in node.label.spans]
        master_span = next(s for s in spans if plain[s[0] : s[1]] == " [Master]")
        assert master_span[2] == "dim"


@pytest.mark.asyncio
async def test_workspace_browser_custom_master_session_scoping():
    app = HerdrMasterApp(demo=True)
    sock1 = Path("/sock1.sock")
    sock2 = Path("/sock2.sock")

    inv1 = make_inventory(
        name="session",
        socket_path=sock1,
        workspaces=[{"workspace_id": "w1", "label": "project"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "name": "custom-agent",
                "agent": "pi",
                "agent_status": "working",
            }
        ],
    )
    inv2 = make_inventory(
        name="session",
        socket_path=sock2,
        workspaces=[{"workspace_id": "w1", "label": "project"}],
        agents=[
            {
                "pane_id": "p1",
                "workspace_id": "w1",
                "name": "custom-agent",
                "agent": "pi",
                "agent_status": "working",
            }
        ],
    )
    app.app_state.live_sessions = [inv1, inv2]
    app.app_state.masters = {
        "m1": MasterSession(
            session_id="master-custom",
            workspace_id="w1",
            pane_id="p1",
            harness=HarnessKind.PI,
            metadata={"socket_path": str(sock1)},
        )
    }

    async with app.run_test() as pilot:
        await pilot.pause()
        browser = app.query_one(WorkspaceBrowser)
        nodes = browser._tree.root.children
        # Only sock1 should be recognized as Master
        assert "[Master]" in nodes[0].label.plain
        assert "[Master]" not in nodes[1].label.plain


def test_no_socket_calls_during_role_computation(monkeypatch):
    # Guard against any socket or network I/O
    sock_mock = Mock(
        side_effect=AssertionError("Socket operations are forbidden in pure role computation")
    )
    monkeypatch.setattr(socket, "socket", sock_mock)

    inv = make_inventory(
        workspaces=[{"workspace_id": "w1"}],
        agents=[
            {"pane_id": "p1", "workspace_id": "w1", "name": "master-coordinator", "agent": "pi"}
        ],
    )
    roles = workspace_roles(inv, "w1")
    assert roles == ("Master",)
    sock_mock.assert_not_called()
