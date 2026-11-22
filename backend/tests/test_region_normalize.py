"""原始地区码来源归一化集成测试。

场景：甲站字典把 07 解释成华东、乙站解释成华南；旧采集只留了关联脚本，
旧运行回执记着甲站、脚本编号和与原码对应的采集标识，脚本现在也被乙站复用。

每个用例通过 tests/run_region_tests.py 在独立子进程运行，保证内存仓库与
JSONL 产物互不串扰；用例本身也全部自包含。
"""
from __future__ import annotations

import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist
from app.store import store
from app.services.field_normalize import FieldNormalizeService
from app.services.region_normalize import RegionNormalizeService
from app.services.data_extract import DataExtractService

SCRIPT = "BEHA-SHARED-07"   # 甲乙两站复用的同一脚本
FIELD = "region_code"

# 进程级隔离：清掉种子数据中预置的地区字典，避免与用例自建字典重复判冲突
store.rows("field_normalize")[:] = [
    r for r in store.rows("field_normalize") if r.get("关联字段") != FIELD
]

# 清掉来源归一化产物
for _sub, _name in [
    ("reports", "region_run_receipt.jsonl"),
    ("reports", "region_dict_hits.jsonl"),
    ("reports", "region_pending_review.jsonl"),
    ("exports", "region_delivered_rows.jsonl"),
]:
    _p = persist.data_path(_sub, _name)
    if os.path.exists(_p):
        os.remove(_p)


def _setup_rules() -> tuple[int, int]:
    """登记甲站(07→华东)和乙站(07→华南)两本生效字典，返回两条规则id。"""
    svc = FieldNormalizeService()
    jia, missing = svc.create_entry({
        "规则编号": "TEST-DICT-JIA",
        "关联字段": FIELD,
        "归一类型": "标准字典",
        "来源站点": "甲站",
        "标准字典": "甲站地区字典",
        "映射表": {"07": "华东"},
        "规则版本": "v1",
        "字典版本": "JIA-DICT-v1",
    })
    assert not missing
    yi, missing = svc.create_entry({
        "规则编号": "TEST-DICT-YI",
        "关联字段": FIELD,
        "归一类型": "标准字典",
        "来源站点": "乙站",
        "标准字典": "乙站地区字典",
        "映射表": {"07": "华南"},
        "规则版本": "v1",
        "字典版本": "YI-DICT-v1",
    })
    assert not missing
    svc.run_action(jia["id"], "标记生效")
    svc.run_action(yi["id"], "标记生效")
    return jia["id"], yi["id"]


def _three_records() -> list[dict]:
    """题设三条 07：明确乙站、凭回执回溯甲站、完全无归属。"""
    return [
        {"采集标识": "COL-YI-07", "脚本编号": SCRIPT, "原始地区码": "07", "来源站点": "乙站"},
        {"采集标识": "COL-JIA-07", "脚本编号": SCRIPT, "原始地区码": "07"},
        {"采集标识": "COL-NONE-07", "脚本编号": SCRIPT, "原始地区码": "07"},
    ]


def test_three_records_settlement():
    """三条07：明确乙站、凭回执回溯甲站、完全无归属，核对进入本次结算的范围。"""
    _setup_rules()
    region = RegionNormalizeService()

    # 旧运行回执：甲站 + 复用脚本 + 与原码对应的采集标识
    region.register_run_receipt(
        source="甲站", script_code=SCRIPT, capture_id="COL-JIA-07",
        raw_code="07", record_no="RUN-OLD-JIA-001",
    )

    summary = region.settle(_three_records())

    # 前两条进入本次结算，第三条留在待回查
    assert summary["本次进入结算"] == 2
    assert summary["本次进入待回查"] == 1
    assert summary["累计字典命中明细"] == 2
    assert summary["累计待回查"] == 1

    hits = {h["采集标识"]: h for h in region.dict_hits()}
    # 乙站记录只用乙站映射，绝不跨来源取甲站字典
    assert hits["COL-YI-07"]["归一结果"] == "华南"
    assert hits["COL-YI-07"]["来源站点"] == "乙站"
    assert hits["COL-YI-07"]["字典版本"] == "YI-DICT-v1"
    assert hits["COL-YI-07"]["来源依据"] == "记录自带站点"
    # 甲站旧记录凭回执回溯到甲站映射（脚本被乙站复用也不影响）
    assert hits["COL-JIA-07"]["归一结果"] == "华东"
    assert hits["COL-JIA-07"]["来源依据"] == "旧运行回执"
    assert hits["COL-JIA-07"]["字典版本"] == "JIA-DICT-v1"

    # 无归属原码进入该脚本的补采明细（待回查）
    pending = region.pending_review(SCRIPT)
    assert [p["采集标识"] for p in pending] == ["COL-NONE-07"]
    assert pending[0]["待回查原因"] == "来源缺失且无唯一可回溯证据"


