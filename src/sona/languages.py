"""Whisper language choices shared by the settings UI and MCP tools."""

AUTO = "auto"

# Whisper language codes; labels are shown in the settings UI.
LANGUAGES = {
    "zh": "中文", "yue": "粤语", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语",
    "ru": "俄语", "uk": "乌克兰语", "ar": "阿拉伯语", "hi": "印地语", "vi": "越南语",
    "th": "泰语", "id": "印尼语", "ms": "马来语", "tr": "土耳其语", "nl": "荷兰语",
    "pl": "波兰语", "sv": "瑞典语",
}


def normalize_language(value) -> str:
    """Return a supported Whisper code or ``auto``; reject anything else."""
    if value is None:
        return AUTO
    if not isinstance(value, str):
        raise ValueError("识别语言格式无效。")
    code = value.strip().lower().replace("_", "-")
    if code in ("", AUTO, "automatic", "detect"):
        return AUTO
    if code in ("zh-cn", "zh-hans", "zh-tw", "zh-hant", "cmn", "chinese", "中文"):
        return "zh"
    if code in ("english", "en-us", "en-gb"):
        return "en"
    if code not in LANGUAGES:
        supported = "、".join(LANGUAGES)
        raise ValueError(f"不支持的识别语言：{value}。可用：auto、{supported}。")
    return code


def language_options() -> list[dict]:
    return [{"code": AUTO, "label": "自动识别"}, *({"code": code, "label": label} for code, label in LANGUAGES.items())]
