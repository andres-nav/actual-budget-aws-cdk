#!/usr/bin/env python3

import os

import aws_cdk as cdk

from actual_budget_cdk.actual_budget_cdk_stack import ActualBudgetCdkStack

app = cdk.App()
ActualBudgetCdkStack(app, "ActualBudgetCdkStack",
                     env=cdk.Environment(region='eu-west-1')),

app.synth()
