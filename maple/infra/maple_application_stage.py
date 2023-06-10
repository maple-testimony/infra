from aws_cdk import CfnOutput, Stage
from aws_cdk import aws_elasticloadbalancingv2 as elbv2

from maple.infra.api_gateway import ApiGateway

from .lobbying_stack import LobbyingStack
from .search_api import SearchApi
from .shared_stack import SharedStack


class MapleApplication(Stage):
    """All the resources that make up maple.

    The resources are physically organized into one shared stack and any number
    of other loosely coupled stacks.

    The shared stack sets up shared resources, and then other logical constructs
    have these dependencies injected by this class.
    """

    def __init__(self, scope, id, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.shared: SharedStack = SharedStack(self, "SharedStack")
        base = self.shared

        self.prod_search: SearchApi = SearchApi(
            base,
            "SearchApi",
            env_name="prod",
            api=base.api,
            cluster=base.cluster,
        )

        self.dev_search: SearchApi = SearchApi(
            self.shared,
            "DevSearchApi",
            env_name="dev",
            api=base.api,
            cluster=base.cluster,
        )

        self.lobbying: LobbyingStack = LobbyingStack(
            self,
            "LobbyingStack",
            cluster=base.cluster,
            db=base.db,
        )
