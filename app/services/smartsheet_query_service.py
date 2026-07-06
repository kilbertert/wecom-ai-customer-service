"""飞书多维表格查询服务 (二阶段 N2 查表 / N9 读旧行)。

2026-07-03: 从企微智能表格 (wedoc REST 48002 / MCP 无 get_records) 改为
**飞书多维表格** (records/search + contains 服务端过滤)。见 :mod:`feishu_bitable`。

查询委托 :mod:`feishu_bitable` (同步实现), 本服务用 asyncio.to_thread 包装供
FastAPI async 路由调用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.services.feishu_bitable import (
    FeishuBitableError,
    get_record as _feishu_get_record,
    record_to_summary as _feishu_record_to_summary,
    search_records as _feishu_search,
)

logger = logging.getLogger(__name__)


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

    async def search_by_keyword(
        self, keyword: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """N2 查表: 关键词在主表"操作描述"字段 contains 匹配 (飞书服务端过滤)。"""
        return await asyncio.to_thread(_feishu_search, keyword, "操作描述", limit)

    async def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """N9 读旧行: 按 record_id 精确取单条。"""
        return await asyncio.to_thread(_feishu_get_record, record_id)

    @staticmethod
    def record_to_summary(record: Dict[str, Any]) -> Dict[str, str]:
        """记录归一成 {record_id, module, op_desc, summary}。"""
        return _feishu_record_to_summary(record)


__all__ = ["SmartSheetQueryService"]
