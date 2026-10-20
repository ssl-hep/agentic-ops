"""Keep tests off the real LLM endpoint, MLflow server and MCP gateway configured in the repo-root .env.

Environment variables take precedence over .env files, so blanking them here (before the app reads
its settings) disables the agent and tracking unless a test wires in fakes.
"""

import os

for _name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "MLFLOW_TRACKING_URI", "MCP_URL", "MCP_BEARER_TOKEN"):
    os.environ[_name] = ""
for _name in ("MLFLOW_TRACKING_USERNAME", "MLFLOW_TRACKING_PASSWORD"):
    os.environ.pop(_name, None)
