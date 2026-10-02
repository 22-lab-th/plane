"""Token-authenticated access to 22lab features, using the existing handlers.

Only explicitly selected routes are published. Session login, registration and
interactive authentication callbacks never become token endpoints.
"""

from django.urls import path
from rest_framework.exceptions import AuthenticationFailed

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle
from plane.app.urls.file import urlpatterns as file_routes
from plane.app.urls.jira import urlpatterns as jira_routes
from plane.app.urls.page import urlpatterns as page_routes
from plane.app.urls.workspace import urlpatterns as workspace_routes
from plane.app.views.page.base import PageViewSet
from plane.db.models import APIToken, WorkspaceMember
from plane.license.api.views.confluence import ConfluenceConfigurationEndpoint, ConfluenceConnectionTestEndpoint
from plane.license.api.views.jira import JiraConfigurationEndpoint, JiraConnectionTestEndpoint


class ExtensionTokenAuthentication(APIKeyAuthentication):
    def authenticate(self, request):
        authenticated = super().authenticate(request)
        if authenticated is None:
            return None
        user, token = authenticated
        slug = request.parser_context.get("kwargs", {}).get("slug")
        record = APIToken.objects.select_related("workspace").get(token=token)
        if slug:
            if record.workspace_id and record.workspace.slug != slug:
                raise AuthenticationFailed("This API token belongs to another workspace.")
            if not WorkspaceMember.objects.filter(member=user, workspace__slug=slug, is_active=True).exists():
                raise AuthenticationFailed("Active workspace membership is required.")
        elif record.workspace_id:
            # A workspace credential must never acquire instance administration.
            raise AuthenticationFailed("Instance operations require an unscoped administrator API token.")
        return authenticated


class TokenSurfaceMixin:
    authentication_classes = [ExtensionTokenAuthentication]

    def get_throttles(self):
        # Keep operation-specific upload/delivery throttles as well as API limits.
        return [ApiKeyRateThrottle(), *super().get_throttles()]


def token_route(route):
    view = route.callback.view_class if hasattr(route.callback, "view_class") else route.callback.cls
    token_view = type(f"Token{view.__name__}", (TokenSurfaceMixin, view), {"__module__": __name__})
    actions = getattr(route.callback, "actions", None)
    callback = token_view.as_view(actions) if actions else token_view.as_view()
    return path(str(route.pattern), callback, name=f"public-{route.name}" if route.name else None)


# Existing public page CRUD/archive endpoints stay authoritative. Add only the
# operations they do not provide, preserving ownership/tree/version checks.
page_operations = (
    "/confluence/",
    "/pages-summary/",
    "/favorite-pages/",
    "/lock/",
    "/access/",
    "/move/",
    "/versions/",
    "/duplicate/",
)
urlpatterns = [
    *(token_route(route) for route in file_routes),
    *(token_route(route) for route in jira_routes),
    *(token_route(route) for route in page_routes if any(part in str(route.pattern) for part in page_operations)),
    *(
        token_route(route)
        for route in workspace_routes
        if "/bookmarks/" in str(route.pattern) or "/bookmark-groups/" in str(route.pattern)
    ),
]

for suffix, view in (
    ("confluence/", ConfluenceConfigurationEndpoint),
    ("confluence/test/", ConfluenceConnectionTestEndpoint),
    ("jira/", JiraConfigurationEndpoint),
    ("jira/test/", JiraConnectionTestEndpoint),
):
    admin_view = type(f"Token{view.__name__}", (TokenSurfaceMixin, view), {"__module__": __name__})
    urlpatterns.append(path(f"instance/{suffix}", admin_view.as_view()))

DeletePage = type("TokenPageDeleteViewSet", (TokenSurfaceMixin, PageViewSet), {"__module__": __name__})
urlpatterns.append(
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/delete/",
        DeletePage.as_view({"delete": "destroy"}),
        name="public-page-delete",
    )
)
