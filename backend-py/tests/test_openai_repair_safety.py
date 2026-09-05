"""Safety regressions for the operator-facing OpenAI scrape repair agent."""

from __future__ import annotations

import json

import pytest

from app.services.scraper.ai_repair_agent import (
    PatchValidationError,
    _apply_to_yaml,
    _restore_yaml,
    _simulate_filter,
    _validate_and_build_config_patch,
    _validated_ai_patches,
    validate_extraction_rule_on_samples,
    validate_ai_repair_target,
    _apply_recipe_to_db,
    _restore_db_config,
    run_ai_repair_loop,
    validate_url_repair_target,
)
from app.services.scraper.config.loader import get_config_for_host


def test_openai_can_clear_each_url_filter_gate() -> None:
    discovery, extraction, errors = _validate_and_build_config_patch(
        [
            {
                "section": "discovery",
                "field": field,
                "action": "replace",
                "value": [],
            }
            for field in (
                "allow_url_patterns",
                "block_url_patterns",
                "must_contain",
                "course_detail_url_patterns",
            )
        ]
    )

    assert extraction == {}
    assert errors == []
    assert discovery == {
        "allow_url_patterns": [],
        "block_url_patterns": [],
        "must_contain": [],
        "course_detail_url_patterns": [],
    }


def test_full_filter_simulation_includes_must_contain_and_detail_gate() -> None:
    urls = [
        "https://www.unisc.edu.au/study/courses-and-programs/postgraduate-degrees/master-of-business",
        "https://www.unisc.edu.au/study/courses-and-programs/bachelor-degrees-undergraduate-programs/bachelor-of-arts",
        "https://www.unisc.edu.au/study/courses-and-programs/courses/search-for-unisc-courses",
    ]

    blocked = _simulate_filter(
        urls,
        allow_pats=[r"/study/"],
        block_pats=[r"/search-for-"],
        must_contain=["/courses-and-programs/"],
        course_detail_pats=[r"/programmes/[^/]+$"],
    )
    repaired = _simulate_filter(
        urls,
        allow_pats=[r"/study/"],
        block_pats=[r"/search-for-"],
        must_contain=["/courses-and-programs/"],
        course_detail_pats=[
            r"/study/courses-and-programs/[a-z][a-z-]+/[a-z][a-z-]+/?$"
        ],
    )

    assert blocked["after"] == 0
    assert repaired["after"] == 2
    assert repaired["total"] == 3


def test_invalid_openai_envelope_is_rejected_before_patch_processing() -> None:
    with pytest.raises(PatchValidationError, match="patches list"):
        _validated_ai_patches({"confidence": 90})
    with pytest.raises(PatchValidationError, match="more than 3"):
        _validated_ai_patches(
            {"confidence": 90, "patches": [{}, {}, {}, {}]}
        )
    with pytest.raises(PatchValidationError, match="integer from 0 to 100"):
        _validated_ai_patches({"confidence": 101, "patches": []})


def test_yaml_apply_preserves_explicit_empty_list_and_can_rollback(tmp_path) -> None:
    yaml_file = tmp_path / "portable_1.yaml"
    original = (
        "discovery:\n"
        "  allow_url_patterns:\n"
        "    - /broken/\n"
        "  must_contain:\n"
        "    - /obsolete/\n"
    )
    yaml_file.write_text(original, encoding="utf-8")

    path, previous = _apply_to_yaml(
        yaml_file,
        tmp_path,
        1,
        "https://portable.edu",
        {
            "discovery": {
                "allow_url_patterns": [],
                "must_contain": [],
            }
        },
    )

    updated = path.read_text(encoding="utf-8")
    assert "allow_url_patterns: []" in updated
    assert "must_contain: []" in updated

    _restore_yaml(path, previous)
    assert path.read_text(encoding="utf-8") == original


