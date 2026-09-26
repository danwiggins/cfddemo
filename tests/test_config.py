"""Tests for environment-driven settings resolution."""

from bedrock_chat.config import (
    DEFAULT_MODEL_ID,
    DEFAULT_REGION,
    Settings,
)


def test_defaults_when_env_empty():
    settings = Settings.from_env(env={})
    assert settings.model_id == DEFAULT_MODEL_ID
    assert settings.region == DEFAULT_REGION
    assert settings.max_tokens == 1024
    # Temperature is unset by default (GPT-5.x rejects it).
    assert settings.temperature is None


def test_region_precedence_prefers_bedrock_region():
    env = {
        "BEDROCK_REGION": "us-east-2",
        "AWS_REGION": "eu-west-1",
        "AWS_DEFAULT_REGION": "ap-south-1",
    }
    assert Settings.from_env(env=env).region == "us-east-2"


def test_region_falls_back_to_aws_region():
    env = {"AWS_REGION": "eu-west-1"}
    assert Settings.from_env(env=env).region == "eu-west-1"


def test_overrides_from_env():
    env = {
        "BEDROCK_MODEL_ID": "openai.gpt-5.4",
        "BEDROCK_MAX_TOKENS": "512",
        "BEDROCK_TEMPERATURE": "0.2",
    }
    settings = Settings.from_env(env=env)
    assert settings.model_id == "openai.gpt-5.4"
    assert settings.max_tokens == 512
    assert settings.temperature == 0.2
