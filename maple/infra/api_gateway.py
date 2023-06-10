from typing import Literal

from aws_cdk import aws_apigatewayv2_alpha as apigw
from aws_cdk import aws_apigatewayv2_integrations_alpha as apigw_integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk.aws_apigatewayv2 import CfnStage
from constructs import Construct

EnvName = Literal["dev", "prod"]


class ApiGateway(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        vpc: ec2.Vpc,
    ) -> None:
        super().__init__(
            scope,
            id,
        )

        # Ingress
        # Create HTTP API Gateway to handle incoming HTTPS traffic
        self.http_api: apigw.HttpApi = apigw.HttpApi(self, "HttpApi")

        # Link between Ingress and Services
        self.vpc_link: apigw.VpcLink = apigw.VpcLink(self, "VpcLink", vpc=vpc)

        # Production site uses the default route without any prefix
        self.prod_api: apigw.HttpApi = apigw.HttpApi(self, "maple-api-prod")

        # Dev site uses the /dev prefixs
        self.dev_api: apigw.HttpApi = apigw.HttpApi(self, "maple-api-dev")

    def get(self, env: EnvName) -> apigw.HttpApi:
        if env == "prod":
            return self.prod_api
        elif env == "dev":
            return self.dev_api
        else:
            raise ValueError(f"Invalid env: {env}")
