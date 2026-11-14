"""Project-scoped settings drafts; no UI objects or terminal I/O."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import secrets
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from herdr_master.core.catalog import (CatalogError, compatibility_presets, describe_herdr_agent,
    ensure_herdr_routable, get_harness, native_binding_error, supported_harnesses, worker_route_note)
from herdr_master.core.config import (DEFAULT_ORCHESTRATION_MODELS, HerdrMasterConfig,
    find_project_config_path, load_project_config, resolve_folder_path, save_project_config,
    squad_to_project_config)
from herdr_master.core.slug import parse_binding
from herdr_master.core.models import Squad
from herdr_master.pipeline.squad import describe_agent, resolve_squad
from .protocol import BridgeError, _bounded, encode


def _fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def catalog_get() -> dict[str, Any]:
    result = {"harnesses": [dataclasses.asdict(get_harness(k)) for k in supported_harnesses(mode="herdr")],
              "native_harnesses": supported_harnesses(mode="native"), "presets": compatibility_presets()}
    _bounded(result)
    encode(result)
    return result


def folders_test(path: str, *, project: str | None = None) -> dict[str, Any]:
    if not isinstance(path, str) or not path.strip():
        raise BridgeError("invalid_params", "A folder path is required", field_errors={"path": "Folder is empty"})
    try:
        resolved = resolve_folder_path(path, base_dir=project)
        return {"path": str(resolved), "available": resolved.is_dir()}
    except (OSError, ValueError, RuntimeError) as exc:
        raise BridgeError("folder_error", str(exc), field_errors={"path": str(exc)}) from exc


def folders_list(path: str, *, project: str | None = None) -> dict[str, Any]:
    result = folders_test(path, project=project)
    if not result["available"]:
        raise BridgeError("folder_error", "Folder is not available", field_errors={"path": "Folder is not available"})
    try:
        items = []
        for entry in Path(result["path"]).iterdir():
            if entry.is_dir():
                items.append(entry)
                if len(items) > 4096:
                    raise BridgeError("too_many_items", "Folder contains too many directories")
        items.sort(key=lambda p: p.name.casefold())
        result["directories"] = [{"name": p.name, "path": str(p)} for p in items]
        encode(result)
        return result
    except OSError as exc:
        raise BridgeError("folder_error", str(exc), field_errors={"path": str(exc)}) from exc


def _merge(base: dict[str, Any], updates: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    for key, value in updates.items():
        name = f"{prefix}.{key}" if prefix else key
        if key not in base:
            if prefix == "orchestration.harnesses" and key in supported_harnesses(mode="native") and isinstance(value, dict):
                base[key] = copy.deepcopy(value)
                continue
            raise BridgeError("invalid_params", f"Unknown field: {name}")
        if isinstance(value, dict) and isinstance(base[key], dict):
            _merge(base[key], value, name)
        else:
            row_fields = {
                "squad.worker_pool": set(Squad().worker_pool[0].model_dump(mode="json")),
                "council.members": set(HerdrMasterConfig().council.members[0].model_dump(mode="json")),
                "projects.favorites": {"name", "path"},
            }
            if name in row_fields and isinstance(value, list):
                if any(not isinstance(row, dict) or not row_fields[name] <= set(row) for row in value):
                    raise BridgeError("invalid_params", f"{name} requires complete rows")
            base[key] = copy.deepcopy(value)
    return base


_ORCH_FIELDS = ("scout_model", "architect_model", "reviewer_model", "worker_models",
                "scout_effort", "architect_effort", "reviewer_effort")


def _store_orchestration(data: dict[str, Any], kind: str, source: dict[str, Any]) -> None:
    existing = copy.deepcopy(data["harnesses"].get(kind, {}))
    if not isinstance(existing, dict):
        raise BridgeError("invalid_params", "Invalid per-harness settings")
    existing.update({key: copy.deepcopy(source[key]) for key in _ORCH_FIELDS})
    data["harnesses"][kind] = existing


def _activate_orchestration(data: dict[str, Any], config: HerdrMasterConfig, *, initial: bool = False) -> None:
    """Match the editor's per-harness defaults without repairing user-entered blanks."""
    kind = data["harness"]
    current = data["harnesses"].get(kind, {})
    if not isinstance(current, dict):
        raise BridgeError("invalid_params", "Invalid per-harness settings")
    if initial and any(data.get(key) for key in _ORCH_FIELDS):
        current = {key: copy.deepcopy(data[key]) for key in _ORCH_FIELDS}
    has_saved = any(current.get(key) for key in _ORCH_FIELDS)
    defaults = DEFAULT_ORCHESTRATION_MODELS.get(kind, {})
    bindings = config.models.default_squad or {}
    for role in ("scout", "architect", "reviewer", "coder"):
        model = defaults.get(role)
        if bindings.get(role):
            try:
                binding = parse_binding(bindings[role])
                if binding.kind.value == kind:
                    model = binding.model
            except (ValueError, TypeError):
                pass
        key = "worker_models" if role == "coder" else role + "_model"
        saved = current.get(key)
        data[key] = copy.deepcopy(saved if has_saved else ([model] if role == "coder" and model else model))
    for role in ("scout", "architect", "reviewer"):
        data[role + "_effort"] = current.get(role + "_effort")
    _store_orchestration(data, kind, data)


