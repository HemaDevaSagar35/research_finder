"""Application token allowances remain separate from API output limits."""
import pytest

from llm_client.client import _chat_params, _response_params, _provider_config
from opportunities.miner import Settings
from reasoning.cross_paper import Budgets


@pytest.mark.parametrize('requested,expected', [(500000,393216),(12000,12000),(393216,393216)])
def test_output_request_respects_provider_limit_for_both_endpoints(requested, expected):
    config={'default_model':'deepseek-flash','max_tokens':500000,
            'temperature':None,'output_token_limit':393216}
    chat=_chat_params('hello',None,None,config,None,None,requested,{})
    response=_response_params('hello',None,config,None,requested,{})
    assert chat['max_tokens']==response['max_output_tokens']==expected


def test_environment_output_allowance_is_capped_without_changing_configuration(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_MAX_TOKENS','500000')
    monkeypatch.setenv('DEEPSEEK_OUTPUT_TOKEN_LIMIT','393216')
    config=_provider_config('deepseek')
    assert _chat_params('hello',None,None,config,None,None,None,{})['max_tokens']==393216
    assert _response_params('hello',None,config,None,None,{})['max_output_tokens']==393216
    assert config['max_tokens']==500000


def test_unconfigured_provider_limit_does_not_invent_one():
    config={'default_model':'test-model','max_tokens':500000,'temperature':None}
    assert _chat_params('hello',None,None,config,None,None,None,{})['max_tokens']==500000


def test_nonpositive_provider_output_limit_rejected(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_OUTPUT_TOKEN_LIMIT','0')
    with pytest.raises(ValueError,match='must be positive'):
        _provider_config('deepseek')


def test_generation_token_allowances_and_explicit_overrides(monkeypatch):
    for name in ['REASON_MAX_DRAFT_INPUT_TOKENS','REASON_MAX_DRAFT_OUTPUT_TOKENS',
                 'REASON_MAX_REVIEW_INPUT_TOKENS','REASON_MAX_REVIEW_OUTPUT_TOKENS',
                 'OPPORTUNITY_MAX_INPUT_TOKENS','OPPORTUNITY_MAX_OUTPUT_TOKENS',
                 'OPPORTUNITY_REVIEW_MAX_OUTPUT_TOKENS']:
        monkeypatch.setenv(name,'500000')
    budgets=Budgets()
    settings=Settings()
    assert all(value==500000 for key,value in budgets.as_dict().items() if key.endswith('_tokens'))
    assert all(value==500000 for key,value in settings.model_dump().items() if key.endswith('_tokens'))
    assert Settings(max_input_tokens=123).max_input_tokens==123
    assert Budgets(max_draft_output_tokens=456).max_draft_output_tokens==456
    monkeypatch.setenv('OPPORTUNITY_MAX_INPUT_TOKENS','400000')
    assert Settings().max_input_tokens==400000
