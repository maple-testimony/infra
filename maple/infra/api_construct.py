from aws_cdk import aws_apigatewayv2_alpha as apigw
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct


class Api(Construct):
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

        # Routes traffic to services
        # Create an Application Load Balancer. This routes HTTP traffic to services running on ECS.
        self.load_balancer: elbv2.ApplicationLoadBalancer = (
            elbv2.ApplicationLoadBalancer(
                self, "LoadBalancer", vpc=vpc, internet_facing=False
            )
        )
