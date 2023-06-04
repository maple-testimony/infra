#!/usr/bin/env python3
import os

import aws_cdk as cdk

from maple.infra.cicd_stack import CiCdStack

app = cdk.App()

root_account = cdk.Environment(account=app.node.get_context("root_account_arn"), 
                               region=app.node.get_context("primary_region"))


CiCdStack(
    app,
    "Maple",
    # admin@mapletestimony.org
    env=root_account,
)

app.synth()
