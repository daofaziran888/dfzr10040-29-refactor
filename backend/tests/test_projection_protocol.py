"""字段投影协议与映射版本钉住测试。

覆盖：
- 共用协议：缺省处理、投影校验、不完整证据涉及行的识别；
- 映射版本：发布后新融合采用新版本列声明；
- 快照导出：断点续读沿用首次钉住的列和明细版本；
- 验收场景：value→subtotal 构建旧分区，取完一行后发布 value→amount，
  旧导出中断再续读仍取 subtotal，新融合作业采用 amount，缺值样例缺省行为保持。
"""
from __future__ import annotations

import json

import pytest

from app.fusion_engine import FusionEngine
from app.projection import (
    MISSING,
    FieldProjectionProtocol,
    ProjectionBasis,
    field_projector,
)


@pytest.fixture
def engine() -> FusionEngine:
    """每个测试获得独立引擎实例。"""
    return FusionEngine()


class TestFieldProjectionProtocol:
    """共用取字段协议：缺省处理与投影校验只有一份实现。"""

    def test_fetch_field_missing_returns_sentinel(self) -> None:
        """缺省处理：列不存在返回缺省哨兵，None 值原样保留。"""
        projector = FieldProjectionProtocol()
        assert projector.fetch_field({"a": 1}, "b") is MISSING
        assert projector.fetch_field({"a": None}, "a") is None
        assert projector.fetch_field({"a": 1.5}, "a") == 1.5

    def test_project_records_defaults_and_incomplete_rows(self) -> None:
        """投影校验：缺省列不写入结果，缺列行能被识别。"""
        basis = ProjectionBasis(
            origin="export_snapshot", columns=("a", "b"),
            version="EXPO-0001", detail={},
        )
        outcome = field_projector.project_records(
            [{"id": "r1", "a": 1, "b": 2}, {"id": "r2", "a": 3}],
            ("a", "b"), basis=basis,
        )
        assert outcome.records == [{"a": 1, "b": 2}, {"a": 3}]
        assert outcome.defaults_applied == 1
        assert outcome.incomplete_rows == [{"row": "r2", "missing_fields": ["b"]}]

    def test_rename_records_shares_default_handling(self) -> None:
        """字段映射的列选择：重命名取字段与投影共用缺省处理。"""
        basis = ProjectionBasis(
            origin="schema_map", requested=("a",), columns=("b",),
            version="SCHE-0001#v1", detail={"a": "b"},
        )
        outcome = field_projector.rename_records(
            [{"id": "r1", "a": 1, "keep": "x"}, {"id": "r2", "keep": "y"}],
            {"a": "b"}, basis=basis,
        )
        assert outcome.records[0] == {"id": "r1", "keep": "x", "b": 1}
        assert outcome.records[1] == {"id": "r2", "keep": "y"}
        assert outcome.incomplete_rows == [{"row": "r2", "missing_fields": ["a"]}]

    def test_resolve_columns_by_detail(self) -> None:
        """按明细版本解析实际字段，未映射的列保持原名。"""
        projector = FieldProjectionProtocol()
        assert projector.resolve_columns(
            ["value", "id"], {"value": "subtotal"}) == ["subtotal", "id"]

    def test_audit_view_exposes_basis(self) -> None:
        """每次取数都能核对字段投影依据。"""
        basis = ProjectionBasis(
            origin="export_snapshot", requested=("value",),
            columns=("subtotal",), version="SCHE-0001#v1",
            detail={"value": "subtotal"},
        )
        outcome = field_projector.project_records(
            [{"id": "r1", "subtotal": 9.0}], ("subtotal",), basis=basis)
        audit = outcome.audit()
        assert audit["basis"]["version"] == "SCHE-0001#v1"
        assert audit["basis"]["columns"] == ["subtotal"]
        assert audit["basis"]["requested"] == ["value"]
        assert audit["row_count"] == 1


class TestMappingVersions:
    """映射版本：发布后成为下一次融合采用的列声明。"""

    def test_versions_increment_per_source_table_pair(
            self, engine: FusionEngine) -> None:
        m1 = engine.create_schema_mapping("SRC-V", "WT_V", "{}")
        m2 = engine.create_schema_mapping("SRC-V", "WT_V", "{}")
        m3 = engine.create_schema_mapping("SRC-W", "WT_V", "{}")
        assert (m1.version, m2.version, m3.version) == (1, 2, 1)

    def test_published_version_wins_over_newer_draft(
            self, engine: FusionEngine) -> None:
        """已发布版本优先于更新的草稿；发布新版本后切换。"""
        m1 = engine.create_schema_mapping(
            "SRC-P", "WT_P", json.dumps({"value": "subtotal"}))
        engine.publish_schema_mapping(m1.mapping_id)
        m2 = engine.create_schema_mapping(
            "SRC-P", "WT_P", json.dumps({"value": "amount"}))
        active = engine._active_schema_mapping("SRC-P", "WT_P")
        assert active is not None and active.mapping_id == m1.mapping_id

        engine.publish_schema_mapping(m2.mapping_id)
        active = engine._active_schema_mapping("SRC-P", "WT_P")
        assert active is not None and active.mapping_id == m2.mapping_id

    def test_draft_mapping_applies_when_nothing_published(
            self, engine: FusionEngine) -> None:
        """既有行为保持：没有已发布版本时最新草稿生效。"""
        engine.register_source(
            source_code="SRC-D", source_name="d_source", source_type="MySQL",
            connection_string="conn://d",
        )
        engine.create_schema_mapping(
            "SRC-D", "WT_D", json.dumps({"name": "order_name"}))
        job = engine.create_fusion_job(
            target_wide_table="WT_D", participant_sources=["SRC-D"], join_key="id")
        result = engine.execute_fusion_pipeline(job.job_id)
        assert result["ok"] is True
        assert all("order_name" in rec for rec in result["records"])
        assert result["projection"]["basis"]["detail"] == {"name": "order_name"}


