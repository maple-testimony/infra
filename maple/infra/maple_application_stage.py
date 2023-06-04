from aws_cdk import CfnOutput, Stage

from .lobbying_stack import LobbyingStack
from .search_stack import SearchStack
from .shared_stack import SharedStack


class MapleApplication(Stage):
    """All the resources that make up maple."""

    def __init__(self, scope, id, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.shared: SharedStack = SharedStack(self, "SharedStack")
        s = self.shared

        self.search: SearchStack = SearchStack(
            self,
            "SearchStack",
            api=s.api,
            cluster=s.cluster,
            vpc=s.vpc,
        )

        self.lobbying: LobbyingStack = LobbyingStack(
            self,
            "LobbyingStack",
            cluster=s.cluster,
            db=s.db,
        )
