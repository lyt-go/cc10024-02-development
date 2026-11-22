"""字段归一化领域服务：将清洗后数据做标准化映射，消费clean_pipeline和transform_rule输出。

标准字典类规则按来源站点隔离：同一原始值在不同站点可以命中不同字典，
规则携带字典版本，供原始地区码来源归一化（region_normalize）形成可复用的字典命中明细。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService
from app import persistence as persist


class FieldNormalizeService(BaseModuleService):
    MODULE = "field_normalize"
    ENTITY = "归一规则"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["规则编号", "关联字段", "归一类型", "来源站点", "标准字典", "映射表",
                   "未知策略", "区间边界", "规则版本", "字典版本"]
    REQUIRED_FIELDS = ["规则编号", "关联字段", "归一类型"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    @staticmethod
    def parse_mapping(rule: dict[str, Any]) -> dict[str, str]:
        """解析规则上的映射表，兼容字典对象和「原码:标准值;...」文本两种写法。"""
        raw = rule.get("映射表", "")
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        mapping: dict[str, str] = {}
        for pair in str(raw).replace("\n", ";").split(";"):
            pair = pair.strip()
            if not pair:
                continue
            for sep in (":", "：", "=", "->", "→"):
                if sep in pair:
                    key, value = pair.split(sep, 1)
                    mapping[key.strip()] = value.strip()
                    break
        return mapping

    def active_dictionary_rules(self, field: str, source: str | None = None) -> list[dict[str, Any]]:
        """返回某字段当前生效的标准字典规则，可按来源站点进一步收窄。"""
        rules = [
            r for r in store.rows(self.MODULE)
            if r.get("status") == "已生效"
            and r.get("归一类型") == "标准字典"
            and r.get("关联字段") == field
        ]
        if source is not None:
            rules = [r for r in rules if r.get("来源站点") == source]
        return rules

    def normalize_value(self, field: str, value: str, source: str | None = None) -> tuple[str, bool]:
        """对单个字段值执行归一化。返回归一化后的值和是否成功。

        标准字典规则按来源站点隔离；同一来源存在多条有效字典且解释不一致时，
        调用方无法自行裁决，返回失败交由来源归一化流程进入待回查。
        """
        rules = [
            r for r in store.rows(self.MODULE)
            if r.get("关联字段") == field and r.get("status") == "已生效"
        ]
        if not rules:
            return value, True
        rule = rules[0]
        norm_type = rule.get("归一类型", "")
        if norm_type == "lowercase":
            return value.lower(), True
        elif norm_type == "trim":
            return value.strip(), True
        elif norm_type == "标准字典":
            candidates = self.active_dictionary_rules(field, source)
            if source is None and not candidates:
                # 未指定来源时回退到旧行为，保持历史调用兼容
                candidates = [r for r in rules if r.get("归一类型") == "标准字典"]
            hits = [r for r in candidates if value in self.parse_mapping(r)]
            if len(hits) == 1:
                return self.parse_mapping(hits[0])[value], True
            if len(hits) > 1:
                return value, False
            unknown = rule.get("未知策略", "keep")
            return value if unknown == "keep" else "", unknown == "keep"
        return value, True

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 关联的清洗管道
        pipelines = store.rows("clean_pipeline")
        base["upstream_pipelines"] = sum(1 for p in pipelines if p.get("status") == "已启用")
        # 关联的质量防护
        guards = store.rows("quality_guard")
        base["downstream_quality_rules"] = sum(1 for g in guards if g.get("status") == "已生效")
        # 按来源站点隔离的生效字典数量，支撑来源归一化的命中裁决
        base["active_dictionaries"] = len(self.active_dictionary_rules("region_code"))
        return base
