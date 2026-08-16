from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Callable, Optional

from reader_api.mobile_workspace import call_hermes_runtime


VOICE_HERMES_ADAPTER_VERSION = "click.voice.hermes.adapter.v1"
VOICE_HERMES_CLEANUP_INPUT_SCHEMA = "click.voice.hermes.cleanup.input.v1"
VOICE_HERMES_CLEANUP_OUTPUT_SCHEMA = "click.voice.hermes.cleanup.output.v1"
VOICE_HERMES_UNDERSTAND_INPUT_SCHEMA = "click.voice.hermes.understand.input.v1"
VOICE_HERMES_UNDERSTAND_OUTPUT_SCHEMA = "click.voice.hermes.understand.output.v1"
VOICE_HERMES_RELEVANCE_INPUT_SCHEMA = "click.voice.hermes.reading_relevance.input.v1"
VOICE_HERMES_RELEVANCE_OUTPUT_SCHEMA = "click.voice.hermes.reading_relevance.output.v1"
VOICE_HERMES_DISCUSS_INPUT_SCHEMA = "click.voice.hermes.discuss.input.v1"
VOICE_HERMES_DISCUSS_OUTPUT_SCHEMA = "click.voice.hermes.discuss.output.v1"

_CLEANUP_CHANGE_TYPES = {"punctuation", "paragraph", "homophone", "proper_noun"}
_LEXICAL_CHANGE_TYPES = {"homophone", "proper_noun"}
_INTENT_TYPES = {
    "task",
    "idea",
    "question",
    "review",
    "reading_note",
    "meeting_note",
    "material",
    "memo",
    "unknown",
}
_ACTION_TYPES = {
    "create_note",
    "create_task",
    "append_reading_note",
    "create_review_item",
    "add_to_knowledge_base",
    "ask_followup",
    "archive",
    "no_action",
}
_EXTERNAL_WRITE_ACTIONS = {
    "create_note",
    "create_task",
    "append_reading_note",
    "create_review_item",
    "add_to_knowledge_base",
    "archive",
}


class VoiceHermesError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        called_hermes: bool,
        response: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.called_hermes = called_hermes
        self.response = response or {}


def _validate_receipt_output(
    receipt: dict[str, Any],
    validator: Callable[..., dict[str, Any]],
    *args: Any,
) -> dict[str, Any]:
    try:
        receipt["output"] = validator(receipt["output"], *args)
    except VoiceHermesError as exc:
        if not exc.response:
            exc.response = receipt.get("raw_response") if isinstance(receipt.get("raw_response"), dict) else {}
        raise
    return receipt


def _parse_exact_json_object(text: str, *, operation: str) -> dict[str, Any]:
    candidate = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.I | re.S)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise VoiceHermesError(
            f"Hermes {operation} 返回了无效 JSON：{exc.msg}",
            operation=operation,
            called_hermes=True,
        ) from exc
    if not isinstance(parsed, dict):
        raise VoiceHermesError(
            f"Hermes {operation} 返回的顶层结构不是对象",
            operation=operation,
            called_hermes=True,
        )
    return parsed


