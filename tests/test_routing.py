"""Model routing (plan 04, decision 3.2): which model a step uses, its sibling, and the per-step settings."""
import pytest

from src.config import LLM_STEP_NAMES, LlmConfig, LlmModels, Settings, StepSettings
from src.llm.routing import Router, Step
from src.llm_steps import STEP_DEFAULTS

LITE, FLASH, PRO = "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"


def router(**options) -> Router:
    return Router(LlmConfig(**options))


def test_the_steps_and_their_defaults_are_defined_once_and_config_uses_them():
    assert tuple(step.value for step in Step) == LLM_STEP_NAMES and set(STEP_DEFAULTS) == set(Step)
    defaults = Settings().llm.steps
    assert set(defaults) == set(LLM_STEP_NAMES)
    assert {name: (s.temperature, s.max_output_tokens) for name, s in defaults.items()} == {
        step.value: pair for step, pair in STEP_DEFAULTS.items()
    }


@pytest.mark.parametrize("step", [Step.INPUT_GUARD, Step.EVIDENCE_CHECK])
def test_the_checking_steps_start_on_flash_lite_and_fall_back_to_flash(step):
    assert router().chain(step) == (LITE, FLASH)


@pytest.mark.parametrize("step", [Step.PLANNER, Step.SQL_WRITE, Step.SQL_REPAIR, Step.COMPOSER])
def test_the_writing_steps_start_on_flash_and_fall_back_to_flash_lite(step):
    assert router().chain(step) == (FLASH, LITE)


@pytest.mark.parametrize("step", [Step.LAST_REPAIR, Step.JUDGE])
def test_pro_is_skipped_when_it_is_disabled(step):
    assert router(pro_enabled=False).chain(step) == (FLASH, LITE)  # the prototype: Flash only
    assert PRO not in router().models_in_use()


@pytest.mark.parametrize("step", [Step.LAST_REPAIR, Step.JUDGE])
def test_pro_steps_have_no_fallback_when_it_is_enabled(step):
    assert router(pro_enabled=True).chain(step) == (PRO,)


def test_model_ids_come_from_config():
    custom = LlmModels(light="lite-x", main="flash-y")
    assert router(models=custom).chain(Step.SQL_WRITE) == ("flash-y", "lite-x")


def test_a_chain_never_lists_a_model_twice():
    same = LlmModels(light="one", main="one")
    assert router(models=same).chain(Step.COMPOSER) == ("one",)


def test_models_in_use_lists_each_model_once():
    assert router().models_in_use() == [LITE, FLASH]
    assert router(pro_enabled=True).models_in_use() == [LITE, FLASH, PRO]


def test_each_step_has_its_temperature_and_output_cap():
    llm_router = router()
    assert llm_router.settings(Step.COMPOSER).temperature == pytest.approx(0.3)
    for step in Step:
        if step is not Step.COMPOSER:
            assert llm_router.settings(step).temperature == 0
        assert llm_router.settings(step).max_output_tokens > 0


def test_config_fills_in_steps_you_leave_out_and_rejects_unknown_ones():
    llm = LlmConfig(steps={"composer": StepSettings(temperature=0.5, max_output_tokens=100)})
    assert llm.steps["composer"].max_output_tokens == 100
    assert llm.steps["sql_write"].max_output_tokens == 2048
    with pytest.raises(ValueError, match="unknown step"):
        LlmConfig(steps={"sql_writer": StepSettings()})


def test_a_setting_left_out_of_a_listed_step_keeps_that_steps_own_default():
    """Review finding: `composer: {temperature: 0.5}` used to give the Composer the generic 2048-token cap."""
    llm = LlmConfig.model_validate({"steps": {"composer": {"temperature": 0.5}, "input_guard": {"max_output_tokens": 700}}})
    assert (llm.steps["composer"].temperature, llm.steps["composer"].max_output_tokens) == (0.5, 4096)
    assert (llm.steps["input_guard"].temperature, llm.steps["input_guard"].max_output_tokens) == (0.0, 700)
    assert llm.steps["planner"].max_output_tokens == 1024  # a step left out entirely


def test_a_step_given_as_an_object_keeps_the_defaults_for_what_it_did_not_set():
    llm = LlmConfig(steps={"composer": StepSettings(temperature=0.5)})
    assert llm.steps["composer"].max_output_tokens == 4096


def test_a_typo_inside_a_step_or_a_wrong_type_is_still_an_error():
    with pytest.raises(ValueError):
        LlmConfig.model_validate({"steps": {"composer": {"temprature": 0.5}}})
    with pytest.raises(ValueError):
        LlmConfig.model_validate({"steps": {"composer": None}})
    with pytest.raises(ValueError):
        LlmConfig.model_validate({"steps": ["composer"]})
