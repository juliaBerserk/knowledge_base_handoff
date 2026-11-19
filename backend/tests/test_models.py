from estafeta.models import Base


def test_organization_metadata_contains_required_tables() -> None:
    assert set(Base.metadata.tables) == {
        "departments",
        "credentials",
        "invitations",
        "auth_sessions",
        "audit_events",
        "totp_credentials",
        "redaction_events",
        "sanitization_policies",
        "login_rate_limits",
        "knowledge_records",
        "build_jobs",
        "chunks",
        "record_sources",
        "materials",
        "material_versions",
        "positions",
        "users",
        "employment_assignments",
    }


def test_one_current_assignment_per_user_is_a_partial_unique_index() -> None:
    assignments = Base.metadata.tables["employment_assignments"]
    index = next(
        index
        for index in assignments.indexes
        if index.name == "uq_assignments_one_current_per_user"
    )

    assert index.unique is True
    assert str(index.dialect_options["postgresql"]["where"]) == "ends_at IS NULL"
