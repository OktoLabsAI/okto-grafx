"""RF06 quota workload definitions; functional boundaries are not timing evidence.

Setup uses an unrestricted handle. The measured handle receives the explicit
positive quota through connect(), so setup cannot consume or fail that quota.
Zero *remaining* resources arise after consuming a positive limit; zero is not
a supported public configuration. Expected refusals are deterministic tests,
never successful temporal samples. Memory-budget controls retain scalar landing.
"""
from __future__ import annotations

FIELDS = ("max_intermediate_rows", "max_traversal_paths", "max_traversal_expansions")


def validate(scenario):
    if scenario.quota_field is None:
        if scenario.quota_limit is not None:
            raise ValueError("quota limit requires an explicit quota field")
        return
    if (scenario.quota_field not in FIELDS or type(scenario.quota_limit) is not int
            or scenario.quota_limit < 1):
        raise ValueError("quota field and positive integer limit required")
    if (scenario.family != "landing" or scenario.reader != "short"
            or scenario.churn_percent or scenario.writers != 1 or scenario.cursor_batch_size):
        raise ValueError("RF06 quotas require one blocking landing statement without churn")


def connection_options(scenario):
    validate(scenario)
    return {} if scenario.quota_field is None else {scenario.quota_field: scenario.quota_limit}


def expected_outcome(scenario):
    """Input-derived oracle for the catalog's one-hop, unfiltered topology."""
    validate(scenario)
    return ("quota_refusal" if scenario.quota_limit is not None
            and scenario.fanout > scenario.quota_limit else "success")


def contract(scenario):
    return {"quota": connection_options(scenario), "expected_outcome": expected_outcome(scenario),
            "quota_setup": "fixture created without traversal quotas; measured handle configured explicitly",
            "qualification_limit": "expected refusals are functional evidence only; temporal qualification remains success-only and requires its memory protocol",
            "memory_regime": "configured query memory budget retains scalar guard; unbudgeted batch has no logical peak measurement"}


def matrix(scenario_type):
    result = []
    for field in FIELDS:
        short = field.removeprefix("max_")
        for count in (1, 63, 64, 65, 128):
            result.append(scenario_type(f"rf06-{short}-n{count}", "landing",
                                        nodes=max(count, 2), edges_per_node=0, fanout=count,
                                        quota_field=field, quota_limit=count,
                                        role="target" if count > 1 else "control",
                                        work_counter="certificate_attempts" if count > 1 else None))
        result.append(scenario_type(f"rf06-{short}-memory-control", "landing",
                                    nodes=65, edges_per_node=0, fanout=65,
                                    quota_field=field, quota_limit=65,
                                    query_memory_budget_bytes=1048576))
    return result
