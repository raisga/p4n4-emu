"""Tests for per-directory overlay paths and the profile they record."""

import pytest

from p4n4_emu.overlays import paths
from p4n4_emu.overlays.generator import render_overlay
from p4n4_emu.profiles.loader import load_profile


@pytest.fixture(autouse=True)
def overlay_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    return tmp_path / "overlays"


def _write(stack_dir, stack="iot", profile="rpi5"):
    path = paths.overlay_path(stack_dir, stack)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_overlay(load_profile(profile), stack, None, services=["mqtt"]))
    return path


def test_two_projects_get_separate_overlays(tmp_path):
    a = paths.overlay_path(tmp_path / "a" / "greenhouse", "iot")
    b = paths.overlay_path(tmp_path / "b" / "greenhouse", "iot")
    assert a != b
    assert a.parent.name.startswith("greenhouse-")


def test_multi_layer_stack_dir_is_named_after_its_project(tmp_path):
    path = paths.overlay_path(tmp_path / "greenhouse" / "ai", "ai")
    assert path.parent.name.startswith("greenhouse-ai-")
    assert path.name == "ai.emu.yml"


def test_same_directory_same_path(tmp_path):
    (tmp_path / "p").mkdir()
    assert paths.overlay_path(tmp_path / "p", "iot") == paths.overlay_path(
        tmp_path / "p" / ".." / "p", "iot"
    )


def test_active_profile_comes_from_the_overlay(tmp_path):
    assert paths.active_profile(tmp_path, "iot") is None
    _write(tmp_path, profile="nuc")
    assert paths.active_profile(tmp_path, "iot") == "nuc"


def test_remove_overlay_keeps_folder_while_another_stack_uses_it(tmp_path):
    _write(tmp_path, "iot")
    ai = _write(tmp_path, "ai")
    paths.remove_overlay(tmp_path, "iot")
    assert paths.existing_overlay(tmp_path, "iot") is None
    assert ai.exists()
    paths.remove_overlay(tmp_path, "ai")
    assert not ai.parent.exists()


def test_remove_missing_overlay_is_a_no_op(tmp_path):
    paths.remove_overlay(tmp_path, "iot")


def test_unreadable_overlay_has_no_profile(tmp_path):
    path = paths.overlay_path(tmp_path, "iot")
    path.parent.mkdir(parents=True)
    path.write_text(":\n  - [")
    assert paths.active_profile(tmp_path, "iot") is None
