from django.urls import path
from plane.app.views.jira import JiraProjectsEndpoint, JiraMembersEndpoint, JiraRunsEndpoint, JiraRunDetailEndpoint

urlpatterns = [
    path("workspaces/<str:slug>/projects/<uuid:project_id>/jira/projects/", JiraProjectsEndpoint.as_view()),
    path("workspaces/<str:slug>/projects/<uuid:project_id>/jira/members/", JiraMembersEndpoint.as_view()),
    path("workspaces/<str:slug>/projects/<uuid:project_id>/jira/runs/", JiraRunsEndpoint.as_view()),
    path("workspaces/<str:slug>/projects/<uuid:project_id>/jira/runs/<uuid:run_id>/", JiraRunDetailEndpoint.as_view()),
]
