import json

from tiktoken import Encoding

from danswer.natural_language_processing.utils import get_default_llm_tokenizer
from danswer.tools.tool import Tool


OPEN_AI_TOOL_CALLING_MODELS = {
    "gpt-3.5-turbo",
    "gpt-4-turbo",
    "gpt-4",
    "gpt-4o",
    "gpt-4o-mini",
}


def explicit_tool_calling_supported(model_provider: str, model_name: str) -> bool:
    if model_provider == "openai" and model_name in OPEN_AI_TOOL_CALLING_MODELS:
        return True

    return False


def compute_tool_tokens(tool: Tool, llm_tokenizer: Encoding | None = None) -> int:
    if not llm_tokenizer:
        llm_tokenizer = get_default_llm_tokenizer()
    return len(llm_tokenizer.encode(json.dumps(tool.tool_definition())))


def compute_all_tool_tokens(
    tools: list[Tool], llm_tokenizer: Encoding | None = None
) -> int:
    return sum(compute_tool_tokens(tool, llm_tokenizer) for tool in tools)
