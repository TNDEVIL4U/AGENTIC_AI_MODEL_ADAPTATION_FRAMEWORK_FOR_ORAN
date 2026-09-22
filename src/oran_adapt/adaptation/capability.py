"""Member 3 - capability: given what the inspector found and what the drifted data actually
looks like, decide what this specific model artifact can be adapted to do. This is where a
strategy Member 2 approved in the abstract gets checked against the concrete artifact."""

from __future__ import annotations

from oran_adapt.adaptation.schemas import CapabilityAssessment, ModelInspection, SchemaCompatibility


def assess_schema_compatibility(
    inspection: ModelInspection, feature_names: list[str]
) -> SchemaCompatibility:
    if not inspection.feature_names_in:
        return SchemaCompatibility(
            compatible=True,
            reason="model does not expose feature names; schema check skipped",
        )

    expected = set(inspection.feature_names_in)
    observed = set(feature_names)
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)

    if missing:
        return SchemaCompatibility(
            compatible=False,
            missing_features=missing,
            extra_features=extra,
            reason=f"drifted data is missing feature(s) the model requires: {missing}",
        )

    reason = "all required features are present in the drifted data"
    if extra:
        reason += f"; {len(extra)} extra feature(s) will be ignored"
    return SchemaCompatibility(compatible=True, extra_features=extra, reason=reason)


def assess_capability(
    inspection: ModelInspection, feature_names: list[str]
) -> CapabilityAssessment:
    schema_check = assess_schema_compatibility(inspection, feature_names)
    can_fine_tune = inspection.supports_partial_fit or inspection.supports_warm_start

    if not schema_check.compatible:
        reason = f"schema incompatible: {schema_check.reason}"
    elif can_fine_tune:
        reason = f"{inspection.model_class} supports incremental updates"
    else:
        reason = (
            f"{inspection.model_class} has no incremental-update mechanism; "
            "only full retraining is possible"
        )

    return CapabilityAssessment(
        supports_fine_tuning=can_fine_tune and schema_check.compatible,
        supports_full_retraining=schema_check.compatible,
        schema_check=schema_check,
        reason=reason,
    )
