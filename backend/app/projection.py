"""字段投影协议：字段映射与快照导出共用的取字段骨架。

字段映射（schema_map）与快照导出（export_snapshot）原本各写了一遍取字段逻辑，
本模块把两份实现里相同的部分收拢成一份共用协议：

- 缺省处理：所选列在行内不存在时不写入结果，保持既有缺省行为与值类型；
- 投影校验：逐行核对所选列，读到不完整证据时识别涉及的行；
- 投影依据：每次取数记录实际采用的列与明细版本，供事后核对。

列选择留给各调用方：字段映射按重命名明细取列（rename_records），
快照导出按字段筛选解析实际字段（resolve_columns + project_records）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# 缺省哨兵：区分“列不存在”（走缺省处理）与“列存在但值为 None”（原样保留）。
MISSING: Any = object()


def default_row_key(record: dict[str, Any], index: int) -> Any:
    """默认行标识：优先 id / _fusion_key，否则用行号，保证不完整证据能定位到行。"""
    for key in ("id", "_fusion_key"):
        if key in record:
            return record[key]
    return f"row#{index}"


@dataclass(frozen=True)
class ProjectionBasis:
    """字段投影依据：一次取数实际采用的列选择与明细版本。"""
    origin: str                      # 取数来源：schema_map / export_snapshot
    columns: tuple[str, ...]         # 实际选择的列
    version: str                     # 列声明的明细版本（如 SCHE-0001#v2，无映射时为“无映射”）
    detail: dict[str, str]           # 明细版本内容（源字段 -> 目标字段）
    requested: tuple[str, ...] = ()  # 调用方请求的列（解析前）

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "requested": list(self.requested),
            "columns": list(self.columns),
            "version": self.version,
            "detail": dict(self.detail),
        }


@dataclass
class ProjectionOutcome:
    """一次取数的结果与投影核对信息。"""
    records: list[dict[str, Any]]
    basis: ProjectionBasis
    defaults_applied: int = 0
    incomplete_rows: list[dict[str, Any]] = field(default_factory=list)

    def audit(self) -> dict[str, Any]:
        """投影依据核对视图：版本、实际列、缺省次数与不完整证据涉及的行。"""
        return {
            "basis": self.basis.as_dict(),
            "row_count": len(self.records),
            "defaults_applied": self.defaults_applied,
            "incomplete_rows": [dict(item) for item in self.incomplete_rows],
        }


RowKey = Callable[[dict[str, Any], int], Any]


class FieldProjectionProtocol:
    """共用取字段协议：缺省处理与投影校验一份实现，列选择各自给出。"""

    # ---- 缺省处理 ----

    def fetch_field(self, record: dict[str, Any], column: str) -> Any:
        """取单个字段；列不存在时返回缺省哨兵（不写入结果），值类型原样保留。"""
        return record.get(column, MISSING)

    # ---- 列选择：按投影依据的明细版本解析实际字段 ----

    def resolve_columns(self, requested: Iterable[str],
                        detail: dict[str, str]) -> list[str]:
        """把请求的列按明细版本解析为实际字段；未出现在明细中的列保持原名。"""
        return [detail.get(column, column) for column in requested]

    # ---- 投影校验 ----

    def validate_row(self, record: dict[str, Any],
                     columns: Iterable[str]) -> list[str]:
        """校验一行缺少哪些所选列，缺列即不完整证据。"""
        return [column for column in columns
                if self.fetch_field(record, column) is MISSING]

    def project_records(
        self,
        records: Iterable[dict[str, Any]],
        columns: Iterable[str],
        *,
        basis: ProjectionBasis,
        row_key: RowKey = default_row_key,
    ) -> ProjectionOutcome:
        """按所选列投影多行：缺省列不写入，缺列行计入不完整证据。"""
        selected = list(columns)
        projected_rows: list[dict[str, Any]] = []
        defaults_applied = 0
        incomplete: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            projected: dict[str, Any] = {}
            for column in selected:
                value = self.fetch_field(record, column)
                if value is MISSING:
                    continue
                projected[column] = value
            missing = self.validate_row(record, selected)
            if missing:
                defaults_applied += len(missing)
                incomplete.append({
                    "row": row_key(record, index),
                    "missing_fields": missing,
                })
            projected_rows.append(projected)
        return ProjectionOutcome(projected_rows, basis, defaults_applied, incomplete)

    def rename_records(
        self,
        records: Iterable[dict[str, Any]],
        renames: dict[str, str],
        *,
        basis: ProjectionBasis,
        row_key: RowKey = default_row_key,
    ) -> ProjectionOutcome:
        """按重命名明细取字段（字段映射的列选择），缺省与校验同 project_records。"""
        renamed_rows: list[dict[str, Any]] = []
        defaults_applied = 0
        incomplete: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            new_record = dict(record)
            missing: list[str] = []
            for src_field, tgt_field in renames.items():
                value = self.fetch_field(new_record, src_field)
                if value is MISSING:
                    missing.append(src_field)
                    continue
                del new_record[src_field]
                new_record[tgt_field] = value
            if missing:
                defaults_applied += len(missing)
                incomplete.append({
                    "row": row_key(record, index),
                    "missing_fields": missing,
                })
            renamed_rows.append(new_record)
        return ProjectionOutcome(renamed_rows, basis, defaults_applied, incomplete)


# 共用单例：字段映射与快照导出各取所需，缺省处理与投影校验只有一份。
field_projector = FieldProjectionProtocol()
