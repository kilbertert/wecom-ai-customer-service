"""飞书多维表格查询服务 (二阶段 N2 查表 / N9 读旧行)。

2026-07-03: 从企微智能表格 (wedoc REST 48002 / MCP 无 get_records) 改为
**飞书多维表格** (records/search + contains 服务端过滤)。见 :mod:`feishu_bitable`。

查询委托 :mod:`feishu_bitable` (同步实现), 本服务用 asyncio.to_thread 包装供
FastAPI async 路由调用。
"""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.feishu_bitable import (
    get_record as _feishu_get_record,
    record_to_summary as _feishu_record_to_summary,
    search_records as _feishu_search,
)

logger = logging.getLogger(__name__)

# Same-module candidates start at 100 points. Operation similarity contributes
# only 40 points, so a module-name match alone cannot become a duplicate.
EXISTING_ISSUE_SCORE_THRESHOLD = 125.0
EXACT_OPERATION_MATCH_BONUS = 50.0
EXPLICIT_CONFIRMATION_SIMILARITY_THRESHOLD = 0.72
EXPLICIT_CONFIRMATION_MIN_COMMON_CHARS = 6


def _normalize_for_match(value: str) -> str:
    """保留中英文数字并统一小写，供飞书候选本地排序。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())


def _record_id(record: Dict[str, Any]) -> str:
    return str(record.get("record_id") or record.get("id") or "")


def _operation_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_for_match(left)
    normalized_right = _normalize_for_match(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _operation_common_chars(left: str, right: str) -> int:
    normalized_left = _normalize_for_match(left)
    normalized_right = _normalize_for_match(right)
    if not normalized_left or not normalized_right:
        return 0
    match = SequenceMatcher(None, normalized_left, normalized_right).find_longest_match()
    return int(match.size)


def _feedback_search_terms(keyword: str, op_desc: str) -> list[str]:
    """Build a small set of exact Feishu probes from one raw description."""

    terms: list[str] = []

    def add(value: str) -> None:
        candidate = (value or "").strip()
        if len(_normalize_for_match(candidate)) < 4 or candidate in terms:
            return
        terms.append(candidate)

    add(keyword)
    add(op_desc)
    for chunk in re.findall(r"[\u4e00-\u9fff]{4,}", op_desc or keyword or ""):
        if len(chunk) <= 20:
            add(chunk)
        for marker in ("失败", "异常", "报错", "丢失", "无响应", "打不开", "不生效", "无法"):
            index = chunk.find(marker)
            if index >= 0:
                add(chunk[max(0, index - 6) : index + len(marker)])
                add(chunk[max(0, index - 4) : index])
        if len(chunk) > 8:
            add(chunk[-8:])
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", op_desc or keyword or ""):
        add(token)
    return terms[:8]


def _feedback_score(
    record: Dict[str, Any], *, keyword: str, module: str, op_desc: str
) -> float:
    """对同模块候选做确定性排序；只决定先展示哪条，不自动判定重复。"""
    summary = _feishu_record_to_summary(record)
    record_module = _normalize_for_match(summary.get("module", ""))
    record_op = _normalize_for_match(summary.get("op_desc", ""))
    query_module = _normalize_for_match(module)
    query_op = _normalize_for_match(op_desc)
    query_keyword = _normalize_for_match(keyword)

    # Module labels commonly repeat at the start of the operation description.
    # Remove one occurrence before scoring, otherwise a query such as "白名单"
    # matches every issue in the 设备白名单 module regardless of the actual fault.
    if record_module and record_module in record_op:
        record_op = record_op.replace(record_module, "", 1)
    if query_module and query_module in query_op:
        query_op = query_op.replace(query_module, "", 1)

    score = 0.0
    if query_module and record_module:
        if query_module == record_module:
            score += 100.0
        elif query_module in record_module or record_module in query_module:
            score += 60.0
    if query_keyword and (
        query_keyword in record_op or record_op in query_keyword
    ):
        score += 45.0
    if query_op and record_op:
        score += SequenceMatcher(None, query_op, record_op).ratio() * 40.0
        # Raw H5/WeCom M2 messages do not have an extracted module yet.  An exact
        # normalized operation description is still strong deterministic evidence
        # of an existing issue and must be able to cross the candidate threshold.
        # The minimum length prevents generic phrases such as "保存失败" from
        # receiving the bonus.
        if len(query_op) >= 12 and query_op == record_op:
            score += EXACT_OPERATION_MATCH_BONUS
    return score


class SmartSheetQueryService:
    """飞书多维表格查询服务 (N2/N9)。

    保留类名 SmartSheetQueryService 以最小化改动 (路由/依赖注入不变)。
    底层走飞书 feishu_bitable。

    Args:
        wechat_service: 兼容保留 (飞书用 app_id/secret, 不用 wechat, 但
            保留参数避免改调用方)。
    """

    def __init__(self, wechat_service: Any = None) -> None:
        self._wechat = wechat_service  # 兼容保留, 飞书不用

    @staticmethod
    def feedback_score(
        record: Dict[str, Any], *, keyword: str, module: str, op_desc: str
    ) -> float:
        """Expose the deterministic candidate score to the internal API.

        The score is advisory: callers must still require a minimum score before
        presenting a row as an existing issue.  Returning it lets Dify distinguish
        a true duplicate from a same-module candidate.
        """
        return _feedback_score(record, keyword=keyword, module=module, op_desc=op_desc)

    @staticmethod
    def operation_similarity(record: Dict[str, Any], *, op_desc: str) -> float:
        summary = _feishu_record_to_summary(record)
        return _operation_similarity(op_desc, summary.get("op_desc", ""))

    @staticmethod
    def operation_common_chars(record: Dict[str, Any], *, op_desc: str) -> int:
        summary = _feishu_record_to_summary(record)
        return _operation_common_chars(op_desc, summary.get("op_desc", ""))

    async def search_by_keyword(
        self, keyword: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """N2 查表: 关键词在主表"操作描述"字段 contains 匹配 (飞书服务端过滤)。"""
        return await asyncio.to_thread(_feishu_search, keyword, "操作描述", limit)

    async def search_by_feedback(
        self,
        keyword: str,
        module: str = "",
        op_desc: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按关键词+模块多字段召回，再以完整问题描述做相似度排序。

        旧实现只执行 ``操作描述 CONTAINS 完整关键词``。当模型给出“设备白名单”，
        旧行描述只有“白名单”但模块字段正是“设备白名单”时会漏查并重复新增。
        这里保留旧关键词查询，同时增加模块字段召回；返回结果仍只用于让用户确认
        “是否同一问题”，不会自动合并或修改飞书记录。
        """
        keyword = (keyword or "").strip()
        module = (module or "").strip()
        op_desc = (op_desc or "").strip()
        fetch_limit = max(min(limit * 4, 100), 20)

        probes: list[tuple[str, str]] = []
        operation_terms = _feedback_search_terms(keyword, op_desc)
        for term, field in (
            *((term, "操作描述") for term in operation_terms),
            (keyword, "模块/功能点"),
            (module, "模块/功能点"),
            (module, "操作描述"),
        ):
            probe = ((term or "").strip(), field)
            if probe[0] and probe not in probes:
                probes.append(probe)
        if not probes:
            return []

        batches = await asyncio.gather(
            *(
                asyncio.to_thread(_feishu_search, term, field, fetch_limit)
                for term, field in probes
            )
        )
        deduped: Dict[str, Dict[str, Any]] = {}
        anonymous_index = 0
        for records in batches:
            for record in records:
                rid = _record_id(record)
                if not rid:
                    anonymous_index += 1
                    rid = f"__anonymous_{anonymous_index}"
                deduped.setdefault(rid, record)

        ranked = sorted(
            deduped.values(),
            key=lambda record: _feedback_score(
                record, keyword=keyword, module=module, op_desc=op_desc
            ),
            reverse=True,
        )
        return ranked[:limit]

    async def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """N9 读旧行: 按 record_id 精确取单条。"""
        return await asyncio.to_thread(_feishu_get_record, record_id)

    @staticmethod
    def record_to_summary(record: Dict[str, Any]) -> Dict[str, str]:
        """记录归一成 {record_id, module, op_desc, summary}。"""
        return _feishu_record_to_summary(record)


__all__ = ["SmartSheetQueryService"]