def test_direct_source_never_crosses_dictionary():
    """带站点的记录只用本来源映射：即使别站字典存在同原码也不命中。"""
    _setup_rules()
    region = RegionNormalizeService()
    result = region.normalize_record({
        "采集标识": "COL-YI-ONLY", "脚本编号": SCRIPT,
        "原始地区码": "07", "来源站点": "乙站",
    })
    assert result["处置"] == "已结算"
    assert result["归一结果"] == "华南"
    assert result["规则编号"] == "TEST-DICT-YI"
    assert result["字典版本"] == "YI-DICT-v1"


def test_structured_rows_and_pending_supplement():
    """结构化行按命中明细生成；补录来源后待回查行进入结算且只计一次。"""
    _setup_rules()
    region = RegionNormalizeService()
    extract = DataExtractService()
    region.register_run_receipt(
        source="甲站", script_code=SCRIPT, capture_id="COL-JIA-07",
        raw_code="07", record_no="RUN-OLD-JIA-001",
    )
    region.settle(_three_records())

    rows = region.build_structured_rows()
    # 命中明细两条 → 结构化行两条，且带实际字典版本
    assert len(rows) == 2
    assert {r["字典版本"] for r in rows} == {"JIA-DICT-v1", "YI-DICT-v1"}

    # 数据抽取视角：已交付行与该脚本的补采明细
    view = extract.extract_region_rows(SCRIPT)
    assert view["结构化行数"] == 2
    assert view["待回查数"] == 1
    assert view["补采明细"][0]["采集标识"] == "COL-NONE-07"

    # 为待回查原码补录乙站来源
    region.supplement_source("COL-NONE-07", "乙站")
    assert region.pending_review(SCRIPT) == []
    hits = {h["采集标识"]: h for h in region.dict_hits()}
    assert "COL-NONE-07" in hits
    assert hits["COL-NONE-07"]["归一结果"] == "华南"
    assert hits["COL-NONE-07"]["来源依据"] == "记录自带站点"

    # 补录后重新生成结构化行：三条且无重复（同一采集标识只计一次）
    rows = region.build_structured_rows()
    capture_ids = [r["采集标识"] for r in rows]
    assert len(rows) == 3
    assert len(set(capture_ids)) == 3

    # 幂等：整批重放结算不产生重复行
    region.settle(_three_records()[:2] + [{
        "采集标识": "COL-NONE-07", "脚本编号": SCRIPT,
        "原始地区码": "07", "来源站点": "乙站",
    }])
    assert len(region.dict_hits()) == 3
    assert len(region.build_structured_rows()) == 3


def test_disabled_dictionary_replay_keeps_delivered_rows():
    """停用字典后仍能按原命中结果（含版本）重新生成已交付的行。"""
    _setup_rules()
    svc = FieldNormalizeService()
    region = RegionNormalizeService()
    region.register_run_receipt(
        source="甲站", script_code=SCRIPT, capture_id="COL-JIA-07",
        raw_code="07", record_no="RUN-OLD-JIA-001",
    )
    region.settle(_three_records())
    region.supplement_source("COL-NONE-07", "乙站")
    before = region.build_structured_rows()
    assert len(before) == 3
    versions_before = {r["采集标识"]: r["字典版本"] for r in before}

    # 停用乙站字典
    yi_rule = next(r for r in store.rows("field_normalize") if r.get("规则编号") == "TEST-DICT-YI")
    svc.run_action(yi_rule["id"], "停用规则")

    # 停用后没有乙站生效字典，新乙站原码无法结算，进入待回查
    result = region.normalize_record({
        "采集标识": "COL-YI-NEW", "脚本编号": SCRIPT,
        "原始地区码": "07", "来源站点": "乙站",
    })
    assert result["处置"] == "待回查"
    assert "未收录" in result["待回查原因"]

    # 但已交付行可凭命中明细原样重放：行数、版本都不变
    replay = region.replay_delivered_rows()
    assert replay["重放行数"] == 3
    assert replay["重放前后行数一致"] is True
    assert replay["版本保持一致"] is True
    versions_after = {r["采集标识"]: r["字典版本"] for r in region.delivered_rows()}
    assert versions_after == versions_before