class TestExportProjectionBasis:
    """快照导出：按字段投影依据选择实际字段，续读沿用首次钉住的版本。"""

    def test_export_resume_keeps_pinned_mapping_version(
            self, engine: FusionEngine) -> None:
        """验收场景：旧导出两次都取 subtotal，新融合作业采用 amount。"""
        engine.register_source(
            source_code="SRC-PIN", source_name="pin_source", source_type="MySQL",
            connection_string="conn://pin",
        )
        # 旧版本列声明：value -> subtotal
        m1 = engine.create_schema_mapping(
            source_table="SRC-PIN", target_wide_table="WT_PIN",
            field_mapping_json=json.dumps({"value": "subtotal"}),
        )
        assert m1.version == 1

        # 以 v1 构建旧分区
        job_old = engine.create_fusion_job(
            target_wide_table="WT_PIN", participant_sources=["SRC-PIN"],
            join_key="id",
        )
        built = engine.execute_fusion_pipeline(job_old.job_id)
        assert built["projection"]["basis"]["version"] == f"{m1.plan_code}#v1"
        assert all("subtotal" in rec for rec in built["records"])

        # 已确认旧分区：指定两行
        job_old.merged_records = job_old.merged_records[:2]
        snap = engine.create_snapshot("WT_PIN", partition_key_value="20261120")
        snap.record_count = 2
        snap.status_mark = "可用"

        # 导出计划：每次交付最多一行，按逻辑列 value 取数
        plan = engine.create_export_plan(
            target_wide_table="WT_PIN", export_format="parquet",
            field_filter=["value"], row_limit=1,
        )

        # 首次取数：取完一行，实际字段按 v1 解析为 subtotal
        first = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert first["ok"] is True
        assert first["resumed"] is False
        assert first["complete"] is False
        assert first["exported_rows"] == 1
        assert "subtotal" in first["records"][0]
        assert "amount" not in first["records"][0]
        assert first["projection"]["basis"]["columns"] == ["subtotal"]
        assert first["projection"]["basis"]["version"] == f"{m1.plan_code}#v1"

        # 发布新版本列声明：value -> amount
        m2 = engine.create_schema_mapping(
            source_table="SRC-PIN", target_wide_table="WT_PIN",
            field_mapping_json=json.dumps({"value": "amount"}),
        )
        assert m2.version == 2
        engine.publish_schema_mapping(m2.mapping_id)

        # 旧导出中断再续读：仍按首次钉住的 v1 取到 subtotal
        interrupted = engine.interrupt_export(plan.plan_id)
        assert interrupted["ok"] is True
        second = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert second["resumed"] is True
        assert second["complete"] is True
        assert second["exported_rows"] == 1
        assert "subtotal" in second["records"][0]
        assert "amount" not in second["records"][0]
        assert second["projection"]["basis"]["version"] == f"{m1.plan_code}#v1"

        # 新融合作业采用 v2 的 amount
        job_new = engine.create_fusion_job(
            target_wide_table="WT_PIN", participant_sources=["SRC-PIN"],
            join_key="id",
        )
        fused = engine.execute_fusion_pipeline(job_new.job_id)
        assert fused["projection"]["basis"]["version"] == f"{m2.plan_code}#v2"
        assert all("amount" in rec for rec in fused["records"])
        assert all("subtotal" not in rec for rec in fused["records"])

    def test_completed_export_repins_basis_on_next_run(
            self, engine: FusionEngine) -> None:
        """交付完成后再次导出视为新一轮：重新钉住当前生效版本。"""
        engine.register_source(
            source_code="SRC-RP", source_name="rp_source", source_type="MySQL",
            connection_string="conn://rp",
        )
        m1 = engine.create_schema_mapping(
            "SRC-RP", "WT_RP", json.dumps({"value": "subtotal"}))
        job = engine.create_fusion_job(
            target_wide_table="WT_RP", participant_sources=["SRC-RP"], join_key="id")
        engine.execute_fusion_pipeline(job.job_id)
        job.merged_records = job.merged_records[:2]
        snap = engine.create_snapshot("WT_RP", partition_key_value="20261120")
        plan = engine.create_export_plan(
            target_wide_table="WT_RP", field_filter=["value"], row_limit=1)

        first = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert first["projection"]["basis"]["version"] == f"{m1.plan_code}#v1"
        second = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert second["complete"] is True

        m2 = engine.create_schema_mapping(
            "SRC-RP", "WT_RP", json.dumps({"value": "amount"}))
        engine.publish_schema_mapping(m2.mapping_id)

        third = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert third["resumed"] is False
        assert third["projection"]["basis"]["version"] == f"{m2.plan_code}#v2"
        # 旧分区没有 amount 字段：不完整证据能识别涉及的行（本次交付一行）
        assert third["projection"]["defaults_applied"] == 1
        assert third["projection"]["incomplete_rows"] == [
            {"row": "SRC-RP-rec-1", "missing_fields": ["amount"]}]

    def test_row_limit_caps_each_delivery(self, engine: FusionEngine) -> None:
        """每次交付的最大行数保持为 row_limit。"""
        engine.register_source(
            source_code="SRC-RL2", source_name="rl2_source", source_type="MySQL",
            connection_string="conn://rl2",
        )
        job = engine.create_fusion_job(
            target_wide_table="WT_RL2", participant_sources=["SRC-RL2"],
            join_key="id",
        )
        engine.execute_fusion_pipeline(job.job_id)
        snap = engine.create_snapshot("WT_RL2", partition_key_value="20261120")
        plan = engine.create_export_plan(target_wide_table="WT_RL2", row_limit=2)

        r1 = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert r1["exported_rows"] == 2
        assert r1["complete"] is False
        r2 = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert r2["resumed"] is True
        assert r2["exported_rows"] == 1
        assert r2["complete"] is True
        assert r2["delivered_rows"] == 3

    def test_interrupt_only_while_exporting(self, engine: FusionEngine) -> None:
        """未在导出中的计划不能中断。"""
        plan = engine.create_export_plan(target_wide_table="WT_IDLE")
        result = engine.interrupt_export(plan.plan_id)
        assert result["ok"] is False
        assert engine.interrupt_export(99999)["ok"] is False


