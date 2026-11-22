"""数据抽取领域服务：消费behavior_script脚本和element_selector锚点，执行字段抽取。

结构化行按原始地区码来源归一化的字典命中明细生成；待回查的原码不产出结构化行，
而是留在对应脚本的补采明细中，等待补录来源后再结算。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService, register_hook
from app.services.region_normalize import RegionNormalizeService
from app import persistence as persist


class DataExtractService(BaseModuleService):
    MODULE = "data_extract"
    ENTITY = "抽取模板"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["模板编号", "关联脚本", "抽取字段", "提取方式", "数据处理", "输出类型", "默认值", "模板版本"]
    REQUIRED_FIELDS = ["模板编号", "关联脚本", "抽取字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    def __init__(self) -> None:
        self.region_normalize = RegionNormalizeService()

    def extract_fields_for_script(self, script_id: str) -> list[dict[str, Any]]:
        """根据脚本编号查询所有关联的抽取模板并返回字段映射。"""
        templates = store.rows(self.MODULE)
        linked = [t for t in templates if t.get("关联脚本") == script_id and t.get("status") == "已生效"]
        return [{"field": t.get("抽取字段"), "method": t.get("提取方式"), "output_type": t.get("输出类型")} for t in linked]

    def extract_region_rows(self, script_code: str) -> dict[str, Any]:
        """按字典命中明细为指定脚本生成结构化行；待回查原码进入该脚本补采明细。"""
        all_rows = self.region_normalize.build_structured_rows()
        delivered = [row for row in all_rows if row.get("脚本编号") == script_code]
        supplement = self.region_normalize.pending_review(script_code)
        return {
            "脚本编号": script_code,
            "已交付结构化行": delivered,
            "结构化行数": len(delivered),
            "补采明细": supplement,
            "待回查数": len(supplement),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        templates = store.rows(self.MODULE)
        # 关联的元素锚点统计
        selectors = store.rows("element_selector")
        base["templates_with_selectors"] = sum(
            1 for t in templates if any(
                s.get("关联脚本") == t.get("关联脚本") for s in selectors))
        # 关联的清洗管道
        pipelines = store.rows("clean_pipeline")
        base["templates_in_pipeline"] = sum(
            1 for t in templates if any(
                p.get("输入源") and t.get("关联脚本") in str(p.get("输入源", "")) for p in pipelines))
        # 来源归一化产物：已交付结构化行与待回查补采明细
        base["region_delivered_rows"] = len(self.region_normalize.delivered_rows())
        base["region_pending_review"] = len(self.region_normalize.pending_review())
        return base