def _required_string(payload: dict[str, Any], key: str, *, operation: str, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VoiceHermesError(
            f"Hermes {operation} 缺少有效字段 {key}",
            operation=operation,
            called_hermes=True,
        )
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise VoiceHermesError(
            f"Hermes {operation} 字段 {key} 超过长度限制",
            operation=operation,
            called_hermes=True,
        )
    return cleaned


def _strict_bool(payload: dict[str, Any], key: str, *, operation: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise VoiceHermesError(
            f"Hermes {operation} 字段 {key} 必须是布尔值",
            operation=operation,
            called_hermes=True,
        )
    return value


def _confidence(value: Any, *, operation: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VoiceHermesError(
            f"Hermes {operation} 字段 {field} 必须是 0 到 1 的数字",
            operation=operation,
            called_hermes=True,
        )
    number = float(value)
    if number < 0.0 or number > 1.0:
        raise VoiceHermesError(
            f"Hermes {operation} 字段 {field} 超出 0 到 1",
            operation=operation,
            called_hermes=True,
        )
    return number


def _semantic_skeleton(text: str) -> str:
    return "".join(
        character
        for character in text
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _validate_cleanup_output(payload: dict[str, Any], raw_text: str) -> dict[str, Any]:
    operation = "忠实整理"
    if payload.get("schema") != VOICE_HERMES_CLEANUP_OUTPUT_SCHEMA:
        raise VoiceHermesError(
            "Hermes 忠实整理返回了不兼容的 schema",
            operation=operation,
            called_hermes=True,
        )
    cleaned_text = _required_string(payload, "cleaned_text", operation=operation, limit=200_000)
    meaning_changed = _strict_bool(payload, "meaning_changed", operation=operation)
    needs_review = _strict_bool(payload, "needs_review", operation=operation)
    if meaning_changed:
        raise VoiceHermesError(
            "Hermes 判断整理结果改变了原意，已拒绝该版本并保留原始转写",
            operation=operation,
            called_hermes=True,
        )
    if len(cleaned_text) > max(len(raw_text) + 80, int(len(raw_text) * 1.35)):
        raise VoiceHermesError(
            "Hermes 整理结果扩写过多，已拒绝该版本",
            operation=operation,
            called_hermes=True,
        )

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        raise VoiceHermesError(
            "Hermes 忠实整理字段 changes 必须是数组",
            operation=operation,
            called_hermes=True,
        )
    changes: list[dict[str, Any]] = []
    lexical_changes = 0
    for index, item in enumerate(raw_changes):
        if not isinstance(item, dict):
            raise VoiceHermesError(
                f"Hermes 忠实整理 changes[{index}] 不是对象",
                operation=operation,
                called_hermes=True,
            )
        change_type = str(item.get("type") or "").strip()
        if change_type not in _CLEANUP_CHANGE_TYPES:
            raise VoiceHermesError(
                f"Hermes 忠实整理 changes[{index}] 使用了不允许的类型",
                operation=operation,
                called_hermes=True,
            )
        before = item.get("from")
        after = item.get("to")
        if not isinstance(before, str) or not isinstance(after, str):
            raise VoiceHermesError(
                f"Hermes 忠实整理 changes[{index}] 缺少 from/to",
                operation=operation,
                called_hermes=True,
            )
        reason = _required_string(item, "reason", operation=operation, limit=500)
        confidence = _confidence(item.get("confidence"), operation=operation, field=f"changes[{index}].confidence")
        if change_type in _LEXICAL_CHANGE_TYPES:
            lexical_changes += 1
            if before and before not in raw_text:
                raise VoiceHermesError(
                    f"Hermes 忠实整理 changes[{index}] 的原词不在原始转写中",
                    operation=operation,
                    called_hermes=True,
                )
            if after and after not in cleaned_text:
                raise VoiceHermesError(
                    f"Hermes 忠实整理 changes[{index}] 的改词不在整理结果中",
                    operation=operation,
                    called_hermes=True,
                )
            if confidence < 0.9:
                needs_review = True
        changes.append(
            {
                "from": before,
                "to": after,
                "type": change_type,
                "reason": reason,
                "confidence": confidence,
            }
        )

    raw_skeleton = _semantic_skeleton(raw_text)
    cleaned_skeleton = _semantic_skeleton(cleaned_text)
    if raw_skeleton != cleaned_skeleton and lexical_changes == 0:
        raise VoiceHermesError(
            "Hermes 修改了词句但没有逐项说明，已拒绝该整理版本",
            operation=operation,
            called_hermes=True,
        )
    if raw_skeleton and cleaned_skeleton:
        similarity = SequenceMatcher(None, raw_skeleton, cleaned_skeleton).ratio()
        if similarity < 0.82:
            raise VoiceHermesError(
                "Hermes 整理结果与原始转写差异过大，已拒绝该版本",
                operation=operation,
                called_hermes=True,
            )
    return {
        "schema": VOICE_HERMES_CLEANUP_OUTPUT_SCHEMA,
        "cleaned_text": cleaned_text,
        "changes": changes,
        "meaning_changed": False,
        "needs_review": needs_review,
    }


def _validate_evidence_spans(value: Any, transcript: str, *, field: str) -> list[dict[str, Any]]:
    operation = "结构化理解"
    if not isinstance(value, list) or not value:
        raise VoiceHermesError(
            f"Hermes 结构化理解字段 {field} 必须包含原话证据",
            operation=operation,
            called_hermes=True,
        )
    spans: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise VoiceHermesError(
                f"Hermes 结构化理解字段 {field}[{index}] 不是对象",
                operation=operation,
                called_hermes=True,
            )
        text = item.get("text")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(text, str) or not text or not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
            raise VoiceHermesError(
                f"Hermes 结构化理解字段 {field}[{index}] 缺少有效 text/start/end",
                operation=operation,
                called_hermes=True,
            )
        if start < 0 or end <= start or end > len(transcript) or transcript[start:end] != text:
            raise VoiceHermesError(
                f"Hermes 结构化理解字段 {field}[{index}] 无法回到当前转写",
                operation=operation,
                called_hermes=True,
            )
        spans.append({"text": text, "start": start, "end": end})
    return spans


def _validate_understand_output(payload: dict[str, Any], transcript: str) -> dict[str, Any]:
    operation = "结构化理解"
    if payload.get("schema") != VOICE_HERMES_UNDERSTAND_OUTPUT_SCHEMA:
        raise VoiceHermesError(
            "Hermes 结构化理解返回了不兼容的 schema",
            operation=operation,
            called_hermes=True,
        )
    title = _required_string(payload, "title", operation=operation, limit=18)
    summary = _required_string(payload, "summary", operation=operation, limit=2000)
    intent_type = str(payload.get("intent_type") or "").strip()
    if intent_type not in _INTENT_TYPES:
        raise VoiceHermesError(
            "Hermes 结构化理解返回了不支持的 intent_type",
            operation=operation,
            called_hermes=True,
        )
    confidence = _confidence(payload.get("confidence"), operation=operation, field="confidence")
    evidence_spans = _validate_evidence_spans(payload.get("evidence_spans"), transcript, field="evidence_spans")
    requires_confirmation = _strict_bool(payload, "requires_confirmation", operation=operation)
    followup = payload.get("followup_question")
    if followup is not None and (not isinstance(followup, str) or not followup.strip()):
        raise VoiceHermesError(
            "Hermes 结构化理解字段 followup_question 必须是有效文本或 null",
            operation=operation,
            called_hermes=True,
        )
    actions_raw = payload.get("actions")
    if not isinstance(actions_raw, list) or not actions_raw:
        raise VoiceHermesError(
            "Hermes 结构化理解必须明确返回至少一个 action",
            operation=operation,
            called_hermes=True,
        )
    actions: list[dict[str, Any]] = []
    for index, item in enumerate(actions_raw):
        if not isinstance(item, dict):
            raise VoiceHermesError(
                f"Hermes 结构化理解 actions[{index}] 不是对象",
                operation=operation,
                called_hermes=True,
            )
        action_type = str(item.get("action_type") or "").strip()
        if action_type not in _ACTION_TYPES:
            raise VoiceHermesError(
                f"Hermes 结构化理解 actions[{index}] 类型不受支持",
                operation=operation,
                called_hermes=True,
            )
        risk = str(item.get("risk") or "").strip()
        if risk not in {"low", "medium", "high"}:
            raise VoiceHermesError(
                f"Hermes 结构化理解 actions[{index}] risk 无效",
                operation=operation,
                called_hermes=True,
            )
        target = item.get("target")
        if not isinstance(target, dict):
            raise VoiceHermesError(
                f"Hermes 结构化理解 actions[{index}] target 必须是对象",
                operation=operation,
                called_hermes=True,
            )
        action_confirmation = _strict_bool(item, "requires_confirmation", operation=operation)
        if action_type in _EXTERNAL_WRITE_ACTIONS:
            action_confirmation = True
        actions.append(
            {
                "action_type": action_type,
                "title": _required_string(item, "title", operation=operation, limit=80),
                "body": _required_string(item, "body", operation=operation, limit=5000),
                "target": target,
                "risk": risk,
                "requires_confirmation": action_confirmation,
                "evidence_spans": _validate_evidence_spans(
                    item.get("evidence_spans"),
                    transcript,
                    field=f"actions[{index}].evidence_spans",
                ),
            }
        )
    if confidence < 0.7 or any(action["requires_confirmation"] for action in actions):
        requires_confirmation = True
    return {
        "schema": VOICE_HERMES_UNDERSTAND_OUTPUT_SCHEMA,
        "title": title,
        "summary": summary,
        "intent_type": intent_type,
        "confidence": confidence,
        "evidence_spans": evidence_spans,
        "requires_confirmation": requires_confirmation,
        "followup_question": followup.strip() if isinstance(followup, str) else None,
        "actions": actions,
    }


def _validate_relevance_output(payload: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    operation = "阅读相关性判断"
    if payload.get("schema") != VOICE_HERMES_RELEVANCE_OUTPUT_SCHEMA:
        raise VoiceHermesError(
            "Hermes 阅读相关性判断返回了不兼容的 schema",
            operation=operation,
            called_hermes=True,
        )
    decisions_raw = payload.get("decisions")
    if not isinstance(decisions_raw, list):
        raise VoiceHermesError(
            "Hermes 阅读相关性判断字段 decisions 必须是数组",
            operation=operation,
            called_hermes=True,
        )
    candidate_map = {str(candidate["book_id"]): candidate for candidate in candidates}
    if len(decisions_raw) != len(candidate_map):
        raise VoiceHermesError(
            "Hermes 阅读相关性判断没有逐本返回决定",
            operation=operation,
            called_hermes=True,
        )
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    relevant_count = 0
    for index, item in enumerate(decisions_raw):
        if not isinstance(item, dict):
            raise VoiceHermesError(
                f"Hermes 阅读相关性判断 decisions[{index}] 不是对象",
                operation=operation,
                called_hermes=True,
            )
        book_id = str(item.get("book_id") or "")
        if book_id not in candidate_map or book_id in seen:
            raise VoiceHermesError(
                f"Hermes 阅读相关性判断 decisions[{index}] 的 book_id 无效或重复",
                operation=operation,
                called_hermes=True,
            )
        seen.add(book_id)
        relevant = _strict_bool(item, "relevant", operation=operation)
        score = _confidence(item.get("score"), operation=operation, field=f"decisions[{index}].score")
        reason = _required_string(item, "reason", operation=operation, limit=600)
        selected_ids = item.get("selected_evidence_ids")
        if not isinstance(selected_ids, list) or any(not isinstance(value, str) for value in selected_ids):
            raise VoiceHermesError(
                f"Hermes 阅读相关性判断 decisions[{index}].selected_evidence_ids 无效",
                operation=operation,
                called_hermes=True,
            )
        allowed_ids = {
            str(evidence["evidence_id"])
            for evidence in candidate_map[book_id].get("evidence") or []
            if evidence.get("evidence_id")
        }
        if len(set(selected_ids)) != len(selected_ids) or any(value not in allowed_ids for value in selected_ids):
            raise VoiceHermesError(
                f"Hermes 阅读相关性判断 decisions[{index}] 引用了不存在的候选证据",
                operation=operation,
                called_hermes=True,
            )
        if relevant and (score < 0.6 or not selected_ids):
            raise VoiceHermesError(
                f"Hermes 将《{candidate_map[book_id].get('title') or book_id}》判为相关，但没有足够证据",
                operation=operation,
                called_hermes=True,
            )
        if not relevant and selected_ids:
            raise VoiceHermesError(
                f"Hermes 对不相关书籍仍选择了证据：{book_id}",
                operation=operation,
                called_hermes=True,
            )
        if relevant:
            relevant_count += 1
        decisions.append(
            {
                "book_id": book_id,
                "relevant": relevant,
                "score": score,
                "reason": reason,
                "selected_evidence_ids": selected_ids,
            }
        )
    if relevant_count > 5:
        raise VoiceHermesError(
            "Hermes 阅读相关性判断选择了过多书籍",
            operation=operation,
            called_hermes=True,
        )
    no_relevant_reason = payload.get("no_relevant_reason")
    if relevant_count == 0:
        if not isinstance(no_relevant_reason, str) or not no_relevant_reason.strip():
            raise VoiceHermesError(
                "Hermes 没有选择相关书籍，但未说明原因",
                operation=operation,
                called_hermes=True,
            )
        no_relevant_reason = no_relevant_reason.strip()
    elif no_relevant_reason is not None and not isinstance(no_relevant_reason, str):
        raise VoiceHermesError(
            "Hermes 阅读相关性判断字段 no_relevant_reason 无效",
            operation=operation,
            called_hermes=True,
        )
    return {
        "schema": VOICE_HERMES_RELEVANCE_OUTPUT_SCHEMA,
        "decisions": decisions,
        "no_relevant_reason": no_relevant_reason or None,
    }


def _discussion_statements(
    value: Any,
    *,
    field: str,
    evidence_classes: dict[str, str],
    allowed_classes: set[str],
) -> list[dict[str, Any]]:
    operation = "灵感讨论"
    if not isinstance(value, list):
        raise VoiceHermesError(
            f"Hermes 灵感讨论字段 {field} 必须是数组",
            operation=operation,
            called_hermes=True,
        )
    output = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise VoiceHermesError(
                f"Hermes 灵感讨论字段 {field}[{index}] 不是对象",
                operation=operation,
                called_hermes=True,
            )
        statement = _required_string(item, "statement", operation=operation, limit=3000)
        citation_ids = item.get("citation_ids")
        if not isinstance(citation_ids, list) or not citation_ids or any(not isinstance(item_id, str) for item_id in citation_ids):
            raise VoiceHermesError(
                f"Hermes 灵感讨论字段 {field}[{index}] 缺少引用",
                operation=operation,
                called_hermes=True,
            )
        if len(set(citation_ids)) != len(citation_ids):
            raise VoiceHermesError(
                f"Hermes 灵感讨论字段 {field}[{index}] 有重复引用",
                operation=operation,
                called_hermes=True,
            )
        for citation_id in citation_ids:
            if evidence_classes.get(citation_id) not in allowed_classes:
                raise VoiceHermesError(
                    f"Hermes 灵感讨论字段 {field}[{index}] 引用了错误类型或不存在的证据",
                    operation=operation,
                    called_hermes=True,
                )
        output.append({"statement": statement, "citation_ids": citation_ids})
    return output


def _required_string_list(payload: dict[str, Any], key: str, *, operation: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise VoiceHermesError(
            f"Hermes {operation} 字段 {key} 必须至少包含一项",
            operation=operation,
            called_hermes=True,
        )
    output = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise VoiceHermesError(
                f"Hermes {operation} 字段 {key}[{index}] 不是有效文本",
                operation=operation,
                called_hermes=True,
            )
        output.append(item.strip()[:3000])
    return output


def _validate_discussion_output(payload: dict[str, Any], context_evidence: list[dict[str, Any]]) -> dict[str, Any]:
    operation = "灵感讨论"
    if payload.get("schema") != VOICE_HERMES_DISCUSS_OUTPUT_SCHEMA:
        raise VoiceHermesError(
            "Hermes 灵感讨论返回了不兼容的 schema",
            operation=operation,
            called_hermes=True,
        )
    evidence_classes = {
        str(item["evidence_id"]): str(item.get("evidence_class") or "")
        for item in context_evidence
        if item.get("evidence_id")
    }
    reading_evidence = {
        evidence_id: evidence_class
        for evidence_id, evidence_class in evidence_classes.items()
        if evidence_class in {"book_source", "user_annotation", "living_book_model"}
    }
    no_relevant_reading = _strict_bool(payload, "no_relevant_reading", operation=operation)
    if bool(reading_evidence) == no_relevant_reading:
        raise VoiceHermesError(
            "Hermes 灵感讨论对“是否有相关阅读证据”的声明与快照不一致",
            operation=operation,
            called_hermes=True,
        )
    book_evidence = _discussion_statements(
        payload.get("book_evidence"),
        field="book_evidence",
        evidence_classes=evidence_classes,
        allowed_classes={"book_source"},
    )
    book_logic = _discussion_statements(
        payload.get("book_logic_inference"),
        field="book_logic_inference",
        evidence_classes=evidence_classes,
        allowed_classes={"book_source", "living_book_model"},
    )
    if no_relevant_reading and (book_evidence or book_logic):
        raise VoiceHermesError(
            "Hermes 声明没有相关阅读，却仍生成了书中结论",
            operation=operation,
            called_hermes=True,
        )
    citation_ids: list[str] = []
    for item in [*book_evidence, *book_logic]:
        for citation_id in item["citation_ids"]:
            if citation_id not in citation_ids:
                citation_ids.append(citation_id)
    return {
        "schema": VOICE_HERMES_DISCUSS_OUTPUT_SCHEMA,
        "user_idea": _required_string(payload, "user_idea", operation=operation, limit=4000),
        "book_evidence": book_evidence,
        "book_logic_inference": book_logic,
        "hermes_synthesis": _required_string(payload, "hermes_synthesis", operation=operation, limit=6000),
        "uncertainty": _required_string(payload, "uncertainty", operation=operation, limit=3000),
        "counterexamples": _required_string_list(payload, "counterexamples", operation=operation),
        "next_validation": _required_string_list(payload, "next_validation", operation=operation),
        "no_relevant_reading": no_relevant_reading,
        "citation_ids": citation_ids,
    }


def format_discussion_content(output: dict[str, Any]) -> str:
    lines = ["用户原话", str(output["user_idea"]), ""]
    lines.append("书中明说")
    if output["book_evidence"]:
        lines.extend(f"- {item['statement']}" for item in output["book_evidence"])
    else:
        lines.append("- 当前证据快照中没有可归为书中明说的内容。")
    lines.extend(["", "按书中逻辑推演"])
    if output["book_logic_inference"]:
        lines.extend(f"- {item['statement']}" for item in output["book_logic_inference"])
    else:
        lines.append("- 当前没有足够证据进行书中逻辑推演。")
    lines.extend(["", "Hermes 综合判断", str(output["hermes_synthesis"]), "", "不确定性", str(output["uncertainty"]), "", "反例与边界"])
    lines.extend(f"- {item}" for item in output["counterexamples"])
    lines.extend(["", "下一步验证"])
    lines.extend(f"- {item}" for item in output["next_validation"])
    return "\n".join(lines).strip()


HermesTransport = Callable[..., dict[str, Any]]


class VoiceHermesAdapter:
    def __init__(self, transport: HermesTransport = call_hermes_runtime) -> None:
        self.transport = transport

    def _invoke(
        self,
        *,
        operation: str,
        input_payload: dict[str, Any],
        prompt: str,
        session_id: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        try:
            response = self.transport(prompt, session_id=session_id, timeout_seconds=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - preserve the actual transport failure.
            raise VoiceHermesError(
                f"Hermes {operation} 调用失败：{exc}",
                operation=operation,
                called_hermes=True,
            ) from exc
        if not isinstance(response, dict) or response.get("status") != "success":
            detail = response.get("error") if isinstance(response, dict) else "response is not an object"
            raise VoiceHermesError(
                f"Hermes {operation} 未成功：{detail or 'runtime returned failure'}",
                operation=operation,
                called_hermes=True,
                response=response if isinstance(response, dict) else {},
            )
        reply = response.get("reply") or response.get("text")
        if not isinstance(reply, str) or not reply.strip():
            raise VoiceHermesError(
                f"Hermes {operation} 返回了空内容",
                operation=operation,
                called_hermes=True,
                response=response,
            )
        try:
            output = _parse_exact_json_object(reply, operation=operation)
        except VoiceHermesError as exc:
            exc.response = response
            raise
        provider_info = response.get("provider_info") if isinstance(response.get("provider_info"), dict) else {}
        return {
            "adapter_version": VOICE_HERMES_ADAPTER_VERSION,
            "operation": operation,
            "input": input_payload,
            "prompt": prompt,
            "raw_response": response,
            "provider": str(provider_info.get("provider") or response.get("provider") or "hermes-runtime"),
            "model": str(provider_info.get("model") or response.get("model") or ""),
            "session_id": str(response.get("session_id") or session_id),
            "called_hermes": True,
            "output": output,
        }

    def clean_transcript(
        self,
        *,
        voice_record_id: str,
        transcript_version_id: Optional[str],
        raw_text: str,
        vocabulary: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        input_payload = {
            "schema": VOICE_HERMES_CLEANUP_INPUT_SCHEMA,
            "adapter_version": VOICE_HERMES_ADAPTER_VERSION,
            "voice_record_id": voice_record_id,
            "transcript_version_id": transcript_version_id,
            "raw_text": raw_text,
            "authorized_vocabulary": vocabulary or [],
        }
        prompt = f"""
你是 Click Voice 的忠实转写整理器。你的职责是让用户口述更易读，不是替用户重写观点。

允许：补标点、合理分段、在证据充分时修正明显错别字、同音词或专有名词。
禁止：删除任何词句或重复片段、补充论据、改善逻辑、润色成文章、删除犹豫或限定、让观点迎合其他内容。
只有 authorized_vocabulary 中明确支持的专有名词才可据此纠正。任何不确定的词汇修改必须 needs_review=true。

只返回严格 JSON，schema 必须是 {VOICE_HERMES_CLEANUP_OUTPUT_SCHEMA}，字段必须包含：
schema, cleaned_text, changes, meaning_changed, needs_review。
changes 每项必须包含 from, to, type, reason, confidence；type 只能是 punctuation / paragraph / homophone / proper_noun。
如果你判断原意发生变化，必须 meaning_changed=true，系统会拒绝该版本。

输入：
{json.dumps(input_payload, ensure_ascii=False, sort_keys=True)}
""".strip()
        receipt = self._invoke(
            operation="忠实整理",
            input_payload=input_payload,
            prompt=prompt,
            session_id=f"click_voice_cleanup_{voice_record_id}",
            timeout_seconds=180,
        )
        return _validate_receipt_output(receipt, _validate_cleanup_output, raw_text)

    def understand(
        self,
        *,
        voice_record: dict[str, Any],
        transcript_version: Optional[dict[str, Any]],
        transcript: str,
    ) -> dict[str, Any]:
        input_payload = {
            "schema": VOICE_HERMES_UNDERSTAND_INPUT_SCHEMA,
            "adapter_version": VOICE_HERMES_ADAPTER_VERSION,
            "voice_record": {
                "id": voice_record.get("id"),
                "source": voice_record.get("source"),
                "recorded_at": voice_record.get("recorded_at") or voice_record.get("created_at"),
                "duration_ms": voice_record.get("duration_ms"),
            },
            "transcript_version_id": (transcript_version or {}).get("id"),
            "transcript": transcript,
        }
        prompt = f"""
你是 Click Voice 的结构化理解器。只根据输入中的当前转写判断，不要补写事实，也不要引用尚未提供的书籍。

只返回严格 JSON，schema 必须是 {VOICE_HERMES_UNDERSTAND_OUTPUT_SCHEMA}。字段：
- schema
- title：18 个字符以内
- summary：1 到 2 句话
- intent_type：task / idea / question / review / reading_note / meeting_note / material / memo / unknown
- confidence：0 到 1
- evidence_spans：至少一项，每项 text/start/end 必须精确回到当前转写
- requires_confirmation：布尔值
- followup_question：字符串或 null
- actions：至少一项，每项含 action_type/title/body/target/risk/requires_confirmation/evidence_spans

action_type 只能是 create_note / create_task / append_reading_note / create_review_item / add_to_knowledge_base / ask_followup / archive / no_action。
risk 只能是 low / medium / high。任何外部写入、长期沉淀或归档动作都必须 requires_confirmation=true。
转写完成不等于处理完成；没有外部动作时明确返回 no_action，信息不足时返回 ask_followup，禁止省略 action。
每个 action.target 必须是 JSON 对象，禁止返回字符串、数组或 null。
no_action / ask_followup / archive 的 target 使用 {{"target_type":"voice_record","voice_record_id":"{voice_record.get('id')}"}}。
新建内容类动作至少使用 {{"target_type":"new_content"}}；只有输入里已有真实 book_id 或 annotation_id 时才能写入对应 target 字段。

输入：
{json.dumps(input_payload, ensure_ascii=False, sort_keys=True, default=str)}
""".strip()
        receipt = self._invoke(
            operation="结构化理解",
            input_payload=input_payload,
            prompt=prompt,
            session_id=f"click_voice_understand_{voice_record.get('id')}",
            timeout_seconds=180,
        )
        return _validate_receipt_output(receipt, _validate_understand_output, transcript)

    def select_relevant_reading(
        self,
        *,
        voice_record_id: str,
        transcript: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        compact_candidates = []
        for candidate in candidates:
            compact_candidates.append(
                {
                    "book_id": candidate["book_id"],
                    "title": candidate.get("title"),
                    "author": candidate.get("author"),
                    "recent_activity_at": candidate.get("recent_activity_at"),
                    "explicitly_selected": bool(candidate.get("explicitly_selected")),
                    "evidence": [
                        {
                            "evidence_id": evidence["evidence_id"],
                            "evidence_class": evidence["evidence_class"],
                            "chapter_title": evidence.get("chapter_title"),
                            "text": str(evidence["text"])[:2000],
                            "review_state": evidence.get("review_state"),
                        }
                        for evidence in candidate.get("evidence") or []
                    ],
                }
            )
        input_payload = {
            "schema": VOICE_HERMES_RELEVANCE_INPUT_SCHEMA,
            "adapter_version": VOICE_HERMES_ADAPTER_VERSION,
            "voice_record_id": voice_record_id,
            "transcript": transcript,
            "candidates": compact_candidates,
        }
        prompt = f"""
你是 Click Voice 的阅读证据相关性判定器。最近读过不等于相关，书名相似也不等于书中证据支持。

请逐本判断候选书是否能用给出的真实 evidence 帮助讨论当前语音。只有至少一条 evidence 与语音中的具体观点、问题或验证方法直接相关时，relevant 才能为 true，score 必须不低于 0.6。只选择确实需要的 evidence_id，最多选择 5 本书。

evidence_class 含义：
- book_source：书中原句或用户在书中划出的原文，可以表述为书中证据。
- user_annotation：用户自己的阅读备注，只能表述为用户当时的想法。
- living_book_model：Click/Hermes 生成的书籍模型；若 review_state 不是 reviewed，只能作为待验证的书中逻辑草稿，不能说成原书明说。

只返回严格 JSON，schema 必须是 {VOICE_HERMES_RELEVANCE_OUTPUT_SCHEMA}。字段：
- schema
- decisions：与 candidates 一一对应，每项含 book_id/relevant/score/reason/selected_evidence_ids
- no_relevant_reason：没有相关书籍时必须说明；有相关书籍时为 null

禁止因为一本书最近读过就强行选择。没有真实相关证据时，所有 relevant=false。

输入：
{json.dumps(input_payload, ensure_ascii=False, sort_keys=True, default=str)}
""".strip()
        receipt = self._invoke(
            operation="阅读相关性判断",
            input_payload=input_payload,
            prompt=prompt,
            session_id=f"click_voice_relevance_{voice_record_id}",
            timeout_seconds=180,
        )
        return _validate_receipt_output(receipt, _validate_relevance_output, candidates)

    def discuss(
        self,
        *,
        discussion_id: str,
        voice_record: dict[str, Any],
        transcript: str,
        user_message: str,
        history: list[dict[str, Any]],
        context_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        context_evidence = context_snapshot.get("evidence_items") if isinstance(context_snapshot.get("evidence_items"), list) else []
        input_payload = {
            "schema": VOICE_HERMES_DISCUSS_INPUT_SCHEMA,
            "adapter_version": VOICE_HERMES_ADAPTER_VERSION,
            "discussion_id": discussion_id,
            "voice_record": {
                "id": voice_record.get("id"),
                "title": voice_record.get("title"),
                "summary": voice_record.get("summary"),
            },
            "transcript": transcript,
            "user_message": user_message,
            "history": history[-30:],
            "context_snapshot": {
                "id": context_snapshot.get("id"),
                "scope": context_snapshot.get("scope"),
                "no_relevant_reason": (context_snapshot.get("metadata") or {}).get("no_relevant_reason"),
                "evidence": [
                    {
                        "citation_id": item.get("evidence_id"),
                        "evidence_class": item.get("evidence_class"),
                        "book_title": item.get("book_title"),
                        "chapter_title": item.get("chapter_title"),
                        "review_state": item.get("review_state"),
                        "text": str(item.get("text") or "")[:3000],
                    }
                    for item in context_evidence
                ],
            },
        }
        prompt = f"""
你是 Hermes，正在 Click Voice 中围绕一条用户灵感进行严谨讨论。你只能使用输入中的录音、历史消息和 context_snapshot；禁止假装读过未提供的书，禁止把用户批注说成书中原话，禁止把待审阅 Living Book 草稿说成原书事实。

只返回严格 JSON，schema 必须是 {VOICE_HERMES_DISCUSS_OUTPUT_SCHEMA}，字段：
- schema
- user_idea：准确复述用户当前要讨论的原始想法，不改进观点
- book_evidence：数组，每项 statement/citation_ids；只能引用 evidence_class=book_source
- book_logic_inference：数组，每项 statement/citation_ids；只能引用 book_source 或 living_book_model，并明确这是推演
- hermes_synthesis：Hermes 自己的综合判断，不能冒充书中结论
- uncertainty：不确定性和证据缺口
- counterexamples：至少一个反例、边界条件或会让判断失败的情况
- next_validation：至少一个可以实际验证的下一步
- no_relevant_reading：上下文没有任何相关阅读证据时为 true，否则为 false

每个 citation_id 必须来自输入。user_annotation 只能帮助理解用户过去怎么想，不能进入 book_evidence 或 book_logic_inference。若 no_relevant_reason 存在，明确承认没有相关书籍，不得强引。

输入：
{json.dumps(input_payload, ensure_ascii=False, sort_keys=True, default=str)}
""".strip()
        receipt = self._invoke(
            operation="灵感讨论",
            input_payload=input_payload,
            prompt=prompt,
            session_id=f"click_voice_discussion_{discussion_id}",
            timeout_seconds=240,
        )
        _validate_receipt_output(receipt, _validate_discussion_output, context_evidence)
        receipt["content"] = format_discussion_content(receipt["output"])
        return receipt


_DEFAULT_ADAPTER = VoiceHermesAdapter()


def get_voice_hermes_adapter() -> VoiceHermesAdapter:
    return _DEFAULT_ADAPTER