class TestMissingValueDefault:
    """缺值样例：缺省行为保持——缺失字段不写入、值类型不变、涉及行可识别。"""

    def test_mapping_default_skips_missing_field(
            self, engine: FusionEngine) -> None:
        engine.register_source(
            source_code="SRC-MV", source_name="mv_source", source_type="MySQL",
            connection_string="conn://mv",
        )
        engine.create_schema_mapping(
            "SRC-MV", "WT_MV", json.dumps({"value": "subtotal"}))
        records = [
            {"_source": "SRC-MV", "id": "mv-1", "value": 100.5},
            {"_source": "SRC-MV", "id": "mv-2"},  # 缺 value
        ]
        outcome = engine._project_records_with_mappings(records, "WT_MV")
        # 缺省行为：缺失字段不写入结果，值类型保持
        assert outcome.records[0]["subtotal"] == 100.5
        assert isinstance(outcome.records[0]["subtotal"], float)
        assert "subtotal" not in outcome.records[1]
        # 不完整证据识别涉及的行
        assert outcome.incomplete_rows == [
            {"row": "mv-2", "missing_fields": ["value"]}]
        assert outcome.defaults_applied == 1

    def test_export_default_skips_missing_field(
            self, engine: FusionEngine) -> None:
        engine.register_source(
            source_code="SRC-EM", source_name="em_source", source_type="MySQL",
            connection_string="conn://em",
        )
        engine.create_schema_mapping(
            "SRC-EM", "WT_EM", json.dumps({"value": "subtotal"}))
        job = engine.create_fusion_job(
            target_wide_table="WT_EM", participant_sources=["SRC-EM"],
            join_key="id",
        )
        engine.execute_fusion_pipeline(job.job_id)
        # 构造缺值样例：第二行缺 subtotal
        job.merged_records = [dict(rec) for rec in job.merged_records]
        del job.merged_records[1]["subtotal"]

        snap = engine.create_snapshot("WT_EM", partition_key_value="20261120")
        plan = engine.create_export_plan(
            target_wide_table="WT_EM", field_filter=["value"])
        result = engine.execute_export_from_snapshot(plan.plan_id, snap.partition_id)
        assert result["ok"] is True
        # 实际字段按投影依据解析为 subtotal；缺省行不写入且被识别
        assert result["records"][0]["subtotal"] == 100.0
        assert isinstance(result["records"][0]["subtotal"], float)
        assert "subtotal" not in result["records"][1]
        assert result["projection"]["incomplete_rows"] == [
            {"row": "SRC-EM-rec-2", "missing_fields": ["subtotal"]}]
