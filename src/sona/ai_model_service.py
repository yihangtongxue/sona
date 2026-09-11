from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from .file_lock import FileLocked, exclusive_file_lock


logger = logging.getLogger(__name__)

AI_MODEL_PROVIDERS = {
    "openai": "OpenAI",
    "anthropic": "Claude",
    "google": "Google Gemini",
    "zhipu": "智谱 GLM",
    "zhipu-coding": "智谱 Coding Plan",
    "openai-compatible": "OpenAI 兼容接口",
}
DEFAULT_FEATURE = "default"
KEYRING_SERVICE = "Sona AI Models"
OFFICIAL_API_BASES = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "zhipu-coding": "https://open.bigmodel.cn/api/coding/paas/v4",
}


class CredentialError(ValueError):
    """A user-facing credential error without backend exception details."""


class ModelTestError(ValueError):
    """A safe explanation for a response that did not pass the text test."""


def model_operation(function):
    @wraps(function)
    def run(self, *args, **kwargs):
        try:
            with exclusive_file_lock(self._lock_path):
                self._cleanup_credentials()
                try:
                    return function(self, *args, **kwargs)
                finally:
                    self._cleanup_credentials()
        except FileLocked:
            raise ValueError("正在处理其他模型操作，请稍后重试。") from None
    return run


@dataclass(frozen=True)
class AIModelProfile:
    id: str
    name: str
    provider: str
    model_name: str
    base_url: str | None = None
    api_key_ref: str | None = None
    config: dict[str, object] | None = None


@dataclass(frozen=True)
class AIGenerationSession:
    profile: dict
    api_key: str = field(repr=False)


class AIModelRepository:
    def __init__(self, database) -> None:
        self.database = database

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def list_profiles(self) -> list[dict[str, object]]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT p.*, s.feature IS NOT NULL AS selected
                   FROM ai_model_profiles p
                   LEFT JOIN ai_model_selections s
                     ON s.model_id=p.id AND s.feature=?
                   ORDER BY p.created_at, p.id""",
                (DEFAULT_FEATURE,),
            ).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            record["selected"] = bool(record["selected"])
            record["has_api_key"] = bool(record.pop("api_key_ref"))
            record["config"] = json.loads(record.pop("config_json"))
            result.append(record)
        return result

    def get_profile(self, identifier: str) -> dict[str, object]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM ai_model_profiles WHERE id=?", (identifier,),
            ).fetchone()
        if row is None:
            raise ValueError("AI 模型不存在。")
        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        return record

    def insert(self, profile: AIModelProfile) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO ai_model_profiles
                   (id,name,provider,model_name,base_url,api_key_ref,config_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (profile.id, profile.name, profile.provider, profile.model_name,
                 profile.base_url, profile.api_key_ref,
                 json.dumps(profile.config or {}, ensure_ascii=False, sort_keys=True)),
            )
            db.execute("DELETE FROM ai_credential_cleanup WHERE key_ref=?", (profile.api_key_ref,))

    def update(self, profile: AIModelProfile) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_editable(db, profile.id)
            old = db.execute("SELECT * FROM ai_model_profiles WHERE id=?", (profile.id,)).fetchone()
            if old is None:
                raise ValueError("AI 模型不存在。")
            reset_test = (
                old["provider"] != profile.provider
                or old["model_name"] != profile.model_name
                or (old["base_url"] or OFFICIAL_API_BASES.get(old["provider"], ""))
                != (profile.base_url or OFFICIAL_API_BASES.get(profile.provider, ""))
                or old["api_key_ref"] != profile.api_key_ref
                or json.loads(old["config_json"]) != (profile.config or {})
            )
            changed = db.execute(
                """UPDATE ai_model_profiles SET name=?, provider=?, model_name=?,
                   base_url=?, api_key_ref=?, config_json=?,
                   status=CASE WHEN ? THEN 'untested' ELSE status END,
                   last_tested_at=CASE WHEN ? THEN NULL ELSE last_tested_at END,
                   last_error=CASE WHEN ? THEN '' ELSE last_error END,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
                (profile.name, profile.provider, profile.model_name, profile.base_url,
                 profile.api_key_ref,
                 json.dumps(profile.config or {}, ensure_ascii=False, sort_keys=True),
                 reset_test, reset_test, reset_test, profile.id),
            )
            if old["api_key_ref"] and old["api_key_ref"] != profile.api_key_ref:
                db.execute("INSERT OR IGNORE INTO ai_credential_cleanup VALUES (?)", (old["api_key_ref"],))
            db.execute("DELETE FROM ai_credential_cleanup WHERE key_ref=?", (profile.api_key_ref,))
        if not changed.rowcount:
            raise ValueError("AI 模型不存在。")

    @staticmethod
    def _assert_editable(db: sqlite3.Connection, identifier: str) -> None:
        if db.execute(
            "SELECT 1 FROM ai_model_selections WHERE feature=? AND model_id=?",
            (DEFAULT_FEATURE, identifier),
        ).fetchone():
            raise ValueError("默认 AI 模型不能编辑，请先设置其他默认模型。")

    def assert_editable(self, identifier: str) -> None:
        with self.connection() as db:
            self._assert_editable(db, identifier)

    def delete(self, identifier: str) -> str | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            selected = db.execute(
                "SELECT 1 FROM ai_model_selections WHERE feature=? AND model_id=?",
                (DEFAULT_FEATURE, identifier),
            ).fetchone()
            if selected:
                raise ValueError("默认 AI 模型不能删除，请先设置其他默认模型。")
            row = db.execute(
                "SELECT api_key_ref FROM ai_model_profiles WHERE id=?", (identifier,),
            ).fetchone()
            if row is None:
                raise ValueError("AI 模型不存在。")
            db.execute("DELETE FROM ai_model_profiles WHERE id=?", (identifier,))
            if row["api_key_ref"]:
                db.execute("INSERT OR IGNORE INTO ai_credential_cleanup VALUES (?)", (row["api_key_ref"],))
        return row["api_key_ref"]

    def select(self, identifier: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            model = db.execute(
                "SELECT status FROM ai_model_profiles WHERE id=?", (identifier,),
            ).fetchone()
            if model is None:
                raise ValueError("AI 模型不存在。")
            if model["status"] != "ready":
                raise ValueError("请先测试连接，通过后才能设为默认模型。")
            db.execute(
                """INSERT INTO ai_model_selections(feature,model_id)
                   VALUES (?,?) ON CONFLICT(feature) DO UPDATE SET
                   model_id=excluded.model_id, selected_at=CURRENT_TIMESTAMP""",
                (DEFAULT_FEATURE, identifier),
            )

    def save_test_result(self, identifier: str, *, success: bool, error: str = "") -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE ai_model_profiles SET status=?, last_tested_at=CURRENT_TIMESTAMP,
                   last_error=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
                ("ready" if success else "failed", error, identifier),
            )


