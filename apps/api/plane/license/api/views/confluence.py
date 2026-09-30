from rest_framework.response import Response
from plane.license.api.views.base import BaseAPIView
from plane.utils.confluence.client import ConfluenceClient, ConfluenceError
from plane.utils.confluence.config import get_confluence_config, public_config, save_confluence_config


class ConfluenceConfigurationEndpoint(BaseAPIView):
    def get(self, request):
        return Response(public_config(get_confluence_config()))

    def patch(self, request):
        try:
            return Response(save_confluence_config(request.data))
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)


class ConfluenceConnectionTestEndpoint(BaseAPIView):
    def post(self, request):
        try:
            config = get_confluence_config(require_enabled=True)
            result = ConfluenceClient(config).json("/wiki/api/v2/spaces", {"limit": 1})
            return Response(
                {"message": "Connected to Atlassian successfully.", "visible_spaces": len(result.get("results", []))}
            )
        except ConfluenceError as exc:
            return Response({"code": exc.code, "error": exc.message}, status=400)
