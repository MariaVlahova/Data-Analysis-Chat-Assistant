"""The trio format, its loader and its per-step views (plan 10)."""
import pytest
from pydantic import ValidationError

from src.golden.schema import REPORT_EXCERPT_CHARS, Trio, load_trio_file, load_trio_files
from tests.golden_support import SEED_DIR, make_trio, trio_data, write_trio


# the format ---------------------------------------------------------------------------------------------------------

def test_a_valid_trio_is_accepted_and_is_frozen():
    trio = make_trio()
    assert trio.searchable and trio.brand_agnostic
    with pytest.raises(ValidationError):
        trio.status = "retired"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["id", "version", "status", "question", "interpretation_notes", "sql", "report", "tables", "brands", "reviewed_by", "created_at"])
def test_every_field_is_required(field):
    data = trio_data()
    del data[field]
    with pytest.raises(ValidationError):
        Trio.model_validate(data)


def test_an_unknown_field_is_refused_so_a_typo_is_not_silently_ignored():
    with pytest.raises(ValidationError):
        Trio.model_validate(trio_data(statuss="approved"))


@pytest.mark.parametrize("change", [
    {"status": "published"}, {"version": 0}, {"id": "has space"}, {"id": ""}, {"question": "short"}, {"tables": []}, {"tables": [" "]},
    {"brands": []}, {"brands": [" "]}, {"brands": ["Maple & Co", "Maple & Co"]}, {"created_at": "not a date"},
])
def test_bad_values_are_refused(change):
    with pytest.raises(ValidationError):
        make_trio(**change)


def test_the_any_brand_marker_cannot_be_mixed_with_brand_names():
    with pytest.raises(ValidationError, match="cannot be mixed"):
        make_trio(brands=["*", "Maple & Co"])
    assert make_trio(brands=["Maple & Co", "Nordic Peak"]).brand_agnostic is False


def test_only_an_approved_trio_is_searchable():
    assert [make_trio(status=status).searchable for status in ("draft", "approved", "retired")] == [False, True, False]


# the per-step views -----------------------------------------------------------------------------------------------

def test_the_planner_view_has_the_question_and_notes_and_no_sql_or_report():
    view = make_trio().view("planner")
    assert set(view) == {"id", "question", "notes"}


def test_the_sql_view_adds_the_sql_and_still_has_no_report():
    trio = make_trio()
    view = trio.view("sql")
    assert set(view) == {"id", "question", "notes", "sql"} and view["sql"] == trio.sql.strip()
    assert trio.report not in " ".join(view.values())


def test_the_composer_view_is_an_excerpt_of_the_report_and_has_no_sql():
    long_report = "word " * 400
    view = make_trio(report=long_report).view("composer")
    assert set(view) == {"id", "report"} and len(view["report"]) == REPORT_EXCERPT_CHARS
    assert "SELECT" not in view["report"]


def test_an_unknown_step_has_no_view():
    with pytest.raises(ValueError):
        make_trio().view("evidence")  # type: ignore[arg-type]


# loading -----------------------------------------------------------------------------------------------------------

def test_the_seed_folder_loads_with_no_errors():
    files = load_trio_files(SEED_DIR)
    assert [item.error for item in files if item.trio is None] == []
    trios = [item.trio for item in files]
    assert len(trios) == 12 and len({trio.id for trio in trios}) == 12
    assert {trio.status for trio in trios} == {"approved", "draft", "retired"}  # the filters have something to filter
    assert sum(1 for trio in trios if not trio.brand_agnostic) == 2  # two brand-specific trios


def test_a_bad_file_is_reported_and_the_others_still_load(tmp_path):
    write_trio(tmp_path, "g-100")
    write_trio(tmp_path, "g-101", id="g-101", sql=None)
    (tmp_path / "g-102.yaml").write_text("id: [unclosed", encoding="utf-8")
    (tmp_path / "g-103.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored: not a .yaml file", encoding="utf-8")
    files = load_trio_files(tmp_path)
    assert [item.path.name for item in files] == ["g-100.yaml", "g-101.yaml", "g-102.yaml", "g-103.yaml"]
    assert [item.trio is not None for item in files] == [True, False, False, False]
    assert "sql" in files[1].error  # names the field
    assert all(item.error for item in files[1:])


def test_a_repeated_id_fails_the_later_file(tmp_path):
    write_trio(tmp_path, "g-100")
    write_trio(tmp_path, "g-101", id="g-100")  # a later file with the same id inside
    first, second = load_trio_files(tmp_path)
    assert first.trio is not None and second.trio is None and "already used by g-100.yaml" in second.error


def test_one_file_can_be_loaded_on_its_own(tmp_path):
    path = write_trio(tmp_path, "g-100")
    assert load_trio_file(path).trio.id == "g-100"
    assert [item.trio.id for item in load_trio_files(path)] == ["g-100"]


def test_a_missing_folder_gives_no_files(tmp_path):
    assert load_trio_files(tmp_path / "nope") == []


def test_a_file_that_is_not_utf8_is_reported(tmp_path):
    path = tmp_path / "g-100.yaml"
    path.write_bytes("id: g-100\n".encode("utf-16"))
    assert "UTF-8" in load_trio_file(path).error