class AIModelService:
    """Manages user-defined remote AI model profiles, not speech assets."""

    def __init__(self, database) -> None:
        self.repository = AIModelRepository(database)
        self._lock_path = Path(database).with_suffix(".ai-models.lock")

    def list_models(self) -> list[dict[str, object]]:
        return self.repository.list_profiles()

    def get_model_api_key(self, identifier: str) -> str:
        """Read a credential only for the editor, never as part of the model list."""
        profile = self.repository.get_profile(identifier)
        secret = self._read_secret(profile.get("api_key_ref"))
        if profile.get("api_key_ref") and not secret:
            raise CredentialError("未找到已保存的 API Key，请重新填写。")
        return secret

    @model_operation
    def add_model(
        self, name: str, provider: str, model_name: str,
        base_url: str = "", api_key: str = "", config: dict | None = None,
    ) -> list[dict[str, object]]:
        profile = self._profile(
            name, provider, model_name, base_url, api_key, config,
        )
        self.repository.insert(self._attach_secret(profile, api_key))
        return self.list_models()

    @model_operation
    def update_model(
        self, identifier: str, name: str, provider: str, model_name: str,
        base_url: str = "", api_key: str = "", config: dict | None = None,
    ) -> list[dict[str, object]]:
        self.repository.assert_editable(identifier)
        current = self.repository.get_profile(identifier)
        profile = self._profile(
            name, provider, model_name, base_url,
            api_key, config, identifier=identifier,
        )
        # Reuse unchanged credentials; an unreadable old key must not block replacement.
        try:
            old_secret = self._read_secret(current.get("api_key_ref"))
        except CredentialError:
            old_secret = ""
        if old_secret and old_secret == api_key.strip():
            profile = replace(profile, api_key_ref=current["api_key_ref"])
        else:
            profile = self._attach_secret(profile, api_key)
        self.repository.update(profile)
        return self.list_models()

    @model_operation
    def delete_model(self, identifier: str) -> list[dict[str, object]]:
        self.repository.delete(identifier)
        return self.list_models()

    @model_operation
    def select_model(self, identifier: str) -> list[dict[str, object]]:
        self.repository.get_profile(identifier)
        self.repository.select(identifier)
        return self.list_models()

    @model_operation
    def test_model(self, identifier: str) -> list[dict[str, object]]:
        profile = self.repository.get_profile(identifier)
        api_key = ""
        try:
            from litellm import completion

            kwargs = {
                "model": self._litellm_model(profile),
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "num_retries": 0,
                "api_base": self._api_base(profile),
                **self._test_options(profile),
            }
            api_key = self._read_secret(profile.get("api_key_ref"))
            if not api_key:
                raise CredentialError("未找到已保存的 API Key，请编辑模型并重新填写。")
            kwargs["api_key"] = api_key
            response = completion(**kwargs)
            self._log_test_response(identifier, response, kwargs["max_tokens"])
            self._validate_test_response(response)
        except Exception as error:
            message = self._connection_error(error)
            logger.warning("AI 模型连接测试失败 model=%s type=%s reason=%s",
                           identifier, type(error).__name__, message)
            self.repository.save_test_result(identifier, success=False, error=message)
        else:
            self.repository.save_test_result(identifier, success=True)
        return self.list_models()

    @model_operation
    def default_generation_profile(self) -> dict:
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT model_id FROM ai_model_selections WHERE feature=?", (DEFAULT_FEATURE,),
            ).fetchone()
        if row is None:
            raise ValueError("请先在设置中测试 AI 模型，并将测试通过的模型设为默认。")
        profile = self.repository.get_profile(row["model_id"])
        if profile["status"] != "ready":
            raise ValueError("默认 AI 模型尚未通过测试，请先到设置中测试连接。")
        return self._generation_profile(profile)

    @staticmethod
    def _generation_profile(profile: dict) -> dict:
        # No secret is stored in the job, and no live foreign key binds its lifetime.
        return {key: profile[key] for key in
                ("id", "provider", "model_name", "base_url", "api_key_ref", "config")}

    @model_operation
    def prepare_generation(self, snapshot: dict) -> AIGenerationSession:
        try:
            current = self.repository.get_profile(snapshot["id"])
        except ValueError:
            raise ModelTestError("任务使用的模型已删除，请设置默认模型后重试。") from None
        if self._generation_profile(current) != snapshot or current["status"] != "ready":
            raise ModelTestError("任务使用的模型配置已变化或测试未通过，请检查设置后重试。")
        secret = self._read_secret(snapshot["api_key_ref"])
        if not secret:
            raise CredentialError("无法读取 API Key，请检查模型配置后重试。")
        # Release the configuration lock before the long request; retain the key
        # only in memory so a later model switch cannot reroute this job.
        return AIGenerationSession(snapshot, secret)

    def generate_text(self, session: AIGenerationSession, instruction: str, content: str) -> str:
        try:
            from litellm import completion

            options = self._test_options(session.profile)
            options.update(max_tokens=8192 if "extra_body" in options else 4096, timeout=120)
            response = completion(
                model=self._litellm_model(session.profile),
                api_base=self._api_base(session.profile), api_key=session.api_key,
                messages=[{"role": "system", "content": instruction},
                          {"role": "user", "content": content}],
                num_retries=0, **options,
            )
            choices = getattr(response, "choices", None)
            choice = choices[0] if choices else None
            reason = self._test_finish_reason(choice)
            text = getattr(getattr(choice, "message", None), "content", None)
            logger.info("AI 文本生成响应 model=%s finish_reason=%s text_chars=%d",
                        session.profile["id"], reason, len(text) if isinstance(text, str) else 0)
            if reason == "length":
                raise ModelTestError("模型输出达到单次上限，文稿尚未完整生成，请换用支持更长输出的模型后重试。")
            if reason != "stop" or not isinstance(text, str) or not text.strip():
                raise ModelTestError("模型未返回完整正文，请检查模型权限或稍后重试。")
            return text.strip()
        except Exception as error:
            # Provider exceptions may contain the full request, URL and credentials.
            message = self._connection_error(error)
            if message == "暂时无法完成连接测试，请核对配置后重试。":
                message = "文稿生成失败，请检查模型配置或稍后重试。"
            raise ModelTestError(message) from None

    def _test_options(self, profile: dict[str, object]) -> dict[str, object]:
        # A short final answer can still require a reasoning budget.
        options: dict[str, object] = {"max_tokens": 1024, "timeout": 30}
        provider, _, model = self._litellm_model(profile).partition("/")
        if provider == "zai" and model.lower() in {"glm-5.3", "glm-5.3-flash"}:
            # Official GLM-5.3 docs: thinking cannot be disabled; low is supported.
            # extra_body passes through parameters absent from LiteLLM's ZAI allowlist.
            options.update({
                "max_tokens": 4096,
                "timeout": 45,
                "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
            })
        return options

    @staticmethod
    def _test_finish_reason(choice: object) -> str:
        fields = getattr(choice, "provider_specific_fields", None)
        native = fields.get("native_finish_reason") if isinstance(fields, dict) else None
        # Keep vendor-supplied text out of logs; LiteLLM can normalize native errors.
        allowed = {"stop", "length", "tool_calls", "function_call", "content_filter",
                   "sensitive", "network_error", "model_context_window_exceeded"}
        for reason in (native, getattr(choice, "finish_reason", None)):
            if isinstance(reason, str) and reason in allowed:
                return reason
        return "unknown"

    def _log_test_response(self, identifier: str, response: object, max_tokens: object) -> None:
        choices = getattr(response, "choices", None)
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        reasoning = getattr(message, "reasoning_content", None)
        logger.info(
            "AI 模型测试响应 model=%s finish_reason=%s text_chars=%d reasoning_chars=%d max_tokens=%s",
            identifier, self._test_finish_reason(choice),
            len(content) if isinstance(content, str) else 0,
            len(reasoning) if isinstance(reasoning, str) else 0, max_tokens,
        )

    def _validate_test_response(self, response: object) -> None:
        choices = getattr(response, "choices", None)
        if not choices:
            raise ModelTestError("接口未返回有效回复，请检查模型标识和 API 地址。")
        choice = choices[0]
        content = getattr(getattr(choice, "message", None), "content", None)
        finish_reason = self._test_finish_reason(choice)
        if finish_reason in {"content_filter", "sensitive"}:
            raise ModelTestError("测试回复被供应商拦截，请检查模型权限或内容限制。")
        if finish_reason == "network_error":
            raise ModelTestError("供应商生成回复时发生异常，请稍后重试。")
        if finish_reason == "model_context_window_exceeded":
            raise ModelTestError("测试请求超出模型上下文限制，请核对模型配置。")
        if finish_reason == "length":
            raise ModelTestError("单次测试输出达到上限，未取得完整回复；这不表示套餐额度耗尽。")
        if not isinstance(content, str) or not content.strip():
            raise ModelTestError("接口未返回有效文本，暂无法确认模型可用。")

    def _profile(
        self, name: str, provider: str, model_name: str,
        base_url: str, api_key: str, config: dict | None,
        *, identifier: str | None = None,
    ) -> AIModelProfile:
        provider = self._text(provider, "供应商", 40)
        if provider not in AI_MODEL_PROVIDERS:
            raise ValueError("不支持的 AI 模型供应商。")
        model_name = self._text(model_name, "模型标识", 255)
        name = name.strip() if isinstance(name, str) else name
        name = self._text(name or model_name[:100], "模型名称", 100)
        base_url = self._validate_url(base_url)
        if provider == "openai-compatible" and not base_url:
            raise ValueError("请填写服务商提供的 API 地址。")
        config = {} if config is None else config
        if not isinstance(config, dict):
            raise ValueError("模型高级配置格式无效。")
        try:
            json.dumps(config, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValueError("模型高级配置必须是 JSON 对象。") from error
        if not isinstance(api_key, str):
            raise ValueError("API Key 格式无效，请重新粘贴。")
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("请填写 API Key。")
        if len(api_key) > 4096:
            raise ValueError("API Key 过长。")
        profile_id = identifier or str(uuid.uuid4())
        return AIModelProfile(
            id=profile_id, name=name, provider=provider,
            model_name=model_name, base_url=base_url, config=config,
        )

    def _attach_secret(self, profile: AIModelProfile, api_key: str) -> AIModelProfile:
        secret = api_key.strip()
        if not secret:
            raise ValueError("请填写 API Key。")
        key_ref = str(uuid.uuid4())
        # Reserve cleanup before writing; committing a profile claims the reference.
        # Interrupted saves and failed keyring deletes are retried on the next operation.
        with self.repository.connection() as db:
            db.execute("INSERT INTO ai_credential_cleanup VALUES (?)", (key_ref,))
        self._save_secret(key_ref, secret)
        return replace(profile, api_key_ref=key_ref)

    def _cleanup_credentials(self) -> None:
        with self.repository.connection() as db:
            pending = db.execute("SELECT key_ref FROM ai_credential_cleanup").fetchall()
        for row in pending:
            try:
                self._delete_secret(row["key_ref"])
                with self.repository.connection() as db:
                    db.execute("DELETE FROM ai_credential_cleanup WHERE key_ref=?", (row["key_ref"],))
            except Exception:
                logger.warning("AI 凭据清理未完成，将在下次模型操作时重试。")

    @staticmethod
    def _api_base(profile: dict) -> str:
        return profile.get("base_url") or OFFICIAL_API_BASES.get(profile["provider"], "")

    @staticmethod
    def _connection_error(error: Exception) -> str:
        if isinstance(error, (CredentialError, ModelTestError)):
            return str(error)
        status = getattr(error, "status_code", None)
        if status == 401:
            return "API Key 未通过验证，请确认它来自所选供应商，并重新粘贴完整密钥。"
        if status == 403:
            return "当前账号无权使用此模型，请在供应商控制台检查模型权限。"
        if status == 404:
            return "未找到该模型或接口，请核对模型标识和 API 地址。"
        if status in (402, 429):
            return "账号额度不足或请求过于频繁，请检查余额、额度，或稍后重试。"
        if status == 400:
            return "供应商未接受此次请求，请核对模型标识，并确认该模型支持文本对话。"
        if isinstance(status, int) and status >= 500:
            return "供应商服务暂时不可用，请稍后重试。"
        if "Timeout" in type(error).__name__ or "Connection" in type(error).__name__:
            return "连接超时或网络不可达，请检查网络连接及代理设置后重试。"
        if isinstance(error, ImportError):
            return "模型连接组件未安装，请先同步项目依赖。"
        return "暂时无法完成连接测试，请核对配置后重试。"

    @staticmethod
    def _text(value: str, label: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f"请填写{label}，且不超过 {limit} 个字符。")
        return value.strip()

    @staticmethod
    def _validate_url(value: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("API 地址必须是 http 或 https 地址。")
        return value.rstrip("/")

    @staticmethod
    def _litellm_model(profile: dict[str, object]) -> str:
        config = profile.get("config") or {}
        override = config.get("litellm_model")
        if isinstance(override, str) and override.strip():
            return override.strip()

        provider = profile["provider"]
        model_name = profile["model_name"]
        prefix = {
            "openai": "openai",
            "anthropic": "anthropic",
            "google": "gemini",
            "zhipu": "zai",
            "zhipu-coding": "zai",
            "openai-compatible": "openai",
        }[provider]
        return f"{prefix}/{model_name}"

    @staticmethod
    def _read_secret(key_ref: object) -> str:
        if not key_ref:
            return ""
        try:
            import keyring
            return keyring.get_password(KEYRING_SERVICE, str(key_ref)) or ""
        except ImportError:
            raise CredentialError("安全凭据组件未安装，请先同步项目依赖。") from None
        except Exception:
            raise CredentialError("无法读取密钥，请解锁系统凭据管理器后重试。") from None

    @staticmethod
    def _save_secret(key_ref: str, secret: str) -> None:
        try:
            import keyring
            keyring.set_password(KEYRING_SERVICE, key_ref, secret)
        except ImportError:
            raise CredentialError("安全凭据组件未安装，请先同步项目依赖。") from None
        except Exception:
            raise CredentialError("无法保存密钥，请允许 Sona 访问系统凭据管理器后重试。") from None

    @staticmethod
    def _delete_secret(key_ref: str) -> None:
        try:
            import keyring
            if keyring.get_password(KEYRING_SERVICE, key_ref) is not None:
                keyring.delete_password(KEYRING_SERVICE, key_ref)
        except ImportError:
            raise CredentialError("安全凭据组件未安装，请先同步项目依赖。") from None
        except Exception:
            raise CredentialError("暂时无法清理密钥，将在下次模型操作时重试。") from None
