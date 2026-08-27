import os
import json

import pytest
import arxiv
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from openai import OpenAI
from dotenv import load_dotenv

from pipeline.models import Preferences

load_dotenv()

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
REQUIRED_ENV_KEYS = [
    "OPENAI_API_KEY",
    "SENDER_EMAIL",
    "RECEIVER_EMAIL",
    "APP_PASSWORD",
    "BUCKET_NAME",
]


# Test to check connection to arxiv
def test_arxiv_connection():
    search = arxiv.Search(query="cat:cs.AI", max_results=1)
    results = list(arxiv.Client().results(search))
    assert len(results) == 1


# Test to check openai_api key credits
def test_openai_api_key_credits():
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    try:
        response = client.models.list()
    except Exception as e:
        pytest.fail(f"OpenAI API key is invalid or has no available credits: {e}")
    assert len(response.data) > 0


# Test to check S3 bucket
def test_s3_bucket_connection():
    bucket_name = os.getenv("BUCKET_NAME")
    assert bucket_name, "BUCKET_NAME is not set in the environment"

    s3 = boto3.client("s3")
    try:
        s3.head_bucket(Bucket=bucket_name)
    except (ClientError, NoCredentialsError) as e:
        pytest.fail(f"Could not connect to S3 bucket '{bucket_name}': {e}")


# Test valid data values for configuration file
def test_config_json_is_valid():
    with open(CONFIG_PATH, "r") as f:
        config_data = json.load(f)

    # will raise a validation error if any field is missing or the wrong type
    preferences = Preferences(**config_data)

    assert len(preferences.arxiv_categories) > 0
    assert len(preferences.keywords) > 0
    assert preferences.papers_per_digest > 0
    assert preferences.max_papers_fetched_per_category > 0
    assert preferences.papers_per_digest <= preferences.max_papers_fetched_per_category


# Test if .env file is populated with appropriate keys
def test_env_file_has_required_keys():
    for key in REQUIRED_ENV_KEYS:
        value = os.getenv(key)
        assert value, f"Missing or empty required .env key: {key}"