def test_repair_target_requires_terminal_job_with_filter_failure_evidence() -> None:
    evidence = {
        "pipeline_stats": {
            "raw_discovered": 142,
            "after_filter": 0,
            "dropped_sample": [
                "https://www.unisc.edu.au/study/courses-and-programs/"
                "postgraduate-degrees/master-of-business"
            ],
        }
    }

    assert validate_url_repair_target("running", evidence)[0] is False
    assert validate_url_repair_target("completed", evidence)[0] is True
    assert validate_url_repair_target(
        "completed",
        {
            "pipeline_stats": {
                "raw_discovered": 142,
                "after_filter": 140,
                "dropped_sample": evidence["pipeline_stats"]["dropped_sample"],
            }
        },
    )[0] is False
    assert validate_ai_repair_target(
        "completed", {}, has_extraction_gap=True
    )[0] is True
    assert validate_ai_repair_target(
        "running", evidence, has_extraction_gap=True
    )[0] is False
    assert validate_url_repair_target(
        "completed",
        {"pipeline_stats": {"raw_discovered": 142, "after_filter": 0}},
    )[0] is False


def test_unisc_verified_recipe_wins_for_recreated_database_id() -> None:
    config = get_config_for_host(
        hostname="www.unisc.edu.au",
        name="University of the Sunshine Coast",
        scrape_url="https://www.unisc.edu.au",
        university_id=20,
        db_scrape_config={
            "admin_config": {
                "discovery": {
                    "allow_url_patterns": [r"^/study/.+"],
                    "block_url_patterns": [r"/courses-and-programs/"],
                    "seed_urls": ["https://www.unisc.edu.au/wrong-listing"],
                    "sitemap_url": "https://www.unisc.edu.au/wrong.xml",
                }
            }
        },
    )

    assert config.discovery.allow_url_patterns == [
        r"/study/courses-and-programs/[a-z][a-z-]+/[a-z][a-z-]+/?$"
    ]
    assert "/courses-and-programs/" not in config.discovery.block_url_patterns
    assert config.discovery.sitemap_url == "https://www.unisc.edu.au/XMLsitemap"
    assert config.discovery.seed_urls[0].endswith("/study/courses-and-programs")


@pytest.mark.parametrize(
    ("field", "rule", "missing_html", "good_html", "good_value"),
    [
        ("international_fee", {"css": ".fee", "transform": "currency", "confidence": .9},
         '<div class="fee">AUD $32,000</div>', '<div class="fee">AUD $30,000</div>', 30000),
        ("ielts_overall", {"css": ".ielts", "transform": "number", "confidence": .9},
         '<div class="ielts">6.5</div>', '<div class="ielts">7.0</div>', 7.0),
        ("duration", {"css": ".duration", "transform": "number", "confidence": .9},
         '<div class="duration">3 years</div>', '<div class="duration">2 years</div>', 2),
        ("course_location", {"css": ".campus", "confidence": .9},
         '<div class="campus">Sydney Campus</div>', '<div class="campus">Melbourne</div>', "Melbourne"),
    ],
)
def test_extraction_repair_fills_missing_and_preserves_good_values(
    field, rule, missing_html, good_html, good_value
) -> None:
    report = validate_extraction_rule_on_samples(field, rule, [
        {"url": "https://example.edu/missing", "html": missing_html, "before": None},
        {"url": "https://example.edu/good", "html": good_html, "before": good_value},
    ])
    assert report["accepted"] is True
    assert report["missing_filled"] == 1
    assert report["regressions"] == 0


def test_extraction_repair_rejects_rule_that_overwrites_good_value() -> None:
    report = validate_extraction_rule_on_samples(
        "international_fee",
        {"css": ".fee", "transform": "currency", "confidence": .95},
        [
            {"url": "https://example.edu/a", "html": '<b class="fee">$32,000</b>', "before": None},
            {"url": "https://example.edu/b", "html": '<b class="fee">$32,000</b>', "before": 30_000},
        ],
    )
    assert report["accepted"] is False
    assert report["regressions"] == 1


