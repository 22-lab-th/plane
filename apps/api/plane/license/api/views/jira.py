from rest_framework.response import Response
from plane.license.api.views.base import BaseAPIView
from plane.utils.jira.client import JiraClient
from plane.utils.confluence.client import ConfluenceError
from plane.utils.jira.config import get_jira_config, public_config, save_jira_config


class JiraConfigurationEndpoint(BaseAPIView):
    def get(self, request):
        return Response(public_config(get_jira_config()))

    def patch(self, request):
        try:
            return Response(save_jira_config(request.data))
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)


class JiraConnectionTestEndpoint(BaseAPIView):
    def post(self, request):
        try:
            config = get_jira_config(require_enabled=True)
            result = JiraClient(config).json("/rest/api/3/project/search", {"maxResults": 1})
            return Response(
                {"message": "Connected to Atlassian successfully.", "visible_projects": len(result.get("values", []))}
            )
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)
