import os

from aws_cdk import IgnoreMode, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import pipelines

from .maple_application_stage import MapleApplication


class CiCdStack(Stack):
    def __init__(self, scope, id, **kwargs):
        super().__init__(scope, id, **kwargs)

        pipeline: pipelines.CodePipeline = pipelines.CodePipeline(
            self,
            "CodePipeline",
            pipeline_name="maple-cicd",
            self_mutation=True,
            synth=pipelines.ShellStep(
                "Synth",
                input=pipelines.CodePipelineSource.connection(
                    "maple-testimony/infra",
                    "main",
                    connection_arn=self.node.get_context("code_connection_arn"),
                    trigger_on_push=True,
                ),
                commands=[
                    "pip install poetry",
                    "poetry lock --check",
                    "poetry install --only main",
                    "npm install -g aws-cdk",
                    "cdk synth",
                ],
            ),
        )

        pipeline.add_stage(MapleApplication(self, "App"))

        # pipeline.add_stage(MapleApplication(self, "Dev"))
        # pipeline.add_stage(MapleApplication(self, "Prod"), pre=[pipelines.ManualApprovalStep("PromoteToProd")])
