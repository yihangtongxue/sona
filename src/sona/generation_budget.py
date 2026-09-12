"""Conservative local request sizing, without downloading model tokenizers."""

from dataclasses import dataclass


class GenerationLimitError(ValueError):
    """A size failure that may be resolved by sending a smaller, new chunk."""


@dataclass(frozen=True)
class GenerationBudget:
    context_tokens: int = 8192
    output_tokens: int = 4096
    reasoning_tokens: int = 0
    margin: int = 256

    def output_limit(self, instruction, content, output_hint=None):
        # One token per UTF-8 byte is deliberately conservative for ordinary
        # text tokenizers. Include message framing, JSON escaping and headroom.
        # Provider-specific tokenizers/unknown proxy limits can still disagree;
        # those errors must be handled by bounded subdivision, never truncation.
        inputs = len(instruction.encode('utf-8')) + len(content.encode('utf-8')) + 64
        requested = self.output_tokens if output_hint is None else max(
            512 + self.reasoning_tokens,
            (len(output_hint.encode('utf-8')) * 5 + 3) // 4 + 128 + self.reasoning_tokens,
        )
        if requested > self.output_tokens or inputs + requested + self.margin > self.context_tokens:
            raise GenerationLimitError('本段文字超过模型请求预算，需要进一步分段。')
        return requested

    def fits(self, instruction, content, output_hint):
        try:
            self.output_limit(instruction, content, output_hint)
            return True
        except GenerationLimitError:
            return False


def model_budget(profile, model, *, reasoning=False):
    import litellm

    catalog = getattr(litellm, 'model_cost', {})
    info = {}
    # An OpenAI-compatible alias does not establish which model is behind it.
    if profile['provider'] != 'openai-compatible' and isinstance(catalog, dict):
        info = catalog.get(model) or catalog.get(model.partition('/')[2]) or {}
    if not isinstance(info, dict):
        info = {}

    def positive(value, default):
        return value if type(value) is int and value > 0 else default

    # max_tokens in LiteLLM metadata is a legacy output limit, not a context size.
    context = positive(info.get('max_input_tokens'), 8192)
    output = min(8192 if reasoning else 4096,
                 positive(info.get('max_output_tokens'), 8192 if reasoning else 4096),
                 context // 2)
    return GenerationBudget(context, output, 2048 if reasoning else 0)