class _FakeMappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    def __init__(self, *, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def mappings(self):
        return _FakeMappings(self._row)


class _FakeConfigDb:
    def __init__(self, config, *, update_rowcount=1):
        self.config = config
        self.update_rowcount = update_rowcount
        self.commits = 0
        self.rollbacks = 0
        self.saved = None

    async def execute(self, statement, params):
        if "SELECT scrape_config" in str(statement):
            return _FakeResult(row={"scrape_config": self.config})
        if "after" in params:
            self.saved = json.loads(params["after"])
        elif "cfg" in params:
            self.saved = json.loads(params["cfg"])
        return _FakeResult(rowcount=self.update_rowcount)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_atomic_extraction_rule_apply_targets_runtime_auto_config() -> None:
    before = {"admin_config": {"discovery": {"bfs_page_budget": 10}}}
    db = _FakeConfigDb(before)
    configs = await _apply_recipe_to_db(
        7,
        {"extraction_rules": {"duration": {"css": ".duration", "confidence": .9}}},
        db,
    )
    assert configs["before"] == before
    assert db.commits == 1
    assert db.saved["admin_config"] == before["admin_config"]
    assert db.saved["auto_config"]["extraction_rules"]["duration"]["css"] == ".duration"


@pytest.mark.asyncio
async def test_atomic_extraction_rule_apply_rolls_back_on_compare_and_swap_conflict() -> None:
    db = _FakeConfigDb({"auto_config": {}}, update_rowcount=0)
    with pytest.raises(RuntimeError, match="changed during validation"):
        await _apply_recipe_to_db(
            7,
            {"extraction_rules": {"duration": {"css": ".duration", "confidence": .9}}},
            db,
        )
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_apply_rejects_operator_edit_made_during_snapshot_validation() -> None:
    config_before_validation = {
        "auto_config": {
            "extraction_rules": {
                "duration": {"css": ".old-duration", "confidence": .9}
            }
        }
    }
    operator_updated_config = {
        "auto_config": {
            "extraction_rules": {
                "duration": {"css": ".operator-duration", "confidence": .95}
            }
        }
    }
    db = _FakeConfigDb(operator_updated_config, update_rowcount=0)
    with pytest.raises(RuntimeError, match="changed during validation"):
        await _apply_recipe_to_db(
            7,
            {
                "extraction_rules": {
                    "duration": {"css": ".ai-duration", "confidence": .9}
                }
            },
            db,
            expected_config=config_before_validation,
        )
    assert db.saved["auto_config"]["extraction_rules"]["duration"]["css"] == ".ai-duration"
    assert db.commits == 0
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_extraction_rule_restore_refuses_to_clobber_newer_config() -> None:
    db = _FakeConfigDb({"auto_config": {}}, update_rowcount=0)
    with pytest.raises(RuntimeError, match="refused to overwrite"):
        await _restore_db_config(
            7,
            {"auto_config": {}},
            db,
            expected_current={"auto_config": {"extraction_rules": {"duration": {}}}},
        )
    assert db.commits == 0
    assert db.rollbacks == 2


@pytest.mark.asyncio
async def test_loop_restores_config_when_post_apply_processing_fails(monkeypatch) -> None:
    import app.services.ai.openai_client as openai_client
    import app.services.scraper.ai_repair_agent as agent

    rule = {"css": ".duration", "transform": "number", "confidence": .9}
    ctx = {
        "job_id": "job-1",
        "university_id": 7,
        "uni_name": "Test University",
        "scrape_url": "https://example.edu",
        "raw_discovered": 10,
        "after_filter": 10,
        "imported": 10,
        "total_errors": 0,
        "drop_rate": 0,
        "dropped_sample": [],
        "passed_sample": [],
        "admin_config": {},
        "effective_discovery": {},
        "yaml_content": "",
        "quality": {
            "total_staged": 2, "fee_pct": 100, "ielts_pct": 100,
            "intakes_pct": 100, "location_pct": 100, "degree_level_pct": 100,
            "mode_pct": 100, "duration_pct": 50,
        },
    }
    restored = []

    async def fake_chat_json(**_kwargs):
        return {
            "diagnosis": "duration missing",
            "root_cause": "duration",
            "confidence": 95,
            "explanation": "A tested selector fills the missing duration.",
            "patches": [{
                "section": "recipe",
                "field": "extraction_rules.duration",
                "action": "replace",
                "value": rule,
            }],
        }

    async def fake_restore(uid, before, db, *, expected_current=None):
        restored.append((uid, before, expected_current))

    monkeypatch.setattr(openai_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(agent, "read_session", lambda _job: {"university_id": 7})
    monkeypatch.setattr(agent, "_write_session", lambda *_args: None)
    monkeypatch.setattr(agent, "_gather_context", lambda *_args: _async_value(ctx))
    monkeypatch.setattr(agent, "_quality_snapshot", lambda *_args: _async_value(dict(ctx["quality"])))
    monkeypatch.setattr(
        agent,
        "_read_scrape_config",
        lambda *_args: _async_value({"auto_config": {}}),
    )
    monkeypatch.setattr(
        agent,
        "_validate_extraction_patch_on_snapshots",
        lambda *_args, **_kwargs: _async_value({
            "accepted": True,
            "rules": {"duration": rule},
            "reports": [],
            "rollback_status": "not_needed",
        }),
    )
    monkeypatch.setattr(
        agent,
        "_apply_recipe_to_db",
        lambda *_args, **_kwargs: _async_value(
            {"before": {"a": 1}, "applied": {"a": 2}}
        ),
    )
    monkeypatch.setattr(agent, "_restore_db_config", fake_restore)

    async def fail_after_apply(*_args, **_kwargs):
        raise RuntimeError("forced post-apply failure")

    monkeypatch.setattr(agent, "_predict_quality", fail_after_apply)
    session = await run_ai_repair_loop("job-1", object())
    assert session["status"] == "failed"
    assert session["rollback_status"] == "restored"
    assert restored == [(7, {"a": 1}, {"a": 2})]


@pytest.mark.asyncio
async def test_loop_reports_no_change_when_extraction_persistence_fails(monkeypatch) -> None:
    import app.services.ai.openai_client as openai_client
    import app.services.scraper.ai_repair_agent as agent

    rule = {"css": ".duration", "transform": "number", "confidence": .9}
    quality = {
        "total_staged": 2, "fee_pct": 100, "ielts_pct": 100,
        "intakes_pct": 100, "location_pct": 100, "degree_level_pct": 100,
        "mode_pct": 100, "duration_pct": 50,
    }
    ctx = {
        "job_id": "job-2", "university_id": 7, "uni_name": "Test University",
        "scrape_url": "https://example.edu", "raw_discovered": 10,
        "after_filter": 10, "imported": 10, "total_errors": 0, "drop_rate": 0,
        "dropped_sample": [], "passed_sample": [], "admin_config": {},
        "effective_discovery": {}, "yaml_content": "", "quality": quality,
    }

    async def fake_chat_json(**_kwargs):
        return {
            "diagnosis": "duration missing",
            "root_cause": "duration",
            "confidence": 95,
            "explanation": "A tested selector fills the missing duration.",
            "patches": [{
                "section": "recipe",
                "field": "extraction_rules.duration",
                "action": "replace",
                "value": rule,
            }],
        }

    async def fail_apply(*_args, **_kwargs):
        raise RuntimeError("compare-and-swap conflict")

    monkeypatch.setattr(openai_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(agent, "read_session", lambda _job: {"university_id": 7})
    monkeypatch.setattr(agent, "_write_session", lambda *_args: None)
    monkeypatch.setattr(agent, "_gather_context", lambda *_args: _async_value(ctx))
    monkeypatch.setattr(agent, "_quality_snapshot", lambda *_args: _async_value(dict(quality)))
    monkeypatch.setattr(
        agent,
        "_read_scrape_config",
        lambda *_args: _async_value({"auto_config": {}}),
    )
    monkeypatch.setattr(
        agent,
        "_validate_extraction_patch_on_snapshots",
        lambda *_args, **_kwargs: _async_value({
            "accepted": True,
            "rules": {"duration": rule},
            "reports": [],
            "rollback_status": "not_needed",
        }),
    )
    monkeypatch.setattr(agent, "_apply_recipe_to_db", fail_apply)
    monkeypatch.setattr(
        agent,
        "_predict_quality",
        lambda *_args: _async_value((dict(quality), {})),
    )

    class _Db:
        async def rollback(self):
            return None

    session = await run_ai_repair_loop("job-2", _Db())
    assert session["status"] == "failed"
    assert session["rollback_status"] == "unchanged"
    assert "No scraper config was changed" in session["error"]
    assert "No extraction repair was applied" in session["final_verdict"]
    assert not session["attempts"][0]["patch_applied_ok"]


async def _async_value(value):
    return value