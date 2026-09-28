"""A crew may not be named after a spec Kiro Crew generates for itself.

Kiro Crew writes its own agent specs into the agents directory and rewrites them at every
boot, reading nothing about who wrote what. A crew installed at one of those filenames is
validated, digest-checked, installed -- and then replaced by the generated spec before the
first turn, so the task answers with a different prompt and a different tool surface than
the bundle digest attested, and nothing says so.

Reachable by an ordinary action, not an exotic one: packaging one's own default agent
(``--crew kirocrew``) resolves straight out of ``<kiro home>/agents/kirocrew.json`` and
produces exactly such a bundle.

Refused at install rather than detected later: the replacement happens inside the backend's
own boot, after the supervisor has finished, so there is no later point at which refusing
is still cheap.
"""

from __future__ import annotations

import json

import pytest
from container.common import ConfigError
from container.supervisor import bundle as bundle_mod

from ._settings_helper import make_settings


def _write_bundle(root, crew: str):
    """A bundle that passes every other check, so only the name is under test."""
    bundle = root / "bundle"
    (bundle / "skills").mkdir(parents=True)
    (bundle / "agent.json").write_text(json.dumps({"name": crew}), encoding="utf-8")
    (bundle / "mcp.json").write_text("{}", encoding="utf-8")
    digest = None
    # The manifest carries the digest of everything except itself, so it is written twice:
    # once to exist, then again with the digest computed over the final file set.
    for _ in range(2):
        (bundle / "manifest.json").write_text(
            json.dumps(
                {
                    "bundle_version": 1,
                    "crew_name": crew,
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "digest": digest or "",
                }
            ),
            encoding="utf-8",
        )
        digest = bundle_mod._content_digest(bundle)
    return bundle


def test_the_reserved_set_matches_the_specs_kiro_crew_generates():
    """The mirrored copy is pinned to the original, so a new managed spec reds here.

    The container cannot import ``kiro_crew`` -- it runs in an image where the package is
    absent -- so the set is copied. A copy nobody compares is a set that silently stops
    covering the newest managed spec, which is the one a crew is most likely to collide
    with.
    """
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

    owned = {name.removesuffix(".json") for name in OWNED_KIRO_AGENT_FILES}

    assert bundle_mod.RESERVED_CREW_NAMES == owned, (
        "the container's reserved-name copy has drifted from "
        f"agent_files.OWNED_KIRO_AGENT_FILES: missing={sorted(owned - bundle_mod.RESERVED_CREW_NAMES)} "
        f"extra={sorted(bundle_mod.RESERVED_CREW_NAMES - owned)}"
    )


@pytest.mark.parametrize("crew", ["kirocrew", "kirocrew-worker", "kirocrew-lite"])
def test_install_refuses_a_crew_named_after_a_generated_spec(tmp_path, crew):
    settings_base = make_settings(tmp_path, crew=crew)
    bundle = _write_bundle(tmp_path, crew)
    settings = settings_base.__class__(**{**settings_base.__dict__, "bundle_dir": bundle})
    agents = tmp_path / "agents"
    agents.mkdir()

    with pytest.raises(ConfigError) as err:
        bundle_mod.install_bundle(settings, agents_dir=agents)

    assert "reserved" in str(err.value)
    assert not list(agents.glob("*.json")), (
        "the refusal came after the write, so the spec the backend will replace is already "
        "on disk"
    )


def test_install_accepts_a_crew_named_anything_else(tmp_path):
    """The refusal is narrow: a name outside the generated set installs as before."""
    crew = "raymonds-crew"
    settings_base = make_settings(tmp_path, crew=crew)
    bundle = _write_bundle(tmp_path, crew)
    settings = settings_base.__class__(**{**settings_base.__dict__, "bundle_dir": bundle})
    agents = tmp_path / "agents"
    agents.mkdir()

    bundle_mod.install_bundle(settings, agents_dir=agents)

    assert (agents / f"{crew}.json").is_file()
