from pathlib import Path

import pytest

from src.access.users import (
    UserContext,
    load_user_directory,
    load_users,
    make_user_hash,
    require_access,
    resolve_salt,
)
from src.config import load_settings
from src.errors import AgentError, ConfigError, ErrorKind


def load_text(tmp_path: Path, text: str):
    path = tmp_path / "users.yaml"
    path.write_text(text, encoding="utf-8")
    return load_user_directory(path, salt="test-salt")


@pytest.fixture
def users(tmp_path, users_yaml):
    return load_text(tmp_path, users_yaml)


def test_ceo_with_the_flag_sees_all(users):
    ceo = users.get("ceo")
    assert ceo.scope_mode == "all"
    assert ceo.all_brands and ceo.brands == ()


def test_manager_with_brands_is_scoped_to_them(users):
    alice = users.get("alice")
    assert alice.scope_mode == "brands"
    assert alice.brands == ("Brand A", "Brand B")
    assert not alice.all_brands


def test_empty_brand_list_for_a_manager_is_none_and_never_all(users):
    nobody = users.get("nobrands")
    assert nobody.scope_mode == "none"
    assert nobody.scope_mode != "all"
    with pytest.raises(AgentError) as err:
        require_access(nobody)
    assert err.value.kind is ErrorKind.NO_ACCESS


def test_require_access_lets_brand_users_and_the_ceo_through(users):
    for name in ("alice", "bob", "ceo"):
        require_access(users.get(name))


def test_a_ceo_without_the_flag_has_no_access(tmp_path):
    ceo = load_text(tmp_path, "users:\n  boss: {name: Boss, role: ceo}\n").get("boss")
    assert ceo.scope_mode == "none"


def test_object_built_by_hand_cannot_widen_scope_through_the_flag():
    # S14: the explicit flag only counts together with the ceo role, even if a bad object slips in.
    sneaky = UserContext("x", "X", "brand_manager", brands=(), all_brands=True)
    assert sneaky.scope_mode == "none"
    with pytest.raises(AgentError):
        require_access(sneaky)
    with_brands = UserContext("x", "X", "brand_manager", brands=("Brand A",), all_brands=True)
    assert with_brands.scope_mode == "brands"


def test_manager_with_all_brands_true_is_rejected_at_load(tmp_path):
    bad = "users:\n  eve: {name: Eve, role: brand_manager, all_brands: true}\n"
    with pytest.raises(ConfigError) as err:
        load_text(tmp_path, bad)
    assert "users.eve" in err.value.message
    assert "only allowed for role: ceo" in err.value.message


def test_unknown_user_lists_the_known_names(users):
    with pytest.raises(ConfigError) as err:
        users.get("zed")
    assert "Unknown user 'zed'" in err.value.message
    assert "alice, bob, ceo, nobrands" in err.value.message


def test_lookup_ignores_case_and_spaces_around_the_name(users):
    assert users.get("  Alice ").user_id == "alice"


def test_missing_file_is_access_store_down(tmp_path):
    with pytest.raises(AgentError) as err:
        load_user_directory(tmp_path / "users.yaml", salt="s")
    assert err.value.kind is ErrorKind.ACCESS_STORE_DOWN


def test_unreadable_file_is_access_store_down(tmp_path):
    folder = tmp_path / "users.yaml"
    folder.mkdir()  # reading a folder as a file fails with an OS error
    with pytest.raises(AgentError) as err:
        load_user_directory(folder, salt="s")
    assert err.value.kind is ErrorKind.ACCESS_STORE_DOWN


def test_bad_yaml_and_bad_schema_name_file_line_and_key(tmp_path):
    with pytest.raises(ConfigError, match=r"users.yaml line \d+"):
        load_text(tmp_path, "users:\n  alice: {name: [Alice\n")
    with pytest.raises(ConfigError) as err:
        load_text(tmp_path, "users:\n  alice:\n    name: Alice\n    role: brand_manager\n    brandz: [A]\n")
    assert "line 5" in err.value.message and "users.alice.brandz" in err.value.message


@pytest.mark.parametrize(
    "text",
    [
        "users: {}\n",
        "",
        "users:\n  alice: {name: Alice, role: wizard}\n",
        'users:\n  alice: {name: Alice, role: brand_manager, brands: ["A", " "]}\n',
        "users:\n  'bad name!': {name: X, role: brand_manager, brands: [A]}\n",
    ],
)
def test_invalid_files_are_startup_errors(tmp_path, text):
    with pytest.raises(ConfigError):
        load_text(tmp_path, text)


def test_other_roles_are_accepted_but_have_no_brands_so_no_access(tmp_path):
    loaded = load_text(tmp_path, "users:\n  ana: {name: Ana, role: analyst}\n  pat: {name: Pat, role: admin, brands: [A]}\n")
    assert loaded.get("ana").scope_mode == "none"
    assert loaded.get("pat").scope_mode == "brands"


def test_brand_names_are_kept_exactly_as_written(tmp_path):
    loaded = load_text(tmp_path, 'users:\n  a: {name: A, role: brand_manager, brands: [" Nike", "nike", "Nike  Pro"]}\n')
    assert loaded.get("a").brands == (" Nike", "nike", "Nike  Pro")


def test_user_hash_is_12_hex_deterministic_and_does_not_contain_the_login(tmp_path, users, users_yaml):
    again = load_text(tmp_path, users_yaml)
    h = users.get("alice").user_hash
    assert h == again.get("alice").user_hash == make_user_hash("test-salt", "alice")
    assert len(h) == 12 and int(h, 16) >= 0
    assert "alice" not in h
    assert h != users.get("bob").user_hash
    assert h != make_user_hash("other-salt", "alice")


def test_a_style_block_is_not_accepted(tmp_path):
    with pytest.raises(ConfigError, match="style"):
        load_text(tmp_path, "users:\n  a: {name: A, role: brand_manager, brands: [B], style: {length: short}}\n")


def test_salt_comes_from_config_or_a_generated_file(project):
    settings = load_settings(project / "config.yaml", load_env_file=False)
    first = resolve_salt(settings)
    assert len(first) >= 16
    assert (project / "data" / "user_hash_salt").read_text() == first
    assert resolve_salt(settings) == first  # stable across runs

    (project / "config.yaml").write_text("app:\n  user_hash_salt: from-config\n", encoding="utf-8")
    configured = load_settings(project / "config.yaml", load_env_file=False)
    assert resolve_salt(configured) == "from-config"


def test_load_users_reads_users_yaml_next_to_the_config(project):
    settings = load_settings(project / "config.yaml", load_env_file=False)
    assert load_users(settings).names == ["alice", "bob", "ceo", "nobrands"]
