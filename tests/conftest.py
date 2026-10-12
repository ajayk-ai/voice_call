import os

# Tests never send traces to LangSmith, even when .env enables tracing (load_dotenv doesn't override this)
os.environ["LANGSMITH_TRACING"] = "false"
