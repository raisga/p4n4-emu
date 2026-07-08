"""Tests for stack directory resolution (flat, multi-layer, and bare layouts)."""

import json

from p4n4_emu.utils.project import (
    expand_stacks,
    find_manifest,
    manifest_layers,
    resolve_stack_dir,
)


def _write_manifest(root, layers):
    root.joinpath(".p4n4.json").write_text(
        json.dumps({"schema_version": 1, "project": "proj", "layers": layers})
    )


def _make_flat_project(root, layer="iot"):
    _write_manifest(root, [layer])
    (root / "docker-compose.yml").touch()


def _make_multi_project(root, layers=("iot", "ai")):
    _write_manifest(root, list(layers))
    for name in layers:
        (root / name).mkdir()
        (root / name / "docker-compose.yml").touch()


# ── find_manifest / manifest_layers ──────────────────────────────────────────


def test_find_manifest_walks_up(tmp_path):
    _make_flat_project(tmp_path)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_manifest(nested) == tmp_path / ".p4n4.json"


def test_find_manifest_none(tmp_path):
    assert find_manifest(tmp_path) is None


def test_manifest_layers_ordered_and_filtered(tmp_path):
    _write_manifest(tmp_path, ["ai", "bogus", "iot"])
    assert manifest_layers(tmp_path / ".p4n4.json") == ["iot", "ai"]


def test_manifest_layers_invalid_json(tmp_path):
    tmp_path.joinpath(".p4n4.json").write_text("not json")
    assert manifest_layers(tmp_path / ".p4n4.json") == []


# ── expand_stacks ─────────────────────────────────────────────────────────────


def test_expand_default_uses_project_layers(tmp_path):
    _make_multi_project(tmp_path)
    assert expand_stacks(None, tmp_path) == ["iot", "ai"]
    assert expand_stacks("all", tmp_path) == ["iot", "ai"]


def test_expand_default_without_project(tmp_path):
    assert expand_stacks(None, tmp_path) == ["iot"]
    assert expand_stacks("all", tmp_path) == ["iot", "ai", "edge"]


def test_expand_explicit_and_comma_separated(tmp_path):
    _make_multi_project(tmp_path)
    assert expand_stacks("edge", tmp_path) == ["edge"]
    assert expand_stacks("iot,ai", tmp_path) == ["iot", "ai"]


# ── resolve_stack_dir ─────────────────────────────────────────────────────────


def test_resolve_multi_layer_subdirs(tmp_path):
    _make_multi_project(tmp_path)
    assert resolve_stack_dir(None, "iot", tmp_path) == tmp_path / "iot"
    assert resolve_stack_dir(None, "ai", tmp_path) == tmp_path / "ai"


def test_resolve_multi_layer_from_nested_cwd(tmp_path):
    _make_multi_project(tmp_path)
    nested = tmp_path / "iot" / "config"
    nested.mkdir()
    assert resolve_stack_dir(None, "ai", nested) == tmp_path / "ai"


def test_resolve_flat_project(tmp_path):
    _make_flat_project(tmp_path, "iot")
    assert resolve_stack_dir(None, "iot", tmp_path) == tmp_path


def test_resolve_flat_project_rejects_other_stacks(tmp_path):
    # The root compose belongs to iot; asking for ai must not match it
    _make_flat_project(tmp_path, "iot")
    assert resolve_stack_dir(None, "ai", tmp_path) is None


def test_resolve_disabled_layer_is_none(tmp_path):
    _make_multi_project(tmp_path, ("iot", "ai"))
    assert resolve_stack_dir(None, "edge", tmp_path) is None


def test_resolve_explicit_base_prefers_stack_subdir(tmp_path):
    (tmp_path / "docker-compose.yml").touch()
    (tmp_path / "ai").mkdir()
    (tmp_path / "ai" / "docker-compose.yml").touch()
    assert resolve_stack_dir(tmp_path, "ai", tmp_path) == tmp_path / "ai"
    assert resolve_stack_dir(tmp_path, "iot", tmp_path) == tmp_path


def test_resolve_bare_checkout_fallbacks(tmp_path):
    (tmp_path / "iot").mkdir()
    (tmp_path / "iot" / "docker-compose.yml").touch()
    assert resolve_stack_dir(None, "iot", tmp_path) == tmp_path / "iot"
    assert resolve_stack_dir(None, "ai", tmp_path) is None
