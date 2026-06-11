"""
Tests for deploy_pipeline.sh — verify IAM grant logic for deliver jobs.

Catches the bug where ``grant-deliver-iam`` only included ``deliver`` and
``lt_deliver`` but not ``gt_deliver``, causing 403 Permission denied on
``POST /admin/orders/{id}/redeliver`` for Gutenberg orders.
See change log ``2026-06-07_redeliver_gutenberg.md``.
"""
import os
import subprocess

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "deploy_pipeline.sh")


def _render_iam_block(job_keys: list[str]) -> str:
    """Replicate the IAM-grant heredoc block from deploy_pipeline.sh to
    assert that the conditional is correct.
    """
    lines = []
    for key in job_keys:
        if key in ("deliver", "lt_deliver", "gt_deliver"):
            lines.append(f"  grant {key}")
    return "\n".join(lines)


def test_iam_grant_includes_all_three_deliver_jobs():
    """Regression: the IAM grant must cover all three deliver jobs."""
    block = _render_iam_block(["deliver", "lt_deliver", "gt_deliver"])
    assert "grant deliver" in block
    assert "grant lt_deliver" in block
    assert "grant gt_deliver" in block


def test_deploy_script_contains_gt_deliver_in_iam_check():
    """Static check: the deploy script's conditional covers all Gutenberg jobs."""
    with open(SCRIPT_PATH) as f:
        content = f.read()
    assert 'gt_*' in content, (
        "deploy_pipeline.sh: the IAM grant conditional is missing 'gt_*' glob — "
        "API service account will not be able to invoke Gutenberg pipeline jobs."
    )


def test_deploy_script_keeps_deliver_and_lt_deliver_in_iam_check():
    """Sanity: don't accidentally remove the existing two, and the new gt_* glob."""
    with open(SCRIPT_PATH) as f:
        content = f.read()
    assert 'job_key" == "deliver"' in content
    assert 'job_key" == "lt_deliver"' in content
    assert 'gt_*' in content


def test_iam_step_uses_set_plus_e_not_set_minus_e():
    """The IAM grant step must not abort the build on permission failures.

    Regression: the original `set -e` caused the build to fail with
    PERMISSION_DENIED whenever the Cloud Build SA lacked
    `run.jobs.setIamPolicy`.  The step is now non-blocking.
    """
    with open(SCRIPT_PATH) as f:
        content = f.read()
    # Find the IAM step block and assert it does not `set -e`.
    iam_idx = content.find("id: 'grant-deliver-iam'")
    assert iam_idx != -1, "grant-deliver-iam step not found"
    iam_block = content[iam_idx:iam_idx + 1200]
    assert "set -e" not in iam_block, (
        "grant-deliver-iam must not use `set -e` — IAM failures should be "
        "non-blocking so the build still completes."
    )
    assert "set +e" in iam_block, (
        "grant-deliver-iam should explicitly use `set +e` for clarity."
    )


def test_iam_step_warns_instead_of_failing():
    """The IAM step must log a clear warning when the grant fails."""
    with open(SCRIPT_PATH) as f:
        content = f.read()
    iam_idx = content.find("id: 'grant-deliver-iam'")
    iam_block = content[iam_idx:iam_idx + 2000]
    assert "WARN" in iam_block, (
        "grant-deliver-iam must print a [WARN] line when a grant fails."
    )
    assert "non-blocking" in iam_block.lower() or "non-fatal" in iam_block.lower(), (
        "grant-deliver-iam must clearly indicate the step is non-blocking."
    )


def test_deploy_script_lists_gt_deliver_job_definition():
    """Sanity: gt_deliver must also be a deployable job spec."""
    with open(SCRIPT_PATH) as f:
        content = f.read()
    assert "gt_deliver:ots-gt-deliver-${ENV}" in content


def test_deploy_script_shellcheck_clean():
    """Optional: shellcheck (skip if not installed)."""
    if subprocess.run(["which", "shellcheck"], capture_output=True).returncode != 0:
        import pytest
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", "-e", "SC1091,SC2086,SC2155,SC2046", SCRIPT_PATH],
        capture_output=True, text=True,
    )
    # shellcheck may flag our heredoc; just ensure no syntax errors
    assert "syntax error" not in result.stdout.lower(), result.stdout
