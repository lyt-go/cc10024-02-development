"""原始地区码来源归一化领域服务。

解决的问题：不同站点对同一原始地区码（如 07）有各自的标准字典解释，旧采集只
关联了行为脚本、没有站点，归一化时同一原码可能同时命中两个有效字典产生解释冲突。

裁决规则（与业务约定一一对应）：
1. 直接带站点的记录，只允许使用本来源站点的字典，绝不跨来源选字典。
2. 来源不全的记录，只允许使用能回溯到那次采集的证据：旧运行回执上的
   （站点、脚本编号、与原码对应的采集标识）。证据不能指向单一来源的原码，
   保留在待回查范围，不进入本次结算。
3. 归一结果固定记录实际用过的字典版本；补录来源后同一采集标识只计一次。
4. 数据抽取按字典命中明细写结构化行；待回查原码进入该脚本的补采明细。
5. 字典停用或替换后，仍可凭已落盘的命中明细（含字典版本）重新生成已交付的行。

产物均通过 persistence 落到 data/reports/ 下的 JSONL：
- region_run_receipt.jsonl       旧运行回执（站点/脚本/采集标识证据）
- region_dict_hits.jsonl         字典命中明细（含字典版本，可复用）
- region_pending_review.jsonl    待回查原码（按脚本补采明细）
- region_delivered_rows.jsonl    已交付结构化行
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.field_normalize import FieldNormalizeService
from app import persistence as persist

REGION_FIELD = "region_code"

RECEIPT_FILE = "region_run_receipt.jsonl"
HIT_FILE = "region_dict_hits.jsonl"
PENDING_FILE = "region_pending_review.jsonl"
DELIVERED_FILE = "region_delivered_rows.jsonl"


class RegionNormalizeService(BaseModuleService):
    """原始地区码来源归一化与结算服务。"""

    MODULE = "field_normalize"
    ENTITY = "来源归一化"
    OPERATION_TERM = "来源归一化"

    def __init__(self) -> None:
        self.dictionary_service = FieldNormalizeService()

    # ------------------------------------------------------------------ 证据登记
    def register_run_receipt(
        self,
        *,
        source: str,
        script_code: str,
        capture_id: str,
        raw_code: str,
        record_no: str,
    ) -> dict[str, Any]:
        """登记旧运行回执：记录站点、脚本编号和与原码对应的采集标识，作为来源证据。"""
        receipt = {
            "记录编号": record_no,
            "来源站点": source,
            "脚本编号": script_code,
            "采集标识": capture_id,
            "原始地区码": raw_code,
            "证据类型": "旧运行回执",
            "登记时间": persist.now_iso(),
        }
        persist.upsert_jsonl("reports", RECEIPT_FILE, receipt, key="采集标识")
        return receipt

    def run_receipts(self) -> list[dict[str, Any]]:
        return persist.read_jsonl("reports", RECEIPT_FILE)

    # ------------------------------------------------------------------ 来源裁决
    def _resolve_source(
        self,
        record: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        """裁决一条采集记录的来源站点，返回(来源站点, 裁决依据)。

        只接受两种来源：记录自带站点，或能凭采集标识+脚本回溯到唯一站点的旧回执。
        无法指向单一来源时返回 (None, None)，由调用方放入待回查。
        """
        source = (record.get("来源站点") or "").strip()
        if source:
            return source, "记录自带站点"

        capture_id = (record.get("采集标识") or "").strip()
        script_code = (record.get("脚本编号") or "").strip()
        if capture_id:
            evidence = [
                r for r in self.run_receipts()
                if r.get("采集标识") == capture_id
                and (not script_code or r.get("脚本编号") == script_code)
            ]
            sites = {r.get("来源站点") for r in evidence if r.get("来源站点")}
            if len(sites) == 1:
                return sites.pop(), "旧运行回执"
        return None, None

    # ------------------------------------------------------------------ 单条归一
    def normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """对一条采集记录做来源归一化，返回字典命中明细或待回查条目。"""
        raw_code = str(record.get("原始地区码", "")).strip()
        capture_id = str(record.get("采集标识", "")).strip()
        script_code = str(record.get("脚本编号", "")).strip()

        source, evidence = self._resolve_source(record)
        if source is None:
            return {
                "采集标识": capture_id,
                "脚本编号": script_code,
                "原始地区码": raw_code,
                "来源站点": "",
                "待回查原因": "来源缺失且无唯一可回溯证据",
                "处置": "待回查",
                "更新时间": persist.now_iso(),
            }

        rules = self.dictionary_service.active_dictionary_rules(REGION_FIELD, source)
        hit_rules = [
            rule for rule in rules
            if raw_code in self.dictionary_service.parse_mapping(rule)
        ]
        if len(hit_rules) != 1:
            reason = "同来源存在多个有效字典解释冲突" if len(hit_rules) > 1 else "本来源字典未收录该原码"
            return {
                "采集标识": capture_id,
                "脚本编号": script_code,
                "原始地区码": raw_code,
                "来源站点": source,
                "来源依据": evidence,
                "待回查原因": reason,
                "处置": "待回查",
                "更新时间": persist.now_iso(),
            }

        rule = hit_rules[0]
        return {
            "采集标识": capture_id,
            "脚本编号": script_code,
            "原始地区码": raw_code,
            "来源站点": source,
            "来源依据": evidence,
            "规则编号": rule.get("规则编号", ""),
            "标准字典": rule.get("标准字典", ""),
            "字典版本": rule.get("字典版本") or rule.get("规则版本") or rule.get("version", ""),
            "归一结果": self.dictionary_service.parse_mapping(rule)[raw_code],
            "处置": "已结算",
            "更新时间": persist.now_iso(),
        }

    # ------------------------------------------------------------------ 批量结算
    def settle(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """对一批采集记录执行来源归一化结算。

        已结算的进入字典命中明细，无法裁决的进入待回查；均按采集标识幂等，
        补录来源后重跑同一记录只更新一次，不会重复计次。
        """
        existing_hits = persist.read_jsonl("reports", HIT_FILE)
        existing_pending = persist.read_jsonl("reports", PENDING_FILE)
        hits_by_id = {h["采集标识"]: h for h in existing_hits}
        pending_by_id = {p["采集标识"]: p for p in existing_pending}

        settled_now = 0
        pending_now = 0
        for record in records:
            result = self.normalize_record(record)
            capture_id = result["采集标识"]
            if result["处置"] == "已结算":
                hits_by_id[capture_id] = result
                pending_by_id.pop(capture_id, None)  # 补录来源后从待回查移除
                settled_now += 1
            else:
                pending_by_id[capture_id] = result
                hits_by_id.pop(capture_id, None)
                pending_now += 1

        hits = sorted(hits_by_id.values(), key=lambda x: x["采集标识"])
        pending = sorted(pending_by_id.values(), key=lambda x: (x["脚本编号"], x["采集标识"]))
        persist.write_jsonl("reports", HIT_FILE, hits)
        persist.write_jsonl("reports", PENDING_FILE, pending)
        return {
            "本次送入": len(records),
            "本次进入结算": settled_now,
            "本次进入待回查": pending_now,
            "累计字典命中明细": len(hits),
            "累计待回查": len(pending),
        }

    def dict_hits(self) -> list[dict[str, Any]]:
        return persist.read_jsonl("reports", HIT_FILE)

    def pending_review(self, script_code: str | None = None) -> list[dict[str, Any]]:
        rows = persist.read_jsonl("reports", PENDING_FILE)
        if script_code:
            rows = [r for r in rows if r.get("脚本编号") == script_code]
        return rows

    def supplement_source(self, capture_id: str, source: str) -> dict[str, Any]:
        """为待回查原码补录来源站点并重新裁决；同一采集标识仍只计一次。"""
        pending = persist.read_jsonl("reports", PENDING_FILE)
        target = next((r for r in pending if r.get("采集标识") == capture_id), None)
        if target is None:
            raise ValueError(f"采集标识 {capture_id} 不在待回查范围")
        return self.settle([{
            "采集标识": capture_id,
            "脚本编号": target.get("脚本编号", ""),
            "原始地区码": target.get("原始地区码", ""),
            "来源站点": source,
        }])

    # ------------------------------------------------------------------ 结构化行
    def build_structured_rows(self) -> list[dict[str, Any]]:
        """按字典命中明细写结构化行（数据抽取消费）。

        结构化行完全由命中明细生成，携带实际使用的字典版本；按采集标识幂等覆盖，
        字典停用/替换后重放仍按原命中结果重建，行数和版本保持不变。
        """
        rows = [
            {
                "采集标识": hit["采集标识"],
                "脚本编号": hit.get("脚本编号", ""),
                "原始地区码": hit["原始地区码"],
                "来源站点": hit["来源站点"],
                "归一结果": hit["归一结果"],
                "规则编号": hit.get("规则编号", ""),
                "字典版本": hit.get("字典版本", ""),
                "生成时间": persist.now_iso(),
            }
            for hit in self.dict_hits()
        ]
        persist.write_jsonl("exports", DELIVERED_FILE, rows)
        return rows

    def delivered_rows(self) -> list[dict[str, Any]]:
        return persist.read_jsonl("exports", DELIVERED_FILE)

    def replay_delivered_rows(self) -> dict[str, Any]:
        """停用/替换字典后重放已交付行：只依据已落盘的命中明细重建。"""
        before = self.delivered_rows()
        before_versions = {r["采集标识"]: r["字典版本"] for r in before}
        rebuilt = self.build_structured_rows()
        after_versions = {r["采集标识"]: r["字典版本"] for r in rebuilt}
        return {
            "重放行数": len(rebuilt),
            "版本保持一致": before_versions == after_versions,
            "重放前后行数一致": len(before) == len(rebuilt),
        }
