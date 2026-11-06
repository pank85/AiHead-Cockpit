"""Pilot tests for catalog-backed TUI launch and squad editing."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from herdr_master.cli.main import app as cli_app
from herdr_master.core.context import HerdrContext
from herdr_master.core.models import EffortLevel, HarnessKind, PipelinePhase, Squad
from herdr_master.pipeline.launcher import LaunchContextError
from herdr_master.pipeline.squad import resolve_squad
from aiheaddev.ui.tui.app import HerdrMasterApp
from aiheaddev.ui.tui.modals.config_drawer import ConfigDrawer
from aiheaddev.ui.tui.modals.launch_ergo import LaunchErgoModal
from aiheaddev.ui.tui.modals.launch_master import LaunchMasterModal
from aiheaddev.ui.tui.state import catalog_worker_template

RUNNER = CliRunner()


def _ctx(
    *,
    inside: bool,
    session: str = "default",
    workspace_id: str | None = None,
) -> HerdrContext:
    return HerdrContext(
        is_inside_herdr=inside,
        session_name=session,
        socket_path=Path("/tmp/herdr.sock"),
        workspace_id=workspace_id,
    )


def _success_result(**extra: str) -> dict:
    payload = {
        "session": "ergo-worker",
        "workspace_id": "w-live",
        "pane_id": "w-live:orch",
        "orchestrator_pane_id": "w-live:orch",
        "agent_name": "scout",
        "harness": "agy",
        "model": "gemini-3.8-flash-medium",
        "effort": "medium",
    }
    payload.update(extra)
    return payload


def _tree_text(modal: LaunchErgoModal) -> str:
    tree = modal.query_one("#squad-tree")
    labels = []

    def walk(node) -> None:
        labels.append(str(node.label))
        for child in node.children:
            walk(child)

    walk(tree.root)
    return "\n".join(labels)


def _assert_operator_squad(squad: Squad) -> None:
    assert squad.scout.kind == HarnessKind.AGY
    assert squad.scout.model == "gemini-3.8-flash-medium"
    assert squad.scout.effort == EffortLevel.MEDIUM
    assert squad.architect.kind == HarnessKind.PI
    assert squad.architect.model == "openai-codex/gpt-6.1-sol"
    assert squad.architect.effort == EffortLevel.MAX
    assert squad.reviewer.kind == HarnessKind.PI
    assert squad.reviewer.model == "xai/grok-4.7"
    assert squad.reviewer.effort == EffortLevel.HIGH
    assert [(worker.kind, worker.model, worker.effort) for worker in squad.worker_pool] == [
        (HarnessKind.AGY, "gemini-3.8-flash", None),
        (HarnessKind.PI, "xai/grok-4.7", None),
    ]
    rendered = str(squad)
    assert "claude-3-opus" not in rendered
    assert "gemini-4-argon" not in rendered
    assert "litellm" not in rendered


@pytest.mark.asyncio
async def test_unconfigured_tui_matches_resolver_cli_and_launch(tmp_path: Path):
    resolved = resolve_squad(tmp_path)
    catalog = json.loads(RUNNER.invoke(cli_app, ["catalog", "show", "--json"]).stdout)
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    _assert_operator_squad(app.app_state.squad)
    assert app.app_state.squad == resolved.squad
    assert app.squad_sources == resolved.sources
    assert app.squad_mode == resolved.mode
    assert catalog["default_squad"]["scout"]["model"] == "gemini-3.8-flash-medium"
    assert catalog["default_squad"]["scout"]["model"] == app.app_state.squad.scout.model
    fake = _success_result()
    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=_ctx(inside=False)),
        patch(
            "aiheaddev.ui.tui.app.launch_ergo",
            new_callable=AsyncMock,
            return_value=fake,
        ) as launch,
    ):
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            modal = pilot.app.screen
            assert isinstance(modal, LaunchErgoModal)
            summary = str(modal.query_one("#squad-summary").render())
            preview = str(modal.query_one("#launch-preview").render())
            assert "product_default" in summary
            assert "gemini-3.8-flash-medium" in summary
            assert "medium" in summary
            assert "--model gemini-3.8-flash-medium" in preview
            assert "--effort medium" in preview
            assert "claude-3-opus" not in preview
            assert "gemini-4-argon" not in preview
            await pilot.click("#btn-start-ergo")
            await pilot.pause()
    launch.assert_awaited_once()
    captured = launch.await_args.kwargs["squad"]
    assert captured == resolved.squad
    assert app.app_state.pipeline_run is not None
    assert app.app_state.pipeline_run.phase == PipelinePhase.PREFLIGHT
    assert app.app_state.pipeline_run.metadata["pane_id"] == "w-live:orch"
    assert app.app_state.pipeline_run.metadata["orchestrator_pane_id"] == "w-live:orch"
    assert "scout_pane_id" not in app.app_state.pipeline_run.metadata
    assert app.return_value is None


@pytest.mark.asyncio
async def test_partial_override_and_custom_pool_reach_launch(tmp_path: Path):
    (tmp_path / ".herdr-master.toml").write_text(
        "\n".join(
            [
                "[models.default_squad]",
                'architect = "pi:xai/grok-4.7:high"',
                "",
                "[models.worker_pool.subtle-jobs]",
                'binding = "pi:xai/grok-4.7"',
                'description = "Custom subtle"',
                "enabled = false",
                "order = 0",
                "",
                "[models.worker_pool.mechanical-jobs]",
                'binding = "agy:gemini-3.8-flash"',
                'description = "Custom mechanical"',
                "enabled = true",
                "order = 1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    resolved = resolve_squad(tmp_path)
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    assert app.app_state.squad == resolved.squad
    assert app.app_state.squad.scout.model == "gemini-3.8-flash-medium"
    assert app.app_state.squad.scout.effort == EffortLevel.MEDIUM
    assert app.app_state.squad.architect.model == "xai/grok-4.7"
    assert app.app_state.squad.architect.effort == EffortLevel.HIGH
    assert app.squad_sources["scout"] == "product_default"
    assert app.squad_sources["architect"] == "project"
    assert app.squad_sources["workers"] == "project"
    fake = _success_result()
    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=_ctx(inside=False)),
        patch(
            "aiheaddev.ui.tui.app.launch_ergo",
            new_callable=AsyncMock,
            return_value=fake,
        ) as launch,
    ):
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            modal = pilot.app.screen
            text = _tree_text(modal)
            assert "subtle-jobs" in text
            assert "disabled" in text
            assert "Custom subtle" in text
            summary = str(modal.query_one("#squad-summary").render())
            assert "product_default" in summary
            assert "project" in summary
            await pilot.click("#btn-start-ergo")
            await pilot.pause()
    assert launch.await_args.kwargs["squad"] == resolved.squad
    assert launch.await_args.kwargs["squad"].worker_pool[0].enabled is False


@pytest.mark.asyncio
async def test_demo_squad_uses_catalog_and_truthful_inventory():
    app = HerdrMasterApp(demo=True)
    _assert_operator_squad(app.app_state.squad)
    assert app.app_state.squad == Squad()
    assert app.app_state.inventory_demo is True
    assert len(app.app_state.live_sessions) == 1
    demo_session = app.app_state.live_sessions[0]
    assert demo_session.session_name == "demo"
    assert demo_session.socket_path == Path("/demo/herdr.sock")
    agent_names = {a.name for a in demo_session.snapshot.agents}
    assert "master-coordinator" in agent_names
    assert "ergo-orchestrator" in agent_names
    assert "coder-1" in agent_names
    assert "claude-3-opus" not in str(app.app_state.workers)
    assert "gemini-4-argon" not in str(app.app_state.workers)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("n")
        assert isinstance(pilot.app.screen, LaunchMasterModal)
        launch = pilot.app.screen.query_one("#btn-launch")
        launch.focus()
        await pilot.pause()
        assert launch.has_focus
        assert launch.region.y >= 0
        assert launch.region.bottom <= pilot.app.size.height


@pytest.mark.asyncio
async def test_master_dialog_scout_medium_override_and_custom_model(tmp_path: Path):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    fake = _success_result(session="master-session", pane_id="w-live:master")
    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=_ctx(inside=True)),
        patch(
            "aiheaddev.ui.tui.app.launch_master",
            new_callable=AsyncMock,
            return_value=fake,
        ) as launch,
    ):
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("n")
            modal = pilot.app.screen
            assert isinstance(modal, LaunchMasterModal)
            assert modal._harness_select.value == "agy"
            assert modal._model_input.value == "gemini-3.8-flash-medium"
            assert modal._effort_select.value == "medium"
            preview = str(modal.query_one("#master-preview").render())
            assert "--model gemini-3.8-flash-medium" in preview
            assert "--effort medium" in preview
            assert "Compatibility presets are not the product default." in str(
                modal.query_one(".modal-desc").render()
            )
            assert "claude-3-opus" not in preview
            assert "gemini-4-argon" not in preview

            modal._model_input.value = "custom-coordinator"
            await pilot.pause()
            modal._harness_select.value = "pi"
            await pilot.pause()
            assert modal._model_input.value == "custom-coordinator"
            assert "claude-3" not in modal._model_input.value

            await pilot.click("#btn-cancel")
            await pilot.press("n")
            modal = pilot.app.screen
            modal._harness_select.value = "claude"
            await pilot.pause()
            assert modal._model_input.value == ""
            modal.query_one("#btn-launch").focus()
            await pilot.pause()
            assert await pilot.click("#btn-launch")
            await pilot.pause()
            assert isinstance(pilot.app.screen, LaunchMasterModal)
            assert "explicit" in modal.status_text.lower() or "--model" in modal.status_text
            launch.assert_not_awaited()

            modal._harness_select.value = "pi"
            await pilot.pause()
            modal._model_input.value = "xai/grok-4.7"
            await pilot.pause()
            modal._effort_select.value = "max"
            await pilot.pause()
            await pilot.click("#btn-launch")
            await pilot.pause()
    launch.assert_awaited_once()
    kwargs = launch.await_args.kwargs
    assert kwargs["harness"] == HarnessKind.PI
    assert kwargs["model"] == "xai/grok-4.7"
    assert kwargs["effort"] == EffortLevel.MAX
    assert app.app_state.masters
    master = next(iter(app.app_state.masters.values()))
    assert master.pane_id == "w-live:master"
    assert app.return_value is None
    assert kwargs["fresh_workspace"] is True
    assert kwargs["focus"] is False


@pytest.mark.asyncio
async def test_squad_editor_validation_effort_and_read_only_root(tmp_path: Path):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    before = app.app_state.squad.model_copy(deep=True)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("e")
        modal = pilot.app.screen
        assert isinstance(modal, LaunchErgoModal)
        assert modal.squad is not app.app_state.squad

        modal._load_agent_into_editor("architect")
        modal._effort_select.value = "high"
        await pilot.pause()
        assert modal.squad.architect.effort == EffortLevel.HIGH
        assert all(worker.effort is None for worker in modal.squad.worker_pool)

        modal._load_agent_into_editor(("worker", 0))
        assert modal._effort_select.display is False
        assert modal._effort_note.display is True
        note = str(modal._effort_note.render())
        assert "Architect" in note
        assert "per-task" in note or "effort" in note.lower()

        scout_model = modal.squad.scout.model
        modal._load_agent_into_editor("root")
        assert modal._model_input.disabled is True
        assert modal._harness_select.disabled is True
        modal._model_input.value = "mutated-scout"
        modal._apply_current_editor_changes()
        assert modal.squad.scout.model == scout_model

        modal._load_agent_into_editor("pool_overview")
        modal._model_input.value = "mutated-scout"
        modal._apply_current_editor_changes()
        assert modal.squad.scout.model == scout_model

        modal._load_agent_into_editor("scout")
        modal._model_input.value = "   "
        await pilot.pause()
        await pilot.click("#btn-start-ergo")
        await pilot.pause()
        assert isinstance(pilot.app.screen, LaunchErgoModal)
        assert "invent" in modal.status_text.lower() or "empty" in modal.status_text.lower()
        assert 'gemini-3.8-flash"' not in modal.status_text
        assert app.app_state.squad == before

        await pilot.click("#btn-cancel")
        assert app.app_state.squad == before
        assert app.app_state.pipeline_run is None


@pytest.mark.asyncio
async def test_reorder_persists_and_save_default_round_trip(tmp_path: Path):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("e")
        modal = pilot.app.screen
        modal._load_agent_into_editor(("worker", 1))
        modal.query_one("#btn-up-worker").focus()
        await pilot.pause()
        assert await pilot.click("#btn-up-worker")
        assert [worker.model for worker in modal.squad.worker_pool] == [
            "xai/grok-4.7",
            "gemini-3.8-flash",
        ]
        assert [worker.order for worker in modal.squad.worker_pool] == [0, 1]
        modal.query_one("#btn-save-default").focus()
        await pilot.pause()
        assert await pilot.click("#btn-save-default")
        await pilot.pause()
        assert "Saved default" in modal.status_text
        await pilot.click("#btn-add-worker")
        added = modal.squad.worker_pool[-1]
        template = catalog_worker_template(2)
        assert added.model == template.model
        assert added.effort is None
        assert added.description == template.description
        assert "Junior dev" not in (added.description or "")
        await pilot.click("#btn-cancel")
    reloaded = resolve_squad(tmp_path).squad
    assert [worker.model for worker in reloaded.worker_pool[:2]] == [
        "xai/grok-4.7",
        "gemini-3.8-flash",
    ]
    assert reloaded.worker_pool[0].label == "worker-2"
    assert reloaded.scout.model == "gemini-3.8-flash-medium"


@pytest.mark.asyncio
async def test_save_default_keeps_disabled_worker(tmp_path: Path):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("e")
        modal = pilot.app.screen
        assert isinstance(modal, LaunchErgoModal)
        disabled = modal.squad.worker_pool[1]
        disabled.enabled = False
        assert any(worker.enabled for worker in modal.squad.worker_pool)
        modal.query_one("#btn-save-default").focus()
        await pilot.pause()
        assert await pilot.click("#btn-save-default")
        await pilot.pause()
        assert "Saved default" in modal.status_text
        await pilot.click("#btn-cancel")
    reloaded = resolve_squad(tmp_path).squad
    saved = next(worker for worker in reloaded.worker_pool if worker.model == "xai/grok-4.7")
    assert saved.enabled is False
    assert any(worker.enabled for worker in reloaded.worker_pool)
    assert reloaded.scout.model == "gemini-3.8-flash-medium"
    assert reloaded.architect.model == "openai-codex/gpt-6.1-sol"


@pytest.mark.asyncio
async def test_omitted_session_adopts_inherited_socket(tmp_path: Path):
    inherited = HerdrContext(
        is_inside_herdr=True,
        session_name="ergo-alpha",
        socket_path=Path("/tmp/ergo-alpha.sock"),
        workspace_id="w-alpha",
    )
    app = HerdrMasterApp(session=None, demo=False, project_path=tmp_path)
    blank = HerdrMasterApp(session="   ", demo=True)
    assert blank._requested_session() is None
    fake = _success_result(session="ergo-alpha", pane_id="w-alpha:master")
    with (
        patch(
            "aiheaddev.ui.tui.app.resolve_launch_context",
            return_value=inherited,
        ) as resolver,
        patch(
            "aiheaddev.ui.tui.app.launch_master",
            new_callable=AsyncMock,
            return_value=fake,
        ) as launch,
    ):
        async with app.run_test(size=(100, 40)) as pilot:
            assert app.session_name == "ergo-alpha"
            assert app.socket_path == Path("/tmp/ergo-alpha.sock")
            assert app.client is not None
            assert app.client.session == "ergo-alpha"
            assert app.client.socket_path == Path("/tmp/ergo-alpha.sock")
            assert resolver.call_args_list
            assert all(call.args == (None,) for call in resolver.call_args_list)
            await pilot.press("n")
            await pilot.click("#btn-launch")
            await pilot.pause()
    launch.assert_awaited()
    kwargs = launch.await_args.kwargs
    assert kwargs["session"] == "ergo-alpha"
    assert kwargs["session"] != "default"
    assert Path(kwargs["client"].socket_path) == Path("/tmp/ergo-alpha.sock")
    assert all(call.args == (None,) for call in resolver.call_args_list)


@pytest.mark.asyncio
async def test_invalid_project_blocks_launch_and_pending_is_single(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".herdr-master.toml").write_text(
        '[models.default_squad]\nscout = "nope"\n',
        encoding="utf-8",
    )
    app = HerdrMasterApp(demo=False, project_path=bad)
    assert app.config_error
    assert "INVALID CONFIG" in app.app_state.branch_status
    assert "nope" in app.config_error
    with patch("aiheaddev.ui.tui.app.launch_ergo", new_callable=AsyncMock) as launch:
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.press("e")
            modal = pilot.app.screen
            assert app.config_error in str(modal.query_one("#squad-summary").render())
            await pilot.click("#btn-start-ergo")
            await pilot.pause()
            assert isinstance(pilot.app.screen, LaunchErgoModal)
            launch.assert_not_awaited()
            assert app.app_state.pipeline_run is None
            assert app.return_value is None
            await pilot.click("#btn-save-default")
            await pilot.pause()
            assert "Saved default" not in modal.status_text
            assert "nope" in modal.status_text or "Invalid" in modal.status_text
            assert 'scout = "nope"' in (bad / ".herdr-master.toml").read_text(encoding="utf-8")
            await pilot.click("#btn-cancel")

    good = tmp_path / "good"
    good.mkdir()
    app = HerdrMasterApp(demo=False, project_path=good)
    gate = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def slow_launch(**kwargs):
        calls.append(kwargs)
        started.set()
        await gate.wait()
        return _success_result()

    with (
        patch("aiheaddev.ui.tui.app.resolve_launch_context", return_value=_ctx(inside=False)),
        patch("aiheaddev.ui.tui.app.launch_ergo", side_effect=slow_launch),
    ):
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            await pilot.click("#btn-start-ergo")
            for _ in range(30):
                await pilot.pause()
                if started.is_set():
                    break
            assert started.is_set()
            assert app._launch_pending is True
            await pilot.press("e")
            await pilot.click("#btn-start-ergo")
            await pilot.pause()
            assert len(calls) == 1
            gate.set()
            await pilot.pause()


@pytest.mark.asyncio
async def test_failure_alias_and_context_error_leave_no_phantom(tmp_path: Path):
    async def _launch_once(side_effect=None, return_value=None):
        instance = HerdrMasterApp(demo=False, project_path=tmp_path)
        if side_effect is not None:
            launch_patch = {"side_effect": side_effect}
        else:
            launch_patch = {"return_value": return_value}
        with (
            patch(
                "aiheaddev.ui.tui.app.resolve_launch_context",
                return_value=_ctx(inside=False),
            ),
            patch(
                "aiheaddev.ui.tui.app.launch_ergo",
                new_callable=AsyncMock,
                **launch_patch,
            ) as launch,
        ):
            async with instance.run_test(size=(110, 45)) as pilot:
                await pilot.press("e")
                assert await pilot.click("#btn-start-ergo")
                await pilot.pause()
        return instance, launch

    failed, _ignored = await _launch_once(side_effect=RuntimeError("socket down"))
    assert failed.app_state.pipeline_run is None
    assert failed.app_state.masters == {}
    assert failed.return_value is None

    alias_only = {"session": "ergo-worker", "workspace_id": "w", "scout_pane_id": "w:old"}
    aliased, _ignored = await _launch_once(return_value=alias_only)
    assert aliased.app_state.pipeline_run is None
    assert aliased.return_value is None

    blocked = HerdrMasterApp(session="other-session", demo=False, project_path=tmp_path)
    with (
        patch(
            "aiheaddev.ui.tui.app.resolve_launch_context",
            side_effect=LaunchContextError(
                "Refusing session 'other-session' inside inherited workspace"
            ),
        ),
        patch("aiheaddev.ui.tui.app.launch_ergo", new_callable=AsyncMock) as launch,
    ):
        async with blocked.run_test(size=(110, 45)) as pilot:
            await pilot.press("e")
            assert await pilot.click("#btn-start-ergo")
            await pilot.pause()
            launch.assert_not_awaited()
            assert blocked.app_state.pipeline_run is None
            assert blocked.return_value is None


@pytest.mark.asyncio
async def test_config_drawer_rejects_blank_and_cancel_keeps_copy(tmp_path: Path):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    before = app.app_state.squad.model_copy(deep=True)
    async with app.run_test(size=(100, 40)) as pilot:
        pilot.app.push_screen(ConfigDrawer(app.app_state.squad, project_path=tmp_path))
        await pilot.pause()
        drawer = pilot.app.screen
        assert isinstance(drawer, ConfigDrawer)
        assert drawer.squad is not app.app_state.squad
        drawer._model_input.value = "   "
        drawer.query_one("#btn-apply-binding").focus()
        await pilot.pause()
        assert await pilot.click("#btn-apply-binding")
        await pilot.pause()
        assert "invent" in drawer.status_text.lower() or "empty" in drawer.status_text.lower()
        assert drawer.squad.scout.model == "gemini-3.8-flash-medium"
        drawer._role_select.value = "pool"
        drawer._load_current_target_fields()
        await pilot.pause()
        add = drawer.query_one("#btn-add-worker")
        assert add.display
        add.focus()
        await pilot.pause()
        add.press()
        await pilot.pause()
        added = drawer.squad.worker_pool[-1]
        assert added.model == catalog_worker_template(0).model
        assert added.effort is None
        assert [worker.order for worker in drawer.squad.worker_pool] == list(
            range(len(drawer.squad.worker_pool))
        )
        await pilot.click("#btn-cancel")
        await pilot.pause()
    assert app.app_state.squad == before


@pytest.mark.asyncio
async def test_ordinary_terminal_keeps_actions_reachable(tmp_path: Path):
    app = HerdrMasterApp(demo=False, project_path=tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("e")
        modal = pilot.app.screen
        start = modal.query_one("#btn-start-ergo")
        cancel = modal.query_one("#btn-cancel")
        start.focus()
        await pilot.pause()
        assert start.has_focus
        assert start.region.y >= 0
        assert start.region.bottom <= pilot.app.size.height
        cancel.focus()
        await pilot.pause()
        assert cancel.has_focus
        preview = modal.query_one("#launch-preview")
        assert preview.region.bottom <= pilot.app.size.height
        assert "gemini-3.8-flash-medium" in str(preview.render())
        await pilot.click("#btn-cancel")
        await pilot.press("n")
        launch = pilot.app.screen.query_one("#btn-launch")
        launch.focus()
        await pilot.pause()
        assert launch.has_focus
        assert launch.region.bottom <= pilot.app.size.height