def test_replace_dictionary_replay_then_new_settlement():
    """替换甲站字典后：已交付行重放保持旧版本，新结算使用新版本。"""
    _setup_rules()
    svc = FieldNormalizeService()
    region = RegionNormalizeService()
    region.register_run_receipt(
        source="甲站", script_code=SCRIPT, capture_id="COL-JIA-07",
        raw_code="07", record_no="RUN-OLD-JIA-001",
    )
    region.settle(_three_records())
    region.supplement_source("COL-NONE-07", "乙站")
    region.build_structured_rows()

    # 用新版本替换甲站字典（旧规则停用，新规则生效，07→华东新区）
    jia_rule = next(r for r in store.rows("field_normalize") if r.get("规则编号") == "TEST-DICT-JIA")
    svc.run_action(jia_rule["id"], "停用规则")
    new_jia, missing = svc.create_entry({
        "规则编号": "TEST-DICT-JIA-V2",
        "关联字段": FIELD,
        "归一类型": "标准字典",
        "来源站点": "甲站",
        "标准字典": "甲站地区字典",
        "映射表": {"07": "华东新区"},
        "规则版本": "v2",
        "字典版本": "JIA-DICT-v2",
    })
    assert not missing
    svc.run_action(new_jia["id"], "标记生效")

    # 重放已交付行：甲站旧记录仍保持 v1 版本和原行数
    replay = region.replay_delivered_rows()
    assert replay["版本保持一致"] is True
    assert replay["重放行数"] == 3
    old_jia_row = next(r for r in region.delivered_rows() if r["采集标识"] == "COL-JIA-07")
    assert old_jia_row["字典版本"] == "JIA-DICT-v1"
    assert old_jia_row["归一结果"] == "华东"

    # 新的甲站采集结算使用替换后的新版本
    region.settle([{
        "采集标识": "COL-JIA-NEW", "脚本编号": SCRIPT,
        "原始地区码": "07", "来源站点": "甲站",
    }])
    new_hit = next(h for h in region.dict_hits() if h["采集标识"] == "COL-JIA-NEW")
    assert new_hit["字典版本"] == "JIA-DICT-v2"
    assert new_hit["归一结果"] == "华东新区"
    rows = region.build_structured_rows()
    assert len(rows) == 4  # 原3行 + 新1行，旧行版本未被覆盖


def test_same_source_conflict_goes_pending():
    """同一来源存在两个有效字典且解释冲突时，不擅自选择，进入待回查。"""
    _setup_rules()
    svc = FieldNormalizeService()
    region = RegionNormalizeService()

    extra, missing = svc.create_entry({
        "规则编号": "TEST-DICT-JIA-CONFLICT",
        "关联字段": FIELD,
        "归一类型": "标准字典",
        "来源站点": "甲站",
        "标准字典": "甲站地区字典-冲突副本",
        "映射表": {"07": "华西"},
        "规则版本": "v1",
        "字典版本": "JIA-DICT-CONFLICT-v1",
    })
    assert not missing
    svc.run_action(extra["id"], "标记生效")

    result = region.normalize_record({
        "采集标识": "COL-JIA-CONFLICT", "脚本编号": SCRIPT,
        "原始地区码": "07", "来源站点": "甲站",
    })
    assert result["处置"] == "待回查"
    assert result["待回查原因"] == "同来源存在多个有效字典解释冲突"


def test_text_mapping_format_supported():
    """映射表兼容「原码:标准值;...」文本格式。"""
    rule = {"映射表": "07:华东;08：华北;09=华南"}
    parsed = FieldNormalizeService.parse_mapping(rule)
    assert parsed == {"07": "华东", "08": "华北", "09": "华南"}


def test_evidence_must_match_script():
    """回执证据必须能对应到那次采集（采集标识+脚本）；脚本不符则不能回溯。"""
    _setup_rules()
    region = RegionNormalizeService()
    region.register_run_receipt(
        source="甲站", script_code=SCRIPT, capture_id="COL-JIA-07",
        raw_code="07", record_no="RUN-OLD-JIA-001",
    )
    result = region.normalize_record({
        "采集标识": "COL-JIA-07",       # 回执存在
        "脚本编号": "BEHA-OTHER-99",    # 但脚本编号对不上
        "原始地区码": "07",
    })
    assert result["处置"] == "待回查"
    assert result["待回查原因"] == "来源缺失且无唯一可回溯证据"
