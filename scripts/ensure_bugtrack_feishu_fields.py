#!/usr/bin/env python3
"""Idempotently ensure fields required by relational Bug synchronization."""

from __future__ import annotations

from app.core.config import settings
from app.services.feishu_bitable import create_field, list_fields


REQUIRED_FIELDS = {
    "Bug截图": 17,
    "业务草稿ID": 1,
}


def main() -> None:
    fields = list_fields(
        settings.bugtrack.feishu_app_token,
        settings.bugtrack.feishu_table_id,
    )
    existing = {str(field.get("field_name") or ""): field for field in fields}
    for name, field_type in REQUIRED_FIELDS.items():
        current = existing.get(name)
        if current is not None:
            actual = int(current.get("type") or 0)
            if actual != field_type:
                raise RuntimeError(
                    f"field {name} exists with type={actual}, expected={field_type}"
                )
            print(f"ok {name} type={actual}")
            continue
        created = create_field(name, field_type)
        print(f"created {name} field_id={created.get('field_id', '')}")


if __name__ == "__main__":
    main()

