"""Standalone CDK app for the budget, deployed before bootstrap (NFR-COST-01, C-02).

Run from the repository root: ``python -m infra.budget_app``. Inputs come from
``AERA_ENV`` and the ``AERA_BUDGET_*`` environment variables.
"""

import os

from aws_cdk import App, LegacyStackSynthesizer

from infra.environments import require_deployable_environment
from infra.stacks.budget import BudgetSettings, BudgetStack, settings_from_environment


def budget_stack_name(env_name: str) -> str:
    return f"aera-{env_name}-budget"


def build_app(*, env_name: str, settings: BudgetSettings) -> App:
    require_deployable_environment(env_name)
    app = App(analytics_reporting=False)
    BudgetStack(
        app,
        budget_stack_name(env_name),
        settings=settings,
        # Inline template deployed with the caller's credentials: no bootstrap role,
        # assets bucket or version check. The stack has no assets, so none is needed.
        synthesizer=LegacyStackSynthesizer(),
        description="AERA monthly cost budget and actual-spend alerts (NFR-COST-01)",
    )
    return app


def main() -> None:
    settings = settings_from_environment(os.environ)
    build_app(env_name=os.environ.get("AERA_ENV", ""), settings=settings).synth()


if __name__ == "__main__":
    main()