def _errors(config: HerdrMasterConfig, squad: Squad) -> tuple[dict[str, str], dict[str, Any]]:
    errors: dict[str, str] = {}
    previews: dict[str, Any] = {}
    bindings = [(f"squad.{role}", getattr(squad, role), role) for role in ("scout", "architect", "reviewer")]
    bindings += [(f"squad.worker_pool.{i}", b, "worker") for i, b in enumerate(squad.worker_pool)]
    bindings += [("council.coordinator", config.council.coordinator, "master"),
                 ("council.scout", config.council.scout, "scout")]
    bindings += [(f"council.members.{i}", b, "reviewer") for i, b in enumerate(config.council.members)]
    bindings += [("ai_head.binding", config.ai_head.binding, "master")]
    if sum(bool(b.enabled) for b in config.council.members) < 2:
        errors["council.members"] = "At least two council members must be enabled."
    if config.ai_head.enabled and not config.ai_head.soul.strip():
        errors["ai_head.soul"] = "AiHead soul / system prompt cannot be blank."
    for key, binding, role in bindings:
        if not binding.model.strip():
            errors[key + ".model"] = "Model is empty. Refusing to invent a model."
            continue
        if role == "worker" and binding.effort is not None:
            errors[key + ".effort"] = "Worker effort must be unset."
            continue
        try:
            if role == "worker":
                ensure_herdr_routable(binding.kind.value, binding.model, None, role="coder", require_effort=False)
                previews[key] = worker_route_note(binding.kind.value, binding.model)
            else:
                previews[key] = describe_agent(binding, mode="herdr", role=role)
        except (CatalogError, ValueError) as exc:
            errors[key] = str(exc)
    orch = config.orchestration
    kind = orch.harness.value
    try:
        if not get_harness(kind).supports_native:
            errors["orchestration.harness"] = "Harness does not support native orchestration."
        configs = {k: v.model_dump(mode="json") for k, v in orch.harnesses.items()}
        configs[kind] = {key: getattr(orch, key) for key in _ORCH_FIELDS}
        for harness, values in configs.items():
            prefix = "orchestration" if harness == kind else f"orchestration.harnesses.{harness}"
            if harness != kind and not any(values.get(key) for key in _ORCH_FIELDS):
                continue
            for role in ("scout", "architect", "reviewer"):
                model = values.get(role + "_model")
                key = f"{prefix}.{role}_model"
                if not model or not model.strip():
                    errors[key] = "Model is empty. Refusing to invent a model."
                else:
                    err = native_binding_error(harness, model, role=role, native_harness=harness)
                    if err: errors[key] = err
            workers = values.get("worker_models") or []
            if not workers:
                errors[f"{prefix}.worker_models"] = "At least one worker model is required."
            for i, model in enumerate(workers):
                key = f"{prefix}.worker_models.{i}"
                if not model.strip(): errors[key] = "Model is empty. Refusing to invent a model."
                elif model.rsplit(":", 1)[-1].lower() in {"low", "medium", "high", "xhigh", "max"} and ":" in model:
                    errors[key] = "Worker model must not specify effort."
                else:
                    err = native_binding_error(harness, model, role="coder", native_harness=harness)
                    if err: errors[key] = err
    except (CatalogError, ValueError) as exc:
        errors["orchestration.harness"] = str(exc)
    return errors, previews


@dataclasses.dataclass
class _Draft:
    project: Path
    destination: Path
    scope: str
    fingerprint: str | None
    config: HerdrMasterConfig
    squad: Squad
    revision: int = 0


