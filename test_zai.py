"""Test Z.ai API key and model access."""

import os
from zai import ZaiClient
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("GLM_API_KEY")
model = os.environ.get("GLM_MODEL", "glm-5")

print(f"Testing Z.ai API...")
print(f"API Key: {api_key[:20]}...{api_key[-10:] if api_key else 'None'}")
print(f"Model: {model}")
print()

client = ZaiClient(api_key=api_key)

try:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Say 'API works!' in JSON format like {\"status\": \"API works!\"}"}],
        temperature=0.1,
        response_format={"type": "json_object"}
    )

    print("✓ API call successful!")
    print(f"Response: {response.choices[0].message.content}")

except Exception as e:
    print(f"✗ API call failed: {e}")