class SettingsService:
    """In-memory drafts, explicitly scoped by a resolved project and destination."""
    def __init__(self) -> None:
        self._drafts: dict[str, _Draft] = {}

    def project_open(self, path: str) -> dict[str, Any]:
        if len(self._drafts) >= 128:
            raise BridgeError("too_many_drafts", "Discard a settings draft before opening another")
        folder = folders_test(path)
        if not folder["available"]:
            raise BridgeError("folder_error", "Project folder is not available", field_errors={"path": "Folder is not available"})
        project = Path(folder["path"]).resolve()
        found = find_project_config_path(project)
        dest = Path(found).resolve() if found else project / ".herdr-master.toml"
        scope = "new-local" if found is None else ("local" if dest.parent == project else "inherited")
        try:
            fingerprint = _fingerprint(dest)
            cfg = load_project_config(dest).model_copy(deep=True)
            orch = cfg.orchestration.model_dump(mode="json")
            _activate_orchestration(orch, cfg, initial=True)
            cfg = cfg.model_copy(update={"orchestration": type(cfg.orchestration).model_validate(orch)})
            squad = resolve_squad(project).squad.model_copy(deep=True)
            if (_fingerprint(dest) != fingerprint or
                    (find_project_config_path(project) or project / ".herdr-master.toml").resolve() != dest):
                raise BridgeError("concurrent_modification", "Configuration changed while opening; reload")
            draft = _Draft(project, dest, scope, fingerprint, cfg, squad)
        except (OSError, ValueError) as exc:
            raise BridgeError("project_error", str(exc)) from exc
        token = secrets.token_urlsafe(24)
        result = self._view(token, draft)
        self._drafts[token] = draft
        return result

    def _view(self, token: str, draft: _Draft) -> dict[str, Any]:
        result = {"token": token, "revision": draft.revision, "project_path": str(draft.project),
                  "destination": str(draft.destination), "scope": draft.scope,
                  "sections": {"projects": draft.config.projects.model_dump(mode="json"),
                               "squad": draft.squad.model_dump(mode="json"),
                               "orchestration": draft.config.orchestration.model_dump(mode="json"),
                               "council": draft.config.council.model_dump(mode="json"),
                               "ai_head": draft.config.ai_head.model_dump(mode="json")},
                  "retained_metadata": {k: v for k, v in draft.config.model_dump(mode="json").items()
                                        if k not in {"projects", "orchestration", "council", "ai_head"}},
                  "catalog": catalog_get()}
        _bounded(result)
        encode(result)
        return result

    def discard(self, token: str, *, revision: int | None = None) -> None:
        draft = self._draft(token)
        if revision is not None and (type(revision) is not int or revision != draft.revision):
            raise BridgeError("stale_revision", "Draft revision changed; reload before editing")
        del self._drafts[token]

    def _draft(self, token: str) -> _Draft:
        try: return self._drafts[token]
        except (KeyError, TypeError) as exc: raise BridgeError("invalid_token", "Unknown draft token") from exc

    def validate(self, token: str, sections: dict[str, Any], *, revision: int) -> dict[str, Any]:
        draft = self._draft(token)
        if type(revision) is not int or revision != draft.revision:
            raise BridgeError("stale_revision", "Draft revision changed; reload before editing")
        if not isinstance(sections, dict) or set(sections) - {"projects", "squad", "orchestration", "council", "ai_head"}:
            raise BridgeError("invalid_params", "Invalid settings sections")
        encode(sections)
        _bounded(sections)
        current = self._view(token, draft)["sections"]
        try:
            previous = copy.deepcopy(current["orchestration"])
            merged = _merge(current, sections)
            updated = merged["orchestration"]
            if not isinstance(updated, dict) or not isinstance(updated.get("harness"), str) or not isinstance(updated.get("harnesses"), dict):
                raise BridgeError("invalid_params", "Invalid orchestration section")
            if updated["harness"] != previous["harness"]:
                _store_orchestration(updated, previous["harness"], previous)
                _activate_orchestration(updated, draft.config)
                explicit = sections.get("orchestration", {})
                for key in _ORCH_FIELDS:
                    if key in explicit:
                        updated[key] = copy.deepcopy(explicit[key])
                _store_orchestration(updated, updated["harness"], updated)
            else:
                _store_orchestration(updated, updated["harness"], updated)
            cfg_data = draft.config.model_dump(mode="json")
            for key in ("projects", "orchestration", "council", "ai_head"):
                cfg_data[key] = merged[key]
            cfg = HerdrMasterConfig.model_validate(cfg_data)
            squad = Squad.model_validate(merged["squad"])
        except ValidationError as exc:
            return {"valid": False, "field_errors": {".".join(str(p) for p in e["loc"]): e["msg"] for e in exc.errors()}, "revision": draft.revision}
        errors, previews = _errors(cfg, squad)
        # Keep valid edits even if route validation fails, so the caller can repair them.
        draft.config, draft.squad = cfg, squad
        draft.revision += 1
        return {"valid": not errors, "field_errors": errors, "route_previews": previews,
                "revision": draft.revision, "sections": self._view(token, draft)["sections"]}

    def save(self, token: str, sections: dict[str, Any], *, revision: int) -> dict[str, Any]:
        draft = self._draft(token)
        if _fingerprint(draft.destination) != draft.fingerprint:
            raise BridgeError("concurrent_modification", "Configuration destination changed; reload before saving")
        result = self.validate(token, sections, revision=revision)
        if not result["valid"]:
            return result
        draft = self._draft(token)
        try:
            if _fingerprint(draft.destination) != draft.fingerprint or (find_project_config_path(draft.project) or draft.project / ".herdr-master.toml").resolve() != draft.destination:
                raise BridgeError("concurrent_modification", "Configuration destination changed; reload before saving")
            updated = squad_to_project_config(draft.squad, existing_config=draft.config.model_copy(deep=True))
            save_project_config(updated, path=draft.destination)
            draft.config = updated
            draft.fingerprint = _fingerprint(draft.destination)
        except OSError as exc:
            raise BridgeError("save_failed", str(exc), field_errors={"revision": str(draft.revision)}, outcome="uncertain") from exc
        return {"valid": True, "revision": draft.revision, "destination": str(draft.destination)}
